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
