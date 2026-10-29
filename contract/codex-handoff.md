# Handoff: aligning codex-session-cleanup with contract draft 1

Baseline: `codex-session-cleanup` commit `f5fed73`
(`feat/harden-retention-plans`). Read [`SPEC.md`](SPEC.md) first. The
measured gaps are listed in
[`compatibility-matrix.md`](compatibility-matrix.md).

Everything below is a change to *previews and selection*. **Do not weaken**
any existing safeguard: section-based pin detection, schema validation,
fail-closed metadata handling, descendant cascade checks, delete-requires-
saved-plan, manifest staleness and replay protection, locking, per-root
revalidation, and exit codes stay as they are.

## 1. Vendor the contract

Copy this `contract/` directory byte-identically into the Codex repository
(same path), including `SHA256SUMS`, and add `contract/** -text` to
`.gitattributes` so Git never rewrites its line endings. Add a test that
recomputes the SHA-256 of every contract file and compares it with
`SHA256SUMS` (see `ContractIntegrityTests` in this repository). Any contract
change is made in one repository, the checksums regenerated there, and the
directory copied to the other.

## 2. Rank root groups, not individual sessions

Current behaviour (`build_plan`): `ranking_universe` holds in-scope roots
**and** every in-scope descendant individually; `latest_ids` is the first N
of that list; a root is kept if it *or any descendant* is in `latest_ids`.

Required behaviour:

1. Build each tree (root + descendants) exactly as today.
2. **Decide exclusion first**, before any retention check. A tree is excluded
   (not ranked, never a candidate) when:
   - the root is malformed (already separate today);
   - `tree_issues[root.id]` is non-empty (malformed descendants, ancestry
     problems, a descendant under multiple roots, conflicting metadata);
   - in `current` scope, any descendant's `cwd` differs from the current cwd.
3. Group activity = `max(recencyAt)` over root and all descendants.
4. Ranking universe = every in-scope tree that is not excluded, including
   pinned/unsafe ones. Sort by `(-group_activity, root.id)`. Ranks start at 1.
5. Retained if `rank <= keep_latest` or `group_activity >= cutoff`.
6. Protected if the root or any descendant is pinned, ephemeral, loaded,
   active, or in any other non-idle runtime state.
7. Selected otherwise. Precedence: excluded > protected > retained >
   selected.

Effect: "keep latest N" keeps N conversations. It can only keep **more**
sessions than `f5fed73`, never fewer, so existing users lose no protection.
Fixtures 08, 11, and 14 then pass.

Saved plans need no format change: apply already regenerates the plan and
compares candidate sets, so a plan saved under the new rule is validated
under the new rule. Plans saved by `f5fed73` builds will usually turn stale
after the upgrade, which is the correct fail-closed outcome.

## 3. Map categories to statuses and reason codes

| `f5fed73` protection | Contract status | Reason codes |
|---|---|---|
| `retention` (root) | retained | `within-latest`, `newer-than-cutoff` (from the group, §2) |
| `descendant-retention` | retained | same, computed on the group |
| `pinned`, `pinned-descendant` | protected | `pinned` |
| `unsafe` / `unsafe-descendant`, status `active` or loaded | protected | `running` |
| `unsafe` / `unsafe-descendant`, ephemeral | protected | `ephemeral` |
| `unsafe` / `unsafe-descendant`, any other non-idle status | protected | `unsafe-runtime-state` |
| `malformed` (root parse failure) | excluded | `metadata-malformed` |
| `malformed-descendant`, parse failure | excluded | `metadata-malformed` |
| `malformed-descendant`, missing parent / cycle / multiple roots / root in own descendants | excluded | `ancestry-ambiguous` |
| `malformed-descendant`, duplicate or conflicting records | excluded | `duplicate-session-id` |
| `project-boundary` | excluded | `related-outside-scope` |
| candidate | selected | `older-than`, `outside-latest` |

Protected groups also list retention reasons that apply (SPEC §2.4).

## 4. Emit the shared preview document from `--json`

Today `--json` prints the plan document (`codex-session-cleanup-plan`, the
manifest format). Change `--json` to print the preview document defined by
[`preview-v1.schema.json`](preview-v1.schema.json). Keep the manifest format
for `--save-plan` / `--apply-plan` unchanged; previews and manifests are
separate contracts. (`--json` has not been released on `main`, so no
consumer migration is needed.)

Add `--include-titles`: preview JSON titles are `null` unless it is given.
Manifests keep titles as they do today.

