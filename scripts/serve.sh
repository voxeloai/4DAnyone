#!/usr/bin/env bash
# serve.sh — (re)start the prototype API + web app on the pod, inside tmux session "api".
#   bash /workspace/4DAnyone/scripts/serve.sh            # port 8000 (RunPod proxies it as https://<pod>-8000.proxy.runpod.net)
#   PORT=8001 bash scripts/serve.sh
set -euo pipefail
PORT=${PORT:-8000}
source /workspace/activate.sh
export XDG_RUNTIME_DIR=/tmp
export FDA_SPIRULA=${FDA_SPIRULA:-/workspace/spirula/spirula}
mkdir -p "$FDA_APPDATA"
tmux kill-session -t api 2>/dev/null || true
tmux new-session -d -s api "cd $FDA_REPO && source /workspace/activate.sh && export XDG_RUNTIME_DIR=/tmp FDA_SPIRULA=$FDA_SPIRULA && \
  uvicorn app.api.main:app --host 0.0.0.0 --port $PORT --workers 1 --log-level info 2>&1 | tee -a $FDA_APPDATA/api.log"
sleep 3
curl -fsS "http://127.0.0.1:$PORT/api/v1/health" && echo && echo "API up on :$PORT (tmux session 'api')"

# Public entry point. RunPod's HTTP proxy (https://<pod>-8000.proxy.runpod.net) drops ~30-40 % of
# concurrent requests, which stalls streamed splat frames, so the demo URL is a Cloudflare quick
# tunnel (no account, ephemeral hostname, HTTP/2, handles parallel downloads fine). Restarting
# serve.sh gives a new hostname; set FDA_NO_TUNNEL=1 to skip.
if [ "${FDA_NO_TUNNEL:-0}" != "1" ]; then
    CF=/workspace/cloudflared/cloudflared
    if [ ! -x "$CF" ]; then
        mkdir -p /workspace/cloudflared
        curl -sSL -o "$CF" https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 && chmod +x "$CF"
    fi
    if tmux has-session -t tunnel 2>/dev/null && grep -q "trycloudflare.com" /workspace/cloudflared/tunnel.log 2>/dev/null; then
        echo "tunnel already up: $(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /workspace/cloudflared/tunnel.log | head -1)"
    else
        tmux kill-session -t tunnel 2>/dev/null || true
        rm -f /workspace/cloudflared/tunnel.log
        tmux new-session -d -s tunnel "$CF tunnel --no-autoupdate --url http://127.0.0.1:$PORT --protocol http2 2>&1 | tee /workspace/cloudflared/tunnel.log"
        for i in $(seq 1 20); do
            sleep 1
            URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /workspace/cloudflared/tunnel.log 2>/dev/null | head -1)
            [ -n "$URL" ] && break
        done
        echo "public URL: ${URL:-<tunnel not ready yet; check /workspace/cloudflared/tunnel.log>}"
    fi
fi
