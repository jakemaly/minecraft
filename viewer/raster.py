"""Render bounded Mineflayer block snapshots from the player's viewpoint."""

from __future__ import annotations

import hashlib
import io
import math
import time
from typing import Any


_COLORS = {
    "grass_block": (91, 151, 69), "dirt": (125, 88, 57), "stone": (130, 136, 144),
    "cobblestone": (111, 116, 121), "oak_log": (133, 94, 55), "oak_wood": (133, 94, 55),
    "oak_planks": (174, 132, 79), "oak_leaves": (54, 126, 57), "spruce_leaves": (48, 91, 63),
    "water": (53, 129, 190), "sand": (218, 201, 142), "bedrock": (52, 54, 59),
    "diamond_ore": (73, 151, 143), "deepslate_diamond_ore": (58, 110, 111),
    "diamond_block": (77, 198, 190), "coal_ore": (61, 65, 70), "iron_ore": (174, 151, 125),
    "crafting_table": (147, 96, 58), "oak_stairs": (174, 132, 79),
}


def render_first_person(
    snapshot: dict[str, Any], *, width: int = 384, height: int = 216, quality: int = 62,
) -> tuple[bytes, dict[str, Any]]:
    """Return a small JPEG and render measurements for one current game snapshot."""
    if not 64 <= width <= 512 or not 64 <= height <= 512:
        raise ValueError("frame dimensions must be between 64 and 512 pixels")
    if not 20 <= quality <= 90:
        raise ValueError("JPEG quality must be between 20 and 90")
    from PIL import Image, ImageDraw

    started = time.perf_counter()
    player = snapshot.get("player") or {}
    position = player.get("position") or snapshot.get("center") or {"x": 0, "y": 0, "z": 0}
    px, py, pz = (float(position[key]) for key in ("x", "y", "z"))
    eye = (px, py + 1.6 - (0.3 if player.get("sneaking") else 0.0), pz)
    yaw, pitch = float(player.get("yaw", 0.0)), float(player.get("pitch", 0.0))
    forward = (-math.sin(yaw) * math.cos(pitch), -math.sin(pitch), math.cos(yaw) * math.cos(pitch))
    right = (math.cos(yaw), 0.0, math.sin(yaw))
    up = (math.sin(yaw) * math.sin(pitch), math.cos(pitch), -math.cos(yaw) * math.sin(pitch))
    blocks = {
        (int(block["x"]), int(block["y"]), int(block["z"])): str(block.get("name", "unknown"))
        for block in snapshot.get("blocks", [])
        if all(key in block for key in ("x", "y", "z"))
    }
    bounds = tuple(
        (min(position[i] for position in blocks), max(position[i] for position in blocks))
        for i in range(3)
    ) if blocks else None

    pixels = bytearray(width * height * 3)
    horizon = height * (0.5 + max(-0.25, min(0.25, pitch / math.pi)))
    for y in range(height):
        sky = y < horizon
        if sky:
            blend = min(1.0, y / max(1.0, horizon))
            background = (int(106 + 50 * blend), int(177 + 51 * blend), int(225 + 25 * blend))
        else:
            background = (91, 100, 77)
        start = y * width * 3
        for x in range(width):
            offset = start + x * 3
            pixels[offset:offset + 3] = bytes(background)

    tan_half_h = math.tan(math.radians(75) / 2)
    tan_half_v = tan_half_h * height / width
    if bounds is not None:
        for y in range(height):
            screen_y = (1.0 - 2.0 * (y + 0.5) / height) * tan_half_v
            for x in range(width):
                screen_x = (2.0 * (x + 0.5) / width - 1.0) * tan_half_h
                direction = tuple(forward[i] + right[i] * screen_x + up[i] * screen_y for i in range(3))
                length = math.sqrt(sum(component * component for component in direction))
                direction = tuple(component / length for component in direction)
                hit = _trace_voxel(eye, direction, blocks, bounds)
                if hit is None:
                    continue
                name, distance, face = hit
                base = _COLORS.get(name, (126, 132, 140))
                face_light = (0.88, 0.7, 1.0)[face]
                fog = max(0.3, 1.0 - distance / 40.0)
                color = tuple(int(channel * face_light * fog + (145, 190, 225)[i] * (1.0 - fog)) for i, channel in enumerate(base))
                offset = (y * width + x) * 3
                pixels[offset:offset + 3] = bytes(color)

    image = Image.frombytes("RGB", (width, height), bytes(pixels))
    draw = ImageDraw.Draw(image)
    cx, cy = width // 2, height // 2
    draw.line((cx - 3, cy, cx + 3, cy), fill=(235, 235, 235), width=1)
    draw.line((cx, cy - 3, cx, cy + 3), fill=(235, 235, 235), width=1)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    jpeg = buffer.getvalue()
    metadata = {
        "sequence": snapshot.get("sequence"),
        "width": width,
        "height": height,
        "bytes": len(jpeg),
        "image_sha256": hashlib.sha256(jpeg).hexdigest(),
        "render_ms": round((time.perf_counter() - started) * 1000, 3),
        "renderer": "python_voxel_raycast",
        "camera": "prismarine-viewer-compatible first-person convention",
        "source": snapshot.get("source", "mineflayer_loaded_region"),
        "blocks_in_frame_source": len(blocks),
        "physics_tick_p95_ms": snapshot.get("physics_tick_p95_ms"),
        "capture_ms": snapshot.get("capture_ms"),
    }
    return jpeg, metadata


def publish_frame(session, snapshot: dict[str, Any], run_id: str) -> tuple[bytes, dict[str, Any]]:
    """Render and atomically publish the one current frame for a run."""
    jpeg, metadata = render_first_person(snapshot)
    metadata.update({"run_id": run_id, "received_at_monotonic": time.monotonic()})
    with session.lock:
        session.current_image_jpeg = jpeg
        session.current_image_meta = metadata
    if getattr(session, "events", None):
        session.events.append("viewer_frame", {key: value for key, value in metadata.items() if key != "received_at_monotonic"})
    return jpeg, metadata


def _trace_voxel(
    origin: tuple[float, float, float],
    direction: tuple[float, float, float],
    blocks: dict[tuple[int, int, int], str],
    bounds: tuple[tuple[int, int], tuple[int, int], tuple[int, int]],
    *,
    max_distance: float = 24.0,
) -> tuple[str, float, int] | None:
    cell = [math.floor(value) for value in origin]
    step = [1 if value > 0 else -1 for value in direction]
    t_delta = [abs(1.0 / value) if abs(value) > 1e-9 else math.inf for value in direction]
    t_max = [
        ((cell[i] + 1 - origin[i]) / direction[i]) if direction[i] > 0 else
        ((origin[i] - cell[i]) / -direction[i]) if direction[i] < 0 else math.inf
        for i in range(3)
    ]
    distance = 0.0
    face = 0
    for _ in range(64):
        if any((cell[i] < bounds[i][0] and step[i] < 0) or (cell[i] > bounds[i][1] and step[i] > 0) for i in range(3)):
            return None
        block_name = blocks.get(tuple(cell))
        if block_name:
            return block_name, distance, face
        axis = min(range(3), key=t_max.__getitem__)
        distance = t_max[axis]
        if distance > max_distance:
            return None
        cell[axis] += step[axis]
        t_max[axis] += t_delta[axis]
        face = axis
    return None
