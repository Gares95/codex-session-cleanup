# codex-session-cleanup

`codex-session-cleanup` is a small Codex skill for previewing retention rules and
safely archiving or deleting old local session history. The helper selects
logical session UUIDs; the installed `codex` command remains responsible for
rollout files, metadata, and descendant cascades.

The helper never edits Codex files, databases, indexes, or other internal state
directly. Python 3.10+ and the standard library are sufficient.

> [!IMPORTANT]
> **Compatibility status (last verified 2026-10-29):** Codex CLI 0.165.0
> exposes pinning through `Thread.section` and paginated
> `threadSection/list`. The helper discovers the unique built-in `Pinned`
> section and validates the installed generated schema on every invocation.
> Compatibility is determined from the installed CLI because public app-server
> [documentation][app-server-docs] may describe a different protocol revision.

## Retention semantics

Supported selectors are deliberately narrow:

- `--older-than 48h` or `--older-than 7d` selects conversations whose newest
  member `recencyAt` is strictly before the absolute cutoff.
- `--keep-latest 5` retains the five most recently active conversations.
- Combining selectors uses intersection semantics: a conversation must be
  older than the cutoff **and** outside the latest N to be eligible.
- `--project current` is the default and requires an exact match with the cwd
  from which the helper is invoked after physical-path and Unicode NFC
  normalization. Parents, children, siblings, and similarly named paths do not
  match.
- `--project all` explicitly considers all discoverable projects.
- `--action archive` is the default. Permanent deletion must be explicit.

One conversation is an ordinary interactive root thread plus every spawned
descendant that Codex would affect when acting on that root. Its activity is
the newest `recencyAt` among all members. Each conversation consumes at most
one latest-N position, regardless of its number of descendants. Non-excluded
conversations are ranked by activity descending and then root UUID ascending.
Pinned, loaded, active, and otherwise protected conversations still consume a
rank. Excluded conversations do not.

Every in-scope conversation has one outcome:

- `selected`: eligible for the requested action;
- `retained`: kept by `within-latest` or `newer-than-cutoff` policy rules;
- `protected`: kept because a safety rule such as `pinned`, `running`, or
  `ephemeral` prevents mutation; and
- `excluded`: not ranked because metadata, ancestry, identity, or exact scope
  cannot be established safely.

Precedence is excluded, protected, retained, then selected. A protected
conversation still reports applicable retention reasons. Exclusion happens
before ranking, so uncertain data can only move another conversation into the
kept latest-N set.

Codex archive/delete operations cascade from a selected root. A conversation
cannot be selected if **any** affected descendant:

- makes the conversation too recent or within latest N;
- is pinned, loaded, active, ephemeral, errored, or malformed;
- crosses the exact cwd boundary for `--project current`; or
- has missing, cyclic, conflicting, or otherwise ambiguous ancestry.

The helper never detaches or independently mutates descendants. Archive is a
Codex-native operation that Codex can reverse with `codex unarchive`.
Permanent delete is irreversible. This tool does not offer a separate
quarantine operation.

## Discovery scope

Root discovery requests ordinary saved interactive sources (`cli` and
`vscode`) and excludes child threads from independent selection. Once a root
is considered, descendant discovery uses every source kind advertised by the
installed schema so subagents and other spawned histories are included in the
cascade check.

Discovery uses `thread/list` with `useStateDbOnly: true`. Consequently:

- only sessions indexed by the supported Codex state database are covered;
- historical or otherwise unindexed rollout files may be omitted;
- the helper does not scan rollout files or ask Codex to repair metadata;
- archive policies consider non-archived roots and descendants only;
- delete policies consider both active and archived roots and descendants;
- current-project discovery enumerates state-DB-indexed ordinary roots so a
  root outside the cwd cannot hide a related descendant inside it;
- a conversation enters current scope when any member exactly matches the cwd,
  then any member outside that cwd excludes the whole conversation; and
- fully out-of-scope groups are omitted from the preview and ranking.

