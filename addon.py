# Code created by Siddharth Ahuja: www.github.com/ahujasid © 2025

import re
import ast
import bpy
import math
import random
import collections
import mathutils
import json
import threading
import socket
import time
import requests
import tempfile
import traceback
import os
import shutil
import zipfile
from bpy.props import IntProperty, BoolProperty, StringProperty
import io
from datetime import datetime
import hashlib, hmac, base64
import os.path as osp
from contextlib import redirect_stdout, suppress

bl_info = {
    "name": "Blender MCP",
    "author": "BlenderMCP",
    "version": (1, 8, 2),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > BlenderMCP",
    "description": "Connect Blender to Claude via MCP",
    "category": "Interface",
}

ADDON_VERSION = "1.8.2"

# Shown to agents talking through a legacy (pre-1.7.1 handshake) MCP server;
# such servers drop stdout/errors from the new execute_code result shape.
LEGACY_SERVER_NOTICE = (
    "Your blender-mcp server is outdated for this addon: stdout/errors from "
    "code execution are not forwarded by old servers. Ask the user to point "
    "their MCP config at the current server (uvx --from <repo> blender-mcp)."
)

# Commands whose results legacy servers pass through to the agent as
# text/json - the only responses where a compat notice can actually be seen.
LEGACY_NOTICE_COMMANDS = ("execute_code", "get_scene_info", "get_object_info")

RODIN_FREE_TRIAL_KEY = "vibecoding"

# Update check: latest released version is published in the VERSION file on GitHub
UPDATE_VERSION_URL = "https://raw.githubusercontent.com/SIM5Y/blender-mcp/main/VERSION"
UPDATE_PAGE_URL = "https://github.com/SIM5Y/blender-mcp"

# Update-check state (written by the background worker thread, read by the panel)
_UPDATE_INFO = {
    "checked": False,
    "checking": False,
    "latest": None,
    "update_available": False,
    "error": None,
}

# One-shot guard: schedule the automatic startup update check once per session
_UPDATE_CHECK_SCHEDULED = False

def _version_tuple(version_str):
    """Parse a version string like "1.7.1" into (1, 7, 1); None if malformed."""
    try:
        return tuple(int(p) for p in str(version_str).strip().split("."))
    except (ValueError, AttributeError):
        return None

def _tag_viewports_redraw():
    """Schedule a VIEW_3D redraw on the main thread (safe from any thread)."""
    def _tag():
        try:
            for w in bpy.context.window_manager.windows:
                for a in w.screen.areas:
                    if a.type == 'VIEW_3D':
                        a.tag_redraw()
        except Exception:
            pass
        return None
    try:
        bpy.app.timers.register(_tag, first_interval=0.0)
    except Exception:
        pass

def _check_for_updates_async():
    """Fetch the latest version from GitHub in a daemon thread.

    Never blocks the UI or a command; all failures are swallowed into
    _UPDATE_INFO["error"] (the panel just stays quiet on errors).
    """
    if _UPDATE_INFO["checking"]:
        return
    _UPDATE_INFO["checking"] = True
    _UPDATE_INFO["error"] = None

    def _worker():
        try:
            response = requests.get(UPDATE_VERSION_URL, timeout=(10, 60))
            response.raise_for_status()
            latest = response.text.strip()
            latest_t = _version_tuple(latest)
            current_t = _version_tuple(ADDON_VERSION)
            _UPDATE_INFO["latest"] = latest or None
            # Malformed local or remote version -> treat as "no update"
            _UPDATE_INFO["update_available"] = bool(
                latest_t is not None and current_t is not None and latest_t > current_t
            )
        except Exception as e:
            _UPDATE_INFO["error"] = str(e)
            _UPDATE_INFO["update_available"] = False
        finally:
            _UPDATE_INFO["checking"] = False
            _UPDATE_INFO["checked"] = True
            _tag_viewports_redraw()

    threading.Thread(target=_worker, daemon=True).start()

# Commands that modify the scene: an undo checkpoint is pushed before each one
# so undo_last / rollback_on_error can revert the change.
MUTATING_COMMANDS = {
    "execute_code",
    "set_transform",
    "place_object",
    "manage_modifiers",
    "boolean_op",
    "organize_scene",
    "set_keyframes",
    "delete_keyframes",
    "set_keyframe_interpolation",
    "manage_timeline",
    "import_local_asset",
    "manage_sequence",
    "render_sequence",
    # Integration commands that modify the scene
    "download_polyhaven_asset",
    "set_texture",
    "download_sketchfab_model",
    "import_generated_asset",
    "import_generated_asset_hunyuan",
}

# Valid keyframe interpolation / easing identifiers (Blender 4.2+ and 5.x)
KEYFRAME_INTERPOLATIONS = {
    "CONSTANT", "LINEAR", "BEZIER", "SINE", "QUAD", "CUBIC", "QUART",
    "QUINT", "EXPO", "CIRC", "BACK", "BOUNCE", "ELASTIC",
}
KEYFRAME_EASINGS = {"AUTO", "EASE_IN", "EASE_OUT", "EASE_IN_OUT"}

# Camera preset view directions: vectors pointing from the framed target
# toward the camera (normalized before use).
CAMERA_PRESETS = {
    "front": (0.0, -1.0, 0.0),
    "right": (1.0, 0.0, 0.0),
    "top": (0.0, 0.0, 1.0),
    "isometric": (1.0, -1.0, 0.8),
    "three_quarter": (1.0, -1.0, 0.35),
}

# Platform delivery presets for the video sequence editor (VSE).
# Keep in sync with DELIVERY_PRESETS in src/blender_mcp/server.py.
DELIVERY_PRESETS = {
    "LINKEDIN_WIDE": {"resolution": (1920, 1080), "fps": 25},
    "SQUARE": {"resolution": (1080, 1080), "fps": 25},
    "VERTICAL": {"resolution": (1080, 1920), "fps": 25},
}

# Media extensions recognized by manage_sequence add_media
VSE_MOVIE_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
VSE_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".exr"}
VSE_AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg"}

# FFMPEG container -> output file extension (render_sequence)
VSE_CONTAINER_EXTENSIONS = {
    "MPEG4": ".mp4",
    "MKV": ".mkv",
    "WEBM": ".webm",
    "QUICKTIME": ".mov",
}

# Async VSE render job state (written by bpy.app.handlers callbacks below,
# read by render_sequence(status_only=True)).
_RENDER_JOB = {
    "active": False,
    "frame_current": None,
    "frame_end": None,
    "filepath": None,
    "done": False,
    "cancelled": False,
    "error": None,
    "started_at": None,
}
# Render settings snapshot to restore when an async render finishes/cancels
_RENDER_JOB_RESTORE = {}
_RENDER_HANDLERS_ADDED = False

# Last output written by a panel render (or a finished async clip render);
# shown in the panel's Output box with an "open folder" button.
_LAST_RENDER_PATH = None


def _finish_render_job(done=False, cancelled=False):
    """Mark the async VSE render job finished and restore render settings."""
    global _LAST_RENDER_PATH
    if not _RENDER_JOB.get("active"):
        return
    _RENDER_JOB["active"] = False
    _RENDER_JOB["done"] = bool(done)
    _RENDER_JOB["cancelled"] = bool(cancelled)
    if done and _RENDER_JOB.get("filepath"):
        _LAST_RENDER_PATH = _RENDER_JOB["filepath"]
    snap = dict(_RENDER_JOB_RESTORE)
    _RENDER_JOB_RESTORE.clear()
    if snap:
        try:
            BlenderMCPServer._restore_vse_render_settings(snap)
        except Exception:
            pass


@bpy.app.handlers.persistent
def _mcp_vse_render_post(scene, _depsgraph=None):
    if _RENDER_JOB.get("active"):
        with suppress(Exception):
            _RENDER_JOB["frame_current"] = scene.frame_current


@bpy.app.handlers.persistent
def _mcp_vse_render_complete(scene, _depsgraph=None):
    _finish_render_job(done=True)


@bpy.app.handlers.persistent
def _mcp_vse_render_cancel(scene, _depsgraph=None):
    _finish_render_job(cancelled=True)


def _ensure_render_handlers():
    """Register the async render job handlers once per session."""
    global _RENDER_HANDLERS_ADDED
    if _RENDER_HANDLERS_ADDED:
        return
    bpy.app.handlers.render_post.append(_mcp_vse_render_post)
    bpy.app.handlers.render_complete.append(_mcp_vse_render_complete)
    bpy.app.handlers.render_cancel.append(_mcp_vse_render_cancel)
    _RENDER_HANDLERS_ADDED = True

def _save_version_snapshot():
    """Write the next numbered version copy to <blend_dir>/versions.

    Shared by the manage_project 'save_version' handler, the panel's
    Save Version operator, and the session-end auto-version hook.
    Returns the written filepath; raises ValueError when the project
    has never been saved.
    """
    current = bpy.data.filepath
    if not current:
        raise ValueError(
            "The project has never been saved. Save the .blend first "
            "(Save Project in the panel, or manage_project action 'save_as')."
        )
    stem = os.path.splitext(os.path.basename(current))[0]
    versions_dir = os.path.join(os.path.dirname(current), "versions")
    os.makedirs(versions_dir, exist_ok=True)
    pattern = re.compile(re.escape(stem) + r"_v(\d+)\.blend$", re.IGNORECASE)
    highest = 0
    for fname in os.listdir(versions_dir):
        match = pattern.match(fname)
        if match:
            highest = max(highest, int(match.group(1)))
    version_path = os.path.join(versions_dir, f"{stem}_v{highest + 1:03d}.blend")
    try:
        bpy.ops.wm.save_as_mainfile(filepath=version_path, copy=True)
    except Exception as e:
        raise Exception(f"Version save failed: {str(e)}")
    return version_path


def _panel_render_dir():
    """Output folder for panel renders: <blend_dir>/render, or temp if unsaved."""
    if bpy.data.filepath:
        out_dir = os.path.join(os.path.dirname(bpy.data.filepath), "render")
    else:
        out_dir = os.path.join(tempfile.gettempdir(), "blendermcp_render")
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def _panel_render_stem():
    """Filename stem for panel renders (blend file name, or 'untitled')."""
    if bpy.data.filepath:
        return os.path.splitext(os.path.basename(bpy.data.filepath))[0]
    return "untitled"


# Persistent namespace for execute_code REPL semantics (lazily initialized)
_EXEC_NAMESPACE = None

def _init_exec_namespace():
    """Build a fresh execution namespace for execute_code."""
    import bmesh
    from mathutils import Vector, Matrix, Euler, Quaternion
    return {
        "bpy": bpy,
        "bmesh": bmesh,
        "mathutils": mathutils,
        "math": math,
        "json": json,
        "random": random,
        "Vector": Vector,
        "Matrix": Matrix,
        "Euler": Euler,
        "Quaternion": Quaternion,
    }

# ---------------------------------------------------------------------------
# Assignment continuity: a persistent per-project assignment record stored in
# the .blend (Text datablock) and mirrored to a human-readable markdown
# sidecar next to saved files so a fresh agent can resume cheaply.
# ---------------------------------------------------------------------------
ASSIGNMENT_TEXT_NAME = "MCP_Assignment"

def _assignment_load():
    """Load the assignment record from the MCP_Assignment text datablock (or None)."""
    text = bpy.data.texts.get(ASSIGNMENT_TEXT_NAME)
    if text is None:
        return None
    try:
        record = json.loads(text.as_string())
        return record if isinstance(record, dict) else None
    except Exception:
        return None

def _assignment_markdown(record):
    """Render the assignment record as human-readable markdown."""
    lines = [f"# {record.get('title', 'Untitled assignment')}", ""]
    brief = record.get("brief")
    if brief:
        lines += [str(brief), ""]
    tokens_k = int(record.get("token_estimate", 0) or 0) // 1000
    lines += [
        "## Status",
        f"{record.get('status', 'active')} — updated {record.get('updated', '?')} — ~{tokens_k}k tokens",
        "",
        "## Plan",
    ]
    for entry in record.get("plan", []):
        mark = "x" if entry.get("done") else " "
        lines.append(f"- [{mark}] {entry.get('step', '')}")
    lines.append("")
    if record.get("decisions"):
        lines.append("## Decisions & Conventions")
        lines += [f"- {d}" for d in record["decisions"]]
        lines.append("")
    if record.get("log"):
        lines.append("## Log")
        lines += [f"- {entry}" for entry in record["log"]]
        lines.append("")
    if record.get("handoff"):
        lines += ["## Handoff", str(record["handoff"]), ""]
    return "\n".join(lines)

def _assignment_sidecar_path():
    """Path of the markdown sidecar next to the saved .blend, or None if unsaved."""
    if not bpy.data.filepath:
        return None
    stem = os.path.splitext(os.path.basename(bpy.data.filepath))[0]
    return os.path.join(os.path.dirname(bpy.data.filepath), f"{stem}.assignment.md")

def _assignment_store(record):
    """Write the record to the text datablock and mirror the markdown sidecar.

    Never raises into the command path: sidecar file I/O failures are reported
    via the returned (sidecar_path, sidecar_error) tuple instead.
    """
    text = bpy.data.texts.get(ASSIGNMENT_TEXT_NAME)
    if text is None:
        text = bpy.data.texts.new(ASSIGNMENT_TEXT_NAME)
    text.clear()
    text.write(json.dumps(record, indent=2))
    sidecar_path = _assignment_sidecar_path()
    if not sidecar_path:
        return None, None
    try:
        with open(sidecar_path, "w", encoding="utf-8") as f:
            f.write(_assignment_markdown(record))
        return sidecar_path, None
    except Exception as e:
        return None, f"Could not write sidecar: {str(e)}"

@bpy.app.handlers.persistent
def _blendermcp_save_post(_dummy=None):
    """Refresh the .assignment.md sidecar whenever the .blend is saved (Ctrl+S too)."""
    try:
        record = _assignment_load()
        if not record:
            return
        server = getattr(bpy.types, "blendermcp_server", None)
        if server is not None:
            try:
                record["token_estimate"] = server._assignment_tokens(record)
            except Exception:
                pass
        _assignment_store(record)
    except Exception as e:
        print(f"BlenderMCP: assignment sidecar refresh failed: {str(e)}")

@bpy.app.handlers.persistent
def _blendermcp_load_post(_dummy=None):
    """Re-sync per-file panel state after any file load (manage_project open too).

    The socket server lives on bpy.types and survives file loads, but
    blendermcp_server_running is a per-scene property stored in the .blend -
    after opening another file it can lie about the actual server state.
    """
    try:
        server = getattr(bpy.types, "blendermcp_server", None)
        running = bool(server and server.running)
        scene = getattr(bpy.context, "scene", None)
        if scene is not None:
            try:
                scene.blendermcp_server_running = running
            except Exception:
                pass
        # Tag a redraw so the panel reflects the re-synced state
        try:
            for w in bpy.context.window_manager.windows:
                for a in w.screen.areas:
                    if a.type == 'VIEW_3D':
                        a.tag_redraw()
        except Exception:
            pass
    except Exception as e:
        print(f"BlenderMCP: load_post state resync failed: {str(e)}")

def get_credential(context, pref_name, scene_prop_name):
    """Read an API credential.

    Addon preferences win (stored in Blender's user config, not in .blend
    files); the legacy per-scene property is kept as a fallback so existing
    setups keep working, and a BLENDERMCP_* environment variable is the
    final fallback for headless/CI setups (#235).
    """
    scene_value = getattr(context.scene, scene_prop_name, "")
    # Let the free-trial button temporarily override a persistent private
    # key for this session without overwriting it (#235).
    if pref_name == 'hyper3d_api_key' and scene_value == RODIN_FREE_TRIAL_KEY:
        return scene_value
    try:
        addon_entry = context.preferences.addons.get(__name__)
        if addon_entry:
            value = getattr(addon_entry.preferences, pref_name, "")
            if value:
                return value
    except (AttributeError, KeyError):
        pass
    if scene_value:
        return scene_value
    return os.getenv("BLENDERMCP_" + pref_name.upper(), "")

# Add User-Agent as required by Poly Haven API
REQ_HEADERS = requests.utils.default_headers()
REQ_HEADERS.update({"User-Agent": "blender-mcp"})

