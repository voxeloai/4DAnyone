# Cycle 1 — 4DAnyone on RunPod H100 + the 4D splat web prototype

**Date:** 2026-09-11
**Set by:** architect
**Cycle:** 1

## Goal

Get `voxeloai/4DAnyone` running on a RunPod H100 80GB pod under `runpod-persistent-gpu-pod`
(bypass-conda variant), add a reconstruction stage that turns the generated multi-view videos into a
streamable 4D Gaussian splat (per-timestep 3DGS via gsplat, packed to a SOG sequence), and serve a
Voxelo-styled upload -> process -> gallery -> PlayCanvas viewer prototype from the pod.

## Verification

- `bash scripts/bootstrap.sh` completes on a fresh pod; `python inference.py --help` works in the venv.
- 6-view example (`data/source/pexels/2785536-*.mp4`) generates `videos/dense/*.mp4` + `cameras.json`.
- `app/pipeline` turns that output into `scans/<id>/frames/frame_NNNN.sog` + `manifest.json`.
- The web app at `https://<pod>-8000.proxy.runpod.net/` uploads a clip, shows job progress, and the
  viewer plays the splat sequence with orbit, background switch and a shareable link.
- Stop/start: sentinels skip reinstall; SOG outputs persist.

## Scope

**In scope:** env, weights, SMPL-X install path, recon stage, API, web app, presets, pre-baked gallery items.

**Out of scope:** 4DAnyone model changes, FlashAttention-3 / SageAttention, the nerfstudio path, auth/billing.

## Prior context

Cycle 1. Upstream = ant-research/4DAnyone (Apache-2.0 code + checkpoint; GVHMR non-commercial;
Turbo LoRA CC BY-NC-SA; SMPL-X licence-gated). 4DAnyone outputs multi-view videos only; the 4DGS
reconstruction is ours (`app/pipeline`).

## Open questions

- SMPL-X (`SMPLX_NEUTRAL.npz`) is licence-gated: needs Vlad's registration + the official ZIP.

## Constraints

- Don't exceed 12 GPU-hours on this cycle (H100 ~ $2.7/hr).
- Don't re-download weights.

## Notes

- Pod `fdanyone` = `v7h27p8gn1so9i`, NVIDIA H100 80GB HBM3, EUR-IS-3, Secure Cloud.
- Volume `fdanyone-workspace` = `r8iy9sbe97` (150 GB, EUR-IS-3).
- Image `runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404`. Ports 22/tcp + 8000/http.
