# Agensi package

The [Agensi](https://www.agensi.io) distribution is a deterministic package
of this repository's existing skill. The repository remains the canonical,
MIT-licensed source; the package adds no cleanup features and maintains no
second implementation.

## Architecture

Agensi writes each packaged script unchanged to a temporary file and executes
it independently. The canonical `scripts/cleanup_sessions.py` is already a
standalone Python 3.10+ standard-library program. It has no relative imports,
repository-relative runtime files, contract/schema file dependency, or script
directory dependency. The package therefore copies it byte-for-byte instead
of generating or transforming source.

The helper may use only the installed Codex executable and its generated
app-server schema, the resolved Codex home, the caller's current working
directory, and plan paths explicitly supplied by the user. In particular,
`--project current` resolves the caller's cwd even when Agensi placed the
script in an unrelated temporary directory.

| Packaged file | Source |
| --- | --- |
| `SKILL.md` | `packaging/agensi/SKILL.md` |
| `scripts/cleanup_sessions.py` | `scripts/cleanup_sessions.py`, byte-identical |
| `references/safety.md` | `packaging/agensi/references/safety.md` |
| `LICENSE` | `LICENSE` |

The standalone helper is about 114 KB. Agensi's suggestion to keep script
logic under roughly 20 KB is a reviewability guideline, not a package limit.
Splitting the helper would make independently executed scripts duplicate or
lose shared discovery, grouping, manifest, and safety logic. Shipping the
audited canonical file unchanged is the smaller and more reviewable design.

## Building

```bash
python3 tools/build_agensi.py
```

This writes the ignored
`dist/agensi/codex-session-cleanup-1.0.0.zip` and prints its size, SHA-256,
and contents. The build sorts file names, fixes ZIP timestamps and modes,
uses LF-only UTF-8 text, and omits directory entries, symlinks, ownership
metadata, and host-specific extra fields. Identical sources produce identical
ZIP bytes across supported platforms.

The build also parses the canonical source and refuses unreviewed imports,
relative imports, runtime `__file__` dependencies, network modules, dynamic
code execution, `os.system`, `shell=`, encoded-looking source, and other
unreviewed process-launch constructs. A new canonical import must be reviewed
and explicitly allowlisted before packaging succeeds.

## Testing

```bash
python3 -m unittest discover -s tests -v
python3 tools/test_agensi_bundle.py
```

The normal suite includes package layout, deterministic-build, isolated-cwd,
privacy, and synthetic fake-Codex integration tests. It exercises preview,
archive, reviewed-plan deletion, stale/replayed plans, descendant changes,
pin/load/activity protection, compatibility failures, advisory locking,
partial native failures, and shared JSON shape without touching personal
Codex sessions.

The second command extracts the ZIP, sets the extracted helper as the test
entry point, and reruns the canonical cleanup suite against those exact bytes.
CI performs both checks on Linux, macOS, and Windows with Python 3.10 through
3.13.

## Marketplace and security review

The package needs Terminal / Shell, Read Files, Write Files, and Environment
Variables permissions. It makes no network calls and needs no browser,
download, package installation, API key, or cloud account. See packaged
`references/safety.md` for the exact permission rationale.

Native Codex subprocesses are intentional and constrained to argument arrays
for compatibility discovery, app-server metadata access, UUID-based archive,
and UUID-based forced delete after reviewed-plan validation. The helper never
uses shell interpolation or directly edits Codex rollout files, databases, or
indexes.

Agensi submission is a separate manual release step. Building this artifact
does not upload it, create a marketplace listing, change the GitHub release,
or configure payouts. The proposed listing is free and discloses the canonical
open-source repository and independence from OpenAI.

## Requirements reviewed

The packaging design follows Agensi's current public guidance:

- [Package a Skill](https://www.agensi.io/docs/packaging)
- [Submit on the Website](https://www.agensi.io/docs/submit-on-the-website)
- [Security](https://www.agensi.io/docs/security)
- [Review and Approval](https://www.agensi.io/docs/review-and-approval)

Before an eventual upload, re-check those pages for changes, build from the
intended release commit, run both test commands, inspect the extracted files,
and verify the printed SHA-256.
