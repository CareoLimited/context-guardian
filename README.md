# Context Guardian

Keeps coding agents inside your application's map.

<p align="center">
  <img src="https://raw.githubusercontent.com/CareoLimited/context-guardian/main/docs/images/overview.svg" width="880" alt="An agent edits src/checkout/pay.ts; Context Guardian finds that src/profile/settings.ts imports it and tells the agent which notes to read and which commands are safe to run.">
</p>

Before an agent edits, Context Guardian tells it which features, invariants, and checks a
task touches. When the agent finishes, it compares what changed against that map and
reports what the agent missed: a feature that imports the code it changed, a platform
twin it did not update, a business rule it should not have weakened.

It works with Claude Code, Gemini CLI, Codex, Cursor, GitHub Copilot, and other agents
that read skills or `AGENTS.md`, and from a plain terminal with no agent at all. The
engine is local, deterministic Python: no model calls, no network, no model tokens. Only
its short reports reach the agent.

> **Status: alpha.** Built for and used on React Native / Expo mobile applications.
> Imports and calls are resolved for TypeScript, JavaScript, and Python; Swift, Kotlin,
> Dart, Go, and other languages get lighter, pattern-based symbol extraction. Expect
> rough edges, and keep your tests: Context Guardian selects context and checks, it does
> not prove behaviour.

## Quick start

You need [uv](https://docs.astral.sh/uv/) and git. uv fetches a suitable Python (3.11+)
if you do not have one.

```bash
# 1. The command (once per machine)
uv tool install context-guardian-cli
#    or straight from GitHub:
#    uv tool install git+https://github.com/CareoLimited/context-guardian

# 2. Let your agents find the skill (optional; shows a plan and asks first)
context-guardian install

# 3. Onboard an application (once per repository)
cd path/to/your-app
context-guardian bootstrap --project .

# 4. Check everything
context-guardian doctor
```

`bootstrap` detects your stack and feature folders, writes `.app-context/`, builds the
map, and adds the integration files listed below. Review `.app-context/profile.json` and
commit it with the rest.

## What each agent gets

| Agent | Start and finish checks | How |
|---|---|---|
| Claude Code | Automatic | Hooks in `.claude/settings.json` |
| Gemini CLI | Automatic | Hooks in `.gemini/settings.json` |
| Codex, Cursor, GitHub Copilot, others | Agent runs them | Instructions in `AGENTS.md`, plus the skill |
| No agent | You run them | `context-guardian begin` / `finish`, `inspect`, `doctor` |

Every hook is one line that is identical on every machine, for example
`context-guardian hook --runtime claude-code --event Stop`. Each teammate installs the
command once; on a machine without it the hook prints a warning and the agent carries on
unchecked. It never blocks anyone.

## What a check looks like

An agent changes `src/checkout/pay.ts`. `src/profile/settings.ts` imports it, so at the
end of the turn Context Guardian selects both features and their notes:

```json
{
  "decision": "allow",
  "changed_files": ["src/checkout/pay.ts"],
  "selected_features": [
    { "id": "checkout", "matched_by": ["trigger", "impact"], "impact_hop": 1 },
    { "id": "profile",  "matched_by": ["impact"],            "impact_hop": 2 }
  ],
  "read": [".app-context/features/checkout.md", ".app-context/features/profile.md",
           ".app-context/invariants/product.md"],
  "validation_plan": [{ "command": "npm run typecheck", "safety": { "tier": "safe" } }]
}
```

(Shortened.) Errors send the agent back for another pass, a bounded number of times.
Warnings are handed to you with the instruction to explain them and ask before changing
anything.

## Your application's `.app-context/`

```text
.app-context/
  profile.json         features, path triggers, invariants, validation commands   (commit)
  features/*.md        what each feature is and must keep doing                   (commit)
  invariants/*.md      rules that apply to every change                           (commit)
  generated/           the code-derived map, refreshed automatically              (commit)
  runs/ findings.json watcher.json   per-turn state                               (ignored)
```

Curated files are yours; Context Guardian never rewrites them. The map is regenerated
from the code.

## Safety

- **Commands.** Validation commands are classified as `safe`, `requires-approval`, or
  `disallowed`, segment by segment, the way a shell would run them. Agents may run only
  `safe` commands unasked. Publishing and pushing are always disallowed.
  `context-guardian inspect-safety --command "<cmd>"` shows the verdict.
- **Trust stays with you.** A repository can *ask* to trust a project-specific check in
  `commands.trusted`; it takes effect only after you approve it on your machine with
  `context-guardian trust --project . --command "<cmd>"`. Approvals are stored in your
  user configuration, never in a repository.
- **Writes stay in the project.** Every file Context Guardian writes or deletes is checked
  first; nothing follows a symlink out of the repository.
- **No network, no telemetry.** The engine reads your repository and runs `git`.

## Commands

| Command | What it does |
|---|---|
| `bootstrap` / `upgrade` | Onboard an application; bring an older setup up to date |
| `begin` / `finish` / `abort` | Start and check a task (hooks do this for you) |
| `preflight-intent --intent "<task>"` | Which features and notes a task description points to |
| `inspect` | Context for the current diff |
| `query-map` / `impact-map` | Bounded neighbourhoods of the code map |
| `check-twins` | iOS / Android counterparts changed together, or intentionally not |
| `inspect-safety`, `trust` | Command risk tiers; your own approvals |
| `install` / `uninstall` / `doctor` | Agent setup on this machine; health check |

Run `context-guardian <command> --help` for options. Most commands print compact JSON for
agents; `install`, `uninstall`, and `doctor` print readable text (`--json` for agents).

## Uninstall

```bash
context-guardian uninstall --project path/to/your-app   # removes hooks and blocks, keeps .app-context/
context-guardian uninstall                              # removes the agent skill links
uv tool uninstall context-guardian-cli
```

## Development

```bash
git clone https://github.com/CareoLimited/context-guardian
cd context-guardian
python3 -m unittest discover -s tests
```

The design and its decision log are in [docs/DESIGN.md](docs/DESIGN.md). The engine
uses only the Python standard library; please keep it that way.

## License

[MIT](LICENSE) © 2026 Careo Limited
