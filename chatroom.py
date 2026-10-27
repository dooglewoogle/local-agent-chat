#!/usr/bin/env python3
"""Agent chatroom: file-based messaging for coding agents (Claude Code, Codex, Hermes, ...).

State lives in data/ next to this script (layout in README.md). Stdlib only.
Linux only: /proc is used to find the agent process, inotify for fast polling.
"""

import argparse
import ctypes
import ctypes.util
import fcntl
import getpass
import json
import os
import re
import select
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
PRESENCE_DIR = DATA_DIR / "presence"
ROOMS_DIR = DATA_DIR / "rooms"
DM_DIR = DATA_DIR / "dm"

# A presence record without a host pid (sandboxed harness) expires this long
# after its last activity. join/send/dm refresh last_seen.
PRESENCE_TTL = 3 * 3600

# Blocking polls re-read at least this often, even without an inotify event.
POLL_INTERVAL = 2

# Process names (comm) of harnesses we recognise, shells we skip when walking
# up the tree, and session-level processes we must never adopt as "the agent".
KNOWN_AGENTS = {"claude", "codex", "hermes", "aider", "gemini", "opencode", "goose", "amp"}
SHELLS = {"bash", "sh", "zsh", "dash", "fish", "rtk"}
STOP = {"tmux", "tmux: server", "sshd", "systemd", "init", "login"}
# pid 1 names of real init systems. Anything else at pid 1 means we are inside a
# PID namespace (Codex sandbox, container) where host pids are invisible.
INIT_COMMS = {"systemd", "init", "runit", "s6-svscan", "openrc-init", "dinit"}
# Environment variables that identify a harness even when its process is not visible.
# Checked in order. Variables a harness sets fresh for its own session (Codex) come
# before ones that are inherited by any child process (CLAUDECODE), so a Codex
# session launched from inside Claude Code is still labelled codex.
ENV_HINTS = [("CODEX_SESSION_ID", "codex"), ("CODEX_THREAD_ID", "codex"), ("HERMES_SESSION_ID", "hermes"),
             ("AIDER_MODEL", "aider"), ("CLAUDECODE", "claude")]


# --- Paths ---


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


# --- Names ---


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


def name_file() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "chatroom" / "name"


def default_human_name() -> str:
    """Display name for the UI: $CHATROOM_NAME, else ~/.config/chatroom/name, else the login name."""
    name = os.environ.get("CHATROOM_NAME")
    if not name:
        try:
            name = name_file().read_text().strip()
        except OSError:
            pass
    if not name:
        try:
            name = getpass.getuser()
        except (KeyError, OSError):
            name = "human"
    return name


# --- Storage ---


def append_jsonl(path: Path, entry: dict) -> None:
    """Append one JSON line under an exclusive lock, flushed before unlock."""
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(entry) + "\n")
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def read_messages(path: Path) -> list:
    """Read all messages from a jsonl file. Lines that do not parse as a message
    (a write in progress, a hand edit) are skipped rather than fatal."""
    messages = []
    if not path.exists():
        return messages
    with open(path) as f:
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


def list_rooms() -> list:
    ensure_dirs()
    return sorted(d.name for d in ROOMS_DIR.iterdir() if d.is_dir())


def create_room(room: str) -> bool:
    """Create `room` (already validated). Returns False if it already existed."""
    try:
        room_path(room).mkdir(parents=True)
    except FileExistsError:
        return False
    messages_path(room).touch()
    return True


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


# --- Liveness ---


def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


def pid1_comm():
    try:
        return Path("/proc/1/comm").read_text().strip()
    except OSError:
        return None


def pids_visible() -> bool:
    """False inside a PID namespace (Codex sandbox, containers): host pids are invisible there,
    so pid-based liveness checks would wrongly report every other agent as dead."""
    return pid1_comm() in INIT_COMMS


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


# --- Agent detection ---


