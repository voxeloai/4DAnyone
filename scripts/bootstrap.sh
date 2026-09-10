#!/usr/bin/env bash
# bootstrap.sh — runs ON the RunPod pod (runpod-persistent-gpu-pod pattern, bypass-conda variant).
# Idempotent + sentinel-gated: safe to re-run after every stop/start. Everything heavy lives on the
# /workspace network volume; only apt packages + the uv binary are ephemeral (re-installed each boot).
#
#   bash /workspace/4DAnyone/scripts/bootstrap.sh          # full (skips finished phases)
#   FDA_SKIP_WEIGHTS=1 bash scripts/bootstrap.sh            # env only, no 21 GB download
#
# Layout on the volume:
#   /workspace/4DAnyone           repo (voxelo/main) + third_party/GVHMR submodule
#   /workspace/envs/fdanyone      uv venv, Python 3.11, torch 2.8+cu128, gsplat, fastapi
#   /workspace/models             4DAnyone model_dir (model.safetensors 12 GB, VAE, GVHMR ckpts, BiRefNet)
#   /workspace/models/body_models/smplx/SMPLX_NEUTRAL.npz   <- licence-gated, installed from a user-provided ZIP
#   /workspace/data/source/pexels 20 bundled example clips
#   /workspace/fdanyone-app       app state: uploads/, jobs/, scans/ (the gallery)
#   /workspace/node               Node 22 + @playcanvas/splat-transform (PLY -> SOG)
#   /workspace/activate.sh        source after every restart
set -euo pipefail

