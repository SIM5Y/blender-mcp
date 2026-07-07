# blender_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context, Image
import socket
import json
import asyncio
import logging
import tempfile
import threading
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List, Union
import os
import sys
from pathlib import Path
import base64
from urllib.parse import urlparse

# Import telemetry
from .telemetry import record_startup, get_telemetry, EventType
from .telemetry_decorator import telemetry_tool, rich_telemetry_tool

# Configure logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("BlenderMCPServer")

# Default configuration
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9876

# Platform delivery presets for VSE encodes.
# Keep in sync with DELIVERY_PRESETS in addon.py.
DELIVERY_PRESETS = {
    "LINKEDIN_WIDE": {"resolution": [1920, 1080], "fps": 25},
    "SQUARE": {"resolution": [1080, 1080], "fps": 25},
    "VERTICAL": {"resolution": [1080, 1920], "fps": 25},
}

# ---------------------------------------------------------------------------
# Session token awareness: approximate token burn of this chat's tool output.
# est_tokens ~= response bytes / 4, plus (width*height)/750 per returned image.
# ---------------------------------------------------------------------------
_SESSION_STATS = {"tool_calls": 0, "est_tokens": 0, "last_advisory_at": 0}

# Advisory thresholds: first at 50k est. tokens, then every additional 30k
_ADVISORY_FIRST = 50_000
_ADVISORY_STEP = 30_000


def _note_image_tokens(width, height):
    """Add the approximate token cost of a returned image to the session stats."""
    try:
        if width and height:
            _SESSION_STATS["est_tokens"] += (int(width) * int(height)) // 750
    except (TypeError, ValueError):
        pass


