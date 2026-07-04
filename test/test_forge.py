#!/usr/bin/env python3
"""Tests for forge.py — FSM, next/rerun/verify, merge git gate."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import forge


def _run_cli(*args):
    """Run forge.main; return (exit_code, parsed_json, raw_stdout)."""
    if len(args) == 1 and isinstance(args[0], (list, tuple)):
        args = args[0]
    import io
    import sys

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    code = 0
    try:
        forge.main(list(args))
    except SystemExit as e:
        code = e.code or 0
    finally:
        sys.stdout = old
    text = buf.getvalue().strip()
    data = json.loads(text) if text else {}
    return code, data, text


class ForgeFsmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.forge_dir = self.root / ".forge"
        self.forge_dir.mkdir()
        # minimal git repo
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "t@test"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "t"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        (self.root / "README").write_text("hi\n")
        subprocess.run(["git", "add", "README"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "integration"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        self.base = "integration"

    def tearDown(self):
        self.tmp.cleanup()

    def _init_session(self, title="test"):
        code, data, _ = _run_cli(
            "--forge-dir", str(self.forge_dir), "init", "--title", title
        )
        self.assertEqual(code, 0)
        self.session = data["session"]
        _run_cli(
            "--forge-dir",
            str(self.forge_dir),
            "phase",
            "--session",
            self.session,
            "--to",
            "architect",
        )
        _run_cli(
            "--forge-dir",
            str(self.forge_dir),
            "phase",
            "--session",
            self.session,
            "--to",
            "tasks",
        )
        _run_cli(
            "--forge-dir",
            str(self.forge_dir),
            "phase",
            "--session",
            self.session,
            "--to",
            "implementing",
        )
        return self.session

    def _add_task(self, branch="feat/t1"):
        code, data, _ = _run_cli(
            [
                "--forge-dir",
                str(self.forge_dir),
                "task-add",
                "--session",
                self.session,
                "--type",
                "implement",
                "--spec",
                "do thing",
                "--impl-model",
                "fast",
                "--review-model",
                "strong",
                "--writes-scope",
                "modA",
            ]
        )
        self.assertEqual(code, 0)
        return data["task"], branch

    def _start_worktree(self, task, branch):
        wt = self.forge_dir / self.session / "worktrees" / branch.replace("/", "+")
        subprocess.run(
            ["git", "worktree", "add", str(wt), "-b", branch, self.base],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        code, data, _ = _run_cli(
            [
                "--forge-dir",
                str(self.forge_dir),
                "start",
                "--session",
                self.session,
                "--task",
                task,
                "--branch",
                branch,
                "--base",
                self.base,
            ]
        )
        self.assertEqual(code, 0)
        return wt

    def test_next_needs_fix_lists_rerun(self):
        self._init_session()
        task, branch = self._add_task()
        self._start_worktree(task, branch)
        for cmd in [
            ["submit", "--task", task, "--output", "done"],
            ["review-add", "--task", task, "--verdict", "problems", "--notes", "x"],
            ["needs-fix", "--task", task],
        ]:
            code, _, _ = _run_cli(
                ["--forge-dir", str(self.forge_dir), cmd[0], "--session", self.session]
                + cmd[1:]
            )
            self.assertEqual(code, 0, cmd)
        code, data, _ = _run_cli(
            [
                "--forge-dir",
                str(self.forge_dir),
                "next",
                "--session",
                self.session,
                "--task",
                task,
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(data["status"], "needs_fix")
        cmds = " ".join(s["cmd"] for s in data["steps"])
        self.assertIn("rerun", cmds)
        self.assertIn("submit", cmds)

    def test_rerun_then_submit_allows_review(self):
        self._init_session()
        task, branch = self._add_task()
        wt = self._start_worktree(task, branch)
        _run_cli(
            [
                "--forge-dir",
                str(self.forge_dir),
                "submit",
                "--session",
                self.session,
                "--task",
                task,
                "--output",
                "v1",
            ]
        )
        _run_cli(
            [
                "--forge-dir",
                str(self.forge_dir),
                "review-add",
                "--session",
                self.session,
                "--task",
                task,
                "--verdict",
                "problems",
            ]
        )
        _run_cli(
            [
                "--forge-dir",
                str(self.forge_dir),
                "needs-fix",
                "--session",
                self.session,
                "--task",
                task,
            ]
        )
        code, data, _ = _run_cli(
            [
                "--forge-dir",
                str(self.forge_dir),
                "rerun",
                "--session",
                self.session,
                "--task",
                task,
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(data["status"], "running")
        (wt / "f.txt").write_text("fix\n")
        subprocess.run(["git", "add", "f.txt"], cwd=wt, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "fix"],
            cwd=wt,
            check=True,
            capture_output=True,
        )
        _run_cli(
            [
                "--forge-dir",
                str(self.forge_dir),
                "submit",
                "--session",
                self.session,
                "--task",
                task,
                "--output",
                "v2",
            ]
        )
        code, _, _ = _run_cli(
            [
                "--forge-dir",
                str(self.forge_dir),
                "review-add",
                "--session",
                self.session,
                "--task",
                task,
                "--verdict",
                "clean",
            ]
        )
        self.assertEqual(code, 0)

    def test_merge_refuses_without_git_integration(self):
        self._init_session()
        task, branch = self._add_task()
        wt = self._start_worktree(task, branch)
        (wt / "f.txt").write_text("x\n")
        subprocess.run(["git", "add", "f.txt"], cwd=wt, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "work"],
            cwd=wt,
            check=True,
            capture_output=True,
        )
        _run_cli(
            [
                "--forge-dir",
                str(self.forge_dir),
                "submit",
                "--session",
                self.session,
                "--task",
                task,
                "--output",
                "done",
            ]
        )
        _run_cli(
            [
                "--forge-dir",
                str(self.forge_dir),
                "review-add",
                "--session",
                self.session,
                "--task",
                task,
                "--verdict",
                "clean",
            ]
        )
        import io
        import sys

        err = io.StringIO()
        old_err = sys.stderr
        sys.stderr = err
        code = 0
        try:
            forge.main(
                [
                    "--forge-dir",
                    str(self.forge_dir),
                    "merge",
                    "--session",
                    self.session,
                    "--task",
                    task,
                ]
            )
        except SystemExit as e:
            code = e.code
        finally:
            sys.stderr = old_err
        self.assertNotEqual(code, 0)
        self.assertIn("git merge not done", err.getvalue())

    def test_review_waive_allows_merge_after_git(self):
        self._init_session()
        task, branch = self._add_task()
        wt = self._start_worktree(task, branch)
        (wt / "f.txt").write_text("x\n")
        subprocess.run(["git", "add", "f.txt"], cwd=wt, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "work"],
            cwd=wt,
            check=True,
            capture_output=True,
        )
        _run_cli(
            [
                "--forge-dir",
                str(self.forge_dir),
                "submit",
                "--session",
                self.session,
                "--task",
                task,
                "--output",
                "done",
            ]
        )
        code, data, _ = _run_cli(
            [
                "--forge-dir",
                str(self.forge_dir),
                "review-waive",
                "--session",
                self.session,
                "--task",
                task,
                "--reason",
                "rename-only mechanical swap, no domain risk",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(data["verdict"], "waived")
        subprocess.run(
            ["git", "checkout", self.base],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "merge", "--no-ff", branch, "-m", "merge t1"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        code, data, _ = _run_cli(
            [
                "--forge-dir",
                str(self.forge_dir),
                "merge",
                "--session",
                self.session,
                "--task",
                task,
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(data["status"], "merged")

    def test_context_includes_exit_protocol(self):
        self._init_session()
        task, branch = self._add_task()
        self._start_worktree(task, branch)
        code, data, _ = _run_cli(
            [
                "--forge-dir",
                str(self.forge_dir),
                "context",
                "--session",
                self.session,
                "--task",
                task,
                "--role",
                "implementer",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("exit_protocol", data)
        self.assertTrue(data["exit_protocol"]["mandatory"])
        self.assertIn("submit", data["exit_protocol"]["steps"][-1])


if __name__ == "__main__":
    unittest.main()