WS=/workspace
REPO_DIR=$WS/4DAnyone
REPO_REMOTE=${REPO_REMOTE:-https://github.com/voxeloai/4DAnyone.git}
BRANCH=${BRANCH:-voxelo/main}
VENV=$WS/envs/fdanyone
PY_VER=${PY_VER:-3.11}
MODELS=$WS/models
DATA=$WS/data
APPDATA=$WS/fdanyone-app
NODE_DIR=$WS/node
NODE_VER=${NODE_VER:-22.12.0}
ENV_SENTINEL=$VENV/.ok-v3

export DEBIAN_FRONTEND=noninteractive
export GIT_TERMINAL_PROMPT=0
export HF_HOME=$WS/hf-cache
export TORCH_EXTENSIONS_DIR=$WS/torch-ext
export UV_PYTHON_INSTALL_DIR=$WS/uv-python
export UV_CACHE_DIR=$WS/uv-cache
export UV_LINK_MODE=copy
export PIP_DISABLE_PIP_VERSION_CHECK=1

log() { echo "[bootstrap $(date +%H:%M:%S)] $*"; }

# ── 1. ephemeral system packages (every boot) ────────────────────────────────
log "apt packages"
apt-get update -qq >/dev/null
apt-get install -y -qq tmux ffmpeg libgl1 libglib2.0-0 build-essential ninja-build \
    git git-lfs curl ca-certificates unzip jq >/dev/null
mkdir -p "$WS/envs" "$MODELS" "$DATA" "$APPDATA"/uploads "$APPDATA"/jobs "$APPDATA"/scans \
    "$HF_HOME" "$TORCH_EXTENSIONS_DIR" "$WS/smplx"

# ── 2. uv (ephemeral binary, persistent python + cache on the volume) ─────────
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
    log "installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
fi
export PATH="$HOME/.local/bin:$PATH"

# ── 3. repo ───────────────────────────────────────────────────────────────────
if [ ! -d "$REPO_DIR/.git" ]; then
    log "cloning $REPO_REMOTE"
    git clone "$REPO_REMOTE" "$REPO_DIR"
fi
cd "$REPO_DIR"
git remote set-url origin "$REPO_REMOTE"
git fetch -q origin
git checkout -q "$BRANCH"
git pull -q --ff-only origin "$BRANCH" || log "WARN: pull --ff-only failed (local edits?) - continuing"
git submodule update --init --quiet third_party/GVHMR
log "repo at $(git rev-parse --short HEAD) on $BRANCH; GVHMR $(git -C third_party/GVHMR rev-parse --short HEAD)"

# ── 4. venv (sentinel-gated) ──────────────────────────────────────────────────
if [ ! -f "$ENV_SENTINEL" ]; then
    log "creating venv $VENV (python $PY_VER)"
    rm -rf "$VENV"
    uv venv --python "$PY_VER" "$VENV" >/dev/null
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
    log "installing 4DAnyone requirements (torch 2.8+cu128 from PyPI default)"
    uv pip install -r requirements.txt
    log "installing app + reconstruction deps"
    uv pip install "fastapi>=0.115" "uvicorn[standard]>=0.30" python-multipart aiofiles \
        "gsplat>=1.5" jaxtyping ninja plyfile scikit-image torchmetrics pyyaml hf_transfer
    python - <<'PY'
import torch, torchvision, av
print("torch", torch.__version__, "cuda", torch.version.cuda, "available", torch.cuda.is_available())
print("torchvision", torchvision.__version__, "| av", av.__version__)
PY
    touch "$ENV_SENTINEL"
else
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
    log "venv OK (sentinel $ENV_SENTINEL)"
fi

# ── 5. node + splat-transform on the volume (sentinel-gated) ──────────────────
if [ ! -x "$NODE_DIR/bin/node" ]; then
    log "installing node $NODE_VER to $NODE_DIR"
    mkdir -p "$NODE_DIR"
    curl -fsSL "https://nodejs.org/dist/v${NODE_VER}/node-v${NODE_VER}-linux-x64.tar.xz" \
        | tar -xJ --strip-components=1 -C "$NODE_DIR"
fi
export PATH="$NODE_DIR/bin:$PATH"
if [ ! -x "$NODE_DIR/bin/splat-transform" ]; then
    log "installing @playcanvas/splat-transform"
    npm install -g --silent @playcanvas/splat-transform >/dev/null
fi
log "node $(node --version) / splat-transform $(splat-transform --version 2>/dev/null | head -1 || echo unknown)"

# ── 6. weights (idempotent: HF snapshot into place; ~21 GB first time) ────────
export PYTHONPATH="$REPO_DIR"
if [ "${FDA_SKIP_WEIGHTS:-0}" != "1" ]; then
    log "ensuring model files in $MODELS (first run downloads ~21 GB)"
    python scripts/download_model.py --model_dir "$MODELS" --gvhmr_root "$REPO_DIR/third_party/GVHMR" | tail -2
    log "ensuring bundled example clips in $DATA/source/pexels"
    python scripts/download_example.py --data_dir "$DATA" | tail -1
fi

# ── 6b. SMPL-X (licence-gated; needs a user-provided ZIP or NPZ) ──────────────
SMPLX_TARGET=$MODELS/body_models/smplx/SMPLX_NEUTRAL.npz
if [ ! -f "$SMPLX_TARGET" ]; then
    SRC=""
    for cand in "$WS/smplx/models_smplx_v1_1.zip" "$WS/smplx/SMPLX_NEUTRAL.npz" "$WS/models_smplx_v1_1.zip"; do
        if [ -f "$cand" ]; then SRC="$cand"; break; fi
    done
    if [ -n "$SRC" ]; then
        log "installing SMPL-X from $SRC"
        python scripts/download_smplx.py --archive_path "$SRC" --model_dir "$MODELS" --gvhmr_root "$REPO_DIR/third_party/GVHMR"
    else
        log "!!! SMPL-X NOT INSTALLED. Register at https://smpl-x.is.tue.mpg.de/, download models_smplx_v1_1.zip,"
        log "!!! copy it to $WS/smplx/ and re-run this script. Inference cannot start without it."
    fi
else
    python scripts/download_smplx.py --model_dir "$MODELS" --gvhmr_root "$REPO_DIR/third_party/GVHMR" >/dev/null 2>&1 || true
    log "SMPL-X present"
fi

# ── 7. activate.sh ────────────────────────────────────────────────────────────
cat > "$WS/activate.sh" <<ACT
# source this after every pod start
export PATH="\$HOME/.local/bin:$NODE_DIR/bin:\$PATH"
source "$VENV/bin/activate"
export HF_HOME="$HF_HOME"
export TORCH_EXTENSIONS_DIR="$TORCH_EXTENSIONS_DIR"
export TORCH_CUDA_ARCH_LIST="\${TORCH_CUDA_ARCH_LIST:-9.0}"
export PYTHONPATH="$REPO_DIR"
export FDA_REPO="$REPO_DIR"
export FDA_MODELS="$MODELS"
export FDA_DATA="$DATA"
export FDA_APPDATA="$APPDATA"
export HF_HUB_ENABLE_HF_TRANSFER=1
cd "$REPO_DIR"
ACT
log "wrote $WS/activate.sh"

# ── 8. summary ────────────────────────────────────────────────────────────────
log "DONE. python=$(python --version 2>&1) / $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"
log "next: source /workspace/activate.sh && bash scripts/serve.sh"
