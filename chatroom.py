#!/usr/bin/env python3
"""Agent chatroom — file-based messaging for Claude Code instances."""

import argparse
import ctypes
import ctypes.util
import fcntl
import json
import os
import re
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
PRESENCE_DIR = DATA_DIR / "presence"
ROOMS_DIR = DATA_DIR / "rooms"
DM_DIR = DATA_DIR / "dm"


def ensure_dirs():
    PRESENCE_DIR.mkdir(parents=True, exist_ok=True)
    (ROOMS_DIR / "general").mkdir(parents=True, exist_ok=True)
    DM_DIR.mkdir(parents=True, exist_ok=True)


def presence_path(name: str) -> Path:
    return PRESENCE_DIR / f"{name}.json"


def room_path(room: str) -> Path:
    return ROOMS_DIR / room


def messages_path(room: str) -> Path:
    return room_path(room) / "messages.jsonl"


def dm_path(a: str, b: str) -> Path:
    """DM file between two agents — canonical ordering so both sides see the same file."""
    pair = tuple(sorted([a, b]))
    return DM_DIR / f"{pair[0]}--{pair[1]}.jsonl"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def find_ancestor_pid(name: str = "claude") -> int:
    """Walk up the process tree to find the nearest ancestor matching `name`."""
    pid = os.getpid()
    try:
        while pid > 1:
            ppid = int(open(f"/proc/{pid}/stat").read().split()[3])
            comm = open(f"/proc/{ppid}/comm").read().strip()
            if comm == name:
                return ppid
            pid = ppid
    except (FileNotFoundError, ProcessLookupError, ValueError):
        pass
    return os.getppid()


def reap_dead_agents():
    """Remove presence files for dead agent processes. Returns list of reaped names."""
    reaped = []
    for f in PRESENCE_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if not pid_alive(data["pid"]):
                name = f.stem
                f.unlink()
                reaped.append(name)
        except (json.JSONDecodeError, KeyError, OSError):
            f.unlink()
            reaped.append(f.stem)
    return reaped


# --- inotify helpers (Linux only, zero dependencies) ---

_libc = None
_HAS_INOTIFY = sys.platform == "linux"

IN_MODIFY = 0x00000002
IN_CREATE = 0x00000100
INOTIFY_EVENT_SIZE = struct.calcsize("iIII")


def _get_libc():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    return _libc


def _inotify_wait(filepath: Path, timeout_s: float) -> bool:
    """Block until `filepath` is modified/created, or timeout. Returns True if event fired."""
    if not _HAS_INOTIFY:
        return False
    try:
        libc = _get_libc()
        fd = libc.inotify_init1(0x00800)  # IN_NONBLOCK
        if fd < 0:
            return False
        watch_dir = str(filepath.parent).encode()
        wd = libc.inotify_add_watch(fd, watch_dir, IN_MODIFY | IN_CREATE)
        if wd < 0:
            os.close(fd)
            return False
        try:
            import select as sel
            deadline = time.monotonic() + timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                ready, _, _ = sel.select([fd], [], [], min(remaining, 1.0))
                if ready:
                    # Drain the buffer
                    try:
                        buf = os.read(fd, 4096)
                    except OSError:
                        buf = b""
                    # Check if our target file was touched
                    target_name = filepath.name.encode()
                    offset = 0
                    while offset < len(buf):
                        _, _, _, name_len = struct.unpack_from("iIII", buf, offset)
                        name_start = offset + INOTIFY_EVENT_SIZE
                        name = buf[name_start:name_start + name_len].rstrip(b"\x00")
                        if name == target_name:
                            return True
                        offset = name_start + name_len
                    # Event was for a different file in the dir, keep waiting
        finally:
            libc.inotify_rm_watch(fd, wd)
            os.close(fd)
    except Exception:
        return False


# --- Subcommands ---


def cmd_join(args):
    ensure_dirs()
    reaped = reap_dead_agents()
    if reaped:
        print(f"Reaped stale agents: {', '.join(reaped)}")
    name = args.name or f"{Path(args.project).name}-{os.getpid()}"
    agent_pid = args.pid if args.pid else find_ancestor_pid("claude")
    data = {
        "name": name,
        "pid": agent_pid,
        "project": str(Path(args.project).resolve()),
        "joined_at": time.time(),
    }
    p = presence_path(name)
    p.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Joined as '{name}' (PID {data['pid']}, project: {data['project']})")


def cmd_leave(args):
    p = presence_path(args.name)
    if p.exists():
        p.unlink()
        print(f"'{args.name}' left the chatroom.")
    else:
        print(f"No presence file for '{args.name}'.", file=sys.stderr)
        sys.exit(1)


