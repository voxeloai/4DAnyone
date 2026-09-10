"""Self-test data for the reconstruction stage, without 4DAnyone: render a known 3DGS PLY from a
4DAnyone-style camera rig with gsplat and write per-timestep Nerfstudio datasets in exactly the
layout ``app/pipeline/export_sequence.py`` produces (RGB images, binary masks, visual-hull PLY,
transforms.json with mask_path). Timesteps animate the splat (a slow turn + bob) so the sequence
exercises the whole chain: spirula per-frame training -> SOG -> flipbook playback.

    python tools/synth_dataset.py --ply /workspace/test/teaser.ply --output_dir /workspace/test/synth_seq \
        --views 24 --timesteps 8 --width 704 --height 1280

The camera rig mimics 4DAnyone: an orbit at radius R around the object centre, pitch 15 deg, yaw 0..360.
World: 4DAnyone-style Y-up while rendering; written to disk in the Nerfstudio Z-up frame via the same
``camera_to_nerfstudio`` / ``points_to_nerfstudio`` helpers the real exporter uses.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import torch
from PIL import Image

from fdanyone.io import write_json
from fdanyone.nerfstudio.cameras import camera_to_nerfstudio
from fdanyone.nerfstudio.visual_hull import NERFSTUDIO_POINT_CLOUD, build_sparse_point_cloud, write_sparse_point_cloud


def read_gaussians(ply: Path) -> dict[str, np.ndarray]:
    from plyfile import PlyData

    v = PlyData.read(str(ply))["vertex"]
    names = v.data.dtype.names
    xyz = np.stack([v["x"], v["y"], v["z"]], 1).astype(np.float32)
    scales = np.stack([v["scale_0"], v["scale_1"], v["scale_2"]], 1).astype(np.float32)
    quats = np.stack([v["rot_0"], v["rot_1"], v["rot_2"], v["rot_3"]], 1).astype(np.float32)
    opac = np.asarray(v["opacity"], dtype=np.float32)
    dc = np.stack([v["f_dc_0"], v["f_dc_1"], v["f_dc_2"]], 1).astype(np.float32)
    rest = [n for n in names if n.startswith("f_rest_")]
    sh = None
    if rest:
        rest_sorted = sorted(rest, key=lambda n: int(n.split("_")[-1]))
        r = np.stack([v[n] for n in rest_sorted], 1).astype(np.float32)  # (N, 3*K) in channel-major order
        k = r.shape[1] // 3
        sh = np.concatenate([dc[:, None, :], r.reshape(-1, 3, k).transpose(0, 2, 1)], axis=1)
    return {"xyz": xyz, "scales": scales, "quats": quats, "opacity": opac, "dc": dc, "sh": sh}


def look_at(eye: np.ndarray, target: np.ndarray, up=np.array([0.0, 1.0, 0.0])) -> np.ndarray:
    """OpenCV camera-to-world (x right, y down, z forward) looking from eye at target, Y-up world."""

    z = target - eye
    z /= np.linalg.norm(z)
    x = np.cross(z, up)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    c2w = np.eye(4)
    c2w[:3, 0], c2w[:3, 1], c2w[:3, 2], c2w[:3, 3] = x, y, z, eye
    return c2w


def rig(centre: np.ndarray, radius: float, views: int, pitch_deg: float) -> list[np.ndarray]:
    cams = []
    for i in range(views):
        yaw = 2 * math.pi * i / views
        p = math.radians(pitch_deg)
        eye = centre + radius * np.array([math.sin(yaw) * math.cos(p), math.sin(p), math.cos(yaw) * math.cos(p)])
        cams.append(look_at(eye, centre))
    return cams


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ply", required=True)
    ap.add_argument("--output_dir", required=True)
    ap.add_argument("--views", type=int, default=24)
    ap.add_argument("--timesteps", type=int, default=8)
    ap.add_argument("--width", type=int, default=704)
    ap.add_argument("--height", type=int, default=1280)
    ap.add_argument("--pitch", type=float, default=15.0)
    ap.add_argument("--fov_deg", type=float, default=40.0)
    ap.add_argument("--flip_y", action="store_true", help="input PLY is y-down (OpenCV world, e.g. SHARP): flip to Y-up")
    args = ap.parse_args()

    from gsplat import rasterization

    dev = torch.device("cuda")
    g = read_gaussians(Path(args.ply))
    xyz = g["xyz"].copy()
    quats = g["quats"].copy()
    if args.flip_y:  # 180 deg about X: (x,y,z) -> (x,-y,-z); quaternion q' = qx180 * q
        xyz[:, 1] *= -1
        xyz[:, 2] *= -1
        w, x, y, z = quats.T
        quats = np.stack([-x, w, z, -y], 1)  # (0,1,0,0) * (w,x,y,z)
    # robust centre / extent, then a rig that frames the object like 4DAnyone does (radius ~3 m for a 1 m target)
    centre = np.median(xyz, 0)
    extent = float(np.percentile(np.linalg.norm(xyz - centre, axis=1), 90))
    radius = extent * 3.0

    means = torch.from_numpy(xyz).to(dev)
    q = torch.from_numpy(quats).to(dev)
    scales = torch.from_numpy(np.exp(g["scales"])).to(dev)
    opac = torch.sigmoid(torch.from_numpy(g["opacity"]).to(dev))
    if g["sh"] is not None and int(math.sqrt(g["sh"].shape[1])) ** 2 == g["sh"].shape[1]:
        colors = torch.from_numpy(g["sh"]).to(dev)
        sh_degree = int(math.sqrt(colors.shape[1]) - 1)
    else:
        colors = torch.from_numpy(g["dc"]).to(dev)[:, None, :]
        sh_degree = 0
    print(f"gaussians {len(xyz)}, sh_degree {sh_degree}, centre {centre.round(3).tolist()}, extent {extent:.3f}, rig radius {radius:.3f}", flush=True)

    W, H = args.width, args.height
    fy = 0.5 * H / math.tan(math.radians(args.fov_deg) / 2)
    K = np.array([[fy, 0, W / 2], [0, fy, H / 2], [0, 0, 1]], dtype=np.float64)
    cams = rig(centre, radius, args.views, args.pitch)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    camera_records = []
    for i, c2w in enumerate(cams):
        camera_records.append({"camera_id": i, "K": K.tolist(), "camera_to_world": c2w.tolist(), "image_width": W, "image_height": H, "video": f"videos/dense/{i:02d}.mp4"})
    (out / "cameras.json").write_text(json.dumps({"camera_model": "OPENCV", "cameras": camera_records}, indent=1))

    base_frames = []
    for i, c2w in enumerate(cams):
        base_frames.append({"file_path": f"images/{i:02d}.png", "mask_path": f"masks/{i:02d}.png", "fl_x": float(K[0, 0]), "fl_y": float(K[1, 1]), "cx": float(K[0, 2]), "cy": float(K[1, 2]), "h": H, "w": W, "transform_matrix": camera_to_nerfstudio(c2w)})

    Ks = torch.from_numpy(np.stack([K] * len(cams))).float().to(dev)
    for t in range(args.timesteps):
        frame_dir = out / f"frame_{t + 1:04d}"
        if (frame_dir / "transforms.json").is_file():
            continue
        # animate: slow turn about the vertical axis through the centre + a bob
        ang = 2 * math.pi * t / max(1, args.timesteps) * 0.5
        R = torch.tensor([[math.cos(ang), 0, math.sin(ang)], [0, 1, 0], [-math.sin(ang), 0, math.cos(ang)]], device=dev, dtype=torch.float32)
        cen = torch.from_numpy(centre).float().to(dev)
        m_t = (means - cen) @ R.T + cen + torch.tensor([0, 0.05 * extent * math.sin(2 * math.pi * t / max(1, args.timesteps)), 0], device=dev)
        # rotate quaternions by R: q' = qR * q
        half = ang / 2
        qR = torch.tensor([math.cos(half), 0, math.sin(half), 0], device=dev)
        w1, x1, y1, z1 = qR
        w2, x2, y2, z2 = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
        q_t = torch.stack([w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2, w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2, w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2, w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2], 1)
        viewmats = torch.from_numpy(np.stack([np.linalg.inv(c) for c in cams])).float().to(dev)
        with torch.no_grad():
            rgb, alpha, _ = rasterization(m_t, q_t / q_t.norm(dim=1, keepdim=True), scales, opac, colors, viewmats, Ks, W, H, sh_degree=sh_degree, render_mode="RGB")
        images = tuple((rgb[i].clamp(0, 1).cpu().numpy() * 255).astype(np.uint8) for i in range(len(cams)))
        masks = (alpha[..., 0] > 0.5).cpu().numpy()
        work = out / f".{frame_dir.name}.tmp"
        (work / "images").mkdir(parents=True, exist_ok=True)
        (work / "masks").mkdir(exist_ok=True)
        for i, im in enumerate(images):
            Image.fromarray(im, "RGB").save(work / "images" / f"{i:02d}.png", compress_level=1)
            Image.fromarray((masks[i] * 255).astype(np.uint8)).save(work / "masks" / f"{i:02d}.png", compress_level=1)
        pts, cols = build_sparse_point_cloud(images, masks, camera_records, "cuda:0")
        write_sparse_point_cloud(work / NERFSTUDIO_POINT_CLOUD, pts, cols)
        write_json(work / "transforms.json", {"camera_model": "OPENCV", "ply_file_path": NERFSTUDIO_POINT_CLOUD, "timestep": t, "frames": base_frames}, sort_keys=False)
        work.rename(frame_dir)
        print(f"frame {t + 1}/{args.timesteps}: {len(pts)} hull points, mask coverage {masks.mean():.3f}", flush=True)

    write_json(out / "sequence.json", {"source_result": str(out), "num_cameras": len(cams), "num_frames_source": 121, "source_fps": [25, 1], "timesteps": list(range(args.timesteps)), "frames": [{"frame": t + 1, "timestep": t, "dir": f"frame_{t + 1:04d}"} for t in range(args.timesteps)], "seconds": 0}, sort_keys=False)
    print("done", out)


if __name__ == "__main__":
    main()
