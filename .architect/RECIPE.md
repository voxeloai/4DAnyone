# RECIPE — 4DAnyone + Spirula Studio + PlayCanvas 4D splat prototype on RunPod

**Status:** provisional (2026-09-11). Recon + web halves verified on a synthetic sequence; the
4DAnyone generation stage is verified only up to its SMPL-X licence check. Lock after one real clip
runs end to end and a stop/start passes.

## Instance

| | |
|---|---|
| Pod | `fdanyone` = `v7h27p8gn1so9i`, NVIDIA H100 80GB HBM3, Secure Cloud, EUR-IS-3, $3.49/hr |
| Volume | `fdanyone-workspace` = `r8iy9sbe97`, 150 GB, EUR-IS-3 (~$10.50/month idle) |
| Image | `runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404` (Ubuntu 24.04, system py3.12 + torch 2.8 unused; we run a py3.11 venv) |
| Ports | `22/tcp` (SSH), `8000/http` (RunPod proxy, fallback only) |
| Public URL | Cloudflare quick tunnel started by `scripts/serve.sh` (hostname changes on restart; read `/workspace/cloudflared/tunnel.log`) |
| Repo | `voxeloai/4DAnyone` `voxelo/main` (upstream `ant-research/4DAnyone`, submodule `third_party/GVHMR` @ `6ec3ca3`) |

## From nothing to serving

```powershell
# laptop
.\scripts\deploy.ps1                      # volume + pod (H100, 150 GB, 22/tcp + 8000/http)
```
```bash
# pod (SSH command printed by deploy.ps1; port changes on every start)
tmux new-session -d -s arch
curl -sSL https://raw.githubusercontent.com/voxeloai/4DAnyone/voxelo/main/scripts/bootstrap.sh -o /workspace/bootstrap.sh
bash /workspace/bootstrap.sh              # ~12 min first time: apt, ICD, spirula, venv, node, 21 GB weights
# SMPL-X: copy models_smplx_v1_1.zip to /workspace/smplx/ and re-run bootstrap (or scripts/push_smplx.ps1 from the laptop)
source /workspace/activate.sh
bash scripts/serve.sh                     # API + web on :8000 in tmux 'api'; cloudflared tunnel in tmux 'tunnel'
bash scripts/prebake.sh                   # optional: queue 3 bundled example clips
```

After a stop/start: re-run `bootstrap.sh` (apt + ICD are ephemeral; everything else is sentinel-skipped), then `serve.sh`.

## What lives where

```
/workspace/4DAnyone            repo, voxelo/main
/workspace/envs/fdanyone       uv venv py3.11: torch 2.8.0+cu128, torchvision 0.23, av 16, gsplat 1.5.3 (JIT → /workspace/torch-ext), fastapi 0.141
/workspace/models              4DAnyone weights (21 GB) + BiRefNet + body_models/smplx/SMPLX_NEUTRAL.npz (licensee-provided)
/workspace/data/source/pexels  20 bundled example clips
/workspace/node                node 22.12 + @playcanvas/splat-transform 3.4.2
/workspace/spirula/spirula     Spirula Studio 2026.9.10 (prebuilt ubuntu-vulkan)
/workspace/cloudflared         cloudflared + tunnel.log
/workspace/fdanyone-app        uploads/ jobs/ scans/ api.log   (the app's state; scans/ is the gallery)
/workspace/activate.sh         env exports; source after every start
```

## Pipeline knobs that matter

- Presets: `app/pipeline/presets.py` (views, timesteps, iterations, cap, sh_degree, res_divisor).
- Spirula flags: `app/pipeline/train_spirula.py::build_command` — `centered-object` preset, nerfstudio
  format, masks trained as empty (`--apply-loss-for-mask 1`, alpha 0.1 / under 0.2), no bilagrid/PPISP
  (synthetic views have constant exposure), **orientation/center `none`** (one world for the whole
  sequence), compressed densification schedule (refine 12 %–80 % of the run, every 50 steps, growth 1.15,
  init 20 % of cap).
- Measured (synthetic, 24 views @ 704×1280, H100): 2000 steps ≈ 16 s/timestep; SOG 3.9 MB @ 300k gaussians.
- **Measured on a real clip (standard preset, pexels 2785536, 2026-09-11):** generation 23.6 min
  (GVHMR preprocess 115 s, pose conditioning 189 s, RCP denoise 96 s + publish 100 s, target denoise
  267 s + decode/publish 252 s; peak VRAM 25.8 GB) · export 30 timesteps 222 s · train 30 × 21 s = 10.7 min
  (2500 steps, 300k cap reached) · pack 85 s · scan 114 MB (3.8 MB/frame) · **~40 min end to end**.
- **wide (48 views, one ring, 30 timesteps):** generation ~33 min · export ~6 min · train ~11 min · pack ~1.5 min → **~52 min**, 115 MB.
- **full (48 views on 3 rings, 48 timesteps, SH1, 500k cap):** generation 34 min · export 9 min · train 48 × 38 s = 30 min ·
  pack **7 h on CPU** (SH1 k-means palette fit ≈ 540 s/frame) vs **7 s/frame on the H100** → ~6 min → **~80 min** once
  SOG encodes on the GPU adapter (`pack.py` now tries `-g 0` first; `splat-transform --list-gpus` sees the H100 through
  the Vulkan ICD). Generation cost tracks view COUNT, not ring layout (wide ≈ full for 48 views). SH0 frames were
  fine on CPU (~3 s); only SH≥1 needs the GPU.
- Sequence export: `app/pipeline/export_sequence.py` decodes each target video once; BiRefNet masks +
  visual hull per timestep; writes RGB `images/`, binary `masks/`, `sparse_pcd.ply`, `transforms.json`
  (+`mask_path`) — the upstream exporter's layout, so nerfstudio/gsplat trainers also work.

## Gotchas banked this cycle

1. Vulkan in the RunPod container: driver libs present, ICD JSON absent → write `nvidia_icd.json`
   (`libGLX_nvidia.so.0`) and install the full libglvnd family (`libopengl0 libegl1 libgles2 libglvnd0 libgl1 libglx0`).
   `NVIDIA_DRIVER_CAPABILITIES` was empty and did not need changing.
2. `spirula train` `--refine-stop-num-iter` defaults to 2500 steps *before the end*: a 2000-step run
   never densifies. Scale the schedule with the step count.
3. 4DAnyone's `ensure_smplx()` prompts on stdin when it sees a tty → never give job subprocesses a tty.
4. RunPod HTTP proxy fails ~30–40 % of concurrent requests → Cloudflare quick tunnel for the demo.
5. `tar --no-same-owner` on the network volume; `splat-transform -g cpu` (no GPU adapter for node).
6. `git pull` on the pod refuses if files were scp'd by hand; `git checkout -- .` first.
7. GVHMR's `hmr4d/utils/body_model/body_model.py` has a stray `from turtle import forward` → imports
   tkinter, which the venv's Python lacks (`apt install python3.11-tk` hung on this image). Fixed with
   `turtle.py` at the repo root: 4DAnyone launches its GVHMR workers with `PYTHONPATH=<repo root>`, so
   the shim shadows the stdlib module only there. Keep it.

## Cleanup

Stop the pod (`runpodctl pod stop v7h27p8gn1so9i`) when not demoing; the volume keeps everything.
Decommission = export `/workspace/fdanyone-app/scans` if wanted, `pod remove`, delete volume `r8iy9sbe97`.
