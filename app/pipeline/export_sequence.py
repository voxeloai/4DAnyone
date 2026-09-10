"""Export K synchronized timesteps of a 4DAnyone result as per-timestep Nerfstudio datasets.

Upstream's ``scripts/export_nerfstudio.py`` exports ONE timestep and re-decodes every target video
from the start to reach it. For a 4D sequence we need dozens of timesteps, so this module decodes
each target video once, keeps only the wanted frames, and writes one dataset per timestep in the
exact layout upstream's exporter produces (``transforms.json`` + ``images/`` + ``masks/`` +
``sparse_pcd.ply``), plus a per-frame ``mask_path`` so any nerfstudio-format trainer (Spirula
Studio, nerfstudio, gsplat) can pick the masks up without guessing.

Frame directories are numbered from 1 (``frame_0001`` ...) to match the PlayCanvas flipbook
``{frame:04}`` convention downstream.

Usage (inside the 4DAnyone venv, cwd = repo root):
    python -m app.pipeline.export_sequence --data_dir <4danyone-output> --output_dir <seq-dir> \
        --num_timesteps 30 --model_dir /workspace/models
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from PIL import Image

from fdanyone.assets import resolve_foreground_model
from fdanyone.config import INFERENCE
from fdanyone.device import select_cuda_device
from fdanyone.download import ensure_foreground_model
from fdanyone.errors import FourDAnyoneError
from fdanyone.foreground import predict_foreground_masks
from fdanyone.io import write_json
from fdanyone.nerfstudio.exporter import (
    NERFSTUDIO_MASK_THRESHOLD,
    _camera_records,
    _dense_video_paths,
    _read_cameras,
    _transforms,
    _validate_raster,
)
from fdanyone.nerfstudio.visual_hull import (
    NERFSTUDIO_POINT_CLOUD,
    build_sparse_point_cloud,
    write_sparse_point_cloud,
)
from fdanyone.output_directory import read_output_metadata


def pick_timesteps(num_frames: int, count: int) -> list[int]:
    """Evenly spaced source frame indices, always including the first and last frame."""

    count = max(1, min(count, num_frames))
    if count == 1:
        return [0]
    picks = np.round(np.linspace(0, num_frames - 1, count)).astype(int)
    return sorted(set(int(p) for p in picks))


def decode_frames(video_path: Path, wanted: set[int]) -> dict[int, np.ndarray]:
    """Decode one video once and keep only the wanted frame indices."""

    frames: dict[int, np.ndarray] = {}
    if not wanted:
        return frames
    last = max(wanted)
    with av.open(str(video_path), mode="r") as container:
        streams = container.streams.video
        if len(streams) != 1:
            raise FourDAnyoneError(f"Expected one video stream in {video_path}.")
        for index, frame in enumerate(container.decode(streams[0])):
            if index in wanted:
                frames[index] = frame.to_ndarray(format="rgb24")
            if index >= last:
                break
    missing = sorted(wanted - frames.keys())
    if missing:
        raise FourDAnyoneError(f"Video {video_path} has no frames {missing[:5]}.")
    return frames


def probe_fps(video_path: Path) -> Fraction:
    with av.open(str(video_path), mode="r") as container:
        stream = container.streams.video[0]
        for value in (stream.average_rate, stream.guessed_rate, stream.base_rate):
            if value is not None and value > 0:
                return Fraction(value.numerator, value.denominator)
    return Fraction(25, 1)


def emit(event: dict) -> None:
    """Progress lines for the job runner (one JSON object per line on stdout)."""

    print(json.dumps(event), flush=True)


def export_sequence(
    data_dir: str,
    output_dir: str,
    num_timesteps: int = 30,
    timesteps: list[int] | None = None,
    model_dir: str = "models",
    device: str = "cuda:0",
) -> dict:
    result = Path(data_dir).expanduser().resolve()
    if not result.is_dir():
        raise FourDAnyoneError(f"4DAnyone result does not exist: {result}")
    read_output_metadata(result)
    cameras = _camera_records(_read_cameras(result))
    videos = _dense_video_paths(result, cameras)
    base = _transforms(cameras)
    for frame in base["frames"]:
        camera_id = int(Path(frame["file_path"]).stem)
        frame["mask_path"] = f"masks/{camera_id:02d}.png"

    picks = sorted(set(int(t) for t in timesteps)) if timesteps else pick_timesteps(INFERENCE.num_frames, num_timesteps)
    for t in picks:
        if not 0 <= t < INFERENCE.num_frames:
            raise FourDAnyoneError(f"timestep {t} outside [0, {INFERENCE.num_frames - 1}]")

    out = Path(output_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    fps = probe_fps(videos[0])

    started = time.monotonic()
    emit({"event": "decode", "cameras": len(cameras), "timesteps": len(picks)})
    frames_by_camera: list[dict[int, np.ndarray]] = []
    wanted = set(picks)
    for camera_id, video in enumerate(videos):
        frames_by_camera.append(decode_frames(video, wanted))
        emit({"event": "decoded", "camera": camera_id, "total": len(videos)})
    emit({"event": "decode_done", "seconds": round(time.monotonic() - started, 1)})

    device, _ = select_cuda_device(device)
    ensure_foreground_model(model_dir)
    foreground_model = resolve_foreground_model(model_dir)

    written = []
    for index, t in enumerate(picks):
        frame_started = time.monotonic()
        frame_dir = out / f"frame_{index + 1:04d}"
        if (frame_dir / "transforms.json").is_file():
            emit({"event": "frame", "index": index, "total": len(picks), "timestep": t, "skipped": True})
            written.append({"frame": index + 1, "timestep": t, "dir": frame_dir.name})
            continue
        images = tuple(frames_by_camera[camera_id][t] for camera_id in range(len(cameras)))
        for image, camera in zip(images, cameras, strict=True):
            _validate_raster(image, camera)
        masks = predict_foreground_masks(images, foreground_model, device)
        expected_shape = (len(images), *images[0].shape[:2])
        if masks.dtype != np.uint8 or masks.shape != expected_shape:
            raise FourDAnyoneError(f"BiRefNet returned masks {masks.shape} {masks.dtype}; expected uint8 {expected_shape}.")
        binary = masks >= NERFSTUDIO_MASK_THRESHOLD
        empty = [camera_id for camera_id in range(len(images)) if not np.any(binary[camera_id])]
        if empty:
            raise FourDAnyoneError(f"BiRefNet found no foreground in cameras {empty} at timestep {t}.")

        work = out / f".{frame_dir.name}.tmp"
        if work.exists():
            import shutil

            shutil.rmtree(work)
        (work / "images").mkdir(parents=True)
        (work / "masks").mkdir()
        for camera_id, image in enumerate(images):
            Image.fromarray(image, mode="RGB").save(work / "images" / f"{camera_id:02d}.png", format="PNG", compress_level=1)
            Image.fromarray((binary[camera_id] * 255).astype(np.uint8)).save(
                work / "masks" / f"{camera_id:02d}.png", format="PNG", compress_level=1
            )
        points, colors = build_sparse_point_cloud(images, binary, cameras, device)
        write_sparse_point_cloud(work / NERFSTUDIO_POINT_CLOUD, points, colors)
        transforms = dict(base)
        transforms["timestep"] = t
        write_json(work / "transforms.json", transforms, sort_keys=False)
        work.rename(frame_dir)
        written.append({"frame": index + 1, "timestep": t, "dir": frame_dir.name, "points": int(len(points))})
        emit(
            {
                "event": "frame",
                "index": index,
                "total": len(picks),
                "timestep": t,
                "points": int(len(points)),
                "seconds": round(time.monotonic() - frame_started, 1),
            }
        )

    summary = {
        "source_result": str(result),
        "num_cameras": len(cameras),
        "num_frames_source": INFERENCE.num_frames,
        "source_fps": [fps.numerator, fps.denominator],
        "timesteps": picks,
        "frames": written,
        "seconds": round(time.monotonic() - started, 1),
    }
    write_json(out / "sequence.json", summary, sort_keys=False)
    emit({"event": "done", "frames": len(written), "seconds": summary["seconds"]})
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data_dir", required=True)
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--num_timesteps", type=int, default=30)
    parser.add_argument("--timesteps", type=str, default=None, help="comma-separated explicit source frame indices")
    parser.add_argument("--model_dir", default="models")
    parser.add_argument("--device", default="cuda:0")
    args = parser.parse_args()
    explicit = [int(x) for x in args.timesteps.split(",")] if args.timesteps else None
    try:
        export_sequence(
            args.data_dir,
            args.output_dir,
            num_timesteps=args.num_timesteps,
            timesteps=explicit,
            model_dir=args.model_dir,
            device=args.device,
        )
    except FourDAnyoneError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
