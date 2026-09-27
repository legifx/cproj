#!/usr/bin/env python3
"""End-to-end tests for cproj: every test runs the CLI in a throwaway HOME with fake projects.

No network: GitHub and the SSH server are switched off in the test config.
Run: python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

CPROJ = Path(__file__).resolve().parent.parent / "cproj.py"
DAY = 86400


class CprojTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cproj-test-"))
        self.home = self.tmp / "home"
        self.projects = self.home / "Projects"
        self.projects.mkdir(parents=True)
        (self.home / ".config/cproj").mkdir(parents=True)
        self.config({})
        self.env = {**os.environ, "HOME": str(self.home), "CLAUDE_CODE_SESSION_ID": "test-session",
                    "XDG_CONFIG_HOME": str(self.home / ".config"), "XDG_CACHE_HOME": str(self.home / ".cache"),
                    "XDG_STATE_HOME": str(self.home / ".local/state"), "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                    "WAYLAND_DISPLAY": "", "DISPLAY": ""}
        self.env.pop("CPROJ_CONFIG", None)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def config(self, extra):
        (self.home / ".config/cproj/config.json").write_text(json.dumps({"github": False, "server": None, **extra}))

    def project(self, name, files=None, age_days=0):
        d = self.projects / name
        d.mkdir()
        for f, c in (files or {"README.md": f"# {name}\n"}).items():
            (d / f).parent.mkdir(parents=True, exist_ok=True)
            (d / f).write_text(c)
        date = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - age_days * DAY))
        env = {**self.env, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        for c in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
            subprocess.run(["git", *c], cwd=d, env=env, check=True, capture_output=True)
        return d

    def run_cproj(self, *args, stdin=None, env=None):
        r = subprocess.run([sys.executable, str(CPROJ), *args], input=stdin, capture_output=True, text=True,
                           env={**self.env, **(env or {})}, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def badge(self, sid="test-session"):
        f = self.home / ".cache/cproj/sessions" / f"{sid}.json"
        return json.loads(f.read_text()) if f.exists() else None

    # ------------------------------------------------------------ picking

    def test_pick_exact_sets_badge_and_briefs(self):
        self.project("weather-station", {"CLAUDE.md": "# Weather\nRules here.\n", "docs/PROGRESS.md": "# Progress\nNext: sensors\n"})
        out = self.run_cproj("pick", "weather-station")
        self.assertTrue(out.startswith("CPROJ: PICKED weather-station"), out)
        self.assertIn("--- CLAUDE.md ---", out)
        self.assertIn("Next: sensors", out)
        self.assertEqual(self.badge()["name"], "weather-station")

    def test_pick_without_query_offers_a_choice(self):
        self.project("alpha")
        self.assertTrue(self.run_cproj("pick").startswith("CPROJ: CHOOSE"))

    def test_ambiguous_and_no_match(self):
        self.project("api-gateway")
        self.project("api-docs")
        self.assertTrue(self.run_cproj("pick", "api").startswith("CPROJ: AMBIGUOUS"))
        self.assertTrue(self.run_cproj("pick", "zzzz").startswith("CPROJ: NO MATCH"))
        self.assertIsNone(self.badge())

    def test_claude_md_imports_are_inlined(self):
        self.project("imp", {"CLAUDE.md": "@AGENTS.md\n", "AGENTS.md": "Agent rules: be kind.\n"})
        self.assertIn("Agent rules: be kind.", self.run_cproj("pick", "imp"))

    # ------------------------------------------------------------ switching, handoffs, sessions

    def test_switch_reports_left_project_and_handoff_shows_in_brief(self):
        a = self.project("alpha")
        self.project("beta")
        self.run_cproj("pick", "alpha")
        (a / "new.txt").write_text("x")
        self.run_cproj("handoff", "Parser half done; next: tests.")
        out = self.run_cproj("pick", "beta")
        self.assertTrue(out.startswith("CPROJ: LEFT alpha"), out)
        self.assertIn("1 uncommitted files", out.splitlines()[0])
        self.assertIn("Parser half done", self.run_cproj("brief", "alpha"))

    def test_off_and_session_end_hook(self):
        self.project("alpha")
        self.run_cproj("pick", "alpha")
        self.assertTrue(self.run_cproj("off").startswith("CPROJ: DESELECTED alpha"))
        self.assertIsNone(self.badge())
        self.assertTrue(self.run_cproj("off").startswith("CPROJ: NO PROJECT"))
        self.run_cproj("pick", "alpha")
        self.run_cproj("session-end", stdin=json.dumps({"session_id": "test-session"}))
        self.assertIsNone(self.badge())
        self.assertIn("session end", self.run_cproj("brief", "alpha"))

    def test_sessions_are_found_in_transcripts(self):
        d = self.project("alpha")
        t = self.home / ".claude/projects/-home-x"
        t.mkdir(parents=True)
        (t / "abc-123.jsonl").write_text(
            json.dumps({"type": "user", "cwd": str(self.home)}) + "\n"
            + json.dumps({"type": "user", "cwd": str(d / "src")}) + "\n"
            + json.dumps({"type": "ai-title", "aiTitle": "Sensor drivers"}) + "\n")
        out = self.run_cproj("brief", "alpha")
        self.assertIn("Sensor drivers", out)
        self.assertIn("claude -r abc-123", out)

    def test_badge_slots_per_harness(self):
        self.project("alpha")
        no_claude = {"CLAUDE_CODE_SESSION_ID": ""}
        self.run_cproj("pick", "alpha", env={**no_claude, "CODEX_THREAD_ID": "t-42"})
        self.assertEqual(self.badge("codex-t-42")["name"], "alpha")
        self.run_cproj("pick", "alpha", env={**no_claude, "HERMES_INTERACTIVE": "1"})
        self.assertIsNotNone(self.badge("hermes"))
        self.run_cproj("pick", "alpha", env={**no_claude, "HERMES_INTERACTIVE": "1", "CPROJ_SESSION": "h-7"})
        self.assertIsNotNone(self.badge("h-7"))
        self.run_cproj("pick", "alpha", env=no_claude)
        self.assertIsNotNone(self.badge("shared"))
        self.assertIn("◆ alpha", self.run_cproj("current", env={**no_claude, "CPROJ_SESSION": "h-7"}))

    # ------------------------------------------------------------ status line

    def test_statusline_badge_and_cwd_fallback(self):
        self.project("alpha")
        self.run_cproj("pick", "alpha")
        line = self.run_cproj("statusline", stdin=json.dumps({"session_id": "test-session", "model": {"display_name": "M"}}))
        self.assertIn("◆ alpha", line)
        line = self.run_cproj("statusline", stdin=json.dumps({"session_id": "other", "cwd": str(self.projects / "beta/src")}))
        self.assertIn("◇ beta", line)
        self.assertIn("no project", self.run_cproj("statusline", stdin="{}"))

    # ------------------------------------------------------------ creating

    def test_new_checks_duplicates_then_creates(self):
        self.project("weather-station")
        self.assertIn("weather-station", self.run_cproj("pick", "new", "weather"))
        self.assertTrue(self.run_cproj("new", "weather-station").startswith("CPROJ: SIMILAR EXISTS"))
        out = self.run_cproj("new", "garden-bot", "--desc=Waters the plants.")
        self.assertIn("CPROJ: CREATED garden-bot", out)
        d = self.projects / "garden-bot"
        self.assertIn("Waters the plants.", (d / "docs/PROGRESS.md").read_text())
        log = subprocess.run(["git", "log", "--format=%s"], cwd=d, capture_output=True, text=True).stdout
        self.assertEqual(log.strip(), "Initial project skeleton")
        self.assertEqual(self.badge()["name"], "garden-bot")

    def test_new_uses_template_dir(self):
        tdir = self.home / "tmpl"
        (tdir / "docs").mkdir(parents=True)
        (tdir / "NOTES.md").write_text("# {name} — {desc}\n")
        self.config({"template_dir": str(tdir)})
        self.run_cproj("new", "tiny", "--desc=Small thing")
        self.assertEqual((self.projects / "tiny/NOTES.md").read_text(), "# tiny — Small thing\n")

    # ------------------------------------------------------------ cold storage and hiding

    def test_cold_storage_hides_and_wakes(self):
        self.project("fresh")
        self.project("fossil", age_days=400)
        self.assertNotIn("fossil", self.run_cproj("list"))
        self.assertTrue(self.run_cproj("pick", "fossil").startswith("CPROJ: IN COLD STORAGE"))
        self.assertIn("fossil", self.run_cproj("pick", "cold"))
        self.assertTrue(self.run_cproj("pick", "cold", "fossil").startswith("CPROJ: WOKEN fossil"))
        self.assertIn("fossil", self.run_cproj("list"))  # the visit warmed it up

    def test_ignore_rules(self):
        self.project("keep-me")
        self.project("old-backup")
        self.run_cproj("ignore", "*backup*")
        out = self.run_cproj("list")
        self.assertIn("keep-me", out)
        self.assertNotIn("old-backup", out)

    # ------------------------------------------------------------ status overview

    def test_status_lists_loose_ends(self):
        d = self.project("dirty")
        self.project("clean")
        (d / "wip.txt").write_text("x")
        out = self.run_cproj("pick", "status")
        self.assertTrue(out.startswith("CPROJ: STATUS — 1 projects"), out)
        self.assertIn("dirty", out)
        self.assertIn("1 uncommitted", out)
        self.assertNotIn("clean ", out)

    # ------------------------------------------------------------ optional metadata

    def test_registry_and_index_metadata(self):
        self.project("alpha")
        reg = self.home / "reg.json"
        reg.write_text(json.dumps({"projects": [{"name": "alpha", "path": str(self.projects / "alpha"),
                                                 "progress": 40, "status": "active", "open_items": ["Write docs"]}]}))
        idx = self.projects / "INDEX.md"
        idx.write_text("| Project | What | Status | Last | Entry |\n|---|---|---|---|---|\n"
                       "| **alpha** | A sensor hub. | active | 2026-01-01 | `alpha/README.md` |\n")
        self.config({"registry_json": str(reg), "index_md": str(idx), "skip_dirs": []})
        out = self.run_cproj("pick", "alpha")
        self.assertIn("A sensor hub.", out)
        self.assertIn("Registry: 40 %", out)
        self.assertIn("- Write docs", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
