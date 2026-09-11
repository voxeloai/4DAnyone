# Cycle 1 — handoff

**Date:** 2026-09-11
**Closed by:** architect (drove the pod directly over SSH + tmux)
**Cycle:** 1
**Status:** `in-progress` — everything built and tested except the 4DAnyone generation stage, which is blocked on the licence-gated SMPL-X body model.

## Summary

Pod `fdanyone` (H100 80GB, EUR-IS-3) is bootstrapped with 4DAnyone (21 GB of weights), Spirula
Studio 2026.9.10 (prebuilt Vulkan binary, H100 visible via a hand-written NVIDIA ICD), node +
splat-transform, and the prototype app (FastAPI + Voxelo web client + PlayCanvas 2.22 viewer). The
reconstruction and web halves are verified end to end on a synthetic sequence (6 timesteps, 24
views, 300k gaussians/frame, ~16 s training per timestep, 3.9 MB SOG per frame). The generation
half fails cleanly with "SMPL-X is not installed" until `SMPLX_NEUTRAL.npz` is provided.

## What changed

- Files: `scripts/{deploy.ps1,bootstrap.sh,serve.sh,push_smplx.ps1,prebake.sh}`, `app/**`, `tools/**`, `.architect/**`.
- Pod state on the volume: `/workspace/envs/fdanyone` (py3.11, torch 2.8+cu128, gsplat 1.5.3 JIT-built),
  `/workspace/models` (21 GB), `/workspace/node` (node 22 + splat-transform 3.4.2), `/workspace/spirula/spirula`,
  `/workspace/cloudflared/cloudflared`, `/workspace/fdanyone-app/{uploads,jobs,scans}`, `/workspace/test/*` (synthetic run).
- Sentinels: `/workspace/envs/fdanyone/.ok-v3`; node + weights are presence-gated.
- Ephemeral (re-done by bootstrap each boot): apt packages, uv binary, the NVIDIA Vulkan ICD JSON + libglvnd libs.
  ⚠️ The ICD/libglvnd step is NOT yet in bootstrap.sh (done by hand this cycle) — see RECIPE.md.

## What worked

- `uv venv --python 3.11` + `pip install -r requirements.txt` straight from PyPI (torch 2.8.0 = cu128).
- Spirula Studio prebuilt Linux release on the pod: write `/usr/share/vulkan/icd.d/nvidia_icd.json`
  pointing at `libGLX_nvidia.so.0` and install `libopengl0 libegl1 libgles2 libglvnd0 libgl1 libglx0`;
  `vulkaninfo --summary` lists the H100. `spirula train centered-object --data-format nerfstudio` reads
  the exporter's layout directly (`mask_path` per frame, `ply_file_path` seed).
- `--orientation-method none --center-method none` keeps the dataset frame (verified: trained splat
  centre/bounds match the visual hull), so every timestep shares one world.
- Compressed densification schedule for 1.5k–3.5k-step runs (defaults assume 30k) — without it a
  2000-step run stayed at 41k gaussians; with it the 300k cap is reached.
- SOG via `splat-transform -g cpu` (no GPU adapter needed), ~13 bytes/gaussian at SH0.
- Web: PlayCanvas 2.22 unified gsplat renderer on WebGPU, forked flipbook with retries, CameraControls.
- Public URL via a Cloudflare quick tunnel (`serve.sh`): 16/16 parallel frame downloads OK.

## What didn't

- RunPod's HTTP proxy (`<pod>-8000.proxy.runpod.net`) drops 30–40 % of concurrent requests (404 /
  timeouts) — enough to stall a streamed splat sequence. Kept only as a fallback URL.
- The 4DAnyone SMPL-X installer prompts on stdin whenever it sees a tty; the API worker inherited the
  tmux pty and a job hung. Fixed: every job subprocess gets `stdin=DEVNULL`.
- The commit-signing path via op-ssh-sign fails when the 1Password desktop app is locked; used the
  `op read` + ssh-keygen recipe from `secrets_handling.md`.
- `tar` onto the network volume must use `--no-same-owner` (mfs rejects chown).

## Open questions for architect / Vlad

- SMPL-X: Vlad registers at https://smpl-x.is.tue.mpg.de/, downloads `models_smplx_v1_1.zip`, then
  `.\scripts\push_smplx.ps1 -Archive <zip>` (or drop it in `Architect\env\`, a watcher is armed).
- Keep the pod running for the demo (H100 $3.49/hr) or stop overnight and start 30 min before.
- Preset defaults (24 views / 30 timesteps / 2500 iters) are untested on a real person clip.

## Next-cycle suggestion

Install SMPL-X → `bash scripts/prebake.sh` (3 example clips, standard preset) → verify a real person
scan in the viewer → tune presets from measured timings → stop/start verification → lock RECIPE.md.

## Pointers

- Log: `.architect/log/deploy-2026-09-11.log` (deploy transcript); job logs under `/workspace/fdanyone-app/jobs/<id>/log.txt`.
- Recipe: `.architect/RECIPE.md`. App docs: `app/README.md`, `app/API.md`.
