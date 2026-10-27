#!/usr/bin/env python3
"""Agent chatroom — file-based messaging for coding agents (Claude Code, Codex, Hermes, ...)."""

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


def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


# Process names (comm) of harnesses we recognise, shells we skip when walking
# up the tree, and session-level processes we must never adopt as "the agent".
KNOWN_AGENTS = {"claude", "codex", "hermes", "aider", "gemini", "opencode", "goose", "amp"}
SHELLS = {"bash", "sh", "zsh", "dash", "fish", "rtk"}
STOP = {"tmux", "tmux: server", "sshd", "systemd", "init", "login"}
# Environment variables that identify a harness even when its process is not visible.
# Checked in order. Variables a harness sets fresh for its own session (Codex) come
# before ones that are inherited by any child process (CLAUDECODE), so a Codex
# session launched from inside Claude Code is still labelled codex.
ENV_HINTS = [("CODEX_SESSION_ID", "codex"), ("CODEX_THREAD_ID", "codex"), ("HERMES_SESSION_ID", "hermes"),
             ("AIDER_MODEL", "aider"), ("CLAUDECODE", "claude")]


def env_agent_label():
    for var, label in ENV_HINTS:
        if os.environ.get(var):
            return label
    return None


def label_from_comm(comm: str):
    """'codex-linux-sandbox' -> 'codex'. Used for pid 1 inside a sandbox namespace."""
    for label in sorted(KNOWN_AGENTS, key=len, reverse=True):
        if comm == label or comm.startswith(label + "-"):
            return label
    return None


def sandbox_agent_label():
    try:
        return label_from_comm(Path("/proc/1/comm").read_text().strip())
    except OSError:
        return None


def _ancestors():
    """Yield (pid, comm) for each ancestor of this process, nearest first."""
    pid = os.getpid()
    try:
        while pid > 1:
            stat = open(f"/proc/{pid}/stat").read()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            comm = open(f"/proc/{ppid}/comm").read().strip()
            yield ppid, comm
            pid = ppid
    except (FileNotFoundError, ProcessLookupError, ValueError, IndexError):
        return


def detect_agent():
    """Find the harness process that owns this session.

    Returns (pid, agent_label, warning). A known agent is matched by comm name.
    Otherwise the nearest ancestor that is not a shell or python is adopted,
    unless it is a session-level process (tmux, sshd, ...), in which case we
    fall back to the parent pid and return a warning.

    Inside a PID namespace (Codex sandbox) no host pid exists; pid is None and
    the label comes from the environment. Such presence expires by PRESENCE_TTL.
    """
    env_label = env_agent_label()
    if not pids_visible():
        return None, sandbox_agent_label() or env_label or "sandboxed", None
    chain = list(_ancestors())
    for pid, comm in chain:
        if comm in KNOWN_AGENTS:
            return pid, comm, None
    for pid, comm in chain:
        if comm in SHELLS or comm.startswith("python"):
            continue
        if comm in STOP or pid == 1:
            break
        return pid, env_label or comm, None
    return (
        os.getppid(),
        env_label or "unknown",
        "could not identify the agent process; pass --pid <pid of your agent> so presence outlives this shell",
    )


def is_valid_name(name: str) -> bool:
    """Agent and room names become file names, so keep them path-safe. '--' is
    reserved: it separates the two parties in a DM file name."""
    return bool(name) and name not in (".", "..") and not any(s in name for s in ("/", "\x00", "--"))


def validate_name(name: str, kind: str = "name") -> str:
    if not is_valid_name(name):
        print(f"Invalid {kind} '{name}': must be non-empty and contain no '/' or '--'.", file=sys.stderr)
        sys.exit(1)
    return name


def require_room(room: str) -> str:
    validate_name(room, "room")
    if not room_path(room).is_dir():
        print(f"Room '{room}' does not exist. Create it with: chatroom create-room --room {room}", file=sys.stderr)
        sys.exit(1)
    return room


def mention_pattern(name: str):
    """Match `name` or `@name` as a whole name: 'doug' matches neither 'dougal' nor 'sony-doug'."""
    return re.compile(r"(?<![\w-])@?" + re.escape(name) + r"(?![\w-])", re.IGNORECASE)