def _pending_advisory():
    """Return the Save & Continue advisory once per crossed token threshold, else None."""
    est = _SESSION_STATS["est_tokens"]
    if est < _ADVISORY_FIRST:
        return None
    threshold = _ADVISORY_FIRST + ((est - _ADVISORY_FIRST) // _ADVISORY_STEP) * _ADVISORY_STEP
    if threshold <= _SESSION_STATS.get("last_advisory_at", 0):
        return None
    _SESSION_STATS["last_advisory_at"] = threshold
    return (
        f"[Session advisory for the user: this chat has consumed roughly "
        f"~{est // 1000}k tokens of tool output. To conserve the chat's token "
        f"allowance, consider Save & Continue: save the project (manage_project), "
        f"write a handoff (manage_assignment action='handoff'), then continue in "
        f"a NEW chat — the next agent reads the assignment file and scene "
        f"directly for a fraction of the tokens.]"
    )


def _with_advisory(text: str) -> str:
    """Append the session advisory to a tool's text response at threshold crossings."""
    advisory = _pending_advisory()
    if advisory:
        return f"{text}\n\n---\n{advisory}"
    return text

@dataclass
class BlenderConnection:
    host: str
    port: int
    sock: socket.socket = None  # Changed from 'socket' to 'sock' to avoid naming conflict
    lock: threading.Lock = field(default_factory=threading.Lock)  # Serializes send_command calls

    def connect(self) -> bool:
        """Connect to the Blender addon socket server"""
        if self.sock:
            return True
            
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            logger.info(f"Connected to Blender at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Blender: {str(e)}")
            self.sock = None
            return False
    
    def disconnect(self):
        """Disconnect from the Blender addon"""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Blender: {str(e)}")
            finally:
                self.sock = None

    def receive_full_response(self, sock, buffer_size=8192):
        """Receive the complete response, potentially in multiple chunks"""
        chunks = []
        # Use a consistent timeout value that matches the addon's timeout
        sock.settimeout(180.0)  # Match the addon's timeout
        
        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        # If we get an empty chunk, the connection might be closed
                        if not chunks:  # If we haven't received anything yet, this is an error
                            raise Exception("Connection closed before receiving any data")
                        break
                    
                    chunks.append(chunk)
                    
                    # Check if we've received a complete JSON object
                    try:
                        data = b''.join(chunks)
                        json.loads(data.decode('utf-8'))
                        # If we get here, it parsed successfully
                        logger.info(f"Received complete response ({len(data)} bytes)")
                        return data
                    except json.JSONDecodeError:
                        # Incomplete JSON, continue receiving
                        continue
                except socket.timeout:
                    # If we hit a timeout during receiving, break the loop and try to use what we have
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Socket connection error during receive: {str(e)}")
                    raise  # Re-raise to be handled by the caller
        except socket.timeout:
            logger.warning("Socket timeout during chunked receive")
        except Exception as e:
            logger.error(f"Error during receive: {str(e)}")
            raise
            
        # If we get here, we either timed out or broke out of the loop
        # Try to use what we have
        if chunks:
            data = b''.join(chunks)
            logger.info(f"Returning data after receive completion ({len(data)} bytes)")
            try:
                # Try to parse what we have
                json.loads(data.decode('utf-8'))
                return data
            except json.JSONDecodeError:
                # If we can't parse it, it's incomplete
                raise Exception("Incomplete JSON response received")
        else:
            raise Exception("No data received")

    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a command to Blender and return the response.

        Thread-safe: the whole request/response exchange is serialized on
        self.lock so concurrent tool calls cannot interleave on the socket.
        """
        with self.lock:
            if not self.sock and not self.connect():
                raise ConnectionError("Not connected to Blender")

            command = {
                "type": command_type,
                "params": params or {}
            }

            try:
                # Log the command being sent
                logger.info(f"Sending command: {command_type} with params: {params}")

                # Send the command
                self.sock.sendall(json.dumps(command).encode('utf-8'))
                logger.info(f"Command sent, waiting for response...")

                # Set a timeout for receiving - use the same timeout as in receive_full_response
                self.sock.settimeout(180.0)  # Match the addon's timeout

                # Receive the response using the improved receive_full_response method
                response_data = self.receive_full_response(self.sock)
                logger.info(f"Received {len(response_data)} bytes of data")

                # Session token awareness (inside the lock, so counts stay consistent)
                _SESSION_STATS["tool_calls"] += 1
                _SESSION_STATS["est_tokens"] += max(len(response_data) // 4, 1)

                response = json.loads(response_data.decode('utf-8'))
                logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")

                if response.get("status") == "error":
                    logger.error(f"Blender error: {response.get('message')}")
                    raise Exception(response.get("message", "Unknown error from Blender"))

                return response.get("result", {})
            except socket.timeout:
                logger.error("Socket timeout while waiting for response from Blender")
                # Don't try to reconnect here - let the get_blender_connection handle reconnection
                # Just invalidate the current socket so it will be recreated next time
                self.sock = None
                raise Exception("Timeout waiting for Blender response - try simplifying your request. If Blender is running headless (blender -b), commands never execute; run Blender with a GUI or via 'xvfb-run -a blender' instead")
            except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                logger.error(f"Socket connection error: {str(e)}")
                self.sock = None
                raise Exception(f"Connection to Blender lost: {str(e)}")
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON response from Blender: {str(e)}")
                # Try to log what was received
                if 'response_data' in locals() and response_data:
                    logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
                raise Exception(f"Invalid response from Blender: {str(e)}")
            except Exception as e:
                logger.error(f"Error communicating with Blender: {str(e)}")
                # Don't try to reconnect here - let the get_blender_connection handle reconnection
                self.sock = None
                raise Exception(f"Communication error with Blender: {str(e)}")

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    # We don't need to create a connection here since we're using the global connection
    # for resources and tools

    try:
        # Just log that we're starting up
        logger.info("BlenderMCP server starting up")

        # Record startup event for telemetry
        try:
            record_startup()
        except Exception as e:
            logger.debug(f"Failed to record startup telemetry: {e}")

        # Try to connect to Blender on startup to verify it's available
        try:
            # This will initialize the global connection if needed
            blender = get_blender_connection()
            logger.info("Successfully connected to Blender on startup")
        except Exception as e:
            logger.warning(f"Could not connect to Blender on startup: {str(e)}")
            logger.warning("Make sure the Blender addon is running before using Blender resources or tools")

        # Return an empty context - we're using the global connection
        yield {}
    finally:
        # Clean up the global connection on shutdown
        global _blender_connection
        if _blender_connection:
            logger.info("Disconnecting from Blender on shutdown")
            _blender_connection.disconnect()
            _blender_connection = None
        logger.info("BlenderMCP server shut down")

# Create the MCP server with lifespan support
mcp = FastMCP(
    "BlenderMCP",
    lifespan=server_lifespan
)

# Resource endpoints

# Global connection for resources (since resources can't access context)
_blender_connection = None

# This server's own version (from installed package metadata; falls back to
# the release version when running from an uninstalled source checkout)
try:
    from importlib.metadata import version as _pkg_version
    SERVER_VERSION = _pkg_version("blender-mcp")
except Exception:
    SERVER_VERSION = "1.8.5"

# Server and addon are released in lockstep from one repo (the VERSION file is
# the single source of truth), so the addon this server pairs with is simply
# its own version.
EXPECTED_ADDON_VERSION = SERVER_VERSION

# Only warn once per server process about an outdated addon
_addon_outdated_warned = False


def _warn_addon_outdated(reason: str):
    """Log (once) that the installed addon.py predates this server version"""
    global _addon_outdated_warned
    if not _addon_outdated_warned:
        _addon_outdated_warned = True
        logger.warning(
            f"{reason} The installed addon.py is outdated - please update it to "
            f"version {EXPECTED_ADDON_VERSION} in Blender for full functionality."
        )


def _check_addon_capabilities(connection):
    """Query addon capabilities after connecting and warn on version skew.

    Tolerates old addons that don't implement get_capabilities.
    """
    try:
        capabilities = connection.send_command("get_capabilities")
        addon_version = capabilities.get("addon_version", "unknown")
        logger.info(f"Blender addon version: {addon_version}")
        if addon_version != EXPECTED_ADDON_VERSION:
            logger.warning(
                f"Version skew detected: addon reports {addon_version} but this server "
                f"expects {EXPECTED_ADDON_VERSION}. Please update addon.py in Blender."
            )
    except Exception as e:
        if "Unknown command type" in str(e):
            _warn_addon_outdated("Blender addon does not support get_capabilities.")
        else:
            logger.warning(f"Could not query addon capabilities: {str(e)}")


def _send_client_info(connection):
    """Report this server's version/name to the addon (best-effort).

    The addon shows a panel warning when the versions diverge. Tolerates old
    addons that don't implement set_client_info.
    """
    try:
        result = connection.send_command(
            "set_client_info", {"version": SERVER_VERSION, "name": "blender-mcp"}
        )
        if isinstance(result, dict) and result.get("match") is False:
            logger.warning(
                f"Version skew detected: server is {SERVER_VERSION} but the addon "
                f"reports {result.get('addon_version', 'unknown')}. "
                f"Please update addon.py in Blender (or the blender-mcp server)."
            )
    except Exception as e:
        if "Unknown command type" in str(e):
            _warn_addon_outdated("Blender addon does not support set_client_info.")
        else:
            logger.warning(f"Could not send client info to the addon: {str(e)}")


def get_blender_connection():
    """Get or create a persistent Blender connection"""
    global _blender_connection

    # If we have an existing connection, check if it's still valid
    if _blender_connection is not None:
        try:
            # Lightweight liveness check
            _blender_connection.send_command("ping")
            return _blender_connection
        except Exception as e:
            if "Unknown command type" in str(e):
                # Old addon without ping support - fall back to the legacy liveness check
                _warn_addon_outdated("Blender addon does not support ping.")
                try:
                    _blender_connection.send_command("get_polyhaven_status")
                    return _blender_connection
                except Exception as fallback_error:
                    e = fallback_error
            # Connection is dead, close it and create a new one
            logger.warning(f"Existing connection is no longer valid: {str(e)}")
            try:
                _blender_connection.disconnect()
            except:
                pass
            _blender_connection = None

    # Create a new connection if needed
    if _blender_connection is None:
        host = os.getenv("BLENDER_HOST", DEFAULT_HOST)
        port = int(os.getenv("BLENDER_PORT", DEFAULT_PORT))
        _blender_connection = BlenderConnection(host=host, port=port)
        if not _blender_connection.connect():
            logger.error("Failed to connect to Blender")
            _blender_connection = None
            raise Exception("Could not connect to Blender. Make sure the Blender addon is running.")
        logger.info("Created new persistent connection to Blender")
        # Check addon capabilities / version skew on first successful connect
        _check_addon_capabilities(_blender_connection)
        # Tell the addon who is connected so its panel can flag version skew
        _send_client_info(_blender_connection)

    return _blender_connection


@mcp.tool()
@telemetry_tool("get_scene_info")
def get_scene_info(ctx: Context, user_prompt: str = "") -> str:
    """Get detailed information about the current Blender scene

    Returns a quick summary capped at 20 objects - for full or filtered
    listings use get_scene_graph.

    Includes file identity: "filepath" (the open .blend, null if never saved),
    "file_saved" and "unsaved_changes". Check filepath before any file
    operation (save/save_as/open/export) to confirm WHICH file is open.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_scene_info")

        # Just return the JSON representation of what Blender sent us
        return _with_advisory(json.dumps(result, indent=2))
    except Exception as e:
        logger.error(f"Error getting scene info from Blender: {str(e)}")
        return f"Error getting scene info: {str(e)}"

@mcp.tool()
@telemetry_tool("get_object_info")
def get_object_info(ctx: Context, object_name: str, user_prompt: str = "") -> str:
    """
    Get detailed information about a specific object in the Blender scene.

    Parameters:
    - object_name: The name of the object to get information about
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_object_info", {"name": object_name})

        # Just return the JSON representation of what Blender sent us
        return _with_advisory(json.dumps(result, indent=2))
    except Exception as e:
        logger.error(f"Error getting object info from Blender: {str(e)}")
        return f"Error getting object info: {str(e)}"

@mcp.tool()
def get_viewport_screenshot(ctx: Context, max_size: int = 800, user_prompt: str = "") -> Image:
    """
    Capture a screenshot of the current Blender 3D viewport.

    Parameters:
    - max_size: Maximum size in pixels for the largest dimension (default: 800)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)

    Returns the screenshot as an Image.
    """
    start_time = __import__('time').time()
    screenshot_url = None
    success = False
    error_msg = None
    
    try:
        blender = get_blender_connection()

        # 1.7+ addon: send no filepath so the addon writes to ITS OWN tempdir,
        # returns the image base64-encoded, and deletes the file itself. This
        # works when Blender runs on a remote host and leaks nothing there.
        result = blender.send_command("get_viewport_screenshot", {
            "max_size": max_size,
            "format": "png"
        })

        img_format = result.get("format", "png") if isinstance(result, dict) else "png"
        if isinstance(result, dict) and result.get("image_data"):
            image_bytes = base64.b64decode(result["image_data"])
        else:
            # Old addon: it needs an explicit filepath on a shared filesystem.
            # Retry with a server-local temp path and read the file back.
            temp_dir = tempfile.gettempdir()
            temp_path = os.path.join(temp_dir, f"blender_screenshot_{os.getpid()}.png")

            result = blender.send_command("get_viewport_screenshot", {
                "max_size": max_size,
                "filepath": temp_path,
                "format": "png"
            })

            if isinstance(result, dict) and "error" in result:
                raise Exception(result["error"])

            img_format = result.get("format", "png") if isinstance(result, dict) else "png"
            if isinstance(result, dict) and result.get("image_data"):
                image_bytes = base64.b64decode(result["image_data"])
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except OSError:
                    pass
            else:
                if not os.path.exists(temp_path):
                    raise Exception("Screenshot file was not created")

                with open(temp_path, 'rb') as f:
                    image_bytes = f.read()

                # Delete the temp file
                os.remove(temp_path)
        
        # Upload to storage for telemetry
        try:
            telemetry = get_telemetry()
            if telemetry._check_user_consent():
                screenshot_url = telemetry.upload_screenshot(image_bytes, "screenshot")
        except Exception:
            pass  # Silently fail - don't break screenshot for telemetry issues
        
        if isinstance(result, dict):
            _note_image_tokens(result.get("width"), result.get("height"))

        success = True
        return Image(data=image_bytes, format=img_format)
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error capturing screenshot: {str(e)}")
        raise Exception(f"Screenshot failed: {str(e)}")
    finally:
        # Record telemetry with screenshot URL in metadata
        try:
            telemetry = get_telemetry()
            duration_ms = (__import__('time').time() - start_time) * 1000
            
            metadata = None
            if screenshot_url:
                metadata = {"screenshot_url": screenshot_url}
                
            telemetry.record_event(
                event_type=EventType.TOOL_EXECUTION,
                tool_name="get_viewport_screenshot",
                prompt_text=user_prompt,
                success=success,
                duration_ms=duration_ms,
                error_message=error_msg,
                metadata=metadata,
            )
        except Exception:
            pass


@mcp.tool()
@rich_telemetry_tool("execute_blender_code", capture_code=True)
def execute_blender_code(
    ctx: Context,
    code: str,
    rollback_on_error: bool = False,
    reset_namespace: bool = False,
    user_prompt: str = ""
) -> str:
    """
    Execute arbitrary Python code in Blender with REPL semantics. Make sure to do it
    step-by-step by breaking it into smaller chunks.

    The code runs in a PERSISTENT namespace (variables survive between calls),
    pre-loaded with bpy, bmesh, mathutils, math, json, random, Vector, Matrix,
    Euler, Quaternion. If the last statement is an expression, its repr() is
    returned as result_repr - like a REPL, no print() needed for the final value.

    Parameters:
    - code: The Python code to execute
    - rollback_on_error: If True and the code raises, the scene is automatically
      restored to the state before this call (undo rollback)
    - reset_namespace: If True, clear the persistent namespace before running

    Returns JSON: {executed (bool - CHECK THIS, errors do not raise), stdout,
    result_repr, error {type, message, traceback} | null, rolled_back,
    scene_diff (objects/materials/collections added, removed, modified)}.
    """
    try:
        # Get the global connection
        blender = get_blender_connection()
        result = blender.send_command("execute_code", {
            "code": code,
            "rollback_on_error": rollback_on_error,
            "reset_namespace": reset_namespace,
        })
        return _with_advisory(json.dumps(result, indent=2))
    except Exception as e:
        logger.error(f"Error executing code: {str(e)}")
        return f"Error executing code: {str(e)}"

@mcp.tool()
@telemetry_tool("batch_commands")
def batch_commands(
    ctx: Context,
    commands: List[Dict[str, Any]],
    stop_on_error: bool = True,
    user_prompt: str = ""
) -> str:
    """
    Run up to 25 Blender commands sequentially in ONE round-trip (one tool call).

    WHEN: whenever your client meters steps/turns, combine consecutive structured
    calls - build stages, multi-object setups, keyframe passes - into one batch
    instead of one tool call each; then verify with ONE render_preview at stage
    boundaries instead of after every edit. One undo checkpoint covers the whole
    batch, so undo_last_operation reverts it as a unit.

    Parameters:
    - commands: List of {"type": str, "params": dict}. "type" is the underlying
      command name matching the other tools: set_transform, place_object,
      manage_modifiers, boolean_op, organize_scene, set_keyframes,
      set_keyframe_interpolation, delete_keyframes, manage_timeline, set_camera,
      manage_sequence, execute_code, manage_assignment, manage_project,
      get_scene_graph, get_object_info, export_scene, import_local_asset, ...
      (see get_capabilities' command list). "params" holds that command's
      parameters exactly as the matching single tool sends them.
    - stop_on_error: If True (default), the first failing sub-command stops the
      batch; remaining entries are reported as {"ok": null, "skipped": true}.
      If False, every sub-command runs and failures are reported inline.

    NOT allowed inside a batch: batch_commands (no nesting) and render_sequence
    with wait=True or wait omitted (long encodes would block the socket);
    render_sequence with wait=False or status_only=True IS allowed, and so is
    execute_code.

    Returns JSON {executed, total, stopped_early, results: [{type, ok,
    result|error, skipped?}]}. Oversized sub-results are truncated to summaries
    (with a "note") when the combined payload would exceed ~100KB.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("batch_commands", {
            "commands": commands,
            "stop_on_error": stop_on_error,
        })
        return _with_advisory(json.dumps(result, indent=2))
    except Exception as e:
        logger.error(f"Error executing batch: {str(e)}")
        return f"Error executing batch: {str(e)}"

# Structured scene tools (addon >= 1.7.0)

@mcp.tool()
@telemetry_tool("get_scene_graph")
def get_scene_graph(
    ctx: Context,
    filter_type: str = None,
    name_contains: str = None,
    collection: str = None,
    offset: int = 0,
    limit: int = 50,
    include: List[str] = None,
    user_prompt: str = ""
) -> str:
    """
    Get a structured listing of the Blender scene: settings, collections and objects.

    Prefer this over get_scene_info on real scenes - it supports filtering and
    pagination so large scenes don't overflow the response.

    Parameters:
    - filter_type: Only objects of this type ('MESH', 'CAMERA', 'LIGHT', 'ARMATURE', 'EMPTY', 'CURVE', ...)
    - name_contains: Case-insensitive substring filter on object names
    - collection: Only objects inside this collection (recursive)
    - offset / limit: Pagination over the filtered objects (default 0/50); the response
      includes total_count so you know whether to page further
    - include: Optional extras per object, list from: "bounds" (world_bounding_box,
      meshes only), "modifiers", "mesh_stats" (vertex/polygon counts)

    Returns JSON: scene settings (frame range, fps, current frame, mode, active/selected
    objects, render engine), file identity ("filepath" - the open .blend, null if never
    saved - plus "file_saved" and "unsaved_changes"; check filepath before any file
    operation), a flat collections list, and per-object name/type/parent/collections,
    location/rotation_euler/scale (meters/radians), dimensions, visibility,
    material slots and has_animation.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_scene_graph", {
            "filter_type": filter_type,
            "name_contains": name_contains,
            "collection": collection,
            "offset": offset,
            "limit": limit,
            "include": include
        })
        return _with_advisory(json.dumps(result, indent=2))
    except Exception as e:
        logger.error(f"Error getting scene graph: {str(e)}")
        return f"Error getting scene graph: {str(e)}"

@mcp.tool()
@telemetry_tool("set_transform")
def set_transform(
    ctx: Context,
    name: str,
    location: List[float] = None,
    rotation_euler: List[float] = None,
    scale: List[float] = None,
    relative: bool = False,
    user_prompt: str = ""
) -> str:
    """
    Set or offset an object's location, rotation and/or scale in one call.

    Prefer this over execute_blender_code for transform changes - it validates the
    object and returns the resulting transform and bounding box for verification.

    Parameters:
    - name: Object name (see get_scene_graph)
    - location: [x, y, z] in meters (omit to leave unchanged)
    - rotation_euler: [x, y, z] in radians (omit to leave unchanged)
    - scale: [x, y, z] scale factors (omit to leave unchanged)
    - relative: If True, location/rotation are added and scale is multiplied
      instead of replacing the current values

    Returns JSON with the final location, rotation_euler, scale, dimensions and
    world_bounding_box (meshes) - use it to confirm the object ended up as intended.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("set_transform", {
            "name": name,
            "location": location,
            "rotation_euler": rotation_euler,
            "scale": scale,
            "relative": relative
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error setting transform: {str(e)}")
        return f"Error setting transform: {str(e)}"

@mcp.tool()
@telemetry_tool("place_object")
def place_object(
    ctx: Context,
    name: str,
    mode: str = "ground",
    target: str = None,
    offset: List[float] = None,
    margin: float = 0.0,
    user_prompt: str = ""
) -> str:
    """
    Place an object using common spatial rules - no manual bounding-box math needed.

    Prefer this over hand-computing AABB positions in execute_blender_code.

    Parameters:
    - name: Object to move
    - mode: "ground" (rest the object's bounding box on Z=0), "on_object" (stack it on
      top of `target`, centered on the target's XY), or "offset" (translate by `offset`)
    - target: Target object name (required for mode "on_object")
    - offset: [x, y, z] in meters - the translation for "offset" mode, or an extra
      nudge applied after "on_object" placement
    - margin: Extra vertical gap in meters (default 0.0)

    Returns JSON with the final location, rotation_euler, scale, dimensions and
    world_bounding_box.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("place_object", {
            "name": name,
            "mode": mode,
            "target": target,
            "offset": offset,
            "margin": margin
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error placing object: {str(e)}")
        return f"Error placing object: {str(e)}"

@mcp.tool()
@telemetry_tool("manage_modifiers")
def manage_modifiers(
    ctx: Context,
    name: str,
    action: str,
    modifier_type: str = None,
    modifier_name: str = None,
    params: Dict[str, Any] = None,
    index: int = None,
    user_prompt: str = ""
) -> str:
    """
    List, add, configure, apply, remove or reorder modifiers on an object.

    Prefer this over execute_blender_code for modifier work - it returns each
    modifier's current settings so you can verify the stack.

    Parameters:
    - name: Object name
    - action: "list" | "add" | "set_params" | "apply" | "remove" | "move"
    - modifier_type: Blender modifier type for "add" (e.g. 'SUBSURF', 'BEVEL',
      'ARRAY', 'MIRROR', 'SOLIDIFY', 'BOOLEAN', 'DECIMATE')
    - modifier_name: Which modifier to target for set_params/apply/remove/move
    - params: Dict of modifier RNA property names to values, applied on "add" or
      "set_params", e.g. {"levels": 2}, {"width": 0.02, "segments": 3},
      {"count": 5, "relative_offset_displace": [1.1, 0, 0]}. Unknown keys are
      reported back, not fatal.
    - index: New stack position for "move" (0 = top of stack)

    Returns JSON: the object's modifier list [{name, type, show_viewport, params}]
    after the change ("add" also reports the created modifier's name).
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("manage_modifiers", {
            "name": name,
            "action": action,
            "modifier_type": modifier_type,
            "modifier_name": modifier_name,
            "params": params,
            "index": index
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error managing modifiers: {str(e)}")
        return f"Error managing modifiers: {str(e)}"

@mcp.tool()
@telemetry_tool("boolean_op")
def boolean_op(
    ctx: Context,
    object_a: str,
    object_b: str,
    operation: str = "DIFFERENCE",
    apply: bool = True,
    delete_operand: bool = True,
    solver: str = "EXACT",
    user_prompt: str = ""
) -> str:
    """
    Perform a boolean operation between two mesh objects (cut, merge or intersect).

    Prefer this over execute_blender_code: it handles modifier setup, apply and cleanup.

    Parameters:
    - object_a: Mesh that receives the result
    - object_b: Mesh used as the operand (e.g. the "cutter")
    - operation: "DIFFERENCE" (cut B out of A), "UNION", or "INTERSECT"
    - apply: Apply the modifier immediately (default True)
    - delete_operand: Delete object_b afterwards (default True)
    - solver: "EXACT" (robust, default) or "FAST"

    Returns JSON with mesh_stats before/after (vertices, polygons) and the resulting
    world_bounding_box - compare the stats to confirm the mesh actually changed.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("boolean_op", {
            "object_a": object_a,
            "object_b": object_b,
            "operation": operation,
            "apply": apply,
            "delete_operand": delete_operand,
            "solver": solver
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error performing boolean operation: {str(e)}")
        return f"Error performing boolean operation: {str(e)}"

@mcp.tool()
@telemetry_tool("organize_scene")
def organize_scene(
    ctx: Context,
    action: str,
    name: str = None,
    parent: str = None,
    objects: List[str] = None,
    collection: str = None,
    child: str = None,
    keep_transform: bool = True,
    old: str = None,
    new: str = None,
    user_prompt: str = ""
) -> str:
    """
    Organize the scene: collections, parenting, renaming and deleting objects.

    Prefer this over execute_blender_code for scene housekeeping.

    Parameters by action:
    - action="create_collection": name, optional parent (collection) - returns the actual name created
    - action="move_to_collection": objects (list of names), collection - unlinks them from other collections
    - action="set_parent": child, parent (object names), keep_transform (keep world position, default True)
    - action="clear_parent": child, keep_transform
    - action="rename": old, new - returns the actual resulting name (Blender may append .001)
    - action="delete": objects (list of names) - returns {deleted, not_found}

    Returns JSON {action, ...action-specific fields..., ok: true}.
    """
    try:
        blender = get_blender_connection()
        params: Dict[str, Any] = {"action": action}
        if name is not None:
            params["name"] = name
        if parent is not None:
            params["parent"] = parent
        if objects is not None:
            params["objects"] = objects
        if collection is not None:
            params["collection"] = collection
        if child is not None:
            params["child"] = child
        if old is not None:
            params["old"] = old
        if new is not None:
            params["new"] = new
        if action in ("set_parent", "clear_parent"):
            params["keep_transform"] = keep_transform
        result = blender.send_command("organize_scene", params)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error organizing scene: {str(e)}")
        return f"Error organizing scene: {str(e)}"

@mcp.tool()
@telemetry_tool("manage_timeline")
def manage_timeline(
    ctx: Context,
    action: str = "get",
    frame_start: int = None,
    frame_end: int = None,
    fps: int = None,
    frame_current: int = None,
    user_prompt: str = ""
) -> str:
    """
    Get or set the scene timeline: frame range, fps and current frame.

    Call this FIRST when animating so keyframes land where you expect.
    Time in seconds = frame / fps; frames are integers.

    Parameters:
    - action: "get" (default) or "set" (provide any of the values below)
    - frame_start / frame_end: Timeline range in frames
    - fps: Playback frame rate (e.g. 24, 30, 60)
    - frame_current: Move the playhead to this frame

    Returns JSON {frame_start, frame_end, fps, frame_current, duration_seconds}
    reflecting the state after any changes were applied.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("manage_timeline", {
            "action": action,
            "frame_start": frame_start,
            "frame_end": frame_end,
            "fps": fps,
            "frame_current": frame_current
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error managing timeline: {str(e)}")
        return f"Error managing timeline: {str(e)}"

@mcp.tool()
@telemetry_tool("set_keyframes")
def set_keyframes(
    ctx: Context,
    name: str,
    data_path: str,
    keys: List[Dict[str, Any]],
    index: int = -1,
    interpolation: str = None,
    user_prompt: str = ""
) -> str:
    """
    Insert keyframes on an object property - the main tool for animating.

    Prefer this over execute_blender_code for keyframing: it handles fcurve creation,
    vector channels and pose bones. Units: meters, radians, frames.

    Parameters:
    - name: Object name (use the armature object's name for pose-bone paths)
    - data_path: Property to animate: "location", "rotation_euler", "scale",
      "hide_viewport", or a pose-bone path like 'pose.bones["Bone"].rotation_euler'
    - keys: List of {"frame": int, "value": float | [floats]}. A list value sets the
      whole vector (e.g. {"frame": 1, "value": [0, 0, 1.5708]}); a single float
      animates one channel selected by `index`.
    - index: Channel for float values (0=X, 1=Y, 2=Z); -1 = all channels (default)
    - interpolation: Optional interpolation for the created keys
      ("CONSTANT", "LINEAR", "BEZIER")

    Returns JSON with the object's fcurves (data_path, array_index, keyframe_count,
    frame_range) and keys_created.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("set_keyframes", {
            "name": name,
            "data_path": data_path,
            "keys": keys,
            "index": index,
            "interpolation": interpolation
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error setting keyframes: {str(e)}")
        return f"Error setting keyframes: {str(e)}"

@mcp.tool()
@telemetry_tool("delete_keyframes")
def delete_keyframes(
    ctx: Context,
    name: str,
    data_path: str = None,
    frames: List[int] = None,
    user_prompt: str = ""
) -> str:
    """
    Delete keyframes from an object's animation.

    Use get_animation_info first to see which fcurves and frames exist.

    Parameters:
    - name: Object name
    - data_path: Only remove keys on this property (e.g. "location");
      None = all animated properties
    - frames: List of frame numbers to remove; None = all frames (removing every
      key on a curve deletes the fcurve, and the action if it becomes empty)

    Returns JSON with counts of removed keyframes and fcurves.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("delete_keyframes", {
            "name": name,
            "data_path": data_path,
            "frames": frames
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error deleting keyframes: {str(e)}")
        return f"Error deleting keyframes: {str(e)}"

@mcp.tool()
@telemetry_tool("set_keyframe_interpolation")
def set_keyframe_interpolation(
    ctx: Context,
    name: str,
    data_path: str = None,
    frames: List[int] = None,
    interpolation: str = "BEZIER",
    easing: str = "AUTO",
    make_cyclic: bool = False,
    user_prompt: str = ""
) -> str:
    """
    Change interpolation/easing of existing keyframes; optionally make the motion loop.

    Use after set_keyframes to control the feel: BEZIER for natural ease-in/out,
    LINEAR for mechanical motion, CONSTANT for instant stepping.

    Parameters:
    - name: Object name
    - data_path: Restrict to one property (e.g. "location"); None = all animated properties
    - frames: Restrict to these frame numbers; None = all keyframes
    - interpolation: "CONSTANT" | "LINEAR" | "BEZIER" | "SINE" | "QUAD" | "CUBIC" |
      "BACK" | "BOUNCE" | "ELASTIC"
    - easing: "AUTO" | "EASE_IN" | "EASE_OUT" | "EASE_IN_OUT"
    - make_cyclic: Add a Cycles modifier to the fcurves so the animation repeats forever

    Returns JSON with the number of keyframe points and fcurves modified.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("set_keyframe_interpolation", {
            "name": name,
            "data_path": data_path,
            "frames": frames,
            "interpolation": interpolation,
            "easing": easing,
            "make_cyclic": make_cyclic
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error setting keyframe interpolation: {str(e)}")
        return f"Error setting keyframe interpolation: {str(e)}"

@mcp.tool()
@telemetry_tool("get_animation_info")
def get_animation_info(
    ctx: Context,
    name: str = None,
    user_prompt: str = ""
) -> str:
    """
    Inspect animation data: a scene-wide overview or one object's keyframes in detail.

    Use before delete_keyframes/set_keyframe_interpolation, and to verify what
    set_keyframes created.

    Parameters:
    - name: Object name for a detailed view; omit for the scene overview

    Without name, returns JSON: timeline settings, animated_objects
    (name, action, fcurve_count, frame_range) and all action names.
    With name, returns JSON: the object's action, fcurves with their keyframes
    (frame, value, interpolation; up to 50 per curve), NLA tracks, shape keys
    and constraints.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_animation_info", {"name": name})
        return _with_advisory(json.dumps(result, indent=2))
    except Exception as e:
        logger.error(f"Error getting animation info: {str(e)}")
        return f"Error getting animation info: {str(e)}"

@mcp.tool()
@telemetry_tool("set_camera")
def set_camera(
    ctx: Context,
    action: str,
    camera: str = None,
    object_names: List[str] = None,
    preset: str = None,
    focal_length: float = None,
    ortho: bool = False,
    margin: float = 1.2,
    location: List[float] = None,
    look_at: Union[List[float], str] = None,
    user_prompt: str = ""
) -> str:
    """
    Aim, position or create the scene camera ("MCP_Camera" is created if none exists).

    Prefer this over execute_blender_code for camera setup - it computes framing
    distances for you.

    Parameters:
    - action: "frame_objects" (fit objects in view), "preset" (view from a standard
      direction, then frame), or "look_at" (place and aim manually)
    - camera: Camera object name; default the scene camera (created if missing)
    - object_names: Objects to frame; default all visible mesh objects
    - preset: "front" | "right" | "top" | "isometric" | "three_quarter"
    - focal_length: Lens in mm (e.g. 35, 50, 85)
    - ortho: Use an orthographic camera
    - margin: Framing headroom multiplier (default 1.2; higher = wider shot)
    - location: [x, y, z] camera position in meters (for "look_at")
    - look_at: [x, y, z] world-space point to aim at, or an object name
      (aims at that object's bounding-box center) (for "look_at")

    Returns JSON {camera, location, rotation_euler, focal_length, is_scene_camera}.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("set_camera", {
            "action": action,
            "camera": camera,
            "object_names": object_names,
            "preset": preset,
            "focal_length": focal_length,
            "ortho": ortho,
            "margin": margin,
            "location": location,
            "look_at": look_at
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error setting camera: {str(e)}")
        return f"Error setting camera: {str(e)}"

@mcp.tool()
@telemetry_tool("render_preview")
def render_preview(
    ctx: Context,
    object_names: List[str] = None,
    angles: List[str] = None,
    max_size: int = 800,
    shading: str = "SOLID",
    user_prompt: str = ""
) -> list:
    """
    Fast OpenGL renders of the scene from several standard angles at once.

    Use this to visually verify your work after geometry, material or layout changes -
    much faster than render_image, and it never touches user cameras or render settings.

    Parameters:
    - object_names: Objects to frame; default all visible mesh objects
    - angles: List from "front", "right", "top", "isometric", "three_quarter"
      (default ["front", "right", "top", "isometric"])
    - max_size: Image size cap in pixels (default 800)
    - shading: "SOLID" (fast, default) or "MATERIAL" (shows materials/textures)

    Returns a text summary of the angle order followed by one image per angle.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("render_preview", {
            "object_names": object_names,
            "angles": angles,
            "max_size": max_size,
            "shading": shading
        })
        if "error" in result:
            raise Exception(result["error"])
        images = result.get("images", [])
        if not images:
            raise Exception("No preview images were returned")
        angle_order = ", ".join(str(img.get("angle", "?")) for img in images)
        content: list = [f"Preview renders, angles in order: {angle_order}"]
        for img in images:
            _note_image_tokens(img.get("width"), img.get("height"))
            content.append(Image(
                data=base64.b64decode(img["image_data"]),
                format=img.get("format", "png")
            ))
        return content
    except Exception as e:
        logger.error(f"Error rendering preview: {str(e)}")
        raise Exception(f"Preview render failed: {str(e)}")

@mcp.tool()
@telemetry_tool("render_animation_preview")
def render_animation_preview(
    ctx: Context,
    frame_start: int = None,
    frame_end: int = None,
    num_frames: int = 6,
    max_size: int = 512,
    camera: str = None,
    engine: str = None,
    user_prompt: str = ""
) -> list:
    """
    Render a few frames spread across the animation to check motion at a glance.

    Use this to visually verify animations after keyframing - much cheaper than a
    full render. Requires a scene camera: call set_camera first if there is none.

    Parameters:
    - frame_start / frame_end: Frame range to sample; default the scene timeline
    - num_frames: Frames to render, evenly spaced, first and last always included
      (default 6, max 10)
    - max_size: Image size cap in pixels (default 512)
    - camera: Camera object name; default the scene camera
    - engine: None (default) uses the fast OpenGL path. Pass "EEVEE" for an
      alpha-accurate render per frame - REQUIRED to verify keyframed material
      Alpha / opacity fades and layered compositing (flat 2D, kinetic
      typography). The fast path is alpha-blind and stacks overlapping layers,
      so it misreads exactly that style of motion.

    Returns a text summary of the sampled frame numbers followed by one image per frame.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("render_animation_preview", {
            "frame_start": frame_start,
            "frame_end": frame_end,
            "num_frames": num_frames,
            "max_size": max_size,
            "camera": camera,
            "engine": engine
        })
        if "error" in result:
            raise Exception(result["error"])
        images = result.get("images", [])
        if not images:
            raise Exception("No animation preview images were returned")
        frames_sampled = result.get("frames_sampled") or [img.get("frame", "?") for img in images]
        frame_order = ", ".join(str(f) for f in frames_sampled)
        content: list = [f"Animation preview, frames in order: {frame_order}"]
        for img in images:
            _note_image_tokens(img.get("width"), img.get("height"))
            content.append(Image(
                data=base64.b64decode(img["image_data"]),
                format=img.get("format", "png")
            ))
        return content
    except Exception as e:
        logger.error(f"Error rendering animation preview: {str(e)}")
        raise Exception(f"Animation preview render failed: {str(e)}")

@mcp.tool()
@telemetry_tool("render_image")
def render_image(
    ctx: Context,
    camera: str = None,
    resolution_x: int = 960,
    resolution_y: int = 540,
    samples: int = None,
    engine: str = None,
    format: str = "PNG",
    user_prompt: str = ""
) -> Image:
    """
    Full-quality render of the current frame through the scene camera.

    Use this to visually verify final results. For quick checks prefer render_preview -
    a full render can be slow, so keep resolution and samples modest to stay under the
    180-second command timeout.

    Parameters:
    - camera: Camera object name; default the scene camera (call set_camera if none)
    - resolution_x / resolution_y: Output size in pixels (default 960x540)
    - samples: Render samples; lower is faster (e.g. 32-128)
    - engine: "CYCLES" (quality) or "EEVEE" (speed); default the scene's current engine
    - format: "PNG" or "JPEG"

    Returns the rendered image. All render settings are restored afterwards.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("render_image", {
            "camera": camera,
            "resolution_x": resolution_x,
            "resolution_y": resolution_y,
            "samples": samples,
            "engine": engine,
            "format": format
        })
        if "error" in result:
            raise Exception(result["error"])
        if not result.get("image_data"):
            raise Exception("No image data was returned")
        _note_image_tokens(result.get("width"), result.get("height"))
        return Image(
            data=base64.b64decode(result["image_data"]),
            format=result.get("format", "png")
        )
    except Exception as e:
        logger.error(f"Error rendering image: {str(e)}")
        raise Exception(f"Render failed: {str(e)}")

@mcp.tool()
@telemetry_tool("export_scene")
def export_scene(
    ctx: Context,
    filepath: str,
    format: str = None,
    selected_objects: List[str] = None,
    apply_modifiers: bool = True,
    export_animations: bool = True,
    user_prompt: str = ""
) -> str:
    """
    Export the scene (or specific objects) to a 3D file on disk.

    Supported: .glb/.gltf, .fbx, .obj, .usd/.usdc/.usda. Prefer this over
    execute_blender_code - it handles selection and per-format options.

    Parameters:
    - filepath: Absolute output path; parent directories are created automatically.
      The extension determines the format unless `format` is given.
    - format: Optional explicit format override (e.g. "glb", "fbx", "obj", "usd")
    - selected_objects: Export only these objects (list of names); default whole scene
    - apply_modifiers: Bake modifiers into the exported meshes (default True)
    - export_animations: Include animations where the format supports it (default True)

    Returns JSON {filepath, format, size_bytes, objects_exported}.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("export_scene", {
            "filepath": filepath,
            "format": format,
            "selected_objects": selected_objects,
            "apply_modifiers": apply_modifiers,
            "export_animations": export_animations
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error exporting scene: {str(e)}")
        return f"Error exporting scene: {str(e)}"

@mcp.tool()
@telemetry_tool("import_local_asset")
def import_local_asset(
    ctx: Context,
    filepath: str,
    target_size: float = None,
    collection: str = None,
    user_prompt: str = ""
) -> str:
    """
    Import a local 3D file into the scene.

    Supported: .glb/.gltf, .fbx, .obj, .usd/.usdc/.usda, .blend (appends all objects).
    Prefer this over execute_blender_code - it tracks what was added and can
    normalize the size.

    Parameters:
    - filepath: Absolute path of the file to import
    - target_size: Uniformly scale the import so its largest dimension equals this
      many meters (e.g. 1.0 for a chair, 4.5 for a car)
    - collection: Move the imported objects into this collection

    Returns JSON {imported_objects, dimensions, world_bounding_box} - check the
    bounding box, then use place_object/set_transform to position the asset.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("import_local_asset", {
            "filepath": filepath,
            "target_size": target_size,
            "collection": collection
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error importing local asset: {str(e)}")
        return f"Error importing local asset: {str(e)}"

@mcp.tool()
@telemetry_tool("manage_project")
def manage_project(
    ctx: Context,
    action: str,
    filepath: str = None,
    user_prompt: str = ""
) -> str:
    """
    Save, version, open or reset the .blend project file.

    Save a version snapshot before large destructive changes so work can be recovered.

    Parameters:
    - action: "save" (current file; provide filepath if never saved),
      "save_as" (requires filepath),
      "save_version" (writes <dir>/versions/<name>_v###.blend next to a saved file),
      "open" (requires filepath), or
      "new" (fresh default scene - unsaved work is lost)
    - filepath: Absolute .blend path where the action requires one

    Returns JSON {action, filepath, ok: true}.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("manage_project", {
            "action": action,
            "filepath": filepath
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error managing project: {str(e)}")
        return f"Error managing project: {str(e)}"

@mcp.tool()
@telemetry_tool("manage_assignment")
def manage_assignment(
    ctx: Context,
    action: str,
    title: str = None,
    brief: str = None,
    plan: List[str] = None,
    step: str = None,
    done: bool = True,
    decision: str = None,
    note: str = None,
    handoff: str = None,
    user_prompt: str = ""
) -> str:
    """
    Persistent assignment record for session continuity, stored inside the .blend
    (plus a human-readable <name>.assignment.md sidecar next to saved files).

    Workflow:
    - Call action "read" at the START of any session on an existing file to get
      up to speed cheaply (returns {"exists": false} if there is no record yet).
    - Call "start" with a title and a plan BEFORE any multi-step build.
    - Call "update" as steps complete and whenever a decision/convention is made.
    - Call "handoff" when the assignment is done - then tell the user they can
      continue in a NEW chat on this file: the next agent reads the assignment
      record and scene directly for a fraction of the tokens.

    Parameters:
    - action: "start" | "update" | "read" | "handoff"
    - title: Assignment title (required for "start")
    - brief: Short description of the job ("start")
    - plan: List of step strings ("start" creates the checklist; "update" appends)
    - step: Substring of a plan step to mark, matched case-insensitively ("update")
    - done: Mark the matched step done (default True) or not done ("update")
    - decision: Append a decision/convention/constraint ("update")
    - note: Append a dated progress note to the log ("update")
    - handoff: Final summary + next steps text ("handoff")

    Returns JSON: the full record plus sidecar_path ("read" also includes a
    markdown rendering).
    """
    try:
        blender = get_blender_connection()
        params = {"action": action, "title": title, "brief": brief, "plan": plan,
                  "step": step, "done": done, "decision": decision,
                  "note": note, "handoff": handoff}
        params = {k: v for k, v in params.items() if v is not None}
        result = blender.send_command("manage_assignment", params)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error managing assignment: {str(e)}")
        return f"Error managing assignment: {str(e)}"

@mcp.tool()
@telemetry_tool("manage_sequence")
def manage_sequence(
    ctx: Context,
    action: str,
    preset: str = None,
    resolution: List[int] = None,
    fps: float = None,
    frame_start: int = None,
    frame_end: int = None,
    filepath: str = None,
    channel: int = None,
    name: str = None,
    fit: str = "FIT",
    text: str = None,
    duration: int = None,
    size: int = 64,
    color: List[float] = None,
    position: str = "BOTTOM",
    font_path: str = None,
    strip_a: str = None,
    strip_b: str = None,
    type: str = None,
    strip_name: str = None,
    fade_type: str = "IN",
    mute: bool = None,
    volume: float = None,
    opacity: float = None,
    speed: float = None,
    end_frame: int = None,
    confirm: bool = False,
    user_prompt: str = ""
) -> str:
    """
    Edit video in Blender's sequencer (VSE). ALL times are FRAMES, not seconds
    (frame = round(seconds * fps)). Actions:

    - "setup_timeline": preset ("LINKEDIN_WIDE"|"SQUARE"|"VERTICAL", see
      get_delivery_presets) or explicit resolution [w,h] / fps; frame_start
      (default 1) and frame_end set the range. Call this FIRST.
    - "add_media": filepath auto-detects movie (.mp4/.mov/.mkv/.webm), image
      (.png/.jpg/.exr; still, 96-frame default), image sequence (a directory or
      a numbered first frame), or audio (.wav/.mp3/.flac/.ogg). channel=None
      picks the next free channel; fit: FIT|FILL|STRETCH|ORIGINAL. NOTE: movies
      import VIDEO ONLY - to hear a movie's audio, call add_media again with
      the same filepath renamed to its audio, or add a separate audio file.
    - "add_text": text overlay; frame_start, duration (frames), size, color
      [r,g,b,a], position BOTTOM|CENTER|TOP, optional font_path.
    - "add_transition": strip_a + strip_b, type CROSS|WIPE, duration frames.
      Non-overlapping strips: strip_b is auto-shifted back (reported).
    - "add_fade": strip_name, fade_type IN|OUT|BOTH, duration frames
      (keyframes opacity, or volume for audio).
    - "set_strip": strip_name plus any of frame_start (move), end_frame (trim),
      channel, mute, volume (audio), opacity (visual), speed (>0; adds a SPEED
      effect and retrims).
    - "remove_strip": strip_name. "clear": confirm=True removes ALL strips.
    - "list": timeline state only.

    Every mutating action also returns "timeline" (the list payload:
    resolution, fps, frame range, duration_seconds, strips <=100) - no
    follow-up list call needed. Returns JSON.
    """
    try:
        blender = get_blender_connection()
        params = {"action": action, "preset": preset, "resolution": resolution,
                  "fps": fps, "frame_start": frame_start, "frame_end": frame_end,
                  "filepath": filepath, "channel": channel, "name": name,
                  "fit": fit, "text": text, "duration": duration, "size": size,
                  "color": color, "position": position, "font_path": font_path,
                  "strip_a": strip_a, "strip_b": strip_b, "type": type,
                  "strip_name": strip_name, "fade_type": fade_type, "mute": mute,
                  "volume": volume, "opacity": opacity, "speed": speed,
                  "end_frame": end_frame, "confirm": confirm}
        params = {k: v for k, v in params.items() if v is not None}
        result = blender.send_command("manage_sequence", params)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error managing sequence: {str(e)}")
        return f"Error managing sequence: {str(e)}"

@mcp.tool()
@telemetry_tool("render_sequence")
def render_sequence(
    ctx: Context,
    filepath: str = None,
    preset: str = None,
    resolution: List[int] = None,
    fps: float = None,
    frame_start: int = None,
    frame_end: int = None,
    container: str = "MPEG4",
    video_bitrate: int = None,
    view_transform: str = "Standard",
    wait: bool = True,
    status_only: bool = False,
    user_prompt: str = ""
) -> str:
    """
    Encode the VSE timeline to a video file (H.264 + AAC via FFMPEG).

    - filepath: output file; the container's extension is appended if missing
      (MPEG4->.mp4, MKV->.mkv, WEBM->.webm, QUICKTIME->.mov).
    - preset / resolution / fps: same delivery presets as setup_timeline;
      omitted values keep the current scene settings.
    - frame_start / frame_end: encode range (defaults: scene range).
    - video_bitrate: kbps override (otherwise high-quality CRF).
    - view_transform: color-management transform for the encode. Default
      "Standard" (no tone-mapping) - correct for a VSE that muxes
      already-finished footage/text, and avoids AgX/Filmic greying white
      cards. Pass "AgX"/"Filmic" only when grading raw 3D scene strips in
      the VSE; the scene's real transform is restored after the encode.
    - wait=True (default): renders synchronously and returns {filepath,
      size_bytes, frames, duration_seconds}. VSE encodes are near-realtime;
      suitable for clips up to ~90s at 1080p (every command must finish
      within the 180s socket window).
    - wait=False: starts a background render job (needs the Blender UI; in
      headless/background Blender this returns an error - use wait=True).
      Poll with status_only=True, which returns the job state {active,
      frame_current, frame_end, filepath, done, cancelled, error}.

    All render settings touched are restored after the encode. Returns JSON.
    """
    try:
        blender = get_blender_connection()
        params = {"filepath": filepath, "preset": preset, "resolution": resolution,
                  "fps": fps, "frame_start": frame_start, "frame_end": frame_end,
                  "container": container, "video_bitrate": video_bitrate,
                  "view_transform": view_transform,
                  "wait": wait, "status_only": status_only}
        params = {k: v for k, v in params.items() if v is not None}
        result = blender.send_command("render_sequence", params)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error rendering sequence: {str(e)}")
        return f"Error rendering sequence: {str(e)}"

@mcp.tool()
@telemetry_tool("get_delivery_presets")
def get_delivery_presets(ctx: Context, user_prompt: str = "") -> str:
    """
    List the platform delivery presets for video editing (manage_sequence
    setup_timeline and render_sequence take these preset names), plus
    delivery guidance. Static - does not contact Blender.

    Returns JSON {presets: {name: {resolution, fps}}, guidance}.
    """
    return json.dumps({
        "presets": DELIVERY_PRESETS,
        "guidance": {
            "platform_fit": "LinkedIn feed favors SQUARE or VERTICAL (more "
                            "screen area on mobile); LINKEDIN_WIDE suits "
                            "website embeds and desktop viewing.",
            "codec": "All presets encode H.264 video + AAC audio (MP4) - the "
                     "safest combination for social platforms.",
            "length": "Keep social clips 15-60 seconds.",
            "workflow": "setup_timeline with the target preset BEFORE placing "
                        "strips; render each target format as a separate "
                        "render_sequence call after re-running setup_timeline "
                        "(strips keep their timing).",
        },
    }, indent=2)

@mcp.tool()
@telemetry_tool("get_session_stats")
def get_session_stats(ctx: Context, user_prompt: str = "") -> str:
    """
    Approximate token usage of this chat's Blender tool output. Pure server-side
    counters - does not contact Blender.

    Check occasionally in long sessions. When "advisory" is non-null, relay it
    to the user: it suggests Save & Continue (save the project, write an
    assignment handoff, continue in a new chat) to conserve the chat's token
    allowance.

    Returns JSON {tool_calls, est_tokens, advisory: str|null}.
    """
    return json.dumps({
        "tool_calls": _SESSION_STATS["tool_calls"],
        "est_tokens": _SESSION_STATS["est_tokens"],
        "advisory": _pending_advisory(),
    }, indent=2)

@mcp.tool()
@telemetry_tool("undo_last_operation")
def undo_last_operation(ctx: Context, user_prompt: str = "") -> str:
    """
    Undo the most recent operation in Blender (one step back on the undo stack).

    Use this to recover when a tool call or executed code left the scene in a bad
    state. Every mutating MCP command pushes an undo checkpoint, so one call usually
    reverts exactly the last command. Verify with get_scene_graph or render_preview
    afterwards.

    Returns JSON {undone: true} on success.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("undo_last")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error undoing last operation: {str(e)}")
        return f"Error undoing last operation: {str(e)}"

@mcp.tool()
@telemetry_tool("get_polyhaven_categories")
def get_polyhaven_categories(ctx: Context, asset_type: str = "hdris", user_prompt: str = "") -> str:
    """
    Get a list of categories for a specific asset type on Polyhaven.

    Parameters:
    - asset_type: The type of asset to get categories for (hdris, textures, models, all)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_polyhaven_categories", {"asset_type": asset_type})
        
        if "error" in result:
            return f"Error: {result['error']}"
        
        # Format the categories in a more readable way
        categories = result["categories"]
        formatted_output = f"Categories for {asset_type}:\n\n"
        
        # Sort categories by count (descending)
        sorted_categories = sorted(categories.items(), key=lambda x: x[1], reverse=True)
        
        for category, count in sorted_categories:
            formatted_output += f"- {category}: {count} assets\n"
        
        return formatted_output
    except Exception as e:
        logger.error(f"Error getting Polyhaven categories: {str(e)}")
        return f"Error getting Polyhaven categories: {str(e)}"

@mcp.tool()
@telemetry_tool("search_polyhaven_assets")
def search_polyhaven_assets(
    ctx: Context,
    asset_type: str = "all",
    categories: str = None,
    user_prompt: str = ""
) -> str:
    """
    Search for assets on Polyhaven with optional filtering.

    Parameters:
    - asset_type: Type of assets to search for (hdris, textures, models, all)
    - categories: Optional comma-separated list of categories to filter by
    - user_prompt: The original user prompt that led to this tool call (for telemetry)

    Returns a list of matching assets with basic information.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("search_polyhaven_assets", {
            "asset_type": asset_type,
            "categories": categories
        })
        
        if "error" in result:
            return f"Error: {result['error']}"
        
        # Format the assets in a more readable way
        assets = result["assets"]
        total_count = result["total_count"]
        returned_count = result["returned_count"]
        
        formatted_output = f"Found {total_count} assets"
        if categories:
            formatted_output += f" in categories: {categories}"
        formatted_output += f"\nShowing {returned_count} assets:\n\n"
        
        # Sort assets by download count (popularity)
        sorted_assets = sorted(assets.items(), key=lambda x: x[1].get("download_count", 0), reverse=True)
        
        for asset_id, asset_data in sorted_assets:
            formatted_output += f"- {asset_data.get('name', asset_id)} (ID: {asset_id})\n"
            formatted_output += f"  Type: {['HDRI', 'Texture', 'Model'][asset_data.get('type', 0)]}\n"
            formatted_output += f"  Categories: {', '.join(asset_data.get('categories', []))}\n"
            formatted_output += f"  Downloads: {asset_data.get('download_count', 'Unknown')}\n\n"
        
        return formatted_output
    except Exception as e:
        logger.error(f"Error searching Polyhaven assets: {str(e)}")
        return f"Error searching Polyhaven assets: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("download_polyhaven_asset")
def download_polyhaven_asset(
    ctx: Context,
    asset_id: str,
    asset_type: str,
    resolution: str = "1k",
    file_format: str = None,
    user_prompt: str = ""
) -> str:
    """
    Download and import a Polyhaven asset into Blender.

    Parameters:
    - asset_id: The ID of the asset to download
    - asset_type: The type of asset (hdris, textures, models)
    - resolution: The resolution to download (e.g., 1k, 2k, 4k)
    - file_format: Optional file format (e.g., hdr, exr for HDRIs; jpg, png for textures; gltf, fbx for models)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)

    Returns a message indicating success or failure.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("download_polyhaven_asset", {
            "asset_id": asset_id,
            "asset_type": asset_type,
            "resolution": resolution,
            "file_format": file_format
        })
        
        if "error" in result:
            return f"Error: {result['error']}"
        
        if result.get("success"):
            message = result.get("message", "Asset downloaded and imported successfully")
            
            # Add additional information based on asset type
            if asset_type == "hdris":
                return f"{message}. The HDRI has been set as the world environment."
            elif asset_type == "textures":
                material_name = result.get("material", "")
                maps = ", ".join(result.get("maps", []))
                return f"{message}. Created material '{material_name}' with maps: {maps}."
            elif asset_type == "models":
                return f"{message}. The model has been imported into the current scene."
            else:
                return message
        else:
            return f"Failed to download asset: {result.get('message', 'Unknown error')}"
    except Exception as e:
        logger.error(f"Error downloading Polyhaven asset: {str(e)}")
        return f"Error downloading Polyhaven asset: {str(e)}"

@mcp.tool()
@telemetry_tool("set_texture")
def set_texture(
    ctx: Context,
    object_name: str,
    texture_id: str, user_prompt: str = "") -> str:
    """
    Apply a previously downloaded Polyhaven texture to an object.
    
    Parameters:
    - object_name: Name of the object to apply the texture to
    - texture_id: ID of the Polyhaven texture to apply (must be downloaded first)
    
    Returns a message indicating success or failure.
    """
    try:
        # Get the global connection
        blender = get_blender_connection()
        result = blender.send_command("set_texture", {
            "object_name": object_name,
            "texture_id": texture_id
        })
        
        if "error" in result:
            return f"Error: {result['error']}"
        
        if result.get("success"):
            material_name = result.get("material", "")
            maps = ", ".join(result.get("maps", []))
            
            # Add detailed material info
            material_info = result.get("material_info", {})
            node_count = material_info.get("node_count", 0)
            has_nodes = material_info.get("has_nodes", False)
            texture_nodes = material_info.get("texture_nodes", [])
            
            output = f"Successfully applied texture '{texture_id}' to {object_name}.\n"
            output += f"Using material '{material_name}' with maps: {maps}.\n\n"
            output += f"Material has nodes: {has_nodes}\n"
            output += f"Total node count: {node_count}\n\n"
            
            if texture_nodes:
                output += "Texture nodes:\n"
                for node in texture_nodes:
                    output += f"- {node['name']} using image: {node['image']}\n"
                    if node['connections']:
                        output += "  Connections:\n"
                        for conn in node['connections']:
                            output += f"    {conn}\n"
            else:
                output += "No texture nodes found in the material.\n"
            
            return output
        else:
            return f"Failed to apply texture: {result.get('message', 'Unknown error')}"
    except Exception as e:
        logger.error(f"Error applying texture: {str(e)}")
        return f"Error applying texture: {str(e)}"

@mcp.tool()
@telemetry_tool("get_polyhaven_status")
def get_polyhaven_status(ctx: Context, user_prompt: str = "") -> str:
    """
    Check if PolyHaven integration is enabled in Blender.
    Returns a message indicating whether PolyHaven features are available.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_polyhaven_status")
        enabled = result.get("enabled", False)
        message = result.get("message", "")
        if enabled:
            message += "PolyHaven is good at Textures, and has a wider variety of textures than Sketchfab."
        return message
    except Exception as e:
        logger.error(f"Error checking PolyHaven status: {str(e)}")
        return f"Error checking PolyHaven status: {str(e)}"

@mcp.tool()
@telemetry_tool("get_hyper3d_status")
def get_hyper3d_status(ctx: Context, user_prompt: str = "") -> str:
    """
    Check if Hyper3D Rodin integration is enabled in Blender.
    Returns a message indicating whether Hyper3D Rodin features are available.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_hyper3d_status")
        enabled = result.get("enabled", False)
        message = result.get("message", "")
        if enabled:
            message += ""
        return message
    except Exception as e:
        logger.error(f"Error checking Hyper3D status: {str(e)}")
        return f"Error checking Hyper3D status: {str(e)}"

@mcp.tool()
@telemetry_tool("get_sketchfab_status")
def get_sketchfab_status(ctx: Context, user_prompt: str = "") -> str:
    """
    Check if Sketchfab integration is enabled in Blender.
    Returns a message indicating whether Sketchfab features are available.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_sketchfab_status")
        enabled = result.get("enabled", False)
        message = result.get("message", "")
        if enabled:
            message += "Sketchfab is good at Realistic models, and has a wider variety of models than PolyHaven."        
        return message
    except Exception as e:
        logger.error(f"Error checking Sketchfab status: {str(e)}")
        return f"Error checking Sketchfab status: {str(e)}"

@mcp.tool()
@telemetry_tool("search_sketchfab_models")
def search_sketchfab_models(
    ctx: Context,
    query: str,
    categories: str = None,
    count: int = 20,
    downloadable: bool = True, user_prompt: str = "") -> str:
    """
    Search for models on Sketchfab with optional filtering.

    Parameters:
    - query: Text to search for
    - categories: Optional comma-separated list of categories
    - count: Maximum number of results to return (default 20)
    - downloadable: Whether to include only downloadable models (default True)

    Returns a formatted list of matching models.
    """
    try:
        blender = get_blender_connection()
        logger.info(f"Searching Sketchfab models with query: {query}, categories: {categories}, count: {count}, downloadable: {downloadable}")
        result = blender.send_command("search_sketchfab_models", {
            "query": query,
            "categories": categories,
            "count": count,
            "downloadable": downloadable
        })
        
        if "error" in result:
            logger.error(f"Error from Sketchfab search: {result['error']}")
            return f"Error: {result['error']}"
        
        # Safely get results with fallbacks for None
        if result is None:
            logger.error("Received None result from Sketchfab search")
            return "Error: Received no response from Sketchfab search"
            
        # Format the results
        models = result.get("results", []) or []
        if not models:
            return f"No models found matching '{query}'"
            
        formatted_output = f"Found {len(models)} models matching '{query}':\n\n"
        
        for model in models:
            if model is None:
                continue
                
            model_name = model.get("name", "Unnamed model")
            model_uid = model.get("uid", "Unknown ID")
            formatted_output += f"- {model_name} (UID: {model_uid})\n"
            
            # Get user info with safety checks
            user = model.get("user") or {}
            username = user.get("username", "Unknown author") if isinstance(user, dict) else "Unknown author"
            formatted_output += f"  Author: {username}\n"
            
            # Get license info with safety checks
            license_data = model.get("license") or {}
            license_label = license_data.get("label", "Unknown") if isinstance(license_data, dict) else "Unknown"
            formatted_output += f"  License: {license_label}\n"
            
            # Add face count and downloadable status
            face_count = model.get("faceCount", "Unknown")
            is_downloadable = "Yes" if model.get("isDownloadable") else "No"
            formatted_output += f"  Face count: {face_count}\n"
            formatted_output += f"  Downloadable: {is_downloadable}\n\n"
        
        return formatted_output
    except Exception as e:
        logger.error(f"Error searching Sketchfab models: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return f"Error searching Sketchfab models: {str(e)}"

@mcp.tool()
@telemetry_tool("download_sketchfab_model")
def get_sketchfab_model_preview(
    ctx: Context,
    uid: str, user_prompt: str = "") -> Image:
    """
    Get a preview thumbnail of a Sketchfab model by its UID.
    Use this to visually confirm a model before downloading.
    
    Parameters:
    - uid: The unique identifier of the Sketchfab model (obtained from search_sketchfab_models)
    
    Returns the model's thumbnail as an Image for visual confirmation.
    """
    try:
        blender = get_blender_connection()
        logger.info(f"Getting Sketchfab model preview for UID: {uid}")
        
        result = blender.send_command("get_sketchfab_model_preview", {"uid": uid})
        
        if result is None:
            raise Exception("Received no response from Blender")
        
        if "error" in result:
            raise Exception(result["error"])
        
        # Decode base64 image data
        image_data = base64.b64decode(result["image_data"])
        img_format = result.get("format", "jpeg")
        
        # Log model info
        model_name = result.get("model_name", "Unknown")
        author = result.get("author", "Unknown")
        logger.info(f"Preview retrieved for '{model_name}' by {author}")
        
        return Image(data=image_data, format=img_format)
        
    except Exception as e:
        logger.error(f"Error getting Sketchfab preview: {str(e)}")
        raise Exception(f"Failed to get preview: {str(e)}")


@mcp.tool()
@rich_telemetry_tool("download_sketchfab_model")
def download_sketchfab_model(
    ctx: Context,
    uid: str,
    target_size: float, user_prompt: str = "") -> str:
    """
    Download and import a Sketchfab model by its UID.
    The model will be scaled so its largest dimension equals target_size.
    
    Parameters:
    - uid: The unique identifier of the Sketchfab model
    - target_size: REQUIRED. The target size in Blender units/meters for the largest dimension.
                  You must specify the desired size for the model.
                  Examples:
                  - Chair: target_size=1.0 (1 meter tall)
                  - Table: target_size=0.75 (75cm tall)
                  - Car: target_size=4.5 (4.5 meters long)
                  - Person: target_size=1.7 (1.7 meters tall)
                  - Small object (cup, phone): target_size=0.1 to 0.3
    
    Returns a message with import details including object names, dimensions, and bounding box.
    The model must be downloadable and you must have proper access rights.
    """
    try:
        blender = get_blender_connection()
        logger.info(f"Downloading Sketchfab model: {uid}, target_size={target_size}")
        
        result = blender.send_command("download_sketchfab_model", {
            "uid": uid,
            "normalize_size": True,  # Always normalize
            "target_size": target_size
        })
        
        if result is None:
            logger.error("Received None result from Sketchfab download")
            return "Error: Received no response from Sketchfab download request"
            
        if "error" in result:
            logger.error(f"Error from Sketchfab download: {result['error']}")
            return f"Error: {result['error']}"
        
        if result.get("success"):
            imported_objects = result.get("imported_objects", [])
            object_names = ", ".join(imported_objects) if imported_objects else "none"
            
            output = f"Successfully imported model.\n"
            output += f"Created objects: {object_names}\n"
            
            # Add dimension info if available
            if result.get("dimensions"):
                dims = result["dimensions"]
                output += f"Dimensions (X, Y, Z): {dims[0]:.3f} x {dims[1]:.3f} x {dims[2]:.3f} meters\n"
            
            # Add bounding box info if available
            if result.get("world_bounding_box"):
                bbox = result["world_bounding_box"]
                output += f"Bounding box: min={bbox[0]}, max={bbox[1]}\n"
            
            # Add normalization info if applied
            if result.get("normalized"):
                scale = result.get("scale_applied", 1.0)
                output += f"Size normalized: scale factor {scale:.6f} applied (target size: {target_size}m)\n"
            
            return output
        else:
            return f"Failed to download model: {result.get('message', 'Unknown error')}"
    except Exception as e:
        logger.error(f"Error downloading Sketchfab model: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return f"Error downloading Sketchfab model: {str(e)}"

def _is_valid_http_url(url) -> bool:
    """Return True if url is an absolute http(s) URL with a host"""
    if not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)

def _process_bbox(original_bbox: list[float] | list[int] | None) -> list[int] | None:
    if original_bbox is None:
        return None
    if all(isinstance(i, int) for i in original_bbox):
        return original_bbox
    if any(i<=0 for i in original_bbox):
        raise ValueError("Incorrect number range: bbox must be bigger than zero!")
    return [int(float(i) / max(original_bbox) * 100) for i in original_bbox] if original_bbox else None

@mcp.tool()
@rich_telemetry_tool("generate_hyper3d_model_via_text")
def generate_hyper3d_model_via_text(
    ctx: Context,
    text_prompt: str,
    bbox_condition: list[float]=None, user_prompt: str = "") -> str:
    """
    Generate 3D asset using Hyper3D by giving description of the desired asset, and import the asset into Blender.
    The 3D asset has built-in materials.
    The generated model has a normalized size, so re-scaling after generation can be useful.

    Parameters:
    - text_prompt: A short description of the desired model in **English**.
    - bbox_condition: Optional. If given, it has to be a list of floats of length 3. Controls the ratio between [Length, Width, Height] of the model.

    Returns a message indicating success or failure.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("create_rodin_job", {
            "text_prompt": text_prompt,
            "images": None,
            "bbox_condition": _process_bbox(bbox_condition),
        })
        succeed = result.get("submit_time", False)
        if succeed:
            return json.dumps({
                "task_uuid": result["uuid"],
                "subscription_key": result["jobs"]["subscription_key"],
            })
        else:
            return json.dumps(result)
    except Exception as e:
        logger.error(f"Error generating Hyper3D task: {str(e)}")
        return f"Error generating Hyper3D task: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("generate_hyper3d_model_via_images")
def generate_hyper3d_model_via_images(
    ctx: Context,
    input_image_paths: list[str]=None,
    input_image_urls: list[str]=None,
    bbox_condition: list[float]=None, user_prompt: str = "") -> str:
    """
    Generate 3D asset using Hyper3D by giving images of the wanted asset, and import the generated asset into Blender.
    The 3D asset has built-in materials.
    The generated model has a normalized size, so re-scaling after generation can be useful.
    
    Parameters:
    - input_image_paths: The **absolute** paths of input images. Even if only one image is provided, wrap it into a list. Required if Hyper3D Rodin in MAIN_SITE mode.
    - input_image_urls: The URLs of input images. Even if only one image is provided, wrap it into a list. Required if Hyper3D Rodin in FAL_AI mode.
    - bbox_condition: Optional. If given, it has to be a list of ints of length 3. Controls the ratio between [Length, Width, Height] of the model.

    Only one of {input_image_paths, input_image_urls} should be given at a time, depending on the Hyper3D Rodin's current mode.
    Returns a message indicating success or failure.
    """
    if input_image_paths is not None and input_image_urls is not None:
        return f"Error: Conflict parameters given!"
    if input_image_paths is None and input_image_urls is None:
        return f"Error: No image given!"
    if input_image_paths is not None:
        if not all(os.path.exists(i) for i in input_image_paths):
            return "Error: not all image paths are valid!"
        images = []
        for path in input_image_paths:
            with open(path, "rb") as f:
                images.append(
                    (Path(path).suffix, base64.b64encode(f.read()).decode("ascii"))
                )
    elif input_image_urls is not None:
        if not all(_is_valid_http_url(i) for i in input_image_urls):
            return "Error: not all image URLs are valid! URLs must be absolute http:// or https:// URLs."
        images = input_image_urls.copy()
    try:
        blender = get_blender_connection()
        result = blender.send_command("create_rodin_job", {
            "text_prompt": None,
            "images": images,
            "bbox_condition": _process_bbox(bbox_condition),
        })
        succeed = result.get("submit_time", False)
        if succeed:
            return json.dumps({
                "task_uuid": result["uuid"],
                "subscription_key": result["jobs"]["subscription_key"],
            })
        else:
            return json.dumps(result)
    except Exception as e:
        logger.error(f"Error generating Hyper3D task: {str(e)}")
        return f"Error generating Hyper3D task: {str(e)}"

@mcp.tool()
@telemetry_tool("poll_rodin_job_status")
def poll_rodin_job_status(
    ctx: Context,
    subscription_key: str=None,
    request_id: str=None,
):
    """
    Check if the Hyper3D Rodin generation task is completed.

    For Hyper3D Rodin mode MAIN_SITE:
        Parameters:
        - subscription_key: The subscription_key given in the generate model step.

        Returns a list of status. The task is done if all status are "Done".
        If "Failed" showed up, the generating process failed.
        This is a polling API, so only proceed if the status are finally determined ("Done" or "Canceled").

    For Hyper3D Rodin mode FAL_AI:
        Parameters:
        - request_id: The request_id given in the generate model step.

        Returns the generation task status. The task is done if status is "COMPLETED".
        The task is in progress if status is "IN_PROGRESS".
        If status other than "COMPLETED", "IN_PROGRESS", "IN_QUEUE" showed up, the generating process might be failed.
        This is a polling API, so only proceed if the status are finally determined ("COMPLETED" or some failed state).
    """
    try:
        blender = get_blender_connection()
        kwargs = {}
        if subscription_key:
            kwargs = {
                "subscription_key": subscription_key,
            }
        elif request_id:
            kwargs = {
                "request_id": request_id,
            }
        result = blender.send_command("poll_rodin_job_status", kwargs)
        return result
    except Exception as e:
        logger.error(f"Error generating Hyper3D task: {str(e)}")
        return f"Error generating Hyper3D task: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("import_generated_asset")
def import_generated_asset(
    ctx: Context,
    name: str,
    task_uuid: str=None,
    request_id: str=None,
):
    """
    Import the asset generated by Hyper3D Rodin after the generation task is completed.

    Parameters:
    - name: The name of the object in scene
    - task_uuid: For Hyper3D Rodin mode MAIN_SITE: The task_uuid given in the generate model step.
    - request_id: For Hyper3D Rodin mode FAL_AI: The request_id given in the generate model step.

    Only give one of {task_uuid, request_id} based on the Hyper3D Rodin Mode!
    Return if the asset has been imported successfully.
    """
    try:
        blender = get_blender_connection()
        kwargs = {
            "name": name
        }
        if task_uuid:
            kwargs["task_uuid"] = task_uuid
        elif request_id:
            kwargs["request_id"] = request_id
        result = blender.send_command("import_generated_asset", kwargs)
        return result
    except Exception as e:
        logger.error(f"Error generating Hyper3D task: {str(e)}")
        return f"Error generating Hyper3D task: {str(e)}"

@mcp.tool()
def get_hunyuan3d_status(ctx: Context, user_prompt: str = "") -> str:
    """
    Check if Hunyuan3D integration is enabled in Blender.
    Returns a message indicating whether Hunyuan3D features are available.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_hunyuan3d_status")
        message = result.get("message", "")
        return message
    except Exception as e:
        logger.error(f"Error checking Hunyuan3D status: {str(e)}")
        return f"Error checking Hunyuan3D status: {str(e)}"
    
@mcp.tool()
@rich_telemetry_tool("generate_hunyuan3d_model")
def generate_hunyuan3d_model(
    ctx: Context,
    text_prompt: str = None,
    input_image_url: str = None, user_prompt: str = "") -> str:
    """
    Generate 3D asset using Hunyuan3D by providing either text description, image reference, 
    or both for the desired asset, and import the asset into Blender.
    The 3D asset has built-in materials.
    
    Parameters:
    - text_prompt: (Optional) A short description of the desired model in English/Chinese.
    - input_image_url: (Optional) The local or remote url of the input image. Accepts None if only using text prompt.

    Returns: 
    - When successful, returns a JSON with job_id (format: "job_xxx") indicating the task is in progress
    - When the job completes, the status will change to "DONE" indicating the model has been imported
    - Returns error message if the operation fails
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("create_hunyuan_job", {
            "text_prompt": text_prompt,
            "image": input_image_url,
        })
        if "JobId" in result.get("Response", {}):
            job_id = result["Response"]["JobId"]
            formatted_job_id = f"job_{job_id}"
            return json.dumps({
                "job_id": formatted_job_id,
            })
        return json.dumps(result)
    except Exception as e:
        logger.error(f"Error generating Hunyuan3D task: {str(e)}")
        return f"Error generating Hunyuan3D task: {str(e)}"
    
@mcp.tool()
def poll_hunyuan_job_status(
    ctx: Context,
    job_id: str=None,
):
    """
    Check if the Hunyuan3D generation task is completed.

    For Hunyuan3D:
        Parameters:
        - job_id: The job_id given in the generate model step.

        Returns the generation task status. The task is done if status is "DONE".
        The task is in progress if status is "RUN".
        If status is "DONE", returns ResultFile3Ds, which is the generated ZIP model path
        When the status is "DONE", the response includes a field named ResultFile3Ds that contains the generated ZIP file path of the 3D model in OBJ format.
        This is a polling API, so only proceed if the status are finally determined ("DONE" or some failed state).
    """
    try:
        blender = get_blender_connection()
        kwargs = {
            "job_id": job_id,
        }
        result = blender.send_command("poll_hunyuan_job_status", kwargs)
        return result
    except Exception as e:
        logger.error(f"Error generating Hunyuan3D task: {str(e)}")
        return f"Error generating Hunyuan3D task: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("import_generated_asset_hunyuan")
def import_generated_asset_hunyuan(
    ctx: Context,
    name: str,
    zip_file_url: str,
):
    """
    Import the asset generated by Hunyuan3D after the generation task is completed.

    Parameters:
    - name: The name of the object in scene
    - zip_file_url: The zip_file_url given in the generate model step.

    Return if the asset has been imported successfully.
    """
    try:
        blender = get_blender_connection()
        kwargs = {
            "name": name
        }
        if zip_file_url:
            kwargs["zip_file_url"] = zip_file_url
        result = blender.send_command("import_generated_asset_hunyuan", kwargs)
        return result
    except Exception as e:
        logger.error(f"Error generating Hunyuan3D task: {str(e)}")
        return f"Error generating Hunyuan3D task: {str(e)}"


@mcp.prompt()
def asset_creation_strategy() -> str:
    """Defines the preferred strategy for creating assets in Blender"""
    return """When creating 3D content in Blender, always start by checking if integrations are available:

    0. Before anything, always check the scene first: use get_scene_graph() on real scenes
       (it supports filtering and pagination); get_scene_info() is only a quick summary
       for tiny scenes.

    **IMPORTANT: Prefer structured tools over execute_blender_code**
    - Move/rotate/scale: set_transform(); placement (on ground, stacking on another
      object): place_object(). Never hand-write bounding-box math in Python.
    - Modifiers: manage_modifiers(); boolean cuts/merges: boolean_op()
    - Collections, parenting, rename, delete: organize_scene()
    - Cameras and renders: set_camera(), render_preview(), render_image()
    - Files: import_local_asset(), export_scene(), manage_project()
    - Animation: manage_timeline(), set_keyframes(), etc. (see the animation_strategy prompt)
    Only use execute_blender_code for things no structured tool covers
    (custom mesh generation, materials/node trees, lights, physics, etc.)

    **IMPORTANT: Visual Verification**
    - Use get_viewport_screenshot() BEFORE making changes to see the current state
    - Use render_preview() AFTER geometry, material or layout changes - it renders from
      several standard angles at once and is the best way to verify the result
    - This helps confirm your changes worked as expected and catch any visual issues

    **Recovering from mistakes**
    - undo_last_operation() reverts the last mutating MCP command
    - execute_blender_code(..., rollback_on_error=True) automatically undoes a script that raised

    **Step/turn budget discipline** (critical when your client meters steps or turns)
    - Prefer batch_commands() for consecutive structured edits: combine build stages,
      multi-object setups and keyframe passes into ONE call instead of one call each.
    - Verify visually at STAGE boundaries - one render_preview() per stage - not after
      every mutation: execute_blender_code already returns scene_diff and structured
      tools return their post-state, so most edits need no extra verification call.
    - Update manage_assignment at each stage boundary so a session killed by a client
      step cap is resumable: the user pastes the same prompt in a new chat and the
      next agent continues from the record.
    - At session start on an existing file, ALWAYS call manage_assignment(action="read")
      first - if a prior record exists, continue its plan instead of restarting.

    **Session continuity (assignment record)**
    - Before any file operation or multi-step build: check get_scene_info's filepath to
      confirm WHICH file is open - never assume. To work on a copy, prefer manage_project
      save_as FIRST (from the currently open file) over open-then-save_as; avoid opening
      different files mid-session unless required, and re-verify filepath afterward.
    - At the START of a session on an existing file, call manage_assignment(action="read")
      to pick up the assignment record (title, plan, decisions, log) cheaply.
    - Before any multi-step build, call manage_assignment(action="start", title=..., plan=[...]).
    - Keep it updated as you work: mark steps done and record decisions/conventions.
    - When the assignment is complete, write a handoff (action="handoff") and suggest the
      user continue further edits in a NEW chat - the next agent resumes from the
      assignment record and scene for a fraction of the tokens.
    - If the work belongs to a campaign, locate the campaign folder (campaign.json), read
      it and the brand's video-brand-pack.json it references, and honor
      colors/title_pairs/fonts/logo guardrails in all titles and materials. Campaign
      workspace root: D:\\Dev\\VideoProduction.
    1. First use the following tools to verify if the following integrations are enabled:
        1. PolyHaven
            Use get_polyhaven_status() to verify its status
            If PolyHaven is enabled:
            - For objects/models: Use download_polyhaven_asset() with asset_type="models"
            - For materials/textures: Use download_polyhaven_asset() with asset_type="textures"
            - For environment lighting: Use download_polyhaven_asset() with asset_type="hdris"
        2. Sketchfab
            Sketchfab is good at Realistic models, and has a wider variety of models than PolyHaven.
            Use get_sketchfab_status() to verify its status
            If Sketchfab is enabled:
            - For objects/models: First search using search_sketchfab_models() with your query
            - Then download specific models using download_sketchfab_model() with the UID
            - Note that only downloadable models can be accessed, and API key must be properly configured
            - Sketchfab has a wider variety of models than PolyHaven, especially for specific subjects
        3. Hyper3D(Rodin)
            Hyper3D Rodin is good at generating 3D models for single item.
            So don't try to:
            1. Generate the whole scene with one shot
            2. Generate ground using Hyper3D
            3. Generate parts of the items separately and put them together afterwards

            Use get_hyper3d_status() to verify its status
            If Hyper3D is enabled:
            - For objects/models, do the following steps:
                1. Create the model generation task
                    - Use generate_hyper3d_model_via_images() if image(s) is/are given
                    - Use generate_hyper3d_model_via_text() if generating 3D asset using text prompt
                    If key type is free_trial and insufficient balance error returned, tell the user that the free trial key can only generated limited models everyday, they can choose to:
                    - Wait for another day and try again
                    - Go to hyper3d.ai to find out how to get their own API key
                    - Go to fal.ai to get their own private API key
                2. Poll the status
                    - Use poll_rodin_job_status() to check if the generation task has completed or failed
                3. Import the asset
                    - Use import_generated_asset() to import the generated GLB model the asset
                4. After importing the asset, ALWAYS check the world_bounding_box of the imported mesh, and adjust the mesh's location and size
                    Adjust the imported mesh's location, scale, rotation, so that the mesh is on the right spot.

                You can reuse assets previous generated by running python code to duplicate the object, without creating another generation task.
        4. Hunyuan3D
            Hunyuan3D is good at generating 3D models for single item.
            So don't try to:
            1. Generate the whole scene with one shot
            2. Generate ground using Hunyuan3D
            3. Generate parts of the items separately and put them together afterwards

            Use get_hunyuan3d_status() to verify its status
            If Hunyuan3D is enabled:
                if Hunyuan3D mode is "OFFICIAL_API":
                    - For objects/models, do the following steps:
                        1. Create the model generation task
                            - Use generate_hunyuan3d_model by providing either a **text description** OR an **image(local or urls) reference**.
                            - Go to cloud.tencent.com out how to get their own SecretId and SecretKey
                        2. Poll the status
                            - Use poll_hunyuan_job_status() to check if the generation task has completed or failed
                        3. Import the asset
                            - Use import_generated_asset_hunyuan() to import the generated OBJ model the asset
                    if Hunyuan3D mode is "LOCAL_API":
                        - For objects/models, do the following steps:
                        1. Create the model generation task
                            - Use generate_hunyuan3d_model if image (local or urls)  or text prompt is given and import the asset

                You can reuse assets previous generated by running python code to duplicate the object, without creating another generation task.

    3. Always check the world_bounding_box for each item so that:
        - Ensure that all objects that should not be clipping are not clipping.
        - Items have right spatial relationship.
        - Fix positions with place_object() (ground/stacking) and set_transform() -
          do not hand-write AABB math in execute_blender_code.

    4. Recommended asset source priority:
        - For specific existing objects: First try Sketchfab, then PolyHaven
        - For generic objects/furniture: First try PolyHaven, then Sketchfab
        - For custom or unique items not available in libraries: Use Hyper3D Rodin or Hunyuan3D
        - For environment lighting: Use PolyHaven HDRIs
        - For materials/textures: Use PolyHaven textures

    Only fall back to scripting when:
    - PolyHaven, Sketchfab, Hyper3D, and Hunyuan3D are all disabled
    - A simple primitive is explicitly requested
    - No suitable asset exists in any of the libraries
    - Hyper3D Rodin or Hunyuan3D failed to generate the desired asset
    - The task specifically requires a basic material/color

    **Best Practices:**
    - Always verify visually after completing a task: render_preview() (multiple angles)
      or get_viewport_screenshot()
    - Always call get_scene_graph() after completing a task to verify the changes worked
    - When executing multiple operations, verify intermediate steps visually to confirm each one
    - If something looks wrong in the render or scene graph, investigate and fix before
      proceeding - undo_last_operation() can revert a bad step
    """

@mcp.prompt()
def animation_strategy() -> str:
    """Defines the preferred workflow for animating in Blender"""
    return """When animating in Blender, follow this workflow:

    1. Timeline first: call manage_timeline() to check or set fps, frame_start and
       frame_end BEFORE placing any keyframes, so frames land where you expect.
       Time in seconds = frame / fps (e.g. at 24 fps, frame 48 is the 2-second mark).

    2. Block the motion: use set_keyframes() to key poses at sparse frames - start,
       key poses, end. Don't keyframe every frame; Blender interpolates between keys.
       Use data_path "location" / "rotation_euler" (radians) / "scale", or pose-bone
       paths like 'pose.bones["Bone"].rotation_euler' for armatures.

    3. Refine the feel: use set_keyframe_interpolation() for easing - BEZIER for
       natural motion, LINEAR for mechanical motion, CONSTANT for stepping.
       For seamless loops (spinning, bobbing), use make_cyclic=True.

    4. Verify visually: render_animation_preview() renders a handful of frames across
       the range so you can check the motion. Use get_animation_info() to inspect
       fcurves and keyframes when something looks off; delete_keyframes() removes bad keys.

    5. Camera moves: set up the shot with set_camera() (frame_objects / preset /
       look_at), then animate the camera object itself with set_keyframes() on its
       "location" and "rotation_euler".

    6. Final output: render_image() for a full-quality still of the current frame.

    Performance note: every command must finish within 180 seconds. Keep renders small
    (low resolution/samples) and prefer render_preview / render_animation_preview over
    full renders while iterating.

    Step/turn budget discipline (critical when your client meters steps or turns):
    - Prefer batch_commands() for consecutive structured edits - a whole keyframe
      pass (set_keyframes on several objects + set_keyframe_interpolation +
      manage_timeline) fits in ONE call.
    - Verify visually at STAGE boundaries (one render_animation_preview per blocking/
      easing pass), not after every keyframe: structured tools already return their
      post-state and execute_blender_code returns scene_diff.
    - Update manage_assignment at each stage boundary so a session killed by a client
      step cap is resumable by pasting the same prompt in a new chat.
    - At session start on an existing file, ALWAYS call manage_assignment(action="read")
      first - if a prior record exists, continue its plan instead of restarting.

    Session continuity: keep the assignment record current with manage_assignment
    (read at session start, update as steps land, handoff when done) - see the
    asset_creation_strategy prompt for the full workflow.
    """

@mcp.prompt()
def production_strategy() -> str:
    """Defines the preferred workflow for editing and delivering video in Blender"""
    return """When assembling and delivering video from Blender (the VSE), follow this workflow:

    0. Campaign context: if the work belongs to a campaign, locate the campaign
       folder (campaign.json), read it and the brand's video-brand-pack.json it
       references, and honor colors/title_pairs/fonts/logo guardrails in all
       titles and materials. Campaign workspace root: D:\\Dev\\VideoProduction.

    0b. File identity: before any file operation or multi-step build, check
       get_scene_info's filepath to confirm WHICH file is open - never assume.
       To work on a copy, prefer manage_project save_as FIRST (from the
       currently open file) over open-then-save_as; avoid opening different
       files mid-session unless required, and re-verify filepath afterward.

    1. Format first: call get_delivery_presets(), then manage_sequence
       action="setup_timeline" with the TARGET preset (LINKEDIN_WIDE / SQUARE /
       VERTICAL) BEFORE placing any strips, so text and framing are composed for
       the final aspect ratio. All strip times are FRAMES, not seconds:
       frame = round(seconds * fps).

    2. Assemble the cut: add shot renders in story order with action="add_media"
       (movies import video only - audio is a separate add_media call). Let
       channel default to the next free channel; use set_strip to move/trim.

    3. Cut to the beat: if a beat map JSON exists in the project folder, read it
       and place cuts and text on beat frames (frame = round(beat_time_seconds *
       fps)). Align strip starts and transitions with those frames.

    4. Brand it: add text overlays with action="add_text" (BOTTOM for captions,
       CENTER for title cards). If a video-brand-pack.json exists in the project
       folder, use its colors and fonts (font_path) for every text strip.

    5. Polish: action="add_transition" (CROSS for soft cuts, WIPE for energy)
       between adjacent shots - non-overlapping strips are auto-shifted;
       action="add_fade" (IN on the first strip, OUT on the last, and on audio).

    6. Verify before encoding: keep total clip length 15-60 seconds for social.
       Check the returned "timeline" state after each edit, and use
       render_animation_preview() to eyeball frames BEFORE a full encode. For
       flat 2D / kinetic-typography work with keyframed Alpha or opacity fades,
       pass engine="EEVEE" - the default fast preview is alpha-blind and stacks
       layers, so it misreads that motion.

    7. Deliver per platform: render_sequence(filepath, preset=...) for each
       required format - WIDE / SQUARE / VERTICAL are separate renders: re-run
       setup_timeline with the next preset (strips keep their timing), then
       render_sequence again. The encode defaults to view_transform="Standard"
       (no AgX/Filmic tone-mapping), which is correct for muxing already-finished
       footage and text; only override it when grading raw 3D scene strips. Use
       wait=True for clips up to ~90s; longer renders need wait=False plus
       status_only=True polling (interactive Blender only).

    8. Hand off: manage_project save, then manage_assignment action="handoff"
       recording delivered files and any remaining formats.
    """

# Main execution

def main():
    """Run the MCP server"""
    # When run by hand (stdin is a TTY) the server appears to "hang" while it
    # silently waits for an MCP client; log a hint so that state is obvious.
    # Launched by a client, stdin is a pipe so this is skipped, and logging goes
    # to stderr, never to the stdio protocol on stdout.
    try:
        interactive = sys.stdin.isatty()
    except (AttributeError, OSError):
        interactive = False
    if interactive:
        logger.info(
            "BlenderMCP is an MCP server and is meant to be launched by your MCP "
            "client (Claude Desktop, Cursor, VS Code, ...), not run by hand. "
            "It will now wait silently for a client on stdin -- that is normal, "
            "not a hang. Press Ctrl-C to exit. "
            "Setup guide: https://github.com/ahujasid/blender-mcp#installation"
        )
    mcp.run()

if __name__ == "__main__":
    main()