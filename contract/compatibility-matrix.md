# Compatibility matrix

Contract draft 1 against each backend as of 2026-10-26.

- Claude Code backend: `claude-session-cleanup` working tree (v0.1.0 +
  contract alignment), Claude Code 2.1.310 storage layout.
- Codex backend: `codex-session-cleanup` commit `f5fed73`
  (`feat/harden-retention-plans`), Codex CLI 0.164.0.

Legend: ✅ conforms · ⚠️ differs (handoff item) · ➖ not applicable.

## Conformance fixtures (status level)

Codex results were measured by running the fixtures through the Codex
`build_plan` of commit `f5fed73` with a test adapter (descendants via
`parentThreadId`, `running` as runtime status `active`, malformed roots as
malformed records, malformed descendants as tree issues). Codex does not
expose ranks, so only statuses were compared.

| Case | Claude Code | Codex `f5fed73` | Codex difference |
|---|---|---|---|
| 01 age-only | ✅ | ✅ | |
| 02 keep-latest | ✅ | ✅ | |
| 03 combined-old-within-latest | ✅ | ✅ | |
| 04 combined-new-outside-latest | ✅ | ✅ | |
| 05 exact-project-boundary | ✅ | ✅ | |
| 06 all-projects | ✅ | ✅ | |
| 07 old-root-recent-descendant | ✅ | ✅ | |
| 08 latest-n-counts-groups | ✅ | ⚠️ | Descendants occupy individual latest-N slots, so `b` is selected instead of retained |
| 09 protected-descendant | ✅ | ✅ | |
| 10 protected-occupies-rank | ✅ | ✅ | |
| 11 related-outside-scope | ✅ | ⚠️ | Boundary-crossing tree is ranked (and reported as retained/protected), so `b` is selected |
| 12 equal-activity-tie-break | ✅ | ✅ | |
| 13 malformed-root-excluded | ✅ | ✅ | |
| 14 malformed-descendant-excluded | ✅ | ⚠️ | Retention is evaluated before tree issues, so the malformed tree is ranked and `b` is selected |
| 15 pinned-group-protected | ➖ skipped (no pinning) | ✅ | |

All three Codex differences are in the same direction: Codex selects a group
the contract keeps. None makes the contract select something Codex keeps.

## Contract elements

| Element | Claude Code | Codex `f5fed73` | Target |
|---|---|---|---|
| Retention unit | ✅ root group (origin + continuations; subagents are data) | ✅ root + `parentThreadId` descendants (cascade set) | root group |
| Group last activity = max over members | ✅ | ✅ for age (any recent descendant blocks); ⚠️ not used for ranking | max |
| Latest-N ranks groups | ✅ | ⚠️ ranks individual sessions | groups |
| Protected groups occupy ranks | ✅ | ✅ | occupy |
| Excluded groups unranked | ✅ | ⚠️ malformed-descendant and boundary-crossing trees are ranked | unranked |
| Strict cutoff, computed once | ✅ | ✅ (`absoluteCutoff`) | |
| Tie-break by root id | ✅ | ✅ (by session id) | root id |
| Exact current-directory scope | ✅ realpath + NFC, verified against recorded cwd | ✅ physical cwd from `os.getcwd()`, server-side exact filter | exact physical path |
| Explicit `all` | ✅ | ✅ | |
| Boundary-crossing group | ✅ excluded `related-outside-scope` | ⚠️ protected (`project-boundary`) | excluded |
| Four statuses | ✅ | ⚠️ candidates / protected / malformed | selected, retained, protected, excluded |
| Reason codes (§3.3) | ✅ | ⚠️ free-text reasons + categories | registry codes |
| Preview JSON v1 | ✅ | ⚠️ `--json` prints the plan/manifest document (`codex-session-cleanup-plan` v1, camelCase) | separate preview document |
| Titles opt-in in preview JSON | ✅ `--include-titles` | ⚠️ always included | opt-in |
| Saved-plan manifests | ➖ none (read-only) | ✅ keep as is | backend-specific |

## Backend differences that remain by design

| Aspect | Claude Code | Codex |
|---|---|---|
| Activity source | Derived: max of transcript mtime, artifact mtimes, message timestamps; conservative | Agent-reported `recencyAt`, 1 s resolution |
| Discovery coverage | Every transcript file under `projects/` | Sessions indexed in the Codex state DB (`useStateDbOnly`); unindexed rollouts are not seen |
| Pinning | None in Claude Code | Built-in `Pinned` section |
| Active-session detection | `partial`: registry + PID start time (Linux), background records, invoking session, minimum-idle guard | `agent-reported`: runtime status and loaded-thread list (standalone app-server cannot see other servers' loads) |
| Sizes | Measured, per artifact kind | Not available (`null`) |
| Archive | Unsupported (Claude Code has none) | Native, reversible by Codex |
| Quarantine | Planned; unsupported in v0.1 (`read-only-release`) | Unsupported (not needed) |
| Permanent delete | Unsupported | Supported via reviewed saved plan only |
| Archived sessions | ➖ | Archive policies see non-archived sessions; delete policies see both |
| Extra protections | `invoking-session`, `possibly-running`, `background-session`, `recently-active` | `pinned`, `ephemeral`, `unsafe-runtime-state` |
| Native retention | Claude Code's `cleanupPeriodDays` sweep reported as an extension | None known |
