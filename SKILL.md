---
name: codex-session-cleanup
description: Safely preview, archive, or permanently delete old local Codex session history using age, latest-count, and exact current-project retention rules. Use for requests to clean Codex sessions older than a duration, keep the most recently used sessions, archive old sessions, delete explicitly identified old session history, or show what a retention policy would affect. Do not use for logs, configuration, authentication, memories, skills, plugins, worktrees, or general Codex cleanup.
---

# Codex Session Cleanup

Use `scripts/cleanup_sessions.py` to turn a narrow retention request into a
deterministic preview. Let the helper select logical session UUIDs and let the
installed `codex` command perform every archive or deletion. Never edit Codex
rollout files, databases, indexes, or other state directly.

## Interpret the request

Use only the helper's narrow policy arguments:

- Map “older than 48 hours” to `--older-than 48h`; durations are positive whole
  hours or days such as `24h`, `7d`, and `30d`.
- Map “keep my latest 5 sessions” to `--keep-latest 5`, meaning five root
  conversations rather than five individual thread records.
- Combine both selectors when requested. They use intersection semantics, so a
  conversation must be older than the cutoff and outside latest N.
- Use `--project current` unless the user explicitly requests all projects.
  Current means an exact match with the directory where the helper is run. A
  conversation enters scope when any member matches, but a related member in
  another cwd excludes the whole conversation.
- Use `--project all` only for wording such as “across all projects.”
- Default ambiguous “clean” or “remove old sessions” wording to
  `--action archive`.
- Use `--action delete` only when irreversible deletion is explicit. Ask before
  proceeding if permanent intent is genuinely ambiguous.
- Require at least one retention selector. For “clean up Codex,” ask for an age
  or latest-count policy and explain that this skill affects session history
  only.

One conversation is an ordinary interactive root plus all discoverable spawned
descendants affected by Codex's cascade. Its activity is the newest
`recencyAt` among those members. Each conversation consumes one latest-N slot,
not one slot per descendant. Protected conversations still rank; excluded
conversations do not. Equal activity is ordered by root UUID.

Use the helper's four outcomes exactly:

- `selected` is eligible for the requested action;
- `retained` is kept by the age/latest-count policy;
- `protected` is kept by a safety rule such as pinning or runtime activity;
- `excluded` cannot safely participate because its scope, metadata, or group
  identity is uncertain.

Exclusion happens before ranking. A root or descendant that is pinned, loaded,
active, ephemeral, or otherwise unsafe protects the entire conversation.
Malformed or ambiguous ancestry and descendants outside the exact current cwd
exclude the entire conversation. Never guess around an exclusion.

Do not reinterpret a request as permission to alter logs, configuration,
authentication, worktrees, memories, skills, plugins, or other Codex state.

## Preview first

Resolve this `SKILL.md` file's directory and run its bundled helper with
`python3`. Omit every apply option for the initial preview:

```bash
python3 <skill-directory>/scripts/cleanup_sessions.py \
  --older-than 7d --keep-latest 5 --project current --action archive
```

Use `--details` when the user needs full UUIDs and expanded state. Use `--json`
when shared structured preview output is requested. JSON titles are `null`
unless the user explicitly asks for identifying titles; only then add
`--include-titles`. Summarize selected, retained, protected, and excluded
conversation groups, every cascading descendant, and reason codes. Confirm
that no changes were made. For delete previews, call out the irreversible
warning.

If compatibility or metadata validation fails, stop. Do not fall back to
rollout paths, filesystem timestamps, direct database reads, or raw deletion.
Treat the unique built-in `Pinned` thread section as the pin authority and
preserve any fail-closed result involving sections, runtime state, ancestry,
pagination, or `recencyAt`.

## Apply an archive

Archive is reversible. After showing a separate preview, add `--apply` only
when the user asks to perform it, preserving the exact policy and cwd:

```bash
python3 <skill-directory>/scripts/cleanup_sessions.py \
  --older-than 7d --keep-latest 5 --project current --action archive --apply
```

The helper locks cooperating cleanup runs and refreshes the remaining state
before each root mutation. Archive apply is not persistently bound to an
earlier command, so never skip the preview.

## Apply permanent deletion through a reviewed plan

Never use `--action delete --apply`; the helper refuses that unbound path.
When permanent deletion is explicit:

1. Save the exact preview to a private manifest, preferably under an ignored
   local directory:

   ```bash
   mkdir -p .local
   python3 <skill-directory>/scripts/cleanup_sessions.py \
     --older-than 30d --project current --action delete --details \
     --save-plan .local/cleanup-plan.json
   ```

2. Show the preview and tell the user the manifest contains full session IDs,
   titles, and local paths. Do not apply it until the user has reviewed the
   selected roots and all descendants and explicitly asks to continue.
3. Apply only that manifest, from the same exact cwd:

   ```bash
   python3 <skill-directory>/scripts/cleanup_sessions.py \
     --apply-plan .local/cleanup-plan.json
   ```

Do not add policy flags to `--apply-plan`. The helper revalidates the installed
contract and full effective candidate set. If membership, descendants,
recency, pin/load/activity, project scope, or other safety state changed, it
marks/refuses the stale plan. Never edit or reuse an applying, stale, partially
consumed, or consumed plan; generate and review a new preview.

The JSON preview schema is not the deletion manifest and never authorizes
mutation. Do not attempt to apply JSON captured from `--json`; only a private
manifest created by `--save-plan` is accepted by `--apply-plan`.

## Report the outcome

After a preview, identify what would happen and say no changes were made. After
apply, distinguish successful, skipped, and failed root targets, state whether
the action was archive or irreversible deletion, and report partial completion
accurately. If Codex refuses a target because another process owns or uses it,
preserve the refusal and do not retry through a less safe operation.

Codex does not currently expose a conditional atomic archive/delete operation.
Explain when relevant that a small race remains between the final read and the
Codex command. The helper's standalone app-server also cannot prove that a
different app-server process has not loaded a thread. Recommend closing other
Codex clients before apply; Codex's own ownership checks are the final
authority.

Discovery intentionally uses `thread/list` with `useStateDbOnly: true` so a
preview does not scan rollout files and repair Codex metadata. Unindexed
history may therefore be omitted. Do not switch to scan-and-repair discovery
silently. The tool offers Codex-native archive and irreversible delete; it does
not offer or emulate a separate quarantine action.
