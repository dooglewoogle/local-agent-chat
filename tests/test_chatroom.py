"""Tests for the chatroom CLI.

Each test runs a private copy of the scripts in a temporary directory, so the
live data/ directory that agents on this machine use is never touched.

    python3 -m unittest discover -s tests -v
"""

import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class ChatroomTestCase(unittest.TestCase):
    FILES = ("chatroom.py", "chatroom_ui.py", "AGENTS.md")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for f in self.FILES:
            if (REPO / f).exists():
                shutil.copy(REPO / f, self.root / f)
        self.data = self.root / "data"

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args, check=True, timeout=15):
        r = subprocess.run(
            [sys.executable, str(self.root / "chatroom.py"), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        if check and r.returncode != 0:
            self.fail(f"chatroom {' '.join(args)} exited {r.returncode}\n{r.stderr}")
        return r

    def join(self, name):
        # Track this test process: it is alive for the duration of the test.
        self.run_cli("join", "--name", name, "--pid", str(os.getpid()))


class MessagingTest(ChatroomTestCase):
    def test_send_then_poll(self):
        self.run_cli("send", "--name", "a", "-m", "hello room")
        r = self.run_cli("poll", "--no-wait")
        self.assertIn("a: hello room", r.stdout)

    def test_dm_poll_with_partner(self):
        self.join("a")
        self.join("b")
        self.run_cli("dm", "--name", "a", "--to", "b", "-m", "hello b")
        r = self.run_cli("dm-poll", "--name", "b", "--with", "a", "--no-wait")
        self.assertIn("hello b", r.stdout)

    def test_room_names_cannot_escape_data_dir(self):
        attempts = [
            ("create-room", "--room", "../../escaped"),
            ("send", "--name", "a", "--room", "../../escaped", "-m", "x"),
            ("poll", "--room", "../../escaped", "--no-wait"),
            ("join", "--name", "../escaped", "--pid", str(os.getpid())),
        ]
        for args in attempts:
            r = self.run_cli(*args, check=False)
            self.assertNotEqual(r.returncode, 0, args)
        self.assertFalse((self.root / "escaped").exists())
        self.assertFalse((self.data / "escaped.json").exists())

    def test_double_dash_names_rejected(self):
        # '--' separates the two parties in a DM file name.
        r = self.run_cli("join", "--name", "a--b", "--pid", str(os.getpid()), check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_malformed_line_is_skipped(self):
        self.run_cli("send", "--name", "a", "-m", "good one")
        with open(self.data / "rooms" / "general" / "messages.jsonl", "a") as f:
            f.write('{"sender": "a", "ts": 1, "mess\n')
            f.write('["not", "a", "message"]\n')
        self.run_cli("send", "--name", "a", "-m", "good two")
        r = self.run_cli("poll", "--no-wait")
        self.assertIn("good one", r.stdout)
        self.assertIn("good two", r.stdout)
        r = self.run_cli("search", "good")
        self.assertIn("good two", r.stdout)

    def test_mentions_match_whole_names_only(self):
        for msg in ("@doug look at this", "dougal said hi", "ping doug.", "see sony-doug"):
            self.run_cli("send", "--name", "x", "-m", msg)
        r = self.run_cli("poll", "--no-wait", "--mentions", "doug")
        self.assertIn("@doug look at this", r.stdout)
        self.assertIn("ping doug.", r.stdout)
        self.assertNotIn("dougal", r.stdout)
        self.assertNotIn("sony-doug", r.stdout)

    def test_blocking_poll_wakes_on_new_message(self):
        self.run_cli("send", "--name", "a", "-m", "first")
        proc = subprocess.Popen(
            [sys.executable, str(self.root / "chatroom.py"), "poll", "--timeout", "20"],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            self.assertIn("first", proc.stdout.readline())  # history printed, now waiting
            start = time.monotonic()
            self.run_cli("send", "--name", "a", "-m", "second")
            out, _ = proc.communicate(timeout=10)
            self.assertIn("second", out)
            self.assertLess(time.monotonic() - start, 1.5)
        finally:
            proc.kill()
            proc.wait()

    def test_blocking_dm_poll_times_out_quietly(self):
        self.join("a")
        self.join("b")
        r = self.run_cli("dm-poll", "--name", "a", "--with", "b", "--timeout", "1")
        self.assertIn("No new messages in DM with b (waited 1s)", r.stdout)


class PresenceTest(ChatroomTestCase):
    def write_presence(self, name, **fields):
        presence = self.data / "presence"
        presence.mkdir(parents=True, exist_ok=True)
        (presence / f"{name}.json").write_text(json.dumps({"name": name, "agent": "test", **fields}))

    def test_pidless_presence_expires_after_ttl(self):
        self.write_presence("fresh", pid=None, last_seen=time.time())
        self.write_presence("stale", pid=None, last_seen=time.time() - 4 * 3600)
        r = self.run_cli("members")
        self.assertIn("Reaped stale agents: stale", r.stdout)
        self.assertIn("fresh", r.stdout)

    def test_reap_keeps_live_pid(self):
        self.join("alive")
        self.write_presence("dead", pid=2 ** 22 + 12345)  # above pid_max, never a real process
        r = self.run_cli("reap")
        self.assertIn("Reaped stale agent: dead", r.stdout)
        self.assertTrue((self.data / "presence" / "alive.json").exists())


class MiscTest(ChatroomTestCase):
    def test_guide_prints_agents_md(self):
        r = self.run_cli("guide")
        self.assertEqual(r.stdout, (REPO / "AGENTS.md").read_text())

    def test_default_human_name(self):
        def name(**env):
            env = dict(os.environ, XDG_CONFIG_HOME=str(self.root / "config"), **env)
            env = {k: v for k, v in env.items() if v is not None}
            code = "import chatroom; print(chatroom.default_human_name())"
            r = subprocess.run([sys.executable, "-c", code], cwd=self.root, env=env, capture_output=True, text=True)
            return r.stdout.strip()

        self.assertEqual(name(CHATROOM_NAME=None), getpass.getuser())
        (self.root / "config" / "chatroom").mkdir(parents=True)
        (self.root / "config" / "chatroom" / "name").write_text("bob\n")
        self.assertEqual(name(CHATROOM_NAME=None), "bob")
        self.assertEqual(name(CHATROOM_NAME="alice"), "alice")


if __name__ == "__main__":
    unittest.main()
