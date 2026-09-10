"""Train + pack a synthetic sequence (from tools/synth_dataset.py) into a gallery scan.

    python tools/synth_pipeline.py --seq /workspace/test/synth_seq --scan_id synthtest \
        --title "Synthetic test" --preset standard [--iterations 2000]

Uses the exact production code path (train_spirula.train_frame + pack.pack_sequence), so what you
see in the gallery is what a real job would produce, minus the 4DAnyone generation stage.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path

from app.pipeline.pack import pack_sequence
from app.pipeline.presets import get_preset
from app.pipeline.train_spirula import train_frame


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seq", required=True)
    ap.add_argument("--scan_id", required=True)
    ap.add_argument("--title", default="Synthetic test")
    ap.add_argument("--preset", default="standard")
    ap.add_argument("--iterations", type=int, default=None)
    ap.add_argument("--train_dir", default=None)
    ap.add_argument("--fresh", action="store_true", help="delete previous training runs first")
    args = ap.parse_args()

    seq = Path(args.seq)
    preset = get_preset(args.preset)
    iterations = args.iterations or preset["iterations"]
    train_dir = Path(args.train_dir or (seq.parent / f"{seq.name}_train"))
    if args.fresh and train_dir.exists():
        shutil.rmtree(train_dir)
    train_dir.mkdir(parents=True, exist_ok=True)
    summary = json.loads((seq / "sequence.json").read_text())
    started = time.monotonic()
    plys = []
    for entry in summary["frames"]:
        ply, secs = train_frame(
            seq / entry["dir"], train_dir, entry["dir"],
            iterations=iterations, cap_max=preset["cap_max"], sh_degree=preset["sh_degree"], res_divisor=preset["res_divisor"], log=print,
        )
        plys.append(ply)
    print(f"trained {len(plys)} frames in {time.monotonic() - started:.0f}s")

    appdata = Path(os.environ.get("FDA_APPDATA", "/workspace/fdanyone-app"))
    scan_dir = appdata / "scans" / args.scan_id
    if scan_dir.exists():
        shutil.rmtree(scan_dir)
    scan_dir.mkdir(parents=True)
    cameras = json.loads((seq / "cameras.json").read_text())
    manifest = pack_sequence(
        plys, scan_dir, scan_id=args.scan_id, title=args.title, preset={**preset, "iterations": iterations},
        sequence_summary=summary, result_dir=seq, cameras=cameras,
        source={"filename": "synthetic", "video": "", "fps": 25, "frames": 121, "duration_seconds": 4.84, "width": 704, "height": 1280},
        log=print,
    )
    print(json.dumps({k: manifest[k] for k in ("id", "stats", "world")}, indent=1))


if __name__ == "__main__":
    main()
