// Voxelo fork of PlayCanvas' official `scripts/esm/gsplat/gsplat-flipbook.mjs` (MIT, engine 2.22.1).
// Same design (streamed splat sequence, shared ref-counted asset cache, preload-ahead buffer) plus
// what a public demo behind flaky proxies needs:
//   - failed frame loads are retried with backoff instead of poisoning the buffer forever
//   - frames that keep failing are skipped so playback never deadlocks
//   - `onFrame(frameNum)` / `onError(url, attempts)` hooks and `loadedCount()` for UI
//   - a bounded number of in-flight downloads (preloadCount) rather than one burst
import { Script, Asset } from "playcanvas";

class AssetCache {
  static cache = new Map(); // url -> { asset, refCount, attempts, failed }

  static getAsset(url, app, onError) {
    const entry = this.cache.get(url);
    if (entry) { entry.refCount++; return entry.asset; }
    const asset = new Asset(url, "gsplat", { url }, { reorder: false });
    const rec = { asset, refCount: 1, attempts: 1, failed: false };
    this.cache.set(url, rec);
    asset.on("error", () => {
      rec.failed = true;
      onError?.(url, rec.attempts);
    });
    app.assets.add(asset);
    app.assets.load(asset);
    return asset;
  }

  /** Re-create and re-load a failed asset (keeps the refCount). Returns the new asset. */
  static retry(url, app, onError) {
    const rec = this.cache.get(url);
    if (!rec) return this.getAsset(url, app, onError);
    try { app.assets.remove(rec.asset); rec.asset.unload(); } catch (_) { /* ignore */ }
    const asset = new Asset(url, "gsplat", { url }, { reorder: false });
    rec.asset = asset; rec.failed = false; rec.attempts++;
    asset.on("error", () => { rec.failed = true; onError?.(url, rec.attempts); });
    app.assets.add(asset);
    app.assets.load(asset);
    return asset;
  }

  static record(url) { return this.cache.get(url); }

  static releaseAsset(url) {
    const entry = this.cache.get(url);
    if (entry) entry.refCount--;
  }

  static processPendingUnloads(app) {
    for (const [url, entry] of this.cache.entries()) {
      if (entry.refCount <= 0 && entry.asset.resource) {
        app.assets.remove(entry.asset);
        entry.asset.unload();
        this.cache.delete(url);
      }
    }
  }
}

const PlayMode = { Once: "once", Loop: "loop", Bounce: "bounce" };

class GSplatFlipbook extends Script {
  static scriptName = "gsplatFlipbook";

  fps = 30;
  folder = "";
  filenamePattern = "frame_{frame:04}.sog";
  startFrame = 1;
  endFrame = 100;
  playMode = PlayMode.Loop;
  playing = true;
  preloadCount = 6;
  /** retries per frame before it is skipped */
  maxRetries = 4;
  /** base delay before a retry (ms), doubled each attempt */
  retryDelayMs = 800;
  onFrame = null;
  onError = null;

  initialize() {
    this.currentFrame = this.startFrame;
    this.frameTime = 0;
    this.direction = 1;
    this.currentAsset = null;
    this.currentAssetUrl = null;
    this.preloadedFrames = [];
    this._retryTimers = new Map();
    this.skipped = new Set();
    if (!this.entity.gsplat) {
      console.error("GSplatFlipbook: Entity must have a gsplat component with unified=true");
      return;
    }
    this._onErr = (url, attempts) => {
      this.onError?.(url, attempts);
      if (attempts > this.maxRetries) {
        const frameNum = this._frameFromUrl(url);
        if (frameNum !== null) this.skipped.add(frameNum);
        this.preloadedFrames = this.preloadedFrames.filter((f) => f.url !== url);
        return;
      }
      if (this._retryTimers.has(url)) return;
      const delay = this.retryDelayMs * Math.pow(2, attempts - 1);
      this._retryTimers.set(url, setTimeout(() => {
        this._retryTimers.delete(url);
        const asset = AssetCache.retry(url, this.app, this._onErr);
        for (const f of this.preloadedFrames) if (f.url === url) f.asset = asset;
        if (this.currentAssetUrl === url) {
          this.currentAsset = asset;
          asset.once("load", () => { if (this.entity.gsplat && this.currentAssetUrl === url) this.entity.gsplat.asset = asset; });
        }
      }, delay));
    };
    this.loadFrame(this.currentFrame);
  }

  update(dt) {
    if (!this.playing) return;
    this.frameTime += dt;
    if (this.frameTime >= 1 / this.fps) {
      this.frameTime = 0;
      // drop frames that were given up on, then advance if the next one is ready
      while (this.preloadedFrames.length && this.skipped.has(this.preloadedFrames[0].frameNum)) this.preloadedFrames.shift();
      if (this.preloadedFrames.length > 0 && this.preloadedFrames[0].asset.loaded && this.preloadedFrames[0].asset.resource) {
        this.switchToNextFrame();
      } else {
        this.maintainPreloadBuffer();
      }
    }
    AssetCache.processPendingUnloads(this.app);
  }

