# Headless smoke test for the blender-mcp v1.7 addon.
#
# Run with:
#   "C:/Program Files/Blender Foundation/Blender 5.1/blender.exe" \
#       --background --factory-startup --python tests/smoke_test.py
#
# Loads addon.py as a module, registers it, and calls
# BlenderMCPServer._execute_command_internal directly (no sockets).
# Integration/network commands and get_viewport_screenshot are NOT tested
# (network access / real window required).

import importlib.util
import os
import re
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta

import bpy

ADDON_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "addon.py"
)

failures = []
passed = 0


def check(name, cond, detail=""):
    global passed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failures.append(f"{name}: {detail}")
        print(f"  FAIL {name}: {detail}")


def load_addon():
    spec = importlib.util.spec_from_file_location("blendermcp_addon", ADDON_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["blendermcp_addon"] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    print(f"Loading addon from {ADDON_PATH}")
    mod = load_addon()
    mod.register()

    # register() may auto-start the TCP server; stop it — this test is socket-free.
    try:
        if hasattr(bpy.types, "blendermcp_server") and bpy.types.blendermcp_server:
            bpy.types.blendermcp_server.stop()
    except Exception:
        pass

    server = mod.BlenderMCPServer(port=9876)  # never started

    def run(cmd_type, expect_success=True, **params):
        """Dispatch a command; return the result dict (or full response on error)."""
        resp = server._execute_command_internal({"type": cmd_type, "params": params})
        if expect_success:
            check(
                f"{cmd_type} status",
                isinstance(resp, dict) and resp.get("status") == "success",
                f"response={str(resp)[:300]}",
            )
        return resp.get("result", {}) if isinstance(resp, dict) else {}

    tmpdir = tempfile.mkdtemp(prefix="blendermcp_smoke_")

    # --- version consistency (VERSION / pyproject.toml / addon) ----------
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(repo_root, "VERSION"), encoding="utf-8") as f:
        version_file = f.read().strip()
    with open(os.path.join(repo_root, "pyproject.toml"), encoding="utf-8") as f:
        m = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.MULTILINE)
    pyproject_version = m.group(1) if m else None
    bl_info_version = ".".join(str(n) for n in mod.bl_info.get("version", ()))
    check("version consistency",
          version_file == pyproject_version == mod.ADDON_VERSION == bl_info_version,
          f"VERSION={version_file!r} pyproject={pyproject_version!r} "
          f"ADDON_VERSION={mod.ADDON_VERSION!r} bl_info={bl_info_version!r}")
    with open(os.path.join(repo_root, "src", "blender_mcp", "server.py"),
              encoding="utf-8") as f:
        m = re.search(r'^\s*SERVER_VERSION\s*=\s*"([^"]+)"', f.read(), re.MULTILINE)
    server_fallback = m.group(1) if m else None
    check("server fallback version consistency", server_fallback == version_file,
          f"server.py fallback={server_fallback!r} VERSION={version_file!r}")

    # --- ping / get_capabilities ---------------------------------------
    r = run("ping")
    check("ping payload",
          r.get("pong") is True and r.get("addon_version") == mod.ADDON_VERSION,
          str(r)[:200])

    r = run("get_capabilities")
    check("capabilities version", r.get("addon_version") == mod.ADDON_VERSION,
          str(r)[:200])
    check("capabilities commands", "get_scene_graph" in r.get("commands", []) and
          "set_transform" in r.get("commands", []) and
          "batch_commands" in r.get("commands", []), str(r.get("commands"))[:300])
    check("capabilities integrations",
          set(r.get("integrations", {}).keys()) ==
          {"polyhaven", "hyper3d", "sketchfab", "hunyuan3d"},
          str(r.get("integrations")))

    # --- set_client_info handshake ---------------------------------------
    r = run("set_client_info", version=mod.ADDON_VERSION, name="blender-mcp")
    check("set_client_info match",
          r.get("ok") is True and r.get("match") is True
          and r.get("addon_version") == mod.ADDON_VERSION,
          str(r)[:200])
    check("set_client_info stored",
          server.client_version == mod.ADDON_VERSION
          and server.client_name == "blender-mcp",
          f"version={server.client_version} name={server.client_name}")
    r = run("set_client_info", version="0.0.1")
    check("set_client_info mismatch",
          r.get("ok") is True and r.get("match") is False, str(r)[:200])

    # --- scene introspection --------------------------------------------
    r = run("get_scene_info")
    check("get_scene_info keys",
          "objects" in r and "frame_start" in r and "fps" in r and "mode" in r,
          str(list(r.keys())))
    check("get_scene_info file identity (unsaved)",
          "filepath" in r and r.get("filepath") in (None, "")
          and r.get("file_saved") is False
          and isinstance(r.get("unsaved_changes"), bool),
          str({k: r.get(k) for k in ("filepath", "file_saved", "unsaved_changes")}))

    r = run("get_scene_graph", include=["bounds", "mesh_stats"], limit=50)
    check("scene_graph scene block",
          isinstance(r.get("scene"), dict) and "engine" in r["scene"], str(r)[:300])
    sblock = r.get("scene") or {}
    check("scene_graph file identity (unsaved)",
          "filepath" in sblock and sblock.get("filepath") in (None, "")
          and sblock.get("file_saved") is False
          and isinstance(sblock.get("unsaved_changes"), bool),
          str({k: sblock.get(k) for k in ("filepath", "file_saved", "unsaved_changes")}))
    names = [o.get("name") for o in r.get("objects", [])]
    check("scene_graph has Cube", "Cube" in names, str(names))
    cube_entry = next((o for o in r.get("objects", []) if o.get("name") == "Cube"), {})
    check("scene_graph mesh_stats", isinstance(cube_entry.get("mesh_stats"), dict),
          str(cube_entry)[:300])
    check("scene_graph counts",
          isinstance(r.get("total_count"), int) and "returned_count" in r,
          str({k: r.get(k) for k in ('total_count', 'returned_count', 'offset')}))
    check("scene_graph collections_total",
          isinstance(r.get("collections_total"), int)
          and r["collections_total"] >= len(r.get("collections", [])),
          str(r.get("collections_total")))

    r = run("get_scene_graph", filter_type="CAMERA")
    check("scene_graph filter_type",
          all(o.get("type") == "CAMERA" for o in r.get("objects", []))
          and r.get("total_count", 0) >= 1,
          str(r.get("objects"))[:200])

    r = run("get_object_info", name="Cube")
    check("object_info enrichment",
          "dimensions" in r and "modifiers" in r and "collections" in r
          and "vertex_groups" in r and "uv_layers" in r,
          str(list(r.keys())))

    # --- execute_code ----------------------------------------------------
    r = run("execute_code", code="40 + 2")
    check("execute_code expr", r.get("executed") is True and r.get("result_repr") == "42",
          str(r)[:300])

    r = run("execute_code", code="print('hello_mcp')")
    check("execute_code stdout", "hello_mcp" in r.get("stdout", ""), str(r)[:300])

    r = run("execute_code", code="raise ValueError('boom')")
    err = r.get("error") or {}
    check("execute_code error dict",
          r.get("executed") is False and err.get("type") == "ValueError"
          and "boom" in str(err.get("message", "")) and err.get("traceback"),
          str(r)[:400])

    r = run("execute_code",
            code="import bpy\nbpy.ops.mesh.primitive_uv_sphere_add()")
    diff = r.get("scene_diff") or {}
    check("execute_code scene_diff",
          any("Sphere" in n for n in diff.get("objects_added", [])),
          str(diff)[:300])

    # --- execute_code rollback_on_error (single-step undo semantics) -------
    # Emulate execute_wrapper's pre-command undo_push, then run failing code
    # with rollback_on_error=True: exactly this command's change must revert
    # (and NOT the previous command's change too).
    run("execute_code", code="bpy.data.objects['Cube'].location.x = 5.0")
    try:
        bpy.ops.ed.undo_push(message="MCP: execute_code")
        undo_available = True
    except Exception:
        undo_available = False
    if undo_available:
        r = run("execute_code", rollback_on_error=True,
                code="bpy.data.objects['Cube'].location.x = 9.0\n"
                     "raise RuntimeError('rollback me')")
        x = bpy.data.objects["Cube"].location.x
        check("execute_code rollback exact",
              r.get("rolled_back") is True and abs(x - 5.0) < 1e-5,
              f"rolled_back={r.get('rolled_back')} cube.x={x} (want 5.0)")
        run("execute_code", code="bpy.data.objects['Cube'].location.x = 0.0")
    else:
        print("  SKIP execute_code rollback exact (undo_push unavailable headless)")

    # --- transforms & placement ------------------------------------------
    r = run("set_transform", name="Cube", location=[1.0, 2.0, 3.0])
    check("set_transform location", r.get("location") == [1.0, 2.0, 3.0], str(r)[:300])

    r = run("place_object", name="Cube", mode="ground")
    bb = r.get("world_bounding_box")
    ground_ok = True
    detail = str(r)[:300]
    if isinstance(bb, (list, tuple)) and bb:
        try:
            min_z = min(v[2] for v in bb)
            ground_ok = abs(min_z) < 1e-3
            detail = f"min_z={min_z}"
        except Exception:
            pass
    check("place_object ground", ground_ok and r.get("name") == "Cube", detail)

    # place_object must apply its delta in WORLD space: a child of a rotated
    # parent would otherwise move along the wrong axis.
    run("execute_code", code=(
        "import bpy, math\n"
        "e = bpy.data.objects.new('SmokeEmpty', None)\n"
        "bpy.context.scene.collection.objects.link(e)\n"
        "e.rotation_euler = (math.radians(90), 0.0, 0.0)\n"
        "bpy.ops.mesh.primitive_cube_add(location=(0, 0, 4))\n"
        "c = bpy.context.active_object\n"
        "c.name = 'SmokeChild'\n"
        "bpy.context.view_layer.update()\n"
        "c.parent = e\n"
        "c.matrix_parent_inverse = e.matrix_world.inverted()\n"
        "bpy.context.view_layer.update()\n"
    ))
    r = run("place_object", name="SmokeChild", mode="ground")
    bb = r.get("world_bounding_box") or []
    try:
        min_z = min(v[2] for v in bb)
    except Exception:
        min_z = None
    check("place_object ground (parented)",
          min_z is not None and abs(min_z) < 1e-3,
          f"min_z={min_z} bb={str(bb)[:200]}")
    run("organize_scene", action="delete", objects=["SmokeChild", "SmokeEmpty"])

    # --- modifiers ---------------------------------------------------------
    r = run("manage_modifiers", name="Cube", action="add",
            modifier_type="SUBSURF", params={"levels": 1})
    check("manage_modifiers add", isinstance(r, dict), str(r)[:300])

    r = run("manage_modifiers", name="Cube", action="list")
    mods = r if isinstance(r, list) else r.get("modifiers", [])
    check("manage_modifiers list",
          any(m.get("type") == "SUBSURF" for m in mods), str(r)[:300])

    r = run("manage_modifiers", name="Cube", action="apply",
            modifier_name=mods[0].get("name") if mods else None)
    check("manage_modifiers apply result", isinstance(r, (dict, list)), str(r)[:300])

    # --- boolean ------------------------------------------------------------
    r = run("boolean_op", object_a="Cube", object_b="Sphere",
            operation="DIFFERENCE", apply=True, delete_operand=True)
    check("boolean_op stats",
          isinstance(r.get("mesh_stats_after"), dict) and r.get("applied") is True,
          str(r)[:300])
    check("boolean_op operand deleted", "Sphere" not in bpy.data.objects, "")

    # --- organize_scene -------------------------------------------------------
    r = run("organize_scene", action="create_collection", name="MCP_Col")
    check("organize create_collection", r.get("ok") is True, str(r)[:300])

    r = run("organize_scene", action="move_to_collection",
            objects=["Cube"], collection="MCP_Col")
    check("organize move_to_collection", r.get("ok") is True, str(r)[:300])

    r = run("organize_scene", action="rename", old="Cube", new="HeroCube")
    check("organize rename", r.get("ok") is True and "HeroCube" in bpy.data.objects,
          str(r)[:300])
    r = run("organize_scene", action="rename", old="HeroCube", new="Cube")
    check("organize rename back", r.get("ok") is True and "Cube" in bpy.data.objects,
          str(r)[:300])

    run("execute_code", code="import bpy\nbpy.ops.mesh.primitive_plane_add()")
    r = run("organize_scene", action="delete", objects=["Plane", "NoSuchObj"])
    check("organize delete",
          "Plane" in r.get("deleted", []) and "NoSuchObj" in r.get("not_found", []),
          str(r)[:300])

    # --- batch_commands (v1.8.3 step-budget batching) -------------------------
    r = run("batch_commands", commands=[
        {"type": "set_transform", "params": {"name": "Cube", "location": [2.0, 0.0, 1.0]}},
        {"type": "get_scene_info", "params": {}},
        {"type": "manage_timeline", "params": {"action": "get"}},
    ])
    check("batch happy path counts",
          r.get("executed") == 3 and r.get("total") == 3
          and r.get("stopped_early") is False
          and all(x.get("ok") is True for x in r.get("results", []))
          and len(r.get("results", [])) == 3,
          str({k: v for k, v in r.items() if k != "results"})
          + str([x.get("ok") for x in r.get("results", [])]))
    loc = list(bpy.data.objects["Cube"].location)
    check("batch set_transform applied",
          abs(loc[0] - 2.0) < 1e-5 and abs(loc[2] - 1.0) < 1e-5, str(loc))
    check("batch sub-result shapes",
          r.get("results", [{}])[0].get("type") == "set_transform"
          and isinstance(r.get("results", [{}, {}])[1].get("result"), dict),
          str(r.get("results"))[:300])

    # stop_on_error: failing middle command stops the batch, tail is skipped
    r = run("batch_commands", commands=[
        {"type": "set_transform", "params": {"name": "Cube", "location": [0.0, 0.0, 0.0]}},
        {"type": "get_object_info", "params": {"name": "NoSuchObject"}},
        {"type": "manage_timeline", "params": {"action": "get"}},
    ])
    bres = r.get("results", [])
    check("batch stop_on_error",
          r.get("executed") == 2 and r.get("stopped_early") is True
          and len(bres) == 3
          and bres[1].get("ok") is False and bres[1].get("error")
          and bres[2].get("ok") is None and bres[2].get("skipped") is True,
          str(r)[:400])

    # stop_on_error=False: everything runs, failures reported inline
    r = run("batch_commands", stop_on_error=False, commands=[
        {"type": "get_object_info", "params": {"name": "NoSuchObject"}},
        {"type": "ping", "params": {}},
    ])
    check("batch continue_on_error",
          r.get("executed") == 2 and r.get("stopped_early") is False
          and r.get("results", [{}])[0].get("ok") is False
          and r.get("results", [{}, {}])[1].get("ok") is True,
          str(r)[:300])

    # nested batch rejected
    resp = server._execute_command_internal({"type": "batch_commands", "params": {
        "commands": [{"type": "batch_commands", "params": {"commands": []}}]}})
    check("batch nested rejected",
          resp.get("status") == "error"
          and "nested" in str(resp.get("message", "")).lower(),
          str(resp)[:300])

    # render_sequence with wait=True (or omitted) rejected; status_only allowed
    resp = server._execute_command_internal({"type": "batch_commands", "params": {
        "commands": [{"type": "render_sequence", "params": {"filepath": "x.mp4"}}]}})
    check("batch render_sequence wait rejected",
          resp.get("status") == "error" and "wait" in str(resp.get("message", "")),
          str(resp)[:300])
    r = run("batch_commands", commands=[
        {"type": "render_sequence", "params": {"status_only": True}}])
    check("batch render_sequence status_only allowed",
          r.get("executed") == 1 and r.get("results", [{}])[0].get("ok") is True,
          str(r)[:300])

    # >25 commands rejected
    resp = server._execute_command_internal({"type": "batch_commands", "params": {
        "commands": [{"type": "ping", "params": {}}] * 26}})
    check("batch size cap",
          resp.get("status") == "error" and "25" in str(resp.get("message", "")),
          str(resp)[:300])

    # Single undo push per batch (lenient): the batch type itself is NOT in
    # MUTATING_COMMANDS (no per-command push), the wrapper derives ONE
    # "batch of N" push from the sub-type counter, and pushing doesn't crash.
    check("batch not in MUTATING_COMMANDS",
          "batch_commands" not in mod.MUTATING_COMMANDS, "")
    sub_types = mod.BlenderMCPServer._batch_sub_types(
        {"commands": [{"type": "set_transform", "params": {}}, {"type": "ping"}]})
    check("batch sub-type counter",
          sub_types == ["set_transform", "ping"] and len(sub_types) == 2
          and any(t in mod.MUTATING_COMMANDS for t in sub_types),
          str(sub_types))
    try:
        bpy.ops.ed.undo_push(message=f"MCP: batch of {len(sub_types)}")
    except Exception:
        pass  # undo may be unavailable headless; execute_wrapper guards it too
    check("batch undo push no crash", True, "")

    # --- timeline & keyframes -----------------------------------------------
    r = run("manage_timeline", action="set", frame_start=1, frame_end=48,
            fps=24, frame_current=1)
    check("manage_timeline set",
          r.get("frame_start") == 1 and r.get("frame_end") == 48 and r.get("fps") == 24,
          str(r)[:300])
    r = run("manage_timeline", action="get")
    check("manage_timeline get", r.get("duration_seconds") == 2.0, str(r)[:300])

    r = run("set_keyframes", name="Cube", data_path="location",
            keys=[{"frame": 1, "value": [0.0, 0.0, 0.0]},
                  {"frame": 24, "value": [0.0, 0.0, 2.0]}])
    check("set_keyframes", r.get("keys_created", 0) >= 2 and r.get("fcurves"),
          str(r)[:300])

    # get_object_info on an animated object (action.fcurves was removed in
    # Blender 5.x - must go through the slotted-action-aware helper)
    r = run("get_object_info", name="Cube")
    anim = r.get("animation") or {}
    check("object_info animation fcurves",
          anim.get("action") and len(anim.get("fcurves") or []) >= 1,
          str(anim)[:300])

    r = run("get_animation_info", name="Cube")
    check("get_animation_info object",
          r.get("action") and isinstance(r.get("fcurves"), list) and r.get("fcurves"),
          str(r)[:300])

    r = run("get_animation_info")
    check("get_animation_info scene",
          isinstance(r.get("timeline"), dict)
          and any(a.get("name") == "Cube" for a in r.get("animated_objects", [])),
          str(r)[:300])

    r = run("set_keyframe_interpolation", name="Cube", data_path="location",
            interpolation="LINEAR")
    check("set_keyframe_interpolation", isinstance(r, dict) and r, str(r)[:300])

    r = run("delete_keyframes", name="Cube", data_path="location")
    check("delete_keyframes", isinstance(r, dict) and r, str(r)[:300])

    # --- camera & rendering ------------------------------------------------
    r = run("set_camera", action="preset", preset="isometric")
    check("set_camera preset",
          r.get("camera") and r.get("is_scene_camera") is True
          and isinstance(r.get("location"), list),
          str(r)[:300])

    resp = server._execute_command_internal(
        {"type": "render_preview", "params": {"angles": ["front"], "max_size": 128}})
    if resp.get("status") == "success":
        imgs = resp["result"].get("images", [])
        check("render_preview", len(imgs) == 1 and imgs[0].get("image_data")
              and imgs[0].get("angle") == "front",
              str([{k: v for k, v in i.items() if k != 'image_data'} for i in imgs]))
    else:
        # OpenGL viewport rendering may be unavailable headless on some systems.
        msg = str(resp.get("message", ""))
        env_related = any(s in msg.lower() for s in ("opengl", "context", "gpu", "window"))
        check("render_preview (env-lenient)", env_related,
              f"unexpected failure: {msg[:300]}")

    resp = server._execute_command_internal(
        {"type": "render_animation_preview",
         "params": {"num_frames": 2, "max_size": 128}})
    if resp.get("status") == "success":
        res = resp["result"]
        check("render_animation_preview",
              len(res.get("images", [])) == 2 and len(res.get("frames_sampled", [])) == 2,
              str(res.get("frames_sampled")))
    else:
        msg = str(resp.get("message", ""))
        env_related = any(s in msg.lower() for s in ("opengl", "context", "gpu", "window"))
        check("render_animation_preview (env-lenient)", env_related,
              f"unexpected failure: {msg[:300]}")

    r = run("render_image", resolution_x=128, resolution_y=128,
            engine="EEVEE", samples=8)
    check("render_image",
          r.get("image_data") and r.get("width") == 128 and r.get("height") == 128,
          str({k: v for k, v in r.items() if k != 'image_data'})[:300])

    # --- assignment continuity (part 1: before the file is saved) ----------
    r = run("manage_assignment", action="read")
    check("assignment read empty", r.get("exists") is False, str(r)[:200])

    r = run("manage_assignment", action="start", title="Smoke Assignment",
            brief="Exercise the assignment continuity record.",
            plan=["Model the hero cube", "Light the scene"])
    check("assignment start",
          r.get("status") == "active" and len(r.get("plan", [])) >= 2
          and r.get("title") == "Smoke Assignment",
          str(r)[:300])

    r = run("manage_assignment", action="update", step="hero cube", done=True,
            decision="Units are meters", note="Marked first step done")
    check("assignment update",
          any(p.get("done") for p in r.get("plan", []))
          and "Units are meters" in r.get("decisions", [])
          and any("Marked first step done" in entry for entry in r.get("log", [])),
          str(r)[:400])

    r = run("manage_assignment", action="read")
    md = r.get("markdown", "")
    check("assignment read markdown",
          "Smoke Assignment" in md and "- [x]" in md and "- [ ]" in md,
          md[:300])

    # --- pipeline ---------------------------------------------------------
    glb_path = os.path.join(tmpdir, "smoke.glb")
    r = run("export_scene", filepath=glb_path)
    check("export_scene glb",
          r.get("size_bytes", 0) > 0 and os.path.exists(glb_path)
          and r.get("format") in ("glb", "gltf", "GLB", "GLTF"),
          str(r)[:300])

    r = run("import_local_asset", filepath=glb_path, target_size=1.0)
    check("import_local_asset", len(r.get("imported_objects", [])) >= 1, str(r)[:300])

    blend_path = os.path.join(tmpdir, "smoke_test.blend")
    r = run("manage_project", action="save_as", filepath=blend_path)
    check("manage_project save_as",
          r.get("ok") is True and os.path.exists(blend_path), str(r)[:300])

    # File identity must now report the real saved path (v1.8.2)
    def _same_path(a, b):
        return os.path.normcase(os.path.normpath(str(a))) == \
            os.path.normcase(os.path.normpath(str(b)))

    r = run("get_scene_info")
    check("get_scene_info file identity (saved)",
          r.get("file_saved") is True and _same_path(r.get("filepath"), blend_path),
          str({k: r.get(k) for k in ("filepath", "file_saved", "unsaved_changes")}))
    r = run("get_scene_graph", limit=1)
    sblock = r.get("scene") or {}
    check("scene_graph file identity (saved)",
          sblock.get("file_saved") is True
          and _same_path(sblock.get("filepath"), blend_path),
          str({k: sblock.get(k) for k in ("filepath", "file_saved", "unsaved_changes")}))

    # --- v1.8.1 panel output controls (save/version/render operators) ------
    # Operators register under RNA names derived from bl_idname:
    # 'blendermcp.save_now' -> bpy.types.BLENDERMCP_OT_save_now
    for op_name in ("save_now", "save_version", "render_still",
                    "render_clip", "cancel_render", "open_render_folder"):
        check(f"operator registered blendermcp.{op_name}",
              hasattr(bpy.types, f"BLENDERMCP_OT_{op_name}"), "not in bpy.types")

    prop = bpy.context.scene.bl_rna.properties.get("blendermcp_render_preset")
    items = {i.identifier for i in prop.enum_items} if prop is not None else set()
    check("render preset scene prop",
          prop is not None
          and items == {"SCENE", "LINKEDIN_WIDE", "SQUARE", "VERTICAL"},
          f"prop={prop} items={items}")

    check("auto_version pref declared",
          "auto_version_on_session_end" in
          getattr(mod.BLENDERMCP_AddonPreferences, "__annotations__", {}),
          str(list(getattr(mod.BLENDERMCP_AddonPreferences,
                           "__annotations__", {}).keys())))

    # Shared version-snapshot helper: writes v-numbered copies next to the
    # .blend saved above (the panel operator and the session-end auto-version
    # hook both end in this exact function)
    vpath = mod._save_version_snapshot()
    check("_save_version_snapshot v001",
          vpath.endswith("smoke_test_v001.blend") and os.path.exists(vpath),
          vpath)
    vpath2 = mod._save_version_snapshot()
    check("_save_version_snapshot increments",
          vpath2.endswith("smoke_test_v002.blend") and os.path.exists(vpath2),
          vpath2)

    # The manage_project handler routes through the same helper
    r = run("manage_project", action="save_version")
    check("manage_project save_version via helper",
          str(r.get("filepath", "")).endswith("smoke_test_v003.blend")
          and os.path.exists(str(r.get("filepath", ""))), str(r)[:300])

    # Session-end hook: must never raise, from any thread, with any stats
    # (the scheduled timer no-ops here: the module is not an installed addon,
    # so the preferences lookup returns None)
    server._maybe_auto_version({"commands": 3, "mutated": True})
    server._maybe_auto_version({"commands": 0, "mutated": False})
    server._maybe_auto_version({})
    check("_maybe_auto_version no crash", True, "")

    # --- assignment continuity (part 2: sidecar after save + handoff) ------
    sidecar_path = os.path.join(tmpdir, "smoke_test.assignment.md")
    sidecar_content = ""
    if os.path.exists(sidecar_path):
        with open(sidecar_path, encoding="utf-8") as f:
            sidecar_content = f.read()
    check("assignment sidecar after save",
          "Smoke Assignment" in sidecar_content and "- [x]" in sidecar_content,
          f"path={sidecar_path} exists={os.path.exists(sidecar_path)} "
          f"content={sidecar_content[:200]}")

    r = run("manage_assignment", action="handoff",
            handoff="Hero cube modeled; next: lighting pass.")
    check("assignment handoff",
          r.get("status") == "complete"
          and "lighting pass" in str(r.get("handoff", ""))
          and r.get("sidecar_path"),
          str(r)[:300])

    # --- automatic session record on disconnect (v1.8.3) --------------------
    # (calls the main-thread body directly - the timer wrapper never fires in
    # a headless script run, same as the auto-version test below)
    # A record EXISTS (the handoff above): the hook appends a session summary.
    server._record_session_now(
        {"commands": 5, "mutated": True,
         "types": {"execute_code": 3, "set_transform": 1, "ping": 1}})
    rec = mod._assignment_load()
    check("session record appends to existing record",
          rec is not None and rec.get("title") == "Smoke Assignment"
          and any("auto: session ran 5 commands" in e for e in rec.get("log", []))
          and any("execute_code x3" in e for e in rec.get("log", [])),
          str((rec or {}).get("log", []))[-400:])

    # NO record: the hook auto-creates a stub with the untracked-session title.
    bpy.data.texts.remove(bpy.data.texts[mod.ASSIGNMENT_TEXT_NAME])
    server._record_session_now(
        {"commands": 7, "mutated": True,
         "types": {"execute_code": 4, "manage_sequence": 2, "get_scene_info": 1}})
    rec = mod._assignment_load()
    check("session record auto-stub created",
          rec is not None
          and rec.get("title") == "smoke_test — untracked session"
          and rec.get("status") == "active"
          and "Auto-created" in str(rec.get("brief", ""))
          and any("auto: session ran 7 commands" in e for e in rec.get("log", [])),
          str(rec)[:400])
    check("session record command-type counting",
          any("execute_code x4" in e and "manage_sequence x2" in e
              for e in (rec or {}).get("log", [])),
          str((rec or {}).get("log", [])))
    check("session record activity log entry",
          any(e.get("type") == "session_record"
              and e.get("summary") == "session record updated"
              for e in server.activity_log),
          str(list(server.activity_log)[-3:]))

    # The scheduling wrapper must never raise, from any thread, with any stats
    server._maybe_record_session({"commands": 3, "mutated": True})
    server._maybe_record_session({"commands": 0, "mutated": False})
    server._maybe_record_session({})
    check("_maybe_record_session no crash", True, "")

    # _summarize_command_types: top-5 by count, ties broken alphabetically
    s = mod._summarize_command_types(
        {"a": 1, "b": 5, "c": 3, "d": 2, "e": 4, "f": 6})
    check("_summarize_command_types top5",
          s == "f x6, b x5, e x4, c x3, d x2", s)
    check("_summarize_command_types empty",
          mod._summarize_command_types({}) == "none"
          and mod._summarize_command_types(None) == "none", "")

    # --- auto session-record debounce (v1.8.4) -------------------------------
    parsed = mod._parse_auto_note(
        "[2026-07-05 10:00] auto: session ran 4 commands (a x3, b x1)")
    check("_parse_auto_note",
          parsed is not None and parsed[1] == 4 and parsed[2] == {"a": 3, "b": 1},
          str(parsed))
    check("_parse_auto_note rejects manual notes",
          mod._parse_auto_note("[2026-07-05 10:00] fixed the lighting") is None
          and mod._parse_auto_note("plain text") is None, "")

    # The auto-stub above just wrote "session ran 7 commands": a second auto
    # note within the merge window must REPLACE it with summed counts.
    rec = mod._assignment_load()
    log_len_before = len(rec.get("log", []))
    server._record_session_now(
        {"commands": 2, "mutated": True, "types": {"render_image": 2}})
    rec = mod._assignment_load()
    check("auto-note debounce merges within window",
          len(rec.get("log", [])) == log_len_before
          and "auto: session ran 9 commands" in rec["log"][-1]
          and "execute_code x4" in rec["log"][-1]
          and "render_image x2" in rec["log"][-1],
          str(rec.get("log", []))[-400:])

    # An auto note OLDER than the window is appended-after, not merged.
    old_ts = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")
    rec["log"][-1] = re.sub(r"^\[[^\]]+\]", f"[{old_ts}]", rec["log"][-1])
    mod._assignment_store(rec)
    server._record_session_now(
        {"commands": 3, "mutated": True, "types": {"ping": 3}})
    rec = mod._assignment_load()
    check("auto-note debounce appends after window",
          len(rec.get("log", [])) == log_len_before + 1
          and "auto: session ran 3 commands" in rec["log"][-1]
          and "auto: session ran 9 commands" in rec["log"][-2],
          str(rec.get("log", []))[-400:])

    # --- GUI-safe deferred renders (v1.8.4; headless: routing only) ----------
    check("RENDER_DEFERRED_COMMANDS constant",
          mod.RENDER_DEFERRED_COMMANDS ==
          {"render_image", "render_preview", "render_animation_preview"}
          and "get_viewport_screenshot" not in mod.RENDER_DEFERRED_COMMANDS,
          str(getattr(mod, "RENDER_DEFERRED_COMMANDS", None)))
    check("deferred routing: background stays synchronous",
          all(mod._should_defer_render(c, background=True) is False
              for c in mod.RENDER_DEFERRED_COMMANDS)
          and mod._should_defer_render("render_sequence", {"wait": True},
                                       background=True) is False, "")
    check("deferred routing: headless default is synchronous",
          mod._should_defer_render("render_image") is False,
          f"bpy.app.background={bpy.app.background}")
    check("deferred routing: GUI defers render commands",
          all(mod._should_defer_render(c, background=False) is True
              for c in mod.RENDER_DEFERRED_COMMANDS), "")
    check("deferred routing: render_sequence variants",
          mod._should_defer_render("render_sequence", {"wait": True},
                                   background=False) is True
          and mod._should_defer_render("render_sequence", {},
                                       background=False) is True
          and mod._should_defer_render("render_sequence", {"wait": False},
                                       background=False) is False
          and mod._should_defer_render("render_sequence", {"status_only": True},
                                       background=False) is False, "")
    check("deferred routing: screenshot never deferred",
          mod._should_defer_render("get_viewport_screenshot",
                                   background=False) is False
          and mod._should_defer_render("get_scene_info",
                                       background=False) is False, "")
    check("render pipeline busy flag",
          mod._render_pipeline_busy() is False, "")
    mod._DEFERRED_JOB["active"] = True
    check("render pipeline busy via deferred job",
          mod._render_pipeline_busy() is True, "")
    mod._DEFERRED_JOB["active"] = False
    r = run("render_sequence", status_only=True)
    check("status_only excludes on_complete",
          "on_complete" not in r and "kind" in r, str(r)[:300])

    # --- client takeover protection (v1.8.4) ---------------------------------
    check("takeover pref declared",
          "client_takeover_policy" in
          getattr(mod.BLENDERMCP_AddonPreferences, "__annotations__", {}),
          str(list(getattr(mod.BLENDERMCP_AddonPreferences,
                           "__annotations__", {}).keys())))
    ann = mod.BLENDERMCP_AddonPreferences.__annotations__.get(
        "client_takeover_policy")
    default = getattr(ann, "keywords", {}).get("default")
    check("takeover pref defaults PROTECT", default == "PROTECT", str(default))

    now = 1000.0
    check("takeover rejects fresh activity",
          mod._takeover_decision("PROTECT", True, now - 30, now=now) == "reject",
          "")
    check("takeover adopts stale activity",
          mod._takeover_decision("PROTECT", True,
                                 now - mod.CLIENT_PROTECT_WINDOW_SECONDS - 1,
                                 now=now) == "adopt", "")
    check("takeover NEWEST always adopts",
          mod._takeover_decision("NEWEST", True, now - 1, now=now) == "adopt", "")
    check("takeover adopts when no client connected",
          mod._takeover_decision("PROTECT", False, now - 1, now=now) == "adopt",
          "")
    check("takeover adopts when client never commanded",
          mod._takeover_decision("PROTECT", True, None, now=now) == "adopt", "")
    check("takeover policy fallback PROTECT",
          server._get_takeover_policy() == "PROTECT",
          server._get_takeover_policy())

    # --- video sequence editor (VSE) ---------------------------------------
    check("DELIVERY_PRESETS constant",
          isinstance(getattr(mod, "DELIVERY_PRESETS", None), dict)
          and set(mod.DELIVERY_PRESETS) == {"LINKEDIN_WIDE", "SQUARE", "VERTICAL"},
          str(getattr(mod, "DELIVERY_PRESETS", None)))

    r = run("manage_sequence", action="setup_timeline", preset="SQUARE",
            frame_start=1, frame_end=48)
    scn = bpy.context.scene
    check("vse setup_timeline SQUARE",
          scn.render.resolution_x == 1080 and scn.render.resolution_y == 1080
          and scn.render.fps == 25 and r.get("timeline", {}).get("fps") == 25,
          f"res={scn.render.resolution_x}x{scn.render.resolution_y} "
          f"fps={scn.render.fps} r={str(r)[:200]}")

    # Two tiny PNGs (deliberately un-numbered names: single stills, not a sequence)
    png_a = os.path.join(tmpdir, "vse_shot_a.png")
    png_b = os.path.join(tmpdir, "vse_shot_b.png")
    img = bpy.data.images.new("vse_smoke_img", 64, 64)
    img.pixels[:] = [0.8, 0.2, 0.2, 1.0] * (64 * 64)
    img.filepath_raw = png_a
    img.file_format = 'PNG'
    img.save()
    img.pixels[:] = [0.2, 0.2, 0.8, 1.0] * (64 * 64)
    img.filepath_raw = png_b
    img.save()

    r = run("manage_sequence", action="add_media", filepath=png_a, frame_start=1)
    strip_a = (r.get("strip") or {})
    check("vse add_media image A",
          r.get("media_type") == "image" and strip_a.get("type") == "IMAGE"
          and strip_a.get("frame_start") == 1
          and strip_a.get("frame_final_end") == 97,  # 96-frame default still
          str(r)[:300])

    r = run("manage_sequence", action="add_media", filepath=png_b,
            frame_start=120, channel=2)
    strip_b = (r.get("strip") or {})
    check("vse add_media image B",
          strip_b.get("channel") == 2 and strip_b.get("frame_start") == 120,
          str(r)[:300])
    check("vse add_media auto channel",
          strip_a.get("channel") == 1, f"channel={strip_a.get('channel')}")

    r = run("manage_sequence", action="add_text", text="Smoke Title",
            frame_start=5, duration=40, position="BOTTOM")
    txt = (r.get("strip") or {})
    check("vse add_text",
          txt.get("type") == "TEXT" and txt.get("frame_start") == 5
          and txt.get("frame_final_end") == 45 and txt.get("text") == "Smoke Title",
          str(r)[:300])

    # A (1..97) and B (120..216) don't overlap: expect an auto-shift report
    r = run("manage_sequence", action="add_transition",
            strip_a=strip_a.get("name"), strip_b=strip_b.get("name"),
            type="CROSS", duration=12)
    shifted = r.get("shifted") or {}
    check("vse add_transition auto-shift",
          shifted.get("strip") == strip_b.get("name")
          and shifted.get("moved_back_frames") == 35  # 120 -> 85 (97 - 12)
          and (r.get("transition") or {}).get("type") in ("CROSS",),
          str(r)[:400])

    r = run("manage_sequence", action="add_fade",
            strip_name=strip_a.get("name"), fade_type="IN", duration=10)
    check("vse add_fade result",
          r.get("property") == "blend_alpha" and r.get("fade_type") == "IN",
          str(r)[:300])
    fade_fcurve = None
    anim = bpy.context.scene.animation_data
    action = anim.action if anim else None
    for fc in mod.BlenderMCPServer._action_fcurves(action):
        if "blend_alpha" in fc.data_path and strip_a.get("name", "?") in fc.data_path:
            fade_fcurve = fc
            break
    check("vse add_fade fcurve",
          fade_fcurve is not None and len(fade_fcurve.keyframe_points) >= 2,
          f"fcurves={[f.data_path for f in mod.BlenderMCPServer._action_fcurves(action)]}")

    r = run("manage_sequence", action="set_strip",
            strip_name=strip_b.get("name"), frame_start=100, channel=3)
    listed = {s.get("name"): s for s in (r.get("timeline") or {}).get("strips", [])}
    moved = listed.get(strip_b.get("name"), {})
    check("vse set_strip move",
          moved.get("frame_start") == 100 and moved.get("channel") == 3,
          str(moved))

    r = run("manage_sequence", action="list")
    check("vse list",
          r.get("resolution") == [1080, 1080] and r.get("fps") == 25
          and r.get("total_strips", 0) >= 4
          and isinstance(r.get("strips"), list)
          and r.get("duration_seconds") == round(48 / 25, 4),
          str({k: v for k, v in r.items() if k != 'strips'}))

    # --- render_sequence: real FFMPEG encode over 12 frames ------------------
    mp4_path = os.path.join(tmpdir, "vse_smoke.mp4")
    r = run("render_sequence", filepath=mp4_path, resolution=[320, 320],
            frame_start=1, frame_end=12, wait=True)
    check("render_sequence mp4",
          r.get("filepath") and os.path.exists(r["filepath"])
          and r.get("size_bytes", 0) > 0 and r.get("frames") == 12,
          str(r)[:300])
    check("render_sequence settings restored",
          scn.render.resolution_x == 1080
          and scn.render.image_settings.file_format != 'FFMPEG'
          and scn.frame_end == 48,
          f"res_x={scn.render.resolution_x} fmt={scn.render.image_settings.file_format} "
          f"frame_end={scn.frame_end}")

    r = run("render_sequence", status_only=True)
    check("render_sequence status_only",
          r.get("active") is False and "done" in r and "filepath" in r,
          str(r)[:300])

    resp = server._execute_command_internal(
        {"type": "render_sequence",
         "params": {"filepath": os.path.join(tmpdir, "vse_async.mp4"), "wait": False}})
    check("render_sequence async headless error",
          resp.get("status") == "error"
          and "wait=True" in str(resp.get("message", "")),
          str(resp)[:300])

    # --- remove_strip + clear -------------------------------------------------
    r = run("manage_sequence", action="remove_strip", strip_name=txt.get("name"))
    check("vse remove_strip",
          r.get("removed") == txt.get("name")
          and txt.get("name") not in
          [s.get("name") for s in (r.get("timeline") or {}).get("strips", [])],
          str(r)[:300])

    resp = server._execute_command_internal(
        {"type": "manage_sequence", "params": {"action": "clear"}})
    check("vse clear requires confirm",
          resp.get("status") == "error" and "confirm" in str(resp.get("message", "")),
          str(resp)[:300])

    r = run("manage_sequence", action="clear", confirm=True)
    check("vse clear",
          (r.get("timeline") or {}).get("total_strips") == 0
          and r.get("removed_strips", 0) >= 1,
          str(r)[:300])

    # --- pause switch --------------------------------------------------------
    bpy.context.scene.blendermcp_paused = True
    resp = server._execute_command_internal({"type": "get_scene_info", "params": {}})
    check("pause blocks commands",
          resp.get("status") == "error" and "Paused" in str(resp.get("message", "")),
          str(resp)[:300])
    resp = server._execute_command_internal({"type": "ping", "params": {}})
    check("pause allows ping", resp.get("status") == "success", str(resp)[:300])
    resp = server._execute_command_internal(
        {"type": "set_client_info", "params": {"version": mod.ADDON_VERSION}})
    check("pause allows set_client_info", resp.get("status") == "success",
          str(resp)[:300])
    bpy.context.scene.blendermcp_paused = False

    # --- update check must not run headless (auto-check is gated on
    # bpy.app.background and deferred via a timer the test never spins) ------
    check("update check not triggered headless",
          mod._UPDATE_INFO["checked"] is False
          and mod._UPDATE_INFO["checking"] is False,
          str(mod._UPDATE_INFO))

    # --- undo_last (lenient: undo state differs headless) ---------------------
    resp = server._execute_command_internal({"type": "undo_last", "params": {}})
    check("undo_last no crash", isinstance(resp, dict) and "status" in resp,
          str(resp)[:300])

    # --- activity log: handler-level {"error": ...} counts as an error --------
    server.last_error = None
    server._log_activity(
        "search_sketchfab_models", {"params": {}},
        {"status": "success", "result": {"error": "Sketchfab is disabled."}},
        time.time())
    entry = server.activity_log[-1] if server.activity_log else {}
    check("activity log handler error",
          entry.get("status") == "error"
          and "disabled" in str((server.last_error or {}).get("message", "")),
          f"entry={entry} last_error={server.last_error}")

    # --- unknown command ------------------------------------------------------
    resp = server._execute_command_internal({"type": "no_such_cmd", "params": {}})
    check("unknown command error",
          resp.get("status") == "error" and "Unknown command type" in str(resp.get("message")),
          str(resp)[:300])

    # --- legacy MCP server detection + response notice (v1.8.2) ---------------
    # Simulated legacy connection: client info never set, >1 command completed,
    # responses run through _execute_command_internal and then injected the
    # same way execute_wrapper does it.
    lsrv = mod.BlenderMCPServer(port=9877)  # never started
    check("legacy flag defaults off",
          lsrv.legacy_client is False and lsrv._saw_client_info is False, "")
    lsrv._update_legacy_detection(1)
    check("legacy not flagged on first command", lsrv.legacy_client is False, "")
    lsrv._update_legacy_detection(2)
    check("legacy flagged after first completed command",
          lsrv.legacy_client is True, "")

    # Modern client (set_client_info received): never flagged, nothing injected
    msrv = mod.BlenderMCPServer(port=9878)
    msrv._execute_command_internal(
        {"type": "set_client_info", "params": {"version": mod.ADDON_VERSION}})
    msrv._update_legacy_detection(5)
    check("modern client never flagged", msrv.legacy_client is False, "")
    resp = msrv._inject_legacy_notice(
        "execute_code",
        {"status": "success", "result": {"executed": True, "stdout": "quiet"}})
    check("modern client gets no notice",
          "result" not in resp["result"] and "server_notice" not in resp["result"],
          str(resp)[:300])

    # Legacy execute_code: notice + stdout packed into a "result" key (the
    # only field old servers forward: result.get("result", ""))
    resp = lsrv._execute_command_internal(
        {"type": "execute_code", "params": {"code": "print('legacy_stdout_probe')"}})
    resp = lsrv._inject_legacy_notice("execute_code", resp)
    res = resp.get("result", {})
    check("legacy execute_code result key",
          isinstance(res.get("result"), str)
          and res["result"].startswith("[blender-mcp]")
          and "outdated" in res["result"]
          and "legacy_stdout_probe" in res["result"],
          str(res.get("result"))[:300])
    check("legacy execute_code new-shape keys intact",
          res.get("executed") is True and "legacy_stdout_probe" in res.get("stdout", ""),
          str(list(res.keys())))

    # Throttle: the next 9 eligible commands carry no notice...
    resp = lsrv._inject_legacy_notice(
        "get_scene_info", {"status": "success", "result": {"name": "Scene"}})
    check("legacy notice throttled", "server_notice" not in resp["result"],
          str(resp)[:300])
    # ...but execute_code stdout keeps being forwarded while throttled
    resp = lsrv._inject_legacy_notice(
        "execute_code", {"status": "success", "result": {"stdout": "still_visible"}})
    check("legacy stdout forwarded while throttled",
          resp["result"].get("result") == "still_visible", str(resp)[:300])
    # every 10th eligible command gets the notice again
    lsrv._legacy_notice_counter = 10
    resp = lsrv._inject_legacy_notice(
        "get_scene_info", {"status": "success", "result": {"name": "Scene"}})
    check("legacy notice every 10 commands",
          resp["result"].get("server_notice") == mod.LEGACY_SERVER_NOTICE,
          str(resp)[:300])
    # only passthrough command types are touched
    resp = lsrv._inject_legacy_notice(
        "get_scene_graph", {"status": "success", "result": {"scene": {}}})
    check("legacy notice only for passthrough commands",
          "server_notice" not in resp["result"] and "result" not in resp["result"],
          str(resp)[:300])
    # errors from executed code also reach legacy servers via the result field
    lsrv._legacy_notice_counter = 1  # notice not due
    resp = lsrv._execute_command_internal(
        {"type": "execute_code", "params": {"code": "raise ValueError('legacy boom')"}})
    resp = lsrv._inject_legacy_notice("execute_code", resp)
    check("legacy execute_code error forwarded",
          "legacy boom" in str(resp["result"].get("result", "")),
          str(resp["result"].get("result"))[:300])

    # --- load_post state resync handler (v1.8.2) -------------------------------
    def _handler_count(handler_list, name):
        return sum(1 for h in handler_list if getattr(h, "__name__", "") == name)

    check("load_post registered once",
          _handler_count(bpy.app.handlers.load_post, "_blendermcp_load_post") == 1,
          str([getattr(h, "__name__", "?") for h in bpy.app.handlers.load_post]))

    # The handler re-syncs the per-file scene prop to the ACTUAL server state
    # (no server is running in this test)
    bpy.context.scene.blendermcp_server_running = True  # lie, as a stale .blend would
    mod._blendermcp_load_post()
    check("load_post resyncs server_running",
          bpy.context.scene.blendermcp_server_running is False,
          f"prop={bpy.context.scene.blendermcp_server_running}")

    # Survives a full unregister/register cycle without duplicating
    mod.unregister()
    mod.register()
    try:
        if hasattr(bpy.types, "blendermcp_server") and bpy.types.blendermcp_server:
            bpy.types.blendermcp_server.stop()
    except Exception:
        pass
    check("load_post survives re-register",
          _handler_count(bpy.app.handlers.load_post, "_blendermcp_load_post") == 1,
          str([getattr(h, "__name__", "?") for h in bpy.app.handlers.load_post]))
    check("save_post survives re-register",
          _handler_count(bpy.app.handlers.save_post, "_blendermcp_save_post") == 1,
          str([getattr(h, "__name__", "?") for h in bpy.app.handlers.save_post]))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        failures.append("smoke test crashed: " + traceback.format_exc()[-500:])

    print()
    if failures:
        print(f"SMOKE FAILED: {len(failures)} failure(s), {passed} passed")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print(f"SMOKE OK {passed} passed")
    sys.exit(0)
