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
        _esm = r'''import * as THREE from "https://esm.sh/three@0.180.0";
export function render({ model, el }) {
  const root = document.createElement("div");
  root.style.cssText = "width:100%;height:420px;position:relative;background:#111827;border-radius:8px;overflow:hidden";
  const label = document.createElement("div");
  label.style.cssText = "position:absolute;z-index:2;top:8px;left:10px;color:white;font:12px sans-serif;background:#111827bb;padding:5px;border-radius:4px";
  const canvas = document.createElement("canvas"); canvas.style.cssText = "width:100%;height:100%";
  root.append(canvas,label); el.append(root);
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  const scene = new THREE.Scene(); scene.background = new THREE.Color(0x111827);
  const camera = new THREE.PerspectiveCamera(55, 1, 0.1, 1000); camera.position.set(10, 12, 14); camera.lookAt(0, 0, 0);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x334155, 2));
  const light = new THREE.DirectionalLight(0xffffff, 2); light.position.set(8, 16, 10); scene.add(light);
  const cube = new THREE.BoxGeometry(0.96, 0.96, 0.96);
  const colors = { grass_block: 0x4e9f3d, dirt: 0x805a3b, stone: 0x858b95, oak_log: 0x866037, oak_planks: 0xb58a54, water: 0x328bd1, sand: 0xe1d096, bedrock: 0x34363c };
  let last = 0;
  let pending = false;
  function resize() { const rect = root.getBoundingClientRect(); renderer.setSize(rect.width, rect.height, false); camera.aspect = Math.max(rect.width, 1) / Math.max(rect.height, 1); camera.updateProjectionMatrix(); renderer.render(scene, camera); }
  const observer = new ResizeObserver(resize); observer.observe(root);
  function update() {
    const started = performance.now();
    const raw = model.get("snapshot_json");
    let data; try { data = JSON.parse(raw || "{}"); } catch { return; }
    pending = false;
    if (!data || data.sequence < last) return; // Ignore stale snapshots during backpressure.
    last = data.sequence || 0;
    for (const child of [...scene.children]) if (child.userData.voxel) { scene.remove(child); child.geometry.dispose(); child.material.dispose(); }
    const center = data.center || {x:0,y:0,z:0};
    for (const b of (data.blocks || []).slice(0, 512)) {
      const material = new THREE.MeshLambertMaterial({ color: colors[b.name] || 0x7b8490 });
      const mesh = new THREE.Mesh(cube, material); mesh.position.set(b.x-center.x,b.y-center.y,b.z-center.z); mesh.userData.voxel = true; scene.add(mesh);
    }
    label.textContent = `Loaded blocks: ${(data.blocks || []).length} · update ${data.sequence} · observer view`;
    resize();
    model.set("render_ms", performance.now() - started); model.save_changes();
  }
  function status() { const message = model.get("status"); if (message) { label.textContent = message; pending = false; } }
  model.on("change:snapshot_json", update); model.on("change:status", status); update();
  const timer = setInterval(() => { if (!pending) { pending = true; model.send({ type: "snapshot_request" }); } }, 1500);
  return () => { clearInterval(timer); model.off("change:snapshot_json", update); model.off("change:status", status); observer.disconnect(); renderer.dispose(); root.remove(); };
}
'''
        _css = ".jupyter-widget { min-width: 320px; }"
        snapshot_json = traitlets.Unicode().tag(sync=True)
        status = traitlets.Unicode("Live observer connected; waiting for snapshot").tag(sync=True)
        render_ms = traitlets.Float(0.0).tag(sync=True)

    widget = VoxelObserver(snapshot_json=json.dumps(snapshot, separators=(",", ":")))

    def _record_render(change):
        measurements = session.reports.setdefault("viewer_render_ms", [])
        measurements.append(round(float(change["new"]), 3))
        del measurements[:-100]
        if session.events:
            session.events.append("viewer_render", {"render_ms": float(change["new"])})

    widget.observe(_record_render, names="render_ms")

    def _handle_widget_message(_widget, content, _buffers):
        if not isinstance(content, dict) or content.get("type") != "snapshot_request":
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
            _widget.status = "Live observer view · Mineflayer data only · not provided to models"
            session.reports["viewer_snapshot"] = current
            if session.events:
                session.events.append("viewer_snapshot_update", {"sequence": current["sequence"], "blocks": len(current["blocks"]), "request_round_trip_ms": current["request_round_trip_ms"], "capture_ms": current["capture_ms"], "physics_tick_samples": current["physics_tick_samples"], "physics_tick_p95_ms": current["physics_tick_p95_ms"]})
        except Exception as exc:
            _widget.status = f"Viewer paused: {type(exc).__name__}: {str(exc)[:160]}"

    widget.on_msg(_handle_widget_message)
    return widget