  /** frames currently decoded and ready (for a buffering indicator) */
  loadedCount() { return this.preloadedFrames.filter((f) => f.asset.loaded && f.asset.resource).length; }

  switchToNextFrame() {
    const next = this.preloadedFrames.shift();
    if (!next) return;
    if (this.currentAssetUrl) AssetCache.releaseAsset(this.currentAssetUrl);
    if (this.entity.gsplat) this.entity.gsplat.asset = next.asset;
    this.currentAsset = next.asset;
    this.currentAssetUrl = next.url;
    this.currentFrame = next.frameNum;
    this.onFrame?.(this.currentFrame);
    this.maintainPreloadBuffer();
  }

  _step(frame, dir) {
    if (this.playMode === "bounce") {
      let n = frame + dir;
      if (n > this.endFrame) { n = this.endFrame - 1; dir = -1; }
      if (n < this.startFrame) { n = this.startFrame + 1; dir = 1; }
      return [Math.max(this.startFrame, Math.min(this.endFrame, n)), dir];
    }
    if (this.playMode === "loop") {
      const n = frame + 1;
      return [n > this.endFrame ? this.startFrame : n, 1];
    }
    const n = frame + 1;
    return [n <= this.endFrame ? n : null, 1];
  }

  maintainPreloadBuffer() {
    let guard = 0;
    while (this.preloadedFrames.length < this.preloadCount && guard++ < 512) {
      let frame, dir;
      if (this.preloadedFrames.length === 0) {
        [frame, dir] = this._step(this.currentFrame, this.direction);
      } else {
        const last = this.preloadedFrames[this.preloadedFrames.length - 1];
        [frame, dir] = this._step(last.frameNum, last.dir);
      }
      if (frame === null) { if (this.playMode === "once" && this.preloadedFrames.length === 0) this.playing = false; break; }
      if (this.skipped.has(frame)) {
        // keep the chain moving past a dead frame
        this.preloadedFrames.push({ frameNum: frame, url: null, asset: { loaded: false, resource: null }, dir });
        continue;
      }
      const url = this.getFramePath(frame);
      const asset = AssetCache.getAsset(url, this.app, this._onErr);
      this.preloadedFrames.push({ frameNum: frame, url, asset, dir });
    }
    // purge placeholder entries for skipped frames at the head
    while (this.preloadedFrames.length && this.preloadedFrames[0].url === null) this.preloadedFrames.shift();
    if (this.preloadedFrames.length) this.direction = this.preloadedFrames[this.preloadedFrames.length - 1].dir;
  }

  loadFrame(frameNum) {
    const url = this.getFramePath(frameNum);
    const asset = AssetCache.getAsset(url, this.app, this._onErr);
    this.currentAssetUrl = url;
    this.currentAsset = asset;
    this.currentFrame = frameNum;
    const apply = () => { if (this.entity.gsplat) this.entity.gsplat.asset = asset; this.onFrame?.(frameNum); this.maintainPreloadBuffer(); };
    if (asset.loaded && asset.resource) apply(); else asset.once("load", apply);
  }

  _frameFromUrl(url) {
    for (let f = this.startFrame; f <= this.endFrame; f++) if (this.getFramePath(f) === url) return f;
    return null;
  }

  getFramePath(frameNum) {
    let filename = this.filenamePattern.replace(/\{frame(?::(\d+))?\}/g, (m, pad) => pad ? frameNum.toString().padStart(parseInt(pad, 10), "0") : frameNum.toString());
    let path = this.folder;
    if (path && !path.endsWith("/")) path += "/";
    return path + filename;
  }

  play() { this.playing = true; }
  pause() { this.playing = false; }
  stop() { this.playing = false; this.currentFrame = this.startFrame; this.direction = 1; this.frameTime = 0; this._flush(); this.loadFrame(this.currentFrame); }

  seekToFrame(frameNum) {
    if (frameNum < this.startFrame || frameNum > this.endFrame) return;
    this.frameTime = 0;
    this._flush();
    this.loadFrame(frameNum);
  }

  _flush() {
    for (const f of this.preloadedFrames) if (f.url) AssetCache.releaseAsset(f.url);
    this.preloadedFrames = [];
    if (this.currentAssetUrl) AssetCache.releaseAsset(this.currentAssetUrl);
    this.currentAssetUrl = null;
  }

  destroy() {
    for (const t of this._retryTimers.values()) clearTimeout(t);
    this._retryTimers.clear();
    this._flush();
  }
}

export { GSplatFlipbook, AssetCache };
