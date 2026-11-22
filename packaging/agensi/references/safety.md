# Safety and permissions

`codex-session-cleanup` selects logical Codex conversation groups. The
installed Codex CLI remains responsible for native archive and permanent
delete operations, including descendant cascades.

## Safety architecture

- Normal invocations are read-only previews.
- Archive is explicit, Codex-native, and reversible through Codex.
- Permanent delete is irreversible and requires a saved plan, human review,
  explicit confirmation, and `--apply-plan`.
- Ordinary `--action delete --apply` is refused.
- Exact UUIDs are passed to Codex with subprocess argument arrays. The helper
  does not use a shell command string.
- The helper never removes rollout files, edits SQLite databases or indexes,
  or falls back to direct mutation when Codex refuses.
- Pinned, active, loaded, ephemeral, malformed, and otherwise unsafe
  conversations fail closed.
- Descendants are evaluated as part of their root conversation. A recent or
  unsafe descendant keeps the entire conversation from being mutated.
- Saved plans are private, single-use state. Membership, descendants, recency,
  pin/load/activity, scope, Codex version, and Codex home are revalidated.
- Cooperating cleanup processes use an advisory lock. State is re-read as
  close as practical to each native command.

## Permissions

### Terminal / Shell

Required to run the temporary Python script and the installed `codex`
executable. The helper launches only constrained subprocess argument arrays:

- `codex --version`;
- `codex app-server generate-json-schema ...`;
- `codex archive --help` and `codex delete --help`;
- `codex app-server --stdio`;
- `codex archive <UUID>`; and
- `codex delete <UUID> --force`.

It does not use `os.system`, `shell=True`, downloads, package installation, or
network bootstrap commands.

### Read Files

The helper reads only files the user explicitly names as saved deletion plans.
Codex exposes session metadata through its supported app-server interface and
owns reads of its internal persistence.

### Write Files

The helper writes a saved deletion plan only when `--save-plan PATH` is
explicitly requested, updates that plan's lifecycle state during apply, and
creates a small advisory lock in the operating system's temporary directory.
Plan paths inside Codex home are refused. Codex—not this helper—performs
session archive/delete state mutation.

### Environment Variables

- `CODEX_HOME`: optional explicit Codex home. It is verified against the home
  returned by the app-server.
- `CODEX_MANAGED_PACKAGE_ROOT`: optional location of a managed Codex CLI entry
  point when `codex` is not on `PATH`.
- Normal process environment such as `PATH`, `HOME`, and platform temporary
  directory variables may be used by Python and subprocess resolution.

### Network and services

The packaged helper makes no network calls and needs no browser, API key,
cloud account, or external service. The installed local Codex CLI is a runtime
dependency, not a cloud integration declared by this package.

## Compatibility

The package does not hard-code support from a version string. Every invocation
generates the installed Codex app-server schema and checks the exact fields and
commands needed for safe discovery and mutation. Unsupported interfaces stop
the operation. Codex CLI 0.169.0 is a tested version, not a permanent guarantee.

Requires Python 3.10+ and local access to the user's Codex installation and
session state. A cloud sandbox without those resources cannot use the helper.
Another local compatible agent may invoke it; the data being managed remains
Codex session history.

## Known limitations

- Discovery uses `thread/list` with `useStateDbOnly: true`; unindexed rollout
  history may be omitted. Normal scan-and-repair listing is intentionally not
  used because preview must remain read-only.
- The standalone app-server's loaded-thread list cannot prove that a different
  Codex process has not loaded a thread after discovery. Close other Codex
  clients before apply.
- Codex exposes no atomic conditional archive/delete command. A small race
  remains between the last validation read and the native command. Codex's own
  ownership/refusal behavior is the final safeguard.
- Malformed or incomplete safety metadata is excluded or stops the operation;
  the helper never substitutes filesystem timestamps or guesses.
- Preview JSON contains logical identifiers and project paths. Titles are null
  by default, but saved plans contain full identifiers, titles, and local paths
  and must be treated as private.

## Open source and independence

Canonical MIT-licensed source, tests, and documentation:
<https://github.com/Gares95/codex-session-cleanup>

This is an independent open-source project and is not affiliated with or
endorsed by OpenAI.
