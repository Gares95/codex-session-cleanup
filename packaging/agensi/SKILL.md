---
name: codex-session-cleanup
description: Safely preview, reversibly archive, or permanently delete old local Codex session history using age, latest-count, and exact current-project retention rules. Use when the user asks which Codex conversations are old, wants to keep only recent conversations, asks to archive old history, explicitly requests permanent deletion through a reviewed plan, or wants a compatibility check. Do not use for logs, configuration, authentication, memories, skills, plugins, worktrees, or general disk cleanup.
version: 1.0.0
summary: Review local Codex conversations, keep what matters, reversibly archive old history, and permanently delete only through a reviewed saved plan. Read-only by default, fail-closed, Codex-native for mutations, and open source under MIT.
license: MIT
compatibility: Requires Python 3.10+, a compatible local Codex CLI, and local access to the user's Codex session state. The helper validates installed interfaces and fails closed when required capabilities are unavailable.
tags:
  - codex
  - openai-codex
  - session-cleanup
  - retention
  - archive
  - privacy
  - python
keywords:
  - Codex session cleanup
  - archive old Codex conversations
  - delete Codex sessions safely
  - keep latest Codex conversations
  - Codex history retention
use_cases:
  - Preview Codex conversations older than a given age
  - Keep only the latest N Codex conversations for a project
  - Reversibly archive old Codex conversations
  - Audit which conversations are retained, protected, or excluded
  - Permanently delete selected old conversations after reviewing a saved plan
docs_url: https://github.com/Gares95/codex-session-cleanup
---

# Codex Session Cleanup

Review Codex session history, keep what matters, archive what the user may want
back, and permanently delete only through a reviewed plan.

This package ships the canonical open-source
`scripts/cleanup_sessions.py` unchanged. It uses Python's standard library and
delegates archive and deletion to the installed `codex` command using exact
logical session UUIDs. It never edits Codex rollout files, databases, indexes,
or other internal state directly and does not use the network.

The canonical project is free and open source under the MIT License:
<https://github.com/Gares95/codex-session-cleanup>. This marketplace package
offers convenient discovery and delivery, not exclusive access to the source.
This is an independent open-source project and is not affiliated with or
endorsed by OpenAI.

## Run the packaged script

Agensi provides `scripts/cleanup_sessions.py` as text. Write that script
unchanged to a temporary file and run it with Python 3.10 or newer:

```bash
python3 -I /path/to/temporary/cleanup_sessions.py --check-compatibility
```

For retention work, run the temporary script from the project directory the
user means. Script location is irrelevant: `--project current` uses the
calling process's current working directory, not the temporary directory.
Remove the temporary script after the run.

The script requires a compatible local `codex` executable on `PATH`, or a
supported entry point under `$CODEX_MANAGED_PACKAGE_ROOT/bin`. It accepts an
explicit `$CODEX_HOME` and verifies it against the home resolved by the Codex
app-server. It must run where it can access the user's local Codex installation
and state; a cloud sandbox without them cannot perform this work.

Every invocation validates the installed app-server schema, pinning section,
loaded-thread API, and archive/delete command shapes before operating.
`--check-compatibility` performs those checks without evaluating a retention
policy or changing sessions. If compatibility fails, report the safety stop and
do not guess, inspect rollout files directly, or substitute another operation.

## Interpret the request

Use only these policy arguments:

- “older than 48 hours / 7 days” maps to `--older-than 48h` / `--older-than 7d`.
- “keep my latest 5” maps to `--keep-latest 5`. Latest N counts conversations,
  where one conversation is a root thread plus all affected descendants.
- When both selectors are requested, pass both. They combine protectively: a
  conversation is selected only when it is older than the cutoff and outside
  latest N.
- Use `--project current` unless the user explicitly asks for all projects.
  Current scope is an exact normalized cwd match; it never expands to parents,
  children, siblings, or similarly named directories.
- Use `--project all` only when the user asks across all projects.
- Archive is the default action for ambiguous cleanup language.
- Add `--action delete` only when irreversible deletion is explicit.
- At least one retention selector is required. Ask for an age or latest count
  when the request is vague.

