# Session cleanup contract, version 1

This contract aligns `claude-session-cleanup` and `codex-session-cleanup` so
that a future single skill (`agent-session-cleanup`) can drive both through
independent backends. It defines:

1. retention-policy semantics ([§2](#2-retention-semantics));
2. a versioned preview JSON document ([§4](#4-preview-json-document)),
   specified by [`preview-v1.schema.json`](preview-v1.schema.json);
3. shared safety terminology ([§3](#3-terminology)).

Declarative conformance cases live in [`fixtures/`](fixtures/). The current
state of each backend is tracked in
[`compatibility-matrix.md`](compatibility-matrix.md); the Codex migration is
described in [`codex-handoff.md`](codex-handoff.md).

There is no shared runtime package. Each repository vendors this directory
(byte-identical copy, verified by `SHA256SUMS`) and tests its own
implementation against it.

Status: **draft 1** — agreed by neither release yet. Both repositories must
pass the fixtures before either publishes a release that claims
`schema_version: 1`.

## 1. Scope and non-goals

The contract covers *previews*: what a policy would select and why. It does
not cover:

- **Saved plans / manifests** used to authorize destructive actions. They
  carry full identifiers, safety snapshots, replay state, and integrity data,
  and have stricter validation and privacy requirements. Each backend keeps
  its own manifest format. A preview document is never an authorization to
  mutate anything.
- Mutation mechanics, locking, or restore procedures.
- How a backend discovers sessions.

## 2. Retention semantics

### 2.1 Retention unit: the root group

Policies are evaluated over **root groups**. A root group is a root session
plus every session whose data or lifecycle is tied to it, so that acting on
the group never affects an unrelated session and keeping a recent member
keeps everything it depends on.

| Backend | Root | Members |
|---|---|---|
| Codex | An ordinary interactive thread with no parent | Every spawned descendant reachable through `parentThreadId` (the set `codex archive`/`codex delete` cascades over) |
| Claude Code | The origin session of a linked chain | Sessions linked by `continued-in` records or by symlinks into each other's session data (background continuations). Subagent transcripts are *data inside* a session, not members |

Rules:

- A member is never selected, retained, protected, or excluded on its own;
  the group has exactly one status.
- Each group is identified by its root session id (`group_id`).
- Members carry `relation` (`root`, `descendant`, `continuation`) and
  `parent_session_id`.
- If membership cannot be determined unambiguously (missing parent, cycle,
  a session claimed by two roots, link to an ambiguous id), the group is
  **excluded** (`ancestry-ambiguous` or `duplicate-session-id`).

### 2.2 Last activity

Each member has a `last_activity` timestamp from the backend's activity
source (§2.7). A group's **last activity** is the maximum over its members.
All age and ranking decisions use the group's last activity.

Consequence: an old root with a recently used descendant, subagent, or
continuation is treated as recent. Nothing recent can be selected indirectly
through an old parent.

### 2.3 Scope

- `current` (default): the scope path is the physical path of the invoking
  directory (symlinks resolved, Unicode NFC; case-insensitive comparison only
  on platforms whose paths are case-insensitive). A session *belongs* to the
  scope only if its project path equals the scope path exactly. Parent
  directories, child directories, sibling git worktrees, and moved
  repositories never match.
- `all`: every discoverable group. Must be requested explicitly.

In `current` scope:

- a group is in scope if any member belongs to the scope path;
- a group with a member that belongs to a *different* project is
  **excluded** (`related-outside-scope`), because acting on it would cross
  the boundary;
- a backend that finds sessions which might belong to the scope but cannot
  verify their project identity must report them as **excluded**
  (`project-identity-unverified` / `project-identity-unknown`), never drop
  them silently.

### 2.4 Statuses and precedence

Every in-scope group has exactly one status:

| Status | Meaning | Ranked |
|---|---|---|
| `excluded` | The group cannot be evaluated safely: unknown, malformed, or ambiguous metadata, unverifiable identity, or a scope-crossing relationship | No |
| `protected` | A safety rule forbids selection whatever the policy says | Yes |
| `retained` | The policy keeps it (too recent, or within the latest N) | Yes |
| `selected` | Every policy selector applies and no protection does | Yes |

Precedence is `excluded` > `protected` > `retained` > `selected`. `reasons`
lists every applicable reason of the winning status and, for `protected`,
also any retention reasons, in registry order (§3.3).

### 2.5 Policy selectors

At least one selector is required.

- **`--older-than DURATION`** — positive whole hours (`48h`) or days (`7d`).
  The cutoff is computed once: `cutoff = evaluated_at - DURATION`. A group
  is old only if `last_activity < cutoff` (strict). Otherwise it is retained
  with `newer-than-cutoff`.
- **`--keep-latest N`** — positive integer. The **ranking universe** is every
  in-scope group that is not excluded (selected, retained, and protected
  groups alike). Groups are ordered by last activity descending, then by
  `group_id` ascending (byte-wise on the canonical lowercase id). Ranks
  start at 1. A group with rank ≤ N is retained with `within-latest`.
  - Each group counts once, however many sessions it contains.
  - Protected groups occupy ranks: "keep latest 5" keeps the five most
    recent groups, whatever else protects them.
  - Excluded groups do not occupy ranks. Removing them can only move other
    groups into the latest N, so unknown data can only cause more to be
    kept.
- **Combination** — intersection. A group is selected only if it is old (when
  `--older-than` is given) **and** ranked above N (when `--keep-latest` is
  given) **and** not protected.

### 2.6 Determinism

- Activity comparisons use the backend's full timestamp precision; output
  timestamps are truncated to seconds.
- Ties are broken by `group_id` ascending; members are ordered root first,
  then by depth, then last activity descending, then `session_id`.
- With identical inputs and the same `evaluated_at`, two runs produce
  byte-identical JSON.

### 2.7 Activity timestamp sources

The shared semantics use one abstract value, `last_activity`. Backends obtain
it differently, and the preview declares which source it used
(`activity_timestamp` object):

| | Codex | Claude Code |
|---|---|---|
| `source` | `codex-app-server:recencyAt` | `max(transcript-mtime, session-artifact-mtimes, message-timestamps)` |
| `kind` | `agent-reported`: Codex's own last-use ordering value | `derived`: computed by the tool from files; Claude Code exposes no last-used value |
| Resolution | 1 s (integer Unix time) | Nanosecond mtimes and millisecond message timestamps |
| Known distortions | Defined by Codex; not independently verified | mtime also advances on non-use writes (title records, failed resumes, copies); all distortions make sessions look *more* recent |
| `conservative` | `false` (single authoritative value) | `true` (maximum over all evidence) |

Neither backend may substitute another timestamp when its declared source is
missing; the member is excluded instead (`metadata-malformed`).

Claude Code's own retention sweep uses transcript mtime only. That is
reported separately (Claude extension `native_retention`), never mixed into
`last_activity`.

## 3. Terminology

### 3.1 Operations

| Term | Definition | Reversible by | Who performs it |
|---|---|---|---|
| **Preview** | Evaluate a policy and report; changes nothing. Every run without an explicit apply option is a preview | — | Tool (read-only) |
| **Archive** | Codex's native operation (`codex archive`). Moves sessions out of the active list; Codex can unarchive them | The agent (`codex unarchive`) | The agent |
| **Quarantine** | A tool-managed operation that moves session data out of the agent's storage into a store owned by the tool. The agent has no knowledge of it | Only this tool's **restore**, under the tool's own integrity guarantees | The tool |
| **Restore** | Undo a quarantine: verify integrity and copy data back without overwriting anything | — | The tool |
| **Permanent delete** | Irreversible removal. Requires an approved saved plan | Nothing | The agent (Codex) or not offered (Claude Code) |

Archive and quarantine are **never interchangeable**. A backend that lacks an
operation reports it as unsupported; it never maps a request for one onto the
other. A unified tool must ask for the action by name and show its
reversibility.

### 3.2 Evaluation outcomes

| Term | Definition |
|---|---|
| **Selected** | Eligible for the requested action under the policy. In a preview, nothing happens to it |
| **Retained** | Kept because the policy says so (`newer-than-cutoff`, `within-latest`) |
| **Protected** | Kept because a safety rule forbids acting on it |
| **Excluded** | Not evaluated because its metadata is unknown, malformed, ambiguous, or crosses the scope; never selected, never ranked |
| **Kept** | Informal umbrella for retained + protected + excluded |
| **Stale plan** | A saved plan whose candidate set or safety-relevant state no longer matches a fresh evaluation; it must be refused, never partially re-targeted (manifest concern, §1) |
| **Unknown state** | A safety-relevant field that is missing, malformed, or has a value the backend does not recognize. Never guessed: the group is excluded (`metadata-malformed`) or protected (`unsafe-runtime-state`) |
| **Unsupported** | An operation or capability the backend does not offer; reported in `capabilities`, never emulated |

### 3.3 Reason code registry

Codes are lowercase, hyphenated, and stable. The registry order below is the
order in which reasons appear in `reasons`.

| Code | Status | Meaning | Emitted by |
|---|---|---|---|
| `older-than` | selected | Last activity before the cutoff | both |
| `outside-latest` | selected | Ranked above N | both |
| `invoking-session` | protected | The session running the tool | Claude Code |
| `pinned` | protected | Pinned by the user in the agent | Codex |
| `running` | protected | The agent reports, or a verified signal shows, the session is running or loaded | both |
| `possibly-running` | protected | A weaker signal suggests the session may be running | Claude Code |
| `background-session` | protected | A background-session record references it | Claude Code |
| `ephemeral` | protected | The agent marks the session ephemeral | Codex |
| `unsafe-runtime-state` | protected | A runtime state other than known idle states (including unrecognized states) | Codex |
| `recently-active` | protected | Activity within the minimum idle period | Claude Code |
| `within-latest` | retained | Rank ≤ N | both |
| `newer-than-cutoff` | retained | Last activity at or after the cutoff | both |
| `related-outside-scope` | excluded | A member belongs to another project (current scope) | both |
| `project-identity-unknown` | excluded | No project path could be read | both |
| `project-identity-unverified` | excluded | A project path exists but cannot be proven to be the session's | Claude Code |
| `project-path-collision` | excluded | Stored where the scope's sessions are stored, but belongs to a different path | Claude Code |
| `metadata-malformed` | excluded | A required field is missing, malformed, or unreadable | both |
| `ancestry-ambiguous` | excluded | Membership cannot be determined unambiguously | both |
| `duplicate-session-id` | excluded | The same id appears in more than one place | both |
| `unsafe-storage` | excluded | Session data contains symbolic links that leave it, special files, or files owned by another user | Claude Code |

Consumers must treat an unknown code as making the group non-selectable,
whatever status accompanies it, and must display it verbatim.

### 3.4 Mutation blockers

`mutation_blockers` (per group) and `capabilities.actions.*.unsupported_reason`
explain why an action could not be applied even to a selected group. Codes
are backend-specific and free-form-but-stable (for example
`unknown-associated-artifact`, `cross-session-symlink`,
`active-session-detection-incomplete`). They never change a group's status.

## 4. Preview JSON document

Normative definition: [`preview-v1.schema.json`](preview-v1.schema.json).

### 4.1 Conventions

- Field names are `snake_case`.
- Timestamps: UTC, ISO 8601, second precision, `Z` suffix
  (`2026-10-25T12:00:00Z`).
- Sizes are bytes; `null` when the backend cannot measure them
  (`capabilities.sizes` says which).
- Every core object is closed (`additionalProperties: false`). Backend-specific
  data goes only in `extensions`, an object keyed by backend (`claude_code`,
  `codex`), at the top level, on each group, and on each session.
- `null` means "not available from this backend"; an empty list means
  "available and empty".
- `changes_made` is always `false` in a preview.
- Titles are **opt-in** (`title` is `null` unless the user asked for titles);
  they may contain prompt text. No message content, tool output, or file
  content ever appears in a preview.

### 4.2 Top level

| Field | Type | Notes |
|---|---|---|
| `schema` | `"session-cleanup/preview"` | |
| `schema_version` | `1` | |
| `agent` | `"claude-code"` \| `"codex"` | Backend identifier |
| `agent_version` | string \| null | Installed agent version if the backend queried it (Codex); `null` if it never runs the agent (Claude Code) |
| `tool` | `{name, version}` | |
| `mode` | `"preview"` | |
| `changes_made` | `false` | |
| `evaluated_at` | timestamp | The single planning clock |
| `scope` | `{mode, path}` | `path` is the scope path for `current`, `null` for `all` |
| `policy` | object | `older_than_seconds`, `cutoff`, `keep_latest`, `min_idle_seconds`, `combination: "intersection"`, `ranking_unit: "root-group"`, `requested_action` |
| `activity_timestamp` | object | `source`, `kind`, `resolution`, `conservative` (§2.7) |
| `capabilities` | object | See §4.3 |
| `totals` | object | `groups`, `sessions`, `bytes` counts per status, and `reasons` counts per status |
| `groups` | array | Ordered by status (`selected`, `retained`, `protected`, `excluded`), then last activity descending, then `group_id` |
| `warnings` | array of `{code, subject}` | Document-wide conditions; `subject` is a path, id, or `null` |
| `extensions` | object | Backend-specific |

### 4.3 Capabilities

```json
{
  "actions": {
    "archive":    {"supported": true,  "reversibility": "agent-native",  "requires_saved_plan": false, "unsupported_reason": null},
    "quarantine": {"supported": false, "reversibility": "tool-managed",  "requires_saved_plan": false, "unsupported_reason": "not-offered-by-backend"},
    "delete":     {"supported": true,  "reversibility": "irreversible",  "requires_saved_plan": true,  "unsupported_reason": null}
  },
  "sizes": false,
  "titles": true,
  "pinning": true,
  "active_session_detection": "agent-reported"
}
```

- `reversibility` is fixed per action name: `archive` → `agent-native`,
  `quarantine` → `tool-managed`, `delete` → `irreversible`.
- `active_session_detection`: `agent-reported` (the agent's own runtime
  state), `partial` (heuristic signals; coverage gaps documented), or `none`.
- `unsupported_reason` codes: `not-offered-by-backend`,
  `read-only-release`, `blocked-by-safety-requirements`, or a backend code.

### 4.4 Group

| Field | Type |
|---|---|
| `group_id` | string (root session id) |
| `status` | `selected` \| `retained` \| `protected` \| `excluded` |
| `reasons` | array of reason codes (§3.3) |
| `rank` | integer ≥ 1, or `null` when excluded |
| `proposed_action` | `policy.requested_action` when selected, else `null` |
| `last_activity` | timestamp |
| `last_activity_session_id` | member that supplied it |
| `bytes` | integer \| null |
| `session_count` | integer ≥ 1 |
| `sessions` | array of sessions (root first) |
| `mutation_blockers` | array of strings |
| `extensions` | object |

### 4.5 Session

| Field | Type |
|---|---|
| `session_id` | string |
| `relation` | `root` \| `descendant` \| `continuation` |
| `parent_session_id` | string \| null |
| `title` | string \| null |
| `title_source` | string \| null (backend vocabulary, e.g. `custom-title`, `name`, `preview`) |
| `project_path` | string \| null |
| `project_identity` | `verified` \| `unverified` \| `unknown` |
| `created_at` | timestamp \| null |
| `last_activity` | timestamp |
| `last_activity_source` | string (which evidence supplied it) |
| `bytes` | integer \| null |
| `issues` | array of `{code, subject}` |
| `extensions` | object |

## 5. Versioning and compatibility

- `schema_version` identifies the document shape and the semantics of §2.
- Within version 1:
  - new reason codes (§3.3), mutation-blocker codes, warning codes, and
    `title_source` / `last_activity_source` values may be added;
  - anything may be added inside `extensions`;
  - nothing else changes. Adding, removing, renaming, or retyping a core
    field, or changing a rule in §2, requires version 2.
- Producers emit exactly one version. Consumers must check `schema` and
  `schema_version` first and refuse versions they do not know.
- Conformance fixtures carry their own `format`/`version`; a semantic change
  that alters any expected outcome is a version-2 change.
- The contract directory is vendored byte-identically into both repositories
  (`contract/** -text` in `.gitattributes`). `SHA256SUMS` lists every file;
  each repository's tests verify it, so an accidental edit fails the build and
  a deliberate one requires regenerating the checksums and copying the
  directory to the other repository.
