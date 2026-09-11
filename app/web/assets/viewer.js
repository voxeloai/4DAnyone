// Voxelo 4D viewer — PlayCanvas 2.22 (unified gsplat renderer) + the engine's official
// GSplatFlipbook (streamed SOG splat sequence) + CameraControls (orbit / zoom / pan, touch).
//
// Data contract: GET /api/v1/scans/<id> -> manifest.json (see app/API.md). Frames are static
// files at <base_url>frames/frame_NNNN.sog. Nothing else is needed to render a scan.

import {
  AppBase, AppOptions, Color, Entity, Vec2, Vec3, Mouse, TouchDevice,
  CameraComponentSystem, GSplatComponentSystem, LightComponentSystem, RenderComponentSystem, ScriptComponentSystem,
  ContainerHandler, GSplatHandler, ScriptHandler, TextureHandler,
  FILLMODE_FILL_WINDOW, RESOLUTION_AUTO, DEVICETYPE_WEBGPU, DEVICETYPE_WEBGL2,
  createGraphicsDevice,
} from "playcanvas";
// Our fork of the engine's official gsplat-flipbook script: same streaming design, plus retries and
// skip-on-failure so a flaky proxy cannot deadlock playback (see assets/gsplat-flipbook.mjs).
import { GSplatFlipbook } from "/assets/gsplat-flipbook.mjs";
import { CameraControls } from "playcanvas/scripts/esm/camera-controls.mjs";

const $ = (id) => document.getElementById(id);
const API = (window.VOXELO_API_BASE || "") + "/api/v1";
const IS_MOBILE = matchMedia("(max-width: 760px)").matches || /Android|iPhone|iPad/i.test(navigator.userAgent);
const BG_LIGHT = new Set(["cream", "white"]);

function scanIdFromLocation() {
  const m = location.pathname.match(/\/view\/([A-Za-z0-9_-]+)/);
  if (m) return m[1];
  return new URLSearchParams(location.search).get("scan");
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._h); toast._h = setTimeout(() => t.classList.remove("show"), 2200);
}
function showError(msg) {
  $("loader").classList.add("hidden");
  const e = $("error"); e.textContent = msg; e.classList.remove("hidden");
}

