#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

# The suite creates throwaway git repositories and commits into them. Without an
# identity in the environment, `git commit` exits 128 on any machine that has no
# global user.name -- a fresh laptop, a container, CI -- and seventeen tests fail
# for a reason that has nothing to do with the code under test. The identity is
# pinned here so the suite depends on nothing outside itself.
os.environ.setdefault("GIT_AUTHOR_NAME", "Context Guardian Tests")
os.environ.setdefault("GIT_AUTHOR_EMAIL", "tests@example.invalid")
os.environ.setdefault("GIT_COMMITTER_NAME", "Context Guardian Tests")
os.environ.setdefault("GIT_COMMITTER_EMAIL", "tests@example.invalid")
os.environ.setdefault("GIT_CONFIG_GLOBAL", os.devnull)

# Most tests inspect the connector's internal result (run_id, issues, ...). The
# runtime-facing output is reduced to each runtime's schema; ShapedOutputTests
# below run without this variable and check that shape.
os.environ["CONTEXT_GUARD_HOOK_DEBUG"] = "1"

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "context-guardian" / "scripts" / "runtime_connector.py"


def valid_profile() -> dict:
    return {
        "schema_version": 1,
        "app_id": "test-app",
        "display_name": "Test App",
        "features": [
            {
                "id": "notifications",
                "triggers": ["src/**"],
                "references": [".app-context/features/notifications.md"],
                "invariants": [".app-context/invariants/notifications.md"],
                "validation": ["npm test -- notifications"],
            }
        ],
    }


def _load_engine():
    import importlib.util

    engine_path = MODULE_PATH.parent / "context_guard.py"
    spec = importlib.util.spec_from_file_location("context_guard_engine", engine_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _onboarded_project(directory: str) -> Path:
    project = Path(directory) / "app"
    (project / ".app-context").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / ".app-context" / "profile.json").write_text(json.dumps(valid_profile()), encoding="utf-8")
    return project


