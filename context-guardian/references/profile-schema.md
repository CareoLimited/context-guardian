# Application Profile Contract

Each application owns `.app-context/profile.json`. Paths are relative to the
application repository root unless explicitly documented otherwise.

## Initial schema

```json
{
  "schema_version": 1,
  "app_id": "example-app",
  "display_name": "Example App",
  "stacks": ["react-native", "expo", "typescript"],
  "commands": {
    "quick": ["npm run typecheck"],
    "full": ["npm test", "npm run typecheck"]
  },
  "global_invariants": [
    ".app-context/invariants/product.md"
  ],
  "features": [
    {
      "id": "notifications",
      "triggers": [
        "src/services/notifications/**",
        "src/**/Notification*.tsx",
        "app.json"
      ],
      "references": [
        ".app-context/features/notifications.md"
      ],
      "invariants": [
        ".app-context/invariants/notifications.md"
      ],
      "validation": [
        "npm test -- notifications"
      ]
    }
  ]
}
```

## Required fields

- `schema_version`: currently `1`.
- `app_id`: stable lowercase identifier using letters, digits, and hyphens.
- `display_name`: human-readable application name.
- `features`: array of feature-routing objects.

## Feature routing

Each feature requires:

- `id`: stable feature identifier;
- `triggers`: repository-relative glob patterns matched against changed files;
- `references`: focused Markdown files an agent reads when the feature is selected.

Optional feature fields:

- `depends_on`: array of other feature IDs this feature depends on; selected features
  transitively pull in dependent references, invariants, and validation commands;
- `invariants`: curated rules that must remain true;
- `validation`: commands relevant to that feature.

## Optional top-level fields

- `stacks`: technology hints used for routing, not installation.
- `commands.quick`: inexpensive checks suitable after ordinary edits.
- `commands.full`: broader checks for risky edits and release preparation.
- `commands.trusted`: exact command strings the team *asks* to have run without
  approval, for project-specific checks no generic rule can recognise (for example
  `node scripts/custom-check.cjs`). Because anyone who can change the repository can
  change this list, it grants nothing by itself: each person approves a listed command on
  their own machine with `context-guardian trust --project . --command "<cmd>"`, which is
  stored in their user configuration, never in the repository. A command that matches a
  disallowed pattern cannot be approved.
- `global_invariants`: curated rules relevant to every application change, always
  included in `read` references.
- `intentional_differences`: array of repository-relative path glob patterns documenting
  accepted cross-platform divergences, suppressing false-positive twin synchronization warnings.
- `hooks.preflight` (boolean, default `false`): when `true`, the start hook injects bounded
  feature routing for the user's prompt into each turn. Off by default so quiet turns cost
  nothing.
- `map.auto_refresh` (boolean, default `true`): `finish` refreshes a stale generated map
  before evaluating a task. Set `false` for repositories where a rebuild is too slow for a
  hook; the map is then refreshed manually with `refresh-map`.

Validation commands should name things that exist. Bootstrap only proposes package.json
scripts that are defined, and `upgrade` reports commands that call missing scripts.

## Design constraints

- Keep references focused by feature; do not create one giant application map.
- Store generated inventories below `.app-context/generated/` and label them as
  generated. Do not overwrite curated invariants mechanically.
- Commands must be non-interactive unless a reference explicitly explains why an
  interactive check is necessary.
- Never put secrets, access tokens, signing material, or private user data in a
  profile, map, invariant, or generated report.
- When this schema changes, update the decision record in `docs/DESIGN.md` in the same
  change.
- Every application owns a separate profile and separate maps. Do not reuse another
  application's business rules without application-specific review.
- The shared Context Guardian engine must not hard-code application IDs, repository
  paths, feature names, frameworks, or product behavior.
- `stacks` is descriptive routing information, not a requirement that limits the
  profile to current frameworks.
- Applications may omit domains they do not use; domain-specific analyzers and fields
  must remain optional.
- A new application should normally be onboardable by adding application-owned
  context rather than editing the shared engine.
- Unknown frameworks and unmatched files must produce explicit gaps and direct-source
  fallback, not false completeness.
- Schema evolution must be versioned; never change the meaning of an existing field
  silently.
