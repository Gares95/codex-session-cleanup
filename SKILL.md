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
- Map “keep my latest 5 sessions” to `--keep-latest 5`.
- Combine both selectors when requested. They use intersection semantics, so a
  session must be older than the cutoff and outside latest N.
- Use `--project current` unless the user explicitly requests all projects.
  Current means an exact match with the directory where the helper is run.
- Use `--project all` only for wording such as “across all projects.”
- Default ambiguous “clean” or “remove old sessions” wording to
  `--action archive`.
- Use `--action delete` only when irreversible deletion is explicit. Ask before
  proceeding if permanent intent is genuinely ambiguous.
- Require at least one retention selector. For “clean up Codex,” ask for an age
  or latest-count policy and explain that this skill affects session history
  only.

Latest N ranks ordinary interactive roots and all discoverable spawned
descendants within the selected project scope. Descendants consume latest-N
positions. An otherwise eligible root is protected if any descendant must be
retained, is pinned/loaded/active/unsafe, has malformed ancestry, or crosses an
exact current-project boundary.

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
when structured output is requested. Summarize selected roots, every cascading
descendant, and protection/exclusion reasons. Confirm that no changes were
made. For delete previews, call out the irreversible warning.

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
