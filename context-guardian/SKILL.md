---
name: context-guardian
description: Maintain mapped applications using repository-specific architecture, feature-impact, invariant, platform-parity, and validation context. Use when diagnosing, changing, reviewing, or validating an application that contains `.app-context/profile.json`, or when asked to onboard, set up, or upgrade Context Guardian for a repository.
---

# Context Guardian

Context Guardian keeps a per-application map of features, invariants, and validation
commands under `.app-context/`, gives the agent only the slice relevant to the current
task, and checks the task's changes when the agent finishes. Scripts are deterministic
Python (standard library only) and cost no model tokens.

Every command below is the `context-guardian` command. If it is not installed, ask the
user before installing it with `uv tool install context-guardian-cli`. Hooks installed
into applications call it by name, so it must be installed on every machine that works
on an onboarded application. (From a source checkout without the command,
`python3 scripts/context_guard.py <command>` next to this `SKILL.md` is equivalent.)

`context-guardian doctor` shows what is installed and what to run to fix the rest.
`context-guardian install` makes this skill available to the other agents on the
machine; it asks first, so run it only when the user wants that, with `--yes` once
they have agreed (see `references/agent-portability.md`).

## 1. One-time setup per application

Not onboarded yet (no `.app-context/profile.json`):

```bash
context-guardian bootstrap --project <repository-root>
```

This detects stacks and feature directories, writes the profile and skeleton docs,
adds Claude Code and Gemini CLI hooks plus a Codex `AGENTS.md` block, adds a
`.gitignore` block for run state, and builds the map. It only proposes validation
commands that exist (for example package.json scripts); review the `notes` it returns.

Onboarded with an older Context Guardian version:

```bash
context-guardian upgrade --project <repository-root> --dry-run
context-guardian upgrade --project <repository-root>
```

`upgrade` changes only managed material (hooks, the `AGENTS.md` block, the `.gitignore`
block, the generated map) and only for integrations that were already installed. Hooks
from older versions that ran a script by absolute path are replaced with
`context-guardian hook`. It never edits the profile, invariants, or feature docs;
problems there are listed under `findings` for the user to decide on.

## 2. Every task

With hooks installed (Claude Code, Gemini CLI) steps 1 and 4 run automatically.

1. `context-guardian begin --project <path>` — keep the `run_id`.
2. Before editing: `context-guardian preflight-intent --project <path> --intent "<task>"`,
   then read only the references and invariants it names.
3. While editing: identify the affected feature, shared contracts, platform twins,
   and migrations. Preserve curated invariants; if the request conflicts with one,
   say so instead of weakening it. Source files and test results are ground truth;
   the map is a navigation aid.
4. Before the final response: `context-guardian finish --project <path> --run <run_id>`.
   It refreshes the generated map itself.
   - `allow`: done.
   - `retry`: an error-level issue. Fix it and run `finish` again.
   - `report` with `user_action_required`: describe the findings to the user in plain
     language and ask before changing anything. Never repair a reported warning unasked,
     and never present an unresolved result as clean.
5. Run the profile's quick or full validation commands in proportion to the change risk.
   Only `safe`-tier commands may run without asking (see `inspect-safety`). Never run
   `context-guardian trust` yourself: approving a command is the user's decision.

Keep reports short. Do not load unrelated feature maps, generated inventories, or
historical notes.

## 3. Optional tools

- `inspect --project <path>`: context for an existing diff.
- `query-map` / `impact-map --depth N`: bounded map neighbourhoods.
- `check-twins --project <path>`: cross-platform twin synchronization.
- `inspect-safety --command "<cmd>"` or `--project <path>`: command risk tier.
- `watch --project <path>`: optional background watcher (a long-running process the
  user starts in their own terminal). Findings are delivered once, at the start of
  the next turn. `scan`, `watch-status`, and `findings --peek` inspect it.
- `uninstall [--project <path>]`: removes what `install` created and, with `--project`,
  the hooks and managed blocks of one application (its `.app-context/` is kept). Only
  when the user asks for it.

## 4. Changing Context Guardian itself

- Read the repository's `docs/DESIGN.md` (in the source repository, one level above
  this skill folder) before changing shared architecture, schemas, workflow, or safety
  rules, and add a dated decision-log entry there in the same change.
- Run the test suite in the repository's `tests/` directory after changing scripts or
  the profile contract.

References, read only when needed:

- [references/profile-schema.md](references/profile-schema.md): creating or changing `.app-context/profile.json`.
- [references/application-map.md](references/application-map.md): generating, refreshing, or querying the map.
- [references/lifecycle-integration.md](references/lifecycle-integration.md): per-task hooks and their output.
- [references/bootstrap-installation.md](references/bootstrap-installation.md): bootstrap and upgrade behaviour.
- [references/agent-portability.md](references/agent-portability.md): installing for another agent runtime.
