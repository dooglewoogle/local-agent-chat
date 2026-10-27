#!/usr/bin/env python3
"""Tk chat window for a human participant in the agent chatroom.

Launch: python3 chatroom.py ui [--name doug]   or   python3 chatroom_ui.py [--name doug]

Stdlib only. Tails the jsonl files by byte offset on a 500 ms timer (no threads,
no inotify), discovers new rooms / DM threads / members every 2 s, and writes
through the same locked append helper the CLI uses.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import simpledialog, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chatroom as cr  # noqa: E402

TICK_MS = 500
DISCOVER_EVERY_TICKS = 4
INITIAL_LINES = 200

BG = "#14213d"
BG_ALT = "#1b2a4a"
FG = "#f2f4f8"
DIM = "#9aa5b8"
MENTION_BG = "#4a3a1a"
PALETTE = ["#8fb8ff", "#f0a35e", "#d99bf5", "#6fd3c7", "#f5d76e", "#ff9aa2", "#a8c686", "#c8b7a6"]


def sender_colour(name: str) -> str:
    h = int(hashlib.md5(name.encode()).hexdigest(), 16)
    return PALETTE[h % len(PALETTE)]


class Tail:
    """Incremental reader for one jsonl file. Skips bad lines, survives truncation."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.buf = b""

    def read_new(self) -> list:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return []
        if size < self.offset:  # truncated or replaced
            self.offset, self.buf = 0, b""
        if size == self.offset:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            chunk = f.read(size - self.offset)
        self.offset = size
        lines = (self.buf + chunk).split(b"\n")
        self.buf = lines.pop()  # trailing partial line, if any
        out = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out


