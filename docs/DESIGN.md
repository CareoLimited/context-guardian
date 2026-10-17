# Context Guardian design

This document records what Context Guardian is for, how it is put together, and the
decisions that shape it. Change it in the same commit as any change to shared
architecture, schemas, workflow, or safety rules, and add a dated entry to the decision
log at the end.

## Purpose

Coding agents change code they do not fully understand. They miss a screen that shares a
service, forget the Android twin of an iOS change, or quietly weaken a business rule.
Context Guardian gives an agent, before it edits, only the part of an application that a
task touches, and checks the agent's changes when it finishes.

It is a navigation and review aid. It does not replace tests, type checks, device runs,
or human review, and a clean result is not proof that behaviour is safe.

## Principles

- **Deterministic and local.** The engine is Python standard library only. It runs no
  model, makes no network calls, and costs no model tokens. Only its short reports enter
  an agent's context.
- **Bounded output.** The full map stays on disk; agents get capped query results.
  A healthy, quiet turn adds nothing to the context.
- **Agent-neutral.** One command (`context-guardian`) does the work. Agent integrations
  are thin: hooks where a runtime has them, an `AGENTS.md` block where it does not, and
  plain terminal use with no agent at all.
- **Report, do not repair.** Warnings are described to the person, who decides. Only
  error-level issues ask the agent to try again, and retries are capped.
- **Generated versus curated.** Code-derived facts (the map) are refreshed mechanically.
  Product rules (invariants, feature notes, intentional platform differences) change only
  on purpose.
- **Fail open.** A missing or broken Context Guardian never blocks the person from
  working; it says the check was skipped.
- **The repository is untrusted input.** Profiles, settings, and file layouts in a
  repository can be written by anyone with access to it. They never grant permissions
  and never lead writes outside the project.

## Architecture

```
context-guardian/            the skill directory, shipped inside the package
  SKILL.md                   instructions agents read
  references/                detailed contracts, read on demand
  scripts/
    context_guard.py         engine and CLI (map, routing, lifecycle, safety, bootstrap)
    runtime_connector.py     `context-guardian hook`: runtime events -> lifecycle
    installer.py             `install`, `uninstall`, `doctor`
```

The package `context-guardian-cli` installs the `context-guardian` command; the skill
directory travels inside it, so the installed package is also a complete skill.

Per application, Context Guardian keeps:

```
.app-context/
  profile.json               features, triggers, invariants, validation commands
  invariants/, features/     curated notes the profile points to
  generated/                 the code-derived map
  runs/, findings.json, watcher.json   per-turn state, ignored by git
```

## Lifecycle

1. **Start of a turn** (`begin`, from a hook or by hand): record a baseline of the
   working tree and route the prompt to the features it concerns (`preflight-intent`).
2. **During the turn**: the agent reads only the named references and invariants.
3. **End of a turn** (`finish`): compute what this turn changed, refresh the map, and
   return `allow`, `retry` (an error-level issue, bounded), or `report` (findings for the
   person, with the instruction to ask before changing anything).

Hooks exist for Claude Code (`UserPromptSubmit`, `Stop`) and Gemini CLI (`BeforeAgent`,
`AfterAgent`). Every hook line is the same on every machine:
`context-guardian hook --runtime <runtime> --event <event>`. In a project without a
profile the hook writes nothing and prints nothing.

## Installation model

| Layer | Who needs it | What it does |
|---|---|---|
| Command | Everyone | `uv tool install context-guardian-cli` |
| Skill discovery | Agents that read skills | `context-guardian install` links the skill into each detected agent's folder |
| Project integration | Each application | `context-guardian bootstrap` writes the profile, map, hooks, and `AGENTS.md` block |

`install` asks before changing anything, never replaces an entry it did not create, and
never edits an agent's own settings. `uninstall` removes exactly what was installed.
`doctor` reports what works and what to run for the rest.

## Safety model

**Command tiers.** Every validation command is split into segments the way a POSIX
shell reads it (`;`, `&&`, `||`, `|`, `|&`, `&`, newlines), and the most severe segment
decides:

- `safe`: a recognised read-only check (`pytest`, `jest`, `tsc`, `npm test`, ...);
- `requires-approval`: anything else, including a checker with an option that writes
  files, downloads code, or runs another program, and anything the classifier refuses to
  interpret (substitution, redirection, backslashes, control characters, open quotes);
- `disallowed`: publishing, pushing, destructive deletion, credential overwrites, even
  with options between the tool and the verb (`git -C . push`).

Agents may run only `safe` commands without asking.

**Trust belongs to the person, not the repository.** `commands.trusted` in a profile is a
request. A person approves a command for a project on their own machine with
`context-guardian trust`; approvals live in the user's configuration directory, never in
a repository. Disallowed commands cannot be approved.

**Writes stay inside the project.** Every file Context Guardian writes or deletes is
resolved first; a path that leaves the project through a symlink or `..` is refused.

**Known limits.** File paths and messages from the repository reach the agent's context
in reports, so a hostile repository can still try to steer an agent with text. A
package script's body (`npm test`) is defined by the repository; `safe` means
"conventionally a check", not "proven harmless".

## Not implemented yet

- Compiler- or LSP-backed call graphs beyond the current Python AST and lexical analyzers.
- Framework-aware analyzers (navigation, storage keys, localization, purchases, ads,
  notifications).
- Incremental map refresh (the refresh re-reads the repository).
- Automatic execution of validation commands.
- A native completion hook for Codex (it follows the `AGENTS.md` block instead).
- Schema migration tooling for profiles.
- Published measurements of accuracy and token use on real maintenance tasks.

## Decision log

### 2026-10-18 — First public release preparation

- The engine is distributed as the Python package `context-guardian-cli` and installed
  with `uv tool install`; the command and the skill keep the name `context-guardian`. Minimum Python is 3.11 (3.10 reached end of life on 2026-10-01).
- Hooks call the installed command by name instead of a script by absolute path, so the
  same committed line works on every machine and operating system. `upgrade` replaces
  hooks written in the older absolute-path format.
- A hook in a project without `.app-context/profile.json` does nothing, so user-level and
  plugin hooks are safe in unrelated projects.
- Added `install`, `uninstall`, and `doctor`, covering Claude Code, Gemini CLI, Cursor,
  GitHub Copilot, and the shared Agent Skills folder (Codex and others), plus manual use.
- Closed command-classifier bypasses (`&`, escaped quotes, control characters, writing
  or downloading options, options before `push` or `publish`).
- `commands.trusted` no longer grants trust; approvals moved to the user's machine.
- Writes and deletes are confined to the project; the watcher never signals a PID read
  from the repository on Windows.
- No agent-specific package in the repository: `context-guardian install` makes the skill
  available to every agent, Claude Code included, so a separate Claude Code plugin is not
  shipped. Project hooks come from `bootstrap` and are versioned with each application.
