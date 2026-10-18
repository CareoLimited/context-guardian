"""Machine-level setup for Context Guardian: `install`, `uninstall`, `doctor`.

`install` makes the skill discoverable by every agent found on this machine by
linking (or, where links are not allowed, copying) this package's skill
directory into each agent's user-level skills folder. It never edits an agent's
own settings files; per-project hooks stay with `bootstrap`.

`uninstall` removes only what `install` created, and with `--project` the
Context Guardian hooks and managed blocks of one application.

`doctor` reports what works and what to run to fix the rest. It also covers
manual use without any agent.

These commands print readable text by default and JSON with `--json`.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any

try:
    from . import context_guard
except ImportError:  # run as a script from a clone, not as the installed package
    import context_guard

SKILL_NAME = "context-guardian"
DIST_NAME = "context-guardian-cli"
COPY_MARKER = ".context-guardian-install.json"

# One row per skills location. Most agents now read the shared Agent Skills
# folder (`~/.agents/skills`); the others keep their own. `detect` lists the
# folders whose presence means the agent is used on this machine.
AGENTS: tuple[dict[str, Any], ...] = (
    {"id": "claude-code", "name": "Claude Code", "detect": (".claude",), "skills": ".claude/skills"},
    {"id": "gemini-cli", "name": "Gemini CLI", "detect": (".gemini",), "skills": ".gemini/skills"},
    {"id": "cursor", "name": "Cursor", "detect": (".cursor",), "skills": ".cursor/skills"},
    {"id": "github-copilot", "name": "GitHub Copilot", "detect": (".copilot",), "skills": ".copilot/skills"},
    {
        "id": "agents",
        "name": "Shared Agent Skills folder (Codex, Cline, OpenCode, Zed, Warp, Amp, ...)",
        "detect": (".agents", ".codex", ".config/opencode"),
        "skills": ".agents/skills",
    },
)
AGENT_IDS = tuple(agent["id"] for agent in AGENTS)


class InstallError(RuntimeError):
    pass


def package_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - Python < 3.8
        return "source"
    try:
        return version(DIST_NAME)
    except PackageNotFoundError:
        return "source"


# ---------------------------------------------------------------------------
# Skill entries in agent folders


def _read_marker(directory: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((directory / COPY_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("managed_by") == SKILL_NAME else None


def _is_installed_package_dir(path: Path) -> bool:
    """A `context_guardian` directory that an installed `context-guardian-cli` owns."""
    parts = path.parts
    return (
        bool(parts)
        and parts[-1] == "context_guardian"
        and any(part in ("site-packages", "dist-packages") for part in parts)
    )


def _same_directory(a: Path, b: Path) -> bool:
    try:
        return a == b.resolve() or os.path.samefile(a, b)
    except OSError:
        return False


def inspect_entry(entry: Path, package_root: Path) -> dict[str, Any]:
    """Classify `<agent skills>/context-guardian`.

    states: missing, current (points at this package), stale (an older
    Context Guardian install that can be replaced safely), conflict (something
    this tool did not create; never touched).
    """
    if entry.is_symlink():
        # Resolve the entry itself: on Windows os.readlink returns a `\\?\`
        # path that never compares equal to an ordinary resolved path.
        try:
            resolved = entry.resolve()
        except OSError:
            raw = Path(os.readlink(entry))
            resolved = raw if raw.is_absolute() else (entry.parent / raw)
        if _same_directory(resolved, package_root):
            return {"state": "current", "kind": "link", "target": str(resolved)}
        if _is_installed_package_dir(resolved):
            return {"state": "stale", "kind": "link", "target": str(resolved)}
        return {"state": "conflict", "kind": "link", "target": str(resolved)}
    if entry.is_dir():
        marker = _read_marker(entry)
        if marker is None:
            return {"state": "conflict", "kind": "directory"}
        if marker.get("version") == package_version() and marker.get("source") == str(package_root.resolve()):
            return {"state": "current", "kind": "copy"}
        return {"state": "stale", "kind": "copy"}
    if entry.exists():
        return {"state": "conflict", "kind": "file"}
    return {"state": "missing"}


def _remove_entry(entry: Path, kind: str) -> None:
    if kind == "link":
        entry.unlink()
    elif kind == "copy":
        shutil.rmtree(entry)
    else:  # pragma: no cover - guarded by callers
        raise InstallError(f"refusing to remove unmanaged {entry}")


def _create_entry(entry: Path, package_root: Path) -> str:
    entry.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(package_root.resolve(), entry, target_is_directory=True)
        return "link"
    except (OSError, NotImplementedError):
        # Windows without Developer Mode cannot create symlinks.
        shutil.copytree(
            package_root, entry, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", COPY_MARKER)
        )
        marker = {"managed_by": SKILL_NAME, "version": package_version(), "source": str(package_root.resolve())}
        (entry / COPY_MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        return "copy"


def agent_status(home: Path, package_root: Path) -> list[dict[str, Any]]:
    rows = []
    for agent in AGENTS:
        entry = home / agent["skills"] / SKILL_NAME
        rows.append({
            "agent": agent["id"],
            "name": agent["name"],
            "detected": any((home / folder).is_dir() for folder in agent["detect"]),
            "entry": str(entry),
            **inspect_entry(entry, package_root),
        })
    return rows


def _select(rows: list[dict[str, Any]], agents: list[str] | None) -> list[dict[str, Any]]:
    if agents:
        unknown = sorted(set(agents) - set(AGENT_IDS))
        if unknown:
            raise InstallError(f"unknown agent(s): {', '.join(unknown)}; choose from {', '.join(AGENT_IDS)}")
        return [row for row in rows if row["agent"] in agents]
    return [row for row in rows if row["detected"]]


def plan_install(home: Path, package_root: Path, agents: list[str] | None = None, force: bool = False) -> list[dict[str, Any]]:
    plan = []
    for row in _select(agent_status(home, package_root), agents):
        state = row["state"]
        if state == "missing":
            action = "create"
        elif state == "current":
            action = "none"
        elif state == "stale":
            action = "replace"
        elif force and row.get("kind") == "link":
            action = "replace"  # an unrelated link: removing it never deletes its target
        else:
            action = "skip"
        plan.append({**row, "action": action})
    return plan


def apply_install(plan: list[dict[str, Any]], package_root: Path) -> list[dict[str, Any]]:
    done = []
    for step in plan:
        entry = Path(step["entry"])
        result = dict(step)
        if step["action"] == "replace":
            _remove_entry(entry, step["kind"])
        if step["action"] in ("create", "replace"):
            result["installed_as"] = _create_entry(entry, package_root)
        done.append(result)
    return done


def plan_uninstall(home: Path, package_root: Path, agents: list[str] | None = None) -> list[dict[str, Any]]:
    rows = agent_status(home, package_root)
    if agents:
        rows = _select(rows, agents)
    plan = []
    for row in rows:
        if row["state"] in ("current", "stale"):
            plan.append({**row, "action": "remove"})
        elif row["state"] == "conflict":
            plan.append({**row, "action": "skip"})
    return plan


def apply_uninstall(plan: list[dict[str, Any]]) -> None:
    for step in plan:
        if step["action"] == "remove":
            _remove_entry(Path(step["entry"]), step["kind"])


# ---------------------------------------------------------------------------
# Project integration removal


def _strip_block(text: str, start: str, end: str) -> str | None:
    if start not in text or end not in text:
        return None
    prefix, remainder = text.split(start, 1)
    _, suffix = remainder.split(end, 1)
    joined = prefix.rstrip("\n")
    rest = suffix.lstrip("\n")
    if joined and rest:
        return joined + "\n\n" + rest
    return (joined + "\n" if joined else "") + rest


def plan_project_removal(project: Path) -> list[dict[str, Any]]:
    """What `uninstall --project` would change. `.app-context/` itself is kept."""
    plan = []
    for relative in (Path(".claude/settings.json"), Path(".gemini/settings.json")):
        path = project / relative
        if not path.is_file():
            continue
        settings = context_guard._load_settings_object(path)
        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            continue
        new_hooks: dict[str, Any] = {}
        removed = 0
        for event, groups in hooks.items():
            if not isinstance(groups, list):
                new_hooks[event] = groups
                continue
            kept_groups = []
            for group in groups:
                if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                    kept = [h for h in group["hooks"] if not context_guard._is_managed_handler(h)]
                    removed += len(group["hooks"]) - len(kept)
                    if kept:
                        kept_groups.append({**group, "hooks": kept})
                else:
                    kept_groups.append(group)
            if kept_groups:
                new_hooks[event] = kept_groups
        if not removed:
            continue
        new_settings = {k: v for k, v in settings.items() if k != "hooks"}
        if new_hooks:
            new_settings["hooks"] = new_hooks
        plan.append({
            "target": str(relative),
            "action": "delete-file" if not new_settings else "update",
            "removed_handlers": removed,
            "_content": new_settings,
        })
    for relative, start, end in (
        (Path("AGENTS.md"), context_guard.AGENTS_START, context_guard.AGENTS_END),
        (Path(".gitignore"), context_guard.GITIGNORE_START, context_guard.GITIGNORE_END),
    ):
        path = project / relative
        if not path.is_file():
            continue
        stripped = _strip_block(path.read_text(encoding="utf-8"), start, end)
        if stripped is None:
            continue
        plan.append({
            "target": str(relative),
            "action": "delete-file" if not stripped.strip() else "update",
            "_content": stripped,
        })
    return plan


def apply_project_removal(project: Path, plan: list[dict[str, Any]]) -> None:
    for step in plan:
        path = context_guard.project_write_path(project, Path(step["target"]))
        if step["action"] == "delete-file":
            path.unlink()
        elif isinstance(step["_content"], dict):
            context_guard._write_json_atomic(path, step["_content"])
        else:
            path.write_text(step["_content"], encoding="utf-8")


# ---------------------------------------------------------------------------
# Doctor


def _check(status: str, label: str, detail: str = "", fix: str = "") -> dict[str, str]:
    return {"status": status, "check": label, "detail": detail, "fix": fix}


def _find_project(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        if (directory / context_guard.PROFILE_RELATIVE_PATH).is_file():
            return directory
    return None


def _project_checks(project: Path) -> list[dict[str, str]]:
    checks = []
    try:
        context_guard.load_profile(project)
        checks.append(_check("ok", "Profile", str(project / context_guard.PROFILE_RELATIVE_PATH)))
    except context_guard.ProfileError as exc:
        checks.append(_check("fail", "Profile", str(exc), "Fix .app-context/profile.json"))

    for label, relative in (("Claude Code hooks", ".claude/settings.json"), ("Gemini CLI hooks", ".gemini/settings.json")):
        path = project / relative
        try:
            settings = context_guard._load_settings_object(path) if path.is_file() else {}
        except context_guard.MapError as exc:
            checks.append(_check("fail", label, str(exc)))
            continue
        commands = [
            h.get("command", "")
            for groups in (settings.get("hooks") or {}).values() if isinstance(groups, list)
            for g in groups if isinstance(g, dict) and isinstance(g.get("hooks"), list)
            for h in g["hooks"] if context_guard._is_managed_handler(h)
        ]
        if not commands:
            checks.append(_check("info", label, "not installed in this project",
                                 f"{context_guard.CLI_COMMAND} bootstrap --project . (adds them)"))
        elif any(context_guard.LEGACY_CONNECTOR_SCRIPT_NAME in c for c in commands):
            checks.append(_check("warn", label, "older format that runs a script by absolute path",
                                 f"{context_guard.CLI_COMMAND} upgrade --project ."))
        else:
            checks.append(_check("ok", label, "installed"))

    agents_md = project / "AGENTS.md"
    text = agents_md.read_text(encoding="utf-8") if agents_md.is_file() else ""
    if context_guard.AGENTS_START not in text:
        checks.append(_check("info", "AGENTS.md instructions", "not installed in this project"))
    elif "python3 " in text.split(context_guard.AGENTS_START, 1)[1].split(context_guard.AGENTS_END, 1)[0]:
        checks.append(_check("warn", "AGENTS.md instructions", "older format with an absolute path",
                             f"{context_guard.CLI_COMMAND} upgrade --project ."))
    else:
        checks.append(_check("ok", "AGENTS.md instructions", "installed"))

    map_file = project / context_guard.MAP_RELATIVE_PATH
    if not map_file.is_file():
        checks.append(_check("warn", "Application map", "missing", f"{context_guard.CLI_COMMAND} build-map --project ."))
    else:
        try:
            freshness = context_guard.check_map_freshness(
                project, context_guard.load_application_map(project, context_guard.MAP_RELATIVE_PATH)
            )
            checks.append(_check("ok", "Application map",
                                 "up to date" if freshness.get("is_fresh") else "out of date; refreshed at the next finish"))
        except context_guard.MapError as exc:
            checks.append(_check("warn", "Application map", str(exc), f"{context_guard.CLI_COMMAND} build-map --project ."))
    return checks


def run_doctor(home: Path, package_root: Path, project: Path | None) -> dict[str, Any]:
    checks = []
    version = sys.version_info
    if version >= (3, 11):
        checks.append(_check("ok", "Python", f"{version.major}.{version.minor}.{version.micro}"))
    else:
        checks.append(_check("warn", "Python", f"{version.major}.{version.minor}: supported versions are 3.11+",
                             "uv tool install --reinstall context-guardian-cli"))

    found = shutil.which(context_guard.CLI_COMMAND)
    if found:
        checks.append(_check("ok", "Command on PATH", found))
    else:
        checks.append(_check("fail", "Command on PATH",
                             "agents and hooks cannot find `context-guardian`; their checks are skipped",
                             "uv tool install context-guardian-cli, then uv tool update-shell and open a new terminal"))

    detected_any = False
    for row in agent_status(home, package_root):
        if not row["detected"] and row["state"] == "missing":
            continue
        detected_any = True
        label = f"{row['name']} skill"
        state = row["state"]
        if state == "current":
            checks.append(_check("ok", label, row["entry"]))
        elif state == "missing":
            checks.append(_check("warn", label, "not installed",
                                 f"{context_guard.CLI_COMMAND} install --agent {row['agent']}"))
        elif state == "stale":
            checks.append(_check("warn", label, "points at an older installation",
                                 f"{context_guard.CLI_COMMAND} install --agent {row['agent']}"))
        else:
            target = row.get("target", row["entry"])
            checks.append(_check("warn", label, f"{row['entry']} exists but was not created by this tool ({target})",
                                 "Remove it yourself, or for a link run install with --force"))
    if not detected_any:
        checks.append(_check("info", "Agents", "none detected; manual use needs only the command"))

    if project is not None:
        checks.extend(_project_checks(project))

    worst = "fail" if any(c["status"] == "fail" for c in checks) else (
        "warn" if any(c["status"] == "warn" for c in checks) else "ok")
    return {"status": worst, "version": package_version(), "package": str(package_root),
            "project": str(project) if project else None, "checks": checks}


# ---------------------------------------------------------------------------
# CLI


def _public(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in step.items() if not k.startswith("_")} for step in steps]


def _confirm(args: argparse.Namespace, out: list[str]) -> bool:
    if args.yes:
        return True
    # Both ends must be a terminal: Windows reports the NUL device as a tty.
    if args.json or not (sys.stdin.isatty() and sys.stdout.isatty()):
        out.append("Nothing changed. Re-run with --yes to apply, or --dry-run to only preview.")
        return False
    print("\n".join(out))
    out.clear()
    try:
        answer = input("Proceed? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        out.append("Cancelled. Nothing changed.")
        return False
    return True


INSTALL_WORDS = {
    "create": "install",
    "replace": "replace older installation",
    "none": "already installed",
    "skip": "skip: existing entry was not created by this tool",
}


def _cmd_install(args: argparse.Namespace, home: Path, package_root: Path) -> tuple[int, dict[str, Any], list[str]]:
    plan = plan_install(home, package_root, args.agent, args.force)
    out = ["Context Guardian install", f"  package: {package_root}", ""]
    if not plan:
        out.append("No agents detected. Use --agent to choose one: " + ", ".join(AGENT_IDS))
        out.append("Manual use needs nothing more than the `context-guardian` command.")
        return 0, {"status": "nothing-to-do", "plan": []}, out
    for step in plan:
        out.append(f"  {step['name']}: {INSTALL_WORDS[step['action']]}")
        out.append(f"      {step['entry']}")
    changes = [s for s in plan if s["action"] in ("create", "replace")]
    if not changes:
        out.append("\nNothing to change.")
        return 0, {"status": "ok", "plan": plan}, out
    if args.dry_run:
        out.append("\nDry run: nothing changed.")
        return 0, {"status": "dry-run", "plan": plan}, out
    if not _confirm(args, out):
        return 1, {"status": "not-confirmed", "plan": plan}, out
    done = apply_install(plan, package_root)
    out.append("\nDone. Start a new agent session so it picks up the skill.")
    out.append("Next, in an application: context-guardian bootstrap --project .")
    return 0, {"status": "ok", "result": done}, out


def _cmd_uninstall(args: argparse.Namespace, home: Path, package_root: Path) -> tuple[int, dict[str, Any], list[str]]:
    out = ["Context Guardian uninstall", ""]
    agent_plan = [] if args.project_only else plan_uninstall(home, package_root, args.agent)
    project_plan: list[dict[str, Any]] = []
    project = args.project.expanduser().resolve() if args.project else None
    if project is not None:
        project_plan = plan_project_removal(project)
    for step in agent_plan:
        word = "remove" if step["action"] == "remove" else "skip: not created by this tool"
        out.append(f"  {step['name']}: {word}")
        out.append(f"      {step['entry']}")
    for step in project_plan:
        word = "delete (only Context Guardian content)" if step["action"] == "delete-file" else "remove Context Guardian entries"
        out.append(f"  {project / step['target']}: {word}")
    if project is not None:
        out.append(f"  {project / '.app-context'}: kept (your profile, map and notes; delete it yourself if unwanted)")
    changes = [s for s in agent_plan if s["action"] == "remove"] + project_plan
    payload = {"agents": agent_plan, "project": _public(project_plan)}
    if not changes:
        out.append("\nNothing to remove.")
        return 0, {"status": "nothing-to-do", **payload}, out
    if args.dry_run:
        out.append("\nDry run: nothing changed.")
        return 0, {"status": "dry-run", **payload}, out
    if not _confirm(args, out):
        return 1, {"status": "not-confirmed", **payload}, out
    apply_uninstall(agent_plan)
    if project is not None:
        apply_project_removal(project, project_plan)
    out.append("\nDone. The command itself stays installed: uv tool uninstall context-guardian-cli")
    return 0, {"status": "ok", **payload}, out


def _cmd_doctor(args: argparse.Namespace, home: Path, package_root: Path) -> tuple[int, dict[str, Any], list[str]]:
    project = args.project.expanduser().resolve() if args.project else _find_project(Path.cwd().resolve())
    report = run_doctor(home, package_root, project)
    out = [f"Context Guardian doctor (version {report['version']})", ""]
    for check in report["checks"]:
        line = f"  [{check['status']}] {check['check']}"
        if check["detail"]:
            line += f": {check['detail']}"
        out.append(line)
        if check["fix"] and check["status"] != "ok":
            out.append(f"         fix: {check['fix']}")
    if project is None:
        out.append("\n  (No onboarded project here. Pass --project to check one.)")
    return (1 if report["status"] == "fail" else 0), report, out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=context_guard.CLI_COMMAND, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="make the skill available to the agents on this machine")
    install.add_argument("--agent", action="append", choices=AGENT_IDS,
                         help="install for this agent only (repeatable); default: every detected agent")
    install.add_argument("--force", action="store_true", help="replace an unrelated link (never a real directory)")

    uninstall = sub.add_parser("uninstall", help="remove what install created; --project also cleans an application")
    uninstall.add_argument("--agent", action="append", choices=AGENT_IDS, help="only this agent (repeatable)")
    uninstall.add_argument("--project", type=Path, help="also remove hooks and managed blocks from this application")
    uninstall.add_argument("--project-only", action="store_true", help="with --project: leave agent skills alone")

    doctor = sub.add_parser("doctor", help="check the installation and the current application")
    doctor.add_argument("--project", type=Path, help="application to check (default: the one containing this directory)")

    for command in (install, uninstall):
        command.add_argument("--yes", "-y", action="store_true", help="apply without asking")
        command.add_argument("--dry-run", action="store_true", help="show the plan only")
    for command in (install, uninstall, doctor):
        command.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    home = Path.home()
    package_root = context_guard.PACKAGE_ROOT
    handlers = {"install": _cmd_install, "uninstall": _cmd_uninstall, "doctor": _cmd_doctor}
    if args.command == "uninstall" and args.project_only and not args.project:
        print("--project-only needs --project", file=sys.stderr)
        return 2
    try:
        code, payload, out = handlers[args.command](args, home, package_root)
    except (InstallError, context_guard.MapError, OSError) as exc:
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    elif out:
        print("\n".join(out))
    return code
