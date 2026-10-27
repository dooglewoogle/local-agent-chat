#!/usr/bin/env bash
# Open the chatroom window, detached from this terminal. Installed as chatroom-ui.
#
#   chatroom-ui                 join under the default name: $CHATROOM_NAME,
#                               else ~/.config/chatroom/name, else your login name
#   chatroom-ui --name alice    join under another name
#   chatroom-ui --fg            stay attached (Ctrl-C closes the window)
#
# Log: /tmp/chatroom-ui.log

set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
LOG=/tmp/chatroom-ui.log
FG=0
ARGS=()
for a in "$@"; do
    case "$a" in
        --fg) FG=1 ;;
        *) ARGS+=("$a") ;;
    esac
done

if ! python3 -c "import tkinter" 2>/dev/null; then
    echo "python3-tk is not installed (sudo apt install python3-tk)" >&2
    exit 1
fi

if [ "$FG" = 1 ]; then
    exec python3 "$HERE/chatroom.py" ui "${ARGS[@]}"
fi

# Refuse to open a second window.
if pgrep -f "$HERE/chatroom.py ui" >/dev/null; then
    echo "chatroom window is already open (pid $(pgrep -f "$HERE/chatroom.py ui" | head -1))"
    exit 0
fi

setsid nohup python3 "$HERE/chatroom.py" ui "${ARGS[@]}" >"$LOG" 2>&1 < /dev/null &
echo "chatroom window started (pid $!, log $LOG)"
