# Automatic Task Lifecycle Integration

Read this reference when implementing, installing, testing, or changing automatic
Context Guardian execution for an agent runtime. It defines the runtime-neutral
contract. Application mapping rules remain in the application repository.

## Goal

For every ordinary task in an onboarded application:

1. capture repository state before the agent acts;
2. monitor locally without consuming model tokens;
3. analyze only the task-created delta when the agent tries to finish;
4. allow a clean completion with no meaningful context injection;
5. return a concise actionable report to the same agent when correction is needed.

`SKILL.md` alone cannot guarantee this lifecycle. Automatic execution requires a
hook-capable runtime or an external wrapper that owns the complete agent turn.

## Architecture Boundary

The design has two layers:

- **Shared engine:** portable Python standard-library code that owns baselines, run
  state, changed-file selection, checks, reports, retry counters, and decisions.
- **Runtime connector:** the smallest possible adapter that translates one runtime's
  hook JSON and exit/decision semantics into the shared engine protocol.

Connectors must not contain application names, feature rules, validation selection,
or duplicated policy. A connector may locate the project, pass event metadata, and
translate the shared result into the runtime's supported response shape.

## Lifecycle Commands

The lifecycle engine CLI surface (implemented in `scripts/context_guard.py`):

```text
context-guardian begin  --project <path> [--runtime <id>] [--session <id>] [--turn <id>]
context-guardian status --project <path> --run <id>
context-guardian finish --project <path> --run <id> [--max-retries <int>] [--map <path>]
context-guardian abort  --project <path> --run <id> [--reason <reason>]
```

`begin` returns a unique run ID and persists a baseline. `finish` calculates the task
delta and returns a single bounded JSON decision envelope (`allow`, `retry`, `report`,
`error`). `abort` transitions the run to `ABORTED`. Run records are stored in
`.app-context/runs/<run_id>.json`.

## Runtime Lifecycle Connector

`context-guardian hook` (implemented in `scripts/runtime_connector.py`) adapts runtime hook
events directly to the lifecycle engine:

```bash
# Invocation via CLI or piped hook payload
context-guardian hook [--project <path>] --runtime claude-code|gemini-cli|generic --event <event-name>
```

- **Claude Code**: `UserPromptSubmit` -> `begin`, `Stop` -> `finish`. Output uses only the
  documented keys (`decision`, `reason`, `systemMessage`, `hookSpecificOutput`, ...).
  `decision` is either `"block"` or absent; `"allow"` is not a valid Claude Code value.
  The agent sees only `reason`, so retries and reports carry the issue list there;
  `systemMessage` is shown to the user, not the agent.
- **Gemini CLI**: `BeforeAgent` -> `begin`, `AfterAgent` -> `finish`; `decision: "deny"`
  with `reason` requests another pass.
- **Outcomes at completion**: `allow` ends the turn silently. `retry` (error-level issue)
  blocks with the issues and the references to read. `report` (warnings, or errors after
  the retry cap) blocks exactly once with the instruction to describe the findings to the
  user and ask; the run mapping is cleared first, so the follow-up completion event finds
  no active run and ends the turn.
- **Diagnostics**: `--runtime generic` or `CONTEXT_GUARD_HOOK_DEBUG=1` prints the full
  internal result (run ID, issues, run status) instead of the runtime-shaped output.
- **Project resolution**: `--project` is optional. The connector uses `--project`, then
  `CLAUDE_PROJECT_DIR` / `GEMINI_PROJECT_DIR`, then the payload `cwd`, then the working
  directory, and walks up to the nearest `.app-context/profile.json`.
- **Automatic preflight**: Start events route the supplied prompt and inject only the
  bounded feature, file, reference, and validation selection into that turn.
- **Session state**: Persists collision-resistant session-to-run mappings atomically
  in `.app-context/runs/session_<safe-id>_<digest>.json`.
- **Failure policy**: Connector failures block the first attempt. A persistent failure
  during a runtime-marked retry is surfaced as unresolved and allowed to terminate so
  infrastructure errors cannot create an infinite agent loop.

Project bootstrap merges command hooks into `.claude/settings.json` and
`.gemini/settings.json`, preserving unrelated settings and handlers. It does not use
standalone `.claude/hooks.json` or `.gemini/hooks/hooks.json` files because those are
not the runtime settings schemas.

Installed hook commands are portable and fail open. Each one is a single line such as
`context-guardian hook --runtime claude-code --event Stop`:

- it names the installed CLI only: no absolute path, no interpreter, no shell syntax, so
  the committed line works on every machine and in sh, bash, or PowerShell;
- the project comes from `$CLAUDE_PROJECT_DIR` / `$GEMINI_PROJECT_DIR` (or the working
  directory), never a path baked in at bootstrap time;
