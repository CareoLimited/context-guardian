# Agent portability

The portable runtime unit is the entire `context-guardian` directory, not
`SKILL.md` alone. Keep `SKILL.md`, `scripts`, and `references` together when exposing
the skill to an agent.

## Canonical source

The canonical editable source is the Context Guardian repository, wherever it is
cloned. Nothing in an application repository may depend on that absolute path.
Project hook commands and the `AGENTS.md` block call the `context-guardian` command by
name, so every machine that works on an onboarded application needs it installed
(`uv tool install context-guardian-cli`). The discovery links below only let agents find
the skill instructions.

Every agent and human contributor should make changes in that repository. Do not
edit a private copied installation and allow it to diverge. Read the repository's
`docs/DESIGN.md` before changing shared architecture, schema, or workflow decisions,
and update it in the same change when such a decision changes.

## Skills-compatible agents

A skills-compatible agent may discover Context Guardian through a link named
`context-guardian` in its normal skills directory. Point the link at the canonical
`context-guardian/` directory. The link name, skill folder name, and the `name`
frontmatter value must remain `context-guardian`.

Automatic discovery is optional. An agent may instead receive the canonical path
and be instructed to read `context-guardian/SKILL.md` before acting.

## Initial discovery installation

Introduce Context Guardian once per machine:

```bash
uv tool install context-guardian-cli
context-guardian install          # shows the plan, then asks; --yes for agents, --dry-run to preview
context-guardian doctor           # what works, and what to run for the rest
```

`install` finds the agents used on the machine and links this package's skill
directory into each one's user-level skills folder. Where links are not allowed
(Windows without Developer Mode) it copies the directory and leaves a
`.context-guardian-install.json` marker so later runs can update or remove the copy.
It never replaces an entry it did not create, and it never edits an agent's own
settings files.

| Agent | `--agent` | User-level entry |
|---|---|---|
| Claude Code | `claude-code` | `~/.claude/skills/context-guardian` |
| Gemini CLI | `gemini-cli` | `~/.gemini/skills/context-guardian` |
| Cursor | `cursor` | `~/.cursor/skills/context-guardian` |
| GitHub Copilot | `github-copilot` | `~/.copilot/skills/context-guardian` |
| Codex, Cline, OpenCode, Zed, Warp, Amp and other Agent Skills readers | `agents` | `~/.agents/skills/context-guardian` |

`context-guardian uninstall` removes exactly these entries; with `--project <path>` it
also removes the hooks, the `AGENTS.md` block and the `.gitignore` block from one
application, keeping its `.app-context/` data.

After installation, start a new agent session if the agent does not discover the
skill. Gemini CLI can refresh discovery with `/skills reload` and verify it with
`/skills list`.

These entries provide skill discovery only. Automatic start and completion checks
come from the per-project hooks that `bootstrap` installs (Claude Code, Gemini CLI)
and from the `AGENTS.md` block that agents without hooks follow (see
`lifecycle-integration.md`).

## Manual use without an agent

Nothing beyond the command is needed. `context-guardian bootstrap --project .`
onboards an application; `doctor`, `inspect`, `check-twins`, `query-map` and
`impact-map` work from any terminal, and `begin` / `finish` can wrap a manual change
the same way an agent would.

Prefer these user-level entries so that one installation covers all onboarded
applications. Application-specific knowledge still belongs in each application's
`.app-context/` directory.

## Other agents

An agent without skill discovery can be told to read `context-guardian/SKILL.md`
(`context-guardian doctor` prints the package path) and follows the `AGENTS.md` block
in onboarded applications. The core uses only the Python standard library and
requires no proprietary agent SDK.

Reading the skill grants no additional filesystem, shell, network, simulator, or
application permissions. Each runtime still enforces its own authorization model.

## Editing contract

- Keep the core skill independent of a proprietary agent SDK.
- Preserve concise JSON output for low-context agent consumption.
- Preserve ordinary CLI help and actionable error messages for humans.
- Run the bundled tests after changing scripts or the profile contract.
- Update `docs/DESIGN.md` whenever a shared decision is introduced, replaced, or
  materially refined.
- Keep application-specific maps and invariants in the application repository, not
  in agent-specific copies of this skill.