def env_agent_label():
    for var, label in ENV_HINTS:
        if os.environ.get(var):
            return label
    return None


def label_from_comm(comm):
    """'codex-linux-sandbox' -> 'codex'. Used for pid 1 inside a sandbox namespace."""
    for label in sorted(KNOWN_AGENTS, key=len, reverse=True):
        if comm == label or (comm or "").startswith(label + "-"):
            return label
    return None


def _ancestors():
    """Yield (pid, comm) for each ancestor of this process, nearest first."""
    pid = os.getpid()
    try:
        while pid > 1:
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat.rsplit(")", 1)[1].split()[1])
            yield pid, Path(f"/proc/{pid}/comm").read_text().strip()
    except (OSError, ValueError, IndexError):
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
        return None, label_from_comm(pid1_comm()) or env_label or "sandboxed", None
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


# --- Waiting for new messages ---

IN_MODIFY = 0x00000002
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_NONBLOCK = 0x00000800


class DirWatch:
    """Wait for any change in a directory via inotify, or just sleep if inotify
    is unavailable. Events are not parsed: callers re-read what they care about."""

    def __init__(self, directory: Path):
        self.fd = -1
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            fd = libc.inotify_init1(IN_NONBLOCK)
            if fd < 0:
                return
            if libc.inotify_add_watch(fd, str(directory).encode(), IN_MODIFY | IN_CREATE | IN_MOVED_TO) < 0:
                os.close(fd)
                return
            self.fd = fd
        except (OSError, AttributeError):
            pass

    def wait(self, timeout: float) -> None:
        if self.fd < 0:
            time.sleep(timeout)
            return
        if select.select([self.fd], [], [], timeout)[0]:
            try:
                os.read(self.fd, 65536)  # drain
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        if self.fd >= 0:
            os.close(self.fd)  # also drops the watch
            self.fd = -1


# --- Output ---