class Pane:
    """One notebook tab: a room, a DM thread, or the read-only all-DMs view."""

    def __init__(self, app, kind: str, target, title: str, paths: list):
        self.app = app
        self.kind = kind  # "room" | "dm" | "alldm"
        self.target = target
        self.title = title
        self.unread = 0
        self.tails = {p: Tail(p) for p in paths}

        self.frame = ttk.Frame(app.nb)
        self.text = tk.Text(
            self.frame, wrap="word", bg=BG, fg=FG, insertbackground=FG,
            relief="flat", padx=8, pady=6, state="disabled", font=app.font, highlightthickness=0,
        )
        sb = ttk.Scrollbar(self.frame, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.text.tag_configure("ts", foreground=DIM)
        self.text.tag_configure("mention", background=MENTION_BG)
        self.text.tag_configure("arrow", foreground=DIM)
        app.nb.add(self.frame, text=title)

    def add_path(self, path: Path):
        if path not in self.tails:
            self.tails[path] = Tail(path)

    def initial_load(self):
        msgs = []
        for t in self.tails.values():
            msgs.extend(t.read_new())
        msgs.sort(key=lambda m: m.get("ts", 0))
        for m in msgs[-INITIAL_LINES:]:
            self.append(m)
        self.text.see("end")

    def poll(self):
        new = []
        for t in self.tails.values():
            new.extend(t.read_new())
        if not new:
            return
        new.sort(key=lambda m: m.get("ts", 0))
        for m in new:
            self.append(m)
        if self.app.current_pane() is not self:
            self.unread += len(new)
            self.app.nb.tab(self.frame, text=f"{self.title} ({self.unread})")

    def append(self, m: dict):
        ts = m.get("ts", 0)
        dt = datetime.fromtimestamp(ts)
        fmt = "%H:%M:%S" if dt.date() == datetime.now().date() else "%Y-%m-%d %H:%M:%S"
        sender = str(m.get("sender", "?"))
        msg = str(m.get("message", ""))
        at_bottom = self.text.yview()[1] >= 0.999

        self.text.configure(state="normal")
        self.text.insert("end", f"{dt.strftime(fmt)} ", "ts")
        tag = f"s:{sender}"
        self.text.tag_configure(tag, foreground=sender_colour(sender), font=self.app.bold)
        self.text.insert("end", sender, tag)
        if self.kind == "alldm":
            self.text.insert("end", f" → {m.get('to', '?')}", "arrow")
        self.text.insert("end", ": ")
        if self.app.mention_re.search(msg):
            self.text.insert("end", msg + "\n", "mention")
        else:
            self.text.insert("end", msg + "\n")
        self.text.configure(state="disabled")
        if at_bottom:
            self.text.see("end")

    def mark_read(self):
        if self.unread:
            self.unread = 0
            self.app.nb.tab(self.frame, text=self.title)


class App:
    def __init__(self, root: tk.Tk, name: str):
        self.root = root
        self.me = cr.validate_name(name)
        self.mention_re = re.compile(r"(?<![\w-])@?" + re.escape(self.me) + r"(?![\w-])", re.IGNORECASE)
        self.panes = {}  # key -> Pane
        self.ticks = 0

        root.title(f"chatroom — {self.me}")
        root.geometry("1100x700")
        root.configure(bg=BG)
        self.font = ("TkDefaultFont", 10)
        self.bold = ("TkDefaultFont", 10, "bold")
        self._style()

        outer = ttk.Frame(root)
        outer.pack(fill="both", expand=True)

        self.nb = ttk.Notebook(outer)
        self.nb.pack(side="left", fill="both", expand=True)
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._on_tab_change())

        side = ttk.Frame(outer, width=220)
        side.pack(side="right", fill="y")
        ttk.Label(side, text="Members").pack(anchor="w", padx=8, pady=(8, 2))
        self.members = tk.Listbox(side, bg=BG_ALT, fg=FG, relief="flat", highlightthickness=0,
                                  selectbackground="#3a5080", font=self.font, width=28)
        self.members.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.members.bind("<Double-Button-1>", lambda e: self._dm_selected())
        ttk.Label(side, text="double-click = DM", foreground=DIM).pack(anchor="w", padx=8, pady=(0, 8))

        bottom = ttk.Frame(root)
        bottom.pack(fill="x", padx=8, pady=6)
        ttk.Label(bottom, text=f"as {self.me}").pack(side="left", padx=(0, 8))
        self.entry = tk.Entry(bottom, bg=BG_ALT, fg=FG, insertbackground=FG, relief="flat", font=self.font,
                              highlightthickness=1, highlightbackground="#3a5080", highlightcolor="#6a8ac0")
        self.entry.pack(side="left", fill="x", expand=True, ipady=4)
        self.entry.bind("<Return>", lambda e: self.send())
        ttk.Button(bottom, text="Send", command=self.send).pack(side="left", padx=6)
        ttk.Button(bottom, text="+Room", command=self.new_room).pack(side="left")

        self.status = ttk.Label(root, text="", foreground=DIM)
        self.status.pack(fill="x", padx=8, pady=(0, 6))

        cr.ensure_dirs()
        self._register()
        self.discover()
        for p in self.panes.values():
            p.initial_load()
        self.refresh_members()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.entry.focus_set()
        root.after(TICK_MS, self.tick)

    # --- setup ---

    def _style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=FG, fieldbackground=BG_ALT)
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=BG_ALT, foreground=FG, padding=(10, 4))
        st.map("TNotebook.Tab", background=[("selected", "#3a5080")])
        st.configure("TButton", background=BG_ALT, foreground=FG)
        st.configure("TScrollbar", background=BG_ALT, troughcolor=BG)

    def _register(self):
        self.presence = cr.write_presence({
            "name": self.me,
            "agent": "human",
            "pid": os.getpid(),
            "project": str(Path(__file__).resolve().parent),
            "joined_at": time.time(),
        })

    # --- panes ---

    def current_pane(self):
        sel = self.nb.select()
        for p in self.panes.values():
            if str(p.frame) == sel:
                return p
        return None

    def _on_tab_change(self):
        p = self.current_pane()
        if p:
            p.mark_read()
            self.entry.focus_set()

    def ensure_room(self, room: str) -> Pane:
        key = f"room:{room}"
        if key not in self.panes:
            self.panes[key] = Pane(self, "room", room, f"#{room}", [cr.messages_path(room)])
        return self.panes[key]

    def ensure_dm(self, partner: str) -> Pane:
        key = f"dm:{partner}"
        if key not in self.panes:
            self.panes[key] = Pane(self, "dm", partner, f"@{partner}", [cr.dm_path(self.me, partner)])
        return self.panes[key]

    def ensure_alldm(self) -> Pane:
        key = "alldm"
        if key not in self.panes:
            self.panes[key] = Pane(self, "alldm", None, "All DMs", [])
        return self.panes[key]

    def discover(self):
        """Pick up new rooms, DM threads involving me, and feed the all-DMs pane."""
        new_panes = []
        for room in cr.list_rooms():
            if f"room:{room}" not in self.panes:
                new_panes.append(self.ensure_room(room))
        alldm = self.ensure_alldm()
        for f in sorted(cr.DM_DIR.glob("*.jsonl")):
            parts = f.stem.split("--")
            if self.me in parts and len(parts) == 2:
                partner = parts[1] if parts[0] == self.me else parts[0]
                if f"dm:{partner}" not in self.panes:
                    new_panes.append(self.ensure_dm(partner))
            if f not in alldm.tails:
                alldm.add_path(f)
                if self.ticks:  # after startup, show the new thread's history as unread
                    pass
        if alldm not in new_panes and not self.ticks:
            new_panes.append(alldm)
        return new_panes

    def refresh_members(self):
        rows = []
        for m in cr.read_presence(alive_only=True):
            rows.append(f"{m.get('name', '?')}   [{m.get('agent', '?')}]")
        current = list(self.members.get(0, "end"))
        if rows != current:
            self.members.delete(0, "end")
            for r in rows:
                self.members.insert("end", r)

    # --- loop ---

    def tick(self):
        self.ticks += 1
        try:
            if self.ticks % DISCOVER_EVERY_TICKS == 0:
                for p in self.discover():
                    p.initial_load()
                self.refresh_members()
            for p in list(self.panes.values()):
                p.poll()
        except Exception as e:  # keep the loop alive; report in the status bar
            self.set_status(f"error: {e}")
        self.root.after(TICK_MS, self.tick)

    # --- actions ---

    def set_status(self, text: str):
        self.status.configure(text=text)

    def send(self):
        msg = self.entry.get().strip()
        if not msg:
            return
        pane = self.current_pane()
        if pane is None or pane.kind == "alldm":
            self.set_status("Pick a room or DM tab to send to.")
            return
        entry = {"sender": self.me, "ts": time.time(), "message": msg}
        if pane.kind == "room":
            cr.append_jsonl(cr.messages_path(pane.target), entry)
            self.set_status(f"sent to #{pane.target}")
        else:
            if not cr.presence_path(pane.target).exists():
                self.set_status(f"'{pane.target}' is not registered; DM not sent.")
                return
            entry["to"] = pane.target
            cr.append_jsonl(cr.dm_path(self.me, pane.target), entry)
            self.set_status(f"DM sent to {pane.target}")
        self.entry.delete(0, "end")

    def _dm_selected(self):
        sel = self.members.curselection()
        if not sel:
            return
        name = self.members.get(sel[0]).split("   [")[0]
        if name == self.me:
            return
        pane = self.ensure_dm(name)
        self.nb.select(pane.frame)

    def new_room(self):
        name = simpledialog.askstring("New room", "Room name:", parent=self.root)
        if not name:
            return
        name = name.strip().lstrip("#")
        if not name or "/" in name or name in (".", ".."):
            self.set_status("Invalid room name.")
            return
        rp = cr.room_path(name)
        if not rp.exists():
            rp.mkdir(parents=True)
            cr.messages_path(name).touch()
        self.nb.select(self.ensure_room(name).frame)

    def close(self):
        try:
            if self.presence.exists():
                data = json.loads(self.presence.read_text())
                if data.get("pid") == os.getpid():
                    self.presence.unlink()
        except (OSError, ValueError):
            pass
        self.root.destroy()


def run(name: str = "doug"):
    root = tk.Tk()
    App(root, name)
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(description="Tk chat window for the agent chatroom.")
    ap.add_argument("--name", default="doug", help="Your display name (default: doug)")
    run(ap.parse_args().name)


if __name__ == "__main__":
    main()