def append_jsonl(path: Path, entry: dict) -> None:
    """Append one JSON line under an exclusive lock, flushed before unlock."""
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(entry) + "\n")
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def write_presence(data: dict) -> Path:
    """Atomically write a presence file (the reaper deletes unparsable ones)."""
    p = presence_path(data["name"])
    tmp = p.with_name(f"{p.stem}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, p)
    return p


def touch_presence(name: str) -> None:
    """Refresh last_seen for `name` if it has a presence record. Best effort."""
    p = presence_path(name)
    try:
        data = json.loads(p.read_text())
        data["last_seen"] = time.time()
        write_presence(data)
    except (OSError, ValueError):
        pass


def read_presence(alive_only: bool = False) -> list:
    """Return presence records as dicts. Never reaps; skips unparsable files."""
    members = []
    for f in sorted(PRESENCE_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if alive_only and not presence_alive(data):
            continue
        members.append(data)
    return members


def list_rooms() -> list:
    ensure_dirs()
    return sorted(d.name for d in ROOMS_DIR.iterdir() if d.is_dir())


# A presence record without a host pid (sandboxed harness) expires this long
# after its last activity. join/send/dm refresh last_seen.
PRESENCE_TTL = 3 * 3600


def pids_visible() -> bool:
    """False inside a PID namespace (Codex sandbox, containers): host pids are invisible there,
    so pid-based liveness checks would wrongly report every other agent as dead."""
    try:
        return Path("/proc/1/comm").read_text().strip() in ("systemd", "init")
    except OSError:
        return False


def presence_alive(data: dict) -> bool:
    pid = data.get("pid")
    if pid:
        if not pids_visible():
            return True  # cannot judge from inside a sandbox; leave it alone
        return pid_alive(pid)
    last = data.get("last_seen") or data.get("joined_at") or 0
    return (time.time() - last) < PRESENCE_TTL


def reap_dead_agents():
    """Remove presence files for dead agents. Returns list of reaped names."""
    reaped = []
    for f in PRESENCE_DIR.glob("*.json"):
        try:
            dead = not presence_alive(json.loads(f.read_text()))
        except (json.JSONDecodeError, OSError):
            dead = True
        if dead:
            try:
                f.unlink()
                reaped.append(f.stem)
            except OSError:
                pass
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
    name = validate_name(args.name or f"{Path(args.project).name}-{os.getpid()}")
    warning = None
    if args.pid:
        agent_pid, agent = args.pid, args.agent or "unknown"
    else:
        agent_pid, agent, warning = detect_agent()
        if args.agent:
            agent = args.agent

    existing = presence_path(name)
    if existing.exists():
        try:
            old = json.loads(existing.read_text())
            if old.get("pid") != agent_pid and pid_alive(old.get("pid")):
                print(f"Warning: '{name}' is already held by a live process (PID {old['pid']}); taking it over.", file=sys.stderr)
        except (json.JSONDecodeError, OSError):
            pass

    data = {
        "name": name,
        "agent": agent,
        "pid": agent_pid,
        "project": str(Path(args.project).resolve()),
        "joined_at": time.time(),
        "last_seen": time.time(),
    }
    write_presence(data)
    if agent_pid:
        print(f"Joined as '{name}' (agent={agent}, PID {agent_pid}, project: {data['project']})")
    else:
        hours = PRESENCE_TTL // 3600
        print(f"Joined as '{name}' (agent={agent}, sandboxed: no host pid visible, project: {data['project']})")
        print(f"Note: presence expires {hours}h after your last send/dm/join; re-join if you go quiet for longer.")
    if warning:
        print(f"Warning: {warning}", file=sys.stderr)


def cmd_leave(args):
    p = presence_path(validate_name(args.name))
    if p.exists():
        p.unlink()
        print(f"'{args.name}' left the chatroom.")
    else:
        print(f"No presence file for '{args.name}'.", file=sys.stderr)
        sys.exit(1)


def cmd_send(args):
    ensure_dirs()
    room = require_room(args.room)

    entry = {
        "sender": validate_name(args.name),
        "ts": time.time(),
        "message": args.message,
    }
    append_jsonl(messages_path(room), entry)
    touch_presence(entry["sender"])

    ts_str = datetime.fromtimestamp(entry["ts"]).strftime("%H:%M:%S")
    print(f"[{ts_str}] Sent to #{room}")


def cmd_dm(args):
    """Send a direct message to another agent."""
    ensure_dirs()
    target = validate_name(args.to)
    # Verify target exists
    tp = presence_path(target)
    if not tp.exists():
        print(f"Agent '{target}' is not registered. Use 'members' to see who's online.", file=sys.stderr)
        sys.exit(1)

    entry = {
        "sender": validate_name(args.name),
        "to": target,
        "ts": time.time(),
        "message": args.message,
    }
    append_jsonl(dm_path(args.name, target), entry)
    touch_presence(entry["sender"])

    ts_str = datetime.fromtimestamp(entry["ts"]).strftime("%H:%M:%S")
    print(f"[{ts_str}] DM sent to {target}")


def cmd_dm_poll(args):
    """Poll for DMs with a specific agent or all DMs."""
    ensure_dirs()
    name = validate_name(args.name)
    partner = args.with_agent and validate_name(args.with_agent)

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
    """Read all messages from a jsonl file. Lines that do not parse as a message
    (a write in progress, a hand edit) are skipped rather than fatal."""
    messages = []
    if not mp.exists():
        return messages
    with open(mp, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if isinstance(m, dict) and {"sender", "ts", "message"} <= m.keys():
                messages.append(m)
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

    pattern = mention_pattern(args.mentions) if getattr(args, "mentions", None) else None
    if pattern:
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

            if pattern:
                new = [m for m in new if pattern.search(m["message"])]

            if new:
                _print_messages(new)
                return
    except KeyboardInterrupt:
        pass

    if not messages:
        print(f"No new messages in {label} (waited {args.timeout}s).")


def cmd_poll(args):
    ensure_dirs()
    room = require_room(args.room)
    if args.last is None:
        # A mentions check-in wants a short digest; a full read wants more history.
        args.last = 5 if args.mentions else 20
    mp = messages_path(room)
    label = f"#{room}" if not args.mentions else f"#{room} mentioning {args.mentions}"
    _poll_file(mp, args, label=label)


def cmd_members(_args):
    ensure_dirs()
    reaped = reap_dead_agents()
    if reaped:
        print(f"Reaped stale agents: {', '.join(reaped)}")

    members = read_presence()
    if not members:
        print("No members online.")
        return

    for data in members:
        joined = datetime.fromtimestamp(data.get("joined_at", 0)).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {data.get('name', '?')}  agent={data.get('agent', '?')}  pid={data.get('pid') or '-'}  project={data.get('project', '?')}  joined={joined}")


def cmd_rooms(_args):
    rooms = list_rooms()
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
    rp = room_path(validate_name(args.room, "room"))
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
        rooms_to_search = [validate_name(args.room, "room")]
    else:
        rooms_to_search = list_rooms()

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


def cmd_reap(_args):
    """Remove stale presence files (used by reaper.sh from cron)."""
    ensure_dirs()
    if not pids_visible():
        print("Skipping: host pids are not visible from here (sandbox).", file=sys.stderr)
        return
    for name in reap_dead_agents():
        print(f"Reaped stale agent: {name}")


def cmd_ui(args):
    """Open the Tk chat window for a human participant."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import chatroom_ui
    chatroom_ui.run(name=args.name)


# --- CLI ---


def main():
    parser = argparse.ArgumentParser(
        prog="chatroom",
        description="File-based chatroom for coding agents (Claude Code, Codex, Hermes, ...).",
    )
    sub = parser.add_subparsers(dest="command")

    # join
    p_join = sub.add_parser("join", help="Register as a chatroom member")
    p_join.add_argument("--name", help="Agent name (default: <project>-<pid>)")
    p_join.add_argument("--project", default=os.getcwd(), help="Project path (default: cwd)")
    p_join.add_argument("--pid", type=int, help="Explicit PID to track (default: auto-detect the agent process)")
    p_join.add_argument("--agent", help="Agent label, e.g. claude, codex, hermes (default: auto-detect)")

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
    p_dmpoll.add_argument("--timeout", type=int, default=100, help="With --with: how long to wait for new messages in seconds (default: 100)")
    p_dmpoll.add_argument("--no-wait", action="store_true", help="With --with: return immediately instead of blocking")

    # poll
    p_poll = sub.add_parser("poll", help="Read messages from a room (blocks for new messages by default)")
    p_poll.add_argument("--room", default="general", help="Room name (default: general)")
    p_poll.add_argument("--since", type=float, help="Only messages after this Unix timestamp")
    p_poll.add_argument("--last", type=int, default=None, help="Show last N messages (default: 20, or 5 with --mentions)")
    p_poll.add_argument("--timeout", type=int, default=100, help="How long to wait for new messages in seconds (default: 100)")
    p_poll.add_argument("--no-wait", action="store_true", help="Return immediately instead of blocking for new messages")
    p_poll.add_argument("--mentions", help="Only show messages that @mention this name (quick check-in: --no-wait --mentions your-name)")

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

    # reap
    sub.add_parser("reap", help="Remove presence files of dead or expired agents")

    # ui
    p_ui = sub.add_parser("ui", help="Open the Tk chat window (for humans)")
    p_ui.add_argument("--name", default="doug", help="Your display name (default: doug)")

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
        "reap": cmd_reap,
        "ui": cmd_ui,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
