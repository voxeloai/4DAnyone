"""Train one timestep with Spirula Studio (the `spirula train` CLI) and return the exported PLY.

Spirula Studio (harry7557558/spirula-studio, GPLv3) is a self-contained C++/Vulkan 3DGS trainer.
We feed it the per-timestep Nerfstudio dataset written by ``export_sequence`` and keep the scene in
the dataset's own coordinate frame (``--orientation-method none --center-method none``) so every
timestep of a sequence lands in the same world, which is what makes a splat *sequence* coherent.

The binary is expected at $FDA_SPIRULA (default /workspace/spirula/spirula).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_SPIRULA = "/workspace/spirula/spirula"


def spirula_binary() -> Path:
    path = Path(os.environ.get("FDA_SPIRULA", DEFAULT_SPIRULA))
    if not path.is_file():
        raise FileNotFoundError(f"spirula binary not found at {path} (set FDA_SPIRULA)")
    return path


def build_command(
    frame_dir: Path,
    out_prefix: Path,
    run_name: str,
    *,
    iterations: int,
    cap_max: int,
    sh_degree: int,
    res_divisor: int,
    extra: list[str] | None = None,
) -> list[str]:
    cmd = [
        str(spirula_binary()),
        "train",
        "centered-object",
        "--data", str(frame_dir),
        "--data-format", "nerfstudio",
        "--output-dir-prefix", str(out_prefix),
        "--output-dir-name", run_name,
        "--num-iterations", str(iterations),
        "--cap-max", str(cap_max),
        "--sh-degree", str(sh_degree),
        # masks: train masked-out pixels as empty space, and fill holes inside the silhouette
        "--load-masks", "1",
        "--apply-loss-for-mask", "1",
        "--alpha-loss-weight", "0.1",
        "--alpha-loss-weight-under", "0.2",
        # keep the dataset frame so every timestep shares one world
        "--orientation-method", "none",
        "--center-method", "none",
        # every generated view trains; nothing is held out
        "--eval-mode", "all",
        # synthetic views have constant exposure: no per-photo colour correction
        "--use-bilateral-grid", "0",
        "--use-ppisp", "0",
        # headless
        "--disable-viewer", "1",
        "--keep-viewer-alive", "0",
        "--steps-per-save", str(iterations),
        "--save-only-latest-checkpoint", "1",
    ]
    if res_divisor and res_divisor > 1:
        cmd += ["--train-resolution-divisor", str(res_divisor)]
    if extra:
        cmd += extra
    return cmd


def find_exported_ply(run_dir: Path) -> Path:
    """Spirula writes its splat PLY inside the run directory; pick the newest real one."""

    candidates = [
        p for p in run_dir.rglob("*.ply")
        if "sparse_pcd" not in p.name and p.stat().st_size > 1024
    ]
    if not candidates:
        raise FileNotFoundError(f"no PLY exported under {run_dir}")
    candidates.sort(key=lambda p: (p.stat().st_mtime, p.stat().st_size))
    return candidates[-1]


def train_frame(
    frame_dir: Path,
    out_prefix: Path,
    run_name: str,
    *,
    iterations: int,
    cap_max: int,
    sh_degree: int,
    res_divisor: int,
    log,
    extra: list[str] | None = None,
) -> tuple[Path, float]:
    """Run spirula on one timestep dataset. Returns (ply_path, seconds)."""

    run_dir = out_prefix / run_name
    if run_dir.exists():
        try:
            ply = find_exported_ply(run_dir)
            log(f"[train] {run_name}: reusing {ply.name}")
            return ply, 0.0
        except FileNotFoundError:
            pass
    cmd = build_command(
        frame_dir, out_prefix, run_name,
        iterations=iterations, cap_max=cap_max, sh_degree=sh_degree, res_divisor=res_divisor, extra=extra,
    )
    log(f"[train] {run_name}: {' '.join(cmd)}")
    started = time.monotonic()
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", "/tmp")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    assert proc.stdout is not None
    tail: list[str] = []
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        if len(tail) > 400:
            tail = tail[-200:]
        # keep the job log readable: device line, warnings, errors and every ~10th progress line
        low = line.lower()
        if "device" in low or "error" in low or "warn" in low or "export" in low or "saved" in low or "ply" in low:
            log(f"[spirula] {line}")
    code = proc.wait()
    seconds = time.monotonic() - started
    if code != 0:
        for line in tail[-40:]:
            log(f"[spirula] {line}")
        raise RuntimeError(f"spirula train failed for {run_name} (exit {code})")
    ply = find_exported_ply(run_dir)
    log(f"[train] {run_name}: {ply.relative_to(out_prefix)} in {seconds:.1f}s")
    return ply, seconds


if __name__ == "__main__":  # ad-hoc: python -m app.pipeline.train_spirula <frame_dir> <out_prefix> <name> [iters]
    frame = Path(sys.argv[1])
    prefix = Path(sys.argv[2])
    name = sys.argv[3]
    iters = int(sys.argv[4]) if len(sys.argv) > 4 else 2000
    ply, secs = train_frame(
        frame, prefix, name, iterations=iters, cap_max=300_000, sh_degree=0, res_divisor=1, log=print
    )
    print(ply, f"{secs:.1f}s")
