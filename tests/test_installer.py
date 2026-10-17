from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("GIT_AUTHOR_NAME", "Context Guardian Tests")
os.environ.setdefault("GIT_AUTHOR_EMAIL", "tests@example.invalid")
os.environ.setdefault("GIT_COMMITTER_NAME", "Context Guardian Tests")
os.environ.setdefault("GIT_COMMITTER_EMAIL", "tests@example.invalid")
os.environ.setdefault("GIT_CONFIG_GLOBAL", os.devnull)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "context-guardian" / "scripts"
ENGINE = SCRIPTS / "context_guard.py"
PACKAGE_ROOT = ROOT / "context-guardian"

sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("installer", SCRIPTS / "installer.py")
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
context_guard = installer.context_guard


def _symlinks_supported() -> bool:
    with tempfile.TemporaryDirectory() as directory:
        try:
            os.symlink(directory, Path(directory) / "link", target_is_directory=True)
        except (OSError, NotImplementedError):
            return False
    return True


SYMLINKS = _symlinks_supported()


def run_cli(home: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    return subprocess.run(
        [sys.executable, str(ENGINE), *args],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, cwd=str(cwd or home),
    )


class InstallTests(unittest.TestCase):
    def _home(self, directory: str, *agents: str) -> Path:
        home = Path(directory) / "home"
        for folder in agents:
            (home / folder).mkdir(parents=True)
        home.mkdir(exist_ok=True)
        return home

    def test_installs_only_for_detected_agents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = self._home(directory, ".claude", ".codex")
            plan = installer.plan_install(home, PACKAGE_ROOT)
            self.assertEqual([s["agent"] for s in plan], ["claude-code", "agents"])
            installer.apply_install(plan, PACKAGE_ROOT)
            for entry in (home / ".claude/skills/context-guardian", home / ".agents/skills/context-guardian"):
                self.assertTrue((entry / "SKILL.md").is_file(), entry)
            self.assertFalse((home / ".cursor").exists())
            again = installer.plan_install(home, PACKAGE_ROOT)
            self.assertEqual({s["action"] for s in again}, {"none"})

    def test_explicit_agent_installs_even_when_not_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = self._home(directory)
            plan = installer.plan_install(home, PACKAGE_ROOT, ["cursor"])
            self.assertEqual([(s["agent"], s["action"]) for s in plan], [("cursor", "create")])

    @unittest.skipUnless(SYMLINKS, "symlinks are not available to this user")
    def test_existing_entries_not_created_by_us_are_never_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = self._home(directory, ".claude", ".gemini")
            own_dir = home / ".claude/skills/context-guardian"
            own_dir.mkdir(parents=True)
            (own_dir / "SKILL.md").write_text("mine", encoding="utf-8")
            elsewhere = Path(directory) / "dev-checkout" / "context-guardian"
            elsewhere.mkdir(parents=True)
            (home / ".gemini/skills").mkdir(parents=True)
            os.symlink(elsewhere, home / ".gemini/skills/context-guardian")

            plan = {s["agent"]: s["action"] for s in installer.plan_install(home, PACKAGE_ROOT)}
            self.assertEqual(plan, {"claude-code": "skip", "gemini-cli": "skip"})
            forced = {s["agent"]: s["action"] for s in installer.plan_install(home, PACKAGE_ROOT, force=True)}
            # --force may swap a link (its target survives) but never a real directory.
            self.assertEqual(forced, {"claude-code": "skip", "gemini-cli": "replace"})
            installer.apply_install(installer.plan_install(home, PACKAGE_ROOT, force=True), PACKAGE_ROOT)
            self.assertEqual((own_dir / "SKILL.md").read_text(encoding="utf-8"), "mine")
            self.assertTrue(elsewhere.is_dir())

            uninstall = {s["agent"]: s["action"] for s in installer.plan_uninstall(home, PACKAGE_ROOT)}
            self.assertEqual(uninstall["claude-code"], "skip")
            self.assertEqual(uninstall["gemini-cli"], "remove")

    @unittest.skipUnless(SYMLINKS, "symlinks are not available to this user")
    def test_older_installed_package_link_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = self._home(directory, ".claude")
            old = Path(directory) / "tools/lib/python3.11/site-packages/context_guardian"
            old.mkdir(parents=True)
            (home / ".claude/skills").mkdir(parents=True)
            os.symlink(old, home / ".claude/skills/context-guardian")
            plan = installer.plan_install(home, PACKAGE_ROOT)
            self.assertEqual(plan[0]["state"], "stale")
            self.assertEqual(plan[0]["action"], "replace")

    def test_copy_fallback_is_marked_and_removable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = self._home(directory, ".claude")
            real_symlink = os.symlink

            def refuse(*args, **kwargs):  # what Windows without Developer Mode does
                raise OSError("symlinks not permitted")

            os.symlink = refuse
            try:
                done = installer.apply_install(installer.plan_install(home, PACKAGE_ROOT), PACKAGE_ROOT)
            finally:
                os.symlink = real_symlink
            entry = home / ".claude/skills/context-guardian"
            self.assertEqual(done[0]["installed_as"], "copy")
            self.assertFalse(entry.is_symlink())
            self.assertTrue((entry / "SKILL.md").is_file())
            self.assertTrue((entry / installer.COPY_MARKER).is_file())
            self.assertFalse(list(entry.rglob("__pycache__")))
            self.assertEqual(installer.inspect_entry(entry, PACKAGE_ROOT)["state"], "current")
            installer.apply_uninstall(installer.plan_uninstall(home, PACKAGE_ROOT))
            self.assertFalse(entry.exists())


class CliTests(unittest.TestCase):
    def test_install_asks_before_changing_anything(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".claude").mkdir()
            entry = home / ".claude/skills/context-guardian"

            dry = run_cli(home, "install", "--dry-run")
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertIn("Claude Code: install", dry.stdout)
            self.assertFalse(entry.exists())

            unconfirmed = run_cli(home, "install")  # no terminal to answer, no --yes
            self.assertEqual(unconfirmed.returncode, 1)
            self.assertIn("--yes", unconfirmed.stdout)
            self.assertFalse(entry.exists())

            done = run_cli(home, "install", "--yes", "--json")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(done.stdout)["status"], "ok")
            self.assertTrue((entry / "SKILL.md").is_file())

            removed = run_cli(home, "uninstall", "--yes")
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertFalse(entry.exists())

    def test_doctor_reports_agents_and_project_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            (home / ".claude").mkdir(parents=True)
            project = Path(directory) / "app"
            (project / "src" / "auth").mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            (project / "src" / "auth" / "login.ts").write_text("export const a = 1;\n", encoding="utf-8")
            context_guard.bootstrap_project(project)

            proc = run_cli(home, "doctor", "--json", cwd=project / "src")
            report = json.loads(proc.stdout)
            checks = {c["check"]: c["status"] for c in report["checks"]}
            self.assertEqual(report["project"], str(project.resolve()))
            self.assertEqual(checks["Claude Code skill"], "warn")  # detected, not installed yet
            self.assertEqual(checks["Profile"], "ok")
            self.assertEqual(checks["Claude Code hooks"], "ok")
            self.assertEqual(checks["AGENTS.md instructions"], "ok")
            self.assertNotIn("Cursor skill", checks)  # not used on this machine

            text = run_cli(home, "doctor", "--project", str(project)).stdout
            self.assertIn("fix: context-guardian install --agent claude-code", text)

    def test_doctor_flags_legacy_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "app"
            (project / ".app-context").mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            profile = {"schema_version": 1, "app_id": "app", "display_name": "App", "commands": {}, "features": []}
            (project / ".app-context" / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
            (project / ".claude").mkdir()
            legacy = "python3 /opt/x/scripts/runtime_connector.py --runtime claude-code --event Stop"
            (project / ".claude" / "settings.json").write_text(
                json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": legacy}]}]}}),
                encoding="utf-8",
            )
            report = installer.run_doctor(Path(directory), PACKAGE_ROOT, project)
            hooks = next(c for c in report["checks"] if c["check"] == "Claude Code hooks")
            self.assertEqual(hooks["status"], "warn")
            self.assertIn("upgrade", hooks["fix"])


class ProjectRemovalTests(unittest.TestCase):
    def test_uninstall_project_removes_only_context_guardian_material(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir()
            project = Path(directory) / "app"
            (project / "src" / "auth").mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            (project / "src" / "auth" / "login.ts").write_text("export const a = 1;\n", encoding="utf-8")
            (project / ".claude").mkdir()
            (project / ".claude" / "settings.json").write_text(json.dumps({
                "permissions": {"allow": ["Read"]},
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]},
            }), encoding="utf-8")
            (project / "AGENTS.md").write_text("# Team rules\n", encoding="utf-8")
            (project / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
            context_guard.bootstrap_project(project)

            preview = run_cli(home, "uninstall", "--project", str(project), "--project-only", "--dry-run")
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertTrue((project / ".gemini" / "settings.json").exists())

            done = run_cli(home, "uninstall", "--project", str(project), "--project-only", "--yes")
            self.assertEqual(done.returncode, 0, done.stderr)
            claude = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(claude, {
                "permissions": {"allow": ["Read"]},
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]},
            })
            # Bootstrap created this file for Context Guardian alone.
            self.assertFalse((project / ".gemini" / "settings.json").exists())
            self.assertEqual((project / "AGENTS.md").read_text(encoding="utf-8"), "# Team rules\n")
            self.assertEqual((project / ".gitignore").read_text(encoding="utf-8"), "node_modules/\n")
            self.assertTrue((project / ".app-context" / "profile.json").is_file())


if __name__ == "__main__":
    unittest.main()