// ── background ─────────────────────────────────────────────────────────────
const stage = $("stage");
function setBackground(name, custom) {
  stage.className = stage.className.replace(/\bbg-\S+/g, "").replace(/\blight\b/g, "").trim();
  stage.classList.add(`bg-${name}`);
  if (name === "custom" && custom) {
    stage.style.setProperty("--custom-bg", custom);
    $("bg-custom-swatch").style.background = custom;
    const r = parseInt(custom.slice(1, 3), 16), g = parseInt(custom.slice(3, 5), 16), b = parseInt(custom.slice(5, 7), 16);
    if ((r * 299 + g * 587 + b * 114) / 1000 > 150) stage.classList.add("light");
  } else if (BG_LIGHT.has(name)) stage.classList.add("light");
  $("bg-label").textContent = name;
  document.querySelectorAll("#bg-chips .chip").forEach((c) => c.classList.toggle("sel", c.dataset.bg === name));
  const url = new URL(location.href);
  url.searchParams.set("bg", name === "custom" ? custom : name);
  history.replaceState(null, "", url);
}
document.querySelectorAll("#bg-chips .chip[data-bg]").forEach((c) => c.addEventListener("click", () => setBackground(c.dataset.bg)));
$("bg-custom").addEventListener("input", (e) => setBackground("custom", e.target.value));
{
  const bg = new URLSearchParams(location.search).get("bg");
  if (bg && /^#[0-9a-f]{6}$/i.test(bg)) { $("bg-custom").value = bg; setBackground("custom", bg); }
  else if (bg && ["dark", "cream", "white", "ember", "magma"].includes(bg)) setBackground(bg);
}

// ── share / fullscreen / settings ──────────────────────────────────────────
$("share").addEventListener("click", async () => {
  const url = location.href;
  if (navigator.share) { try { await navigator.share({ title: document.title, url }); return; } catch (_) { /* cancelled */ } }
  try { await navigator.clipboard.writeText(url); toast("Link copied"); } catch (_) { prompt("Copy this link", url); }
});
$("fullscreen").addEventListener("click", () => {
  if (document.fullscreenElement) document.exitFullscreen(); else stage.requestFullscreen?.();
});
$("settings-toggle").addEventListener("click", () => $("panel").classList.toggle("open"));

// ── engine ─────────────────────────────────────────────────────────────────
async function main() {
  const scanId = scanIdFromLocation();
  if (!scanId) { showError("No scan id in the URL."); return; }
  let manifest;
  let lastErr = null;
  for (let attempt = 0; attempt < 4 && !manifest; attempt++) {
    try {
      const r = await fetch(`${API}/scans/${scanId}`, { cache: "no-store" });
      if (r.status === 404 && attempt >= 1) { lastErr = new Error("no such scan"); break; }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      manifest = await r.json();
    } catch (e) {
      lastErr = e;
      $("loader-text").textContent = `connecting… (retry ${attempt + 1})`;
      await new Promise((res) => setTimeout(res, 1200 * (attempt + 1)));
    }
  }
  if (!manifest) { showError(`Could not load scan ${scanId} (${lastErr?.message || "unknown error"}).`); return; }

  const base = manifest.base_url || `/scans/${scanId}/`;
  const seq = manifest.sequence;
  const world = manifest.world || {};
  const stats = manifest.stats || {};
  document.title = `${manifest.title || scanId} · Voxelo 4D`;
  $("vtitle").textContent = manifest.title || scanId;
  const views = (manifest.preset?.views_per_layer || 0) * ((manifest.preset?.layer_pitches || [0]).length || 1);
  $("vmeta").textContent = `4D splat · ${seq.count} frames · ${views} views · ${(stats.bytes_total / 1e6).toFixed(0)} MB`;
  $("facts").innerHTML = [
    ["frames", seq.count], ["playback", `${seq.fps.toFixed(1)} fps`], ["views", views],
    ["gaussians", stats.gaussians_per_frame ? `${(stats.gaussians_per_frame / 1000).toFixed(0)}k / frame` : "—"],
    ["size", `${(stats.bytes_total / 1e6).toFixed(0)} MB`], ["preset", manifest.preset?.name || "—"],
  ].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  $("engine-label").textContent = "sog sequence";

  const canvas = $("app");
  const device = await createGraphicsDevice(canvas, {
    deviceTypes: [DEVICETYPE_WEBGPU, DEVICETYPE_WEBGL2],
    antialias: false,
    alpha: true,
    powerPreference: "high-performance",
  });
  device.maxPixelRatio = Math.min(window.devicePixelRatio || 1, IS_MOBILE ? 1.5 : 2);

  const opts = new AppOptions();
  opts.graphicsDevice = device;
  opts.mouse = new Mouse(canvas);
  opts.touch = new TouchDevice(canvas);
  opts.componentSystems = [RenderComponentSystem, CameraComponentSystem, LightComponentSystem, ScriptComponentSystem, GSplatComponentSystem];
  opts.resourceHandlers = [TextureHandler, ContainerHandler, ScriptHandler, GSplatHandler];
  const app = new AppBase(canvas);
  app.init(opts);
  app.setCanvasFillMode(FILLMODE_FILL_WINDOW);
  app.setCanvasResolution(RESOLUTION_AUTO);
  const fit = () => app.resizeCanvas(window.innerWidth, window.innerHeight);
  window.addEventListener("resize", fit);
  window.addEventListener("orientationchange", () => setTimeout(fit, 120));
  if (window.visualViewport) window.visualViewport.addEventListener("resize", fit);
  app.scene.gsplat.alphaClip = 0.05;

  // ── camera + controls ──
  // Splats are exported in the dataset's Nerfstudio frame (Z-up). Rotate -90° about X to get
  // PlayCanvas' Y-up: (x, y, z)_zup -> (x, z, -y)_yup. The same mapping is applied to the framing stats.
  const zupToYup = (v) => new Vec3(v[0], v[2], -v[1]);
  const centre = world.center ? zupToYup(world.center) : new Vec3(0, 0.9, 0);
  const radius = Math.max(0.15, world.radius || 1.0);
  let dir = new Vec3(0, 0.25, 1);
  if (world.front_camera_position) {
    dir = zupToYup(world.front_camera_position).sub(centre);
    if (dir.length() < 1e-4) dir = new Vec3(0, 0.25, 1);
  }
  dir.normalize();
  const homeDistance = radius * 2.4;
  const homePos = centre.clone().add(dir.clone().mulScalar(homeDistance));

  const camera = new Entity("camera");
  camera.addComponent("camera", { clearColor: new Color(0, 0, 0, 0), fov: IS_MOBILE ? 60 : 50, nearClip: 0.02, farClip: 200 });
  camera.setPosition(homePos);
  camera.lookAt(centre);
  camera.addComponent("script");
  const controls = camera.script.create(CameraControls);
  controls.enableFly = false;
  controls.zoomRange = new Vec2(radius * 0.6, radius * 10);
  controls.pitchRange = new Vec2(-35, 80);
  controls.rotateDamping = 0.93;
  controls.zoomDamping = 0.9;
  controls.moveDamping = 0.93;
  app.root.addChild(camera);
  const resetView = () => { controls.reset(centre, homePos); player.setLocalEulerAngles(-90, 0, 0); };

  // ── the 4D splat: official GSplatFlipbook streams the SOG sequence ──
  const player = new Entity("splat");
  player.addComponent("gsplat", { unified: true });
  player.addComponent("script");
  const flipbook = player.script.create(GSplatFlipbook);
  const baseFps = Math.max(1, seq.fps || 10);
  flipbook.fps = baseFps;
  flipbook.folder = base + (seq.folder || "frames");
  flipbook.filenamePattern = seq.pattern || "frame_{frame:04}.sog";
  flipbook.startFrame = seq.start || 1;
  flipbook.endFrame = seq.end || seq.count;
  flipbook.playMode = "loop";
  flipbook.playing = true;
  flipbook.preloadCount = Math.min(IS_MOBILE ? 3 : 5, Math.max(1, seq.count - 1));
  let failures = 0;
  flipbook.onError = (url, attempts) => { failures++; if (attempts === 1) console.warn("frame load failed, retrying", url); if (failures === 3) toast("Slow link: retrying frames"); };
  player.setLocalEulerAngles(-90, 0, 0);
  app.root.addChild(player);
  window.__pc = { app, camera, controls, player, flipbook, manifest };

  app.start();
  controls.reset(centre, homePos);

  // ── UI wiring ──
  const total = flipbook.endFrame - flipbook.startFrame + 1;
  const scrub = $("scrub");
  scrub.min = String(flipbook.startFrame); scrub.max = String(flipbook.endFrame); scrub.value = String(flipbook.startFrame);
  const pad = (n) => String(n).padStart(3, "0");
  let scrubbing = false;
  scrub.addEventListener("input", () => { scrubbing = true; flipbook.pause(); flipbook.seekToFrame(Number(scrub.value)); setPlayIcon(); });
  scrub.addEventListener("change", () => { scrubbing = false; });
  const setPlayIcon = () => { $("play-icon").textContent = flipbook.playing ? "❚❚" : "▶"; };
  $("play").addEventListener("click", () => { if (flipbook.playing) flipbook.pause(); else flipbook.play(); setPlayIcon(); });
  $("speed").addEventListener("change", (e) => { flipbook.fps = baseFps * Number(e.target.value); $("fps-label").textContent = `${flipbook.fps.toFixed(1)} fps`; });
  $("loopmode").addEventListener("click", () => { flipbook.playMode = flipbook.playMode === "loop" ? "bounce" : "loop"; $("loopmode").textContent = flipbook.playMode; });
  $("reset-view").addEventListener("click", resetView);
  let autoRotate = false;
  $("auto-rotate").addEventListener("click", () => { autoRotate = !autoRotate; $("auto-rotate").textContent = `Auto-rotate: ${autoRotate ? "on" : "off"}`; $("auto-rotate").classList.toggle("on", autoRotate); });
  $("fps-label").textContent = `${baseFps.toFixed(1)} fps`;
  document.addEventListener("keydown", (e) => {
    if (e.code === "Space") { e.preventDefault(); $("play").click(); }
    if (e.code === "KeyR") resetView();
    if (e.code === "ArrowRight") { flipbook.pause(); flipbook.seekToFrame(Math.min(flipbook.endFrame, flipbook.currentFrame + 1)); setPlayIcon(); }
    if (e.code === "ArrowLeft") { flipbook.pause(); flipbook.seekToFrame(Math.max(flipbook.startFrame, flipbook.currentFrame - 1)); setPlayIcon(); }
  });
  let lastInteraction = 0;
  const bump = () => { lastInteraction = performance.now(); $("hint").classList.add("hidden"); };
  canvas.addEventListener("pointerdown", bump);
  canvas.addEventListener("wheel", bump, { passive: true });

  // loader: first frame decoded -> hint; then a buffering badge whenever the next frame isn't ready
  let firstShown = false;
  let framesSeen = 0;
  app.on("update", (dt) => {
    const loaded = flipbook.currentAsset?.loaded && flipbook.currentAsset?.resource;
    const buffered = flipbook.loadedCount();
    if (!firstShown) {
      if (loaded) {
        firstShown = true;
        $("loader").classList.add("hidden");
        $("hint").classList.remove("hidden");
        setTimeout(() => $("hint").classList.add("hidden"), 4000);
      } else {
        $("loader-text").textContent = `loading frame 1 of ${total}`;
        $("loader-bar").style.width = `${Math.min(90, 10 + buffered * 8)}%`;
      }
    } else {
      $("buffering").classList.toggle("hidden", !(flipbook.playing && buffered === 0 && total > 1));
    }
    if (!scrubbing) scrub.value = String(flipbook.currentFrame);
    $("counter").textContent = `${pad(flipbook.currentFrame - flipbook.startFrame + 1)} / ${pad(total)}`;
    if (autoRotate && performance.now() - lastInteraction > 1500) player.rotate(0, 0, dt * 12);
    framesSeen++;
  });
  setPlayIcon();
  setTimeout(() => { if (!firstShown) $("loader-text").textContent = `still loading frame 1 of ${total}…`; }, 8000);
}

main().catch((e) => { console.error(e); showError(`Viewer failed to start: ${e.message || e}`); });
