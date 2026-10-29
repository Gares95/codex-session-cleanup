# Conformance fixtures

Each `*.json` file is one backend-independent retention case
(`format: "session-cleanup/conformance-case"`, `version: 1`). Both
repositories run every case through their own policy engine with a small
test adapter; there is no shared runtime code.

## Format

```json
{
  "format": "session-cleanup/conformance-case",
  "version": 1,
  "id": "old-root-recent-descendant",
  "description": "…",
  "requires": [],
  "evaluated_at": "2026-10-25T12:00:00Z",
  "scope": {"mode": "current", "path": "/work/repo"},
  "policy": {"older_than_hours": 720, "keep_latest": null},
  "sessions": [
    {"id": "a", "parent": null, "project": "/work/repo", "hours_ago": 2160, "state": "idle", "metadata": "valid"},
    {"id": "a1", "parent": "a", "project": "/work/repo", "hours_ago": 2, "state": "idle", "metadata": "valid"}
  ],
  "expected": [
    {"group": "a", "status": "retained", "rank": 1, "reasons_include": ["newer-than-cutoff"]}
  ]
}
```

| Field | Meaning |
|---|---|
| `requires` | Capabilities the case needs, named as in `capabilities` (`pinning`). A backend may skip the case only if it genuinely lacks a listed capability and reports it as `false` in its previews; the skip must be reported (SPEC §6) |
| `evaluated_at` | The planning clock |
| `scope.path` | The invoking directory, already canonical; POSIX form |
| `policy.older_than_hours` | Duration in hours, or `null` |
| `sessions[].id` | Opaque identifier. Ordering between ids is byte-wise ascending; an adapter that must use real ids (for example UUIDs) maps them **preserving that order** |
| `sessions[].parent` | `null` for a root; otherwise the id of the parent member. Members form trees |
| `sessions[].project` | The session's verified project path |
| `sessions[].hours_ago` | `last_activity = evaluated_at - hours_ago` |
| `sessions[].state` | `idle`; `running` (the agent or a verified signal reports it running or loaded); `pinned` |
| `sessions[].metadata` | `valid`, or `malformed` (a required field the backend needs is unreadable) |
| `expected` | One entry per **in-scope** group. Groups not listed must not appear at all (out of scope) |
| `expected[].group` | Root id |
| `expected[].status` | Exact status |
| `expected[].rank` | Exact rank, or `null` for excluded groups |
| `expected[].reasons_include` | Reason codes that must be present. Backends may emit additional codes |

## Adapter rules

- Build the backend's own session objects from `sessions`; do not shortcut
  the policy engine.
- Map `parent` onto the backend's relationship model: Codex
  `parentThreadId` descendants; Claude Code continuation links.
- Map `running` onto the strongest protection signal the backend has: Codex
  runtime status `active`; Claude Code a verified registry entry.
- Map `malformed` onto the backend's own "cannot parse a required field"
  path, so the real exclusion code is exercised.
- Disable backend-only guards that the case does not model (for example
  Claude Code's minimum-idle guard) or set them so they cannot trigger.
- Compare: the set of group ids, then status, rank, and `reasons_include` for
  each group.
- Derive skips from the backend's reported capabilities, not from a separate
  hard-coded list, so a skip cannot outlive the capability gap.

## Cases

| File | Rule |
|---|---|
| `01-age-only` | Strict cutoff |
| `02-keep-latest` | Latest N by group |
| `03-combined-old-within-latest` | Intersection keeps old groups inside N |
| `04-combined-new-outside-latest` | Intersection keeps recent groups outside N |
| `05-exact-project-boundary` | Parent, child, sibling worktree, other project out of scope |
| `06-all-projects` | Explicit all-project scope; tie-break |
| `07-old-root-recent-descendant` | Recent descendant keeps old root |
| `08-latest-n-counts-groups` | A group counts once toward N |
| `09-protected-descendant` | Running descendant protects the group |
| `10-protected-occupies-rank` | Protected groups take ranks |
| `11-related-outside-scope` | Boundary-crossing group excluded, unranked |
| `12-equal-activity-tie-break` | Deterministic ordering by id |
| `13-malformed-root-excluded` | Malformed metadata excluded, unranked |
| `14-malformed-descendant-excluded` | Malformed member excludes the group |
| `15-pinned-group-protected` | Pinned protection (requires `pinning`) |