| Preview field | Codex source |
|---|---|
| `agent` | `"codex"` |
| `agent_version` | installed CLI version string |
| `tool` | `{"name": "codex-session-cleanup", "version": …}` |
| `evaluated_at` | `plan.now` |
| `scope` | `{"mode": policy.project, "path": current_cwd or null}` |
| `policy.cutoff` | `effective_cutoff` |
| `policy.min_idle_seconds` | `null` |
| `policy.requested_action` | `--action` (`archive` or `delete`) |
| `activity_timestamp` | `{"source": "codex-app-server:recencyAt", "kind": "agent-reported", "resolution": "1 second", "conservative": false}` |
| `capabilities.actions.archive` | `supported: true`, `agent-native`, `requires_saved_plan: false` |
| `capabilities.actions.delete` | `supported: true`, `irreversible`, `requires_saved_plan: true` |
| `capabilities.actions.quarantine` | `supported: false`, `tool-managed`, `unsupported_reason: "not-offered-by-backend"` |
| `capabilities.sizes / titles / pinning` | `false` / `true` / `true` |
| `capabilities.active_session_detection` | `"agent-reported"` |
| `groups[].group_id` | root thread id |
| `groups[].proposed_action` | requested action when selected, else `null` |
| `groups[].bytes`, `sessions[].bytes`, `totals.bytes.*` | `null` |
| `sessions[].relation` | `root` or `descendant` |
| `sessions[].parent_session_id` | `parentThreadId` |
| `sessions[].project_path` / `project_identity` | `cwd` / `verified` |
| `sessions[].created_at` / `last_activity` | `createdAt` / `recencyAt` (ISO 8601, `Z`) |
| `sessions[].last_activity_source` | `"recencyAt"` |
| `sessions[].title_source` | `"name"` or `"preview"` |
| `extensions.codex` (top) | `codex_home`, discovery notes (`use_state_db_only`, archived scope) |
| `extensions.codex` (session) | `session_id` (Codex `sessionId`), `archived`, `status`, `active_flags`, `loaded`, `ephemeral`, `source_kind` |
| `warnings` | compatibility notes, e.g. `{"code": "standalone-app-server-load-visibility", "subject": null}` |

Order groups by status (`selected`, `retained`, `protected`, `excluded`),
then group activity descending, then `group_id`; members root first, then
depth, activity descending, id (Codex already orders descendants this way).

## 5. Conformance test

Add a test that loads `contract/fixtures/*.json` and checks status, rank, and
`reasons_include` for every case (Codex supports pinning, so none are
skipped). Adapter rules are in [`fixtures/README.md`](fixtures/README.md).
A minimal adapter for `build_plan` (status-only; extend it to ranks and
reason codes once §2–§3 exist):

```python
def run_case(case, cleanup):
    now = parse_iso(case["evaluated_at"])
    ids = sorted(s["id"] for s in case["sessions"])
    to_uuid = {i: str(uuid.UUID(int=n + 1)) for n, i in enumerate(ids)}  # order-preserving
    def session(s):
        t = now - dt.timedelta(hours=s["hours_ago"])
        return cleanup.Session(
            id=to_uuid[s["id"]], title="", created_at=t, updated_at=t, recency_at=t,
            cwd=s["project"], pinned=s["state"] == "pinned", ephemeral=False,
            status="active" if s["state"] == "running" else "idle", active_flags=(),
            archived=False, parent_id=to_uuid[s["parent"]] if s["parent"] else None,
            session_id=to_uuid[s["id"]], source_kind="cli")
    # descendant_lookup: walk children by "parent"; malformed descendants
    # become UnsafeRecord issues, malformed roots go into `malformed`.
    ...
```

Also validate the `--json` output of a fake-Codex run against
`preview-v1.schema.json`.

## 6. Documentation

- README/SKILL: describe latest N as "N most recently active conversations
  (root threads with their descendants)", and use the contract's terms
  (selected, retained, protected, excluded) in text output.
- Keep the archive/delete wording: archive is Codex-native and reversible by
  Codex; delete is irreversible and needs a reviewed saved plan. Never call
  either "quarantine".

## 7. Acceptance

- All 15 fixtures pass (status, rank, reasons).
- `--json` validates against `preview-v1.schema.json`.
- Existing tests pass, with only these expected changes: tests that assert a
  descendant consumes a latest-N slot individually now assert group ranking;
  tests that assert `project-boundary` / `malformed-descendant` as
  "protected" now assert "excluded".
- Manifest format, exit codes, and apply safeguards unchanged.