class BlenderMCPServer:
    def __init__(self, host='localhost', port=9876):
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None
        # Status tracking (read by the UI panel)
        self.active_client = None
        self.client_connected = False
        self.client_address = None
        self.last_command_type = None
        self.last_command_time = None
        self.last_error = None  # {"time", "command", "message"} or None
        self.commands_executed = 0
        self.executing = False
        self.activity_log = collections.deque(maxlen=50)
        # Session token awareness: bytes sent over the socket (est. tokens = bytes/4)
        self.bytes_sent = 0
        # MCP server (client) identity, reported via set_client_info handshake
        self.client_version = None
        self.client_name = None
        # Legacy/unknown MCP server detection: modern servers (>= 1.7.1) call
        # set_client_info right after connecting; legacy servers never do.
        # Reset on every new connection and on set_client_info arrival.
        self.legacy_client = False
        self._saw_client_info = False
        self._legacy_notice_counter = 0
        # Assignment token accounting (see manage_assignment / _assignment_tokens)
        self._assignment_token_base = None
        self._assignment_prior_tokens = 0

    def _get_hyper3d_api_key(self):
        return get_credential(bpy.context, 'hyper3d_api_key', 'blendermcp_hyper3d_api_key')

    def _get_sketchfab_api_key(self):
        return get_credential(bpy.context, 'sketchfab_api_key', 'blendermcp_sketchfab_api_key')

    def _get_hunyuan3d_secret_id(self):
        return get_credential(bpy.context, 'hunyuan3d_secret_id', 'blendermcp_hunyuan3d_secret_id')

    def _get_hunyuan3d_secret_key(self):
        return get_credential(bpy.context, 'hunyuan3d_secret_key', 'blendermcp_hunyuan3d_secret_key')

    def _get_hunyuan3d_api_url(self):
        return get_credential(bpy.context, 'hunyuan3d_api_url', 'blendermcp_hunyuan3d_api_url') or "http://localhost:8081"

    def start(self):
        if bpy.app.background:
            print("BlenderMCP: cannot start server in background mode (blender -b) - commands would never execute\n"
                  "BlenderMCP: run Blender with a GUI, or use a virtual display: xvfb-run -a blender")
            return

        if self.running:
            print("Server is already running")
            return

        self.running = True

        try:
            # Create socket
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            self.socket.listen(1)

            # Start server thread
            self.server_thread = threading.Thread(target=self._server_loop)
            self.server_thread.daemon = True
            self.server_thread.start()

            print(f"BlenderMCP server started on {self.host}:{self.port}")
        except Exception as e:
            print(f"Failed to start server: {str(e)}")
            self.stop()

    def stop(self):
        self.running = False

        # Close any active client connection
        if self.active_client:
            try:
                self.active_client.close()
            except:
                pass
            self.active_client = None
        self.client_connected = False
        self.client_address = None

        # Close socket
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None

        # Wait for thread to finish
        if self.server_thread:
            try:
                if self.server_thread.is_alive():
                    self.server_thread.join(timeout=1.0)
            except:
                pass
            self.server_thread = None

        print("BlenderMCP server stopped")

    def _server_loop(self):
        """Main server loop in a separate thread"""
        print("Server thread started")
        self.socket.settimeout(1.0)  # Timeout to allow for stopping

        while self.running:
            try:
                # Accept new connection
                try:
                    client, address = self.socket.accept()
                    print(f"Connected to client: {address}")

                    # Single active client: adopt the new connection,
                    # closing any previous client (its handler will exit).
                    if self.active_client is not None:
                        print(f"New client connected - closing previous client {self.client_address}")
                        try:
                            self.active_client.close()
                        except:
                            pass
                    self.active_client = client
                    self.client_connected = True
                    self.client_address = f"{address[0]}:{address[1]}"
                    # Fresh connection: legacy detection starts over
                    self.legacy_client = False
                    self._saw_client_info = False
                    self._legacy_notice_counter = 0
                    self._redraw_ui()

                    # Handle client in a separate thread
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client, address)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                except socket.timeout:
                    # Just check running condition
                    continue
                except Exception as e:
                    print(f"Error accepting connection: {str(e)}")
                    time.sleep(0.5)
            except Exception as e:
                print(f"Error in server loop: {str(e)}")
                if not self.running:
                    break
                time.sleep(0.5)

        print("Server thread stopped")

    def _handle_client(self, client, address=None):
        """Handle connected client"""
        print("Client handler started")
        client.settimeout(None)  # No timeout
        buffer = b''
        # Per-connection session stats (read on disconnect for auto-versioning)
        conn_stats = {"commands": 0, "mutated": False}

        try:
            while self.running:
                # Receive data
                try:
                    data = client.recv(8192)
                    if not data:
                        print("Client disconnected")
                        break

                    buffer += data
                    try:
                        # Try to parse command
                        command = json.loads(buffer.decode('utf-8'))
                        buffer = b''

                        # Execute command in Blender's main thread
                        def execute_wrapper():
                            cmd_type = command.get("type")
                            start_time = time.time()
                            self.executing = True
                            self.last_command_type = cmd_type
                            self.last_command_time = start_time

                            # Push an undo checkpoint before mutating commands
                            # (skipped while paused - the command will be rejected anyway)
                            conn_stats["commands"] += 1
                            if cmd_type in MUTATING_COMMANDS and \
                                    not getattr(bpy.context.scene, "blendermcp_paused", False):
                                conn_stats["mutated"] = True
                                try:
                                    bpy.ops.ed.undo_push(message=f"MCP: {cmd_type}")
                                except Exception as undo_err:
                                    print(f"Undo push failed: {str(undo_err)}")

                            response = None
                            try:
                                response = self.execute_command(command)
                                # Legacy MCP server detection + compat notice
                                # (must run before the response is serialized)
                                try:
                                    self._update_legacy_detection(conn_stats["commands"])
                                    response = self._inject_legacy_notice(cmd_type, response)
                                except Exception:
                                    pass
                                response_json = json.dumps(response)
                                try:
                                    payload = response_json.encode('utf-8')
                                    client.sendall(payload)
                                    self.bytes_sent += len(payload)
                                except:
                                    print("Failed to send response - client disconnected")
                            except Exception as e:
                                print(f"Error executing command: {str(e)}")
                                traceback.print_exc()
                                response = {
                                    "status": "error",
                                    "message": str(e)
                                }
                                try:
                                    payload = json.dumps(response).encode('utf-8')
                                    client.sendall(payload)
                                    self.bytes_sent += len(payload)
                                except:
                                    pass
                            finally:
                                self.executing = False
                                try:
                                    self._log_activity(cmd_type, command, response, start_time)
                                except Exception:
                                    pass
                                self._redraw_ui()
                            return None

                        # Schedule execution in main thread
                        bpy.app.timers.register(execute_wrapper, first_interval=0.0)
                    except json.JSONDecodeError:
                        # Incomplete data, wait for more
                        pass
                except Exception as e:
                    print(f"Error receiving data: {str(e)}")
                    break
        except Exception as e:
            print(f"Error in client handler: {str(e)}")
        finally:
            try:
                client.close()
            except:
                pass
            # Only clear connection status if we are still the active client
            # (a newer connection may already have been adopted)
            if self.active_client is client:
                self.active_client = None
                self.client_connected = False
                self.client_address = None
                self._redraw_ui()
            # Session ended: optionally snapshot a version of the work
            self._maybe_auto_version(conn_stats)
            print("Client handler stopped")

    def _maybe_auto_version(self, conn_stats):
        """Auto-save a version snapshot when an AI session ends.

        Runs on the socket thread, so the actual save is scheduled on
        Blender's main thread via bpy.app.timers. Only fires when the
        session executed at least one command AND at least one mutating
        command ran; the preference and saved-file checks happen on the
        main thread. Never raises.
        """
        try:
            if not conn_stats.get("commands") or not conn_stats.get("mutated"):
                return

            def _auto_version():
                try:
                    addon_entry = bpy.context.preferences.addons.get(__name__)
                    prefs = addon_entry.preferences if addon_entry else None
                    if prefs is None or \
                            not getattr(prefs, "auto_version_on_session_end", True):
                        return None
                    if not bpy.data.filepath:
                        return None
                    path = _save_version_snapshot()
                    self.activity_log.append({
                        "time": time.strftime("%H:%M:%S"),
                        "type": "auto_version",
                        "status": "ok",
                        "duration_ms": 0,
                        "summary": f"auto version saved: {path}"[:120],
                    })
                    print(f"BlenderMCP: auto version saved: {path}")
                    self._redraw_ui()
                except Exception as e:
                    print(f"BlenderMCP: auto version failed: {str(e)}")
                return None

            bpy.app.timers.register(_auto_version, first_interval=0.0)
        except Exception:
            pass

    def _log_activity(self, cmd_type, command, response, start_time):
        """Record a command execution in the activity log (read by the UI panel)"""
        duration_ms = int((time.time() - start_time) * 1000)
        # Handlers may report partial/external failures as {"error": ...} inside
        # a status='success' envelope (e.g. disabled integrations) - surface
        # those as errors in the log / last_error too.
        handler_error = None
        if isinstance(response, dict) and isinstance(response.get("result"), dict) \
                and "error" in response["result"]:
            handler_error = str(response["result"]["error"])
        status = "ok" if isinstance(response, dict) and response.get("status") == "success" \
            and handler_error is None else "error"
        summary = cmd_type or "?"
        if cmd_type == "execute_code":
            code = (command.get("params") or {}).get("code", "")
            first_line = code.splitlines()[0].strip() if code else ""
            if first_line:
                summary = first_line
        summary = summary[:120]
        self.commands_executed += 1
        if status == "error":
            message = handler_error or \
                (response.get("message", "") if isinstance(response, dict) else "")
            self.last_error = {
                "time": time.time(),
                "command": cmd_type,
                "message": message,
            }
        self.activity_log.append({
            "time": time.strftime("%H:%M:%S"),
            "type": cmd_type,
            "status": status,
            "duration_ms": duration_ms,
            "summary": summary,
        })

    def _redraw_ui(self):
        """Tag all 3D viewports for redraw so the panel status updates.

        Safe to call from any thread: the actual tagging is scheduled
        on Blender's main thread via a timer.
        """
        def _tag():
            try:
                for w in bpy.context.window_manager.windows:
                    for a in w.screen.areas:
                        if a.type == 'VIEW_3D':
                            a.tag_redraw()
            except Exception:
                pass
            return None
        try:
            bpy.app.timers.register(_tag, first_interval=0.0)
        except Exception:
            pass

    def execute_command(self, command):
        """Execute a command in the main Blender thread"""
        try:
            return self._execute_command_internal(command)

        except Exception as e:
            print(f"Error executing command: {str(e)}")
            traceback.print_exc()
            return {"status": "error", "message": str(e)}

    def _get_handlers(self):
        """Build the full command handler registry.

        Every command handler must be registered here. Integration handlers
        are always registered; each one starts with a guard that returns an
        actionable error when its integration is disabled.
        """
        return {
            # Always available (also allowed while paused)
            "ping": self.ping,
            "get_capabilities": self.get_capabilities,
            "get_telemetry_consent": self.get_telemetry_consent,
            "set_client_info": self.set_client_info,
            # Core
            "get_scene_info": self.get_scene_info,
            "get_scene_graph": self.get_scene_graph,
            "get_object_info": self.get_object_info,
            "get_viewport_screenshot": self.get_viewport_screenshot,
            "execute_code": self.execute_code,
            "undo_last": self.undo_last,
            # Modelling
            "set_transform": self.set_transform,
            "place_object": self.place_object,
            "manage_modifiers": self.manage_modifiers,
            "boolean_op": self.boolean_op,
            "organize_scene": self.organize_scene,
            # Animation
            "manage_timeline": self.manage_timeline,
            "set_keyframes": self.set_keyframes,
            "delete_keyframes": self.delete_keyframes,
            "set_keyframe_interpolation": self.set_keyframe_interpolation,
            "get_animation_info": self.get_animation_info,
            # Cameras & rendering
            "set_camera": self.set_camera,
            "render_preview": self.render_preview,
            "render_animation_preview": self.render_animation_preview,
            "render_image": self.render_image,
            # Pipeline
            "export_scene": self.export_scene,
            "import_local_asset": self.import_local_asset,
            "manage_project": self.manage_project,
            "manage_assignment": self.manage_assignment,
            # Video sequence editor (VSE)
            "manage_sequence": self.manage_sequence,
            "render_sequence": self.render_sequence,
            # Integration status
            "get_polyhaven_status": self.get_polyhaven_status,
            "get_hyper3d_status": self.get_hyper3d_status,
            "get_sketchfab_status": self.get_sketchfab_status,
            "get_hunyuan3d_status": self.get_hunyuan3d_status,
            # PolyHaven
            "get_polyhaven_categories": self.get_polyhaven_categories,
            "search_polyhaven_assets": self.search_polyhaven_assets,
            "download_polyhaven_asset": self.download_polyhaven_asset,
            "set_texture": self.set_texture,
            # Hyper3D Rodin
            "create_rodin_job": self.create_rodin_job,
            "poll_rodin_job_status": self.poll_rodin_job_status,
            "import_generated_asset": self.import_generated_asset,
            # Sketchfab
            "search_sketchfab_models": self.search_sketchfab_models,
            "get_sketchfab_model_preview": self.get_sketchfab_model_preview,
            "download_sketchfab_model": self.download_sketchfab_model,
            # Hunyuan3D
            "create_hunyuan_job": self.create_hunyuan_job,
            "poll_hunyuan_job_status": self.poll_hunyuan_job_status,
            "import_generated_asset_hunyuan": self.import_generated_asset_hunyuan,
        }

    def _execute_command_internal(self, command):
        """Internal command execution with proper context"""
        cmd_type = command.get("type")
        params = command.get("params", {})

        # Pause switch: reject everything except lightweight status commands
        if getattr(bpy.context.scene, "blendermcp_paused", False) and \
                cmd_type not in ("ping", "get_capabilities", "get_telemetry_consent", "set_client_info"):
            return {
                "status": "error",
                "message": "Paused by the user in Blender. Stop and tell the user to press Resume in the BlenderMCP panel."
            }

        handlers = self._get_handlers()

        handler = handlers.get(cmd_type)
        if handler:
            try:
                print(f"Executing handler for {cmd_type}")
                result = handler(**params)
                print(f"Handler execution complete")
                return {"status": "success", "result": result}
            except Exception as e:
                print(f"Error in handler: {str(e)}")
                traceback.print_exc()
                return {"status": "error", "message": str(e)}
        else:
            return {"status": "error", "message": f"Unknown command type: {cmd_type}"}

    def ping(self):
        """Lightweight liveness check (allowed while paused)"""
        return {"pong": True, "addon_version": ADDON_VERSION, "protocol": 1}

    def set_client_info(self, version=None, name=None):
        """Record the connected MCP server's version/name (allowed while paused).

        The panel warns when the server and addon versions diverge.
        """
        self.client_version = version
        self.client_name = name
        # Handshake received: this connection is a modern server, not legacy
        self._saw_client_info = True
        self.legacy_client = False
        return {
            "ok": True,
            "addon_version": ADDON_VERSION,
            "match": version == ADDON_VERSION,
        }

    def _update_legacy_detection(self, commands_on_connection):
        """Flag the connection as a legacy/unknown MCP server.

        Modern servers (>= 1.7.1) send set_client_info right after connecting
        (their second command, after get_capabilities). If at least one command
        has already completed on this connection and no set_client_info was
        received, the server predates the handshake.
        """
        if commands_on_connection >= 2 and not self._saw_client_info:
            self.legacy_client = True

    def _inject_legacy_notice(self, cmd_type, response):
        """Make responses useful for agents behind legacy MCP servers.

        Old servers pass through the results of execute_code, get_scene_info
        and get_object_info, but for execute_code they only forward
        result.get("result", "") in an f-string - a key the new execute_code
        dict doesn't have, so agents see an empty string instead of stdout or
        errors. For legacy clients we therefore:
        - execute_code: ADD a "result" key packing captured stdout (and any
          error summary), with the upgrade notice prepended when due. This
          un-blinds agents on legacy servers.
        - get_scene_info / get_object_info: add a "server_notice" key when due.
        The notice is throttled to once every 10 eligible commands. New-shape
        keys are left untouched, so modern servers are unaffected (they never
        trigger legacy detection and would ignore the extra keys anyway).
        """
        if not self.legacy_client:
            return response
        if cmd_type not in LEGACY_NOTICE_COMMANDS:
            return response
        if not isinstance(response, dict) or not isinstance(response.get("result"), dict):
            return response
        result = response["result"]
        notice_due = self._legacy_notice_counter % 10 == 0
        self._legacy_notice_counter += 1
        if cmd_type == "execute_code":
            pieces = []
            if notice_due:
                pieces.append("[blender-mcp] " + LEGACY_SERVER_NOTICE)
            stdout = result.get("stdout") or ""
            if stdout:
                pieces.append(stdout)
            error = result.get("error")
            if isinstance(error, dict) and error.get("message"):
                pieces.append(
                    f"[error] {error.get('type', 'Error')}: {error.get('message')}")
            if pieces:
                result["result"] = "\n".join(pieces)
        elif notice_due:
            result["server_notice"] = LEGACY_SERVER_NOTICE
        return response

    def get_capabilities(self):
        """Report addon version, Blender version, integration toggles and available commands"""
        scene = bpy.context.scene
        return {
            "addon_version": ADDON_VERSION,
            "protocol": 1,
            "blender_version": list(bpy.app.version),
            "integrations": {
                "polyhaven": bool(getattr(scene, "blendermcp_use_polyhaven", False)),
                "hyper3d": bool(getattr(scene, "blendermcp_use_hyper3d", False)),
                "sketchfab": bool(getattr(scene, "blendermcp_use_sketchfab", False)),
                "hunyuan3d": bool(getattr(scene, "blendermcp_use_hunyuan3d", False)),
            },
            "commands": sorted(self._get_handlers().keys()),
        }

    def undo_last(self):
        """Undo the last operation (MCP mutating commands push undo checkpoints)"""
        try:
            # ed.undo() restores the snapshot of the step BEFORE the active one.
            # The checkpoints pushed in execute_wrapper capture PRE-command state,
            # so first push a step capturing the CURRENT (post-command) state;
            # a single undo then lands exactly on the last pre-command snapshot.
            try:
                bpy.ops.ed.undo_push(message="MCP: undo_last")
            except Exception:
                pass
            bpy.ops.ed.undo()
        except Exception as e:
            raise Exception(f"Undo failed: {str(e)}")
        return {"undone": True}

    def _integration_disabled_error(self, scene_prop, label):
        """Return a disabled-error dict if the integration toggle is off, else None"""
        if not getattr(bpy.context.scene, scene_prop, False):
            return {"error": f"{label} is disabled. Ask the user to enable it in the BlenderMCP sidebar panel in Blender."}
        return None

    def get_scene_info(self):
        """Get information about the current Blender scene"""
        try:
            print("Getting scene info...")
            scene = bpy.context.scene
            try:
                mode = bpy.context.mode
            except Exception:
                mode = "OBJECT"
            try:
                active_object = bpy.context.active_object.name if bpy.context.active_object else None
                selected_objects = [o.name for o in bpy.context.selected_objects][:20]
            except Exception:
                active_object = None
                selected_objects = []
            # Simplify the scene info to reduce data size
            scene_info = {
                "name": scene.name,
                # File identity: which .blend is actually open (None if never saved).
                # Agents must check this before any file operation.
                "filepath": bpy.data.filepath or None,
                "file_saved": bool(bpy.data.filepath),
                "unsaved_changes": bpy.data.is_dirty,
                "object_count": len(scene.objects),
                "objects": [],
                "materials_count": len(bpy.data.materials),
                "frame_start": scene.frame_start,
                "frame_end": scene.frame_end,
                "fps": scene.render.fps,
                "frame_current": scene.frame_current,
                "mode": mode,
                "active_object": active_object,
                "selected_objects": selected_objects,
            }

            # Collect minimal object information (limit to first 20 objects)
            for i, obj in enumerate(scene.objects):
                if i >= 20:
                    break

                obj_info = {
                    "name": obj.name,
                    "type": obj.type,
                    # Only include basic location data
                    "location": [round(float(obj.location.x), 2),
                                round(float(obj.location.y), 2),
                                round(float(obj.location.z), 2)],
                }
                scene_info["objects"].append(obj_info)

            print(f"Scene info collected: {len(scene_info['objects'])} objects")
            return scene_info
        except Exception as e:
            print(f"Error in get_scene_info: {str(e)}")
            traceback.print_exc()
            return {"error": str(e)}

    @staticmethod
    def _get_aabb(obj):
        """ Returns the world-space axis-aligned bounding box (AABB) of an object. """
        if obj.type != 'MESH':
            raise TypeError("Object must be a mesh")

        # Get the bounding box corners in local space
        local_bbox_corners = [mathutils.Vector(corner) for corner in obj.bound_box]

        # Convert to world coordinates
        world_bbox_corners = [obj.matrix_world @ corner for corner in local_bbox_corners]

        # Compute axis-aligned min/max coordinates
        min_corner = mathutils.Vector(map(min, zip(*world_bbox_corners)))
        max_corner = mathutils.Vector(map(max, zip(*world_bbox_corners)))

        return [
            [*min_corner], [*max_corner]
        ]

    @staticmethod
    def _get_world_aabb(objs):
        """World-space AABB spanning one or more objects of any type.

        Unlike _get_aabb this accepts non-mesh objects (their display
        bound_box is used; objects without a usable bound_box contribute
        their origin point). Returns [[min_x,min_y,min_z],[max_x,max_y,max_z]].
        """
        if not isinstance(objs, (list, tuple)):
            objs = [objs]
        corners = []
        for obj in objs:
            try:
                local_corners = [mathutils.Vector(c) for c in obj.bound_box]
                corners.extend(obj.matrix_world @ c for c in local_corners)
            except Exception:
                corners.append(obj.matrix_world.translation.copy())
        if not corners:
            raise ValueError("No objects to compute a bounding box from.")
        min_corner = mathutils.Vector(map(min, zip(*corners)))
        max_corner = mathutils.Vector(map(max, zip(*corners)))
        return [[*min_corner], [*max_corner]]

    @staticmethod
    def _vec_list(vec):
        """Serialize a vector/euler/quaternion to a plain list of floats rounded to 4 decimals"""
        return [round(float(v), 4) for v in vec]

    @staticmethod
    def _cap_string(text, max_len=8000):
        """Cap a long string at max_len chars with a truncation suffix"""
        if text is None:
            return None
        if len(text) > max_len:
            return text[:max_len] + "... [truncated]"
        return text

    @staticmethod
    def _filter_traceback(tb):
        """Strip traceback frames that don't come from executed MCP code ("<mcp>")"""
        try:
            filtered = []
            skipping = False
            for line in tb.splitlines():
                if line.startswith('  File "'):
                    skipping = '"<mcp>"' not in line
                    if not skipping:
                        filtered.append(line)
                elif line.startswith('    ') and skipping:
                    continue
                else:
                    skipping = False
                    filtered.append(line)
            return "\n".join(filtered)
        except Exception:
            return tb

    @staticmethod
    def _snapshot_scene_state():
        """Snapshot object transforms, material and collection names for scene diffing"""
        objects = {}
        for obj in bpy.data.objects:
            try:
                mat = obj.matrix_world
                objects[obj.name] = hash(tuple(round(v, 6) for row in mat for v in row))
            except Exception:
                objects[obj.name] = None
        return {
            "objects": objects,
            "materials": set(m.name for m in bpy.data.materials),
            "collections": set(c.name for c in bpy.data.collections),
        }

    @staticmethod
    def _compute_scene_diff(before):
        """Compute what changed in the scene since a _snapshot_scene_state snapshot"""
        after = BlenderMCPServer._snapshot_scene_state()
        added = sorted(set(after["objects"]) - set(before["objects"]))
        removed = sorted(set(before["objects"]) - set(after["objects"]))
        modified = sorted(
            name for name, h in before["objects"].items()
            if name in after["objects"] and after["objects"][name] != h
        )
        return {
            "objects_added": added[:50],
            "objects_removed": removed[:50],
            "objects_modified": modified[:50],
            "materials_added": sorted(after["materials"] - before["materials"])[:50],
            "collections_added": sorted(after["collections"] - before["collections"])[:50],
        }

    def get_scene_graph(self, filter_type=None, name_contains=None, collection=None,
                        offset=0, limit=50, include=None):
        """Get a filterable, paginated view of the scene: objects, collections and scene state"""
        scene = bpy.context.scene
        include = set(include) if include else set()

        try:
            mode = bpy.context.mode
        except Exception:
            mode = "OBJECT"
        try:
            active_object = bpy.context.active_object.name if bpy.context.active_object else None
            selected_objects = [o.name for o in bpy.context.selected_objects][:20]
        except Exception:
            active_object = None
            selected_objects = []

        scene_block = {
            "name": scene.name,
            # File identity: which .blend is actually open (None if never saved).
            # Agents must check this before any file operation.
            "filepath": bpy.data.filepath or None,
            "file_saved": bool(bpy.data.filepath),
            "unsaved_changes": bpy.data.is_dirty,
            "frame_start": scene.frame_start,
            "frame_end": scene.frame_end,
            "fps": scene.render.fps,
            "frame_current": scene.frame_current,
            "mode": mode,
            "active_object": active_object,
            "selected_objects": selected_objects,
            "engine": scene.render.engine,
        }

        # Flat collection list with parents (top-level collections have parent None)
        parent_map = {}
        for col in bpy.data.collections:
            for child in col.children:
                parent_map[child.name] = col.name
        collections_block = [
            {
                "name": col.name,
                "parent": parent_map.get(col.name),
                "objects_count": len(col.objects),
            }
            for col in bpy.data.collections
        ][:100]
        collections_total = len(bpy.data.collections)

        # Filter objects
        objs = list(scene.objects)
        if filter_type:
            wanted = str(filter_type).upper()
            objs = [o for o in objs if o.type == wanted]
        if name_contains:
            needle = str(name_contains).lower()
            objs = [o for o in objs if needle in o.name.lower()]
        if collection:
            col = bpy.data.collections.get(collection)
            if col is None:
                raise ValueError(f"Collection '{collection}' not found. Use get_scene_graph to list collections.")
            col_names = set(o.name for o in col.all_objects)
            objs = [o for o in objs if o.name in col_names]

        total_count = len(objs)
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 100))
        objs = objs[offset:offset + limit]

        objects_block = []
        for obj in objs:
            try:
                visible = obj.visible_get()
            except Exception:
                visible = True
            entry = {
                "name": obj.name,
                "type": obj.type,
                "parent": obj.parent.name if obj.parent else None,
                "collections": [c.name for c in obj.users_collection],
                "location": self._vec_list(obj.location),
                "rotation_euler": self._vec_list(obj.rotation_euler),
                "scale": self._vec_list(obj.scale),
                "dimensions": self._vec_list(obj.dimensions),
                "visible": visible,
                "material_slots": [s.material.name for s in obj.material_slots if s.material],
                "has_animation": bool(obj.animation_data and obj.animation_data.action),
            }
            if "modifiers" in include:
                entry["modifiers"] = [{"name": m.name, "type": m.type} for m in obj.modifiers]
            if "bounds" in include and obj.type == 'MESH':
                try:
                    bbox = self._get_aabb(obj)
                    entry["world_bounding_box"] = [self._vec_list(bbox[0]), self._vec_list(bbox[1])]
                except Exception:
                    pass
            if "mesh_stats" in include and obj.type == 'MESH' and obj.data:
                entry["mesh_stats"] = {
                    "vertices": len(obj.data.vertices),
                    "polygons": len(obj.data.polygons),
                }
            objects_block.append(entry)

        return {
            "scene": scene_block,
            "collections": collections_block,
            "collections_total": collections_total,
            "total_count": total_count,
            "returned_count": len(objects_block),
            "offset": offset,
            "objects": objects_block,
        }

    def get_object_info(self, name):
        """Get detailed information about a specific object"""
        obj = bpy.data.objects.get(name)
        if not obj:
            raise ValueError(f"Object '{name}' not found. Use get_scene_graph to list objects.")

        # Basic object info
        obj_info = {
            "name": obj.name,
            "type": obj.type,
            "location": [obj.location.x, obj.location.y, obj.location.z],
            "rotation": [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z],
            "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            "visible": obj.visible_get(),
            "materials": [],
            "dimensions": self._vec_list(obj.dimensions),
            "parent": obj.parent.name if obj.parent else None,
            "collections": [c.name for c in obj.users_collection],
            "modifiers": [
                {"name": m.name, "type": m.type, "show_viewport": m.show_viewport}
                for m in obj.modifiers
            ],
            "constraints": [{"name": c.name, "type": c.type} for c in obj.constraints],
            "vertex_groups": [vg.name for vg in obj.vertex_groups],
        }

        if obj.type == "MESH":
            bounding_box = self._get_aabb(obj)
            obj_info["world_bounding_box"] = bounding_box

        # Add material slots
        for slot in obj.material_slots:
            if slot.material:
                obj_info["materials"].append(slot.material.name)

        # Add mesh data if applicable
        if obj.type == 'MESH' and obj.data:
            mesh = obj.data
            obj_info["mesh"] = {
                "vertices": len(mesh.vertices),
                "edges": len(mesh.edges),
                "polygons": len(mesh.polygons),
            }

        # UV layers (mesh data only)
        uv_layers = getattr(obj.data, "uv_layers", None) if obj.data else None
        obj_info["uv_layers"] = [uv.name for uv in uv_layers] if uv_layers else []

        # Shape keys (mesh/curve/lattice data)
        shape_keys = getattr(obj.data, "shape_keys", None) if obj.data else None
        if shape_keys:
            obj_info["shape_keys"] = [
                {"name": kb.name, "value": round(float(kb.value), 4)}
                for kb in shape_keys.key_blocks
            ]
        else:
            obj_info["shape_keys"] = None

        # Animation data
        anim = obj.animation_data
        if anim:
            fcurves = []
            action = anim.action
            if action:
                for fc in self._action_fcurves(action)[:50]:
                    try:
                        frame_range = self._vec_list(fc.range())
                    except Exception:
                        frame_range = None
                    fcurves.append({
                        "data_path": fc.data_path,
                        "array_index": fc.array_index,
                        "keyframe_count": len(fc.keyframe_points),
                        "frame_range": frame_range,
                    })
            obj_info["animation"] = {
                "action": action.name if action else None,
                "fcurves": fcurves,
                "nla_tracks": [t.name for t in anim.nla_tracks],
            }
        else:
            obj_info["animation"] = None

        # Armature bones
        if obj.type == 'ARMATURE' and obj.data:
            obj_info["bones"] = [
                {
                    "name": bone.name,
                    "parent": bone.parent.name if bone.parent else None,
                    "head": self._vec_list(bone.head_local),
                    "tail": self._vec_list(bone.tail_local),
                }
                for bone in obj.data.bones[:100]
            ]

        return obj_info

    def get_viewport_screenshot(self, max_size=800, filepath=None, format="png"):
        """
        Capture a screenshot of the current 3D viewport and save it to the specified path.

        Parameters:
        - max_size: Maximum size in pixels for the largest dimension of the image
        - filepath: Optional path where to save the screenshot file (a temp file is used if omitted)
        - format: Image format (png, jpg, etc.)

        Returns image_data (base64), width, height and format (plus filepath when one was given)
        """
        try:
            fmt = str(format).lower().lstrip(".")
            if fmt == "jpg":
                fmt = "jpeg"
            auto_generated = not filepath
            if auto_generated:
                ext = "jpg" if fmt == "jpeg" else fmt
                filepath = os.path.join(
                    tempfile.gettempdir(),
                    f"blendermcp_screenshot_{os.getpid()}_{int(time.time() * 1000)}.{ext}"
                )

            # Find the active 3D viewport
            area = None
            for a in bpy.context.screen.areas:
                if a.type == 'VIEW_3D':
                    area = a
                    break

            if not area:
                return {"error": "No 3D viewport found"}

            # Take screenshot with proper context override
            with bpy.context.temp_override(area=area):
                bpy.ops.screen.screenshot_area(filepath=filepath)

            # Load and resize if needed
            img = bpy.data.images.load(filepath)
            width, height = img.size

            if max(width, height) > max_size:
                scale = max_size / max(width, height)
                new_width = int(width * scale)
                new_height = int(height * scale)
                img.scale(new_width, new_height)

                # Set format and save
                img.file_format = "JPEG" if fmt == "jpeg" else fmt.upper()
                img.save()
                width, height = new_width, new_height

            # Cleanup Blender image data
            bpy.data.images.remove(img)

            # Always return the image bytes as base64 so remote MCP servers
            # (which may not share a filesystem with Blender) still get the image
            with open(filepath, "rb") as f:
                image_data = base64.b64encode(f.read()).decode("utf-8")

            if auto_generated:
                try:
                    os.remove(filepath)
                except Exception:
                    pass
                filepath = None

            return {
                "success": True,
                "width": width,
                "height": height,
                "filepath": filepath,
                "image_data": image_data,
                "format": fmt,
            }

        except Exception as e:
            return {"error": str(e)}

    def execute_code(self, code, rollback_on_error=False, reset_namespace=False):
        """Execute arbitrary Blender Python code with REPL semantics.

        Runs in a persistent namespace (pre-loaded with bpy, bmesh, mathutils,
        math, json, random, Vector, Matrix, Euler, Quaternion). If the last
        top-level statement is an expression, its repr() is returned as
        result_repr. On error this returns (not raises) the result dict so the
        caller gets the traceback and scene diff; rollback_on_error undoes the
        checkpoint pushed before the command ran.
        """
        # This is powerful but potentially dangerous - use with caution
        global _EXEC_NAMESPACE
        if reset_namespace or _EXEC_NAMESPACE is None:
            _EXEC_NAMESPACE = _init_exec_namespace()
        namespace = _EXEC_NAMESPACE

        snapshot = self._snapshot_scene_state()
        result = {
            "executed": False,
            "stdout": "",
            "result_repr": None,
            "error": None,
            "rolled_back": False,
            "scene_diff": {},
        }

        capture_buffer = io.StringIO()
        try:
            tree = ast.parse(code, filename="<mcp>")
            last_expr = None
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                last_expr = ast.Expression(tree.body[-1].value)
                tree.body = tree.body[:-1]
            exec_code = compile(tree, "<mcp>", "exec")

            # Capture stdout during execution
            with redirect_stdout(capture_buffer):
                exec(exec_code, namespace)
                if last_expr is not None:
                    eval_code = compile(last_expr, "<mcp>", "eval")
                    value = eval(eval_code, namespace)
                    result["result_repr"] = self._cap_string(repr(value))
            result["executed"] = True
        except Exception as e:
            tb = self._filter_traceback(traceback.format_exc())
            result["error"] = {
                "type": type(e).__name__,
                "message": str(e),
                "traceback": self._cap_string(tb),
            }
            if rollback_on_error:
                # The undo checkpoint pushed before this command (see
                # MUTATING_COMMANDS in execute_wrapper) captured the pre-command
                # state. ed.undo() restores the step BEFORE the active one, so
                # push a step for the current (failed) state first - the single
                # undo then lands exactly on the pre-command snapshot instead of
                # also reverting the previous command.
                try:
                    try:
                        bpy.ops.ed.undo_push(message="MCP: failed state")
                    except Exception:
                        pass
                    bpy.ops.ed.undo()
                    result["rolled_back"] = True
                except Exception as undo_err:
                    print(f"Rollback failed: {str(undo_err)}")

        result["stdout"] = self._cap_string(capture_buffer.getvalue())
        result["scene_diff"] = self._compute_scene_diff(snapshot)
        return result

    # ------------------------------------------------------------------
    # Modelling helpers & handlers (C1)
    # ------------------------------------------------------------------

    def _transform_status(self, obj):
        """Standard transform report returned by set_transform/place_object"""
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass
        status = {
            "name": obj.name,
            "location": self._vec_list(obj.location),
            "rotation_euler": self._vec_list(obj.rotation_euler),
            "scale": self._vec_list(obj.scale),
            "dimensions": self._vec_list(obj.dimensions),
        }
        if obj.type == 'MESH':
            try:
                bbox = self._get_aabb(obj)
                status["world_bounding_box"] = [self._vec_list(bbox[0]), self._vec_list(bbox[1])]
            except Exception:
                pass
        return status

    @staticmethod
    def _ensure_object_mode():
        """Switch to OBJECT mode if needed (some operators require it)"""
        try:
            if bpy.context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            pass

    @staticmethod
    def _get_object_or_raise(name):
        obj = bpy.data.objects.get(name)
        if not obj:
            raise ValueError(f"Object '{name}' not found. Use get_scene_graph to list objects.")
        return obj

    @staticmethod
    def _check_vec3(value, label):
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise ValueError(f"{label} must be a list of 3 floats.")
        return [float(v) for v in value]

    def set_transform(self, name, location=None, rotation_euler=None, scale=None, relative=False):
        """Set or offset an object's location/rotation (radians)/scale"""
        obj = self._get_object_or_raise(name)

        if location is not None:
            loc = self._check_vec3(location, "location")
            if relative:
                obj.location = [obj.location[i] + loc[i] for i in range(3)]
            else:
                obj.location = loc
        if rotation_euler is not None:
            rot = self._check_vec3(rotation_euler, "rotation_euler")
            if relative:
                obj.rotation_euler = [obj.rotation_euler[i] + rot[i] for i in range(3)]
            else:
                obj.rotation_euler = rot
        if scale is not None:
            scl = self._check_vec3(scale, "scale")
            if relative:
                obj.scale = [obj.scale[i] * scl[i] for i in range(3)]
            else:
                obj.scale = scl

        return self._transform_status(obj)

    @staticmethod
    def _translate_world(obj, delta):
        """Translate an object by a world-space delta.

        obj.location is expressed in parent space, so adding a world-space
        delta to it is wrong for children of rotated/scaled parents; move the
        world matrix translation instead (Blender back-computes location).
        """
        mw = obj.matrix_world.copy()
        mw.translation = mw.translation + mathutils.Vector(delta)
        obj.matrix_world = mw
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass

    def place_object(self, name, mode="ground", target=None, offset=None, margin=0.0):
        """Place an object using AABB math: on the ground, on another object, or by offset"""
        obj = self._get_object_or_raise(name)
        mode = str(mode).lower()
        if mode not in ("ground", "on_object", "offset"):
            raise ValueError(f"Unknown mode '{mode}'. Use 'ground', 'on_object' or 'offset'.")
        if offset is not None:
            offset = self._check_vec3(offset, "offset")
        margin = float(margin)

        try:
            bpy.context.view_layer.update()
        except Exception:
            pass

        if mode == "offset":
            if offset is None:
                raise ValueError("mode 'offset' requires an offset [x, y, z].")
            self._translate_world(obj, offset)
            return self._transform_status(obj)

        aabb_min, aabb_max = self._get_world_aabb(obj)

        if mode == "ground":
            delta = [0.0, 0.0, margin - aabb_min[2]]
        else:  # on_object
            if not target:
                raise ValueError("mode 'on_object' requires a target object name.")
            tobj = self._get_object_or_raise(target)
            if tobj is obj:
                raise ValueError("target must be a different object than name.")
            t_min, t_max = self._get_world_aabb(tobj)
            delta = [
                (t_min[0] + t_max[0]) / 2.0 - (aabb_min[0] + aabb_max[0]) / 2.0,
                (t_min[1] + t_max[1]) / 2.0 - (aabb_min[1] + aabb_max[1]) / 2.0,
                (t_max[2] + margin) - aabb_min[2],
            ]
        if offset is not None:
            delta = [delta[i] + offset[i] for i in range(3)]
        self._translate_world(obj, delta)
        return self._transform_status(obj)

    @staticmethod
    def _modifier_params_summary(mod, max_props=8):
        """Up to max_props interesting non-default rna props of a modifier, as primitives"""
        skip = {
            "name", "type", "show_viewport", "show_render", "show_in_editmode",
            "show_on_cage", "show_expanded", "is_active", "use_pin_to_last",
            "is_override_data", "use_apply_on_spline", "execution_time",
            "persistent_uid", "rna_type",
        }
        out = {}
        try:
            for prop in mod.bl_rna.properties:
                if len(out) >= max_props:
                    break
                pid = prop.identifier
                if pid in skip or prop.is_readonly:
                    continue
                try:
                    if prop.type == 'POINTER':
                        val = getattr(mod, pid, None)
                        if isinstance(val, bpy.types.ID):
                            out[pid] = val.name
                        continue
                    if prop.type not in ('BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'):
                        continue
                    if prop.type == 'ENUM' and getattr(prop, "is_enum_flag", False):
                        continue
                    if getattr(prop, "is_array", False) and getattr(prop, "array_length", 0) > 0:
                        val = [round(float(v), 4) for v in getattr(mod, pid)]
                        try:
                            default = [round(float(v), 4) for v in prop.default_array]
                            if val == default:
                                continue
                        except Exception:
                            pass
                        out[pid] = val
                        continue
                    val = getattr(mod, pid)
                    default = getattr(prop, "default", None)
                    if prop.type == 'FLOAT':
                        if default is not None and abs(float(val) - float(default)) < 1e-9:
                            continue
                        out[pid] = round(float(val), 4)
                    else:
                        if default is not None and val == default:
                            continue
                        out[pid] = val
                except Exception:
                    continue
        except Exception:
            pass
        return out

    def _modifier_list(self, obj):
        """Serialize an object's modifier stack"""
        return [
            {
                "name": m.name,
                "type": m.type,
                "show_viewport": m.show_viewport,
                "params": self._modifier_params_summary(m),
            }
            for m in obj.modifiers
        ][:100]

    @staticmethod
    def _set_modifier_params(mod, params):
        """setattr params onto a modifier; returns {key: reason} for ignored/failed keys"""
        ignored = {}
        if not params:
            return ignored
        for key, value in params.items():
            if not hasattr(mod, key):
                ignored[key] = "unknown parameter"
                continue
            try:
                prop = mod.bl_rna.properties.get(key)
                if prop is not None and prop.type == 'POINTER' and isinstance(value, str):
                    # Resolve datablock references by name (e.g. Boolean/Array targets)
                    resolved = bpy.data.objects.get(value)
                    if resolved is None:
                        resolved = bpy.data.collections.get(value)
                    if resolved is None:
                        ignored[key] = f"could not resolve '{value}' to an object or collection"
                        continue
                    setattr(mod, key, resolved)
                else:
                    setattr(mod, key, value)
            except Exception as e:
                ignored[key] = str(e)
        return ignored

    def _find_modifier(self, obj, modifier_name=None, modifier_type=None):
        """Resolve a modifier by name, else by type, else the only one present"""
        if not obj.modifiers:
            raise ValueError(f"Object '{obj.name}' has no modifiers.")
        if modifier_name:
            mod = obj.modifiers.get(modifier_name)
            if not mod:
                raise ValueError(
                    f"Modifier '{modifier_name}' not found on '{obj.name}'. "
                    f"Modifiers: {[m.name for m in obj.modifiers]}"
                )
            return mod
        if modifier_type:
            wanted = str(modifier_type).upper()
            for m in obj.modifiers:
                if m.type == wanted:
                    return m
            raise ValueError(f"No modifier of type '{wanted}' on '{obj.name}'.")
        if len(obj.modifiers) == 1:
            return obj.modifiers[0]
        raise ValueError(
            f"Specify modifier_name (or modifier_type). "
            f"Modifiers on '{obj.name}': {[m.name for m in obj.modifiers]}"
        )

    def manage_modifiers(self, name, action, modifier_type=None, modifier_name=None, params=None, index=None):
        """List/add/configure/apply/remove/reorder modifiers on an object"""
        obj = self._get_object_or_raise(name)
        action = str(action).lower()
        valid_actions = ("list", "add", "set_params", "apply", "remove", "move")
        if action not in valid_actions:
            raise ValueError(f"Unknown action '{action}'. Use one of: {', '.join(valid_actions)}.")

        result = {"object": obj.name, "action": action}

        if action == "list":
            result["modifiers"] = self._modifier_list(obj)
            return result

        if action == "add":
            if not modifier_type:
                raise ValueError("action 'add' requires modifier_type (e.g. 'SUBSURF', 'BEVEL', 'ARRAY').")
            mod_type = str(modifier_type).upper()
            try:
                mod = obj.modifiers.new(name=modifier_name or mod_type.title(), type=mod_type)
            except Exception as e:
                raise ValueError(f"Could not add modifier of type '{mod_type}': {str(e)}")
            if mod is None:
                raise ValueError(
                    f"Modifier type '{mod_type}' is not valid for object '{obj.name}' (type {obj.type})."
                )
            ignored = self._set_modifier_params(mod, params)
            result["modifier"] = mod.name
            if ignored:
                result["ignored_params"] = ignored
            result["modifiers"] = self._modifier_list(obj)
            return result

        mod = self._find_modifier(obj, modifier_name, modifier_type)

        if action == "set_params":
            if not params:
                raise ValueError("action 'set_params' requires a params dict.")
            ignored = self._set_modifier_params(mod, params)
            result["modifier"] = mod.name
            if ignored:
                result["ignored_params"] = ignored
        elif action == "apply":
            mod_name = mod.name
            self._ensure_object_mode()
            try:
                with bpy.context.temp_override(object=obj, active_object=obj, selected_objects=[obj]):
                    bpy.ops.object.modifier_apply(modifier=mod_name)
            except Exception as e:
                raise Exception(f"Failed to apply modifier '{mod_name}' on '{obj.name}': {str(e)}")
            result["applied"] = mod_name
        elif action == "remove":
            mod_name = mod.name
            obj.modifiers.remove(mod)
            result["removed"] = mod_name
        elif action == "move":
            if index is None:
                raise ValueError("action 'move' requires an index (0 = top of the stack).")
            idx = max(0, min(int(index), len(obj.modifiers) - 1))
            try:
                with bpy.context.temp_override(object=obj, active_object=obj, selected_objects=[obj]):
                    bpy.ops.object.modifier_move_to_index(modifier=mod.name, index=idx)
            except Exception as e:
                raise Exception(f"Failed to move modifier '{mod.name}': {str(e)}")
            result["modifier"] = mod.name
            result["index"] = idx

        result["modifiers"] = self._modifier_list(obj)
        return result

    def boolean_op(self, object_a, object_b, operation="DIFFERENCE", apply=True, delete_operand=True, solver="EXACT"):
        """Boolean modifier on object_a targeting object_b, optionally applied + operand deleted"""
        obj_a = self._get_object_or_raise(object_a)
        obj_b = self._get_object_or_raise(object_b)
        if obj_a is obj_b:
            raise ValueError("object_a and object_b must be different objects.")
        if obj_a.type != 'MESH' or obj_b.type != 'MESH':
            raise ValueError("Boolean operations require two MESH objects.")
        operation = str(operation).upper()
        if operation not in ("DIFFERENCE", "UNION", "INTERSECT"):
            raise ValueError(f"Invalid operation '{operation}'. Use DIFFERENCE, UNION or INTERSECT.")

        mesh_stats_before = {
            "vertices": len(obj_a.data.vertices),
            "polygons": len(obj_a.data.polygons),
        }

        mod = obj_a.modifiers.new(name="MCP_Boolean", type='BOOLEAN')
        if mod is None:
            raise Exception(f"Could not add a Boolean modifier to '{obj_a.name}'.")
        mod.object = obj_b
        mod.operation = operation
        if solver and hasattr(mod, "solver"):
            try:
                mod.solver = str(solver).upper()
            except Exception:
                obj_a.modifiers.remove(mod)
                raise ValueError(f"Invalid solver '{solver}'. Use 'EXACT' or 'FAST'.")

        applied = False
        operand_deleted = False
        if apply:
            mod_name = mod.name
            self._ensure_object_mode()
            try:
                with bpy.context.temp_override(object=obj_a, active_object=obj_a, selected_objects=[obj_a]):
                    bpy.ops.object.modifier_apply(modifier=mod_name)
                applied = True
            except Exception as e:
                with suppress(Exception):
                    obj_a.modifiers.remove(mod)
                raise Exception(f"Failed to apply Boolean modifier: {str(e)}")
            if delete_operand:
                try:
                    bpy.data.objects.remove(obj_b, do_unlink=True)
                    operand_deleted = True
                except Exception as e:
                    print(f"Could not delete operand: {str(e)}")

        try:
            bpy.context.view_layer.update()
        except Exception:
            pass

        if applied:
            mesh_stats_after = {
                "vertices": len(obj_a.data.vertices),
                "polygons": len(obj_a.data.polygons),
            }
        else:
            # Modifier left live: report the evaluated (with-modifier) mesh stats
            try:
                deps = bpy.context.evaluated_depsgraph_get()
                eval_obj = obj_a.evaluated_get(deps)
                mesh_stats_after = {
                    "vertices": len(eval_obj.data.vertices),
                    "polygons": len(eval_obj.data.polygons),
                }
            except Exception:
                mesh_stats_after = mesh_stats_before

        bbox = self._get_aabb(obj_a)
        result = {
            "object": obj_a.name,
            "operation": operation,
            "applied": applied,
            "mesh_stats_before": mesh_stats_before,
            "mesh_stats_after": mesh_stats_after,
            "world_bounding_box": [self._vec_list(bbox[0]), self._vec_list(bbox[1])],
        }
        if not applied:
            result["modifier"] = mod.name
            if delete_operand:
                result["note"] = "operand kept: it is still referenced by the live Boolean modifier"
        elif delete_operand:
            result["operand_deleted"] = operand_deleted
        return result

    def organize_scene(self, action, name=None, parent=None, objects=None, collection=None,
                       child=None, keep_transform=True, old=None, new=None):
        """Scene organization: collections, parenting, renaming and deletion"""
        action = str(action).lower()
        scene = bpy.context.scene

        if action == "create_collection":
            if not name:
                raise ValueError("create_collection requires a name.")
            parent_col = None
            if parent:
                parent_col = bpy.data.collections.get(parent)
                if parent_col is None:
                    raise ValueError(
                        f"Parent collection '{parent}' not found. Use get_scene_graph to list collections."
                    )
            new_col = bpy.data.collections.new(name)
            (parent_col or scene.collection).children.link(new_col)
            return {
                "action": action,
                "collection": new_col.name,
                "parent": parent_col.name if parent_col else None,
                "ok": True,
            }

        if action == "move_to_collection":
            if not objects or not collection:
                raise ValueError("move_to_collection requires objects (list of names) and collection.")
            if isinstance(objects, str):
                objects = [objects]
            col = bpy.data.collections.get(collection)
            if col is None:
                if collection == scene.collection.name:
                    col = scene.collection
                else:
                    raise ValueError(
                        f"Collection '{collection}' not found. Use organize_scene create_collection first."
                    )
            moved, not_found = [], []
            for obj_name in objects:
                obj = bpy.data.objects.get(obj_name)
                if obj is None:
                    not_found.append(obj_name)
                    continue
                for c in list(obj.users_collection):
                    with suppress(Exception):
                        c.objects.unlink(obj)
                with suppress(Exception):
                    col.objects.link(obj)
                moved.append(obj.name)
            return {"action": action, "collection": col.name, "moved": moved,
                    "not_found": not_found, "ok": True}

        if action == "set_parent":
            if not child or not parent:
                raise ValueError("set_parent requires child and parent object names.")
            child_obj = self._get_object_or_raise(child)
            parent_obj = self._get_object_or_raise(parent)
            if child_obj is parent_obj:
                raise ValueError("child and parent must be different objects.")
            try:
                bpy.context.view_layer.update()
            except Exception:
                pass
            child_obj.parent = parent_obj
            if keep_transform:
                child_obj.matrix_parent_inverse = parent_obj.matrix_world.inverted()
            else:
                child_obj.matrix_parent_inverse.identity()
            return {"action": action, "child": child_obj.name, "parent": parent_obj.name,
                    "keep_transform": bool(keep_transform), "ok": True}

        if action == "clear_parent":
            if not child:
                raise ValueError("clear_parent requires a child object name.")
            child_obj = self._get_object_or_raise(child)
            if child_obj.parent is not None:
                try:
                    bpy.context.view_layer.update()
                except Exception:
                    pass
                if keep_transform:
                    world_matrix = child_obj.matrix_world.copy()
                    child_obj.parent = None
                    child_obj.matrix_world = world_matrix
                else:
                    child_obj.parent = None
            return {"action": action, "child": child_obj.name,
                    "keep_transform": bool(keep_transform), "ok": True}

        if action == "rename":
            if not old or not new:
                raise ValueError("rename requires old and new names.")
            obj = bpy.data.objects.get(old)
            if not obj:
                raise ValueError(f"Object '{old}' not found. Use get_scene_graph to list objects.")
            obj.name = new
            return {"action": action, "old": old, "name": obj.name, "ok": True}

        if action == "delete":
            if not objects:
                raise ValueError("delete requires objects (list of names).")
            if isinstance(objects, str):
                objects = [objects]
            deleted, not_found = [], []
            for obj_name in objects:
                obj = bpy.data.objects.get(obj_name)
                if obj is None:
                    not_found.append(obj_name)
                else:
                    bpy.data.objects.remove(obj, do_unlink=True)
                    deleted.append(obj_name)
            return {"action": action, "deleted": deleted, "not_found": not_found, "ok": True}

        raise ValueError(
            f"Unknown action '{action}'. Use create_collection, move_to_collection, "
            f"set_parent, clear_parent, rename or delete."
        )

    # ------------------------------------------------------------------
    # Animation helpers & handlers (C2)
    # ------------------------------------------------------------------

    @staticmethod
    def _timeline_status():
        """Current timeline state (shared by manage_timeline and get_animation_info)"""
        scene = bpy.context.scene
        try:
            fps = scene.render.fps / scene.render.fps_base
        except Exception:
            fps = float(scene.render.fps)
        frame_count = scene.frame_end - scene.frame_start + 1
        return {
            "frame_start": scene.frame_start,
            "frame_end": scene.frame_end,
            "fps": round(fps, 4),
            "frame_current": scene.frame_current,
            "duration_seconds": round(frame_count / fps, 4) if fps else None,
        }

    @staticmethod
    def _action_fcurves(action):
        """Fcurves of an action; handles slotted/layered actions (Blender 4.4+/5.x)"""
        if action is None:
            return []
        fcurves = []
        try:
            fcurves = list(action.fcurves)
            if fcurves:
                return fcurves
        except Exception:
            fcurves = []
        try:
            for layer in action.layers:
                for strip in layer.strips:
                    for bag in strip.channelbags:
                        fcurves.extend(bag.fcurves)
        except Exception:
            pass
        return fcurves

    @staticmethod
    def _remove_fcurve(action, fc):
        """Remove an fcurve from an action, handling slotted actions"""
        try:
            action.fcurves.remove(fc)
            return True
        except Exception:
            pass
        try:
            for layer in action.layers:
                for strip in layer.strips:
                    for bag in strip.channelbags:
                        try:
                            bag.fcurves.remove(fc)
                            return True
                        except Exception:
                            continue
        except Exception:
            pass
        return False

    def _fcurve_summaries(self, obj, data_path=None):
        """Summaries (data_path/index/count/range) of an object's action fcurves"""
        summaries = []
        anim = obj.animation_data
        action = anim.action if anim else None
        for fc in self._action_fcurves(action):
            if data_path and fc.data_path != data_path:
                continue
            try:
                frame_range = self._vec_list(fc.range())
            except Exception:
                frame_range = None
            summaries.append({
                "data_path": fc.data_path,
                "array_index": fc.array_index,
                "keyframe_count": len(fc.keyframe_points),
                "frame_range": frame_range,
            })
        return summaries[:100]

    def _count_keyframe_points(self, obj, data_path=None):
        """Total keyframe points across an object's action fcurves (optionally filtered)"""
        anim = obj.animation_data
        action = anim.action if anim else None
        total = 0
        for fc in self._action_fcurves(action):
            if data_path and fc.data_path != data_path:
                continue
            total += len(fc.keyframe_points)
        return total

    @staticmethod
    def _resolve_keyframe_target(obj, data_path):
        """Resolve the property owner + attribute for a data path (supports pose bones etc.)"""
        if "." in data_path:
            owner_path, _, attr = data_path.rpartition(".")
            try:
                owner = obj.path_resolve(owner_path)
            except Exception:
                raise ValueError(
                    f"Cannot resolve data path '{data_path}' on object '{obj.name}'. "
                    f"Example paths: 'location', 'pose.bones[\"Bone\"].rotation_euler'."
                )
        else:
            owner, attr = obj, data_path
        if not hasattr(owner, attr):
            raise ValueError(f"Property '{attr}' not found for data path '{data_path}' on '{obj.name}'.")
        return owner, attr

    def manage_timeline(self, action="get", frame_start=None, frame_end=None, fps=None, frame_current=None):
        """Get or set the scene timeline (frame range, fps, current frame)"""
        action = str(action).lower()
        if action not in ("get", "set"):
            raise ValueError(f"Unknown action '{action}'. Use 'get' or 'set'.")
        scene = bpy.context.scene
        if frame_start is not None:
            scene.frame_start = int(frame_start)
        if frame_end is not None:
            scene.frame_end = int(frame_end)
        if fps is not None:
            scene.render.fps = int(round(float(fps)))
            scene.render.fps_base = 1.0
        if frame_current is not None:
            scene.frame_set(int(frame_current))
        return self._timeline_status()

    def set_keyframes(self, name, data_path, keys, index=-1, interpolation=None):
        """Insert keyframes on a property. keys = [{"frame": int, "value": float|[floats]}]"""
        obj = self._get_object_or_raise(name)
        if not keys or not isinstance(keys, (list, tuple)):
            raise ValueError("keys must be a non-empty list of {'frame': int, 'value': float|[floats]} dicts.")
        index = -1 if index is None else int(index)
        if interpolation is not None:
            interpolation = str(interpolation).upper()
            if interpolation not in KEYFRAME_INTERPOLATIONS:
                raise ValueError(
                    f"Invalid interpolation '{interpolation}'. "
                    f"Valid: {', '.join(sorted(KEYFRAME_INTERPOLATIONS))}."
                )

        owner, attr = self._resolve_keyframe_target(obj, data_path)
        points_before = self._count_keyframe_points(obj, data_path)
        frames_written = []

        for key in keys:
            if not isinstance(key, dict) or "frame" not in key or "value" not in key:
                raise ValueError("Each key must be a dict with 'frame' and 'value'.")
            frame = int(round(float(key["frame"])))
            value = key["value"]
            current = getattr(owner, attr)
            is_vector = hasattr(current, "__len__") and not isinstance(current, str)
            insert_index = -1
            if isinstance(value, (list, tuple)):
                if not is_vector:
                    raise ValueError(f"'{data_path}' is not a vector property; pass a single float value.")
                try:
                    setattr(owner, attr, [float(v) for v in value])
                except Exception as e:
                    raise ValueError(f"Cannot set '{data_path}' to {value}: {str(e)}")
            elif is_vector:
                if index < 0:
                    raise ValueError(
                        f"'{data_path}' is a vector property: pass a list value, "
                        f"or a float with index (0-{len(current) - 1}) to key one channel."
                    )
                if index >= len(current):
                    raise ValueError(f"index {index} out of range for '{data_path}' (length {len(current)}).")
                current[index] = float(value)
                insert_index = index
            else:
                try:
                    setattr(owner, attr, value)
                except Exception as e:
                    raise ValueError(f"Cannot set '{data_path}' to {value!r}: {str(e)}")
            try:
                obj.keyframe_insert(data_path=data_path, frame=frame, index=insert_index)
            except Exception as e:
                raise Exception(f"keyframe_insert failed for '{data_path}' at frame {frame}: {str(e)}")
            frames_written.append(frame)

        anim = obj.animation_data
        action = anim.action if anim else None
        if interpolation and action:
            frame_set = set(frames_written)
            for fc in self._action_fcurves(action):
                if fc.data_path != data_path:
                    continue
                changed = False
                for kp in fc.keyframe_points:
                    if int(round(kp.co[0])) in frame_set:
                        kp.interpolation = interpolation
                        changed = True
                if changed:
                    with suppress(Exception):
                        fc.update()

        return {
            "object": obj.name,
            "fcurves": self._fcurve_summaries(obj, data_path),
            "keys_created": max(0, self._count_keyframe_points(obj, data_path) - points_before),
        }

    def delete_keyframes(self, name, data_path=None, frames=None):
        """Delete keyframe points (all fcurves if data_path None; whole fcurves if frames None)"""
        obj = self._get_object_or_raise(name)
        anim = obj.animation_data
        action = anim.action if anim else None
        if not action:
            return {"object": obj.name, "keyframes_removed": 0,
                    "fcurves_removed": 0, "action_removed": False}

        frame_set = None
        if frames is not None:
            if isinstance(frames, (int, float)):
                frames = [frames]
            frame_set = set(int(round(float(f))) for f in frames)

        keyframes_removed = 0
        fcurves_removed = 0
        for fc in list(self._action_fcurves(action)):
            if data_path and fc.data_path != data_path:
                continue
            if frame_set is None:
                keyframes_removed += len(fc.keyframe_points)
                if self._remove_fcurve(action, fc):
                    fcurves_removed += 1
                continue
            removed_any = True
            while removed_any:
                removed_any = False
                for kp in fc.keyframe_points:
                    if int(round(kp.co[0])) in frame_set:
                        fc.keyframe_points.remove(kp)
                        keyframes_removed += 1
                        removed_any = True
                        break
            if len(fc.keyframe_points) == 0:
                if self._remove_fcurve(action, fc):
                    fcurves_removed += 1
            else:
                with suppress(Exception):
                    fc.update()

        action_removed = False
        if not self._action_fcurves(action):
            with suppress(Exception):
                anim.action = None
                action_removed = True

        return {
            "object": obj.name,
            "keyframes_removed": keyframes_removed,
            "fcurves_removed": fcurves_removed,
            "action_removed": action_removed,
        }

    def set_keyframe_interpolation(self, name, data_path=None, frames=None,
                                   interpolation="BEZIER", easing="AUTO", make_cyclic=False):
        """Set interpolation/easing on keyframe points; optionally add a CYCLES modifier"""
        obj = self._get_object_or_raise(name)
        interpolation = str(interpolation).upper()
        if interpolation not in KEYFRAME_INTERPOLATIONS:
            raise ValueError(
                f"Invalid interpolation '{interpolation}'. "
                f"Valid: {', '.join(sorted(KEYFRAME_INTERPOLATIONS))}."
            )
        easing = str(easing).upper()
        if easing not in KEYFRAME_EASINGS:
            raise ValueError(f"Invalid easing '{easing}'. Valid: {', '.join(sorted(KEYFRAME_EASINGS))}.")

        anim = obj.animation_data
        action = anim.action if anim else None
        if not action:
            raise ValueError(f"Object '{name}' has no animation action. Use set_keyframes first.")

        frame_set = None
        if frames is not None:
            if isinstance(frames, (int, float)):
                frames = [frames]
            frame_set = set(int(round(float(f))) for f in frames)

        points_modified = 0
        fcurves_modified = 0
        cyclic_added = 0
        for fc in self._action_fcurves(action):
            if data_path and fc.data_path != data_path:
                continue
            touched = 0
            for kp in fc.keyframe_points:
                if frame_set is None or int(round(kp.co[0])) in frame_set:
                    kp.interpolation = interpolation
                    with suppress(Exception):
                        kp.easing = easing
                    touched += 1
            if make_cyclic and not any(m.type == 'CYCLES' for m in fc.modifiers):
                try:
                    fc.modifiers.new('CYCLES')
                    cyclic_added += 1
                except Exception as e:
                    print(f"Could not add CYCLES modifier: {str(e)}")
            if touched or make_cyclic:
                with suppress(Exception):
                    fc.update()
            if touched:
                points_modified += touched
                fcurves_modified += 1

        return {
            "object": obj.name,
            "interpolation": interpolation,
            "easing": easing,
            "points_modified": points_modified,
            "fcurves_modified": fcurves_modified,
            "cyclic_modifiers_added": cyclic_added,
        }

    def get_animation_info(self, name=None):
        """Animation overview of the scene, or full animation detail for one object"""
        if name is None:
            scene = bpy.context.scene
            animated = []
            total = 0
            for obj in scene.objects:
                anim = obj.animation_data
                action = anim.action if anim else None
                if not action:
                    continue
                total += 1
                if len(animated) < 100:
                    try:
                        frame_range = self._vec_list(action.frame_range)
                    except Exception:
                        frame_range = None
                    animated.append({
                        "name": obj.name,
                        "action": action.name,
                        "fcurve_count": len(self._action_fcurves(action)),
                        "frame_range": frame_range,
                    })
            return {
                "timeline": self._timeline_status(),
                "animated_objects": animated,
                "total_count": total,
                "actions": sorted(a.name for a in bpy.data.actions)[:100],
            }

        obj = self._get_object_or_raise(name)
        anim = obj.animation_data
        action = anim.action if anim else None

        all_fcurves = self._action_fcurves(action)
        fcurves_block = []
        for fc in all_fcurves[:100]:
            keyframes = [
                {
                    "frame": round(float(kp.co[0]), 2),
                    "value": round(float(kp.co[1]), 4),
                    "interpolation": kp.interpolation,
                }
                for kp in list(fc.keyframe_points)[:50]
            ]
            fcurves_block.append({
                "data_path": fc.data_path,
                "array_index": fc.array_index,
                "keyframe_count": len(fc.keyframe_points),
                "keyframes": keyframes,
            })

        nla_block = []
        if anim:
            for track in anim.nla_tracks:
                nla_block.append({
                    "name": track.name,
                    "strips": [
                        {
                            "name": s.name,
                            "action": s.action.name if s.action else None,
                            "frame_start": round(float(s.frame_start), 2),
                            "frame_end": round(float(s.frame_end), 2),
                        }
                        for s in track.strips
                    ][:100],
                })

        shape_keys = getattr(obj.data, "shape_keys", None) if obj.data else None
        shape_block = [
            {"name": kb.name, "value": round(float(kb.value), 4)}
            for kb in shape_keys.key_blocks
        ][:100] if shape_keys else []

        return {
            "object": obj.name,
            "action": action.name if action else None,
            "fcurves": fcurves_block,
            "fcurve_total": len(all_fcurves),
            "nla_tracks": nla_block[:100],
            "shape_keys": shape_block,
            "constraints": [{"name": c.name, "type": c.type} for c in obj.constraints][:100],
        }

    # ------------------------------------------------------------------
    # Camera & rendering helpers & handlers (C3)
    # ------------------------------------------------------------------

    def _resolve_view_targets(self, object_names):
        """Resolve object names to frame; default = all visible mesh objects"""
        if object_names:
            if isinstance(object_names, str):
                object_names = [object_names]
            return [self._get_object_or_raise(n) for n in object_names]
        objs = []
        for obj in bpy.context.scene.objects:
            if obj.type != 'MESH':
                continue
            try:
                if obj.visible_get():
                    objs.append(obj)
            except Exception:
                objs.append(obj)
        if not objs:
            raise ValueError(
                "No objects to frame. Pass object_names or add visible mesh objects to the scene."
            )
        return objs

    def _resolve_render_camera(self, camera):
        """Resolve a camera by name, else the scene camera; actionable error if neither"""
        if camera:
            cam_obj = self._get_object_or_raise(camera)
            if cam_obj.type != 'CAMERA':
                raise ValueError(f"Object '{camera}' is not a camera (type {cam_obj.type}).")
            return cam_obj
        cam_obj = bpy.context.scene.camera
        if cam_obj is None:
            raise ValueError("No scene camera. Call set_camera first to create and aim one.")
        return cam_obj

    @staticmethod
    def _create_temp_camera(name="MCP_TempCamera"):
        """Create a throwaway camera linked to the scene root collection"""
        cam_data = bpy.data.cameras.new(name)
        cam_obj = bpy.data.objects.new(name, cam_data)
        bpy.context.scene.collection.objects.link(cam_obj)
        return cam_obj

    @staticmethod
    def _remove_temp_camera(cam_obj):
        """Delete a temp camera object and its orphaned camera datablock"""
        try:
            cam_data = cam_obj.data
            bpy.data.objects.remove(cam_obj, do_unlink=True)
            if cam_data and cam_data.users == 0:
                bpy.data.cameras.remove(cam_data)
        except Exception as e:
            print(f"Could not remove temp camera: {str(e)}")

    @staticmethod
    def _aim_camera(cam_obj, location, target):
        """Move a camera to location and rotate it to look at a target point"""
        cam_obj.location = location
        direction = mathutils.Vector(target) - mathutils.Vector(location)
        if direction.length < 1e-9:
            direction = mathutils.Vector((0.0, 0.0, -1.0))
        cam_obj.rotation_mode = 'XYZ'
        cam_obj.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()

    def _frame_camera_on_aabb(self, cam_obj, aabb, direction, margin):
        """Place a camera along direction from the AABB center so the AABB fits in view"""
        aabb_min = mathutils.Vector(aabb[0])
        aabb_max = mathutils.Vector(aabb[1])
        center = (aabb_min + aabb_max) / 2.0
        radius = max((aabb_max - aabb_min).length / 2.0, 1e-3)
        direction = mathutils.Vector(direction)
        if direction.length < 1e-9:
            direction = mathutils.Vector(CAMERA_PRESETS["isometric"])
        direction.normalize()
        margin = max(float(margin), 0.01)

        cam_data = cam_obj.data
        if cam_data.type == 'ORTHO':
            distance = radius * 2.0 * margin + 1.0
            with suppress(Exception):
                cam_data.ortho_scale = 2.0 * radius * margin
        else:
            # Use the smaller field of view so the bounding sphere fits both axes
            try:
                fov = min(cam_data.angle_x, cam_data.angle_y)
            except Exception:
                fov = cam_data.angle
            fov = max(float(fov), 0.01)
            distance = (radius * margin) / math.tan(fov / 2.0)
        distance = max(distance, radius + max(float(cam_data.clip_start), 0.001) + 0.01)
        with suppress(Exception):
            if cam_data.clip_end < distance + radius * 2.0:
                cam_data.clip_end = distance + radius * 4.0
        self._aim_camera(cam_obj, center + direction * distance, center)

    @staticmethod
    def _snapshot_render_settings():
        """Capture every render/scene setting the render handlers may touch"""
        scene = bpy.context.scene
        render = scene.render
        snap = {
            "engine": render.engine,
            "resolution_x": render.resolution_x,
            "resolution_y": render.resolution_y,
            "resolution_percentage": render.resolution_percentage,
            "filepath": render.filepath,
            "file_format": render.image_settings.file_format,
            "camera": scene.camera,
            "frame_current": scene.frame_current,
        }
        if hasattr(scene, "cycles") and hasattr(scene.cycles, "samples"):
            snap["cycles_samples"] = scene.cycles.samples
        if hasattr(scene, "eevee") and hasattr(scene.eevee, "taa_render_samples"):
            snap["eevee_samples"] = scene.eevee.taa_render_samples
        try:
            snap["shading_type"] = scene.display.shading.type
        except Exception:
            pass
        return snap

    @staticmethod
    def _restore_render_settings(snap):
        """Restore settings captured by _snapshot_render_settings (best effort per key)"""
        scene = bpy.context.scene
        render = scene.render
        with suppress(Exception):
            render.engine = snap["engine"]
        with suppress(Exception):
            render.resolution_x = snap["resolution_x"]
        with suppress(Exception):
            render.resolution_y = snap["resolution_y"]
        with suppress(Exception):
            render.resolution_percentage = snap["resolution_percentage"]
        with suppress(Exception):
            render.filepath = snap["filepath"]
        with suppress(Exception):
            render.image_settings.file_format = snap["file_format"]
        with suppress(Exception):
            scene.camera = snap["camera"]
        if "cycles_samples" in snap:
            with suppress(Exception):
                scene.cycles.samples = snap["cycles_samples"]
        if "eevee_samples" in snap:
            with suppress(Exception):
                scene.eevee.taa_render_samples = snap["eevee_samples"]
        if "shading_type" in snap:
            with suppress(Exception):
                scene.display.shading.type = snap["shading_type"]
        with suppress(Exception):
            if scene.frame_current != snap["frame_current"]:
                scene.frame_set(snap["frame_current"])

    def _opengl_render_to_base64(self, filepath):
        """OpenGL-render the scene camera to filepath, return the file as base64"""
        bpy.context.scene.render.filepath = filepath
        bpy.ops.render.opengl(write_still=True, view_context=False)
        with open(filepath, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    def set_camera(self, action, camera=None, object_names=None, preset=None,
                   focal_length=None, ortho=False, margin=1.2, location=None, look_at=None):
        """Create/aim the scene camera: frame objects, apply a view preset, or look at a point"""
        action = str(action).lower()
        valid_actions = ("frame_objects", "preset", "look_at")
        if action not in valid_actions:
            raise ValueError(f"Unknown action '{action}'. Use one of: {', '.join(valid_actions)}.")
        scene = bpy.context.scene

        if preset is not None:
            preset = str(preset).lower()
            if preset not in CAMERA_PRESETS:
                raise ValueError(
                    f"Unknown preset '{preset}'. Valid presets: {', '.join(sorted(CAMERA_PRESETS))}."
                )

        # Resolve the camera: named, else the scene camera, else create one
        if camera:
            cam_obj = self._get_object_or_raise(camera)
            if cam_obj.type != 'CAMERA':
                raise ValueError(f"Object '{camera}' is not a camera (type {cam_obj.type}).")
        elif scene.camera:
            cam_obj = scene.camera
        else:
            cam_data = bpy.data.cameras.new("MCP_Camera")
            cam_obj = bpy.data.objects.new("MCP_Camera", cam_data)
            scene.collection.objects.link(cam_obj)
        scene.camera = cam_obj

        if ortho:
            cam_obj.data.type = 'ORTHO'
        if focal_length is not None:
            cam_obj.data.lens = float(focal_length)

        with suppress(Exception):
            bpy.context.view_layer.update()

        if action in ("frame_objects", "preset"):
            if action == "preset" and preset is None:
                raise ValueError(
                    f"action 'preset' requires a preset name ({', '.join(sorted(CAMERA_PRESETS))})."
                )
            targets = self._resolve_view_targets(object_names)
            aabb = self._get_world_aabb(targets)
            if preset:
                direction = mathutils.Vector(CAMERA_PRESETS[preset])
            else:
                # Keep the camera's current view direction (camera looks along local -Z)
                direction = cam_obj.matrix_world.to_quaternion() @ mathutils.Vector((0.0, 0.0, 1.0))
            self._frame_camera_on_aabb(cam_obj, aabb, direction, margin)
        else:  # look_at
            if location is not None:
                cam_obj.location = self._check_vec3(location, "location")
            if look_at is None:
                raise ValueError(
                    "action 'look_at' requires look_at: a [x, y, z] point or an object name."
                )
            if isinstance(look_at, str):
                target_obj = self._get_object_or_raise(look_at)
                t_min, t_max = self._get_world_aabb(target_obj)
                target_point = [(t_min[i] + t_max[i]) / 2.0 for i in range(3)]
            else:
                target_point = self._check_vec3(look_at, "look_at")
            self._aim_camera(cam_obj, list(cam_obj.location), target_point)

        with suppress(Exception):
            bpy.context.view_layer.update()

        return {
            "camera": cam_obj.name,
            "location": self._vec_list(cam_obj.location),
            "rotation_euler": self._vec_list(cam_obj.rotation_euler),
            "focal_length": round(float(cam_obj.data.lens), 4),
            "is_scene_camera": True,
        }

    def render_preview(self, object_names=None, angles=None, max_size=800, shading="SOLID"):
        """OpenGL-render the target objects from preset angles using a temp camera"""
        if angles is None:
            angles = ["front", "right", "top", "isometric"]
        if isinstance(angles, str):
            angles = [angles]
        if not angles:
            raise ValueError("angles must be a non-empty list of preset names.")
        angles = [str(a).lower() for a in angles][:10]
        for angle in angles:
            if angle not in CAMERA_PRESETS:
                raise ValueError(
                    f"Unknown angle '{angle}'. Valid angles: {', '.join(sorted(CAMERA_PRESETS))}."
                )
        shading = str(shading).upper()
        if shading not in ("SOLID", "MATERIAL"):
            raise ValueError(f"Invalid shading '{shading}'. Use 'SOLID' or 'MATERIAL'.")

        scene = bpy.context.scene
        size = max(64, min(int(max_size), 2048))
        targets = self._resolve_view_targets(object_names)
        with suppress(Exception):
            bpy.context.view_layer.update()
        aabb = self._get_world_aabb(targets)

        snap = self._snapshot_render_settings()
        cam_obj = self._create_temp_camera()
        temp_files = []
        images = []
        try:
            render = scene.render
            render.resolution_x = size
            render.resolution_y = size
            render.resolution_percentage = 100
            render.image_settings.file_format = 'PNG'
            scene.camera = cam_obj
            if shading == "MATERIAL" and hasattr(scene, "display") and hasattr(scene.display, "shading"):
                with suppress(Exception):
                    scene.display.shading.type = 'MATERIAL'

            for i, angle in enumerate(angles):
                self._frame_camera_on_aabb(cam_obj, aabb, CAMERA_PRESETS[angle], 1.2)
                with suppress(Exception):
                    bpy.context.view_layer.update()
                filepath = os.path.join(
                    tempfile.gettempdir(),
                    f"blendermcp_preview_{os.getpid()}_{int(time.time() * 1000)}_{i}.png"
                )
                temp_files.append(filepath)
                images.append({
                    "angle": angle,
                    "image_data": self._opengl_render_to_base64(filepath),
                    "format": "png",
                    "width": size,
                    "height": size,
                })
        finally:
            self._remove_temp_camera(cam_obj)
            for filepath in temp_files:
                with suppress(Exception):
                    if os.path.exists(filepath):
                        os.remove(filepath)
            self._restore_render_settings(snap)

        return {"images": images}

    def render_animation_preview(self, frame_start=None, frame_end=None, num_frames=6,
                                 max_size=512, camera=None):
        """OpenGL-render evenly sampled frames of the animation from the scene camera"""
        scene = bpy.context.scene
        frame_start = scene.frame_start if frame_start is None else int(frame_start)
        frame_end = scene.frame_end if frame_end is None else int(frame_end)
        if frame_end < frame_start:
            frame_start, frame_end = frame_end, frame_start
        num_frames = max(1, min(int(num_frames), 10))
        size = max(64, min(int(max_size), 2048))
        cam_obj = self._resolve_render_camera(camera)

        # Sample frames evenly, always including the first and last
        if num_frames == 1 or frame_end == frame_start:
            frames = [frame_start]
        else:
            span = frame_end - frame_start
            frames = sorted({
                frame_start + int(round(span * i / (num_frames - 1)))
                for i in range(num_frames)
            })

        snap = self._snapshot_render_settings()
        temp_files = []
        images = []
        try:
            render = scene.render
            render.resolution_x = size
            render.resolution_y = size
            render.resolution_percentage = 100
            render.image_settings.file_format = 'PNG'
            scene.camera = cam_obj

            for i, frame in enumerate(frames):
                scene.frame_set(frame)
                filepath = os.path.join(
                    tempfile.gettempdir(),
                    f"blendermcp_animprev_{os.getpid()}_{int(time.time() * 1000)}_{i}.png"
                )
                temp_files.append(filepath)
                images.append({
                    "frame": frame,
                    "image_data": self._opengl_render_to_base64(filepath),
                    "format": "png",
                    "width": size,
                    "height": size,
                })
        finally:
            for filepath in temp_files:
                with suppress(Exception):
                    if os.path.exists(filepath):
                        os.remove(filepath)
            # Also restores the original current frame
            self._restore_render_settings(snap)

        return {"frames_sampled": frames, "images": images}

    def render_image(self, camera=None, resolution_x=960, resolution_y=540,
                     samples=None, engine=None, format="PNG"):
        """Full render through the render engine, returned as a base64 image"""
        scene = bpy.context.scene
        cam_obj = self._resolve_render_camera(camera)

        fmt = str(format).upper().lstrip(".")
        if fmt in ("JPG", "JPEG"):
            fmt, file_format, ext = "jpeg", "JPEG", "jpg"
        elif fmt == "PNG":
            fmt, file_format, ext = "png", "PNG", "png"
        else:
            raise ValueError(f"Invalid format '{format}'. Use 'PNG' or 'JPEG'.")

        res_x = max(16, min(int(resolution_x), 3840))
        res_y = max(16, min(int(resolution_y), 3840))

        snap = self._snapshot_render_settings()
        filepath = os.path.join(
            tempfile.gettempdir(),
            f"blendermcp_render_{os.getpid()}_{int(time.time() * 1000)}.{ext}"
        )
        try:
            render = scene.render
            if engine is not None:
                wanted = str(engine).upper()
                if wanted in ("EEVEE", "BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
                    # EEVEE engine id differs across Blender versions
                    candidates = ["BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"]
                else:
                    candidates = [wanted]
                for candidate in candidates:
                    try:
                        render.engine = candidate
                        break
                    except Exception:
                        continue
                else:
                    raise ValueError(
                        f"Render engine '{engine}' is not available. Use 'CYCLES' or 'EEVEE'."
                    )
            if samples is not None:
                samples = int(samples)
                if render.engine == 'CYCLES':
                    if hasattr(scene, "cycles") and hasattr(scene.cycles, "samples"):
                        scene.cycles.samples = samples
                elif hasattr(scene, "eevee") and hasattr(scene.eevee, "taa_render_samples"):
                    scene.eevee.taa_render_samples = samples
            render.resolution_x = res_x
            render.resolution_y = res_y
            render.resolution_percentage = 100
            render.image_settings.file_format = file_format
            render.filepath = filepath
            scene.camera = cam_obj

            bpy.ops.render.render(write_still=True)

            with open(filepath, "rb") as f:
                image_data = base64.b64encode(f.read()).decode("utf-8")
        finally:
            with suppress(Exception):
                if os.path.exists(filepath):
                    os.remove(filepath)
            self._restore_render_settings(snap)

        return {"image_data": image_data, "format": fmt, "width": res_x, "height": res_y}

    # ------------------------------------------------------------------
    # Pipeline handlers (C4)
    # ------------------------------------------------------------------

    def export_scene(self, filepath, format=None, selected_objects=None,
                     apply_modifiers=True, export_animations=True):
        """Export the scene (or selected objects) to glb/gltf/fbx/obj/usd"""
        if not filepath:
            raise ValueError("filepath is required (e.g. 'C:/exports/scene.glb').")
        filepath = os.path.abspath(bpy.path.abspath(filepath))
        fmt = (format or os.path.splitext(filepath)[1].lstrip(".")).lower().lstrip(".")
        valid_formats = ("glb", "gltf", "fbx", "obj", "usd", "usdc", "usda")
        if not fmt:
            raise ValueError(
                "Could not infer the format: pass format or use a filepath with an "
                "extension (.glb, .gltf, .fbx, .obj, .usd)."
            )
        if fmt not in valid_formats:
            raise ValueError(f"Unsupported format '{fmt}'. Use one of: {', '.join(valid_formats)}.")
        if not os.path.splitext(filepath)[1]:
            filepath += f".{fmt}"
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        use_selection = bool(selected_objects)
        if use_selection:
            if isinstance(selected_objects, str):
                selected_objects = [selected_objects]
            objs = [self._get_object_or_raise(n) for n in selected_objects]
            self._ensure_object_mode()
            for obj in bpy.context.view_layer.objects:
                with suppress(Exception):
                    obj.select_set(False)
            for obj in objs:
                try:
                    obj.select_set(True)
                except Exception as e:
                    raise ValueError(
                        f"Cannot select '{obj.name}' for export (is it hidden?): {str(e)}"
                    )
            bpy.context.view_layer.objects.active = objs[0]
            objects_exported = len(objs)
        else:
            objects_exported = len(bpy.context.scene.objects)

        try:
            if fmt in ("glb", "gltf"):
                bpy.ops.export_scene.gltf(
                    filepath=filepath,
                    export_format='GLTF_SEPARATE' if fmt == "gltf" else 'GLB',
                    use_selection=use_selection,
                    export_apply=bool(apply_modifiers),
                    export_animations=bool(export_animations),
                )
            elif fmt == "fbx":
                bpy.ops.export_scene.fbx(
                    filepath=filepath,
                    use_selection=use_selection,
                    use_mesh_modifiers=bool(apply_modifiers),
                    bake_anim=bool(export_animations),
                )
            elif fmt == "obj":
                bpy.ops.wm.obj_export(
                    filepath=filepath,
                    export_selected_objects=use_selection,
                )
            else:  # usd / usdc / usda
                bpy.ops.wm.usd_export(
                    filepath=filepath,
                    selected_objects_only=use_selection,
                )
        except Exception as e:
            raise Exception(f"Export failed ({fmt}): {str(e)}")

        if not os.path.exists(filepath):
            raise Exception(
                f"Export finished but no file was written at {filepath}. Check the path is writable."
            )
        return {
            "filepath": filepath,
            "format": fmt,
            "size_bytes": os.path.getsize(filepath),
            "objects_exported": objects_exported,
        }

    def import_local_asset(self, filepath, target_size=None, collection=None):
        """Import a local model file (.glb/.gltf/.fbx/.obj/.usd*/.blend) into the scene"""
        if not filepath:
            raise ValueError("filepath is required.")
        filepath = os.path.abspath(bpy.path.abspath(filepath))
        if not os.path.exists(filepath):
            raise ValueError(f"File not found: {filepath}")
        ext = os.path.splitext(filepath)[1].lower().lstrip(".")

        target_col = None
        if collection:
            target_col = bpy.data.collections.get(collection)
            if target_col is None:
                raise ValueError(
                    f"Collection '{collection}' not found. Use organize_scene create_collection first."
                )

        existing = set(bpy.data.objects)
        self._ensure_object_mode()

        try:
            if ext in ("glb", "gltf"):
                bpy.ops.import_scene.gltf(filepath=filepath)
            elif ext == "fbx":
                bpy.ops.import_scene.fbx(filepath=filepath)
            elif ext == "obj":
                if hasattr(bpy.ops.wm, "obj_import"):
                    bpy.ops.wm.obj_import(filepath=filepath)
                else:
                    bpy.ops.import_scene.obj(filepath=filepath)
            elif ext in ("usd", "usdc", "usda", "usdz"):
                bpy.ops.wm.usd_import(filepath=filepath)
            elif ext == "blend":
                # Append all objects from the .blend file
                with bpy.data.libraries.load(filepath, link=False) as (data_from, data_to):
                    data_to.objects = data_from.objects
                for obj in data_to.objects:
                    if obj is not None:
                        bpy.context.scene.collection.objects.link(obj)
            else:
                raise ValueError(
                    f"Unsupported file type '.{ext}'. Supported: .glb, .gltf, .fbx, .obj, "
                    f".usd/.usdc/.usda/.usdz, .blend"
                )
        except ValueError:
            raise
        except Exception as e:
            raise Exception(f"Import failed for {filepath}: {str(e)}")

        with suppress(Exception):
            bpy.context.view_layer.update()

        imported = [obj for obj in bpy.data.objects if obj not in existing]
        if not imported:
            raise Exception("Import finished but no new objects were added to the scene.")
        imported_set = set(imported)
        roots = [obj for obj in imported if obj.parent not in imported_set]

        scale_applied = 1.0
        aabb = self._get_world_aabb(imported)
        if target_size is not None:
            target_size = float(target_size)
            max_dim = max(aabb[1][i] - aabb[0][i] for i in range(3))
            if target_size > 0 and max_dim > 0:
                scale_applied = target_size / max_dim
                # Scale only root objects: children inherit through matrix_world
                for root in roots:
                    root.scale = [root.scale[i] * scale_applied for i in range(3)]
                with suppress(Exception):
                    bpy.context.view_layer.update()
                aabb = self._get_world_aabb(imported)

        if target_col is not None:
            for obj in imported:
                for col in list(obj.users_collection):
                    with suppress(Exception):
                        col.objects.unlink(obj)
                with suppress(Exception):
                    target_col.objects.link(obj)

        result = {
            "imported_objects": [obj.name for obj in imported][:100],
            "dimensions": [round(aabb[1][i] - aabb[0][i], 4) for i in range(3)],
            "world_bounding_box": [self._vec_list(aabb[0]), self._vec_list(aabb[1])],
        }
        if len(imported) > 100:
            result["total_count"] = len(imported)
        if target_size is not None:
            result["scale_applied"] = round(scale_applied, 6)
        if target_col is not None:
            result["collection"] = target_col.name
        return result

    def manage_project(self, action, filepath=None):
        """Save/open/version the .blend project"""
        action = str(action).lower()
        valid_actions = ("save", "save_as", "save_version", "open", "new")
        if action not in valid_actions:
            raise ValueError(f"Unknown action '{action}'. Use one of: {', '.join(valid_actions)}.")

        if filepath:
            filepath = os.path.abspath(bpy.path.abspath(filepath))

        if action == "save":
            try:
                if filepath:
                    os.makedirs(os.path.dirname(filepath), exist_ok=True)
                    bpy.ops.wm.save_mainfile(filepath=filepath)
                elif bpy.data.filepath:
                    bpy.ops.wm.save_mainfile()
                else:
                    raise ValueError(
                        "The project has never been saved. Call manage_project with "
                        "action 'save_as' and a filepath ending in .blend."
                    )
            except ValueError:
                raise
            except Exception as e:
                raise Exception(f"Save failed: {str(e)}")
            return {"action": action, "filepath": bpy.data.filepath, "ok": True}

        if action == "save_as":
            if not filepath:
                raise ValueError("action 'save_as' requires a filepath ending in .blend.")
            if not filepath.lower().endswith(".blend"):
                filepath += ".blend"
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            try:
                bpy.ops.wm.save_as_mainfile(filepath=filepath)
            except Exception as e:
                raise Exception(f"Save As failed: {str(e)}")
            return {"action": action, "filepath": bpy.data.filepath, "ok": True}

        if action == "save_version":
            version_path = _save_version_snapshot()
            return {"action": action, "filepath": version_path, "ok": True}

        if action == "open":
            if not filepath:
                raise ValueError("action 'open' requires a filepath to a .blend file.")
            if not os.path.exists(filepath):
                raise ValueError(f"File not found: {filepath}")
            try:
                bpy.ops.wm.open_mainfile(filepath=filepath)
            except Exception as e:
                raise Exception(f"Open failed: {str(e)}")
            return {"action": action, "filepath": bpy.data.filepath, "ok": True}

        # action == "new"
        try:
            bpy.ops.wm.read_homefile(use_empty=False)
        except Exception as e:
            raise Exception(f"New file failed: {str(e)}")
        return {"action": action, "filepath": None, "ok": True}

    def _assignment_tokens(self, record):
        """Cumulative token estimate for the assignment (prior sessions + this one).

        On the first touch of an existing record this session, the record's
        stored token_estimate becomes the prior-session baseline; from then on
        this session's contribution is bytes_sent/4 since that moment.
        """
        session_tokens = self.bytes_sent // 4
        if self._assignment_token_base is None:
            self._assignment_token_base = session_tokens
            self._assignment_prior_tokens = \
                int((record or {}).get("token_estimate", 0) or 0)
        return self._assignment_prior_tokens + \
            max(session_tokens - self._assignment_token_base, 0)

    def manage_assignment(self, action, title=None, brief=None, plan=None,
                          step=None, done=True, decision=None, note=None,
                          handoff=None):
        """Persistent assignment record for session continuity.

        Canonical storage: the MCP_Assignment text datablock (JSON, travels
        inside the .blend). Mirror: <blend_stem>.assignment.md next to saved
        files (also refreshed by the save_post handler on every save).
        """
        action = str(action).lower()
        valid_actions = ("start", "update", "read", "handoff")
        if action not in valid_actions:
            raise ValueError(
                f"Unknown action '{action}'. Use one of: {', '.join(valid_actions)}."
            )

        # LLMs sometimes send the plan as a JSON-encoded string - tolerate it.
        if isinstance(plan, str):
            try:
                parsed = json.loads(plan)
                plan = parsed if isinstance(parsed, list) else [plan]
            except Exception:
                plan = [plan]
        if plan is not None and not isinstance(plan, list):
            raise ValueError("plan must be a list of step strings.")

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        record = _assignment_load()

        if action == "read":
            if not record:
                return {"exists": False}
            record["token_estimate"] = self._assignment_tokens(record)
            result = dict(record)
            result["exists"] = True
            result["markdown"] = _assignment_markdown(record)
            result["sidecar_path"] = _assignment_sidecar_path()
            return result

        if action == "start":
            if not title:
                raise ValueError(
                    "action 'start' requires a title. Provide a short assignment "
                    "title and a plan (list of step strings)."
                )
            # Fresh assignment: attribute this whole session's traffic to it
            self._assignment_prior_tokens = 0
            self._assignment_token_base = 0
            record = {
                "version": 1,
                "title": str(title),
                "brief": str(brief) if brief else "",
                "created": now,
                "updated": now,
                "status": "active",
                "plan": [{"step": str(s), "done": False} for s in (plan or [])],
                "decisions": [],
                "log": [],
                "handoff": None,
                "token_estimate": 0,
            }
        else:
            # update / handoff need an existing record
            if not record:
                raise ValueError(
                    "No assignment record exists. Use manage_assignment "
                    "action 'start' first (or 'read' to check)."
                )
            if action == "update":
                if step is not None:
                    needle = str(step).lower()
                    match = next(
                        (p for p in record.get("plan", [])
                         if needle in str(p.get("step", "")).lower()), None)
                    if match is None:
                        existing = ", ".join(
                            str(p.get("step")) for p in record.get("plan", [])
                        ) or "(no steps)"
                        raise ValueError(
                            f"No plan step matches '{step}'. Existing steps: {existing}"
                        )
                    match["done"] = bool(done)
                if decision is not None:
                    record.setdefault("decisions", []).append(str(decision))
                if note is not None:
                    record.setdefault("log", []).append(f"[{now}] {str(note)}")
                    record["log"] = record["log"][-100:]
                if plan:
                    record.setdefault("plan", []).extend(
                        {"step": str(s), "done": False} for s in plan)
            elif action == "handoff":
                if not handoff:
                    raise ValueError(
                        "action 'handoff' requires handoff text "
                        "(final summary + next steps)."
                    )
                record["handoff"] = str(handoff)
                record["status"] = "complete"

        record["updated"] = now
        record["token_estimate"] = self._assignment_tokens(record)
        sidecar_path, sidecar_error = _assignment_store(record)
        result = dict(record)
        result["sidecar_path"] = sidecar_path
        if sidecar_error:
            result["sidecar_error"] = sidecar_error
        return result

    # ------------------------------------------------------------------
    # Video sequence editor (VSE) helpers & handlers
    # ------------------------------------------------------------------
    # Blender 5.x renamed the sequencer API (strips/strips_all, Strip types,
    # new_effect(length=, input1=, input2=), content_start/left_handle/
    # right_handle/duration). The helpers below feature-detect and fall back
    # to the 4.x names (sequences/sequences_all, frame_final_*, frame_end=,
    # seq1=/seq2=) so both generations keep working.

    @staticmethod
    def _get_sequence_editor():
        """Return the scene's sequence editor, creating it on first use."""
        scene = bpy.context.scene
        if scene.sequence_editor is None:
            scene.sequence_editor_create()
        return scene.sequence_editor

    @staticmethod
    def _seq_collection(se):
        """Top-level strip collection: 'strips' (5.x) or 'sequences' (4.x)"""
        col = getattr(se, "strips", None)
        if col is None:
            col = getattr(se, "sequences", None)
        if col is None:
            raise RuntimeError("This Blender build exposes no sequencer strip collection.")
        return col

    @classmethod
    def _seq_all(cls, se):
        """All strips including inside metas: 'strips_all' (5.x) / 'sequences_all' (4.x)"""
        col = getattr(se, "strips_all", None)
        if col is None:
            col = getattr(se, "sequences_all", None)
        return col if col is not None else cls._seq_collection(se)

    def _find_strip(self, name):
        """Find a strip by name or raise with guidance"""
        se = self._get_sequence_editor()
        strip = self._seq_all(se).get(str(name))
        if strip is None:
            existing = ", ".join(s.name for s in list(self._seq_all(se))[:20]) or "(none)"
            raise ValueError(
                f"Strip '{name}' not found. Existing strips: {existing}. "
                f"Use manage_sequence action 'list' to inspect the timeline."
            )
        return strip

    @staticmethod
    def _strip_final_start(strip):
        """First shown frame (left_handle on 5.x, frame_final_start on 4.x)"""
        if hasattr(strip, "left_handle"):
            return int(strip.left_handle)
        return int(strip.frame_final_start)

    @staticmethod
    def _strip_final_end(strip):
        """End frame, exclusive (right_handle on 5.x, frame_final_end on 4.x)"""
        if hasattr(strip, "right_handle"):
            return int(strip.right_handle)
        return int(strip.frame_final_end)

    @staticmethod
    def _strip_duration(strip):
        if hasattr(strip, "duration"):
            return int(strip.duration)
        return int(strip.frame_final_duration)

    @staticmethod
    def _set_strip_duration(strip, duration):
        duration = max(1, int(duration))
        if hasattr(strip, "duration"):
            strip.duration = duration
        else:
            strip.frame_final_duration = duration

    @classmethod
    def _move_strip_to(cls, strip, frame_start):
        """Move a whole strip so its first shown frame lands on frame_start"""
        delta = int(frame_start) - cls._strip_final_start(strip)
        if not delta:
            return
        if hasattr(strip, "content_start"):
            strip.content_start = strip.content_start + delta
        else:
            strip.frame_start = strip.frame_start + delta

    def _next_free_channel(self, se, frame_start, frame_end, exclude=None, minimum=1):
        """Lowest channel with no strip overlapping [frame_start, frame_end)"""
        used = set()
        for s in self._seq_collection(se):
            if exclude is not None and s.name == getattr(exclude, "name", None):
                continue
            if self._strip_final_end(s) <= frame_start or self._strip_final_start(s) >= frame_end:
                continue
            used.add(int(s.channel))
        channel = max(1, int(minimum))
        while channel in used and channel < 128:
            channel += 1
        return channel

    def _place_strip_on_channel(self, se, strip, channel):
        """Assign a strip's channel: explicit value, or the lowest free channel"""
        start, end = self._strip_final_start(strip), self._strip_final_end(strip)
        if channel is None:
            channel = self._next_free_channel(se, start, end, exclude=strip)
        strip.channel = max(1, min(int(channel), 128))
        return int(strip.channel)

    def _strip_summary(self, strip):
        """Serializable summary of one strip (for list())"""
        stype = str(getattr(strip, "type", ""))
        entry = {
            "name": strip.name,
            "type": stype,
            "channel": int(strip.channel),
            "frame_start": self._strip_final_start(strip),
            "frame_final_end": self._strip_final_end(strip),
            "mute": bool(getattr(strip, "mute", False)),
            "has_audio": stype == 'SOUND',
        }
        filepath = None
        if stype == 'MOVIE':
            filepath = getattr(strip, "filepath", None)
        elif stype == 'IMAGE':
            try:
                elems = strip.elements
                filepath = os.path.join(strip.directory, elems[0].filename) if len(elems) \
                    else strip.directory
            except Exception:
                filepath = getattr(strip, "directory", None)
        elif stype == 'SOUND':
            with suppress(Exception):
                filepath = strip.sound.filepath
        if stype == 'TEXT':
            entry["text"] = str(getattr(strip, "text", ""))[:200]
        elif filepath:
            entry["filepath"] = str(filepath)
        return entry

    def _sequence_state(self):
        """Full timeline state: the manage_sequence 'list' payload"""
        scene = bpy.context.scene
        se = self._get_sequence_editor()
        strips = sorted(
            self._seq_collection(se),
            key=lambda s: (int(s.channel), self._strip_final_start(s)),
        )
        timeline = self._timeline_status()
        return {
            "resolution": [scene.render.resolution_x, scene.render.resolution_y],
            "fps": timeline["fps"],
            "frame_start": timeline["frame_start"],
            "frame_end": timeline["frame_end"],
            "duration_seconds": timeline["duration_seconds"],
            "strips": [self._strip_summary(s) for s in strips[:100]],
            "total_strips": len(strips),
        }

    @staticmethod
    def _resolve_delivery(preset=None, resolution=None, fps=None):
        """Resolve preset/explicit overrides to (resolution|None, fps|None)"""
        res, fps_val = None, None
        if preset is not None:
            key = str(preset).upper()
            if key not in DELIVERY_PRESETS:
                raise ValueError(
                    f"Unknown preset '{preset}'. Valid presets: "
                    f"{', '.join(sorted(DELIVERY_PRESETS))}."
                )
            entry = DELIVERY_PRESETS[key]
            res, fps_val = list(entry["resolution"]), entry["fps"]
        if resolution is not None:
            if not isinstance(resolution, (list, tuple)) or len(resolution) != 2:
                raise ValueError("resolution must be [width, height].")
            res = [int(resolution[0]), int(resolution[1])]
        if fps is not None:
            fps_val = float(fps)
        return res, fps_val

    @staticmethod
    def _apply_delivery(res, fps_val):
        scene = bpy.context.scene
        if res is not None:
            scene.render.resolution_x = int(res[0])
            scene.render.resolution_y = int(res[1])
            scene.render.resolution_percentage = 100
        if fps_val is not None:
            scene.render.fps = int(round(float(fps_val)))
            scene.render.fps_base = 1.0

    @staticmethod
    def _new_effect_strip(col, name, effect_type, channel, frame_start, length,
                          input1=None, input2=None):
        """new_effect across API generations: 5.x length/input1/input2, 4.x frame_end/seq1/seq2"""
        length = max(1, int(length))
        kwargs = {"name": name, "type": effect_type, "channel": int(channel),
                  "frame_start": int(frame_start), "length": length}
        if input1 is not None:
            kwargs["input1"] = input1
        if input2 is not None:
            kwargs["input2"] = input2
        try:
            return col.new_effect(**kwargs)
        except TypeError:
            kwargs = {"name": name, "type": effect_type, "channel": int(channel),
                      "frame_start": int(frame_start),
                      "frame_end": int(frame_start) + length}
            if input1 is not None:
                kwargs["seq1"] = input1
            if input2 is not None:
                kwargs["seq2"] = input2
            return col.new_effect(**kwargs)

    @staticmethod
    def _collect_image_sequence(filepath):
        """Image-sequence detection: a directory, or a numbered first frame with siblings.

        Returns (directory, [filenames]) for a sequence, or None for a single still.
        """
        if os.path.isdir(filepath):
            files = sorted(
                f for f in os.listdir(filepath)
                if os.path.splitext(f)[1].lower() in VSE_IMAGE_EXTENSIONS
            )
            if not files:
                raise ValueError(f"Directory '{filepath}' contains no image files.")
            return filepath, files
        if os.path.splitext(filepath)[1].lower() not in VSE_IMAGE_EXTENSIONS:
            return None
        directory, filename = os.path.split(filepath)
        m = re.match(r"^(.*?)(\d+)(\.[A-Za-z0-9]+)$", filename)
        if not m:
            return None
        prefix, _, ext = m.group(1), m.group(2), m.group(3)
        pattern = re.compile(
            r"^" + re.escape(prefix) + r"(\d+)" + re.escape(ext) + r"$", re.IGNORECASE
        )
        siblings = []
        with suppress(Exception):
            for f in os.listdir(directory or "."):
                mm = pattern.match(f)
                if mm:
                    siblings.append((int(mm.group(1)), f))
        if len(siblings) < 2:
            return None
        siblings.sort()
        return directory, [f for _, f in siblings]

    def _vse_setup_timeline(self, preset, resolution, fps, frame_start, frame_end):
        scene = bpy.context.scene
        self._get_sequence_editor()
        res, fps_val = self._resolve_delivery(preset, resolution, fps)
        self._apply_delivery(res, fps_val)
        scene.frame_start = int(frame_start if frame_start is not None else 1)
        if frame_end is not None:
            scene.frame_end = int(frame_end)
        return {"action": "setup_timeline",
                "preset": str(preset).upper() if preset else None}

    def _vse_add_media(self, filepath, channel, frame_start, name, fit):
        if not filepath:
            raise ValueError(
                "add_media requires filepath (a movie/image/audio file, an image "
                "directory, or the first frame of a numbered image sequence)."
            )
        fit = str(fit or "FIT").upper()
        if fit not in ("FIT", "FILL", "STRETCH", "ORIGINAL"):
            raise ValueError(f"Invalid fit '{fit}'. Use FIT, FILL, STRETCH or ORIGINAL.")
        filepath = os.path.abspath(bpy.path.abspath(str(filepath)))
        if not os.path.exists(filepath):
            raise ValueError(f"File not found: {filepath}")
        frame_start = int(frame_start if frame_start is not None else 1)

        se = self._get_sequence_editor()
        col = self._seq_collection(se)
        ext = os.path.splitext(filepath)[1].lower()
        # Create on a guaranteed-free scratch channel; relocate once the
        # final frame range is known (movie/audio lengths come from the file).
        scratch = min(128, max((int(s.channel) for s in col), default=0) + 1)
        base_name = name or os.path.splitext(os.path.basename(filepath))[0]

        def _new_visual(factory, **kwargs):
            try:
                return factory(fit_method=fit, **kwargs)
            except TypeError:
                return factory(**kwargs)  # very old Blender: no fit_method

        sequence = None if ext in (VSE_MOVIE_EXTENSIONS | VSE_AUDIO_EXTENSIONS) \
            else self._collect_image_sequence(filepath)
        if ext in VSE_MOVIE_EXTENSIONS:
            media_type = "movie"
            strip = _new_visual(col.new_movie, name=base_name, filepath=filepath,
                                channel=scratch, frame_start=frame_start)
        elif ext in VSE_AUDIO_EXTENSIONS:
            media_type = "audio"
            strip = col.new_sound(name=base_name, filepath=filepath,
                                  channel=scratch, frame_start=frame_start)
        elif sequence is not None:
            media_type = "image_sequence"
            directory, files = sequence
            strip = _new_visual(col.new_image, name=base_name,
                                filepath=os.path.join(directory, files[0]),
                                channel=scratch, frame_start=frame_start)
            for extra in files[1:]:
                strip.elements.append(extra)
            self._set_strip_duration(strip, len(files))
        elif ext in VSE_IMAGE_EXTENSIONS:
            media_type = "image"
            strip = _new_visual(col.new_image, name=base_name, filepath=filepath,
                                channel=scratch, frame_start=frame_start)
            self._set_strip_duration(strip, 96)  # default still duration
        else:
            raise ValueError(
                f"Unsupported extension '{ext}'. Movies: "
                f"{', '.join(sorted(VSE_MOVIE_EXTENSIONS))}; images: "
                f"{', '.join(sorted(VSE_IMAGE_EXTENSIONS))}; audio: "
                f"{', '.join(sorted(VSE_AUDIO_EXTENSIONS))}."
            )

        self._place_strip_on_channel(se, strip, channel)
        return {"action": "add_media", "media_type": media_type,
                "strip": self._strip_summary(strip)}

    def _vse_add_text(self, text, frame_start, duration, channel, size, color,
                      position, font_path, name):
        if not text:
            raise ValueError("add_text requires text.")
        position = str(position or "BOTTOM").upper()
        positions = {"BOTTOM": 0.12, "CENTER": 0.5, "TOP": 0.85}
        if position not in positions:
            raise ValueError(f"Invalid position '{position}'. Use BOTTOM, CENTER or TOP.")
        frame_start = int(frame_start if frame_start is not None else 1)
        duration = max(1, int(duration if duration is not None else 96))

        se = self._get_sequence_editor()
        col = self._seq_collection(se)
        if channel is None:
            channel = self._next_free_channel(se, frame_start, frame_start + duration)
        strip = self._new_effect_strip(
            col, name or "Text", 'TEXT', channel, frame_start, duration)
        strip.text = str(text)
        with suppress(Exception):
            strip.font_size = float(size if size is not None else 64)
        if color is not None:
            col4 = list(color) + [1.0] * (4 - len(list(color)))
            with suppress(Exception):
                strip.color = col4[:4]
        if hasattr(strip, "location"):
            strip.location = (0.5, positions[position])
        with suppress(Exception):
            strip.anchor_x = 'CENTER'
            strip.anchor_y = position
        if hasattr(strip, "use_shadow"):
            strip.use_shadow = True  # legibility over any footage
        warning = None
        if font_path:
            try:
                strip.font = bpy.data.fonts.load(
                    os.path.abspath(bpy.path.abspath(str(font_path))),
                    check_existing=True)
            except Exception as e:
                warning = f"Could not load font '{font_path}': {e}"
        result = {"action": "add_text", "strip": self._strip_summary(strip)}
        if warning:
            result["warning"] = warning
        return result

    def _vse_add_transition(self, strip_a, strip_b, transition_type, duration):
        if not strip_a or not strip_b:
            raise ValueError("add_transition requires strip_a and strip_b (strip names).")
        ttype = str(transition_type or "CROSS").upper()
        if ttype not in ("CROSS", "WIPE", "GAMMA_CROSS"):
            raise ValueError(f"Invalid transition type '{ttype}'. Use CROSS or WIPE.")
        duration = max(2, int(duration if duration is not None else 12))
        a = self._find_strip(strip_a)
        b = self._find_strip(strip_b)
        if a.name == b.name:
            raise ValueError("strip_a and strip_b must be different strips.")
        # a = the earlier strip
        if self._strip_final_start(b) < self._strip_final_start(a):
            a, b = b, a

        se = self._get_sequence_editor()
        col = self._seq_collection(se)
        overlap = self._strip_final_end(a) - self._strip_final_start(b)
        shifted_by = 0
        if overlap < duration:
            # Shift b back so the strips overlap by exactly `duration` frames
            # (covers both a gap and a too-small overlap).
            shifted_by = duration - overlap
            self._move_strip_to(b, self._strip_final_start(b) - shifted_by)
        start = self._strip_final_start(b)
        length = min(duration, self._strip_final_end(a) - start)
        channel = self._next_free_channel(
            se, start, start + length, minimum=max(int(a.channel), int(b.channel)) + 1)
        fx = self._new_effect_strip(
            col, f"{ttype}_{a.name}_{b.name}"[:60], ttype, channel, start, length,
            input1=a, input2=b)
        result = {
            "action": "add_transition",
            "transition": self._strip_summary(fx),
            "type": ttype,
            "strip_a": a.name,
            "strip_b": b.name,
        }
        if shifted_by:
            result["shifted"] = {
                "strip": b.name,
                "moved_back_frames": shifted_by,
                "reason": "strips did not overlap; strip_b was shifted back to "
                          "create the transition overlap",
            }
        return result

    def _vse_add_fade(self, strip_name, fade_type, duration):
        if not strip_name:
            raise ValueError("add_fade requires strip_name.")
        fade_type = str(fade_type or "IN").upper()
        if fade_type not in ("IN", "OUT", "BOTH"):
            raise ValueError(f"Invalid fade_type '{fade_type}'. Use IN, OUT or BOTH.")
        duration = max(1, int(duration if duration is not None else 12))
        strip = self._find_strip(strip_name)
        prop = "volume" if strip.type == 'SOUND' else "blend_alpha"
        if not hasattr(strip, prop):
            raise ValueError(
                f"Strip '{strip.name}' (type {strip.type}) supports no fade property.")
        full = float(getattr(strip, prop)) or 1.0
        start = self._strip_final_start(strip)
        end = self._strip_final_end(strip)
        duration = min(duration, max(1, end - start))

        def _key(frame, value):
            setattr(strip, prop, value)
            strip.keyframe_insert(prop, frame=int(frame))

        frames = []
        if fade_type in ("IN", "BOTH"):
            _key(start, 0.0)
            _key(min(start + duration, end), full)
            frames += [start, min(start + duration, end)]
        if fade_type in ("OUT", "BOTH"):
            _key(max(end - duration, start), full)
            _key(end, 0.0)
            frames += [max(end - duration, start), end]
        return {"action": "add_fade", "strip": strip.name, "property": prop,
                "fade_type": fade_type, "keyframed_frames": frames}

    def _vse_set_strip(self, strip_name, frame_start, channel, mute, volume,
                       opacity, speed, end_frame):
        if not strip_name:
            raise ValueError("set_strip requires strip_name.")
        strip = self._find_strip(strip_name)
        se = self._get_sequence_editor()
        changed = []
        if frame_start is not None:
            self._move_strip_to(strip, int(frame_start))
            changed.append("frame_start")
        if end_frame is not None:
            if int(end_frame) <= self._strip_final_start(strip):
                raise ValueError("end_frame must be greater than the strip's start frame.")
            if hasattr(strip, "right_handle"):
                strip.right_handle = int(end_frame)
            else:
                strip.frame_final_end = int(end_frame)
            changed.append("end_frame")
        if channel is not None:
            strip.channel = max(1, min(int(channel), 128))
            changed.append("channel")
        if mute is not None:
            strip.mute = bool(mute)
            changed.append("mute")
        if volume is not None:
            if not hasattr(strip, "volume"):
                raise ValueError(
                    f"Strip '{strip.name}' (type {strip.type}) has no volume; "
                    f"volume only applies to SOUND strips.")
            strip.volume = float(volume)
            changed.append("volume")
        if opacity is not None:
            if not hasattr(strip, "blend_alpha"):
                raise ValueError(
                    f"Strip '{strip.name}' (type {strip.type}) has no opacity; "
                    f"opacity only applies to visual strips.")
            strip.blend_alpha = max(0.0, min(float(opacity), 1.0))
            changed.append("opacity")
        speed_info = None
        if speed is not None:
            speed = float(speed)
            if speed <= 0:
                raise ValueError("speed must be > 0 (e.g. 2.0 = twice as fast).")
            if strip.type == 'SOUND':
                raise ValueError(
                    "speed applies to visual strips only (SOUND strips cannot take "
                    "a SPEED effect). Re-add the audio trimmed to length instead.")
            col = self._seq_collection(se)
            original = self._strip_duration(strip)
            start = self._strip_final_start(strip)
            fx = self._new_effect_strip(
                col, f"Speed_{strip.name}"[:60], 'SPEED',
                self._next_free_channel(se, start, start + original,
                                        minimum=int(strip.channel) + 1),
                start, original, input1=strip)
            if hasattr(fx, "speed_control"):
                fx.speed_control = 'MULTIPLY'
                fx.speed_factor = speed
            elif hasattr(fx, "speed_factor"):
                fx.speed_factor = speed
            else:
                col.remove(fx)
                return {
                    "error": "This Blender build exposes no usable speed control "
                             "(no speed_control/speed_factor on the SPEED effect). "
                             "Retime the source clip externally instead."
                }
            new_duration = max(1, int(round(original / speed)))
            self._set_strip_duration(strip, new_duration)
            speed_info = {"effect": fx.name, "factor": speed,
                          "original_frames": original, "new_frames": new_duration}
            changed.append("speed")
        if not changed:
            raise ValueError(
                "set_strip: provide at least one of frame_start, channel, mute, "
                "volume, opacity, speed, end_frame.")
        result = {"action": "set_strip", "strip": self._strip_summary(strip),
                  "changed": changed}
        if speed_info:
            result["speed"] = speed_info
        return result

    def _vse_remove_strip(self, strip_name):
        if not strip_name:
            raise ValueError("remove_strip requires strip_name.")
        strip = self._find_strip(strip_name)
        removed = strip.name
        self._seq_collection(self._get_sequence_editor()).remove(strip)
        return {"action": "remove_strip", "removed": removed}

    def _vse_clear(self, confirm):
        if confirm is not True:
            raise ValueError(
                "clear removes ALL strips from the timeline; call again with "
                "confirm=true to proceed.")
        se = self._get_sequence_editor()
        col = self._seq_collection(se)
        removed = 0
        # Removing a strip may cascade-remove dependent effect strips, so
        # re-fetch from the live collection each pass instead of iterating.
        guard = 0
        while len(col) and guard < 1024:
            with suppress(Exception):
                col.remove(col[0])
            removed += 1
            guard += 1
        return {"action": "clear", "removed_strips": removed}

    def manage_sequence(self, action, preset=None, resolution=None, fps=None,
                        frame_start=None, frame_end=None, filepath=None,
                        channel=None, name=None, fit="FIT", text=None,
                        duration=None, size=64, color=None, position="BOTTOM",
                        font_path=None, strip_a=None, strip_b=None, type=None,
                        strip_name=None, fade_type="IN", mute=None, volume=None,
                        opacity=None, speed=None, end_frame=None, confirm=False):
        """Composite video-sequence-editor handler (see server tool docstring)"""
        action = str(action).lower()
        if action == "list":
            return self._sequence_state()
        if action == "setup_timeline":
            result = self._vse_setup_timeline(preset, resolution, fps, frame_start, frame_end)
        elif action == "add_media":
            result = self._vse_add_media(filepath, channel, frame_start, name, fit)
        elif action == "add_text":
            result = self._vse_add_text(text, frame_start, duration, channel, size,
                                        color, position, font_path, name)
        elif action == "add_transition":
            result = self._vse_add_transition(strip_a, strip_b, type, duration)
        elif action == "add_fade":
            result = self._vse_add_fade(strip_name, fade_type, duration)
        elif action == "set_strip":
            result = self._vse_set_strip(strip_name, frame_start, channel, mute,
                                         volume, opacity, speed, end_frame)
        elif action == "remove_strip":
            result = self._vse_remove_strip(strip_name)
        elif action == "clear":
            result = self._vse_clear(confirm)
        else:
            raise ValueError(
                f"Unknown action '{action}'. Use setup_timeline, add_media, add_text, "
                f"add_transition, add_fade, set_strip, remove_strip, clear or list."
            )
        if "error" not in result:
            # Every mutating action returns the post-state so the agent never
            # needs a follow-up list() round-trip.
            result["timeline"] = self._sequence_state()
        return result

    @staticmethod
    def _snapshot_vse_render_settings():
        """Capture every setting render_sequence may touch"""
        scene = bpy.context.scene
        render = scene.render
        snap = {
            "resolution_x": render.resolution_x,
            "resolution_y": render.resolution_y,
            "resolution_percentage": render.resolution_percentage,
            "filepath": render.filepath,
            "fps": render.fps,
            "fps_base": render.fps_base,
            "frame_start": scene.frame_start,
            "frame_end": scene.frame_end,
            "frame_current": scene.frame_current,
            "file_format": render.image_settings.file_format,
            "use_sequencer": getattr(render, "use_sequencer", None),
        }
        if hasattr(render.image_settings, "media_type"):
            snap["media_type"] = render.image_settings.media_type
        with suppress(Exception):
            ff = render.ffmpeg
            snap["ffmpeg"] = {
                "format": ff.format,
                "codec": ff.codec,
                "audio_codec": ff.audio_codec,
                "constant_rate_factor": ff.constant_rate_factor,
                "video_bitrate": ff.video_bitrate,
                "gopsize": ff.gopsize,
            }
        return snap

    @staticmethod
    def _apply_ffmpeg_output_settings(container="MPEG4", video_bitrate=None):
        """Configure FFMPEG video output (container/codec/rate/sequencer).

        Shared by the render_sequence handler and the panel's Render Clip
        operator. Caller is responsible for snapshotting/restoring settings.
        """
        render = bpy.context.scene.render
        if hasattr(render.image_settings, "media_type"):
            render.image_settings.media_type = 'VIDEO'  # 5.x gates FFMPEG on this
        render.image_settings.file_format = 'FFMPEG'
        ff = render.ffmpeg
        ff.format = container
        if container == "WEBM":
            codecs, audio_codecs = ("WEBM", "VP9", "AV1"), ("OPUS", "VORBIS")
        else:
            codecs, audio_codecs = ("H264",), ("AAC",)
        for codec in codecs:
            with suppress(Exception):
                ff.codec = codec
                break
        for audio_codec in audio_codecs:
            with suppress(Exception):
                ff.audio_codec = audio_codec
                break
        if video_bitrate is not None:
            with suppress(Exception):
                ff.constant_rate_factor = 'NONE'
            ff.video_bitrate = int(video_bitrate)
        else:
            with suppress(Exception):
                ff.constant_rate_factor = 'HIGH'
        if hasattr(render, "use_sequencer"):
            render.use_sequencer = True

    @staticmethod
    def _restore_vse_render_settings(snap):
        """Restore settings captured by _snapshot_vse_render_settings (best effort)"""
        scene = bpy.context.scene
        render = scene.render
        # media_type gates the file_format enum on 5.x: restore it first
        if "media_type" in snap:
            with suppress(Exception):
                render.image_settings.media_type = snap["media_type"]
        with suppress(Exception):
            render.image_settings.file_format = snap["file_format"]
        ff_snap = snap.get("ffmpeg") or {}
        for key, value in ff_snap.items():
            with suppress(Exception):
                setattr(render.ffmpeg, key, value)
        for key in ("resolution_x", "resolution_y", "resolution_percentage",
                    "filepath", "fps", "fps_base"):
            with suppress(Exception):
                setattr(render, key, snap[key])
        if snap.get("use_sequencer") is not None:
            with suppress(Exception):
                render.use_sequencer = snap["use_sequencer"]
        with suppress(Exception):
            scene.frame_start = snap["frame_start"]
            scene.frame_end = snap["frame_end"]
        with suppress(Exception):
            if scene.frame_current != snap["frame_current"]:
                scene.frame_set(snap["frame_current"])

    def render_sequence(self, filepath=None, preset=None, resolution=None, fps=None,
                        frame_start=None, frame_end=None, container="MPEG4",
                        video_bitrate=None, wait=True, status_only=False):
        """Encode the sequencer timeline to a video file (see server tool docstring)"""
        if status_only:
            job = dict(_RENDER_JOB)
            if job.get("started_at"):
                job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
            return job

        if not filepath:
            raise ValueError("filepath is required (e.g. 'C:/renders/clip.mp4').")
        container = str(container).upper()
        if container not in VSE_CONTAINER_EXTENSIONS:
            raise ValueError(
                f"Unknown container '{container}'. Use one of: "
                f"{', '.join(sorted(VSE_CONTAINER_EXTENSIONS))}."
            )
        expected_ext = VSE_CONTAINER_EXTENSIONS[container]
        filepath = os.path.abspath(bpy.path.abspath(str(filepath)))
        if not filepath.lower().endswith(expected_ext):
            filepath += expected_ext
        parent = os.path.dirname(filepath)
        if parent:
            os.makedirs(parent, exist_ok=True)

        if not wait and bpy.app.background:
            raise ValueError(
                "wait=False needs Blender's interactive event loop and cannot work "
                "in background (headless) mode. Call again with wait=True."
            )
        if not wait and _RENDER_JOB.get("active"):
            raise ValueError(
                "A render job is already active. Poll with status_only=true, or "
                "wait for it to finish."
            )

        scene = bpy.context.scene
        self._get_sequence_editor()
        res, fps_val = self._resolve_delivery(preset, resolution, fps)

        snap = self._snapshot_vse_render_settings()
        restore_in_finally = True
        try:
            render = scene.render
            self._apply_delivery(res, fps_val)
            if frame_start is not None:
                scene.frame_start = int(frame_start)
            if frame_end is not None:
                scene.frame_end = int(frame_end)
            self._apply_ffmpeg_output_settings(container, video_bitrate)
            render.filepath = filepath

            frames = scene.frame_end - scene.frame_start + 1
            try:
                fps_eff = render.fps / render.fps_base
            except Exception:
                fps_eff = float(render.fps)

            if wait:
                bpy.ops.render.render(animation=True, write_still=False)
                actual = filepath
                if not os.path.exists(actual):
                    import glob as _glob
                    candidates = sorted(_glob.glob(filepath + "*")) + sorted(
                        _glob.glob(os.path.splitext(filepath)[0] + "*" + expected_ext))
                    actual = candidates[0] if candidates else None
                if not actual or not os.path.exists(actual):
                    return {"error": f"Render finished but no output file was found at "
                                     f"'{filepath}'. Check the filepath is writable."}
                return {
                    "filepath": actual,
                    "size_bytes": os.path.getsize(actual),
                    "frames": frames,
                    "duration_seconds": round(frames / fps_eff, 3) if fps_eff else None,
                    "fps": round(fps_eff, 4),
                    "resolution": [render.resolution_x, render.resolution_y],
                    "container": container,
                }

            # Async job: restore happens in the render_complete/cancel handlers
            _ensure_render_handlers()
            _RENDER_JOB.update({
                "active": True,
                "frame_current": scene.frame_start,
                "frame_end": scene.frame_end,
                "filepath": filepath,
                "done": False,
                "cancelled": False,
                "error": None,
                "started_at": time.time(),
            })
            _RENDER_JOB_RESTORE.clear()
            _RENDER_JOB_RESTORE.update(snap)
            restore_in_finally = False
            try:
                bpy.ops.render.render('INVOKE_DEFAULT', animation=True)
            except Exception as e:
                _RENDER_JOB["active"] = False
                _RENDER_JOB["error"] = str(e)
                _RENDER_JOB_RESTORE.clear()
                restore_in_finally = True
                raise
            return {
                "job_started": True,
                "filepath": filepath,
                "frames": frames,
                "poll": "Call render_sequence with status_only=true to track progress.",
            }
        finally:
            if restore_in_finally:
                self._restore_vse_render_settings(snap)

    def get_polyhaven_categories(self, asset_type):
        """Get categories for a specific asset type from Polyhaven"""
        disabled = self._integration_disabled_error("blendermcp_use_polyhaven", "PolyHaven")
        if disabled:
            return disabled
        try:
            if asset_type not in ["hdris", "textures", "models", "all"]:
                return {"error": f"Invalid asset type: {asset_type}. Must be one of: hdris, textures, models, all"}

            response = requests.get(f"https://api.polyhaven.com/categories/{asset_type}", headers=REQ_HEADERS, timeout=(10, 60))
            if response.status_code == 200:
                return {"categories": response.json()}
            else:
                return {"error": f"API request failed with status code {response.status_code}"}
        except Exception as e:
            return {"error": str(e)}

    def search_polyhaven_assets(self, asset_type=None, categories=None):
        """Search for assets from Polyhaven with optional filtering"""
        disabled = self._integration_disabled_error("blendermcp_use_polyhaven", "PolyHaven")
        if disabled:
            return disabled
        try:
            url = "https://api.polyhaven.com/assets"
            params = {}

            if asset_type and asset_type != "all":
                if asset_type not in ["hdris", "textures", "models"]:
                    return {"error": f"Invalid asset type: {asset_type}. Must be one of: hdris, textures, models, all"}
                params["type"] = asset_type

            if categories:
                params["categories"] = categories

            response = requests.get(url, params=params, headers=REQ_HEADERS, timeout=(10, 60))
            if response.status_code == 200:
                # Limit the response size to avoid overwhelming Blender
                assets = response.json()
                # Return only the first 20 assets to keep response size manageable
                limited_assets = {}
                for i, (key, value) in enumerate(assets.items()):
                    if i >= 20:  # Limit to 20 assets
                        break
                    limited_assets[key] = value

                return {"assets": limited_assets, "total_count": len(assets), "returned_count": len(limited_assets)}
            else:
                return {"error": f"API request failed with status code {response.status_code}"}
        except Exception as e:
            return {"error": str(e)}

    def download_polyhaven_asset(self, asset_id, asset_type, resolution="1k", file_format=None):
        disabled = self._integration_disabled_error("blendermcp_use_polyhaven", "PolyHaven")
        if disabled:
            return disabled
        try:
            # First get the files information
            files_response = requests.get(f"https://api.polyhaven.com/files/{asset_id}", headers=REQ_HEADERS, timeout=(10, 60))
            if files_response.status_code != 200:
                return {"error": f"Failed to get asset files: {files_response.status_code}"}

            files_data = files_response.json()

            # Handle different asset types
            if asset_type == "hdris":
                # For HDRIs, download the .hdr or .exr file
                if not file_format:
                    file_format = "hdr"  # Default format for HDRIs

                if "hdri" in files_data and resolution in files_data["hdri"] and file_format in files_data["hdri"][resolution]:
                    file_info = files_data["hdri"][resolution][file_format]
                    file_url = file_info["url"]

                    # For HDRIs, we need to save to a temporary file first
                    # since Blender can't properly load HDR data directly from memory
                    with tempfile.NamedTemporaryFile(suffix=f".{file_format}", delete=False) as tmp_file:
                        # Download the file
                        response = requests.get(file_url, headers=REQ_HEADERS, timeout=(10, 60))
                        if response.status_code != 200:
                            return {"error": f"Failed to download HDRI: {response.status_code}"}

                        tmp_file.write(response.content)
                        tmp_path = tmp_file.name

                    try:
                        # Create a new world if none exists
                        if not bpy.data.worlds:
                            bpy.data.worlds.new("World")

                        world = bpy.data.worlds[0]
                        world.use_nodes = True
                        node_tree = world.node_tree

                        # Clear existing nodes
                        for node in node_tree.nodes:
                            node_tree.nodes.remove(node)

                        # Create nodes
                        tex_coord = node_tree.nodes.new(type='ShaderNodeTexCoord')
                        tex_coord.location = (-800, 0)

                        mapping = node_tree.nodes.new(type='ShaderNodeMapping')
                        mapping.location = (-600, 0)

                        # Load the image from the temporary file
                        env_tex = node_tree.nodes.new(type='ShaderNodeTexEnvironment')
                        env_tex.location = (-400, 0)
                        env_tex.image = bpy.data.images.load(tmp_path)

                        # Use a color space that exists in all Blender versions
                        if file_format.lower() == 'exr':
                            # Try to use Linear color space for EXR files
                            try:
                                env_tex.image.colorspace_settings.name = 'Linear'
                            except:
                                # Fallback to Non-Color if Linear isn't available
                                env_tex.image.colorspace_settings.name = 'Non-Color'
                        else:  # hdr
                            # For HDR files, try these options in order
                            for color_space in ['Linear', 'Linear Rec.709', 'Non-Color']:
                                try:
                                    env_tex.image.colorspace_settings.name = color_space
                                    break  # Stop if we successfully set a color space
                                except:
                                    continue

                        background = node_tree.nodes.new(type='ShaderNodeBackground')
                        background.location = (-200, 0)

                        output = node_tree.nodes.new(type='ShaderNodeOutputWorld')
                        output.location = (0, 0)

                        # Connect nodes
                        node_tree.links.new(tex_coord.outputs['Generated'], mapping.inputs['Vector'])
                        node_tree.links.new(mapping.outputs['Vector'], env_tex.inputs['Vector'])
                        node_tree.links.new(env_tex.outputs['Color'], background.inputs['Color'])
                        node_tree.links.new(background.outputs['Background'], output.inputs['Surface'])

                        # Set as active world
                        bpy.context.scene.world = world

                        # Clean up temporary file
                        try:
                            tempfile._cleanup()  # This will clean up all temporary files
                        except:
                            pass

                        return {
                            "success": True,
                            "message": f"HDRI {asset_id} imported successfully",
                            "image_name": env_tex.image.name
                        }
                    except Exception as e:
                        return {"error": f"Failed to set up HDRI in Blender: {str(e)}"}
                else:
                    return {"error": f"Requested resolution or format not available for this HDRI"}

            elif asset_type == "textures":
                if not file_format:
                    file_format = "jpg"  # Default format for textures

                downloaded_maps = {}

                try:
                    for map_type in files_data:
                        if map_type not in ["blend", "gltf"]:  # Skip non-texture files
                            if resolution in files_data[map_type] and file_format in files_data[map_type][resolution]:
                                file_info = files_data[map_type][resolution][file_format]
                                file_url = file_info["url"]

                                # Use NamedTemporaryFile like we do for HDRIs
                                with tempfile.NamedTemporaryFile(suffix=f".{file_format}", delete=False) as tmp_file:
                                    # Download the file
                                    response = requests.get(file_url, headers=REQ_HEADERS, timeout=(10, 60))
                                    if response.status_code == 200:
                                        tmp_file.write(response.content)
                                        tmp_path = tmp_file.name

                                        # Load image from temporary file
                                        image = bpy.data.images.load(tmp_path)
                                        image.name = f"{asset_id}_{map_type}.{file_format}"

                                        # Pack the image into .blend file
                                        image.pack()

                                        # Set color space based on map type
                                        if map_type in ['color', 'diffuse', 'albedo']:
                                            try:
                                                image.colorspace_settings.name = 'sRGB'
                                            except:
                                                pass
                                        else:
                                            try:
                                                image.colorspace_settings.name = 'Non-Color'
                                            except:
                                                pass

                                        downloaded_maps[map_type] = image

                                        # Clean up temporary file
                                        try:
                                            os.unlink(tmp_path)
                                        except:
                                            pass

                    if not downloaded_maps:
                        return {"error": f"No texture maps found for the requested resolution and format"}

                    # Create a new material with the downloaded textures
                    mat = bpy.data.materials.new(name=asset_id)
                    mat.use_nodes = True
                    nodes = mat.node_tree.nodes
                    links = mat.node_tree.links

                    # Clear default nodes
                    for node in nodes:
                        nodes.remove(node)

                    # Create output node
                    output = nodes.new(type='ShaderNodeOutputMaterial')
                    output.location = (300, 0)

                    # Create principled BSDF node
                    principled = nodes.new(type='ShaderNodeBsdfPrincipled')
                    principled.location = (0, 0)
                    links.new(principled.outputs[0], output.inputs[0])

                    # Add texture nodes based on available maps
                    tex_coord = nodes.new(type='ShaderNodeTexCoord')
                    tex_coord.location = (-800, 0)

                    mapping = nodes.new(type='ShaderNodeMapping')
                    mapping.location = (-600, 0)
                    mapping.vector_type = 'TEXTURE'  # Changed from default 'POINT' to 'TEXTURE'
                    links.new(tex_coord.outputs['UV'], mapping.inputs['Vector'])

                    # Position offset for texture nodes
                    x_pos = -400
                    y_pos = 300

                    # Connect different texture maps
                    for map_type, image in downloaded_maps.items():
                        tex_node = nodes.new(type='ShaderNodeTexImage')
                        tex_node.location = (x_pos, y_pos)
                        tex_node.image = image

                        # Set color space based on map type
                        if map_type.lower() in ['color', 'diffuse', 'albedo']:
                            try:
                                tex_node.image.colorspace_settings.name = 'sRGB'
                            except:
                                pass  # Use default if sRGB not available
                        else:
                            try:
                                tex_node.image.colorspace_settings.name = 'Non-Color'
                            except:
                                pass  # Use default if Non-Color not available

                        links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])

                        # Connect to appropriate input on Principled BSDF
                        if map_type.lower() in ['color', 'diffuse', 'albedo']:
                            links.new(tex_node.outputs['Color'], principled.inputs['Base Color'])
                        elif map_type.lower() in ['roughness', 'rough']:
                            links.new(tex_node.outputs['Color'], principled.inputs['Roughness'])
                        elif map_type.lower() in ['metallic', 'metalness', 'metal']:
                            links.new(tex_node.outputs['Color'], principled.inputs['Metallic'])
                        elif map_type.lower() in ['normal', 'nor']:
                            # Add normal map node
                            normal_map = nodes.new(type='ShaderNodeNormalMap')
                            normal_map.location = (x_pos + 200, y_pos)
                            links.new(tex_node.outputs['Color'], normal_map.inputs['Color'])
                            links.new(normal_map.outputs['Normal'], principled.inputs['Normal'])
                        elif map_type in ['displacement', 'disp', 'height']:
                            # Add displacement node
                            disp_node = nodes.new(type='ShaderNodeDisplacement')
                            disp_node.location = (x_pos + 200, y_pos - 200)
                            links.new(tex_node.outputs['Color'], disp_node.inputs['Height'])
                            links.new(disp_node.outputs['Displacement'], output.inputs['Displacement'])

                        y_pos -= 250

                    return {
                        "success": True,
                        "message": f"Texture {asset_id} imported as material",
                        "material": mat.name,
                        "maps": list(downloaded_maps.keys())
                    }

                except Exception as e:
                    return {"error": f"Failed to process textures: {str(e)}"}

            elif asset_type == "models":
                # For models, prefer glTF format if available
                if not file_format:
                    file_format = "gltf"  # Default format for models

                if file_format in files_data and resolution in files_data[file_format]:
                    file_info = files_data[file_format][resolution][file_format]
                    file_url = file_info["url"]

                    # Create a temporary directory to store the model and its dependencies
                    temp_dir = tempfile.mkdtemp()
                    main_file_path = ""

                    try:
                        # Download the main model file
                        main_file_name = file_url.split("/")[-1]
                        main_file_path = os.path.join(temp_dir, main_file_name)

                        response = requests.get(file_url, headers=REQ_HEADERS, timeout=(10, 60))
                        if response.status_code != 200:
                            return {"error": f"Failed to download model: {response.status_code}"}

                        with open(main_file_path, "wb") as f:
                            f.write(response.content)

                        # Check for included files and download them
                        if "include" in file_info and file_info["include"]:
                            for include_path, include_info in file_info["include"].items():
                                # Get the URL for the included file - this is the fix
                                include_url = include_info["url"]

                                # Create the directory structure for the included file
                                include_file_path = os.path.join(temp_dir, include_path)
                                os.makedirs(os.path.dirname(include_file_path), exist_ok=True)

                                # Download the included file
                                include_response = requests.get(include_url, headers=REQ_HEADERS, timeout=(10, 60))
                                if include_response.status_code == 200:
                                    with open(include_file_path, "wb") as f:
                                        f.write(include_response.content)
                                else:
                                    print(f"Failed to download included file: {include_path}")

                        # Import the model into Blender
                        if file_format == "gltf" or file_format == "glb":
                            bpy.ops.import_scene.gltf(filepath=main_file_path)
                        elif file_format == "fbx":
                            bpy.ops.import_scene.fbx(filepath=main_file_path)
                        elif file_format == "obj":
                            bpy.ops.import_scene.obj(filepath=main_file_path)
                        elif file_format == "blend":
                            # For blend files, we need to append or link
                            with bpy.data.libraries.load(main_file_path, link=False) as (data_from, data_to):
                                data_to.objects = data_from.objects

                            # Link the objects to the scene
                            for obj in data_to.objects:
                                if obj is not None:
                                    bpy.context.collection.objects.link(obj)
                        else:
                            return {"error": f"Unsupported model format: {file_format}"}

                        # Get the names of imported objects
                        imported_objects = [obj.name for obj in bpy.context.selected_objects]

                        return {
                            "success": True,
                            "message": f"Model {asset_id} imported successfully",
                            "imported_objects": imported_objects
                        }
                    except Exception as e:
                        return {"error": f"Failed to import model: {str(e)}"}
                    finally:
                        # Clean up temporary directory
                        with suppress(Exception):
                            shutil.rmtree(temp_dir)
                else:
                    return {"error": f"Requested format or resolution not available for this model"}

            else:
                return {"error": f"Unsupported asset type: {asset_type}"}

        except Exception as e:
            return {"error": f"Failed to download asset: {str(e)}"}

    def set_texture(self, object_name, texture_id):
        """Apply a previously downloaded Polyhaven texture to an object by creating a new material"""
        disabled = self._integration_disabled_error("blendermcp_use_polyhaven", "PolyHaven")
        if disabled:
            return disabled
        try:
            # Get the object
            obj = bpy.data.objects.get(object_name)
            if not obj:
                return {"error": f"Object not found: {object_name}"}

            # Make sure object can accept materials
            if not hasattr(obj, 'data') or not hasattr(obj.data, 'materials'):
                return {"error": f"Object {object_name} cannot accept materials"}

            # Find all images related to this texture and ensure they're properly loaded
            texture_images = {}
            for img in bpy.data.images:
                if img.name.startswith(texture_id + "_"):
                    # Extract the map type from the image name
                    map_type = img.name.split('_')[-1].split('.')[0]

                    # Force a reload of the image
                    img.reload()

                    # Ensure proper color space
                    if map_type.lower() in ['color', 'diffuse', 'albedo']:
                        try:
                            img.colorspace_settings.name = 'sRGB'
                        except:
                            pass
                    else:
                        try:
                            img.colorspace_settings.name = 'Non-Color'
                        except:
                            pass

                    # Ensure the image is packed
                    if not img.packed_file:
                        img.pack()

                    texture_images[map_type] = img
                    print(f"Loaded texture map: {map_type} - {img.name}")

                    # Debug info
                    print(f"Image size: {img.size[0]}x{img.size[1]}")
                    print(f"Color space: {img.colorspace_settings.name}")
                    print(f"File format: {img.file_format}")
                    print(f"Is packed: {bool(img.packed_file)}")

            if not texture_images:
                return {"error": f"No texture images found for: {texture_id}. Please download the texture first."}

            # Create a new material
            new_mat_name = f"{texture_id}_material_{object_name}"

            # Remove any existing material with this name to avoid conflicts
            existing_mat = bpy.data.materials.get(new_mat_name)
            if existing_mat:
                bpy.data.materials.remove(existing_mat)

            new_mat = bpy.data.materials.new(name=new_mat_name)
            new_mat.use_nodes = True

            # Set up the material nodes
            nodes = new_mat.node_tree.nodes
            links = new_mat.node_tree.links

            # Clear default nodes
            nodes.clear()

            # Create output node
            output = nodes.new(type='ShaderNodeOutputMaterial')
            output.location = (600, 0)

            # Create principled BSDF node
            principled = nodes.new(type='ShaderNodeBsdfPrincipled')
            principled.location = (300, 0)
            links.new(principled.outputs[0], output.inputs[0])

            # Add texture nodes based on available maps
            tex_coord = nodes.new(type='ShaderNodeTexCoord')
            tex_coord.location = (-800, 0)

            mapping = nodes.new(type='ShaderNodeMapping')
            mapping.location = (-600, 0)
            mapping.vector_type = 'TEXTURE'  # Changed from default 'POINT' to 'TEXTURE'
            links.new(tex_coord.outputs['UV'], mapping.inputs['Vector'])

            # Position offset for texture nodes
            x_pos = -400
            y_pos = 300

            # Connect different texture maps
            for map_type, image in texture_images.items():
                tex_node = nodes.new(type='ShaderNodeTexImage')
                tex_node.location = (x_pos, y_pos)
                tex_node.image = image

                # Set color space based on map type
                if map_type.lower() in ['color', 'diffuse', 'albedo']:
                    try:
                        tex_node.image.colorspace_settings.name = 'sRGB'
                    except:
                        pass  # Use default if sRGB not available
                else:
                    try:
                        tex_node.image.colorspace_settings.name = 'Non-Color'
                    except:
                        pass  # Use default if Non-Color not available

                links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])

                # Connect to appropriate input on Principled BSDF
                if map_type.lower() in ['color', 'diffuse', 'albedo']:
                    links.new(tex_node.outputs['Color'], principled.inputs['Base Color'])
                elif map_type.lower() in ['roughness', 'rough']:
                    links.new(tex_node.outputs['Color'], principled.inputs['Roughness'])
                elif map_type.lower() in ['metallic', 'metalness', 'metal']:
                    links.new(tex_node.outputs['Color'], principled.inputs['Metallic'])
                elif map_type.lower() in ['normal', 'nor', 'dx', 'gl']:
                    # Add normal map node
                    normal_map = nodes.new(type='ShaderNodeNormalMap')
                    normal_map.location = (x_pos + 200, y_pos)
                    links.new(tex_node.outputs['Color'], normal_map.inputs['Color'])
                    links.new(normal_map.outputs['Normal'], principled.inputs['Normal'])
                elif map_type.lower() in ['displacement', 'disp', 'height']:
                    # Add displacement node
                    disp_node = nodes.new(type='ShaderNodeDisplacement')
                    disp_node.location = (x_pos + 200, y_pos - 200)
                    disp_node.inputs['Scale'].default_value = 0.1  # Reduce displacement strength
                    links.new(tex_node.outputs['Color'], disp_node.inputs['Height'])
                    links.new(disp_node.outputs['Displacement'], output.inputs['Displacement'])

                y_pos -= 250

            # Second pass: Connect nodes with proper handling for special cases
            texture_nodes = {}

            # First find all texture nodes and store them by map type
            for node in nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    for map_type, image in texture_images.items():
                        if node.image == image:
                            texture_nodes[map_type] = node
                            break

            # Now connect everything using the nodes instead of images
            # Handle base color (diffuse)
            for map_name in ['color', 'diffuse', 'albedo']:
                if map_name in texture_nodes:
                    links.new(texture_nodes[map_name].outputs['Color'], principled.inputs['Base Color'])
                    print(f"Connected {map_name} to Base Color")
                    break

            # Handle roughness
            for map_name in ['roughness', 'rough']:
                if map_name in texture_nodes:
                    links.new(texture_nodes[map_name].outputs['Color'], principled.inputs['Roughness'])
                    print(f"Connected {map_name} to Roughness")
                    break

            # Handle metallic
            for map_name in ['metallic', 'metalness', 'metal']:
                if map_name in texture_nodes:
                    links.new(texture_nodes[map_name].outputs['Color'], principled.inputs['Metallic'])
                    print(f"Connected {map_name} to Metallic")
                    break

            # Handle normal maps
            for map_name in ['gl', 'dx', 'nor']:
                if map_name in texture_nodes:
                    normal_map_node = nodes.new(type='ShaderNodeNormalMap')
                    normal_map_node.location = (100, 100)
                    links.new(texture_nodes[map_name].outputs['Color'], normal_map_node.inputs['Color'])
                    links.new(normal_map_node.outputs['Normal'], principled.inputs['Normal'])
                    print(f"Connected {map_name} to Normal")
                    break

            # Handle displacement
            for map_name in ['displacement', 'disp', 'height']:
                if map_name in texture_nodes:
                    disp_node = nodes.new(type='ShaderNodeDisplacement')
                    disp_node.location = (300, -200)
                    disp_node.inputs['Scale'].default_value = 0.1  # Reduce displacement strength
                    links.new(texture_nodes[map_name].outputs['Color'], disp_node.inputs['Height'])
                    links.new(disp_node.outputs['Displacement'], output.inputs['Displacement'])
                    print(f"Connected {map_name} to Displacement")
                    break

            # Handle ARM texture (Ambient Occlusion, Roughness, Metallic)
            if 'arm' in texture_nodes:
                separate_rgb = nodes.new(type='ShaderNodeSeparateRGB')
                separate_rgb.location = (-200, -100)
                links.new(texture_nodes['arm'].outputs['Color'], separate_rgb.inputs['Image'])

                # Connect Roughness (G) if no dedicated roughness map
                if not any(map_name in texture_nodes for map_name in ['roughness', 'rough']):
                    links.new(separate_rgb.outputs['G'], principled.inputs['Roughness'])
                    print("Connected ARM.G to Roughness")

                # Connect Metallic (B) if no dedicated metallic map
                if not any(map_name in texture_nodes for map_name in ['metallic', 'metalness', 'metal']):
                    links.new(separate_rgb.outputs['B'], principled.inputs['Metallic'])
                    print("Connected ARM.B to Metallic")

                # For AO (R channel), multiply with base color if we have one
                base_color_node = None
                for map_name in ['color', 'diffuse', 'albedo']:
                    if map_name in texture_nodes:
                        base_color_node = texture_nodes[map_name]
                        break

                if base_color_node:
                    mix_node = nodes.new(type='ShaderNodeMixRGB')
                    mix_node.location = (100, 200)
                    mix_node.blend_type = 'MULTIPLY'
                    mix_node.inputs['Fac'].default_value = 0.8  # 80% influence

                    # Disconnect direct connection to base color
                    for link in base_color_node.outputs['Color'].links:
                        if link.to_socket == principled.inputs['Base Color']:
                            links.remove(link)

                    # Connect through the mix node
                    links.new(base_color_node.outputs['Color'], mix_node.inputs[1])
                    links.new(separate_rgb.outputs['R'], mix_node.inputs[2])
                    links.new(mix_node.outputs['Color'], principled.inputs['Base Color'])
                    print("Connected ARM.R to AO mix with Base Color")

            # Handle AO (Ambient Occlusion) if separate
            if 'ao' in texture_nodes:
                base_color_node = None
                for map_name in ['color', 'diffuse', 'albedo']:
                    if map_name in texture_nodes:
                        base_color_node = texture_nodes[map_name]
                        break

                if base_color_node:
                    mix_node = nodes.new(type='ShaderNodeMixRGB')
                    mix_node.location = (100, 200)
                    mix_node.blend_type = 'MULTIPLY'
                    mix_node.inputs['Fac'].default_value = 0.8  # 80% influence

                    # Disconnect direct connection to base color
                    for link in base_color_node.outputs['Color'].links:
                        if link.to_socket == principled.inputs['Base Color']:
                            links.remove(link)

                    # Connect through the mix node
                    links.new(base_color_node.outputs['Color'], mix_node.inputs[1])
                    links.new(texture_nodes['ao'].outputs['Color'], mix_node.inputs[2])
                    links.new(mix_node.outputs['Color'], principled.inputs['Base Color'])
                    print("Connected AO to mix with Base Color")

            # CRITICAL: Make sure to clear all existing materials from the object
            while len(obj.data.materials) > 0:
                obj.data.materials.pop(index=0)

            # Assign the new material to the object
            obj.data.materials.append(new_mat)

            # CRITICAL: Make the object active and select it
            bpy.context.view_layer.objects.active = obj
            obj.select_set(True)

            # CRITICAL: Force Blender to update the material
            bpy.context.view_layer.update()

            # Get the list of texture maps
            texture_maps = list(texture_images.keys())

            # Get info about texture nodes for debugging
            material_info = {
                "name": new_mat.name,
                "has_nodes": new_mat.use_nodes,
                "node_count": len(new_mat.node_tree.nodes),
                "texture_nodes": []
            }

            for node in new_mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    connections = []
                    for output in node.outputs:
                        for link in output.links:
                            connections.append(f"{output.name} → {link.to_node.name}.{link.to_socket.name}")

                    material_info["texture_nodes"].append({
                        "name": node.name,
                        "image": node.image.name,
                        "colorspace": node.image.colorspace_settings.name,
                        "connections": connections
                    })

            return {
                "success": True,
                "message": f"Created new material and applied texture {texture_id} to {object_name}",
                "material": new_mat.name,
                "maps": texture_maps,
                "material_info": material_info
            }

        except Exception as e:
            print(f"Error in set_texture: {str(e)}")
            traceback.print_exc()
            return {"error": f"Failed to apply texture: {str(e)}"}

    def get_telemetry_consent(self):
        """Get the current telemetry consent status"""
        try:
            # Get addon preferences - use the module name
            addon_prefs = bpy.context.preferences.addons.get(__name__)
            if addon_prefs:
                consent = addon_prefs.preferences.telemetry_consent
            else:
                # Fallback to default if preferences not available
                consent = True
        except (AttributeError, KeyError):
            # Fallback to default if preferences not available
            consent = True
        return {"consent": consent}

    def get_polyhaven_status(self):
        """Get the current status of PolyHaven integration"""
        enabled = bpy.context.scene.blendermcp_use_polyhaven
        if enabled:
            return {"enabled": True, "message": "PolyHaven integration is enabled and ready to use."}
        else:
            return {
                "enabled": False,
                "message": """PolyHaven integration is currently disabled. To enable it:
                            1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                            2. Check the 'Use assets from Poly Haven' checkbox
                            3. Restart the connection to Claude"""
        }

    #region Hyper3D
    def get_hyper3d_status(self):
        """Get the current status of Hyper3D Rodin integration"""
        enabled = bpy.context.scene.blendermcp_use_hyper3d
        hyper3d_api_key = self._get_hyper3d_api_key()
        if enabled:
            if not hyper3d_api_key:
                return {
                    "enabled": False,
                    "message": """Hyper3D Rodin integration is currently enabled, but API key is not given. To enable it:
                                1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                                2. Keep the 'Use Hyper3D Rodin 3D model generation' checkbox checked
                                3. Choose the right plaform and fill in the API Key
                                4. Restart the connection to Claude"""
                }
            mode = bpy.context.scene.blendermcp_hyper3d_mode
            message = f"Hyper3D Rodin integration is enabled and ready to use. Mode: {mode}. " + \
                f"Key type: {'private' if hyper3d_api_key != RODIN_FREE_TRIAL_KEY else 'free_trial'}"
            return {
                "enabled": True,
                "message": message
            }
        else:
            return {
                "enabled": False,
                "message": """Hyper3D Rodin integration is currently disabled. To enable it:
                            1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                            2. Check the 'Use Hyper3D Rodin 3D model generation' checkbox
                            3. Restart the connection to Claude"""
            }

    def create_rodin_job(self, *args, **kwargs):
        disabled = self._integration_disabled_error("blendermcp_use_hyper3d", "Hyper3D Rodin")
        if disabled:
            return disabled
        match bpy.context.scene.blendermcp_hyper3d_mode:
            case "MAIN_SITE":
                return self.create_rodin_job_main_site(*args, **kwargs)
            case "FAL_AI":
                return self.create_rodin_job_fal_ai(*args, **kwargs)
            case _:
                return f"Error: Unknown Hyper3D Rodin mode!"

    def create_rodin_job_main_site(
            self,
            text_prompt: str=None,
            images: list[tuple[str, str]]=None,
            bbox_condition=None
        ):
        try:
            api_key = self._get_hyper3d_api_key()
            if not api_key:
                return {"error": "Hyper3D API key is not given"}
            if images is None:
                images = []
            """Call Rodin API, get the job uuid and subscription key"""
            files = [
                *[("images", (f"{i:04d}{img_suffix}", base64.b64decode(img) if isinstance(img, str) else img)) for i, (img_suffix, img) in enumerate(images)],
                ("tier", (None, "Sketch")),
                ("mesh_mode", (None, "Raw")),
                ("texture_mode", (None, "high")),
            ]
            if text_prompt:
                files.append(("prompt", (None, text_prompt)))
            if bbox_condition:
                files.append(("bbox_condition", (None, json.dumps(bbox_condition))))
            response = requests.post(
                "https://hyperhuman.deemos.com/api/v2/rodin",
                headers={
                    "Authorization": f"Bearer {api_key}",
                },
                files=files,
                timeout=(10, 60)
            )
            data = response.json()
            return data
        except Exception as e:
            return {"error": str(e)}

    def create_rodin_job_fal_ai(
            self,
            text_prompt: str=None,
            images: list[tuple[str, str]]=None,
            bbox_condition=None
        ):
        try:
            api_key = self._get_hyper3d_api_key()
            if not api_key:
                return {"error": "Hyper3D API key is not given"}
            req_data = {
                "tier": "Sketch",
            }
            if images:
                req_data["input_image_urls"] = images
            if text_prompt:
                req_data["prompt"] = text_prompt
            if bbox_condition:
                req_data["bbox_condition"] = bbox_condition
            response = requests.post(
                "https://queue.fal.run/fal-ai/hyper3d/rodin",
                headers={
                    "Authorization": f"Key {api_key}",
                    "Content-Type": "application/json",
                },
                json=req_data,
                timeout=(10, 60)
            )
            data = response.json()
            return data
        except Exception as e:
            return {"error": str(e)}

    def poll_rodin_job_status(self, *args, **kwargs):
        disabled = self._integration_disabled_error("blendermcp_use_hyper3d", "Hyper3D Rodin")
        if disabled:
            return disabled
        match bpy.context.scene.blendermcp_hyper3d_mode:
            case "MAIN_SITE":
                return self.poll_rodin_job_status_main_site(*args, **kwargs)
            case "FAL_AI":
                return self.poll_rodin_job_status_fal_ai(*args, **kwargs)
            case _:
                return f"Error: Unknown Hyper3D Rodin mode!"

    def poll_rodin_job_status_main_site(self, subscription_key: str):
        """Call the job status API to get the job status"""
        api_key = self._get_hyper3d_api_key()
        if not api_key:
            return {"error": "Hyper3D API key is not given"}
        response = requests.post(
            "https://hyperhuman.deemos.com/api/v2/status",
            headers={
                "Authorization": f"Bearer {api_key}",
            },
            json={
                "subscription_key": subscription_key,
            },
            timeout=(10, 60),
        )
        data = response.json()
        return {
            "status_list": [i["status"] for i in data["jobs"]]
        }

    def poll_rodin_job_status_fal_ai(self, request_id: str):
        """Call the job status API to get the job status"""
        api_key = self._get_hyper3d_api_key()
        if not api_key:
            return {"error": "Hyper3D API key is not given"}
        response = requests.get(
            f"https://queue.fal.run/fal-ai/hyper3d/requests/{request_id}/status",
            headers={
                "Authorization": f"KEY {api_key}",
            },
            timeout=(10, 60),
        )
        data = response.json()
        return data

    @staticmethod
    def _clean_imported_glb(filepath, mesh_name=None):
        # Get the set of existing objects before import
        existing_objects = set(bpy.data.objects)

        # Import the GLB file
        bpy.ops.import_scene.gltf(filepath=filepath)

        # Ensure the context is updated
        bpy.context.view_layer.update()

        # Get all imported objects
        imported_objects = list(set(bpy.data.objects) - existing_objects)
        # imported_objects = [obj for obj in bpy.context.view_layer.objects if obj.select_get()]

        if not imported_objects:
            print("Error: No objects were imported.")
            return

        # Identify the mesh object
        mesh_obj = None

        if len(imported_objects) == 1 and imported_objects[0].type == 'MESH':
            mesh_obj = imported_objects[0]
            print("Single mesh imported, no cleanup needed.")
        else:
            if len(imported_objects) == 2:
                empty_objs = [i for i in imported_objects if i.type == "EMPTY"]
                if len(empty_objs) != 1:
                    print("Error: Expected an empty node with one mesh child or a single mesh object.")
                    return
                parent_obj = empty_objs.pop()
                if len(parent_obj.children) == 1:
                    potential_mesh = parent_obj.children[0]
                    if potential_mesh.type == 'MESH':
                        print("GLB structure confirmed: Empty node with one mesh child.")

                        # Unparent the mesh from the empty node
                        potential_mesh.parent = None

                        # Remove the empty node
                        bpy.data.objects.remove(parent_obj)
                        print("Removed empty node, keeping only the mesh.")

                        mesh_obj = potential_mesh
                    else:
                        print("Error: Child is not a mesh object.")
                        return
                else:
                    print("Error: Expected an empty node with one mesh child or a single mesh object.")
                    return
            else:
                print("Error: Expected an empty node with one mesh child or a single mesh object.")
                return

        # Rename the mesh if needed
        try:
            if mesh_obj and mesh_obj.name is not None and mesh_name:
                mesh_obj.name = mesh_name
                if mesh_obj.data.name is not None:
                    mesh_obj.data.name = mesh_name
                print(f"Mesh renamed to: {mesh_name}")
        except Exception as e:
            print("Having issue with renaming, give up renaming.")

        return mesh_obj

    def import_generated_asset(self, *args, **kwargs):
        disabled = self._integration_disabled_error("blendermcp_use_hyper3d", "Hyper3D Rodin")
        if disabled:
            return disabled
        match bpy.context.scene.blendermcp_hyper3d_mode:
            case "MAIN_SITE":
                return self.import_generated_asset_main_site(*args, **kwargs)
            case "FAL_AI":
                return self.import_generated_asset_fal_ai(*args, **kwargs)
            case _:
                return f"Error: Unknown Hyper3D Rodin mode!"

    def import_generated_asset_main_site(self, task_uuid: str, name: str):
        """Fetch the generated asset, import into blender"""
        api_key = self._get_hyper3d_api_key()
        if not api_key:
            return {"succeed": False, "error": "Hyper3D API key is not given"}
        response = requests.post(
            "https://hyperhuman.deemos.com/api/v2/download",
            headers={
                "Authorization": f"Bearer {api_key}",
            },
            json={
                'task_uuid': task_uuid
            },
            timeout=(10, 60)
        )
        data_ = response.json()
        temp_file = None
        for i in data_["list"]:
            if i["name"].endswith(".glb"):
                temp_file = tempfile.NamedTemporaryFile(
                    delete=False,
                    prefix=task_uuid,
                    suffix=".glb",
                )

                try:
                    # Download the content
                    response = requests.get(i["url"], stream=True, timeout=(10, 60))
                    response.raise_for_status()  # Raise an exception for HTTP errors

                    # Write the content to the temporary file
                    for chunk in response.iter_content(chunk_size=8192):
                        temp_file.write(chunk)

                    # Close the file
                    temp_file.close()

                except Exception as e:
                    # Clean up the file if there's an error
                    temp_file.close()
                    os.unlink(temp_file.name)
                    return {"succeed": False, "error": str(e)}

                break
        else:
            return {"succeed": False, "error": "Generation failed. Please first make sure that all jobs of the task are done and then try again later."}

        try:
            obj = self._clean_imported_glb(
                filepath=temp_file.name,
                mesh_name=name
            )
            result = {
                "name": obj.name,
                "type": obj.type,
                "location": [obj.location.x, obj.location.y, obj.location.z],
                "rotation": [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z],
                "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            }

            if obj.type == "MESH":
                bounding_box = self._get_aabb(obj)
                result["world_bounding_box"] = bounding_box

            return {
                "succeed": True, **result
            }
        except Exception as e:
            return {"succeed": False, "error": str(e)}

    def import_generated_asset_fal_ai(self, request_id: str, name: str):
        """Fetch the generated asset, import into blender"""
        api_key = self._get_hyper3d_api_key()
        if not api_key:
            return {"succeed": False, "error": "Hyper3D API key is not given"}
        response = requests.get(
            f"https://queue.fal.run/fal-ai/hyper3d/requests/{request_id}",
            headers={
                "Authorization": f"Key {api_key}",
            }
        )
        data_ = response.json()
        temp_file = None

        temp_file = tempfile.NamedTemporaryFile(
            delete=False,
            prefix=request_id,
            suffix=".glb",
        )

        try:
            # Download the content
            response = requests.get(data_["model_mesh"]["url"], stream=True, timeout=(10, 60))
            response.raise_for_status()  # Raise an exception for HTTP errors

            # Write the content to the temporary file
            for chunk in response.iter_content(chunk_size=8192):
                temp_file.write(chunk)

            # Close the file
            temp_file.close()

        except Exception as e:
            # Clean up the file if there's an error
            temp_file.close()
            os.unlink(temp_file.name)
            return {"succeed": False, "error": str(e)}

        try:
            obj = self._clean_imported_glb(
                filepath=temp_file.name,
                mesh_name=name
            )
            result = {
                "name": obj.name,
                "type": obj.type,
                "location": [obj.location.x, obj.location.y, obj.location.z],
                "rotation": [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z],
                "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            }

            if obj.type == "MESH":
                bounding_box = self._get_aabb(obj)
                result["world_bounding_box"] = bounding_box

            return {
                "succeed": True, **result
            }
        except Exception as e:
            return {"succeed": False, "error": str(e)}
    #endregion
 
    #region Sketchfab API
    def get_sketchfab_status(self):
        """Get the current status of Sketchfab integration"""
        enabled = bpy.context.scene.blendermcp_use_sketchfab
        api_key = self._get_sketchfab_api_key()

        # Test the API key if present
        if api_key:
            try:
                headers = {
                    "Authorization": f"Token {api_key}"
                }

                response = requests.get(
                    "https://api.sketchfab.com/v3/me",
                    headers=headers,
                    timeout=30  # Add timeout of 30 seconds
                )

                if response.status_code == 200:
                    user_data = response.json()
                    username = user_data.get("username", "Unknown user")
                    return {
                        "enabled": True,
                        "message": f"Sketchfab integration is enabled and ready to use. Logged in as: {username}"
                    }
                else:
                    return {
                        "enabled": False,
                        "message": f"Sketchfab API key seems invalid. Status code: {response.status_code}"
                    }
            except requests.exceptions.Timeout:
                return {
                    "enabled": False,
                    "message": "Timeout connecting to Sketchfab API. Check your internet connection."
                }
            except Exception as e:
                return {
                    "enabled": False,
                    "message": f"Error testing Sketchfab API key: {str(e)}"
                }

        if enabled and api_key:
            return {"enabled": True, "message": "Sketchfab integration is enabled and ready to use."}
        elif enabled and not api_key:
            return {
                "enabled": False,
                "message": """Sketchfab integration is currently enabled, but API key is not given. To enable it:
                            1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                            2. Keep the 'Use Sketchfab' checkbox checked
                            3. Enter your Sketchfab API Key
                            4. Restart the connection to Claude"""
            }
        else:
            return {
                "enabled": False,
                "message": """Sketchfab integration is currently disabled. To enable it:
                            1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                            2. Check the 'Use assets from Sketchfab' checkbox
                            3. Enter your Sketchfab API Key
                            4. Restart the connection to Claude"""
            }

    def search_sketchfab_models(self, query, categories=None, count=20, downloadable=True):
        """Search for models on Sketchfab based on query and optional filters"""
        disabled = self._integration_disabled_error("blendermcp_use_sketchfab", "Sketchfab")
        if disabled:
            return disabled
        try:
            api_key = self._get_sketchfab_api_key()
            if not api_key:
                return {"error": "Sketchfab API key is not configured"}

            # Build search parameters with exact fields from Sketchfab API docs
            params = {
                "type": "models",
                "q": query,
                "count": count,
                "downloadable": downloadable,
                "archives_flavours": False
            }

            if categories:
                params["categories"] = categories

            # Make API request to Sketchfab search endpoint
            # The proper format according to Sketchfab API docs for API key auth
            headers = {
                "Authorization": f"Token {api_key}"
            }


            # Use the search endpoint as specified in the API documentation
            response = requests.get(
                "https://api.sketchfab.com/v3/search",
                headers=headers,
                params=params,
                timeout=30  # Add timeout of 30 seconds
            )

            if response.status_code == 401:
                return {"error": "Authentication failed (401). Check your API key."}

            if response.status_code != 200:
                return {"error": f"API request failed with status code {response.status_code}"}

            response_data = response.json()

            # Safety check on the response structure
            if response_data is None:
                return {"error": "Received empty response from Sketchfab API"}

            # Handle 'results' potentially missing from response
            results = response_data.get("results", [])
            if not isinstance(results, list):
                return {"error": f"Unexpected response format from Sketchfab API: {response_data}"}

            return response_data

        except requests.exceptions.Timeout:
            return {"error": "Request timed out. Check your internet connection."}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON response from Sketchfab API: {str(e)}"}
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": str(e)}

    def get_sketchfab_model_preview(self, uid):
        """Get thumbnail preview image of a Sketchfab model by its UID"""
        disabled = self._integration_disabled_error("blendermcp_use_sketchfab", "Sketchfab")
        if disabled:
            return disabled
        try:
            import base64
            
            api_key = self._get_sketchfab_api_key()
            if not api_key:
                return {"error": "Sketchfab API key is not configured"}

            headers = {"Authorization": f"Token {api_key}"}
            
            # Get model info which includes thumbnails
            response = requests.get(
                f"https://api.sketchfab.com/v3/models/{uid}",
                headers=headers,
                timeout=30
            )
            
            if response.status_code == 401:
                return {"error": "Authentication failed (401). Check your API key."}
            
            if response.status_code == 404:
                return {"error": f"Model not found: {uid}"}
            
            if response.status_code != 200:
                return {"error": f"Failed to get model info: {response.status_code}"}
            
            data = response.json()
            thumbnails = data.get("thumbnails", {}).get("images", [])
            
            if not thumbnails:
                return {"error": "No thumbnail available for this model"}
            
            # Find a suitable thumbnail (prefer medium size ~640px)
            selected_thumbnail = None
            for thumb in thumbnails:
                width = thumb.get("width", 0)
                if 400 <= width <= 800:
                    selected_thumbnail = thumb
                    break
            
            # Fallback to the first available thumbnail
            if not selected_thumbnail:
                selected_thumbnail = thumbnails[0]
            
            thumbnail_url = selected_thumbnail.get("url")
            if not thumbnail_url:
                return {"error": "Thumbnail URL not found"}
            
            # Download the thumbnail image
            img_response = requests.get(thumbnail_url, timeout=30)
            if img_response.status_code != 200:
                return {"error": f"Failed to download thumbnail: {img_response.status_code}"}
            
            # Encode image as base64
            image_data = base64.b64encode(img_response.content).decode('ascii')
            
            # Determine format from content type or URL
            content_type = img_response.headers.get("Content-Type", "")
            if "png" in content_type or thumbnail_url.endswith(".png"):
                img_format = "png"
            else:
                img_format = "jpeg"
            
            # Get additional model info for context
            model_name = data.get("name", "Unknown")
            author = data.get("user", {}).get("username", "Unknown")
            
            return {
                "success": True,
                "image_data": image_data,
                "format": img_format,
                "model_name": model_name,
                "author": author,
                "uid": uid,
                "thumbnail_width": selected_thumbnail.get("width"),
                "thumbnail_height": selected_thumbnail.get("height")
            }
            
        except requests.exceptions.Timeout:
            return {"error": "Request timed out. Check your internet connection."}
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": f"Failed to get model preview: {str(e)}"}

    def download_sketchfab_model(self, uid, normalize_size=False, target_size=1.0):
        """Download a model from Sketchfab by its UID
        
        Parameters:
        - uid: The unique identifier of the Sketchfab model
        - normalize_size: If True, scale the model so its largest dimension equals target_size
        - target_size: The target size in Blender units (meters) for the largest dimension
        """
        disabled = self._integration_disabled_error("blendermcp_use_sketchfab", "Sketchfab")
        if disabled:
            return disabled
        try:
            api_key = self._get_sketchfab_api_key()
            if not api_key:
                return {"error": "Sketchfab API key is not configured"}

            # Use proper authorization header for API key auth
            headers = {
                "Authorization": f"Token {api_key}"
            }

            # Request download URL using the exact endpoint from the documentation
            download_endpoint = f"https://api.sketchfab.com/v3/models/{uid}/download"

            response = requests.get(
                download_endpoint,
                headers=headers,
                timeout=30  # Add timeout of 30 seconds
            )

            if response.status_code == 401:
                return {"error": "Authentication failed (401). Check your API key."}

            if response.status_code != 200:
                return {"error": f"Download request failed with status code {response.status_code}"}

            data = response.json()

            # Safety check for None data
            if data is None:
                return {"error": "Received empty response from Sketchfab API for download request"}

            # Extract download URL with safety checks
            gltf_data = data.get("gltf")
            if not gltf_data:
                return {"error": "No gltf download URL available for this model. Response: " + str(data)}

            download_url = gltf_data.get("url")
            if not download_url:
                return {"error": "No download URL available for this model. Make sure the model is downloadable and you have access."}

            # Download the model (already has timeout)
            model_response = requests.get(download_url, timeout=60)  # 60 second timeout

            if model_response.status_code != 200:
                return {"error": f"Model download failed with status code {model_response.status_code}"}

            # Save to temporary file
            temp_dir = tempfile.mkdtemp()
            zip_file_path = os.path.join(temp_dir, f"{uid}.zip")

            with open(zip_file_path, "wb") as f:
                f.write(model_response.content)

            # Extract the zip file with enhanced security
            with zipfile.ZipFile(zip_file_path, 'r') as zip_ref:
                # More secure zip slip prevention
                for file_info in zip_ref.infolist():
                    # Get the path of the file
                    file_path = file_info.filename

                    # Convert directory separators to the current OS style
                    # This handles both / and \ in zip entries
                    target_path = os.path.join(temp_dir, os.path.normpath(file_path))

                    # Get absolute paths for comparison
                    abs_temp_dir = os.path.abspath(temp_dir)
                    abs_target_path = os.path.abspath(target_path)

                    # Ensure the normalized path doesn't escape the target directory
                    if not abs_target_path.startswith(abs_temp_dir):
                        with suppress(Exception):
                            shutil.rmtree(temp_dir)
                        return {"error": "Security issue: Zip contains files with path traversal attempt"}

                    # Additional explicit check for directory traversal
                    if ".." in file_path:
                        with suppress(Exception):
                            shutil.rmtree(temp_dir)
                        return {"error": "Security issue: Zip contains files with directory traversal sequence"}

                # If all files passed security checks, extract them
                zip_ref.extractall(temp_dir)

            # Find the main glTF file
            gltf_files = [f for f in os.listdir(temp_dir) if f.endswith('.gltf') or f.endswith('.glb')]

            if not gltf_files:
                with suppress(Exception):
                    shutil.rmtree(temp_dir)
                return {"error": "No glTF file found in the downloaded model"}

            main_file = os.path.join(temp_dir, gltf_files[0])

            # Import the model
            bpy.ops.import_scene.gltf(filepath=main_file)

            # Get the imported objects
            imported_objects = list(bpy.context.selected_objects)
            imported_object_names = [obj.name for obj in imported_objects]

            # Clean up temporary files
            with suppress(Exception):
                shutil.rmtree(temp_dir)

            # Find root objects (objects without parents in the imported set)
            root_objects = [obj for obj in imported_objects if obj.parent is None]

            # Helper function to recursively get all mesh children
            def get_all_mesh_children(obj):
                """Recursively collect all mesh objects in the hierarchy"""
                meshes = []
                if obj.type == 'MESH':
                    meshes.append(obj)
                for child in obj.children:
                    meshes.extend(get_all_mesh_children(child))
                return meshes

            # Collect ALL meshes from the entire hierarchy (starting from roots)
            all_meshes = []
            for obj in root_objects:
                all_meshes.extend(get_all_mesh_children(obj))
            
            if all_meshes:
                # Calculate combined world bounding box for all meshes
                all_min = mathutils.Vector((float('inf'), float('inf'), float('inf')))
                all_max = mathutils.Vector((float('-inf'), float('-inf'), float('-inf')))
                
                for mesh_obj in all_meshes:
                    # Get world-space bounding box corners
                    for corner in mesh_obj.bound_box:
                        world_corner = mesh_obj.matrix_world @ mathutils.Vector(corner)
                        all_min.x = min(all_min.x, world_corner.x)
                        all_min.y = min(all_min.y, world_corner.y)
                        all_min.z = min(all_min.z, world_corner.z)
                        all_max.x = max(all_max.x, world_corner.x)
                        all_max.y = max(all_max.y, world_corner.y)
                        all_max.z = max(all_max.z, world_corner.z)
                
                # Calculate dimensions
                dimensions = [
                    all_max.x - all_min.x,
                    all_max.y - all_min.y,
                    all_max.z - all_min.z
                ]
                max_dimension = max(dimensions)
                
                # Apply normalization if requested
                scale_applied = 1.0
                if normalize_size and max_dimension > 0:
                    scale_factor = target_size / max_dimension
                    scale_applied = scale_factor
                    
                    # ✅ Only apply scale to ROOT objects (not children!)
                    # Child objects inherit parent's scale through matrix_world
                    for root in root_objects:
                        root.scale = (
                            root.scale.x * scale_factor,
                            root.scale.y * scale_factor,
                            root.scale.z * scale_factor
                        )
                    
                    # Update the scene to recalculate matrix_world for all objects
                    bpy.context.view_layer.update()
                    
                    # Recalculate bounding box after scaling
                    all_min = mathutils.Vector((float('inf'), float('inf'), float('inf')))
                    all_max = mathutils.Vector((float('-inf'), float('-inf'), float('-inf')))
                    
                    for mesh_obj in all_meshes:
                        for corner in mesh_obj.bound_box:
                            world_corner = mesh_obj.matrix_world @ mathutils.Vector(corner)
                            all_min.x = min(all_min.x, world_corner.x)
                            all_min.y = min(all_min.y, world_corner.y)
                            all_min.z = min(all_min.z, world_corner.z)
                            all_max.x = max(all_max.x, world_corner.x)
                            all_max.y = max(all_max.y, world_corner.y)
                            all_max.z = max(all_max.z, world_corner.z)
                    
                    dimensions = [
                        all_max.x - all_min.x,
                        all_max.y - all_min.y,
                        all_max.z - all_min.z
                    ]
                
                world_bounding_box = [[all_min.x, all_min.y, all_min.z], [all_max.x, all_max.y, all_max.z]]
            else:
                world_bounding_box = None
                dimensions = None
                scale_applied = 1.0

            result = {
                "success": True,
                "message": "Model imported successfully",
                "imported_objects": imported_object_names
            }
            
            if world_bounding_box:
                result["world_bounding_box"] = world_bounding_box
            if dimensions:
                result["dimensions"] = [round(d, 4) for d in dimensions]
            if normalize_size:
                result["scale_applied"] = round(scale_applied, 6)
                result["normalized"] = True
            
            return result

        except requests.exceptions.Timeout:
            return {"error": "Request timed out. Check your internet connection and try again with a simpler model."}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON response from Sketchfab API: {str(e)}"}
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": f"Failed to download model: {str(e)}"}
    #endregion

    #region Hunyuan3D
    def get_hunyuan3d_status(self):
        """Get the current status of Hunyuan3D integration"""
        enabled = bpy.context.scene.blendermcp_use_hunyuan3d
        hunyuan3d_mode = bpy.context.scene.blendermcp_hunyuan3d_mode
        secret_id = self._get_hunyuan3d_secret_id()
        secret_key = self._get_hunyuan3d_secret_key()
        api_url = self._get_hunyuan3d_api_url()
        if enabled:
            match hunyuan3d_mode:
                case "OFFICIAL_API":
                    if not secret_id or not secret_key:
                        return {
                            "enabled": False, 
                            "mode": hunyuan3d_mode, 
                            "message": """Hunyuan3D integration is currently enabled, but SecretId or SecretKey is not given. To enable it:
                                1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                                2. Keep the 'Use Tencent Hunyuan 3D model generation' checkbox checked
                                3. Choose the right platform and fill in the SecretId and SecretKey
                                4. Restart the connection to Claude"""
                        }
                case "LOCAL_API":
                    if not api_url:
                        return {
                            "enabled": False, 
                            "mode": hunyuan3d_mode, 
                            "message": """Hunyuan3D integration is currently enabled, but API URL  is not given. To enable it:
                                1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                                2. Keep the 'Use Tencent Hunyuan 3D model generation' checkbox checked
                                3. Choose the right platform and fill in the API URL
                                4. Restart the connection to Claude"""
                        }
                case _:
                    return {
                        "enabled": False, 
                        "message": "Hunyuan3D integration is enabled and mode is not supported."
                    }
            return {
                "enabled": True, 
                "mode": hunyuan3d_mode,
                "message": "Hunyuan3D integration is enabled and ready to use."
            }
        return {
            "enabled": False, 
            "message": """Hunyuan3D integration is currently disabled. To enable it:
                        1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                        2. Check the 'Use Tencent Hunyuan 3D model generation' checkbox
                        3. Restart the connection to Claude"""
        }
    
    @staticmethod
    def get_tencent_cloud_sign_headers(
        method: str,
        path: str,
        headParams: dict,
        data: dict,
        service: str,
        region: str,
        secret_id: str,
        secret_key: str,
        host: str = None
    ):
        """Generate the signature header required for Tencent Cloud API requests headers"""
        # Generate timestamp
        timestamp = int(time.time())
        date = datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d")
        
        # If host is not provided, it is generated based on service and region.
        if not host:
            host = f"{service}.tencentcloudapi.com"
        
        endpoint = f"https://{host}"
        
        # Constructing the request body
        payload_str = json.dumps(data)
        
        # ************* Step 1: Concatenate the canonical request string *************
        canonical_uri = path
        canonical_querystring = ""
        ct = "application/json; charset=utf-8"
        canonical_headers = f"content-type:{ct}\nhost:{host}\nx-tc-action:{headParams.get('Action', '').lower()}\n"
        signed_headers = "content-type;host;x-tc-action"
        hashed_request_payload = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()
        
        canonical_request = (method + "\n" +
                            canonical_uri + "\n" +
                            canonical_querystring + "\n" +
                            canonical_headers + "\n" +
                            signed_headers + "\n" +
                            hashed_request_payload)

        # ************* Step 2: Construct the reception signature string *************
        credential_scope = f"{date}/{service}/tc3_request"
        hashed_canonical_request = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        string_to_sign = ("TC3-HMAC-SHA256" + "\n" +
                        str(timestamp) + "\n" +
                        credential_scope + "\n" +
                        hashed_canonical_request)

        # ************* Step 3: Calculate the signature *************
        def sign(key, msg):
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        secret_date = sign(("TC3" + secret_key).encode("utf-8"), date)
        secret_service = sign(secret_date, service)
        secret_signing = sign(secret_service, "tc3_request")
        signature = hmac.new(
            secret_signing, 
            string_to_sign.encode("utf-8"), 
            hashlib.sha256
        ).hexdigest()

        # ************* Step 4: Connect Authorization *************
        authorization = ("TC3-HMAC-SHA256" + " " +
                        "Credential=" + secret_id + "/" + credential_scope + ", " +
                        "SignedHeaders=" + signed_headers + ", " +
                        "Signature=" + signature)

        # Constructing request headers
        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json; charset=utf-8",
            "Host": host,
            "X-TC-Action": headParams.get("Action", ""),
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": headParams.get("Version", ""),
            "X-TC-Region": region
        }

        return headers, endpoint

    def create_hunyuan_job(self, *args, **kwargs):
        disabled = self._integration_disabled_error("blendermcp_use_hunyuan3d", "Hunyuan3D")
        if disabled:
            return disabled
        match bpy.context.scene.blendermcp_hunyuan3d_mode:
            case "OFFICIAL_API":
                return self.create_hunyuan_job_main_site(*args, **kwargs)
            case "LOCAL_API":
                return self.create_hunyuan_job_local_site(*args, **kwargs)
            case _:
                return f"Error: Unknown Hunyuan3D mode!"

    def create_hunyuan_job_main_site(
        self,
        text_prompt: str = None,
        image: str = None
    ):
        try:
            secret_id = self._get_hunyuan3d_secret_id()
            secret_key = self._get_hunyuan3d_secret_key()

            if not secret_id or not secret_key:
                return {"error": "SecretId or SecretKey is not given"}

            # Parameter verification
            if not text_prompt and not image:
                return {"error": "Prompt or Image is required"}
            if text_prompt and image:
                return {"error": "Prompt and Image cannot be provided simultaneously"}
            # Fixed parameter configuration
            service = "hunyuan"
            action = "SubmitHunyuanTo3DJob"
            version = "2023-09-01"
            region = "ap-guangzhou"

            headParams={
                "Action": action,
                "Version": version,
                "Region": region,
            }

            # Constructing request parameters
            data = {
                "Num": 1  # The current API limit is only 1
            }

            # Handling text prompts
            if text_prompt:
                if len(text_prompt) > 200:
                    return {"error": "Prompt exceeds 200 characters limit"}
                data["Prompt"] = text_prompt

            # Handling image
            if image:
                if re.match(r'^https?://', image, re.IGNORECASE) is not None:
                    data["ImageUrl"] = image
                else:
                    try:
                        # Convert to Base64 format
                        with open(image, "rb") as f:
                            image_base64 = base64.b64encode(f.read()).decode("ascii")
                        data["ImageBase64"] = image_base64
                    except Exception as e:
                        return {"error": f"Image encoding failed: {str(e)}"}
            
            # Get signed headers
            headers, endpoint = self.get_tencent_cloud_sign_headers("POST", "/", headParams, data, service, region, secret_id, secret_key)

            response = requests.post(
                endpoint,
                headers = headers,
                data = json.dumps(data),
                timeout=(10, 60)
            )

            if response.status_code == 200:
                return response.json()
            return {
                "error": f"API request failed with status {response.status_code}: {response}"
            }
        except Exception as e:
            return {"error": str(e)}

    def create_hunyuan_job_local_site(
        self,
        text_prompt: str = None,
        image: str = None):
        try:
            base_url = self._get_hunyuan3d_api_url().rstrip('/')
            octree_resolution = bpy.context.scene.blendermcp_hunyuan3d_octree_resolution
            num_inference_steps = bpy.context.scene.blendermcp_hunyuan3d_num_inference_steps
            guidance_scale = bpy.context.scene.blendermcp_hunyuan3d_guidance_scale
            texture = bpy.context.scene.blendermcp_hunyuan3d_texture

            if not base_url:
                return {"error": "API URL is not given"}
            # Parameter verification
            if not text_prompt and not image:
                return {"error": "Prompt or Image is required"}

            # Constructing request parameters
            data = {
                "octree_resolution": octree_resolution,
                "num_inference_steps": num_inference_steps,
                "guidance_scale": guidance_scale,
                "texture": texture,
            }

            # Handling text prompts
            if text_prompt:
                data["text"] = text_prompt

            # Handling image
            if image:
                if re.match(r'^https?://', image, re.IGNORECASE) is not None:
                    try:
                        resImg = requests.get(image, timeout=(10, 60))
                        resImg.raise_for_status()
                        image_base64 = base64.b64encode(resImg.content).decode("ascii")
                        data["image"] = image_base64
                    except Exception as e:
                        return {"error": f"Failed to download or encode image: {str(e)}"} 
                else:
                    try:
                        # Convert to Base64 format
                        with open(image, "rb") as f:
                            image_base64 = base64.b64encode(f.read()).decode("ascii")
                        data["image"] = image_base64
                    except Exception as e:
                        return {"error": f"Image encoding failed: {str(e)}"}

            response = requests.post(
                f"{base_url}/generate",
                json = data,
                timeout=(10, 60),
            )

            if response.status_code != 200:
                return {
                    "error": f"Generation failed: {response.text}"
                }
        
            # Decode base64 and save to temporary file
            with tempfile.NamedTemporaryFile(delete=False, suffix=".glb") as temp_file:
                temp_file.write(response.content)
                temp_file_name = temp_file.name

            # Import the GLB file in the main thread
            def import_handler():
                bpy.ops.import_scene.gltf(filepath=temp_file_name)
                os.unlink(temp_file.name)
                return None
            
            bpy.app.timers.register(import_handler)

            return {
                "status": "DONE",
                "message": "Generation and Import glb succeeded"
            }
        except Exception as e:
            print(f"An error occurred: {e}")
            return {"error": str(e)}
        
    
    def poll_hunyuan_job_status(self, *args, **kwargs):
        disabled = self._integration_disabled_error("blendermcp_use_hunyuan3d", "Hunyuan3D")
        if disabled:
            return disabled
        return self.poll_hunyuan_job_status_ai(*args, **kwargs)
    
    def poll_hunyuan_job_status_ai(self, job_id: str):
        """Call the job status API to get the job status"""
        print(job_id)
        try:
            secret_id = self._get_hunyuan3d_secret_id()
            secret_key = self._get_hunyuan3d_secret_key()

            if not secret_id or not secret_key:
                return {"error": "SecretId or SecretKey is not given"}
            if not job_id:
                return {"error": "JobId is required"}
            
            service = "hunyuan"
            action = "QueryHunyuanTo3DJob"
            version = "2023-09-01"
            region = "ap-guangzhou"

            headParams={
                "Action": action,
                "Version": version,
                "Region": region,
            }

            clean_job_id = job_id.removeprefix("job_")
            data = {
                "JobId": clean_job_id
            }

            headers, endpoint = self.get_tencent_cloud_sign_headers("POST", "/", headParams, data, service, region, secret_id, secret_key)

            response = requests.post(
                endpoint,
                headers=headers,
                data=json.dumps(data),
                timeout=(10, 60)
            )

            if response.status_code == 200:
                return response.json()
            return {
                "error": f"API request failed with status {response.status_code}: {response}"
            }
        except Exception as e:
            return {"error": str(e)}

    def import_generated_asset_hunyuan(self, *args, **kwargs):
        disabled = self._integration_disabled_error("blendermcp_use_hunyuan3d", "Hunyuan3D")
        if disabled:
            return disabled
        return self.import_generated_asset_hunyuan_ai(*args, **kwargs)
            
    def import_generated_asset_hunyuan_ai(self, name: str , zip_file_url: str):
        if not zip_file_url:
            return {"error": "Zip file not found"}
        
        # Validate URL
        if not re.match(r'^https?://', zip_file_url, re.IGNORECASE):
            return {"error": "Invalid URL format. Must start with http:// or https://"}
        
        # Create a temporary directory
        temp_dir = tempfile.mkdtemp(prefix="tencent_obj_")
        zip_file_path = osp.join(temp_dir, "model.zip")
        obj_file_path = osp.join(temp_dir, "model.obj")
        mtl_file_path = osp.join(temp_dir, "model.mtl")

        try:
            # Download ZIP file
            zip_response = requests.get(zip_file_url, stream=True, timeout=(10, 60))
            zip_response.raise_for_status()
            with open(zip_file_path, "wb") as f:
                for chunk in zip_response.iter_content(chunk_size=8192):
                    f.write(chunk)

            # Unzip the ZIP
            with zipfile.ZipFile(zip_file_path, "r") as zip_ref:
                zip_ref.extractall(temp_dir)

            # Find the .obj file (there may be multiple, assuming the main file is model.obj)
            for file in os.listdir(temp_dir):
                if file.endswith(".obj"):
                    obj_file_path = osp.join(temp_dir, file)

            if not osp.exists(obj_file_path):
                return {"succeed": False, "error": "OBJ file not found after extraction"}

            # Import obj file
            if bpy.app.version>=(4, 0, 0):
                bpy.ops.wm.obj_import(filepath=obj_file_path)
            else:
                bpy.ops.import_scene.obj(filepath=obj_file_path)

            imported_objs = [obj for obj in bpy.context.selected_objects if obj.type == 'MESH']
            if not imported_objs:
                return {"succeed": False, "error": "No mesh objects imported"}

            obj = imported_objs[0]
            if name:
                obj.name = name

            result = {
                "name": obj.name,
                "type": obj.type,
                "location": [obj.location.x, obj.location.y, obj.location.z],
                "rotation": [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z],
                "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            }

            if obj.type == "MESH":
                bounding_box = self._get_aabb(obj)
                result["world_bounding_box"] = bounding_box

            return {"succeed": True, **result}
        except Exception as e:
            return {"succeed": False, "error": str(e)}
        finally:
            #  Clean up temporary zip and obj, save texture and mtl
            try:
                if os.path.exists(zip_file_path):
                    os.remove(zip_file_path) 
                if os.path.exists(obj_file_path):
                    os.remove(obj_file_path)
            except Exception as e:
                print(f"Failed to clean up temporary directory {temp_dir}: {e}")
    #endregion

# Blender Addon Preferences
class BLENDERMCP_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__
    
    telemetry_consent: BoolProperty(
        name="Allow Telemetry",
        description="Allow collection of prompts, code snippets, and screenshots to help improve Blender MCP",
        default=True
    )
    auto_check_updates: BoolProperty(
        name="Check for updates on startup",
        description="Check GitHub for a newer BlenderMCP version once per Blender session (a few seconds after startup)",
        default=True
    )
    auto_version_on_session_end: BoolProperty(
        name="Auto-save version when an AI session ends",
        description="When an MCP client disconnects after modifying a saved .blend, "
                    "automatically write a numbered snapshot to the versions folder",
        default=True
    )
    hyper3d_api_key: bpy.props.StringProperty(
        name="Hyper3D API Key",
        subtype="PASSWORD",
        description="Persistent Hyper3D API Key",
        default=""
    )
    sketchfab_api_key: bpy.props.StringProperty(
        name="Sketchfab API Key",
        subtype="PASSWORD",
        description="Persistent Sketchfab API Key",
        default=""
    )
    hunyuan3d_secret_id: bpy.props.StringProperty(
        name="Hunyuan3D SecretId",
        description="Persistent Hunyuan3D SecretId",
        default=""
    )
    hunyuan3d_secret_key: bpy.props.StringProperty(
        name="Hunyuan3D SecretKey",
        subtype="PASSWORD",
        description="Persistent Hunyuan3D SecretKey",
        default=""
    )
    hunyuan3d_api_url: bpy.props.StringProperty(
        name="Hunyuan3D API URL",
        description="Persistent Hunyuan3D API URL",
        default=""
    )

    sketchfab_api_key: StringProperty(
        name="Sketchfab API Key",
        subtype='PASSWORD',
        description="API Key provided by Sketchfab (stored in Blender preferences, not in .blend files)",
        default=""
    )

    hyper3d_api_key: StringProperty(
        name="Hyper3D API Key",
        subtype='PASSWORD',
        description="API Key provided by Hyper3D (stored in Blender preferences, not in .blend files)",
        default=""
    )

    hunyuan3d_secret_id: StringProperty(
        name="Hunyuan 3D SecretId",
        subtype='PASSWORD',
        description="SecretId provided by Hunyuan 3D (stored in Blender preferences, not in .blend files)",
        default=""
    )

    hunyuan3d_secret_key: StringProperty(
        name="Hunyuan 3D SecretKey",
        subtype='PASSWORD',
        description="SecretKey provided by Hunyuan 3D (stored in Blender preferences, not in .blend files)",
        default=""
    )

    def draw(self, context):
        layout = self.layout

        # API keys section
        layout.label(text="API Keys:", icon='LOCKED')
        box = layout.box()
        box.prop(self, "sketchfab_api_key")
        box.prop(self, "hyper3d_api_key")
        box.prop(self, "hunyuan3d_secret_id")
        box.prop(self, "hunyuan3d_secret_key")
        box.label(text="Keys are stored in Blender preferences, not in .blend files", icon='INFO')

        # Telemetry section
        layout.label(text="Telemetry & Privacy:", icon='PREFERENCES')
        
        box = layout.box()
        row = box.row()
        row.prop(self, "telemetry_consent", text="Allow Telemetry")
        
        # Info text
        box.separator()
        if self.telemetry_consent:
            box.label(text="With consent: We collect anonymized prompts, code, and screenshots.", icon='INFO')
        else:
            box.label(text="Without consent: We only collect minimal anonymous usage data", icon='INFO')
            box.label(text="(tool names, success/failure, duration - no prompts or code).", icon='BLANK1')
        box.separator()
        box.label(text="All data is fully anonymized. You can change this anytime.", icon='CHECKMARK')
        
        # Terms and Conditions link
        box.separator()
        row = box.row()
        row.operator("blendermcp.open_terms", text="View Terms and Conditions", icon='TEXT')

        # Sessions section
        layout.separator()
        layout.label(text="Sessions:", icon='FILE_BLEND')
        box = layout.box()
        box.prop(self, "auto_version_on_session_end")
        box.label(text="Snapshots go to the 'versions' folder next to the saved .blend",
                  icon='INFO')

        # Updates section
        layout.separator()
        layout.label(text="Updates:", icon='FILE_REFRESH')
        box = layout.box()
        box.prop(self, "auto_check_updates")
        row = box.row()
        row.operator("blendermcp.check_updates", text="Check for Updates", icon='FILE_REFRESH')
        if _UPDATE_INFO["update_available"] and _UPDATE_INFO["latest"]:
            row.operator("blendermcp.open_update_page", text="Get Update", icon='URL')

        layout.separator()
        layout.label(text="Persistent API Credentials:", icon='LOCKED')
        cred_box = layout.box()
        cred_box.prop(self, "sketchfab_api_key", text="Sketchfab API Key")
        cred_box.prop(self, "hyper3d_api_key", text="Hyper3D API Key")
        cred_box.prop(self, "hunyuan3d_secret_id", text="Hunyuan3D SecretId")
        cred_box.prop(self, "hunyuan3d_secret_key", text="Hunyuan3D SecretKey")
        cred_box.prop(self, "hunyuan3d_api_url", text="Hunyuan3D API URL")

# Blender UI Panel
class BLENDERMCP_PT_Panel(bpy.types.Panel):
    bl_label = "Blender MCP"
    bl_idname = "BLENDERMCP_PT_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'BlenderMCP'

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        server = getattr(bpy.types, "blendermcp_server", None)
        addon_entry = context.preferences.addons.get(__name__)
        prefs = addon_entry.preferences if addon_entry else None
        paused = getattr(scene, "blendermcp_paused", False)

        # --- Status box ---
        box = layout.box()
        if server and server.running:
            # Truthful port: read from the server object, not the scene prop
            box.label(text=f"Server running on port {server.port}", icon='CHECKMARK')
            if paused:
                box.label(text="Paused - commands are rejected", icon='PAUSE')
            if server.client_connected and server.client_address:
                box.label(text=f"Client connected: {server.client_address}", icon='LINKED')
                if getattr(server, "legacy_client", False):
                    row = box.row()
                    row.alert = True
                    row.label(
                        text="Legacy/unknown MCP server connected — update server config (see README)",
                        icon='ERROR')
            else:
                box.label(text="Waiting for client...", icon='UNLINKED')
            if server.commands_executed:
                last = server.last_command_type or "-"
                age = ""
                if server.last_command_time:
                    age = f" ({int(time.time() - server.last_command_time)}s ago)"
                box.label(text=f"Commands: {server.commands_executed} · Last: {last}{age}", icon='INFO')
                box.label(text=f"Session: {server.commands_executed} cmds · ~{server.bytes_sent // 4 // 1000}k tok", icon='TIME')
            if server.last_error:
                row = box.row()
                row.alert = True
                err_cmd = server.last_error.get("command") or "?"
                err_msg = str(server.last_error.get("message") or "")[:64]
                row.label(text=f"{err_cmd}: {err_msg}", icon='ERROR')
                row.operator("blendermcp.dismiss_error", text="", icon='X')
        else:
            box.label(text="Server not running", icon='CANCEL')

        # Server<->addon version skew (reported via the set_client_info handshake)
        if server and getattr(server, "client_version", None) and \
                server.client_version != ADDON_VERSION:
            row = box.row()
            row.alert = True
            row.label(
                text=f"Server v{server.client_version} ≠ addon v{ADDON_VERSION} — update addon.py or the server",
                icon='ERROR')

        # Addon update notice (filled in by the async update check)
        if _UPDATE_INFO["update_available"] and _UPDATE_INFO["latest"]:
            row = box.row()
            row.alert = True
            row.label(
                text=f"Update available: v{_UPDATE_INFO['latest']} (installed v{ADDON_VERSION})",
                icon='IMPORT')
            row.operator("blendermcp.open_update_page", text="Get Update")

        # --- Controls row ---
        row = layout.row(align=True)
        if not scene.blendermcp_server_running:
            row.operator("blendermcp.start_server", text="Connect", icon='PLAY')
        else:
            row.operator("blendermcp.stop_server", text="Disconnect", icon='X')
        row.prop(scene, "blendermcp_paused",
                 text="Resume" if paused else "Pause",
                 toggle=True,
                 icon='PLAY' if paused else 'PAUSE')
        layout.operator("blendermcp.undo_last_ai", text="Undo AI Action", icon='LOOP_BACK')

        # --- Port row (locked while running) ---
        row = layout.row(align=True)
        sub = row.row(align=True)
        sub.enabled = not scene.blendermcp_server_running
        sub.prop(scene, "blendermcp_port")
        row.prop(scene, "blendermcp_auto_start_server", text="Auto-start")

        # --- Activity log box ---
        box = layout.box()
        box.prop(scene, "blendermcp_show_activity",
                 text="Activity",
                 icon='TRIA_DOWN' if scene.blendermcp_show_activity else 'TRIA_RIGHT',
                 emboss=False)
        if scene.blendermcp_show_activity:
            if server and server.activity_log:
                entries = list(server.activity_log)[-8:]
                for entry in reversed(entries):
                    icon = 'CHECKMARK' if entry.get("status") == "ok" else 'ERROR'
                    text = f"[{entry.get('time')}] {entry.get('type')} ({entry.get('duration_ms')}ms)"
                    summary = entry.get("summary") or ""
                    if summary and summary != entry.get("type"):
                        text += f" — {summary}"
                    box.label(text=text[:120], icon=icon)
                box.operator("blendermcp.dump_log", text="Copy Log to Text Editor", icon='TEXT')
            else:
                box.label(text="No activity yet", icon='INFO')

        # --- Output box (human-facing save / version / render controls) ---
        box = layout.box()
        box.label(text="Output", icon='OUTPUT')
        row = box.row(align=True)
        row.operator("blendermcp.save_now", text="Save", icon='FILE_TICK')
        row.operator("blendermcp.save_version", text="Save Version", icon='DUPLICATE')
        row = box.row(align=True)
        row.prop(scene, "blendermcp_render_preset", text="")
        row.operator("blendermcp.render_clip", text="Render Clip", icon='RENDER_ANIMATION')
        row.operator("blendermcp.render_still", text="Render Still", icon='RENDER_STILL')
        if _RENDER_JOB.get("active"):
            row = box.row()
            cur = _RENDER_JOB.get("frame_current")
            end = _RENDER_JOB.get("frame_end")
            row.label(text=f"Rendering frame {cur} / {end}", icon='RENDER_ANIMATION')
            row.operator("blendermcp.cancel_render", text="Cancel", icon='X')
        elif _LAST_RENDER_PATH:
            row = box.row()
            shown = _LAST_RENDER_PATH
            if len(shown) > 48:
                shown = "..." + shown[-45:]
            row.label(text=shown, icon='CHECKMARK')
            row.operator("blendermcp.open_render_folder", text="", icon='FILE_FOLDER')

        # --- Integrations box (keys come from addon preferences) ---
        box = layout.box()
        box.label(text="Integrations", icon='WORLD')

        box.prop(scene, "blendermcp_use_polyhaven", text="Use assets from Poly Haven")

        box.prop(scene, "blendermcp_use_hyper3d", text="Use Hyper3D Rodin 3D model generation")
        if scene.blendermcp_use_hyper3d:
            box.prop(scene, "blendermcp_hyper3d_mode", text="Rodin Mode")
            if prefs is not None:
                box.prop(prefs, "hyper3d_api_key", text="API Key")
            box.operator("blendermcp.set_hyper3d_free_trial_api_key", text="Set Free Trial API Key")

        box.prop(scene, "blendermcp_use_sketchfab", text="Use assets from Sketchfab")
        if scene.blendermcp_use_sketchfab and prefs is not None:
            box.prop(prefs, "sketchfab_api_key", text="API Key")

        box.prop(scene, "blendermcp_use_hunyuan3d", text="Use Tencent Hunyuan 3D model generation")
        if scene.blendermcp_use_hunyuan3d:
            box.prop(scene, "blendermcp_hunyuan3d_mode", text="Hunyuan3D Mode")
            if scene.blendermcp_hunyuan3d_mode == 'OFFICIAL_API' and prefs is not None:
                box.prop(prefs, "hunyuan3d_secret_id", text="SecretId")
                box.prop(prefs, "hunyuan3d_secret_key", text="SecretKey")
            if scene.blendermcp_hunyuan3d_mode == 'LOCAL_API':
                box.prop(scene, "blendermcp_hunyuan3d_api_url", text="API URL")
                box.prop(scene, "blendermcp_hunyuan3d_octree_resolution", text="Octree Resolution")
                box.prop(scene, "blendermcp_hunyuan3d_num_inference_steps", text="Number of Inference Steps")
                box.prop(scene, "blendermcp_hunyuan3d_guidance_scale", text="Guidance Scale")
                box.prop(scene, "blendermcp_hunyuan3d_texture", text="Generate Texture")

        box.label(text="Keys are stored in Blender preferences, not in .blend files", icon='INFO')

        # --- Telemetry row ---
        row = layout.row()
        consent = bool(prefs.telemetry_consent) if prefs is not None else True
        row.label(text=f"Telemetry: {'On' if consent else 'Off'}",
                  icon='RADIOBUT_ON' if consent else 'RADIOBUT_OFF')
        row.operator("blendermcp.check_updates", text="", icon='FILE_REFRESH')
        row.operator("blendermcp.open_prefs", text="", icon='PREFERENCES')

# Operator to set Hyper3D API Key
class BLENDERMCP_OT_SetFreeTrialHyper3DAPIKey(bpy.types.Operator):
    bl_idname = "blendermcp.set_hyper3d_free_trial_api_key"
    bl_label = "Set Free Trial API Key"

    def execute(self, context):
        addon_entry = context.preferences.addons.get(__name__)
        prefs = addon_entry.preferences if addon_entry else None
        if prefs:
            if not prefs.hyper3d_api_key or prefs.hyper3d_api_key == RODIN_FREE_TRIAL_KEY:
                prefs.hyper3d_api_key = RODIN_FREE_TRIAL_KEY
            else:
                self.report(
                    {'INFO'},
                    "Using free trial for this session only; saved private key was kept."
                )
        context.scene.blendermcp_hyper3d_api_key = RODIN_FREE_TRIAL_KEY
        context.scene.blendermcp_hyper3d_mode = 'MAIN_SITE'
        self.report({'INFO'}, "API Key set successfully!")
        return {'FINISHED'}

# Operator to start the server
class BLENDERMCP_OT_StartServer(bpy.types.Operator):
    bl_idname = "blendermcp.start_server"
    bl_label = "Connect to Claude"
    bl_description = "Start the BlenderMCP server to connect with Claude"

    def execute(self, context):
        scene = context.scene

        # Create a new server instance
        if not hasattr(bpy.types, "blendermcp_server") or not bpy.types.blendermcp_server:
            bpy.types.blendermcp_server = BlenderMCPServer(port=scene.blendermcp_port)

        # Start the server
        bpy.types.blendermcp_server.start()
        scene.blendermcp_server_running = bpy.types.blendermcp_server.running

        return {'FINISHED'}

# Operator to stop the server
class BLENDERMCP_OT_StopServer(bpy.types.Operator):
    bl_idname = "blendermcp.stop_server"
    bl_label = "Stop the connection to Claude"
    bl_description = "Stop the connection to Claude"

    def execute(self, context):
        scene = context.scene

        # Stop the server if it exists
        if hasattr(bpy.types, "blendermcp_server") and bpy.types.blendermcp_server:
            bpy.types.blendermcp_server.stop()
            del bpy.types.blendermcp_server

        scene.blendermcp_server_running = False

        return {'FINISHED'}

# Operator to open Terms and Conditions
class BLENDERMCP_OT_OpenTerms(bpy.types.Operator):
    bl_idname = "blendermcp.open_terms"
    bl_label = "View Terms and Conditions"
    bl_description = "Open the Terms and Conditions document"

    def execute(self, context):
        # Open the Terms and Conditions on GitHub
        terms_url = "https://github.com/ahujasid/blender-mcp/blob/main/TERMS_AND_CONDITIONS.md"
        try:
            import webbrowser
            webbrowser.open(terms_url)
            self.report({'INFO'}, "Terms and Conditions opened in browser")
        except Exception as e:
            self.report({'ERROR'}, f"Could not open Terms and Conditions: {str(e)}")
        
        return {'FINISHED'}

# Operator to check GitHub for a newer addon version (async, never blocks)
class BLENDERMCP_OT_CheckUpdates(bpy.types.Operator):
    bl_idname = "blendermcp.check_updates"
    bl_label = "Check for Updates"
    bl_description = "Check GitHub for a newer version of BlenderMCP (runs in the background)"

    def execute(self, context):
        if _UPDATE_INFO["checking"]:
            self.report({'INFO'}, "Update check already in progress")
            return {'FINISHED'}
        _check_for_updates_async()
        self.report({'INFO'}, "Checking for updates in the background...")
        return {'FINISHED'}

# Operator to open the update/download page on GitHub
class BLENDERMCP_OT_OpenUpdatePage(bpy.types.Operator):
    bl_idname = "blendermcp.open_update_page"
    bl_label = "Get Update"
    bl_description = "Open the BlenderMCP GitHub page to download the latest version"

    def execute(self, context):
        try:
            import webbrowser
            webbrowser.open(UPDATE_PAGE_URL)
        except Exception as e:
            self.report({'ERROR'}, f"Could not open update page: {str(e)}")
            return {'CANCELLED'}
        return {'FINISHED'}

# Operator to dismiss the last error shown in the panel status box
class BLENDERMCP_OT_DismissError(bpy.types.Operator):
    bl_idname = "blendermcp.dismiss_error"
    bl_label = "Dismiss Error"
    bl_description = "Clear the last MCP error shown in the panel"

    @classmethod
    def poll(cls, context):
        server = getattr(bpy.types, "blendermcp_server", None)
        return server is not None and server.last_error is not None

    def execute(self, context):
        server = getattr(bpy.types, "blendermcp_server", None)
        if server is not None:
            server.last_error = None
        return {'FINISHED'}

# Operator to undo the last AI (MCP) action
class BLENDERMCP_OT_UndoLastAI(bpy.types.Operator):
    bl_idname = "blendermcp.undo_last_ai"
    bl_label = "Undo AI Action"
    bl_description = "Undo the last change made via MCP (an undo checkpoint is pushed before every mutating command)"

    @classmethod
    def poll(cls, context):
        return getattr(bpy.types, "blendermcp_server", None) is not None

    def execute(self, context):
        try:
            # Push a step for the current state first so a single undo lands on
            # the pre-command checkpoint (ed.undo restores the step before the
            # active one) instead of also reverting the previous command.
            try:
                bpy.ops.ed.undo_push(message="MCP: undo AI action")
            except Exception:
                pass
            bpy.ops.ed.undo()
        except Exception as e:
            self.report({'ERROR'}, f"Undo failed: {str(e)}")
            return {'CANCELLED'}
        self.report({'INFO'}, "Undid last AI action")
        return {'FINISHED'}

# Operator to dump the full activity log to a text datablock
class BLENDERMCP_OT_DumpLog(bpy.types.Operator):
    bl_idname = "blendermcp.dump_log"
    bl_label = "Copy Log to Text Editor"
    bl_description = "Write the full MCP activity log to the 'MCP_Activity_Log' text datablock"

    @classmethod
    def poll(cls, context):
        server = getattr(bpy.types, "blendermcp_server", None)
        return server is not None and len(server.activity_log) > 0

    def execute(self, context):
        server = getattr(bpy.types, "blendermcp_server", None)
        if server is None:
            return {'CANCELLED'}
        text = bpy.data.texts.get("MCP_Activity_Log")
        if text is None:
            text = bpy.data.texts.new("MCP_Activity_Log")
        text.clear()
        lines = [f"BlenderMCP activity log ({time.strftime('%Y-%m-%d %H:%M:%S')})"]
        for entry in server.activity_log:
            mark = "OK " if entry.get("status") == "ok" else "ERR"
            lines.append(
                f"[{entry.get('time')}] {mark} {entry.get('type')} "
                f"({entry.get('duration_ms')}ms) - {entry.get('summary') or ''}"
            )
        text.write("\n".join(lines) + "\n")
        self.report({'INFO'}, "Activity log written to text datablock 'MCP_Activity_Log'")
        return {'FINISHED'}

# Operator to open the addon preferences (telemetry + API keys)
class BLENDERMCP_OT_OpenPrefs(bpy.types.Operator):
    bl_idname = "blendermcp.open_prefs"
    bl_label = "Open BlenderMCP Preferences"
    bl_description = "Open the BlenderMCP addon preferences (telemetry and API keys)"

    def execute(self, context):
        try:
            bpy.ops.preferences.addon_show(module=__name__)
        except Exception as e:
            self.report({'ERROR'}, f"Could not open preferences: {str(e)}")
            return {'CANCELLED'}
        return {'FINISHED'}

# --- Output box operators: human-facing save/version/render controls ------

# Operator to save the project (opens Save As for never-saved files)
class BLENDERMCP_OT_SaveNow(bpy.types.Operator):
    bl_idname = "blendermcp.save_now"
    bl_label = "Save Project"
    bl_description = "Save the .blend file (opens the Save As dialog if the file has never been saved)"

    def execute(self, context):
        had_path = bool(bpy.data.filepath)
        try:
            result = bpy.ops.wm.save_mainfile('INVOKE_DEFAULT')
        except Exception as e:
            self.report({'ERROR'}, f"Save failed: {str(e)}")
            return {'CANCELLED'}
        if 'FINISHED' in result and bpy.data.filepath:
            self.report({'INFO'}, f"Saved: {bpy.data.filepath}")
        elif not had_path:
            # Unsaved file: the Save As file browser is now open
            self.report({'INFO'}, "Choose a location in the Save As dialog")
        return {'FINISHED'}

# Operator to write a numbered version snapshot next to the saved .blend
class BLENDERMCP_OT_SaveVersion(bpy.types.Operator):
    bl_idname = "blendermcp.save_version"
    bl_label = "Save Version Snapshot"
    bl_description = "Write the next numbered snapshot copy to the versions folder next to the .blend (the working file stays open)"

    @classmethod
    def poll(cls, context):
        return bool(bpy.data.filepath)

    def execute(self, context):
        try:
            path = _save_version_snapshot()
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Version saved: {path}")
        return {'FINISHED'}

# Operator to render the current frame through the scene camera
class BLENDERMCP_OT_RenderStill(bpy.types.Operator):
    bl_idname = "blendermcp.render_still"
    bl_label = "Render Still"
    bl_description = "Render the current frame through the scene camera (current render settings) to the render folder next to the .blend"

    @classmethod
    def poll(cls, context):
        return context.scene.camera is not None and not _RENDER_JOB.get("active")

    def execute(self, context):
        global _LAST_RENDER_PATH
        scene = context.scene
        out_path = os.path.join(
            _panel_render_dir(),
            f"{_panel_render_stem()}_f{scene.frame_current}.png",
        )
        snap = BlenderMCPServer._snapshot_render_settings()
        try:
            scene.render.image_settings.file_format = 'PNG'
            scene.render.filepath = out_path
            bpy.ops.render.render(write_still=True)
        except Exception as e:
            self.report({'ERROR'}, f"Render failed: {str(e)}")
            return {'CANCELLED'}
        finally:
            BlenderMCPServer._restore_render_settings(snap)
        if not os.path.exists(out_path):
            self.report({'ERROR'}, f"Render finished but no file was written to {out_path}")
            return {'CANCELLED'}
        _LAST_RENDER_PATH = out_path
        self.report({'INFO'}, f"Rendered: {out_path}")
        return {'FINISHED'}

# Operator to render the full animation to an MP4 (async, tracked in the panel)
class BLENDERMCP_OT_RenderClip(bpy.types.Operator):
    bl_idname = "blendermcp.render_clip"
    bl_label = "Render Clip"
    bl_description = "Render the full animation to an MP4 in the render folder next to the .blend (runs in the background; progress shows in this panel)"

    @classmethod
    def poll(cls, context):
        return not _RENDER_JOB.get("active") and not bpy.app.background

    def execute(self, context):
        scene = context.scene
        preset = getattr(scene, "blendermcp_render_preset", "SCENE")
        filepath = os.path.join(
            _panel_render_dir(),
            f"{_panel_render_stem()}_{preset}.mp4",
        )
        snap = BlenderMCPServer._snapshot_vse_render_settings()
        try:
            if preset != "SCENE":
                res, fps_val = BlenderMCPServer._resolve_delivery(preset=preset)
                BlenderMCPServer._apply_delivery(res, fps_val)
            BlenderMCPServer._apply_ffmpeg_output_settings("MPEG4")
            scene.render.filepath = filepath
        except Exception as e:
            BlenderMCPServer._restore_vse_render_settings(snap)
            self.report({'ERROR'}, f"Could not set up the render: {str(e)}")
            return {'CANCELLED'}

        _ensure_render_handlers()
        _RENDER_JOB.update({
            "active": True,
            "frame_current": scene.frame_start,
            "frame_end": scene.frame_end,
            "filepath": filepath,
            "done": False,
            "cancelled": False,
            "error": None,
            "started_at": time.time(),
        })
        _RENDER_JOB_RESTORE.clear()
        _RENDER_JOB_RESTORE.update(snap)
        try:
            bpy.ops.render.render('INVOKE_DEFAULT', animation=True)
        except Exception as e:
            _RENDER_JOB["active"] = False
            _RENDER_JOB["error"] = str(e)
            _RENDER_JOB_RESTORE.clear()
            BlenderMCPServer._restore_vse_render_settings(snap)
            self.report({'ERROR'}, f"Render failed to start: {str(e)}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Rendering clip to {filepath}")
        return {'FINISHED'}

# Operator to cancel an active clip render (as far as the render API allows)
class BLENDERMCP_OT_CancelRender(bpy.types.Operator):
    bl_idname = "blendermcp.cancel_render"
    bl_label = "Cancel Render"
    bl_description = "Cancel the active clip render. Blender's Python API cannot abort a render mid-frame - press Esc in the render window to stop it"

    @classmethod
    def poll(cls, context):
        return bool(_RENDER_JOB.get("active"))

    def execute(self, context):
        # There is no Python API to abort a running render job: the render
        # window's Esc key is the only hard stop. Cleanup (settings restore,
        # job state) happens in the render_cancel handler when it fires.
        self.report(
            {'WARNING'},
            "Rendering can't be stopped from a script mid-frame - press Esc "
            "in the render window to cancel."
        )
        return {'FINISHED'}

# Operator to open the folder containing the last panel render output
class BLENDERMCP_OT_OpenRenderFolder(bpy.types.Operator):
    bl_idname = "blendermcp.open_render_folder"
    bl_label = "Open Render Folder"
    bl_description = "Open the folder containing the last rendered output"

    @classmethod
    def poll(cls, context):
        return bool(_LAST_RENDER_PATH)

    def execute(self, context):
        folder = os.path.dirname(_LAST_RENDER_PATH or "")
        if not folder or not os.path.isdir(folder):
            self.report({'ERROR'}, f"Folder not found: {folder}")
            return {'CANCELLED'}
        try:
            if hasattr(os, "startfile"):  # Windows
                os.startfile(folder)
            else:
                import subprocess
                import sys as _sys
                opener = "open" if _sys.platform == "darwin" else "xdg-open"
                subprocess.Popen([opener, folder])
        except Exception as e:
            self.report({'ERROR'}, f"Could not open folder: {str(e)}")
            return {'CANCELLED'}
        return {'FINISHED'}

# Registration functions
def register():
    bpy.types.Scene.blendermcp_port = IntProperty(
        name="Port",
        description="Port for the BlenderMCP server",
        default=9876,
        min=1024,
        max=65535
    )

    bpy.types.Scene.blendermcp_server_running = bpy.props.BoolProperty(
        name="Server Running",
        default=False
    )

    bpy.types.Scene.blendermcp_auto_start_server = bpy.props.BoolProperty(
        name="Auto-Start Server",
        description="Automatically start the MCP server when Blender loads",
        default=True
    )

    bpy.types.Scene.blendermcp_paused = bpy.props.BoolProperty(
        name="Pause MCP",
        description="Pause execution of MCP commands (status commands still work)",
        default=False
    )

    bpy.types.Scene.blendermcp_show_activity = bpy.props.BoolProperty(
        name="Show Activity",
        description="Show the recent MCP command activity log in the panel",
        default=True
    )

    bpy.types.Scene.blendermcp_render_preset = bpy.props.EnumProperty(
        name="Delivery Preset",
        description="Output preset for the panel's Render Clip button",
        items=[
            ("SCENE", "Scene settings", "Keep the scene's current resolution and frame rate"),
            ("LINKEDIN_WIDE", "LinkedIn 16:9", "1920x1080 @ 25 fps"),
            ("SQUARE", "Square 1:1", "1080x1080 @ 25 fps"),
            ("VERTICAL", "Vertical 9:16", "1080x1920 @ 25 fps"),
        ],
        default="SCENE"
    )

    bpy.types.Scene.blendermcp_use_polyhaven = bpy.props.BoolProperty(
        name="Use Poly Haven",
        description="Enable Poly Haven asset integration",
        default=False
    )

    bpy.types.Scene.blendermcp_use_hyper3d = bpy.props.BoolProperty(
        name="Use Hyper3D Rodin",
        description="Enable Hyper3D Rodin generatino integration",
        default=False
    )

    bpy.types.Scene.blendermcp_hyper3d_mode = bpy.props.EnumProperty(
        name="Rodin Mode",
        description="Choose the platform used to call Rodin APIs",
        items=[
            ("MAIN_SITE", "hyper3d.ai", "hyper3d.ai"),
            ("FAL_AI", "fal.ai", "fal.ai"),
        ],
        default="MAIN_SITE"
    )

    bpy.types.Scene.blendermcp_hyper3d_api_key = bpy.props.StringProperty(
        name="Hyper3D API Key",
        subtype="PASSWORD",
        description="API Key provided by Hyper3D",
        default=""
    )

    bpy.types.Scene.blendermcp_use_hunyuan3d = bpy.props.BoolProperty(
        name="Use Hunyuan 3D",
        description="Enable Hunyuan asset integration",
        default=False
    )

    bpy.types.Scene.blendermcp_hunyuan3d_mode = bpy.props.EnumProperty(
        name="Hunyuan3D Mode",
        description="Choose a local or official APIs",
        items=[
            ("LOCAL_API", "local api", "local api"),
            ("OFFICIAL_API", "official api", "official api"),
        ],
        default="LOCAL_API"
    )

    bpy.types.Scene.blendermcp_hunyuan3d_secret_id = bpy.props.StringProperty(
        name="Hunyuan 3D SecretId",
        description="SecretId provided by Hunyuan 3D",
        default=""
    )

    bpy.types.Scene.blendermcp_hunyuan3d_secret_key = bpy.props.StringProperty(
        name="Hunyuan 3D SecretKey",
        subtype="PASSWORD",
        description="SecretKey provided by Hunyuan 3D",
        default=""
    )

    bpy.types.Scene.blendermcp_hunyuan3d_api_url = bpy.props.StringProperty(
        name="API URL",
        description="URL of the Hunyuan 3D API service",
        default="http://localhost:8081"
    )

    bpy.types.Scene.blendermcp_hunyuan3d_octree_resolution = bpy.props.IntProperty(
        name="Octree Resolution",
        description="Octree resolution for the 3D generation",
        default=256,
        min=128,
        max=512,
    )

    bpy.types.Scene.blendermcp_hunyuan3d_num_inference_steps = bpy.props.IntProperty(
        name="Number of Inference Steps",
        description="Number of inference steps for the 3D generation",
        default=20,
        min=20,
        max=50,
    )

    bpy.types.Scene.blendermcp_hunyuan3d_guidance_scale = bpy.props.FloatProperty(
        name="Guidance Scale",
        description="Guidance scale for the 3D generation",
        default=5.5,
        min=1.0,
        max=10.0,
    )

    bpy.types.Scene.blendermcp_hunyuan3d_texture = bpy.props.BoolProperty(
        name="Generate Texture",
        description="Whether to generate texture for the 3D model",
        default=False,
    )
    
    bpy.types.Scene.blendermcp_use_sketchfab = bpy.props.BoolProperty(
        name="Use Sketchfab",
        description="Enable Sketchfab asset integration",
        default=False
    )

    bpy.types.Scene.blendermcp_sketchfab_api_key = bpy.props.StringProperty(
        name="Sketchfab API Key",
        subtype="PASSWORD",
        description="API Key provided by Sketchfab",
        default=""
    )

    # Register preferences class
    bpy.utils.register_class(BLENDERMCP_AddonPreferences)

    bpy.utils.register_class(BLENDERMCP_PT_Panel)
    bpy.utils.register_class(BLENDERMCP_OT_SetFreeTrialHyper3DAPIKey)
    bpy.utils.register_class(BLENDERMCP_OT_StartServer)
    bpy.utils.register_class(BLENDERMCP_OT_StopServer)
    bpy.utils.register_class(BLENDERMCP_OT_OpenTerms)
    bpy.utils.register_class(BLENDERMCP_OT_DismissError)
    bpy.utils.register_class(BLENDERMCP_OT_UndoLastAI)
    bpy.utils.register_class(BLENDERMCP_OT_DumpLog)
    bpy.utils.register_class(BLENDERMCP_OT_OpenPrefs)
    bpy.utils.register_class(BLENDERMCP_OT_CheckUpdates)
    bpy.utils.register_class(BLENDERMCP_OT_OpenUpdatePage)
    bpy.utils.register_class(BLENDERMCP_OT_SaveNow)
    bpy.utils.register_class(BLENDERMCP_OT_SaveVersion)
    bpy.utils.register_class(BLENDERMCP_OT_RenderStill)
    bpy.utils.register_class(BLENDERMCP_OT_RenderClip)
    bpy.utils.register_class(BLENDERMCP_OT_CancelRender)
    bpy.utils.register_class(BLENDERMCP_OT_OpenRenderFolder)

    # Keep the .assignment.md sidecar current on every save (plain Ctrl+S too).
    # Guard against double-append on addon reload: the function object changes
    # between loads, so match by name before appending.
    for h in list(bpy.app.handlers.save_post):
        if getattr(h, "__name__", "") == "_blendermcp_save_post":
            bpy.app.handlers.save_post.remove(h)
    bpy.app.handlers.save_post.append(_blendermcp_save_post)

    # Re-sync per-file panel state after any file load (same double-append
    # guard: match by name, the function object changes between addon loads)
    for h in list(bpy.app.handlers.load_post):
        if getattr(h, "__name__", "") == "_blendermcp_load_post":
            bpy.app.handlers.load_post.remove(h)
    bpy.app.handlers.load_post.append(_blendermcp_load_post)

    # Auto-start the server so the MCP client can connect without manual UI interaction
    scene = getattr(bpy.context, 'scene', None)
    if scene is not None:
        port = scene.blendermcp_port
        auto_start = scene.blendermcp_auto_start_server
    else:
        port = 9876
        auto_start = True

    if auto_start and (not hasattr(bpy.types, "blendermcp_server") or not bpy.types.blendermcp_server):
        bpy.types.blendermcp_server = BlenderMCPServer(port=port)
    if auto_start and not bpy.types.blendermcp_server.running:
        bpy.types.blendermcp_server.start()
        try:
            bpy.context.scene.blendermcp_server_running = bpy.types.blendermcp_server.running
        except AttributeError:
            pass

    # Deferred startup update check: once per Blender session, never in
    # background mode (headless/CI), never synchronously in register().
    # The auto_check_updates preference is read inside the timer callback so
    # the addon preferences entry is guaranteed to exist by then.
    global _UPDATE_CHECK_SCHEDULED
    if not bpy.app.background and not _UPDATE_CHECK_SCHEDULED:
        _UPDATE_CHECK_SCHEDULED = True

        def _deferred_update_check():
            try:
                addon_entry = bpy.context.preferences.addons.get(__name__)
                auto_check = True
                if addon_entry is not None:
                    auto_check = bool(getattr(addon_entry.preferences,
                                              "auto_check_updates", True))
                if auto_check and not _UPDATE_INFO["checked"] and not _UPDATE_INFO["checking"]:
                    _check_for_updates_async()
            except Exception:
                pass
            return None

        try:
            bpy.app.timers.register(_deferred_update_check, first_interval=3.0)
        except Exception:
            pass

    print("BlenderMCP addon registered")

def unregister():
    # Stop the server if it's running
    if hasattr(bpy.types, "blendermcp_server") and bpy.types.blendermcp_server:
        bpy.types.blendermcp_server.stop()
        del bpy.types.blendermcp_server

    # Remove the assignment sidecar save handler (match by name - the function
    # object may belong to an earlier load of the addon module)
    for h in list(bpy.app.handlers.save_post):
        if getattr(h, "__name__", "") == "_blendermcp_save_post":
            bpy.app.handlers.save_post.remove(h)

    # Remove the file-load state resync handler (same by-name matching)
    for h in list(bpy.app.handlers.load_post):
        if getattr(h, "__name__", "") == "_blendermcp_load_post":
            bpy.app.handlers.load_post.remove(h)

    bpy.utils.unregister_class(BLENDERMCP_PT_Panel)
    bpy.utils.unregister_class(BLENDERMCP_OT_SetFreeTrialHyper3DAPIKey)
    bpy.utils.unregister_class(BLENDERMCP_OT_StartServer)
    bpy.utils.unregister_class(BLENDERMCP_OT_StopServer)
    bpy.utils.unregister_class(BLENDERMCP_OT_OpenTerms)
    bpy.utils.unregister_class(BLENDERMCP_OT_DismissError)
    bpy.utils.unregister_class(BLENDERMCP_OT_UndoLastAI)
    bpy.utils.unregister_class(BLENDERMCP_OT_DumpLog)
    bpy.utils.unregister_class(BLENDERMCP_OT_OpenPrefs)
    bpy.utils.unregister_class(BLENDERMCP_OT_CheckUpdates)
    bpy.utils.unregister_class(BLENDERMCP_OT_OpenUpdatePage)
    bpy.utils.unregister_class(BLENDERMCP_OT_SaveNow)
    bpy.utils.unregister_class(BLENDERMCP_OT_SaveVersion)
    bpy.utils.unregister_class(BLENDERMCP_OT_RenderStill)
    bpy.utils.unregister_class(BLENDERMCP_OT_RenderClip)
    bpy.utils.unregister_class(BLENDERMCP_OT_CancelRender)
    bpy.utils.unregister_class(BLENDERMCP_OT_OpenRenderFolder)
    bpy.utils.unregister_class(BLENDERMCP_AddonPreferences)

    del bpy.types.Scene.blendermcp_port
    del bpy.types.Scene.blendermcp_server_running
    del bpy.types.Scene.blendermcp_auto_start_server
    del bpy.types.Scene.blendermcp_paused
    del bpy.types.Scene.blendermcp_show_activity
    del bpy.types.Scene.blendermcp_render_preset
    del bpy.types.Scene.blendermcp_use_polyhaven
    del bpy.types.Scene.blendermcp_use_hyper3d
    del bpy.types.Scene.blendermcp_hyper3d_mode
    del bpy.types.Scene.blendermcp_hyper3d_api_key
    del bpy.types.Scene.blendermcp_use_sketchfab
    del bpy.types.Scene.blendermcp_sketchfab_api_key
    del bpy.types.Scene.blendermcp_use_hunyuan3d
    del bpy.types.Scene.blendermcp_hunyuan3d_mode
    del bpy.types.Scene.blendermcp_hunyuan3d_secret_id
    del bpy.types.Scene.blendermcp_hunyuan3d_secret_key
    del bpy.types.Scene.blendermcp_hunyuan3d_api_url
    del bpy.types.Scene.blendermcp_hunyuan3d_octree_resolution
    del bpy.types.Scene.blendermcp_hunyuan3d_num_inference_steps
    del bpy.types.Scene.blendermcp_hunyuan3d_guidance_scale
    del bpy.types.Scene.blendermcp_hunyuan3d_texture

    print("BlenderMCP addon unregistered")

if __name__ == "__main__":
    register()
