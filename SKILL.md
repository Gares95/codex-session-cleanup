---
name: codex-session-cleanup
description: Safely preview, archive, or permanently delete old local Codex session history using age, latest-count, and exact current-project retention rules. Use for requests to clean Codex sessions older than a duration, keep the most recently used sessions, archive old sessions, delete explicitly identified old session history, or show what a retention policy would affect. Do not use for logs, configuration, authentication, memories, skills, plugins, worktrees, or general Codex cleanup.
---

# Codex Session Cleanup

Use `scripts/cleanup_sessions.py` to turn a narrow retention decision into a
deterministic preview or operation. Let the helper discover and select logical
session IDs; let the installed `codex` command perform archive or deletion.

## Interpret the request

Choose only these policy arguments:

- Map “older than 48 hours” to `--older-than 48h`; accept positive whole hours
  or days such as `24h`, `7d`, and `30d`.
- Map “keep my latest 5 sessions” to `--keep-latest 5`.
- Combine both selectors when both constraints were requested. The helper uses
  intersection semantics, so the combined cleanup is narrower.
- Use `--project current` unless the user explicitly requests all projects.
- Use `--project all` only for wording such as “across all projects.”
- Default ambiguous “clean” or “remove old sessions” wording to
  `--action archive`.
- Use `--action delete` only when permanent deletion is explicit. Ask before
  proceeding if irreversible intent is genuinely ambiguous.
- Require at least one retention selector. For a vague request such as “clean
  up Codex,” ask for an age or latest-count policy and make clear that this
  skill affects stored session history only.

Do not invent additional flags or reinterpret a request as permission to alter
logs, configuration, authentication, worktrees, memories, skills, plugins, or
other Codex state.

## Preview first

Resolve this `SKILL.md` file's directory and run the bundled helper with
`python3`, omitting `--apply`. For example:

```bash
python3 <skill-directory>/scripts/cleanup_sessions.py \
  --older-than 7d --keep-latest 5 --project current --action archive
```

Treat every invocation without `--apply` as a preview. Explain the policy,
protected sessions, root candidates, descendant impact, and the “No changes
made” result. State clearly that a delete preview is irreversible if applied.

If the helper reports that the installed Codex schema lacks required safety
metadata, stop. Recommend updating or using a Codex version that exposes the
required app-server fields. Do not inspect internal databases or fall back to
rollout paths or filesystem timestamps.

## Apply deliberately

Add `--apply` only when the user asked to perform the previewed action. Keep all
other arguments identical. The helper recalculates the plan, refreshes state
immediately before mutation, and aborts if selection or safety state changed.

```bash
python3 <skill-directory>/scripts/cleanup_sessions.py \
  --older-than 7d --keep-latest 5 --project current --action archive --apply
```

For delete, confirm that the user's wording explicitly authorizes permanent
deletion before adding both `--action delete` and `--apply`.

Never bypass pinned, active, malformed-metadata, project-boundary, descendant,
stale-state, or Codex ownership protections. Never mutate rollout files,
SQLite databases, indexes, or any path inside `CODEX_HOME` directly.

## Report the outcome

After a preview, summarize what would happen and say that no changes were made.
After apply, distinguish successful, skipped, and failed root targets; identify
whether the action was archive or irreversible deletion. If Codex refuses a
target because another process owns or uses it, preserve the refusal and report
the Codex error without attempting a workaround.
