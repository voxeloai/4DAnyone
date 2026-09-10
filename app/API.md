# Voxelo 4D prototype — API contract (v1)

The web client in `app/web` is one consumer of this API. An iOS app, a separate SaaS front end or a
batch tool can use exactly the same routes. Everything a client needs to render a scan is in the
scan's `manifest.json` plus static files next to it; the viewer is a pure function of the manifest.

Base URL: the pod's public proxy, e.g. `https://<pod-id>-8000.proxy.runpod.net`. CORS is open.
Interactive docs: `/api/docs`.

## Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/health` | `{ok, gpu, ready, smplx_installed, checkpoint_installed, spirula_installed, queue:{queued, running, running_since}, limits}` |
| GET | `/api/v1/presets` | `[{name, label, blurb, views, timesteps, eta_minutes, default}]` |
| POST | `/api/v1/jobs` | multipart form: `video` (file, ≤600 MB, mp4/mov/m4v/webm/mkv), `preset` (`fast`/`standard`/`full`), `title` (optional). Returns the job summary (201). |
| GET | `/api/v1/jobs` | Recent jobs, newest first. |
| GET | `/api/v1/jobs/{id}` | Job summary + `log_tail` (last 40 log lines). |
| GET | `/api/v1/jobs/{id}/log` | Full job log (text/plain). |
| GET | `/api/v1/scans` | Gallery: `[{id, title, created, preset, views, frames, fps, duration_seconds, gaussians_per_frame, bytes_total, poster, preview, url}]` |
| GET | `/api/v1/scans/{id}` | The manifest (below) plus `base_url`. |
| GET | `/scans/{id}/...` | Static scan assets (immutable, long cache): `manifest.json`, `poster.jpg`, `preview.mp4`, `source.mp4`, `frames/frame_NNNN.sog`. |
| GET | `/view/{id}` | The web viewer page for a scan (shareable link; `?bg=dark|cream|white|ember|magma|#rrggbb`). |

### Job summary

```json
{ "id": "k4x7...", "title": "Anna dance", "preset": "standard", "status": "running",
  "stage": "train", "progress": 0.71, "message": "trained timestep 9/30, ~4.2 min left",
  "error": null, "created": 1757550000.1, "started": 1757550012.9, "finished": null,
  "scan_id": null, "eta_minutes": 18 }
```

`status` ∈ `queued | starting | running | done | failed`. `stage` ∈ `queued | starting | probe |
generate | export | train | pack | done`. `progress` is 0..1 across the whole job. When `status` is
`done`, `scan_id` is set and the scan is live under `/scans/{scan_id}/` and `/view/{scan_id}`.

### Scan manifest (`/scans/{id}/manifest.json`)

```json
{
  "version": 1, "id": "k4x7...", "title": "Anna dance", "created": "2026-09-11T02:14:09Z",
  "engine": {"generator": "4DAnyone (ant-research)", "reconstruction": "Spirula Studio per-timestep 3DGS", "packer": "playcanvas splat-transform"},
  "preset": {"name": "standard", "views_per_layer": 24, "layer_pitches": [15], "timesteps": 30, "iterations": 2500, "cap_max": 300000, "sh_degree": 0, "res_divisor": 1},
  "source": {"filename": "IMG_0421.mov", "video": "source.mov", "fps": 30, "frames": 180, "duration_seconds": 6.0, "width": 1080, "height": 1920},
  "sequence": {"folder": "frames", "pattern": "frame_{frame:04}.sog", "start": 1, "end": 30, "count": 30,
               "fps": 6.2, "source_fps": 30, "source_frames": 121, "duration_seconds": 4.03, "timesteps": [0, 4, 8, "..."]},
  "world": {"up": [0, 0, 1], "convention": "nerfstudio (OpenGL c2w, Z-up)", "front_camera_position": [x, y, z],
            "center": [x, y, z], "radius": 0.92, "bounds_min": [..], "bounds_max": [..]},
  "stats": {"gaussians_per_frame": 212000, "bytes_total": 61000000, "bytes_per_frame": 2030000, "pack_seconds": 41.2},
  "assets": {"poster": "poster.jpg", "preview": "preview.mp4"},
  "job_id": "k4x7..."
}
```

Rendering rules for any client:

- Frames are `sequence.folder + "/" + pattern` with `{frame:04}` zero-padded from `start` to `end`.
  Play at `sequence.fps` for real-time motion (`count / duration_seconds`).
- Splats are in the Nerfstudio Z-up frame. For a Y-up engine rotate the whole sequence by
  −90° about X: `(x, y, z) → (x, z, −y)`. Apply the same mapping to `world.center` and
  `world.front_camera_position`.
- Home camera: sit on the line from `center` towards `front_camera_position` at `≈2.4 × radius`,
  look at `center`. Zoom range `[0.6, 10] × radius`.
- SOG (`.sog`) is PlayCanvas' compressed splat bundle; `@playcanvas/splat-transform` converts it to
  `.ply` / `.spz` for other engines (three.js Spark, gsplat.js, native).

## Storage layout on the pod (`$FDA_APPDATA`)

```
uploads/<job>/source.<ext>
jobs/<job>/job.json state.json log.txt work/{fdanyone,sequence,train}
scans/<scan>/manifest.json poster.jpg preview.mp4 source.<ext> frames/frame_NNNN.sog
```

## Decoupling later

- The API is one FastAPI process with an in-process worker thread. To scale, move the job store to a
  DB/queue and run `app.pipeline.run_job` on any GPU worker that has the volume layout; the routes
  don't change.
- Static scan assets can move to object storage + CDN (R2) unchanged; only `base_url` in the scan
  response changes.
- The web client reads `window.VOXELO_API_BASE` (defaults to same-origin) so it can be hosted anywhere.