One conversation's activity is the newest app-server `recencyAt` among its root
and descendants. Pinned, active, loaded, ephemeral, and otherwise unsafe
members protect the whole conversation. Malformed or ambiguous metadata and
cross-project descendants are excluded before ranking. Never work around a
protected or excluded result.

Use the four reported statuses exactly:

- `selected`: eligible for the requested action;
- `retained`: kept by the age or latest-N rule;
- `protected`: kept by a safety rule such as pinning or runtime activity;
- `excluded`: cannot safely participate because identity, ancestry, metadata,
  or exact scope is uncertain.

## Preview first

Every policy run is read-only unless an explicit apply mode is present. Start
without `--apply` or `--apply-plan`:

```bash
python3 -I /path/to/temporary/cleanup_sessions.py \
  --older-than 30d --project current --action archive
```

Use `--details` for full UUIDs and expanded state. Use `--json` for the stable
shared preview structure; titles remain `null` unless the user explicitly asks
for identifying titles and `--include-titles` is added. Summarize selected,
retained, protected, and excluded conversations, every affected descendant,
and the reason codes. Say that no changes were made.

Discovery intentionally uses Codex state-database-only listing so preview does
not scan rollout files and repair metadata. Unindexed history may be omitted.
Do not silently switch to scan-and-repair discovery.

## Archive after review

Archive is Codex-native and reversible through `codex unarchive`. After showing
a separate preview, use `--apply` only when the user asks to archive, keeping
the same policy and cwd:

```bash
python3 -I /path/to/temporary/cleanup_sessions.py \
  --older-than 30d --project current --action archive --apply
```

The helper takes an advisory lock and refreshes safety-critical state before
each root command. It reports successful, skipped, and failed targets. Preserve
any Codex ownership or active-session refusal; never retry with a less safe
operation.

## Permanently delete through a reviewed plan

Permanent deletion is irreversible. Never use `--action delete --apply`; the
helper refuses that path. When the user explicitly requests deletion:

1. Preview and save a private plan outside Codex home:

   ```bash
   mkdir -p "${XDG_CACHE_HOME:-$HOME/.cache}/codex-session-cleanup"
   python3 -I /path/to/temporary/cleanup_sessions.py \
     --older-than 30d --project current --action delete --details \
     --save-plan "${XDG_CACHE_HOME:-$HOME/.cache}/codex-session-cleanup/delete-plan.json"
   ```

2. Show the preview. Explain that the manifest contains full session IDs,
   titles, and local paths. Ask the user to review the exact roots and all
   descendants and explicitly confirm this plan. Do not apply before a clear
   confirmation.
3. Apply only that manifest, from the same cwd:

   ```bash
   python3 -I /path/to/temporary/cleanup_sessions.py \
     --apply-plan "${XDG_CACHE_HOME:-$HOME/.cache}/codex-session-cleanup/delete-plan.json"
   ```

Do not add policy flags to `--apply-plan`, edit a manifest, or reuse a stale,
applying, partially consumed, or consumed plan. The helper revalidates the
Codex version, full candidate set, descendants, recency, pin/load/activity,
and project scope. Any relevant change refuses the plan and requires a new
preview. Shared `--json` preview output is not a deletion manifest and cannot
authorize mutation.

## Report safety stops and limitations

If the helper prints `Safety stop:`, `Invalid plan:`, or `Stale plan:`, report
the message and stop. Do not bypass it through direct file or database access.
See `references/safety.md` for the permission rationale and full limitations.

Important limits:

- Requires local Python 3.10+, a compatible Codex CLI, and local Codex state.
- State-database-only discovery can omit unindexed history.
- A standalone app-server cannot prove that another Codex process has not
  loaded or changed a session after discovery.
- Codex currently has no atomic conditional archive/delete operation, leaving
  a small race between final validation and the native command.
- This tool handles stored Codex session history only. It never cleans logs,
  authentication, configuration, memories, skills, plugins, or worktrees.
