# Compatibility matrix

Contract version 1 (stable) against each backend, measured 2026-10-29.

| Backend | Revision measured | Agent version |
|---|---|---|
| Claude Code (`claude-session-cleanup`) | `main`: implementation at `d615842`, plus the v1 conformance-test update (skips derived from reported capabilities) | storage layout of Claude Code 2.1.310 |
| Codex (`codex-session-cleanup`) | `feat/align-shared-cleanup-contract` at `9d3552e` | Codex CLI 0.164.0 |

Legend: ✅ pass · ⏭️ skipped because a required capability is unsupported
and reported as such (SPEC §6) · ❌ semantic failure.

## Summary

| | Claude Code | Codex |
|---|---|---|
| Fixtures passed | 14 | 15 |
| Skipped (unsupported capability) | 1 (`pinning`) | 0 |
| Semantic failures | **0** | **0** |
| Preview validates against `preview-v1.schema.json` | ✅ | ✅ |
| Contract files match `SHA256SUMS` | ✅ | ✅ |
| Conforms to version 1 | ✅ | ✅ |

How each result was measured:

- **Claude Code:** `tests/test_conformance.py` runs every fixture through
  the real grouping and policy code and checks status, rank, and
  `reasons_include`; `tests/test_cli.py` validates generated previews against
  the schema; the contract integrity test recomputes `SHA256SUMS`. Full suite:
  115 tests, 1 skip (the pinning fixture).
- **Codex:** `ContractConformanceTests` asserts that all 15 fixtures are
  present and checks status, rank, and `reasons_include` for each, with no
  skip path; an isolated fake-Codex run validates `--json` against the
  schema; `ContractIntegrityTests` verifies `SHA256SUMS`. Full suite: 72
  tests, all passing.

## The pinning skip

Fixture `15-pinned-group-protected` requires `pinning`. Claude Code has no
pin, favourite, or equivalent marker that this tool could read, so
`claude-session-cleanup` reports `capabilities.pinning: false` in every
preview and its conformance test derives the skip from that reported value.
This is a capability gap, not a semantic deviation: every other protection,
retention, and exclusion rule applies unchanged. If Claude Code gains pinning
and the backend starts reporting `pinning: true`, the fixture must then pass.

## Conformance fixtures

| Case | Claude Code | Codex |
|---|---|---|
| 01 age-only | ✅ | ✅ |
| 02 keep-latest | ✅ | ✅ |
| 03 combined-old-within-latest | ✅ | ✅ |
| 04 combined-new-outside-latest | ✅ | ✅ |
| 05 exact-project-boundary | ✅ | ✅ |
| 06 all-projects | ✅ | ✅ |
| 07 old-root-recent-descendant | ✅ | ✅ |
| 08 latest-n-counts-groups | ✅ | ✅ |
| 09 protected-descendant | ✅ | ✅ |
| 10 protected-occupies-rank | ✅ | ✅ |
| 11 related-outside-scope | ✅ | ✅ |
| 12 equal-activity-tie-break | ✅ | ✅ |
| 13 malformed-root-excluded | ✅ | ✅ |
| 14 malformed-descendant-excluded | ✅ | ✅ |
| 15 pinned-group-protected | ⏭️ `pinning` unsupported | ✅ |

## Contract elements

| Element | Claude Code | Codex |
|---|---|---|
| Retention unit | ✅ root group: origin + continuations; subagents are data | ✅ root + `parentThreadId` descendants (the cascade set) |
| Group last activity = max over members | ✅ | ✅ |
| Latest-N ranks root groups; each counts once | ✅ | ✅ |
| Protected groups occupy ranks; excluded groups do not | ✅ | ✅ |
| Strict cutoff computed once | ✅ | ✅ |
| Tie-break by root id | ✅ | ✅ |
| Exact current-directory scope; group enters scope when any member matches | ✅ | ✅ |
| Boundary-crossing group excluded (`related-outside-scope`) | ✅ | ✅ |
| Four statuses and precedence | ✅ | ✅ |
| Reason-code registry (§3.3) | ✅ | ✅ |
| Preview JSON v1, titles opt-in (`--include-titles`) | ✅ | ✅ |
| Preview kept separate from saved-plan manifests | ➖ no manifests (read-only) | ✅ manifests unchanged |

## Backend differences that remain by design

These are reported through `capabilities`, `activity_timestamp`, and
`extensions`; none changes a shared rule.

| Aspect | Claude Code | Codex |
|---|---|---|
| Activity source | `derived`: max of transcript mtime, artifact mtimes, message timestamps; conservative | `agent-reported`: `recencyAt`, 1 s resolution |
| Discovery coverage | Every transcript file under `projects/` | Sessions indexed in the Codex state DB; unindexed rollouts are not seen |
| Pinning | Unsupported (`pinning: false`) | Built-in `Pinned` section |
| Active-session detection | `partial`: registry + PID start time (Linux), background records, invoking session, minimum-idle guard | `agent-reported`: runtime status and loaded-thread list (a standalone app-server cannot see other servers' loads) |
| Sizes | Measured, per artifact kind | Not available (`null`) |
| Archive | Unsupported (Claude Code has none) | Native, reversible by Codex |
| Quarantine | Unsupported (`read-only-release`) | Unsupported (`not-offered-by-backend`) |
| Permanent delete | Unsupported | Supported through a reviewed saved plan only |
| Archived sessions | ➖ | Archive policies see non-archived sessions; delete policies see both |
| Backend-only protections | `invoking-session`, `possibly-running`, `background-session`, `recently-active` | `pinned`, `ephemeral`, `unsafe-runtime-state` |
| Native retention | Claude Code's `cleanupPeriodDays` sweep reported in `extensions.claude_code` | None known |

## History

- **Draft 1 (2026-10-26).** Codex `f5fed73` measured 12/15. All three
  differences selected groups the contract keeps: descendants consumed
  individual latest-N slots (08), and boundary-crossing (11) and
  malformed-descendant (14) trees were ranked. Resolved by
  [`codex-handoff.md`](codex-handoff.md) in Codex commits `5635693` and
  `9d3552e`.
- **Version 1 (2026-10-29).** Promoted to stable after both backends reached
  conformance. The only change from draft 1 is clarifying wording (status,
  §6 Conformance, capability-dependent skips); no rule, field, schema, or
  fixture changed.
