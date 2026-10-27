#!/usr/bin/env bash
# Reaper — removes presence files for dead agent processes.
# Install: crontab -e → */30 * * * * ~/projects/chatroom/reaper.sh

PRESENCE_DIR="$(dirname "$(readlink -f "$0")")/data/presence"

if [ ! -d "$PRESENCE_DIR" ]; then
    exit 0
fi

for f in "$PRESENCE_DIR"/*.json; do
    [ -e "$f" ] || continue
    pid=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['pid'])" "$f" 2>/dev/null)
    if [ -z "$pid" ]; then
        rm -f "$f"
        continue
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
        name=$(basename "$f" .json)
        rm -f "$f"
        echo "Reaped stale agent: $name (pid $pid)"
    fi
done