This utility does not claim to cover every file that may exist in Codex
history. Codex 0.165.0 still documents and exposes a normal `thread/list` mode
that scans rollout files and repairs state-database metadata. The helper does
not use that mode because a preview must not silently repair local Codex state.
There is currently no supported way to gain that extra completeness while
preserving the same strict read-only preview behavior.

## Safety model

Every policy invocation is a preview unless an explicit apply mode is used.
The helper requires `recencyAt`, exact cwd, section-based pin state, runtime
status, loaded-thread state, source/ephemeral metadata, ancestry filtering,
and pagination. Missing or malformed safety metadata stops or excludes the
affected operation rather than being guessed.

Human previews include sanitized titles, last-used time and age, project path,
archived state, root/descendant relationships, every group outcome, and shared
reason codes. `--details` prints full UUIDs and expanded state.

`--json` emits the shared stable-v1 `session-cleanup/preview` structure defined
under `contract/`. Titles are `null` by default and appear only with
`--include-titles`. Preview JSON is an audit/reporting artifact and cannot
authorize mutation. It is deliberately separate from the private saved-plan
manifest used by `--save-plan` and `--apply-plan`; JSON preview mode cannot be
combined with an apply mode.

Before mutation, cooperating cleanup processes take a per-Codex-home advisory
lock. The helper re-discovers the remaining expected candidate set immediately
before each root command and stops later mutations after a changed state or a
Codex command failure. Partial success is reported explicitly.

The helper starts a standalone app-server for discovery. Its loaded-thread list
cannot prove that an independent Codex/app-server process has not loaded a
thread after discovery. Close other Codex clients before applying cleanup. The
CLI's own ownership/refusal behavior remains an important final safeguard.

The installed 0.165.0 archive/delete APIs accept a thread ID but expose no
conditional state token or atomic compare-and-mutate operation. A session can
still change in the small interval between the last read and the Codex command.
Codex ownership/refusal checks remain the final authority; the helper never
retries with a less safe operation.

## Permanent-delete manifests

Direct permanent deletion with `--action delete --apply` is disabled. Use a
reviewed plan:

1. Preview the exact roots and descendants.
2. Save the preview with `--save-plan`.
3. Review the text output and private JSON file.
4. Apply that file with `--apply-plan`.

The manifest records its format version, creation time, absolute cutoff,
policy, exact cwd, Codex home and CLI version, full candidate/descendant UUIDs,
and all safety-critical metadata. It is created with mode `0600` on POSIX. Its
digest detects accidental editing, but safety does not rely on the digest.
Apply also regenerates the plan from Codex and compares the full effective
candidate set.

Manifests keep the Codex-specific `codex-session-cleanup-plan` format; they do
not use the shared preview schema. A manifest created under older per-session
latest-N semantics is re-evaluated using conversation groups and is refused as
stale if its effective candidate set differs.

Changed membership, a new descendant, recency changes, new pin/load/activity,
project changes, or malformed metadata makes the plan stale. A stale,
`applying`, partially consumed, or consumed plan cannot be replayed; generate
and review a new one. Applying from a different Codex home—or, for current
project plans, a different cwd—is also refused. A Codex update requires a new
preview. Put manifests under an ignored private directory such as `.local/`
because they contain local paths, titles, and full session UUIDs. Plan paths
inside the resolved Codex home are refused;
the helper performs no direct writes there.

Archive remains reversible and may use `--apply` without a saved manifest. It
still performs locking and per-root validation, but it is not persistently
bound to a preview from an earlier command. The skill should show a separate
archive preview before applying it.

## Installation

Clone or copy the repository into the documented user skill location:

```bash
git clone <repository-url> "$HOME/.agents/skills/codex-session-cleanup"
```

The distributable skill requires only:

```text
SKILL.md
scripts/cleanup_sessions.py
```

Run the helper by absolute path while remaining in the project whose sessions
you want to target. Running it from the skill directory would make that
directory the exact current-project scope.

## Examples

Replace `<skill-directory>` with the installed skill directory.

### 1. Current-project archive preview

```bash
python3 <skill-directory>/scripts/cleanup_sessions.py \
  --older-than 7d --project current --action archive
```