- on a machine without the CLI the shell exits 127. Claude Code and Gemini CLI both treat
  exit codes other than 0 and 2 as non-blocking: they show a warning and the agent
  continues without the check. Exit code 2 would block every prompt, which is why the
  hook never uses it;
- timeouts are 60 seconds in each runtime's unit (Claude Code: seconds, Gemini CLI:
  milliseconds);
- managed handlers are recognised by name, by `context-guardian hook` in the command, or by
  `runtime_connector.py` (handlers from versions that ran the script by absolute path), so
  re-running bootstrap or `upgrade` replaces older handlers instead of duplicating them.

## Run State Machine

```text
NEW -> ACTIVE -> CHECKING -> CLEAN
                        `-> ISSUES -> ACTIVE (bounded correction retry)
                                   `-> UNRESOLVED
ACTIVE -> ABORTED
ACTIVE -> STALE
```

Only one completion check may mutate a run record at a time. Repeated hook delivery
must be idempotent. Nested agents or parallel tasks require distinct run IDs even
when they share a working tree. The engine must detect overlapping writes and lower
confidence rather than assigning ownership without evidence.

## Baseline Requirements

Before the agent edits, record enough information to distinguish task changes from
pre-existing work:

- canonical repository path and worktree identity;
- current commit when one exists;
- tracked staged and unstaged paths;
- untracked paths;
- content identity for dirty paths needed to detect subsequent modification;
- profile identity and engine/schema version;
- runtime, session, turn, and parent-run identifiers when supplied;
- timestamp and retry count.

Do not store the complete user prompt by default. A non-reversible prompt digest or
runtime-provided turn identifier is sufficient for correlation unless a later
documented privacy decision authorizes more.

## Background Monitoring

Background monitoring is optional optimization, not the source of truth. A
task-scoped process may watch filesystem or Git state, debounce changes, and prepare
compact selection caches. `finish` must always re-read authoritative repository
state so missed filesystem events cannot produce a clean result.

The monitor:

- uses no LLM or remote service;
- never edits application source;
- never runs deployment or externally mutating commands;
- writes state below an operating-system cache location;
- has an inactivity timeout and exits on finish, abort, or stale-run cleanup;
- logs to bounded files and never writes ordinary logs to hook JSON stdout.

A permanent daemon is not required for the first implementation. Prefer one small
task-scoped process because ownership, cleanup, upgrades, and fault isolation are
clearer.

## Completion Decision Envelope

The shared engine should return a bounded object equivalent to:

```json
{
  "schema_version": 1,
  "run_id": "opaque-id",
  "decision": "allow",
  "retry_count": 0,
  "summary": "Context Guardian found no actionable issue.",
  "issues": [],
  "coverage_gaps": [],
  "checks": [],
  "truncated": false
}
```

`decision` values are:

- `allow`: completion may proceed;
- `retry`: actionable problems should be returned to the same agent for correction;
- `report`: do not retry; expose unresolved problems or an incomplete check;
- `error`: the guard could not establish a trustworthy result.

Each issue needs a stable code, severity, concise message, evidence paths, applicable
feature/invariant identifiers, and one clear expected action. Include complete
counts when issue or path lists are truncated. Do not inject entire feature maps,
test logs, or diffs into the agent context.

## Token Policy

- Background monitoring and deterministic checks use zero model tokens.
- A clean result should inject nothing when the runtime permits, otherwise only a
  tiny machine status.
- A retry injects the bounded issue summary plus only the references needed to fix
  those issues.
- The connector must never append the entire run log, map, profile, or validation
  output to the prompt.
- Token and byte caps belong in shared configuration and must be regression-tested.

## Validation Safety & Risk Classification

Profiles are repository-controlled input and commands are executable code. Context Guardian
inspects every validation command before reporting or execution via `inspect_command_safety`
and the CLI command `inspect-safety`:

```bash
context-guardian inspect-safety --command "<cmd>"
context-guardian inspect-safety --project <path>
```

Commands are classified into three deterministic safety tiers:
- `safe`: a recognised verification invocation: a test/lint/typecheck tool (`pytest`,
  `jest`, `tsc`, `eslint`, `ruff`, `cargo test`, `go test`, `swift test`, ...) or a
  package-manager script whose *name* is a verification name (`npm test`, `yarn lint`,
  `pnpm run typecheck`). A verification word elsewhere in a command (`node deploy-test.js`)
  is not enough. The body of a package script is repository-defined: `safe` means
  "conventionally a check", not "proven harmless". `*:audit` scripts are not safe by name (some
  write reports); a person can approve a verified read-only one on their machine with
  `context-guardian trust` (see `profile-schema.md`, `commands.trusted`);
