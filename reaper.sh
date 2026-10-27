#!/usr/bin/env bash
# Reaper — removes presence files for dead or expired agents.
# Install: crontab -e → */30 * * * * ~/projects/chatroom/reaper.sh
exec python3 "$(dirname "$(readlink -f "$0")")/chatroom.py" reap