### 2. Current-project permanent-delete preview

```bash
python3 <skill-directory>/scripts/cleanup_sessions.py \
  --older-than 30d --project current --action delete
```

This never deletes anything and explains that a saved plan is required.

### 3. Save a deletion manifest

```bash
mkdir -p .local
python3 <skill-directory>/scripts/cleanup_sessions.py \
  --older-than 30d --project current --action delete \
  --details --save-plan .local/cleanup-plan.json
```

### 4. Apply the reviewed manifest

```bash
python3 <skill-directory>/scripts/cleanup_sessions.py \
  --apply-plan .local/cleanup-plan.json
```

No policy flags are accepted with `--apply-plan`; the reviewed manifest is the
policy and candidate authority.

### 5. Keep the most recent N conversations

```bash
python3 <skill-directory>/scripts/cleanup_sessions.py \
  --keep-latest 5 --project current
```

### 6. Combine age and latest-count retention

```bash
python3 <skill-directory>/scripts/cleanup_sessions.py \
  --older-than 7d --keep-latest 5 --project current
```

### 7. Consider all projects

```bash
python3 <skill-directory>/scripts/cleanup_sessions.py \
  --older-than 30d --project all --action archive
```

### 8. Review full UUIDs and all affected descendants

```bash
python3 <skill-directory>/scripts/cleanup_sessions.py \
  --older-than 30d --project current --action delete --details
```

Compact previews also list every affected descendant; `--details` expands IDs
and runtime metadata.

### 9. Obtain machine-readable JSON

```bash
python3 <skill-directory>/scripts/cleanup_sessions.py \
  --older-than 30d --project current --action delete --json
```

The document has `schema: "session-cleanup/preview"` and
`schema_version: 1` from the stable shared contract. It reports selected,
retained, protected, and excluded groups,
their ranks and reason codes, all root/descendant members, Codex capabilities,
and Codex-specific metadata under `extensions.codex`.

Titles are omitted by default. Request them explicitly when needed for local
review:

```bash
python3 <skill-directory>/scripts/cleanup_sessions.py \
  --older-than 30d --project current --action delete \
  --json --include-titles
```

Preview JSON contains identifiers and local project paths even without titles,
so treat it as private. It contains no conversation messages, tool output, or
file content. The private mutation manifest remains a different backend-specific
document created only with `--save-plan`.

### Archive after review

```bash
python3 <skill-directory>/scripts/cleanup_sessions.py \
  --older-than 7d --project current --action archive --apply
```

### Read-only compatibility check

```bash
python3 <skill-directory>/scripts/cleanup_sessions.py --check-compatibility
```

The check validates the generated schema, mutation command shapes, Codex home,
built-in pinned section, and loaded-thread API without mutating sessions.

Operational errors return nonzero. In particular, invalid/replayed manifests
return `2`, stale-plan refusals return `3`, and advisory-lock conflicts return
`4`. A mutation failure or safety stop returns `1`.

## Requirements

- Python 3.10 or newer.
- An installed `codex` executable on `PATH`, or a validated `codex`/`codex.js`
  entry point under `$CODEX_MANAGED_PACKAGE_ROOT/bin`.
- A compatible generated app-server schema.
- UUID-based `codex archive` and `codex delete --force` commands.

The helper trusts the app-server's initialized `codexHome` and verifies an
explicit `CODEX_HOME` override. It never assumes a hard-coded Codex home.

## Development

Run the standard-library suite, including the isolated fake Codex integration:

```bash
python3 -m unittest discover -s tests -v
```

The suite verifies the vendored contract checksums, all 15 shared conformance
fixtures, shared preview schema shape, saved-plan replay/staleness rules, and
isolated fake-Codex mutation behavior.

Validate skill frontmatter with the `quick_validate.py` helper bundled with
Codex's `skill-creator` skill. Tests use fictional UUIDs and temporary state;
they never destructively test personal Codex sessions.

## License

MIT. Copyright (c) 2026 Gares95.

[app-server-docs]: https://developers.openai.com/codex/app-server
