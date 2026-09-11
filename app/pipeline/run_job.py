"""Run one job end to end: video -> 4DAnyone multi-view videos -> per-timestep 3DGS -> SOG scan.

Invoked by the API's worker loop as a subprocess:
    python -m app.pipeline.run_job <job_id>

Reads  $FDA_APPDATA/jobs/<job_id>/job.json   (written by the API at submit time)
Writes $FDA_APPDATA/jobs/<job_id>/state.json  (stage / progress / message, atomically, every step)
       $FDA_APPDATA/jobs/<job_id>/log.txt     (append-only, everything the stages print)
       $FDA_APPDATA/scans/<scan_id>/          (the published scan; see pack.py)

Stages: probe -> generate -> export -> train -> pack -> done.  A failed job keeps everything it
produced so it can be diagnosed; re-running the same job id reuses completed stages.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import time
import traceback
from fractions import Fraction
from pathlib import Path

import av

from app.pipeline.presets import get_preset

REPO = Path(os.environ.get("FDA_REPO", Path(__file__).resolve().parents[2]))
APPDATA = Path(os.environ.get("FDA_APPDATA", "/workspace/fdanyone-app"))
MODELS = os.environ.get("FDA_MODELS", "/workspace/models")
GVHMR_ROOT = str(REPO / "third_party" / "GVHMR")
NUM_FRAMES = 121


class JobContext:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.job_dir = APPDATA / "jobs" / job_id
        self.job = json.loads((self.job_dir / "job.json").read_text())
        self.state_path = self.job_dir / "state.json"
        self.log_path = self.job_dir / "log.txt"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.is_file() else {}
        self._log_fh = self.log_path.open("a", encoding="utf-8")

    def log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"{stamp} {message}"
        self._log_fh.write(line + "\n")
        self._log_fh.flush()
        print(line, flush=True)

    def set_state(self, **fields) -> None:
        self.state.update(fields)
        self.state["updated"] = time.time()
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        os.replace(tmp, self.state_path)

    def stage(self, name: str, progress: float, message: str = "") -> None:
        self.set_state(status="running", stage=name, progress=round(progress, 4), message=message)
        self.log(f"[{name}] {message}" if message else f"[{name}]")


def probe_video(path: Path) -> dict:
    with av.open(str(path), mode="r") as container:
        stream = container.streams.video[0]
        rate = None
        for value in (stream.average_rate, stream.guessed_rate, stream.base_rate):
            if value is not None and value > 0:
                rate = Fraction(value.numerator, value.denominator)
                break
        if rate is None:
            raise ValueError("video declares no usable frame rate")
        frames = int(stream.frames or 0)
        duration = float(stream.duration * stream.time_base) if stream.duration and stream.time_base else None
        if frames <= 0:
            frames = sum(1 for _ in container.decode(stream))
        width, height = stream.codec_context.width, stream.codec_context.height
        # Phones store orientation as a display matrix (PyAV exposes it as frame.rotation), older files
        # as a 'rotate' tag. 4DAnyone's decoder honours both; mirror that here so the portrait check is right.
        rotation = int(round(float(stream.metadata.get("rotate", "0") or 0))) % 360
        try:
            first = next(iter(container.decode(stream)))
            frame_rotation = getattr(first, "rotation", 0) or 0
            if float(frame_rotation) != 0.0:
                rotation = int(round(float(frame_rotation))) % 360
        except Exception:  # noqa: BLE001 - orientation is advisory only
            pass
    if rotation in (90, 270):
        width, height = height, width
    return {
        "fps": float(rate),
        "fps_fraction": [rate.numerator, rate.denominator],
        "frames": frames,
        "duration_seconds": duration if duration else frames / float(rate),
        "width": width,
        "height": height,
        "portrait": height >= width,
    }


def required_source_frames(rate: Fraction) -> tuple[int, Fraction]:
    from fdanyone.video import choose_canonical_fps

    canonical = choose_canonical_fps(rate)
    multiple = rate / canonical
    return int(math.ceil(NUM_FRAMES * float(multiple))), canonical


def run_generate(ctx: JobContext, video: Path, preset: dict, out_dir: Path) -> None:
    if (out_dir / "cameras.json").is_file() and (out_dir / "videos" / "dense").is_dir():
        ctx.log("[generate] reusing existing 4DAnyone output")
        return
    cmd = [
        sys.executable, "inference.py",
        "--video_path", str(video),
        "--output_dir", str(out_dir),
        "--views_per_layer", str(preset["views_per_layer"]),
        "--layer_pitches", json.dumps(preset["layer_pitches"]).replace(" ", ""),
        "--model_dir", MODELS,
        "--gvhmr_root", GVHMR_ROOT,
        "--attention_backend", os.environ.get("FDA_ATTENTION", "sdpa"),
        "--enable_turbo", "True",
        "--seed", str(int(ctx.job.get("seed", 42))),
    ]
    start_time = float(ctx.job.get("start_time", 0.0) or 0.0)
    if start_time > 0:
        cmd += ["--start_time", str(start_time)]
    ctx.log("[generate] " + " ".join(cmd))
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    started = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=str(REPO), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    assert proc.stdout is not None
    eta = preset["eta_minutes"] * 60 * 0.55  # generation is roughly half of a job's wall time
    last_error = None
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        ctx.log(f"[4danyone] {line}")
        if line.startswith("error:") or "Error:" in line or "Traceback" in line:
            last_error = line
        frac = min(0.95, (time.monotonic() - started) / max(60.0, eta))
        ctx.set_state(progress=round(0.05 + 0.45 * frac, 4), message=line[-160:])
    code = proc.wait()
    if code != 0:
        detail = f": {last_error[:300]}" if last_error else "; see log"
        raise RuntimeError(f"4DAnyone inference failed (exit {code}){detail}")
    if not (out_dir / "cameras.json").is_file():
        raise RuntimeError("4DAnyone finished but cameras.json is missing")
    ctx.log(f"[generate] done in {time.monotonic() - started:.0f}s")


def run_export(ctx: JobContext, result_dir: Path, seq_dir: Path, preset: dict) -> dict:
    if (seq_dir / "sequence.json").is_file():
        ctx.log("[export] reusing existing sequence export")
        return json.loads((seq_dir / "sequence.json").read_text())
    cmd = [
        sys.executable, "-m", "app.pipeline.export_sequence",
        "--data_dir", str(result_dir),
        "--output_dir", str(seq_dir),
        "--num_timesteps", str(preset["timesteps"]),
        "--model_dir", MODELS,
    ]
    ctx.log("[export] " + " ".join(cmd))
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(cmd, cwd=str(REPO), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                event = None
            if event and event.get("event") == "frame":
                frac = (event["index"] + 1) / max(1, event["total"])
                ctx.set_state(progress=round(0.50 + 0.12 * frac, 4), message=f"masks + visual hull {event['index'] + 1}/{event['total']}")
            elif event and event.get("event") == "decoded":
                ctx.set_state(message=f"decoding view {event['camera'] + 1}/{event['total']}")
            continue
        ctx.log(f"[export] {line}")
    code = proc.wait()
    if code != 0:
        raise RuntimeError(f"sequence export failed (exit {code}); see log")
    return json.loads((seq_dir / "sequence.json").read_text())


def run_train(ctx: JobContext, seq_dir: Path, train_dir: Path, preset: dict, summary: dict) -> list[Path]:
    from app.pipeline.train_spirula import train_frame

    plys: list[Path] = []
    frames = summary["frames"]
    total_secs = 0.0
    for i, entry in enumerate(frames):
        name = entry["dir"]
        ply, secs = train_frame(
            seq_dir / name, train_dir, name,
            iterations=preset["iterations"], cap_max=preset["cap_max"], sh_degree=preset["sh_degree"],
            res_divisor=preset["res_divisor"], log=ctx.log,
        )
        total_secs += secs
        plys.append(ply)
        per = total_secs / (i + 1) if secs else None
        remaining = f", ~{per * (len(frames) - i - 1) / 60:.1f} min left" if per else ""
        ctx.set_state(progress=round(0.62 + 0.30 * (i + 1) / len(frames), 4), message=f"trained timestep {i + 1}/{len(frames)}{remaining}")
    ctx.log(f"[train] {len(plys)} timesteps in {total_secs / 60:.1f} min")
    return plys


def main(job_id: str) -> int:
    ctx = JobContext(job_id)
    ctx.set_state(status="running", started=ctx.state.get("started") or time.time(), error=None)
    try:
        preset = get_preset(ctx.job.get("preset"))
        video = Path(ctx.job["video"])
        title = ctx.job.get("title") or video.stem
        scan_id = ctx.job.get("scan_id") or job_id
        work = ctx.job_dir / "work"
        work.mkdir(exist_ok=True)

        ctx.stage("probe", 0.01, f"probing {video.name}")
        info = probe_video(video)
        rate = Fraction(*info["fps_fraction"])
        needed, canonical = required_source_frames(rate)
        info["canonical_fps"] = float(canonical)
        info["required_frames"] = needed
        ctx.set_state(source=info)
        ctx.log(f"[probe] {info}")
        if info["frames"] < needed:
            raise ValueError(
                f"Clip too short: {info['frames']} frames at {info['fps']:.2f} fps; 4DAnyone needs {needed} "
                f"({needed / info['fps']:.1f} s). Record at least 5 seconds."
            )
        if not info["portrait"]:
            ctx.log("[probe] WARNING: landscape clip; 4DAnyone expects a 9:16 portrait shot of one person")

        ctx.stage("generate", 0.05, f"4DAnyone: {preset['views_per_layer'] * len(preset['layer_pitches'])} views, turbo")
        result_dir = work / "fdanyone"
        run_generate(ctx, video, preset, result_dir)

        ctx.stage("export", 0.50, "masks + visual hull per timestep")
        seq_dir = work / "sequence"
        summary = run_export(ctx, result_dir, seq_dir, preset)

        ctx.stage("train", 0.62, f"Spirula Studio, {len(summary['frames'])} timesteps")
        train_dir = work / "train"
        train_dir.mkdir(exist_ok=True)
        plys = run_train(ctx, seq_dir, train_dir, preset, summary)

        ctx.stage("pack", 0.92, "PLY -> SOG + manifest")
        from app.pipeline.pack import pack_sequence

        scan_dir = APPDATA / "scans" / scan_id
        staging = APPDATA / "scans" / f".{scan_id}.tmp"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        cameras = json.loads((result_dir / "cameras.json").read_text())
        source = {
            "filename": ctx.job.get("original_filename") or video.name,
            "video": "source.mp4" if video.suffix.lower() == ".mp4" else video.name,
            **{k: info[k] for k in ("fps", "frames", "duration_seconds", "width", "height")},
        }
        def pack_progress(index: int, total: int, seconds_left: float) -> None:
            left = f", ~{seconds_left / 60:.1f} min left" if seconds_left > 0 else ""
            ctx.set_state(progress=round(0.92 + 0.07 * index / max(1, total), 4), message=f"packed frame {index}/{total}{left}")

        manifest = pack_sequence(
            plys, staging, scan_id=scan_id, title=title, preset=preset, sequence_summary=summary,
            result_dir=result_dir, cameras=cameras, source=source, log=ctx.log, progress=pack_progress,
        )
        shutil.copyfile(video, staging / source["video"])
        manifest["job_id"] = job_id
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2))
        if scan_dir.exists():
            shutil.rmtree(scan_dir)
        os.replace(staging, scan_dir)

        ctx.set_state(status="done", stage="done", progress=1.0, message="ready", scan_id=scan_id, finished=time.time())
        ctx.log(f"[done] scan {scan_id} published ({manifest['stats']['bytes_total'] / 1e6:.1f} MB)")
        return 0
    except Exception as exc:  # noqa: BLE001 - the job runner must record every failure
        ctx.log("[failed] " + "".join(traceback.format_exception(exc)).rstrip())
        ctx.set_state(status="failed", error=str(exc)[:500], finished=time.time())
        return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m app.pipeline.run_job <job_id>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