- `requires-approval`: Network-accessing (`curl`, `git pull`), database migration (`prisma migrate`), or infrastructure tools (`kubectl apply`, `terraform apply`);
  a recognised checker with an option that rewrites files (`eslint --fix`, `ruff format`,
  `prettier --write`), downloads code (`npx --yes`) or runs another program
  (`go test -exec`, `gradle --init-script`); and anything the classifier will not interpret:
  substitution, redirection, backslash escapes, control characters, unclosed quotes;
- `disallowed`: Destructive operations (`rm -rf /`, `mkfs`), package publishing (`npm publish`, `cargo publish`), remote repository pushes (`git push`, also `git -C . push`), or credential overwrites.

Every command is split on `;`, `&&`, `||`, `|`, `|&`, `&`, newlines and carriage returns,
reading quotes as a POSIX shell does, and the most severe segment decides the tier.

When a profile declares disallowed commands, `finish` flags a `disallowed-validation-command` issue.

## Task-Intent Pre-Flight Routing Engine

Before source files are modified, agents can evaluate natural language prompts using
`preflight-intent`:

```bash
context-guardian preflight-intent \
  --project <path> --intent "Fix push notification delivery and badge counter"
```

The router tokenizes the task intent, matches keywords against feature identifiers, triggers,
doc summaries, and map symbols, and transitively pulls in dependent features and safety-inspected
validation plans. Matching is lexical and English-oriented; write intents in English.

## Runtime Event Mapping

### Claude Code

- Start: `UserPromptSubmit` calls `begin` before Claude processes the turn.
- Finish: `Stop` calls `finish` when Claude finishes responding.
- An issue response uses the runtime's documented stop decision/feedback mechanism
  to keep the turn active for correction.
- Guard against recursive stop-hook execution using runtime hook state and the
  shared retry counter.

Claude Code also supports project settings and plugin hook bundles. Installation
scope must be chosen explicitly; a project hook is portable with that application,
while a plugin can enable the same connector across projects.

### Gemini CLI

- Start: `BeforeAgent` calls `begin` after prompt submission and before planning.
- Finish: `AfterAgent` calls `finish` after the final response.
- `decision: "deny"` with a reason requests an automatic retry; combine the runtime's
  stop-hook-active indicator with the shared retry counter.

Gemini CLI supports extension `hooks/hooks.json` and project settings. Connector
tests must target the installed CLI version because lifecycle bugs or behavior
changes are possible even when an event exists in the documented schema.

### Codex

The local Codex plugin format demonstrably supports hook bundles and tool lifecycle
events such as `PostToolUse`. Before declaring automatic protection complete, test
the exact Codex desktop and CLI versions in scope for:

- a reliable once-per-user-turn start event;
- a reliable completion event;
- structured feedback to the same running agent;
- the ability to reject completion and retry safely;
- behavior in subagents, worktrees, interruptions, and resumed tasks.

If a Codex surface lacks one of these capabilities, use a Codex-owned turn wrapper or
an explicit mandatory preflight/postflight instruction as the documented fallback.
Do not claim that a post-tool hook is equivalent to a completion hook.

The current bootstrap uses that fallback: it merges a managed Context Guardian block
into the application's `AGENTS.md`. Codex therefore discovers the lifecycle instructions
in each new task and runs the zero-token local `begin`, `preflight-intent`, and `finish`
commands itself. This is instruction-enforced rather than a native completion hook and
must continue to be described that way.

## Run Record Retention

`begin` prunes `.app-context/runs/` records older than 14 days and keeps at most 500 files.
Run state is listed in the managed `.gitignore` block and should never be committed.

## Retry and Failure Policy

The retry cap must be finite and configurable, with a conservative default selected
after pilot measurement. On reaching the cap, return `report`, allow the agent to
finish, and require the final response to name unresolved issues and unchecked paths.
Infrastructure failure must not masquerade as clean. Use `error` or `report`
according to whether safe continuation is possible.

## Minimum Test Matrix

Test the shared engine independently from every connector:

- clean task with no changes;
- mapped safe change;
- invariant conflict;
- unmapped file and stale map;
- failed, timed-out, and disallowed validation;
- pre-existing dirty file left untouched;
- pre-existing dirty file also modified during the task;
- staged, unstaged, untracked, rename, and unborn-branch states;
- cancelled and crashed task;
- duplicate hook delivery and stale-run recovery;
- nested agent and parallel-task overlap;
- retry succeeds, retry reaches cap, and connector recursion prevention;
- output truncation, token cap, and paths containing spaces.

Each of Codex, Claude Code, and Gemini CLI then needs an integration fixture proving
that start runs once, finish runs once per attempt, issues reach the same agent, a
clean result does not bloat context, and a retry cannot loop indefinitely.
