"""Pack a sequence of trained PLYs into a web-ready scan: SOG frames + manifest + poster.

SOG (PlayCanvas' self-organising-gaussian bundle) is ~10-20x smaller than PLY and streams frame by
frame through the engine's GSplatFlipbook script. Conversion uses @playcanvas/splat-transform.

Scan layout (served as static files by the API):
    scans/<scan_id>/
      manifest.json          everything a client needs (see below)
      poster.jpg             front view at t=0
      preview.mp4            the generated front-view video (gallery hover)
      frames/frame_0001.sog  ... frame_NNNN.sog
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import av
import numpy as np
from PIL import Image

MANIFEST_VERSION = 1


def splat_transform_binary() -> str:
    for cand in (os.environ.get("FDA_SPLAT_TRANSFORM"), "/workspace/node/bin/splat-transform", shutil.which("splat-transform")):
        if cand and Path(cand).is_file():
            return str(cand)
    raise FileNotFoundError("splat-transform not found (npm i -g @playcanvas/splat-transform)")


def ply_to_sog(ply: Path, sog: Path, log) -> float:
    started = time.monotonic()
    # SOG compression can use a GPU adapter; headless pods have no usable one for node, so default to CPU.
    cmd = [splat_transform_binary(), "-q", "-w", "-g", os.environ.get("FDA_SPLAT_GPU", "cpu"), str(ply), str(sog)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not sog.is_file():
        log(proc.stdout[-2000:])
        log(proc.stderr[-2000:])
        raise RuntimeError(f"splat-transform failed for {ply.name} (exit {proc.returncode})")
    return time.monotonic() - started


def read_ply_positions(ply: Path, max_points: int = 400_000) -> np.ndarray:
    """Positions (N,3) from a binary little-endian 3DGS PLY (subsampled) for framing stats."""

    with ply.open("rb") as fh:
        header: list[str] = []
        while True:
            line = fh.readline()
            if not line:
                raise ValueError(f"{ply}: truncated header")
            header.append(line.decode("ascii", "replace").strip())
            if header[-1] == "end_header":
                break
        count = 0
        props: list[tuple[str, str]] = []
        in_vertex = False
        for h in header:
            parts = h.split()
            if parts[:2] == ["element", "vertex"]:
                count = int(parts[2])
                in_vertex = True
            elif parts[0] == "element":
                in_vertex = False
            elif parts[0] == "property" and in_vertex and len(parts) == 3:
                props.append((parts[1], parts[2]))
        if "binary_little_endian" not in " ".join(header):
            raise ValueError(f"{ply}: expected binary_little_endian")
        type_map = {"float": "<f4", "float32": "<f4", "double": "<f8", "uchar": "u1", "uint8": "u1", "int": "<i4", "uint": "<u4", "short": "<i2", "ushort": "<u2", "char": "i1"}
        dtype = np.dtype([(name, type_map[t]) for t, name in props])
        data = np.fromfile(fh, dtype=dtype, count=count)
    xyz = np.stack([data["x"], data["y"], data["z"]], axis=1).astype(np.float32)
    if len(xyz) > max_points:
        idx = np.random.default_rng(0).choice(len(xyz), max_points, replace=False)
        xyz = xyz[idx]
    return xyz


def framing_from_positions(xyz: np.ndarray) -> dict:
    """Robust centre + radius (median / 97th percentile, floaters ignored)."""

    centre = np.median(xyz, axis=0)
    dist = np.linalg.norm(xyz - centre, axis=1)
    radius = float(np.percentile(dist, 97.0))
    lo = np.percentile(xyz, 1.0, axis=0)
    hi = np.percentile(xyz, 99.0, axis=0)
    return {
        "center": [float(v) for v in centre],
        "radius": max(radius, 1e-3),
        "bounds_min": [float(v) for v in lo],
        "bounds_max": [float(v) for v in hi],
    }


def write_poster(video: Path, poster: Path, max_side: int = 900) -> None:
    with av.open(str(video), mode="r") as container:
        for frame in container.decode(container.streams.video[0]):
            image = Image.fromarray(frame.to_ndarray(format="rgb24"), mode="RGB")
            break
        else:
            raise ValueError(f"{video}: no frames")
    image.thumbnail((max_side, max_side))
    image.save(poster, format="JPEG", quality=86, optimize=True)


def pack_sequence(
    plys: list[Path],
    scan_dir: Path,
    *,
    scan_id: str,
    title: str,
    preset: dict,
    sequence_summary: dict,
    result_dir: Path,
    cameras: dict,
    source: dict,
    log,
) -> dict:
    """Convert every PLY to SOG under scan_dir/frames and write manifest.json."""

    frames_dir = scan_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    gaussians = []
    bytes_total = 0
    for index, ply in enumerate(plys, start=1):
        sog = frames_dir / f"frame_{index:04d}.sog"
        if not sog.is_file():
            secs = ply_to_sog(ply, sog, log)
            log(f"[pack] frame {index}/{len(plys)}: {sog.name} {sog.stat().st_size / 1e6:.1f} MB in {secs:.1f}s")
        bytes_total += sog.stat().st_size
        if index == 1 or index == len(plys) or index % 8 == 0:
            xyz = read_ply_positions(ply)
            gaussians.append(len(xyz))
    framing = framing_from_positions(read_ply_positions(plys[0]))

    # poster + preview from the front target view (camera 00 = yaw 0)
    front = result_dir / "videos" / "dense" / "00.mp4"
    if front.is_file():
        write_poster(front, scan_dir / "poster.jpg")
        shutil.copyfile(front, scan_dir / "preview.mp4")

    fps_num, fps_den = sequence_summary.get("source_fps", [25, 1])
    source_fps = fps_num / fps_den
    num_source = sequence_summary.get("num_frames_source", 121)
    timesteps = sequence_summary.get("timesteps", list(range(len(plys))))
    duration = num_source / source_fps
    playback_fps = len(plys) / duration if duration > 0 else 10.0

    cam_list = cameras.get("cameras", []) if isinstance(cameras, dict) else []
    front_pos = None
    if cam_list:
        c2w = np.asarray(cam_list[0].get("camera_to_world"), dtype=float)
        if c2w.shape == (4, 4):
            # 4DAnyone world is Y-up; the exported splats are in the Nerfstudio Z-up frame.
            y_up_to_z_up = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], dtype=float)
            front_pos = [float(v) for v in (y_up_to_z_up @ c2w[:3, 3])]

    manifest = {
        "version": MANIFEST_VERSION,
        "id": scan_id,
        "title": title,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "engine": {"generator": "4DAnyone (ant-research)", "reconstruction": "Spirula Studio per-timestep 3DGS", "packer": "playcanvas splat-transform"},
        "preset": {k: preset[k] for k in ("name", "label", "views_per_layer", "layer_pitches", "timesteps", "iterations", "cap_max", "sh_degree", "res_divisor")},
        "source": source,
        "sequence": {
            "folder": "frames",
            "pattern": "frame_{frame:04}.sog",
            "start": 1,
            "end": len(plys),
            "count": len(plys),
            "fps": round(playback_fps, 3),
            "source_fps": round(source_fps, 3),
            "source_frames": num_source,
            "duration_seconds": round(duration, 3),
            "timesteps": timesteps,
        },
        "world": {
            "up": [0, 0, 1],
            "convention": "nerfstudio (OpenGL c2w, Z-up)",
            "front_camera_position": front_pos,
            **framing,
        },
        "stats": {
            "gaussians_per_frame": int(np.mean(gaussians)) if gaussians else None,
            "bytes_total": bytes_total,
            "bytes_per_frame": int(bytes_total / max(1, len(plys))),
            "pack_seconds": round(time.monotonic() - started, 1),
        },
        "assets": {"poster": "poster.jpg", "preview": "preview.mp4"},
    }
    (scan_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    log(f"[pack] manifest written: {len(plys)} frames, {bytes_total / 1e6:.1f} MB total, playback {playback_fps:.2f} fps")
    return manifest
