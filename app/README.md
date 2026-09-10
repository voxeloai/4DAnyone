# Voxelo 4D prototype (4DAnyone → 4D Gaussian splat → web)

A person in a five-second phone clip, turned into a streamable 4D Gaussian splat you can orbit in
any browser or phone. Built on the `voxelo/main` branch of the 4DAnyone fork; runs on one RunPod
H100 pod under architect's `runpod-persistent-gpu-pod` pattern.

```
video ──▶ 4DAnyone (multi-view video diffusion) ──▶ N synchronised orbit videos + cameras.json
      ──▶ app/pipeline/export_sequence.py   K timesteps → masks (BiRefNet) + visual hull + transforms.json
      ──▶ app/pipeline/train_spirula.py     Spirula Studio `train centered-object` per timestep → PLY
      ──▶ app/pipeline/pack.py              splat-transform PLY → SOG, manifest.json, poster, preview
      ──▶ app/api (FastAPI)                 /api/v1 jobs + scans, static /scans, web client
      ──▶ app/web (PlayCanvas 2.22)         GSplatFlipbook streams the SOG sequence; CameraControls orbit
```

## Why it is shaped like this

- **4DAnyone does not produce a splat.** It produces N view-consistent videos of the person
  (24 views on one orbit by default) plus exact cameras. The 4DGS reconstruction is explicitly not
  released ("evaluating open-source alternatives"). So reconstruction is ours.
- **Per-timestep 3DGS with a fixed world frame.** Every timestep is trained independently from the
  same camera rig, in the same coordinate frame (`--orientation-method none --center-method none`),
  seeded from that timestep's visual hull. A sequence of static splats is exactly what the web can
  stream today (PlayCanvas' `GSplatFlipbook`), and it keeps the trainer a black box we can swap
  (Spirula Studio now; gsplat or a true deformable 4DGS later) without touching the web contract.
- **Spirula Studio** (Vlad's call): one self-contained binary, the trainer Voxelo already benches
  against, nerfstudio-format input with masks, fast fused kernels. Prebuilt Vulkan release runs on
  the pod once the NVIDIA Vulkan ICD is present (`scripts/bootstrap.sh` handles it).
- **SOG + flipbook** = compression + streamability. SOG is ~5-8 bytes per gaussian (a 250k-splat
  frame is ~1.5-2 MB); the flipbook preloads a few frames ahead and releases frames behind, so a
  60-frame scan never has to be fully resident.
- **The manifest is the product boundary.** `app/API.md` documents everything a client needs. The
  web client is plain ES modules with no build step; an iOS app would read the same manifest and
  render the same SOG frames (PlayCanvas → SuperSplat / native via splat-transform to PLY/SPZ).

## Running it (on the pod)

```bash
bash /workspace/4DAnyone/scripts/bootstrap.sh     # idempotent: venv, weights, node, spirula deps
source /workspace/activate.sh
bash scripts/serve.sh                              # API + web on :8000 (tmux 'api')
```

Public URL: `https://<pod-id>-8000.proxy.runpod.net/`. Health: `/api/v1/health`.
Spirula binary: `/workspace/spirula/spirula` (release 2026.9.10, Vulkan). Verify the H100 is the
Vulkan device with `vulkaninfo --summary` after boot; the ICD JSON is written by bootstrap.

SMPL-X is licence-gated and must be installed by the licensee: `scripts/push_smplx.ps1 -Archive
<models_smplx_v1_1.zip>` from the laptop, or copy the ZIP to `/workspace/smplx/` and re-run
bootstrap. Until then `/api/v1/health.ready` is false and jobs fail at the generate stage.

## Presets (`app/pipeline/presets.py`)

| preset | views | timesteps | iterations / frame | cap | notes |
|---|---|---|---|---|---|
| fast | 12 | 16 | 1500 | 150k | half-res training, ~8 min |
| standard | 24 | 30 | 2500 | 300k | default, ~18 min |
| full | 48 (3 pitch rings) | 48 | 3500 | 500k | SH1, ~45 min |

## Self-test without SMPL-X

`tools/synth_dataset.py` renders any 3DGS PLY from a 4DAnyone-style rig with gsplat and writes the
exact per-timestep dataset layout the real exporter produces (with a slow turn + bob so it is a real
sequence). Train + pack it with the same code path:

```bash
python tools/synth_dataset.py --ply /workspace/test/teaser.ply --output_dir /workspace/test/synth_seq --views 24 --timesteps 6 --flip_y
python -m app.pipeline.train_spirula /workspace/test/synth_seq/frame_0001 /workspace/test/train frame_0001 2000
```

## Layout

```
app/api/main.py            FastAPI: routes, static mounts, single-GPU worker thread
app/pipeline/run_job.py    stage runner (probe → generate → export → train → pack)
app/pipeline/export_sequence.py, train_spirula.py, pack.py, presets.py
app/web/index.html + assets/app.{css,js}         upload / jobs / gallery (Voxelo Design System)
app/web/view.html + assets/viewer.{css,js}       PlayCanvas flipbook viewer, orbit, backgrounds, share
tools/mock_server.py       local UI dev server (no GPU)
tools/synth_dataset.py     recon self-test data
scripts/{deploy.ps1,bootstrap.sh,serve.sh,push_smplx.ps1}
```

## Licences (read before productising)

4DAnyone code + checkpoint Apache-2.0 · GVHMR non-commercial research · SMPL-X research licence ·
Wan2.2 5B Turbo LoRA CC BY-NC-SA 4.0 (Base model without the LoRA is Apache-2.0 but 5.6× slower) ·
Sapiens2 schema notice (prohibits deepfakes, biometric ID) · Spirula Studio GPLv3 (invoked as a
separate binary, not linked) · PlayCanvas MIT · BiRefNet MIT. This is an R&D prototype.
