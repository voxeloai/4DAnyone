// Voxelo 4D prototype — upload, job progress, gallery. Talks only to /api/v1 (see app/API.md).
const API = (window.VOXELO_API_BASE || "") + "/api/v1";
const $ = (id) => document.getElementById(id);
const fmtMB = (b) => (b / 1e6).toFixed(b < 10e6 ? 1 : 0) + " MB";
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let presets = [];
let selectedPreset = "standard";
let file = null;
let health = null;

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return r.json();
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._h);
  toast._h = setTimeout(() => t.classList.remove("show"), 2200);
}

// ── readiness ──────────────────────────────────────────────────────────────
function renderReadiness() {
  const box = $("readiness");
  if (!health) { box.innerHTML = ""; return; }
  $("gpu-label").textContent = health.gpu ? `GPU · ${health.gpu}` : "";
  if (health.ready) { box.innerHTML = ""; return; }
  const missing = [];
  if (!health.smplx_installed) missing.push("SMPL-X body model (licence-gated, install pending)");
  if (!health.checkpoint_installed) missing.push("4DAnyone checkpoint (downloading)");
  if (!health.spirula_installed) missing.push("Spirula Studio binary");
  box.innerHTML = `<div class="banner warn"><span class="mono tag">Paused</span><p>Uploads are accepted and queued, but processing waits for: ${esc(missing.join(", "))}.</p></div>`;
}

// ── presets ────────────────────────────────────────────────────────────────
function renderPresets() {
  const box = $("presets");
  box.innerHTML = presets.map((p) => `
    <label class="preset ${p.name === selectedPreset ? "sel" : ""}" data-name="${p.name}">
      <input type="radio" name="preset" value="${p.name}" ${p.name === selectedPreset ? "checked" : ""} />
      <span><span class="t">${esc(p.label)}</span><br/><span class="b">${esc(p.blurb)}</span></span>
      <span class="mono eta">~${p.eta_minutes} min</span>
    </label>`).join("");
  box.querySelectorAll("input").forEach((inp) => inp.addEventListener("change", () => {
    selectedPreset = inp.value;
    box.querySelectorAll(".preset").forEach((el) => el.classList.toggle("sel", el.dataset.name === selectedPreset));
  }));
}

// ── upload ─────────────────────────────────────────────────────────────────
function setFile(f) {
  file = f || null;
  const chip = $("file-chip");
  if (!file) { chip.innerHTML = ""; $("submit").disabled = true; $("submit-note").textContent = "Choose a clip to start."; return; }
  chip.innerHTML = `<span class="file-chip"><span class="n">${esc(file.name)}</span><span class="mono">${fmtMB(file.size)}</span><button type="button" class="x" aria-label="remove">×</button></span>`;
  chip.querySelector(".x").addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); $("video").value = ""; setFile(null); });
  if (!$("title").value) $("title").value = file.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").slice(0, 80);
  $("submit").disabled = false;
  $("submit-note").textContent = `Ready. ${fmtMB(file.size)} will upload, then the job queues on the GPU.`;
}

function wireUpload() {
  const drop = $("drop");
  const input = $("video");
  input.addEventListener("change", () => setFile(input.files[0]));
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => { const f = e.dataTransfer.files && e.dataTransfer.files[0]; if (f) setFile(f); });

  $("upload-form").addEventListener("submit", (e) => {
    e.preventDefault();
    if (!file) return;
    const fd = new FormData();
    fd.append("video", file, file.name);
    fd.append("preset", selectedPreset);
    fd.append("title", $("title").value.trim());
    const xhr = new XMLHttpRequest();
    const bar = $("upload-progress");
    bar.classList.remove("hidden");
    $("submit").disabled = true;
    $("submit-note").textContent = "Uploading…";
    xhr.upload.addEventListener("progress", (ev) => {
      if (ev.lengthComputable) {
        const pct = Math.round((ev.loaded / ev.total) * 100);
        bar.querySelector("i").style.width = pct + "%";
        $("submit-note").textContent = `Uploading… ${pct}%`;
      }
    });
    xhr.addEventListener("load", () => {
      bar.classList.add("hidden");
      bar.querySelector("i").style.width = "0";
      if (xhr.status >= 200 && xhr.status < 300) {
        toast("Queued. Watch it in Processing below.");
        $("video").value = ""; $("title").value = ""; setFile(null);
        refreshJobs(true);
        location.hash = "#jobs";
      } else {
        let msg = "Upload failed";
        try { msg = JSON.parse(xhr.responseText).detail || msg; } catch (_) {}
        $("submit-note").textContent = msg;
        $("submit").disabled = false;
      }
    });
    xhr.addEventListener("error", () => { bar.classList.add("hidden"); $("submit-note").textContent = "Network error during upload."; $("submit").disabled = false; });
    xhr.open("POST", `${API}/jobs`);
    xhr.send(fd);
  });
}

