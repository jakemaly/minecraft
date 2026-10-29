"""Small Three.js voxel observer synced over the existing marimo widget channel."""

from __future__ import annotations

import json


def build_widget(snapshot: dict, session):
    try:
        import anywidget
        import traitlets
    except ImportError as exc:
        raise RuntimeError("install the optional anywidget package before opening the viewer") from exc

    class VoxelObserver(anywidget.AnyWidget):
        _esm = r'''import * as THREE from "https://esm.sh/three@0.128.0";
export function render({ model, el }) {
  const root = document.createElement("div");
  root.style.cssText = "width:100%;height:420px;position:relative;background:#111827;border-radius:8px;overflow:hidden";
  const label = document.createElement("div");
  label.style.cssText = "position:absolute;z-index:2;top:8px;left:10px;color:white;font:12px sans-serif;background:#111827bb;padding:5px;border-radius:4px";
  const canvas = document.createElement("canvas"); canvas.style.cssText = "width:100%;height:100%";
  root.append(canvas,label); el.append(root);
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(1); renderer.setSize(384,216,false);
  const scene = new THREE.Scene(); scene.background = new THREE.Color(0x111827);
  const camera = new THREE.PerspectiveCamera(75, 384 / 216, 0.1, 1000);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x334155, 2));
  const light = new THREE.DirectionalLight(0xffffff, 2); light.position.set(8, 16, 10); scene.add(light);
  const cube = new THREE.BoxGeometry(0.96, 0.96, 0.96);
  const materials = new Map();
  const colors = { grass_block: 0x4e9f3d, dirt: 0x805a3b, stone: 0x858b95, oak_log: 0x866037, oak_planks: 0xb58a54, water: 0x328bd1, sand: 0xe1d096, bedrock: 0x34363c, diamond_ore: 0x4c9b91, deepslate_diamond_ore: 0x3a6f70, diamond_block: 0x53d7d0, coal_ore: 0x343840, iron_ore: 0xbba58a, oak_leaves: 0x3c8b40, spruce_leaves: 0x365d43, crafting_table: 0x9b6841, cobblestone: 0x70777d };
  let last = 0;
  let pending = false;
  function resize() { camera.aspect = 384 / 216; camera.updateProjectionMatrix(); renderer.render(scene, camera); }
  const observer = new ResizeObserver(resize); observer.observe(root);
  function update() {
    const started = performance.now();
    const raw = model.get("snapshot_json");
    let data; try { data = JSON.parse(raw || "{}"); } catch { return; }
    pending = false;
    if (!data || data.sequence < last) return; // Ignore stale snapshots during backpressure.
    last = data.sequence || 0;
    for (const child of [...scene.children]) if (child.userData.voxel) scene.remove(child);
    const center = data.center || {x:0,y:0,z:0};
    const player = data.player || {position:center,yaw:0,pitch:0};
    const playerPos = player.position || center;
    const relativePlayer = new THREE.Vector3(playerPos.x-center.x, playerPos.y-center.y, playerPos.z-center.z);
    // Match Prismarine Viewer 1.33.0's first-person camera: eye height 1.6, lowered 0.3 while sneaking.
    camera.position.set(relativePlayer.x, relativePlayer.y + 1.6 - (player.sneaking ? 0.3 : 0), relativePlayer.z);
    camera.rotation.set(player.pitch || 0, player.yaw || 0, 0, "ZYX");
    for (const b of (data.blocks || []).slice(0, 512)) {
      const color = colors[b.name] || 0x7b8490;
      if (!materials.has(color)) materials.set(color, new THREE.MeshLambertMaterial({ color }));
      const mesh = new THREE.Mesh(cube, materials.get(color));
      mesh.position.set(b.x-center.x+0.5,b.y-center.y+0.5,b.z-center.z+0.5); mesh.userData.voxel = true; scene.add(mesh);
    }
    label.textContent = `First-person view · ${(data.blocks || []).length} nearby blocks · frame ${data.sequence}`;
    resize();
    model.set("render_ms", performance.now() - started); model.save_changes();
    try {
      const jpeg = canvas.toDataURL("image/jpeg", 0.62);
      model.send({ type: "vision_frame", jpeg, sequence: data.sequence, run_id: model.get("run_id"), width: 384, height: 216, render_ms: performance.now() - started, physics_tick_p95_ms: data.physics_tick_p95_ms ?? null, source: "prismarine-viewer-camera-snapshot" });
    } catch (error) { model.send({ type: "vision_frame_error", sequence: data.sequence, message: String(error).slice(0, 200) }); }
  }
  function status() { const message = model.get("status"); if (message) { label.textContent = message; pending = false; } }
  model.on("change:snapshot_json", update); model.on("change:status", status); update();
    const timer = setInterval(() => { if (!pending) { pending = true; model.send({ type: "snapshot_request" }); } }, 1500);
  return () => { clearInterval(timer); model.off("change:snapshot_json", update); model.off("change:status", status); observer.disconnect(); for (const material of materials.values()) material.dispose(); cube.dispose(); renderer.dispose(); root.remove(); };
}
'''
        _css = ".jupyter-widget { min-width: 320px; }"
        snapshot_json = traitlets.Unicode().tag(sync=True)
        run_id = traitlets.Unicode("").tag(sync=True)
        status = traitlets.Unicode("Live observer connected; waiting for snapshot").tag(sync=True)
        render_ms = traitlets.Float(0.0).tag(sync=True)

    widget = VoxelObserver(snapshot_json=json.dumps(snapshot, separators=(",", ":")), run_id=session.run_id or "")

    def _record_render(change):
        measurements = session.reports.setdefault("viewer_render_ms", [])
        measurements.append(round(float(change["new"]), 3))
        del measurements[:-100]
        if session.events:
            session.events.append("viewer_render", {"render_ms": float(change["new"])})

    widget.observe(_record_render, names="render_ms")

    def _handle_widget_message(_widget, content, _buffers):
        if not isinstance(content, dict):
            return
        if content.get("type") == "vision_frame":
            import base64
            import hashlib
            import time
            encoded = content.get("jpeg", "")
            if not isinstance(encoded, str) or not encoded.startswith("data:image/jpeg;base64,"):
                return
            if content.get("run_id") != session.run_id:
                return
            image = base64.b64decode(encoded.split(",", 1)[1])
            metadata = {key: value for key, value in content.items() if key not in {"jpeg", "type"}}
            metadata.update({"image_sha256": hashlib.sha256(image).hexdigest(), "bytes": len(image), "received_at_monotonic": time.monotonic()})
            with session.lock:
                session.current_image_jpeg = image
                session.current_image_meta = metadata
            if session.events and int(metadata.get("sequence", 0)) % 10 == 0:
                session.events.append("viewer_frame_sample", {key: value for key, value in metadata.items() if key != "received_at_monotonic"})
            return
        if content.get("type") == "vision_frame_error":
            with session.lock:
                session.current_image_meta = {"status": "unavailable", "message": str(content.get("message", "capture failed"))[:200]}
            return
        if content.get("type") != "snapshot_request":
            return
        try:
            if not session.bridge:
                raise RuntimeError("Mineflayer bridge is disconnected")
            import time
            _started = time.monotonic()
            current = session.bridge.request("viewer_snapshot", {}, request_timeout=0.15)
            current["request_round_trip_ms"] = round((time.monotonic() - _started) * 1000, 3)
            payload = json.dumps(current, separators=(",", ":"))
            if len(payload.encode("utf-8")) > 100_000:
                raise RuntimeError("viewer snapshot exceeded 100 KiB")
            _widget.snapshot_json = payload
            _widget.status = "Embedded first-person voxel view · image passed to Nemotron only"
            session.reports["viewer_snapshot"] = current
            if session.events:
                session.events.append("viewer_snapshot_update", {"sequence": current["sequence"], "blocks": len(current["blocks"]), "request_round_trip_ms": current["request_round_trip_ms"], "capture_ms": current["capture_ms"], "physics_tick_samples": current["physics_tick_samples"], "physics_tick_p95_ms": current["physics_tick_p95_ms"]})
        except Exception as exc:
            _widget.status = f"Viewer paused: {type(exc).__name__}: {str(exc)[:160]}"

    widget.on_msg(_handle_widget_message)
    return widget
