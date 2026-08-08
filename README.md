# codex-session-cleanup

`codex-session-cleanup` is a small Codex skill for retaining recent local
session history and safely archiving or deleting older sessions. It exists to
make routine cleanup predictable without turning Codex's session storage into
a user-managed filesystem.

The helper decides which logical sessions match a retention policy. The
installed `codex` command performs every archive or deletion.

## Supported policies

- Select sessions last used before an age cutoff: `--older-than 48h` or
  `--older-than 7d`.
- Preserve the N most recently used sessions: `--keep-latest 5`.
- Combine both rules with intersection semantics: only sessions older than the
  cutoff and outside the latest N become candidates.
- Restrict matching to the exact invoking cwd with `--project current`, the
  default.
- Consider sessions across projects only with `--project all`.
- Archive by default, or permanently delete only with an explicit
  `--action delete`.

At least one retention selector is required. Counts must be positive whole
numbers, and durations accept only positive whole hours or days.

## Safety model

Every normal invocation is a preview. It discovers ordinary saved interactive
sessions through the installed Codex app-server and uses `recencyAt` as “last
used.” It never substitutes rollout creation time or filesystem timestamps.

Before selecting roots, the helper protects pinned, ephemeral, active, errored,
or malformed sessions. It enumerates spawned descendants, blocks roots with a
pinned or active descendant, and prevents exact-project cleanup from cascading
outside the selected cwd. Apply mode performs a second discovery immediately
before mutation and aborts on stale candidates or safety metadata.

The helper generates the installed app-server schema at runtime and verifies
the required fields. If the installed Codex does not expose pin state,
`recencyAt`, runtime status, exact cwd, or descendant filtering, cleanup fails
closed. There is no unsafe compatibility fallback.

The script does not remove or move rollout files and does not edit Codex
databases, indexes, configuration, authentication, logs, skills, plugins,
memories, or other state. It invokes exact UUID argument arrays with:

```text
codex archive <UUID>
codex delete <UUID> --force
```

Codex remains responsible for session files, metadata, descendant cascades,
and ownership protections.

## Installation

Clone or copy the repository directly into the documented user skill location:

```bash
git clone <repository-url> "$HOME/.agents/skills/codex-session-cleanup"
```

Restart Codex if the skill does not appear automatically. Do not replace
`<repository-url>` until the repository has been published.

The distributable skill needs only:

```text
SKILL.md
scripts/cleanup_sessions.py
```

## Usage

Ask Codex naturally after installing the skill:

```text
Clean Codex sessions older than 48 hours.
Keep my 5 most recently used Codex sessions and archive the rest.
Archive sessions older than 7 days for the current project.
Show me what would be removed if I keep only sessions from the last 3 days.
Keep my latest 10 sessions across all projects and archive older ones.
Delete sessions older than 30 days permanently.
```

The skill translates the request to the narrow helper interface. You can also
run the helper directly from the installed skill directory:

```bash
python3 scripts/cleanup_sessions.py --older-than 48h
python3 scripts/cleanup_sessions.py --keep-latest 5 --project all
python3 scripts/cleanup_sessions.py --older-than 7d --keep-latest 5
python3 scripts/cleanup_sessions.py --older-than 30d --action delete
```

These commands are read-only previews. A preview ends with `No changes made.`
Use the same policy with `--apply` only after reviewing it:

```bash
python3 scripts/cleanup_sessions.py \
  --older-than 7d --keep-latest 5 --project current --action archive --apply
```

Archive is reversible through Codex. Delete is irreversible and requires both
`--action delete` and `--apply`; the skill uses delete only when permanent
deletion was explicitly requested.

## Requirements and compatibility

- Python 3.10 or newer, using only the standard library.
- An installed `codex` executable on `PATH`.
- A Codex app-server schema that exposes pagination, `recencyAt`, exact cwd,
  pin and runtime state, source/ephemeral metadata, and descendant filtering.
- UUID-based `codex archive` and forced `codex delete` commands.

The helper inherits the runtime environment and trusts the app-server's
initialized `codexHome`, verifying an explicit `CODEX_HOME` override when one
is present. It does not assume that Codex state lives at a hard-coded path.

Run a preview after installing or updating Codex to verify compatibility. A
safety-contract error means no changes were made and the installed version is
not suitable for this helper.

## Development

Run the synthetic standard-library test suite without accessing real sessions:

```bash
python3 -m unittest discover -s tests -v
```

Validate the skill frontmatter with the `quick_validate.py` helper bundled with
Codex's `skill-creator` skill when available. No package manager, dependency
installation, database, daemon, or build step is required.

## License

MIT. Copyright (c) 2026 Gares95.
