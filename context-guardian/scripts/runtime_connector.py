#!/usr/bin/env python3
"""Runtime lifecycle connector for Context Guardian.

Translates agent runtime hook events (Claude Code, Gemini CLI, Codex/generic) into
Context Guardian deterministic lifecycle operations (begin, status, finish, abort).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

# Import shared engine
try:
    from . import context_guard
except ImportError:
    import context_guard


def _safe_session_filename(session_id: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", session_id)
    if not cleaned:
        cleaned = "session"
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    return f"session_{cleaned[:80]}_{digest}.json"


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temporary:
            json.dump(value, temporary, ensure_ascii=False, separators=(",", ":"))
            temporary.write("\n")
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def save_session_run_mapping(project: Path, session_id: str, run_id: str) -> None:
    runs_dir = context_guard.project_write_path(project, context_guard.RUNS_RELATIVE_PATH)
    runs_dir.mkdir(parents=True, exist_ok=True)
    mapping_file = context_guard.project_write_path(project, runs_dir / _safe_session_filename(session_id))
    _write_json_atomic(mapping_file, {"session_id": session_id, "run_id": run_id})


def load_session_run_mapping(project: Path, session_id: str) -> str | None:
    mapping_file = project / context_guard.RUNS_RELATIVE_PATH / _safe_session_filename(session_id)
    if not mapping_file.is_file():
        return None
    try:
        data = json.loads(mapping_file.read_text(encoding="utf-8"))
        return data.get("run_id")
    except Exception:
        return None


def clear_session_run_mapping(project: Path, session_id: str) -> None:
    try:
        runs_dir = context_guard.project_write_path(project, context_guard.RUNS_RELATIVE_PATH)
        (runs_dir / _safe_session_filename(session_id)).unlink(missing_ok=True)
    except (OSError, context_guard.MapError):
        pass


def _findings_context(project: Path) -> str | None:
    """The only thing the start hook says on a healthy project: nothing.

    Detection happens in the watcher, outside the conversation, and costs the
    agent nothing. This is the one place findings enter the agent's context, and
    it stays empty until the watcher has actually queued something -- which is
    what keeps a quiet project down to a single file read per turn instead of a
    routing report the agent did not ask for.
    """
    try:
        drained = context_guard.drain_findings(project)
    except Exception:
        return None
    if not drained["count"]:
        return None
    lines = [
        f"Context Guardian found {drained['count']} issue(s) while you were working:"
    ]
    for issue in drained["findings"][:10]:
        paths = ", ".join(issue.get("paths", [])[:5])
        lines.append(
            f"- [{issue.get('severity', 'warning')}] {issue.get('message', '')}"
            + (f" ({paths})" if paths else "")
        )
    lines.append(drained["agent_instruction"])
    return "\n".join(lines)


def _preflight_enabled(project: Path) -> bool:
    """Off unless the project asks for it.

    Routing context on every prompt is the expensive half of the old design: it
    was paid for on turns with nothing to report. A project that wants it says
    so with `"hooks": {"preflight": true}` in its profile.
    """
    try:
        profile = context_guard.load_profile(project)
    except Exception:
        return False
    hooks = profile.get("hooks")
    return bool(isinstance(hooks, dict) and hooks.get("preflight"))


def _preflight_context(project: Path, payload: dict[str, Any]) -> str | None:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    if not _preflight_enabled(project):
        return None
    try:
        profile = context_guard.load_profile(project)
        map_data = None
        map_path = project / context_guard.MAP_RELATIVE_PATH
        if map_path.is_file():
            map_data = context_guard.load_application_map(
                project, context_guard.MAP_RELATIVE_PATH
            )
        report = context_guard.route_task_intent(
            project, profile, prompt, map_data=map_data, validation_level="quick"
        )
    except (context_guard.ProfileError, context_guard.MapError):
        return None

    bounded = {
        "run_guidance": "Read only the selected references before editing; run the listed safe validations before completion.",
        "matched_features": report.get("matched_features", []),
        "candidate_files": report.get("candidate_files", []),
        "read": report.get("read", []),
        "validation_plan": report.get("validation_plan", []),
    }
    return "Context Guardian preflight: " + json.dumps(
        bounded, ensure_ascii=False, separators=(",", ":")
    )


ISSUE_TEXT_LIMIT = 10


def _issues_text(issues: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for issue in issues[:ISSUE_TEXT_LIMIT]:
        paths = ", ".join(issue.get("paths", [])[:5])
        line = f"- [{issue.get('severity', 'warning')}] {issue.get('code', 'issue')}: {issue.get('message', '')}"
        if paths:
            line += f" ({paths})"
        if issue.get("action"):
            line += f" -> {issue['action']}"
        lines.append(line)
    if len(issues) > ISSUE_TEXT_LIMIT:
        lines.append(f"- ... and {len(issues) - ISSUE_TEXT_LIMIT} more")
    return "\n".join(lines)


def _retry_result(run_id: str, finish_res: dict[str, Any], decision: str) -> dict[str, Any]:
    """Return error-level issues to the same agent with enough detail to fix them.

    Claude Code shows the agent only `reason`; a bare summary such as
    "1 error-level issue(s) found" left it nothing to act on.
    """
    pieces = [f"Context Guardian: {finish_res['summary']}", _issues_text(finish_res.get("issues", []))]
    read = finish_res.get("read", [])
    if read:
        pieces.append("Read before fixing: " + ", ".join(read[:10]))
    pieces.append("Fix the error-level issues, then finish your response again.")
    return {
        "decision": decision,
        "reason": "\n".join(piece for piece in pieces if piece),
        "run_id": run_id,
        "run_status": finish_res["run_status"],
        "retry_count": finish_res["retry_count"],
        "issues": finish_res.get("issues", []),
        "read": read,
    }


def _report_result(
    run_id: str, finish_res: dict[str, Any], blocking_decision: str
) -> dict[str, Any]:
    """Hand a finished run's findings to the agent without deciding for the user.

    Context Guardian reports; the person decides. In Claude Code a Stop hook can
    reach the agent only by blocking, and `systemMessage` is shown to the user,
    never to the agent -- so a report that only set `systemMessage` never
    reached the agent that was supposed to explain it. The report therefore
    blocks exactly once with an instruction to *describe and ask*, not to fix.
    The run mapping is cleared before this is returned, so the follow-up Stop
    finds no active run and is allowed: one extra turn at most, never a loop.
    """
    issues = finish_res.get("issues", [])
    instruction = finish_res.get("agent_instruction") or (
        "Describe these findings to the user in plain language and ask whether to fix them. "
        "Do not change anything on your own."
    )
    reason = "\n".join(
        piece
        for piece in (
            f"Context Guardian: {finish_res['summary']}",
            _issues_text(issues),
            instruction + " Then end your turn.",
        )
        if piece
    )
    return {
        "decision": blocking_decision if issues else "allow",
        "reason": reason,
        "run_id": run_id,
        "run_status": finish_res["run_status"],
        "summary": finish_res["summary"],
        "issues": issues,
        "user_action_required": finish_res.get("user_action_required", False),
        "agent_instruction": instruction,
        "systemMessage": f"Context Guardian: {finish_res['summary']}",
    }


def handle_claude_code_event(
    project: Path, event: str, payload: dict[str, Any]
) -> dict[str, Any]:
    session_id = payload.get("session_id") or payload.get("sessionId") or "default"
    turn_id = payload.get("turn_id") or payload.get("turnId")

    if event in ("UserPromptSubmit", "start", "begin"):
        res = context_guard.begin_run(
            project, runtime="claude-code", session_id=session_id, turn_id=turn_id
        )
        save_session_run_mapping(project, session_id, res["run_id"])
        result: dict[str, Any] = {
            "decision": "allow",
            "run_id": res["run_id"],
            "status": "ok",
        }
        pieces = [
            piece
            for piece in (_findings_context(project), _preflight_context(project, payload))
            if piece
        ]
        if pieces:
            result["hookSpecificOutput"] = {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "\n\n".join(pieces),
            }
        return result

    if event in ("Stop", "finish", "stop"):
        run_id = payload.get("run_id") or load_session_run_mapping(project, session_id)
        if not run_id:
            return {"decision": "allow", "warning": "no active run found"}

        max_retries = int(payload.get("max_retries", context_guard.DEFAULT_MAX_RETRIES))
        finish_res = context_guard.finish_run(project, run_id, max_retries=max_retries)

        if finish_res["decision"] == "allow":
            clear_session_run_mapping(project, session_id)
            return {
                "decision": "allow",
                "run_id": run_id,
                "summary": finish_res["summary"],
            }
        elif finish_res["decision"] == "retry":
            return _retry_result(run_id, finish_res, "block")
        else:
            clear_session_run_mapping(project, session_id)
            return _report_result(run_id, finish_res, "block")

    return {"decision": "allow", "warning": f"unhandled event: {event}"}


def handle_gemini_cli_event(
    project: Path, event: str, payload: dict[str, Any]
) -> dict[str, Any]:
    session_id = payload.get("session_id") or payload.get("sessionId") or "default"
    turn_id = payload.get("turn_id") or payload.get("turnId")

    if event in ("BeforeAgent", "start", "begin"):
        res = context_guard.begin_run(
            project, runtime="gemini-cli", session_id=session_id, turn_id=turn_id
        )
        save_session_run_mapping(project, session_id, res["run_id"])
        result: dict[str, Any] = {
            "status": "ok",
            "run_id": res["run_id"],
        }
        pieces = [
            piece
            for piece in (_findings_context(project), _preflight_context(project, payload))
            if piece
        ]
        if pieces:
            result["hookSpecificOutput"] = {
                "hookEventName": "BeforeAgent",
                "additionalContext": "\n\n".join(pieces),
            }
        return result

    if event in ("AfterAgent", "finish", "stop"):
        run_id = payload.get("run_id") or load_session_run_mapping(project, session_id)
        if not run_id:
            return {"decision": "allow", "warning": "no active run found"}

        max_retries = int(payload.get("max_retries", context_guard.DEFAULT_MAX_RETRIES))
        finish_res = context_guard.finish_run(project, run_id, max_retries=max_retries)

        if finish_res["decision"] == "allow":
            clear_session_run_mapping(project, session_id)
            return {
                "decision": "allow",
                "run_id": run_id,
                "summary": finish_res["summary"],
            }
        elif finish_res["decision"] == "retry":
            return _retry_result(run_id, finish_res, "deny")
        else:
            clear_session_run_mapping(project, session_id)
            return _report_result(run_id, finish_res, "deny")

    return {"decision": "allow", "warning": f"unhandled event: {event}"}


def handle_event(
    project: Path, runtime: str, event: str, payload: dict[str, Any]
) -> dict[str, Any]:
    runtime_norm = runtime.lower().strip()
    if "claude" in runtime_norm:
        return handle_claude_code_event(project, event, payload)
    elif "gemini" in runtime_norm:
        return handle_gemini_cli_event(project, event, payload)
    else:
        # Default / generic / codex. An unrecognised event used to fall through
        # to "Stop", so a runtime that emits, say, SessionStart would close a run
        # that had never begun. Unknown events are now inert.
        if event in ("start", "begin", "UserPromptSubmit", "BeforeAgent"):
            return handle_claude_code_event(project, "UserPromptSubmit", payload)
        if event in ("finish", "stop", "Stop", "AfterAgent"):
            return handle_claude_code_event(project, "Stop", payload)
        return {"decision": "allow", "warning": f"unhandled event: {event}"}


COMPLETION_EVENTS = ("Stop", "AfterAgent", "finish", "stop")
CLAUDE_OUTPUT_KEYS = ("continue", "stopReason", "suppressOutput", "systemMessage", "hookSpecificOutput")


def resolve_project(explicit: Path | None, payload: dict[str, Any]) -> Path:
    """Find the onboarded project for a hook invocation.

    Order: explicit `--project`, the runtime's project variables, the payload
    `cwd`, then the process working directory. The nearest ancestor holding
    `.app-context/profile.json` wins, so a session started in a subdirectory
    still reaches its project.
    """
    candidates: list[str] = []
    if explicit is not None and str(explicit).strip():
        candidates.append(str(explicit))
    for variable in ("CLAUDE_PROJECT_DIR", "GEMINI_PROJECT_DIR"):
        if os.environ.get(variable):
            candidates.append(os.environ[variable])
    if isinstance(payload.get("cwd"), str) and payload["cwd"]:
        candidates.append(payload["cwd"])
    candidates.append(os.getcwd())
    start = Path(candidates[0]).expanduser().resolve()
    for directory in (start, *start.parents):
        if (directory / context_guard.PROFILE_RELATIVE_PATH).is_file():
            return directory
    return start


def shape_output(runtime: str, event: str, result: dict[str, Any]) -> dict[str, Any]:
    """Translate an internal result into the runtime's documented hook schema.

    Claude Code accepts `decision: "block"` or no decision at all; "allow" is
    not a valid value and fails output validation. Diagnostic fields such as
    run_id or issues are not part of either schema and are dropped. Set
    CONTEXT_GUARD_HOOK_DEBUG=1 (or use the generic runtime) for the raw result.
    """
    runtime_norm = runtime.lower()
    if os.environ.get("CONTEXT_GUARD_HOOK_DEBUG") == "1" or (
        "claude" not in runtime_norm and "gemini" not in runtime_norm
    ):
        return result
    shaped = {key: result[key] for key in CLAUDE_OUTPUT_KEYS if key in result}
    decision = result.get("decision")
    if decision in ("block", "deny"):
        shaped["decision"] = "block" if "claude" in runtime_norm else "deny"
        shaped["reason"] = result.get("reason") or "Context Guardian requested another pass."
    return shaped


def build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    parser.add_argument("--project", type=Path, help="project root (defaults to the runtime's project directory)")
    parser.add_argument("--runtime", default="generic", help="agent runtime (claude-code, gemini-cli, codex, generic)")
    parser.add_argument("--event", help="lifecycle event name")
    parser.add_argument("--session", dest="session_id", help="runtime session ID")
    parser.add_argument("--run", dest="run_id", help="explicit run ID")
    parser.add_argument("--max-retries", type=int, default=context_guard.DEFAULT_MAX_RETRIES)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for `context-guardian hook ...` (argv given) or this script."""
    parser = build_parser(prog=f"{context_guard.CLI_COMMAND} hook" if argv is not None else None)
    args, unknown = parser.parse_known_args(argv)

    payload: dict[str, Any] = {}
    event = args.event

    # Read JSON from stdin if piped
    if not sys.stdin.isatty():
        try:
            content = sys.stdin.read().strip()
            if content:
                payload = json.loads(content)
                if not event:
                    event = payload.get("event") or payload.get("hook_event_name")
        except Exception:
            pass

    project = resolve_project(args.project, payload)

    # Hooks installed for a user or by a plugin fire in every project, most of
    # which never opted in. Without a profile there is nothing to guard: write
    # nothing (no run records, no session files) and say nothing.
    if not (project / context_guard.PROFILE_RELATIVE_PATH).is_file():
        result = {"status": "not-onboarded", "project": str(project)}
        print(json.dumps(shape_output(args.runtime, event or "", result), ensure_ascii=False, separators=(",", ":")))
        return 0

    if args.session_id:
        payload["session_id"] = args.session_id
    if args.run_id:
        payload["run_id"] = args.run_id
    if args.max_retries is not None:
        payload["max_retries"] = args.max_retries

    if not event:
        event = "start"

    try:
        result = handle_event(project, args.runtime, event, payload)
    except Exception as exc:
        runtime = args.runtime.lower()
        completion_event = event in COMPLETION_EVENTS
        retry_already_active = bool(payload.get("stop_hook_active"))
        if completion_event and retry_already_active:
            # Surface persistent infrastructure failure without creating an
            # unbounded stop-hook loop.
            result = {
                "status": "error",
                "error": str(exc),
                "decision": "allow",
                "run_status": "UNRESOLVED",
                "systemMessage": "Context Guardian could not complete verification after a retry.",
            }
        elif completion_event:
            result = {
                "status": "error",
                "error": str(exc),
                "decision": "deny" if "gemini" in runtime else "block",
                "reason": f"Context Guardian verification failed: {exc}",
            }
        else:
            # A start-of-turn crash used to return `block`, which in Claude Code
            # rejects the user's prompt outright: any bug in Context Guardian
            # would have stopped the person from talking to their agent at all.
            # A broken guardian degrades to no guardian, never to a locked door.
            result = {
                "status": "error",
                "error": str(exc),
                "decision": "allow",
                "systemMessage": (
                    "Context Guardian could not run its preflight check "
                    f"({exc}). Continuing without it."
                ),
            }

    print(json.dumps(shape_output(args.runtime, event, result), ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
