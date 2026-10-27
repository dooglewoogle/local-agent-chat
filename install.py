#!/usr/bin/env python3
"""Install the agent chatroom for the current user.

Puts `chatroom` and `chatroom-ui` on PATH and wires up Claude Code and Codex
when they are present. Idempotent: run it again after pulling or moving the repo.

    python3 install.py --dry-run     show what would change, write nothing
    python3 install.py               install
    python3 install.py --uninstall   undo everything except data/

See README.md for what each step touches and why.
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
HOME = Path.home()

BACKUP = ".chatroom-bak"
CLAUDE_RULE = "Bash(chatroom:*)"
CODEX_RULE = 'prefix_rule(pattern=["chatroom"], decision="allow")'
BEGIN, END = "<!-- chatroom:begin -->", "<!-- chatroom:end -->"
CODEX_BLOCK = f"""{BEGIN}
## Local agent chatroom

Other coding agents on this machine coordinate through a file-based chatroom. At session start run `chatroom guide`, join with a descriptive name, and announce what you are working on. Use the local-chatroom skill for the quick reference.
{END}
"""


def short(p) -> str:
    s = str(p)
    return "~" + s[len(str(HOME)):] if s.startswith(str(HOME) + os.sep) else s


class Installer:
    def __init__(self, dry_run: bool, uninstall: bool):
        self.dry = dry_run
        self.uninstall = uninstall
        self.manual = []
        self.changes = 0
        self.cr = None  # the chatroom module, imported once preflight has checked the platform

    # --- reporting ---

    def ok(self, target, detail=""):
        print(f"  ok       {short(target)}  {detail}".rstrip())

    def changed(self, target, detail=""):
        self.changes += 1
        print(f"  {'would' if self.dry else 'changed':<8} {short(target)}  {detail}".rstrip())

    def skip(self, target, detail):
        print(f"  skipped  {short(target)}  {detail}")

    def todo(self, target, detail):
        self.manual.append(f"{short(target)}: {detail}")
        print(f"  manual   {short(target)}  {detail}")

    def warn(self, detail):
        print(f"  warning  {detail}")

    # --- primitives ---

    def write_text(self, path: Path, text: str):
        """Replace `path` atomically, keeping its mode, after a one-time backup."""
        if self.dry:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = path.with_name(path.name + BACKUP)
        if path.exists() and not backup.exists():
            shutil.copy2(path, backup)
        tmp = path.with_name(f".{path.name}.chatroom-tmp")
        tmp.write_text(text)
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)

    def link(self, dst: Path, src: Path, backup_dirs: bool = False):
        """Make `dst` a symlink to `src`. A real directory is moved aside first when
        `backup_dirs` is set; any other existing file is left for the user."""
        if self.uninstall:
            return self.unlink(dst, src)
        if dst.is_symlink():
            if dst.resolve() == src.resolve():
                return self.ok(dst, f"-> {short(src)}")
            old = os.readlink(dst)
            if not self.dry:
                dst.unlink()
                dst.symlink_to(src)
            return self.changed(dst, f"-> {short(src)} (was -> {old})")
        if dst.exists():
            backup = dst.with_name(dst.name + BACKUP)
            if not (backup_dirs and dst.is_dir()) or backup.exists():
                return self.todo(dst, f"exists and is not a symlink; move it away, then re-run (wanted -> {short(src)})")
            if not self.dry:
                dst.rename(backup)
                dst.symlink_to(src)
            return self.changed(dst, f"-> {short(src)} (old directory kept as {backup.name})")
        if not self.dry:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src)
        self.changed(dst, f"-> {short(src)}")

    def unlink(self, dst: Path, src: Path):
        if not dst.is_symlink() or dst.resolve() != src.resolve():
            return self.ok(dst, "not linked to this repo")
        backup = dst.with_name(dst.name + BACKUP)
        if not self.dry:
            dst.unlink()
            if backup.is_dir():
                backup.rename(dst)
        self.changed(dst, "removed" + (f", restored {backup.name}" if backup.is_dir() else ""))

    # --- steps ---

    def preflight(self, bin_dir: Path) -> bool:
        print("preflight")
        if not sys.platform.startswith("linux"):
            print("  error    Linux only: agent detection reads /proc and polling uses inotify.")
            return False
        if sys.version_info < (3, 8):
            print(f"  error    Python 3.8+ required, found {sys.version.split()[0]}.")
            return False
        self.ok("python", sys.version.split()[0])
        try:
            import tkinter  # noqa: F401
            self.ok("tkinter", "chat window available")
        except ImportError:
            self.warn("tkinter missing; agents work, the chat window does not. Install python3-tk (apt), "
                      "python3-tkinter (dnf) or tk (pacman).")
        sys.path.insert(0, str(REPO))
        import chatroom
        self.cr = chatroom
        if not chatroom.pids_visible():
            self.warn(f"pid 1 is '{chatroom.pid1_comm()}', not a recognised init system: presence will be "
                      f"TTL-only and reaping disabled. See README, Troubleshooting.")
        path_dirs = {Path(p).resolve() for p in os.environ.get("PATH", "").split(os.pathsep) if p}
        if bin_dir.resolve() not in path_dirs:
            self.warn(f"{short(bin_dir)} is not on PATH. Add  export PATH=\"{bin_dir}:$PATH\"  to ~/.profile "
                      f"and start a new login shell.")
        return True

    def init_repo(self):
        if self.uninstall:
            return self.ok(REPO / "data", "kept (delete it yourself to drop history)")
        for name in ("chatroom.py", "ui.sh"):
            p = REPO / name
            if not os.access(p, os.X_OK):
                if not self.dry:
                    p.chmod(p.stat().st_mode | 0o111)
                self.changed(p, "made executable")
        data = REPO / "data"
        if data.is_dir():
            return self.ok(data)
        if not self.dry:
            self.cr.ensure_dirs()
        self.changed(data, "created")

    def ui_name(self, name):
        """The name the chat window joins as, unless --name or $CHATROOM_NAME says otherwise."""
        path = self.cr.name_file()
        if self.uninstall:
            return self.ok(path, "kept") if path.exists() else None
        current = path.read_text().strip() if path.exists() else None
        if name is None or name == current:
            return self.ok(path if current else "chat window", f"joins as '{self.cr.default_human_name()}'"
                           + ("" if name else " (set with --ui-name)"))
        if not self.cr.is_valid_name(name):
            return self.todo(path, f"'{name}' is not a valid name (no '/' or '--')")
        self.write_text(path, name + "\n")
        self.changed(path, f"chat window joins as '{name}'" + (f" (was '{current}')" if current else ""))

    def bin_links(self, bin_dir: Path):
        self.link(bin_dir / "chatroom", REPO / "chatroom.py")
        self.link(bin_dir / "chatroom-ui", REPO / "ui.sh")

    def claude(self):
        root = HOME / ".claude"
        if not root.is_dir():
            return self.skip(root, "Claude Code not found")
        self.link(root / "skills" / "local-chatroom", REPO / "skill", backup_dirs=True)
        self.claude_permission(root / "settings.json")

    def claude_permission(self, path: Path):
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
            allow = data.setdefault("permissions", {}).setdefault("allow", [])
            if not isinstance(allow, list):
                raise ValueError("permissions.allow is not a list")
        except (ValueError, AttributeError) as e:
            return self.todo(path, f"could not parse ({e}); add \"{CLAUDE_RULE}\" to permissions.allow by hand")
        present = CLAUDE_RULE in allow
        if present != self.uninstall:
            return self.ok(path, f"{CLAUDE_RULE} {'not present' if self.uninstall else 'allowed'}")
        if self.uninstall:
            allow.remove(CLAUDE_RULE)
            if not allow:  # drop the containers if we were their only content
                del data["permissions"]["allow"]
                if not data["permissions"]:
                    del data["permissions"]
        else:
            allow.append(CLAUDE_RULE)
        self.write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        self.changed(path, f"{'removed' if self.uninstall else 'allowed'} {CLAUDE_RULE}")

    def codex(self):
        root = HOME / ".codex"
        if not root.is_dir():
            return self.skip(root, "Codex not found")
        self.link(root / "skills" / "local-chatroom", REPO / "skill", backup_dirs=True)
        self.codex_agents_md(root / "AGENTS.md")
        self.codex_rule(root / "rules" / "default.rules")
        self.codex_writable_root(root / "config.toml")

    def codex_agents_md(self, path: Path):
        text = path.read_text() if path.exists() else ""
        start, end = text.find(BEGIN), text.find(END)
        has_block = 0 <= start < end
        if self.uninstall:
            if not has_block:
                return self.ok(path, "no chatroom block")
            new = text[:start].rstrip("\n") + "\n" + text[end + len(END):].lstrip("\n")
            self.write_text(path, new.lstrip("\n"))
            return self.changed(path, "removed chatroom block")
        if has_block:
            current = text[start:end + len(END)] + "\n"
            if current == CODEX_BLOCK:
                return self.ok(path, "chatroom block present")
            self.write_text(path, text[:start] + CODEX_BLOCK + text[end + len(END):].lstrip("\n"))
            return self.changed(path, "updated chatroom block")
        if "chatroom" in text.lower():
            return self.ok(path, "already mentions the chatroom (hand-written; left alone)")
        self.write_text(path, (text.rstrip("\n") + "\n\n" if text.strip() else "") + CODEX_BLOCK)
        self.changed(path, "added chatroom block")

    def codex_rule(self, path: Path):
        lines = path.read_text().splitlines() if path.exists() else []
        present = CODEX_RULE in (ln.strip() for ln in lines)
        if present != self.uninstall:
            return self.ok(path, f"chatroom rule {'not present' if self.uninstall else 'present'}")
        if self.uninstall:
            lines = [ln for ln in lines if ln.strip() != CODEX_RULE]
        else:
            lines.append(CODEX_RULE)
        self.write_text(path, "\n".join(lines) + "\n")
        self.changed(path, f"{'removed' if self.uninstall else 'added'} {CODEX_RULE}")

    def codex_writable_root(self, path: Path):
        """The Codex sandbox only lets commands write inside writable_roots."""
        data = str(REPO / "data")
        text = path.read_text() if path.exists() else ""
        listed = json.dumps(data) in text
        if self.uninstall:
            if listed:
                return self.todo(path, f"left as is; remove {json.dumps(data)} from writable_roots if unused")
            return self.ok(path, "data dir not listed")
        if listed:
            return self.ok(path, "data dir in writable_roots")
        if "sandbox_workspace_write" in text:
            return self.todo(path, f"add {json.dumps(data)} to writable_roots under [sandbox_workspace_write]")
        table = f"[sandbox_workspace_write]\nwritable_roots = [{json.dumps(data)}]\n"
        self.write_text(path, (text.rstrip("\n") + "\n\n" if text.strip() else "") + table)
        self.changed(path, "added [sandbox_workspace_write] writable_roots")


def main():
    ap = argparse.ArgumentParser(description="Install the agent chatroom for the current user.")
    ap.add_argument("--dry-run", action="store_true", help="Show what would change; write nothing")
    ap.add_argument("--uninstall", action="store_true", help="Remove links and config entries (data/ is kept)")
    ap.add_argument("--bin-dir", type=Path, default=HOME / ".local" / "bin", help="Where to link chatroom and chatroom-ui (default: ~/.local/bin)")
    ap.add_argument("--no-claude", action="store_true", help="Leave Claude Code alone")
    ap.add_argument("--no-codex", action="store_true", help="Leave Codex alone")
    ap.add_argument("--ui-name", help="Name the chat window joins as (saved to ~/.config/chatroom/name)")
    args = ap.parse_args()

    inst = Installer(dry_run=args.dry_run, uninstall=args.uninstall)
    mode = "uninstall" if args.uninstall else "install"
    print(f"chatroom {mode} (repo {short(REPO)}){'  [dry run: nothing is written]' if args.dry_run else ''}")
    if not inst.preflight(args.bin_dir.expanduser()):
        sys.exit(1)

    print(mode)
    inst.init_repo()
    inst.ui_name(args.ui_name)
    inst.bin_links(args.bin_dir.expanduser())
    if not args.no_claude:
        inst.claude()
    if not args.no_codex:
        inst.codex()

    if inst.manual:
        print("\nDo these by hand:")
        for m in inst.manual:
            print(f"  - {m}")
    if not args.uninstall and not args.dry_run:
        print("\nTry it:  chatroom members   |   chatroom-ui")
    elif args.dry_run and inst.changes:
        print(f"\n{inst.changes} change(s) pending. Re-run without --dry-run to apply.")


if __name__ == "__main__":
    main()