// ── jobs ───────────────────────────────────────────────────────────────────
const STAGE_LABEL = { queued: "Queued", starting: "Starting", probe: "Checking clip", generate: "Generating views", export: "Masks + hull", train: "Training splats", pack: "Packing SOG", done: "Ready", failed: "Failed" };
let jobsTimer = null;

function badgeFor(j) {
  if (j.status === "done") return `<span class="badge done">Ready</span>`;
  if (j.status === "failed") return `<span class="badge flame">Failed</span>`;
  if (j.status === "queued") return `<span class="badge outline">Queued</span>`;
  return `<span class="badge gold">${esc(STAGE_LABEL[j.stage] || j.stage)}</span>`;
}

function etaText(j) {
  if (j.status !== "running" && j.status !== "starting") return "";
  if (!j.eta_minutes || !j.started) return "";
  const elapsed = (Date.now() / 1000 - j.started) / 60;
  const left = Math.max(0.5, j.eta_minutes - elapsed);
  return ` · ~${left.toFixed(0)} min left`;
}

function renderJobs(jobs) {
  const box = $("jobs-list");
  const visible = jobs.filter((j) => j.status !== "done" || (j.finished && Date.now() / 1000 - j.finished < 3600));
  $("jobs-count").textContent = visible.length ? `${visible.length} recent` : "";
  if (!visible.length) { box.innerHTML = `<div class="empty">Nothing processing. Upload a clip above.</div>`; return; }
  box.innerHTML = visible.map((j) => {
    const pct = Math.round((j.progress || 0) * 100);
    const active = j.status === "running" || j.status === "starting";
    return `<div class="card job" data-id="${j.id}">
      <div>
        <div class="title">${esc(j.title || j.id)} ${badgeFor(j)} <span class="badge outline">${esc(j.preset || "")}</span></div>
        <div class="msg mono">${esc(j.message || "")}${esc(etaText(j))}</div>
      </div>
      <div>${j.status === "done" && j.scan_id ? `<a class="btn primary small" href="/view/${j.scan_id}">Open in 4D</a>` : `<span class="mono" style="color:var(--muted)">${pct}%</span>`}</div>
      ${j.status !== "done" ? `<div class="bar progress ${active && pct < 3 ? "indet" : ""}"><i style="width:${pct}%"></i></div>` : ""}
      ${j.error ? `<div class="err">${esc(j.error)}</div>` : ""}
      ${active || j.status === "failed" ? `<details><summary class="mono">Log</summary><pre data-log="${j.id}">loading…</pre></details>` : ""}
    </div>`;
  }).join("");
  box.querySelectorAll("details").forEach((d) => d.addEventListener("toggle", async () => {
    if (!d.open) return;
    const pre = d.querySelector("pre");
    try { const j = await getJSON(`${API}/jobs/${pre.dataset.log}`); pre.textContent = (j.log_tail || []).join("\n") || "(empty)"; } catch (e) { pre.textContent = String(e); }
  }));
}

async function refreshJobs(force) {
  try {
    const jobs = await getJSON(`${API}/jobs`);
    renderJobs(jobs);
    const active = jobs.some((j) => ["queued", "starting", "running"].includes(j.status));
    const wasDone = refreshJobs._done || new Set();
    for (const j of jobs) if (j.status === "done" && !wasDone.has(j.id)) { wasDone.add(j.id); if (refreshJobs._seen) { refreshScans(); toast(`“${j.title || j.id}” is ready`); } }
    refreshJobs._done = wasDone; refreshJobs._seen = true;
    // open logs auto-refresh
    document.querySelectorAll("details[open] pre[data-log]").forEach(async (pre) => {
      try { const j = await getJSON(`${API}/jobs/${pre.dataset.log}`); pre.textContent = (j.log_tail || []).join("\n"); pre.scrollTop = pre.scrollHeight; } catch (_) {}
    });
    clearTimeout(jobsTimer);
    jobsTimer = setTimeout(refreshJobs, active ? 3000 : 15000);
  } catch (e) {
    clearTimeout(jobsTimer);
    jobsTimer = setTimeout(refreshJobs, 8000);
  }
}

