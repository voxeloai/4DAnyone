#!/usr/bin/env bash
# prebake.sh — queue bundled 4DAnyone example clips through the running API so the gallery has
# content before anyone uploads. Run ON the pod after SMPL-X is installed.
#
#   bash scripts/prebake.sh                # 3 clips, preset standard
#   COUNT=6 PRESET=fast bash scripts/prebake.sh
#   CLIPS="2785536 5390224" bash scripts/prebake.sh   # pick by pexels id prefix
set -euo pipefail
API=${API:-http://127.0.0.1:8000}
PRESET=${PRESET:-standard}
COUNT=${COUNT:-3}
DATA=${FDA_DATA:-/workspace/data}
DEFAULT_CLIPS="2785536 5390224 7017803 6616344 5885633 8059623"
CLIPS=${CLIPS:-$DEFAULT_CLIPS}

ready=$(curl -s "$API/api/v1/health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ready"])')
if [ "$ready" != "True" ]; then
    echo "API not ready (SMPL-X / checkpoint / spirula missing): $(curl -s $API/api/v1/health)"; exit 1
fi
n=0
for id in $CLIPS; do
    [ "$n" -ge "$COUNT" ] && break
    f=$(ls "$DATA"/source/pexels/${id}*.mp4 2>/dev/null | head -1)
    [ -z "$f" ] && { echo "no clip for $id"; continue; }
    title="Pexels $id ($PRESET)"
    echo "queueing $(basename "$f") as '$title'"
    curl -s -F "video=@$f;type=video/mp4" -F "preset=$PRESET" -F "title=$title" "$API/api/v1/jobs" | python3 -c 'import json,sys; j=json.load(sys.stdin); print("  ->", j["id"], j["status"])'
    n=$((n+1))
done
echo "queued $n job(s); watch: curl -s $API/api/v1/jobs | python3 -m json.tool | head -40"