class RuntimeConnectorTests(unittest.TestCase):
    def test_connector_failure_is_not_reported_as_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = _onboarded_project(directory)
            # A file where the runs directory belongs makes `begin` fail.
            (project / ".app-context" / "runs").write_text("", encoding="utf-8")
            failed = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "--project",
                    str(project),
                    "--runtime",
                    "claude-code",
                    "--event",
                    "UserPromptSubmit",
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            result = json.loads(failed.stdout)
            # The failure is still surfaced -- never dressed up as a clean run --
            # but it must not reject the person's prompt. A broken guardian
            # degrades to no guardian, not to a locked door.
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["decision"], "allow")
            self.assertIn("Context Guardian", result["systemMessage"])

    def test_completion_failure_still_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = _onboarded_project(directory)
            failed = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "--project",
                    str(project),
                    "--runtime",
                    "claude-code",
                    "--event",
                    "Stop",
                    "--run",
                    "run-that-does-not-exist",
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            result = json.loads(failed.stdout)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["decision"], "block")

    def test_start_hook_injects_bounded_preflight_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            (project / "src").mkdir()
            (project / "src" / "notifications.ts").write_text("export const n = 1;\n")
            profile_dir = project / ".app-context"
            profile_dir.mkdir()
            opted_in = valid_profile()
            opted_in["hooks"] = {"preflight": True}
            (profile_dir / "profile.json").write_text(
                json.dumps(opted_in), encoding="utf-8"
            )
            subprocess.run(["git", "-C", str(project), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(project), "commit", "-q", "-m", "init"], check=True
            )

            started = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "--project",
                    str(project),
                    "--runtime",
                    "gemini-cli",
                    "--event",
                    "BeforeAgent",
                ],
                input=json.dumps(
                    {"session_id": "preflight", "prompt": "Fix notification delivery"}
                ),
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            result = json.loads(started.stdout)
            context = result["hookSpecificOutput"]["additionalContext"]
            self.assertIn('"id":"notifications"', context)
            self.assertNotIn("project_content_sha256", context)

    def _quiet_project(self, project: Path) -> None:
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        (project / "src").mkdir()
        (project / "src" / "notifications.ts").write_text("export const n = 1;\n")
        profile_dir = project / ".app-context"
        profile_dir.mkdir()
        (profile_dir / "profile.json").write_text(
            json.dumps(valid_profile()), encoding="utf-8"
        )
        subprocess.run(["git", "-C", str(project), "add", "."], check=True)
        subprocess.run(["git", "-C", str(project), "commit", "-q", "-m", "init"], check=True)

    def _start_turn(self, project: Path) -> dict:
        started = subprocess.run(
            [
                sys.executable, str(MODULE_PATH), "--project", str(project),
                "--runtime", "claude-code", "--event", "UserPromptSubmit",
            ],
            input=json.dumps({"session_id": "quiet", "prompt": "Fix notification delivery"}),
            check=True, stdout=subprocess.PIPE, text=True,
        )
        return json.loads(started.stdout)

    def test_start_hook_says_nothing_when_there_is_nothing_to_say(self) -> None:
        """The whole point of the watcher split.

        Without an opted-in preflight and without a queued finding, a turn on a
        healthy project must put zero tokens into the agent's context.
        """
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            self._quiet_project(project)
            result = self._start_turn(project)
            self.assertEqual(result["decision"], "allow")
            self.assertNotIn("hookSpecificOutput", result)

    def test_start_hook_delivers_a_queued_finding_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            self._quiet_project(project)
            context_guard = _load_engine()
            context_guard.queue_findings(
                project,
                [
                    {
                        "code": "unsynchronized-platform-twin",
                        "severity": "warning",
                        "message": "iOS twin changed without its Android counterpart",
                        "paths": ["ios_ayri/src/Twin.ts"],
                        "action": "Synchronize the counterpart.",
                    }
                ],
            )

            first = self._start_turn(project)
            context = first["hookSpecificOutput"]["additionalContext"]
            self.assertIn("iOS twin changed", context)
            self.assertIn("ask", context.lower())

            # Delivered once. A finding repeated every turn is noise, and noise
            # is what makes a person stop reading the guardian's output.
            second = self._start_turn(project)
            self.assertNotIn("hookSpecificOutput", second)

    def test_claude_code_lifecycle_clean_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            (project / "src").mkdir()
            (project / "src" / "app.ts").write_text("export const a = 1;\n", encoding="utf-8")
            profile_dir = project / ".app-context"
            profile_dir.mkdir(parents=True)
            (profile_dir / "profile.json").write_text(json.dumps(valid_profile()), encoding="utf-8")

            subprocess.run(["git", "-C", str(project), "add", "."], check=True)
            subprocess.run(["git", "-C", str(project), "commit", "-q", "-m", "init"], check=True)

            # 1. UserPromptSubmit event
            start_proc = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "--project",
                    str(project),
                    "--runtime",
                    "claude-code",
                    "--event",
                    "UserPromptSubmit",
                    "--session",
                    "claude-sess-1",
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            start_res = json.loads(start_proc.stdout)
            self.assertEqual(start_res["decision"], "allow")
            self.assertEqual(start_res["status"], "ok")
            self.assertTrue(start_res["run_id"].startswith("run-"))

            # 2. Modify mapped file
            (project / "src" / "app.ts").write_text("export const a = 2;\n", encoding="utf-8")

            # 3. Stop event
            stop_proc = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "--project",
                    str(project),
                    "--runtime",
                    "claude-code",
                    "--event",
                    "Stop",
                    "--session",
                    "claude-sess-1",
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            stop_res = json.loads(stop_proc.stdout)
            self.assertEqual(stop_res["decision"], "allow")

    def test_claude_code_reports_unmapped_change_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            (project / "src").mkdir()
            profile = valid_profile()
            profile["features"][0]["triggers"] = ["src/services/**"]
            profile_dir = project / ".app-context"
            profile_dir.mkdir(parents=True)
            (profile_dir / "profile.json").write_text(json.dumps(profile), encoding="utf-8")

            subprocess.run(["git", "-C", str(project), "add", "."], check=True)
            subprocess.run(["git", "-C", str(project), "commit", "-q", "-m", "init"], check=True)

            # 1. UserPromptSubmit
            subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "--project",
                    str(project),
                    "--runtime",
                    "claude-code",
                    "--event",
                    "UserPromptSubmit",
                    "--session",
                    "claude-sess-retry",
                ],
                check=True,
                stdout=subprocess.PIPE,
            )

            # 2. Create unmapped file
            (project / "src" / "other.ts").write_text("export const x = 1;\n", encoding="utf-8")

            # 3. Stop -> should block with retry
            stop_proc = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "--project",
                    str(project),
                    "--runtime",
                    "claude-code",
                    "--event",
                    "Stop",
                    "--session",
                    "claude-sess-retry",
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            stop_res = json.loads(stop_proc.stdout)
            # The agent is held for exactly one more pass, told to describe the
            # findings and ask -- never to repair them on its own.
            self.assertEqual(stop_res["decision"], "block")
            self.assertIn("ask", stop_res["reason"].lower())
            self.assertIn("unmapped-change", stop_res["reason"])
            self.assertEqual(stop_res["run_status"], "REPORTED")
            self.assertTrue(stop_res["user_action_required"])
            self.assertIn("ask", stop_res["agent_instruction"].lower())
            self.assertIn("Context Guardian", stop_res["systemMessage"])
            self.assertTrue(len(stop_res["issues"]) > 0)

            # The follow-up Stop has no active run and ends the turn: no loop.
            again = subprocess.run(
                [sys.executable, str(MODULE_PATH), "--project", str(project), "--runtime", "claude-code",
                 "--event", "Stop", "--session", "claude-sess-retry"],
                input=json.dumps({"stop_hook_active": True}),
                check=True, stdout=subprocess.PIPE, text=True,
            )
            self.assertEqual(json.loads(again.stdout)["decision"], "allow")

    def test_gemini_cli_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            (project / "src").mkdir()
            (project / "src" / "app.ts").write_text("export const a = 1;\n", encoding="utf-8")
            profile_dir = project / ".app-context"
            profile_dir.mkdir(parents=True)
            (profile_dir / "profile.json").write_text(json.dumps(valid_profile()), encoding="utf-8")

            subprocess.run(["git", "-C", str(project), "add", "."], check=True)
            subprocess.run(["git", "-C", str(project), "commit", "-q", "-m", "init"], check=True)

            # 1. BeforeAgent
            start_proc = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "--project",
                    str(project),
                    "--runtime",
                    "gemini-cli",
                    "--event",
                    "BeforeAgent",
                    "--session",
                    "gemini-sess-1",
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            start_res = json.loads(start_proc.stdout)
            self.assertEqual(start_res["status"], "ok")

            # 2. AfterAgent clean
            stop_proc = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "--project",
                    str(project),
                    "--runtime",
                    "gemini-cli",
                    "--event",
                    "AfterAgent",
                    "--session",
                    "gemini-sess-1",
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            stop_res = json.loads(stop_proc.stdout)
            self.assertEqual(stop_res["decision"], "allow")