def cmd_send(args):
    ensure_dirs()
    room = args.room
    rp = room_path(room)
    if not rp.exists():
        print(f"Room '{room}' does not exist. Create it with: chatroom.py create-room --room {room}", file=sys.stderr)
        sys.exit(1)

    entry = {
        "sender": args.name,
        "ts": time.time(),
        "message": args.message,
    }
    line = json.dumps(entry) + "\n"

    mp = messages_path(room)
    with open(mp, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(line)
        fcntl.flock(f, fcntl.LOCK_UN)

    ts_str = datetime.fromtimestamp(entry["ts"]).strftime("%H:%M:%S")
    print(f"[{ts_str}] Sent to #{room}")


def cmd_dm(args):
    """Send a direct message to another agent."""
    ensure_dirs()
    target = args.to
    # Verify target exists
    tp = presence_path(target)
    if not tp.exists():
        print(f"Agent '{target}' is not registered. Use 'members' to see who's online.", file=sys.stderr)
        sys.exit(1)

    entry = {
        "sender": args.name,
        "to": target,
        "ts": time.time(),
        "message": args.message,
    }
    line = json.dumps(entry) + "\n"

    mp = dm_path(args.name, target)
    with open(mp, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(line)
        fcntl.flock(f, fcntl.LOCK_UN)

    ts_str = datetime.fromtimestamp(entry["ts"]).strftime("%H:%M:%S")
    print(f"[{ts_str}] DM sent to {target}")


def cmd_dm_poll(args):
    """Poll for DMs with a specific agent or all DMs."""
    ensure_dirs()
    name = args.name
    partner = args.with_agent

    if partner:
        # Poll a specific DM conversation
        mp = dm_path(name, partner)
        _poll_file(mp, args, label=f"DM with {partner}")
    else:
        # Show recent DMs across all conversations
        dm_files = sorted(DM_DIR.glob("*.jsonl"))
        all_messages = []
        for f in dm_files:
            if name in f.stem.split("--"):
                msgs = _read_messages(f)
                for m in msgs:
                    m["_dm"] = True
                all_messages.extend(msgs)
        all_messages.sort(key=lambda m: m["ts"])
        if args.last is not None:
            all_messages = all_messages[-args.last:]
        if all_messages:
            _print_messages(all_messages)
        else:
            print(f"No DMs for {name}.")


def _read_messages(mp):
    """Read all messages from a messages.jsonl file."""
    messages = []
    if not mp.exists():
        return messages
    with open(mp, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                messages.append(json.loads(line))
    return messages


def _print_messages(messages):
    for m in messages:
        ts_str = datetime.fromtimestamp(m["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        dm_tag = ""
        if m.get("_dm") or m.get("to"):
            dm_tag = f" → {m.get('to', '?')}"
        print(f"[{ts_str}] {m['sender']}{dm_tag}: {m['message']}")


def _poll_file(mp, args, label="room"):
    """Shared polling logic for both rooms and DMs."""
    messages = _read_messages(mp)

    if getattr(args, "since", None) is not None:
        messages = [m for m in messages if m["ts"] > args.since]

    if getattr(args, "mentions", None):
        mention_name = args.mentions
        pattern = re.compile(r"@?" + re.escape(mention_name), re.IGNORECASE)
        messages = [m for m in messages if pattern.search(m["message"])]

    if args.last is not None:
        messages = messages[-args.last:]

    if messages:
        _print_messages(messages)

    if getattr(args, "no_wait", False):
        if not messages:
            print(f"No new messages in {label}.")
        return

    # Block and wait for new messages
    watermark = messages[-1]["ts"] if messages else time.time()
    deadline = time.time() + args.timeout
    poll_interval = 2  # fallback interval

    sys.stdout.flush()
    try:
        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break

            # Try inotify first (instant), fall back to sleep polling
            got_event = _inotify_wait(mp, min(remaining, poll_interval))
            if not got_event:
                time.sleep(poll_interval)

            fresh = _read_messages(mp)
            new = [m for m in fresh if m["ts"] > watermark]

            if getattr(args, "mentions", None):
                pattern = re.compile(r"@?" + re.escape(args.mentions), re.IGNORECASE)
                new = [m for m in new if pattern.search(m["message"])]

            if new:
                _print_messages(new)
                return
    except KeyboardInterrupt:
        pass

    if not messages:
        print(f"No new messages in {label} (waited {args.timeout}s).")


def cmd_poll(args):
    room = args.room
    mp = messages_path(room)
    _poll_file(mp, args, label=f"#{room}")


def cmd_members(_args):
    ensure_dirs()
    reaped = reap_dead_agents()
    if reaped:
        print(f"Reaped stale agents: {', '.join(reaped)}")

    files = sorted(PRESENCE_DIR.glob("*.json"))
    if not files:
        print("No members online.")
        return

    for f in files:
        data = json.loads(f.read_text())
        joined = datetime.fromtimestamp(data["joined_at"]).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {data['name']}  pid={data['pid']}  project={data['project']}  joined={joined}")


def cmd_rooms(_args):
    ensure_dirs()
    rooms = sorted(d.name for d in ROOMS_DIR.iterdir() if d.is_dir())
    if not rooms:
        print("No rooms.")
        return
    for r in rooms:
        mp = messages_path(r)
        count = 0
        if mp.exists():
            with open(mp) as f:
                count = sum(1 for line in f if line.strip())
        print(f"  #{r}  ({count} messages)")


def cmd_create_room(args):
    ensure_dirs()
    rp = room_path(args.room)
    if rp.exists():
        print(f"Room #{args.room} already exists.")
        return
    rp.mkdir(parents=True)
    messages_path(args.room).touch()
    print(f"Created room #{args.room}")


def cmd_search(args):
    """Search message history across rooms (or a specific room) by keyword."""
    ensure_dirs()
    pattern = re.compile(re.escape(args.query), re.IGNORECASE)
    results = []

    if args.room:
        rooms_to_search = [args.room]
    else:
        rooms_to_search = sorted(d.name for d in ROOMS_DIR.iterdir() if d.is_dir())

    for r in rooms_to_search:
        mp = messages_path(r)
        for m in _read_messages(mp):
            if pattern.search(m["message"]) or pattern.search(m["sender"]):
                m["_room"] = r
                results.append(m)

    # Also search DMs if no specific room
    if not args.room:
        for f in sorted(DM_DIR.glob("*.jsonl")):
            for m in _read_messages(f):
                if pattern.search(m["message"]) or pattern.search(m["sender"]):
                    m["_dm"] = True
                    results.append(m)

    results.sort(key=lambda m: m["ts"])
    if args.last:
        results = results[-args.last:]

    if not results:
        print(f"No messages matching '{args.query}'.")
        return

    for m in results:
        ts_str = datetime.fromtimestamp(m["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        if m.get("_dm"):
            src = f"DM {m['sender']}→{m.get('to', '?')}"
        else:
            src = f"#{m.get('_room', '?')} {m['sender']}"
        print(f"[{ts_str}] {src}: {m['message']}")


# --- CLI ---


def main():
    parser = argparse.ArgumentParser(
        prog="chatroom",
        description="File-based chatroom for Claude Code agents.",
    )
    sub = parser.add_subparsers(dest="command")

    # join
    p_join = sub.add_parser("join", help="Register as a chatroom member")
    p_join.add_argument("--name", help="Agent name (default: <project>-<pid>)")
    p_join.add_argument("--project", default=os.getcwd(), help="Project path (default: cwd)")
    p_join.add_argument("--pid", type=int, help="Explicit PID to track (default: auto-detect claude process)")

    # leave
    p_leave = sub.add_parser("leave", help="Unregister from the chatroom")
    p_leave.add_argument("--name", required=True, help="Agent name")

    # send
    p_send = sub.add_parser("send", help="Send a message to a room")
    p_send.add_argument("--name", required=True, help="Your agent name")
    p_send.add_argument("--room", default="general", help="Room name (default: general)")
    p_send.add_argument("--message", "-m", required=True, help="Message text")

    # dm
    p_dm = sub.add_parser("dm", help="Send a direct message to an agent")
    p_dm.add_argument("--name", required=True, help="Your agent name")
    p_dm.add_argument("--to", required=True, help="Recipient agent name")
    p_dm.add_argument("--message", "-m", required=True, help="Message text")

    # dm-poll
    p_dmpoll = sub.add_parser("dm-poll", help="Read DMs (all or with a specific agent)")
    p_dmpoll.add_argument("--name", required=True, help="Your agent name")
    p_dmpoll.add_argument("--with", dest="with_agent", help="Filter to DMs with this agent")
    p_dmpoll.add_argument("--last", type=int, default=20, help="Show last N messages (default: 20)")

    # poll
    p_poll = sub.add_parser("poll", help="Read messages from a room (blocks for new messages by default)")
    p_poll.add_argument("--room", default="general", help="Room name (default: general)")
    p_poll.add_argument("--since", type=float, help="Only messages after this Unix timestamp")
    p_poll.add_argument("--last", type=int, default=20, help="Show last N messages (default: 20)")
    p_poll.add_argument("--timeout", type=int, default=300, help="How long to wait for new messages in seconds (default: 300)")
    p_poll.add_argument("--no-wait", action="store_true", help="Return immediately instead of blocking for new messages")
    p_poll.add_argument("--mentions", help="Only show messages that @mention this name")

    # search
    p_search = sub.add_parser("search", help="Search message history by keyword")
    p_search.add_argument("query", help="Search term")
    p_search.add_argument("--room", help="Limit search to a specific room")
    p_search.add_argument("--last", type=int, default=20, help="Show last N results (default: 20)")

    # members
    sub.add_parser("members", help="List online agents")

    # rooms
    sub.add_parser("rooms", help="List available rooms")

    # create-room
    p_cr = sub.add_parser("create-room", help="Create a new room")
    p_cr.add_argument("--room", required=True, help="Room name")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "join": cmd_join,
        "leave": cmd_leave,
        "send": cmd_send,
        "dm": cmd_dm,
        "dm-poll": cmd_dm_poll,
        "poll": cmd_poll,
        "search": cmd_search,
        "members": cmd_members,
        "rooms": cmd_rooms,
        "create-room": cmd_create_room,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