def fmt_ts(ts: float, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return datetime.fromtimestamp(ts).strftime(fmt)


def print_messages(messages):
    for m in messages:
        to = f" → {m['to']}" if m.get("to") else ""
        print(f"[{fmt_ts(m['ts'])}] {m['sender']}{to}: {m['message']}")


def poll_file(path: Path, args, label: str):
    """Print recent messages from `path`, then (unless --no-wait) block until a
    new one arrives or --timeout expires. Shared by poll and dm-poll --with."""
    everything = read_messages(path)
    messages = everything
    if getattr(args, "since", None) is not None:
        messages = [m for m in messages if m["ts"] > args.since]
    pattern = mention_pattern(args.mentions) if getattr(args, "mentions", None) else None
    if pattern:
        messages = [m for m in messages if pattern.search(m["message"])]
    if args.last is not None:
        messages = messages[-args.last:]
    if messages:
        print_messages(messages)

    if args.no_wait:
        if not messages:
            print(f"No new messages in {label}.")
        return

    # The file is append-only, so anything past the lines already seen is new.
    seen = len(everything)
    deadline = time.monotonic() + args.timeout
    sys.stdout.flush()
    try:
        with DirWatch(path.parent) as watch:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                watch.wait(min(remaining, POLL_INTERVAL))
                fresh = read_messages(path)
                if len(fresh) < seen:  # truncated or replaced
                    seen = 0
                new, seen = fresh[seen:], len(fresh)
                if pattern:
                    new = [m for m in new if pattern.search(m["message"])]
                if new:
                    print_messages(new)
                    return
    except KeyboardInterrupt:
        pass

    if not messages:
        print(f"No new messages in {label} (waited {args.timeout}s).")


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

    now = time.time()
    data = {
        "name": name,
        "agent": agent,
        "pid": agent_pid,
        "project": str(Path(args.project).resolve()),
        "joined_at": now,
        "last_seen": now,
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
    entry = {"sender": validate_name(args.name), "ts": time.time(), "message": args.message}
    append_jsonl(messages_path(room), entry)
    touch_presence(entry["sender"])
    print(f"[{fmt_ts(entry['ts'], '%H:%M:%S')}] Sent to #{room}")


def cmd_dm(args):
    ensure_dirs()
    target = validate_name(args.to)
    if not presence_path(target).exists():
        print(f"Agent '{target}' is not registered. Use 'members' to see who's online.", file=sys.stderr)
        sys.exit(1)
    entry = {"sender": validate_name(args.name), "to": target, "ts": time.time(), "message": args.message}
    append_jsonl(dm_path(entry["sender"], target), entry)
    touch_presence(entry["sender"])
    print(f"[{fmt_ts(entry['ts'], '%H:%M:%S')}] DM sent to {target}")


def cmd_dm_poll(args):
    """With --with: read (and by default wait on) one DM thread. Without: list
    recent DMs across all of this agent's threads, without waiting."""
    ensure_dirs()
    name = validate_name(args.name)
    if args.with_agent:
        partner = validate_name(args.with_agent)
        poll_file(dm_path(name, partner), args, label=f"DM with {partner}")
        return

    messages = []
    for f in sorted(DM_DIR.glob("*.jsonl")):
        if name in f.stem.split("--"):
            messages.extend(read_messages(f))
    messages.sort(key=lambda m: m["ts"])
    if args.last is not None:
        messages = messages[-args.last:]
    if messages:
        print_messages(messages)
    else:
        print(f"No DMs for {name}.")


def cmd_poll(args):
    ensure_dirs()
    room = require_room(args.room)
    if args.last is None:
        # A mentions check-in wants a short digest; a full read wants more history.
        args.last = 5 if args.mentions else 20
    label = f"#{room}" if not args.mentions else f"#{room} mentioning {args.mentions}"
    poll_file(messages_path(room), args, label=label)


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
        print(f"  {data.get('name', '?')}  agent={data.get('agent', '?')}  pid={data.get('pid') or '-'}"
              f"  project={data.get('project', '?')}  joined={fmt_ts(data.get('joined_at', 0))}")


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
    room = validate_name(args.room, "room")
    if create_room(room):
        print(f"Created room #{room}")
    else:
        print(f"Room #{room} already exists.")


def cmd_search(args):
    """Search message history across rooms (or one room) and DMs by keyword."""
    ensure_dirs()
    pattern = re.compile(re.escape(args.query), re.IGNORECASE)
    rooms = [validate_name(args.room, "room")] if args.room else list_rooms()

    sources = [(f"#{r}", messages_path(r)) for r in rooms]
    if not args.room:
        sources += [("DM", f) for f in sorted(DM_DIR.glob("*.jsonl"))]

    results = []
    for where, path in sources:
        for m in read_messages(path):
            if pattern.search(m["message"]) or pattern.search(m["sender"]):
                results.append((where, m))
    results.sort(key=lambda r: r[1]["ts"])
    if args.last:
        results = results[-args.last:]

    if not results:
        print(f"No messages matching '{args.query}'.")
        return
    for where, m in results:
        src = f"DM {m['sender']}→{m.get('to', '?')}" if where == "DM" else f"{where} {m['sender']}"
        print(f"[{fmt_ts(m['ts'])}] {src}: {m['message']}")


def cmd_reap(_args):
    """Remove stale presence files (safe to run from cron)."""
    ensure_dirs()
    if not pids_visible():
        print("Skipping: host pids are not visible from here (sandbox).", file=sys.stderr)
        return
    for name in reap_dead_agents():
        print(f"Reaped stale agent: {name}")


def cmd_guide(_args):
    """Print the agent protocol, so instructions can say 'run chatroom guide' instead of naming a path."""
    print((HERE / "AGENTS.md").read_text(), end="")


def cmd_ui(args):
    """Open the Tk chat window for a human participant."""
    sys.path.insert(0, str(HERE))
    import chatroom_ui
    chatroom_ui.run(name=args.name or default_human_name())


# --- CLI ---


def main():
    parser = argparse.ArgumentParser(
        prog="chatroom",
        description="File-based chatroom for coding agents (Claude Code, Codex, Hermes, ...).",
        epilog="Agents: run 'chatroom guide' for the protocol.",
    )
    sub = parser.add_subparsers(dest="command")

    p_join = sub.add_parser("join", help="Register as a chatroom member")
    p_join.add_argument("--name", help="Agent name (default: <project>-<pid>)")
    p_join.add_argument("--project", default=os.getcwd(), help="Project path (default: cwd)")
    p_join.add_argument("--pid", type=int, help="Explicit PID to track (default: auto-detect the agent process)")
    p_join.add_argument("--agent", help="Agent label, e.g. claude, codex, hermes (default: auto-detect)")

    p_leave = sub.add_parser("leave", help="Unregister from the chatroom")
    p_leave.add_argument("--name", required=True, help="Agent name")

    p_send = sub.add_parser("send", help="Send a message to a room")
    p_send.add_argument("--name", required=True, help="Your agent name")
    p_send.add_argument("--room", default="general", help="Room name (default: general)")
    p_send.add_argument("--message", "-m", required=True, help="Message text")

    p_dm = sub.add_parser("dm", help="Send a direct message to an agent")
    p_dm.add_argument("--name", required=True, help="Your agent name")
    p_dm.add_argument("--to", required=True, help="Recipient agent name")
    p_dm.add_argument("--message", "-m", required=True, help="Message text")

    p_dmpoll = sub.add_parser("dm-poll", help="Read DMs (all threads, or wait on one with --with)")
    p_dmpoll.add_argument("--name", required=True, help="Your agent name")
    p_dmpoll.add_argument("--with", dest="with_agent", help="Only the thread with this agent, and wait for new messages")
    p_dmpoll.add_argument("--last", type=int, default=20, help="Show last N messages (default: 20)")
    p_dmpoll.add_argument("--timeout", type=int, default=100, help="With --with: how long to wait for new messages in seconds (default: 100)")
    p_dmpoll.add_argument("--no-wait", action="store_true", help="With --with: return immediately instead of blocking")

    p_poll = sub.add_parser("poll", help="Read messages from a room (blocks for new messages by default)")
    p_poll.add_argument("--room", default="general", help="Room name (default: general)")
    p_poll.add_argument("--since", type=float, help="Only messages after this Unix timestamp")
    p_poll.add_argument("--last", type=int, default=None, help="Show last N messages (default: 20, or 5 with --mentions)")
    p_poll.add_argument("--timeout", type=int, default=100, help="How long to wait for new messages in seconds (default: 100)")
    p_poll.add_argument("--no-wait", action="store_true", help="Return immediately instead of blocking for new messages")
    p_poll.add_argument("--mentions", help="Only show messages that @mention this name (quick check-in: --no-wait --mentions your-name)")

    p_search = sub.add_parser("search", help="Search message history by keyword")
    p_search.add_argument("query", help="Search term")
    p_search.add_argument("--room", help="Limit search to a specific room")
    p_search.add_argument("--last", type=int, default=20, help="Show last N results (default: 20)")

    sub.add_parser("members", help="List online agents")
    sub.add_parser("rooms", help="List available rooms")

    p_cr = sub.add_parser("create-room", help="Create a new room")
    p_cr.add_argument("--room", required=True, help="Room name")

    sub.add_parser("reap", help="Remove presence files of dead or expired agents")
    sub.add_parser("guide", help="Print the agent protocol (AGENTS.md)")

    p_ui = sub.add_parser("ui", help="Open the Tk chat window (for humans)")
    p_ui.add_argument("--name", help="Your display name (default: $CHATROOM_NAME, else your login name)")

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
        "guide": cmd_guide,
        "ui": cmd_ui,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
