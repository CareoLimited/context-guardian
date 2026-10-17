# Self-Bootstrap Installation Contract

Read this reference when implementing, invoking, testing, or repairing Context
Guardian installation. The goal is that a user can supply one package or repository
URL while working on an application and the receiving agent completes setup before
resuming the original task.

## Accepted Handoffs

Preferred forms are:

- the complete `context-guardian/` directory;
- the canonical Context Guardian repository directory;
- the canonical GitHub repository URL when network access is authorized;
- a release archive containing the complete portable package.

A standalone `SKILL.md` is enough to explain the concept but not enough for offline
installation or operation because scripts and references are absent. When only that
file is supplied, the agent must request or retrieve the complete trusted package
rather than inventing missing files.

## Intended User Interaction

The user should be able to say the equivalent of:

```text
Use this Context Guardian package for this application and complete my task.
```

The agent then performs one bootstrap operation. It may surface a native permission
request when the runtime requires approval to write a user-level skill or hook
configuration. It must not ask the user to choose paths or commands that can be
detected safely.

## Bootstrap Phases

### 1. Discover

Determine, without model inference where possible:

- package root and package integrity;
- active runtime and version;
- operating system and supported path conventions;
- user-level skill/configuration roots;
- current application repository/worktree root;
- existing Context Guardian installation and version;
- existing `.app-context` profile, index, freshness, and coverage status.

### 2. Plan

Produce a bounded plan containing exact paths and operations. Classify every action
as read-only, application-local, or user-level runtime configuration. Stop on an
ambiguous existing path, untrusted package, unsupported runtime, or configuration
that cannot be merged safely.

### 3. Install discovery

Link the runtime's discovery entry to one canonical package. Do not create a private
copy that can drift. If the supplied source is a release archive or remote repository
without a durable local canonical location, install it into one explicitly managed
Context Guardian source directory and record its provenance/version before linking.

### 4. Install lifecycle connector

Install only the connector for the detected runtime. Merge hook configuration while
preserving unrelated settings and comments where the format permits. Never replace a
whole settings file to add Context Guardian. Record enough managed metadata to make
upgrade and uninstall precise.

### 5. Verify runtime integration

Run a harmless probe that proves:

- the runtime discovers `context-guardian`;
- prompt/task start reaches the shared lifecycle engine;
- completion reaches the shared lifecycle engine;
- a synthetic issue can reach the same agent without an infinite retry;
- clean completion adds no substantial model context;
- runtime state and cleanup work at the expected paths.

A configuration file existing on disk is not proof of readiness.

### 6. Onboard or refresh the application

If `.app-context` is missing, invalid, stale, or incomplete, invoke the full
code-derived map onboarding workflow. Application-local writes must remain within the
application the user supplied. Generated facts and curated knowledge stay separate;
the agent must review inferred product boundaries and report remaining coverage gaps.

If initial indexing is too expensive to finish before the original task, create no
false ready state. Report partial coverage, perform direct source analysis for the
task, and continue indexing only through an authorized background mechanism.

### 7. Resume

Return a compact readiness envelope to the receiving agent, then resume the original
user request without requiring the user to repeat it. The first protected task must
have a valid baseline; setup changes themselves must not be confused with the
application-task delta.

## Implemented Command Surface

The application-local onboarding entrypoint is:

```text
context-guardian bootstrap --project <application-root>
```

It detects supported stacks and feature directories, creates the application profile
and documentation skeletons when absent, merges current Claude Code and Gemini CLI project
hook schemas, installs the Codex `AGENTS.md` lifecycle fallback and a managed `.gitignore`
block, and builds the initial map last so the first task does not start with a stale map.
Re-running it updates only Context Guardian-managed hook handlers and blocks while
preserving unrelated settings. With an existing profile it changes neither the profile nor
the feature docs.

Bootstrap proposes only validation commands that can run: package.json scripts that are
defined (using the lockfile's package manager), pytest only when a test suite exists, ruff
only when configured. Anything it cannot infer (for example an Xcode scheme) is returned in
`notes` for a human to add.

Projects onboarded by an older version are migrated with:

```text
context-guardian upgrade --project <application-root> [--dry-run]
```

`upgrade` refreshes managed hook handlers (only in runtimes already installed), the
`AGENTS.md` block (only if present), the `.gitignore` block, and the generated map. It
never edits curated files; it reports profile commands that call missing scripts, commands
that are not `safe`, referenced docs that do not exist, and run state tracked by Git.
`--dry-run` reports the same plan without writing. A second run is a no-op.

User-level discovery installation, `doctor`, runtime-version probing, transactional
rollback, upgrade, and uninstall remain later installation milestones. Do not imply
that application-local bootstrap performs those operations.

## Idempotency and Conflict Rules

- Re-running the same version against the same canonical source must be a no-op plus
  verification.
- Upgrades must preserve application profiles, curated maps, unrelated settings, and
  runtime permissions.
- A valid Context Guardian link to another canonical source is a conflict, not
  permission to replace it silently.
- A real directory at the intended link path must never be deleted or overwritten.
- Partially written configuration must be recoverable or rolled back precisely.
- Concurrent bootstrap operations require locking and deterministic ownership.
- Uninstall removes only artifacts recorded as Context Guardian-managed.

## Security and Trust

- Validate package identity and expected structure before executing bundled code.
- Show the exact remote repository and revision before first remote installation.
- Do not execute application-declared validation during installation until command
  safety policy is active.
- Never collect or persist prompts, secrets, credentials, signing material, or source
  contents outside the declared local map/cache locations.
- Respect runtime sandboxing, workspace trust, and operating-system permissions.
- Do not weaken hook security, approval policy, or workspace trust to make setup pass.

## Readiness Envelope

Return bounded machine-readable status with at least:

- detected runtime and version;
- canonical package path and package version;
- discovery status;
- lifecycle connector status and probe result;
- application root, profile validity, map freshness, and coverage summary;
- actions completed, actions requiring approval, warnings, and blockers;
- whether the original task can proceed as fully protected, partially protected, or
  ordinary direct-source analysis.

Do not inject installation logs into model context. Keep full local diagnostics in a
bounded cache and provide paths or compact error codes only when needed.

## Test Matrix

Test on Codex, Claude Code, and Gemini CLI for:

- package-directory, repository-directory, URL, and release-archive handoffs;
- fresh user configuration and existing unrelated configuration;
- repeat setup and version upgrade;
- broken link and moved canonical source;
- conflicting file/directory/link at the discovery target;
- permission denial and later retry;
- unsupported runtime version;
- offline operation with a complete package;
- missing/invalid/stale application profile;
- setup interruption and recovery;
- two concurrent setup attempts;
- verification failure after apparently successful file installation;
- correct resumption of the original task.