// ── gallery ────────────────────────────────────────────────────────────────
function shareLink(id) {
  const url = `${location.origin}/view/${id}`;
  if (navigator.share) { navigator.share({ title: "Voxelo 4D", url }).catch(() => {}); return; }
  navigator.clipboard?.writeText(url).then(() => toast("Link copied"), () => toast(url));
}

async function refreshScans() {
  try {
    const scans = await getJSON(`${API}/scans`);
    $("scans-count").textContent = scans.length ? `${scans.length} ${scans.length === 1 ? "scan" : "scans"}` : "";
    const grid = $("scans-grid");
    if (!scans.length) { grid.innerHTML = `<div class="empty" style="grid-column:1/-1">No scans yet. The first one you create lands here.</div>`; return; }
    grid.innerHTML = scans.map((s) => `
      <a class="gcard" href="${s.url}" data-id="${s.id}">
        <div class="media">
          <img src="${s.poster}" alt="" loading="lazy" />
          <video src="${s.preview}" muted loop playsinline preload="none"></video>
          <span class="badge accent">4D splat</span>
          <button type="button" class="share mono" data-share="${s.id}" aria-label="share">Share</button>
          <span class="btn primary small cta">View in 4D</span>
        </div>
        <div class="body">
          <div class="t">${esc(s.title || s.id)}</div>
          <div class="meta mono"><span>${s.views} views</span><span>${s.frames} frames</span><span>${s.bytes_total ? fmtMB(s.bytes_total) : ""}</span></div>
        </div>
      </a>`).join("");
    grid.querySelectorAll(".gcard").forEach((card) => {
      const v = card.querySelector("video");
      card.addEventListener("mouseenter", () => { v.play().catch(() => {}); });
      card.addEventListener("mouseleave", () => { v.pause(); v.currentTime = 0; });
      card.querySelector("[data-share]").addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); shareLink(card.dataset.id); });
    });
  } catch (e) {
    $("scans-grid").innerHTML = `<div class="empty" style="grid-column:1/-1">Could not load the gallery (${esc(e.message)}).</div>`;
  }
}

// ── boot ───────────────────────────────────────────────────────────────────
const FALLBACK_PRESETS = [
  { name: "fast", label: "Fast", blurb: "12 views, 16 timesteps, half-resolution training. About 18 minutes.", eta_minutes: 18 },
  { name: "standard", label: "Standard", blurb: "24 views on one orbit, 30 timesteps, full resolution. About 40 minutes.", eta_minutes: 40, default: true },
  { name: "wide", label: "Wide", blurb: "48 views on one orbit (7.5 degree spacing), 30 timesteps, full resolution. About 70 minutes.", eta_minutes: 70 },
  { name: "full", label: "Full", blurb: "48 views on three pitch rings, 48 timesteps, view-dependent colour. About 90 minutes.", eta_minutes: 90 },
];

// The RunPod proxy answers 404/timeouts for ~30 s after the API restarts; never let one failed
// request leave the page half-rendered.
async function getJSONRetry(url, attempts = 4) {
  let delay = 1200;
  for (let i = 0; i < attempts; i++) {
    try { return await getJSON(url); } catch (e) { if (i === attempts - 1) throw e; await new Promise((r) => setTimeout(r, delay)); delay *= 1.6; }
  }
}

(async function boot() {
  wireUpload();
  presets = FALLBACK_PRESETS; selectedPreset = "standard"; renderPresets();
  refreshJobs();
  refreshScans();
  getJSONRetry(`${API}/presets`).then((p) => { if (Array.isArray(p) && p.length) { presets = p; if (!presets.some((x) => x.name === selectedPreset)) selectedPreset = (presets.find((x) => x.default) || presets[0]).name; renderPresets(); } }).catch(() => {});
  getJSONRetry(`${API}/health`).then((h) => { health = h; renderReadiness(); }).catch(() => {});
  setInterval(async () => { try { health = await getJSON(`${API}/health`); renderReadiness(); } catch (_) {} }, 30000);
  document.querySelectorAll(".nav .links a[href^='#']").forEach((a) => a.addEventListener("click", () => {
    document.querySelectorAll(".nav .links a").forEach((x) => x.classList.remove("active")); a.classList.add("active");
  }));
})();