def _shaped_env() -> dict:
    env = dict(os.environ)
    env.pop("CONTEXT_GUARD_HOOK_DEBUG", None)
    return env


class ShapedOutputTests(unittest.TestCase):
    ALLOWED_CLAUDE_KEYS = {"continue", "stopReason", "suppressOutput", "systemMessage",
                           "hookSpecificOutput", "decision", "reason"}

    def _project(self, directory: str, triggers: list[str]) -> Path:
        project = Path(directory)
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        (project / "src").mkdir()
        (project / "src" / "app.ts").write_text("export const a = 1;\n", encoding="utf-8")
        profile = valid_profile()
        profile["features"][0]["triggers"] = triggers
        (project / ".app-context").mkdir()
        (project / ".app-context" / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
        subprocess.run(["git", "-C", str(project), "add", "."], check=True)
        subprocess.run(["git", "-C", str(project), "commit", "-q", "-m", "init"], check=True)
        return project

    def _hook(self, project: Path, runtime: str, event: str, payload: dict, *, cwd: Path | None = None,
              explicit_project: bool = True) -> dict:
        args = [sys.executable, str(MODULE_PATH), "--runtime", runtime, "--event", event]
        if explicit_project:
            args[2:2] = ["--project", str(project)]
        proc = subprocess.run(args, input=json.dumps(payload), check=True, stdout=subprocess.PIPE,
                              text=True, env=_shaped_env(), cwd=str(cwd or project))
        return json.loads(proc.stdout)

    def test_claude_clean_turn_uses_only_schema_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self._project(directory, ["src/**"])
            start = self._hook(project, "claude-code", "UserPromptSubmit", {"session_id": "s"})
            (project / "src" / "app.ts").write_text("export const a = 2;\n", encoding="utf-8")
            stop = self._hook(project, "claude-code", "Stop", {"session_id": "s"})
            for output in (start, stop):
                self.assertLessEqual(set(output), self.ALLOWED_CLAUDE_KEYS)
                self.assertNotIn("decision", output)

    def test_claude_report_reaches_the_agent_through_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self._project(directory, ["src/services/**"])
            self._hook(project, "claude-code", "UserPromptSubmit", {"session_id": "s"})
            (project / "src" / "other.ts").write_text("export const x = 1;\n", encoding="utf-8")
            stop = self._hook(project, "claude-code", "Stop", {"session_id": "s"})
            self.assertLessEqual(set(stop), self.ALLOWED_CLAUDE_KEYS)
            self.assertEqual(stop["decision"], "block")
            self.assertIn("Do not change anything", stop["reason"])

    def test_gemini_report_uses_deny(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self._project(directory, ["src/services/**"])
            self._hook(project, "gemini-cli", "BeforeAgent", {"session_id": "g"})
            (project / "src" / "other.ts").write_text("export const x = 1;\n", encoding="utf-8")
            stop = self._hook(project, "gemini-cli", "AfterAgent", {"session_id": "g"})
            self.assertEqual(stop["decision"], "deny")
            self.assertIn("reason", stop)

    def test_project_is_found_from_environment_and_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self._project(directory, ["src/**"])
            env_project = project / "src"
            proc = subprocess.run(
                [sys.executable, str(MODULE_PATH), "--runtime", "claude-code", "--event", "UserPromptSubmit"],
                input=json.dumps({"session_id": "e"}), check=True, stdout=subprocess.PIPE, text=True,
                env={**_shaped_env(), "CLAUDE_PROJECT_DIR": str(env_project)}, cwd=directory,
            )
            self.assertNotIn("decision", json.loads(proc.stdout))
            self.assertTrue(any((project / ".app-context" / "runs").glob("run-*.json")))

    def test_project_without_profile_is_left_untouched(self) -> None:
        # User-level and plugin hooks fire in every project. One that never opted
        # in must get no files and no output from Context Guardian.
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "unrelated"
            project.mkdir()
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            (project / "README.md").write_text("hello\n", encoding="utf-8")
            for runtime, events in (("claude-code", ("UserPromptSubmit", "Stop")),
                                    ("gemini-cli", ("BeforeAgent", "AfterAgent"))):
                for event in events:
                    proc = subprocess.run(
                        [sys.executable, str(MODULE_PATH), "--runtime", runtime, "--event", event],
                        input=json.dumps({"session_id": "u", "prompt": "fix the login bug"}),
                        check=True, stdout=subprocess.PIPE, text=True,
                        env={**_shaped_env(), "CLAUDE_PROJECT_DIR": str(project), "GEMINI_PROJECT_DIR": str(project)},
                        cwd=str(project),
                    )
                    self.assertEqual(json.loads(proc.stdout), {}, f"{runtime} {event}")
            self.assertFalse((project / ".app-context").exists())
            self.assertEqual(sorted(p.name for p in project.iterdir()), [".git", "README.md"])


if __name__ == "__main__":
    unittest.main()
