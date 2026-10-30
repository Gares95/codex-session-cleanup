#!/usr/bin/env python3
"""Preview and apply conservative Codex session-retention policies.

The helper reads session metadata through the installed Codex app-server and
delegates every mutation to the installed ``codex`` CLI.  It never edits Codex
state or rollout files directly.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence
import unicodedata
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows only
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX only
    msvcrt = None  # type: ignore[assignment]


INTERACTIVE_SOURCES = ("cli", "vscode")
SAFE_STATUSES = ("notLoaded", "idle")
PINNED_SECTION_NAME = "Pinned"
DURATION_RE = re.compile(r"([1-9][0-9]*)([hd])\Z")
PLAN_FORMAT = "codex-session-cleanup-plan"
PLAN_VERSION = 1
TOOL_VERSION = "1.0.0"
EXIT_ERROR = 1
EXIT_INVALID_PLAN = 2
EXIT_STALE_PLAN = 3
EXIT_LOCKED = 4


class CleanupError(RuntimeError):
    """Base class for safe operational failures."""


class CompatibilityError(CleanupError):
    """The installed Codex interface lacks a required safety contract."""


class ProtocolError(CleanupError):
    """The app-server returned an invalid or unsuccessful response."""


class StaleStateError(CleanupError):
    """Session state changed between the apply plan and final safety check."""


class ManifestError(CleanupError):
    """A saved preview manifest is malformed, unsafe, or already consumed."""


class LockError(CleanupError):
    """Another cooperating cleanup process already holds the mutation lock."""


@dataclasses.dataclass(frozen=True)
class Policy:
    older_than: dt.timedelta | None
    keep_latest: int | None
    project: str
    action: str
    current_cwd: str
    cutoff: dt.datetime | None = None


@dataclasses.dataclass(frozen=True)
class Session:
    id: str
    title: str
    created_at: dt.datetime
    updated_at: dt.datetime
    recency_at: dt.datetime
    cwd: str
    pinned: bool
    ephemeral: bool
    status: str
    active_flags: tuple[str, ...]
    archived: bool
    parent_id: str | None
    session_id: str
    source_kind: str
    loaded: bool = False
    title_source: str | None = None

    @property
    def unsafe(self) -> bool:
        return self.ephemeral or self.loaded or self.status not in SAFE_STATUSES


@dataclasses.dataclass(frozen=True)
class UnsafeRecord:
    id: str | None
    reason: str
    cwd: str | None = None


@dataclasses.dataclass(frozen=True)
class Candidate:
    root: Session
    descendants: tuple[Session, ...] = ()


@dataclasses.dataclass(frozen=True)
class Protection:
    category: str
    reason: str
    target_id: str | None
    root_id: str | None
    session: Session | None = None
    blocker: Session | None = None


@dataclasses.dataclass(frozen=True)
class GroupEvaluation:
    tree: Candidate
    status: str
    reasons: tuple[str, ...]
    rank: int | None
    activity: dt.datetime
    activity_session_id: str
    issues: tuple[UnsafeRecord, ...] = ()


@dataclasses.dataclass(frozen=True)
class Plan:
    policy: Policy
    now: dt.datetime
    observed: tuple[Session, ...]
    malformed: tuple[UnsafeRecord, ...]
    protected_recent: tuple[Session, ...]
    protected_pinned: tuple[Session, ...]
    protected_unsafe: tuple[UnsafeRecord, ...]
    trees: tuple[Candidate, ...]
    candidates: tuple[Candidate, ...]
    protections: tuple[Protection, ...]
    groups: tuple[GroupEvaluation, ...]
    ranking_universe: tuple[Candidate, ...]

    @property
    def examined(self) -> int:
        return self.examined_roots + self.examined_descendants + len(self.malformed)

    @property
    def examined_roots(self) -> int:
        return len(self.observed)

    @property
    def examined_descendants(self) -> int:
        return len({item.id for tree in self.trees for item in tree.descendants})


@dataclasses.dataclass(frozen=True)
class SchemaCapabilities:
    all_source_kinds: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class ThreadSection:
    id: str
    name: str


@dataclasses.dataclass(frozen=True)
class TargetFailure:
    session_id: str
    error: str


@dataclasses.dataclass(frozen=True)
class MutationReport:
    successful: tuple[str, ...]
    skipped: tuple[TargetFailure, ...]
    failed: tuple[TargetFailure, ...]
    stopped_reason: str | None = None


@dataclasses.dataclass(frozen=True)
class SavedPlan:
    path: Path
    document: dict[str, Any]
    policy: Policy
    codex_home: str
    codex_version: str
    candidates: tuple[Candidate, ...]


def parse_duration(value: str) -> dt.timedelta:
    """Parse a positive integer number of hours or days."""
    match = DURATION_RE.fullmatch(value)
    if not match:
        raise argparse.ArgumentTypeError("duration must be a positive integer followed by h or d")
    amount = int(match.group(1))
    return dt.timedelta(hours=amount) if match.group(2) == "h" else dt.timedelta(days=amount)


def positive_int(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely preview or apply Codex session-retention policies.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  preview archive:  cleanup_sessions.py --older-than 7d --project current
  apply archive:    cleanup_sessions.py --older-than 7d --project current --apply
  save delete plan: cleanup_sessions.py --older-than 30d --action delete --save-plan PLAN.json
  apply exact plan: cleanup_sessions.py --apply-plan PLAN.json

Permanent deletion cannot use --apply; it requires a saved, reviewed plan.
Latest N ranks root conversations; each root plus its descendants counts once.
""",
    )
    parser.add_argument(
        "--older-than",
        type=parse_duration,
        metavar="DURATION",
        help="select conversations last used before the fixed age cutoff (for example 48h or 7d)",
    )
    parser.add_argument(
        "--keep-latest",
        type=positive_int,
        metavar="N",
        help="retain the latest N root conversations in the selected scope",
    )
    parser.add_argument(
        "--project",
        choices=("current", "all"),
        help="use an exact current-cwd scope (default) or explicitly use all projects",
    )
    parser.add_argument(
        "--action",
        choices=("archive", "delete"),
        help="archive by default; delete is irreversible and requires a saved plan",
    )
    apply_group = parser.add_mutually_exclusive_group()
    apply_group.add_argument(
        "--apply",
        action="store_true",
        help="apply an archive policy after fresh safety checks",
    )
    apply_group.add_argument(
        "--apply-plan",
        type=Path,
        metavar="PATH",
        help="apply the exact candidates in a saved preview manifest",
    )
    parser.add_argument(
        "--save-plan",
        type=Path,
        metavar="PATH",
        help="save this preview as a private, reviewable manifest",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="show full UUIDs and expanded session metadata",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the shared machine-readable preview structure",
    )
    parser.add_argument(
        "--include-titles",
        action="store_true",
        help="include sanitized titles in shared JSON preview output",
    )
    parser.add_argument(
        "--check-compatibility",
        action="store_true",
        help="validate installed Codex read/mutation contracts without changing sessions",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argument_parser()
    args = parser.parse_args(argv)

    work_options = (
        args.older_than,
        args.keep_latest,
        args.project,
        args.action,
        args.apply,
        args.apply_plan,
        args.save_plan,
    )
    if args.check_compatibility:
        if any(value not in (None, False) for value in work_options):
            parser.error("--check-compatibility cannot be combined with cleanup options")
        return args

    if args.include_titles and not args.json:
        parser.error("--include-titles requires --json")
    if args.json and (args.apply or args.apply_plan is not None):
        parser.error("--json is a preview format and cannot be combined with an apply mode")

    if args.apply_plan is not None:
        incompatible = (
            args.older_than,
            args.keep_latest,
            args.project,
            args.action,
            args.save_plan,
        )
        if any(value is not None for value in incompatible):
            parser.error("--apply-plan cannot be combined with policy or --save-plan options")
        return args

    args.project = args.project or "current"
    args.action = args.action or "archive"
    if args.older_than is None and args.keep_latest is None:
        parser.error("at least one of --older-than or --keep-latest is required")
    if args.save_plan is not None and args.apply:
        parser.error("--save-plan creates a preview and cannot be combined with --apply")
    if args.action == "delete" and args.apply:
        parser.error(
            "permanent deletion requires --save-plan followed by --apply-plan; "
            "direct --action delete --apply is disabled"
        )
    return args


def normalize_invoking_cwd(path: str) -> str:
    if not os.path.isabs(path):
        raise CleanupError("the invoking working directory must be absolute")
    physical = os.path.realpath(path)
    return os.path.normcase(unicodedata.normalize("NFC", os.path.normpath(physical)))


def canonical_uuid(value: Any, field: str = "session id") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} is not a string UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field} is not a valid UUID") from exc
    canonical = str(parsed)
    if value.lower() != canonical:
        raise ValueError(f"{field} is not a canonical UUID")
    return canonical


def timestamp(value: Any, field: str) -> dt.datetime:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} is not a non-negative integer Unix timestamp")
    try:
        return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f"{field} is outside the supported timestamp range") from exc


def source_kind(value: Any) -> str:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, Mapping) and len(value) == 1:
        key = next(iter(value))
        if isinstance(key, str) and key:
            return key
    raise ValueError("source is not interpretable")


def optional_uuid(value: Any, field: str) -> str | None:
    return None if value is None else canonical_uuid(value, field)


def sanitize_title(value: Any, *, limit: int = 200) -> str:
    """Return terminal-safe identifying metadata without control characters."""
    if not isinstance(value, str):
        return ""
    characters = [
        " " if unicodedata.category(character).startswith("C") else character
        for character in value
    ]
    cleaned = " ".join("".join(characters).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def parse_thread_section(raw: Mapping[str, Any]) -> ThreadSection:
    section_id = canonical_uuid(raw.get("id"), "section id")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("section name is missing or empty")
    return ThreadSection(section_id, name)


def parse_thread(
    raw: Mapping[str, Any],
    *,
    archived: bool,
    pinned_section_id: str,
    loaded: bool = False,
) -> Session:
    """Validate a thread object without substituting missing safety metadata."""
    session_id = canonical_uuid(raw.get("id"))
    cwd = raw.get("cwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise ValueError("cwd is missing or is not absolute")
    cwd = normalize_invoking_cwd(cwd)

    if "section" not in raw:
        raise ValueError("section is missing")
    section_value = raw["section"]
    if section_value is None:
        pinned = False
    elif isinstance(section_value, Mapping):
        section = parse_thread_section(section_value)
        pinned_id = canonical_uuid(pinned_section_id, "pinned section id")
        if (section.id == pinned_id) != (section.name == PINNED_SECTION_NAME):
            raise ValueError(
                "section metadata conflicts with the built-in Pinned section"
            )
        pinned = section.id == pinned_id
    else:
        raise ValueError("section is malformed")
    ephemeral = raw.get("ephemeral")
    if not isinstance(ephemeral, bool):
        raise ValueError("ephemeral is missing or is not boolean")

    status_value = raw.get("status")
    if not isinstance(status_value, Mapping) or not isinstance(status_value.get("type"), str):
        raise ValueError("runtime status is missing or malformed")
    status = status_value["type"]
    flags_value = status_value.get("activeFlags", [])
    if not isinstance(flags_value, list) or not all(isinstance(item, str) for item in flags_value):
        raise ValueError("activeFlags is malformed")
    if status != "active" and flags_value:
        raise ValueError("activeFlags are present for a non-active runtime status")

    name = raw.get("name")
    preview = raw.get("preview")
    raw_title = name if isinstance(name, str) and name else preview
    title = sanitize_title(raw_title)
    title_source = (
        "name"
        if isinstance(name, str) and name
        else "preview"
        if isinstance(preview, str) and preview
        else None
    )

    return Session(
        id=session_id,
        title=title,
        created_at=timestamp(raw.get("createdAt"), "createdAt"),
        updated_at=timestamp(raw.get("updatedAt"), "updatedAt"),
        recency_at=timestamp(raw.get("recencyAt"), "recencyAt"),
        cwd=cwd,
        pinned=pinned,
        ephemeral=ephemeral,
        status=status,
        active_flags=tuple(flags_value),
        archived=archived,
        parent_id=optional_uuid(raw.get("parentThreadId"), "parentThreadId"),
        session_id=canonical_uuid(raw.get("sessionId"), "sessionId"),
        source_kind=source_kind(raw.get("source")),
        loaded=loaded,
        title_source=title_source,
    )


def _session_sort_key(session: Session) -> tuple[float, str]:
    return (-session.recency_at.timestamp(), session.id)


def effective_cutoff(policy: Policy, now: dt.datetime) -> dt.datetime | None:
    if policy.cutoff is not None:
        return policy.cutoff
    return now - policy.older_than if policy.older_than is not None else None


def _unsafe_reason(session: Session) -> str | None:
    if session.pinned:
        return "session is pinned"
    if session.ephemeral:
        return "session is ephemeral"
    if session.loaded:
        return "session is loaded by Codex"
    if session.status not in SAFE_STATUSES:
        flags = ", ".join(session.active_flags)
        suffix = f" ({flags})" if flags else ""
        return f"unsafe runtime state: {session.status}{suffix}"
    return None


REASON_ORDER = (
    "older-than",
    "outside-latest",
    "invoking-session",
    "pinned",
    "running",
    "possibly-running",
    "background-session",
    "ephemeral",
    "unsafe-runtime-state",
    "recently-active",
    "within-latest",
    "newer-than-cutoff",
    "related-outside-scope",
    "project-identity-unknown",
    "project-identity-unverified",
    "project-path-collision",
    "metadata-malformed",
    "ancestry-ambiguous",
    "duplicate-session-id",
    "unsafe-storage",
)
REASON_INDEX = {reason: index for index, reason in enumerate(REASON_ORDER)}
STATUS_ORDER = {"selected": 0, "retained": 1, "protected": 2, "excluded": 3}


def _ordered_reason_codes(reasons: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            set(reasons),
            key=lambda reason: (REASON_INDEX.get(reason, len(REASON_INDEX)), reason),
        )
    )


def _runtime_reason_codes(session: Session) -> tuple[str, ...]:
    reasons: list[str] = []
    if session.pinned:
        reasons.append("pinned")
    if session.loaded or session.status == "active":
        reasons.append("running")
    if session.ephemeral:
        reasons.append("ephemeral")
    if session.status not in SAFE_STATUSES and session.status != "active":
        reasons.append("unsafe-runtime-state")
    return _ordered_reason_codes(reasons)


def _issue_reason_code(issue: UnsafeRecord) -> str:
    reason = issue.reason.lower()
    if "duplicate" in reason or "conflicting metadata" in reason:
        return "duplicate-session-id"
    if any(
        text in reason
        for text in (
            "ancestry",
            "parent",
            "multiple roots",
            "descendant set",
            "cycle",
        )
    ):
        return "ancestry-ambiguous"
    return "metadata-malformed"


def _group_activity(tree: Candidate) -> tuple[dt.datetime, str]:
    member = min(
        (tree.root, *tree.descendants),
        key=lambda item: (-item.recency_at.timestamp(), item.id),
    )
    return member.recency_at, member.id


def _descendant_depth(root: Session, item: Session, by_id: Mapping[str, Session]) -> int:
    depth = 1
    parent_id = item.parent_id
    visited = {item.id}
    while parent_id != root.id:
        if parent_id is None or parent_id not in by_id or parent_id in visited:
            return len(by_id) + 1
        visited.add(parent_id)
        parent_id = by_id[parent_id].parent_id
        depth += 1
    return depth


def validate_descendant_tree(
    root: Session, descendants: Sequence[Session]
) -> tuple[UnsafeRecord, ...]:
    """Ensure every returned descendant has an unambiguous path to its root."""
    by_id: dict[str, Session] = {}
    issues: list[UnsafeRecord] = []
    for item in descendants:
        if item.id == root.id:
            issues.append(UnsafeRecord(item.id, "root appeared in its descendant set"))
            continue
        if item.id in by_id:
            issues.append(UnsafeRecord(item.id, "duplicate descendant id"))
            continue
        by_id[item.id] = item

    for item in by_id.values():
        if item.parent_id is None:
            issues.append(UnsafeRecord(item.id, "descendant parentThreadId is missing"))
            continue
        if item.parent_id != root.id and item.parent_id not in by_id:
            issues.append(
                UnsafeRecord(item.id, "descendant parent is absent from the affected tree")
            )
            continue
        visited = {item.id}
        parent_id = item.parent_id
        while parent_id != root.id:
            if parent_id is None or parent_id not in by_id:
                break
            if parent_id in visited:
                issues.append(UnsafeRecord(item.id, "descendant ancestry contains a cycle"))
                break
            visited.add(parent_id)
            parent_id = by_id[parent_id].parent_id
    return tuple(issues)


def _ordered_descendants(
    root: Session, descendants: Sequence[Session]
) -> tuple[Session, ...]:
    by_id = {item.id: item for item in descendants}
    return tuple(
        sorted(
            descendants,
            key=lambda item: (
                _descendant_depth(root, item, by_id),
                *_session_sort_key(item),
            ),
        )
    )


def build_plan(
    sessions: Iterable[Session],
    policy: Policy,
    *,
    now: dt.datetime,
    malformed: Iterable[UnsafeRecord] = (),
    descendant_lookup: Callable[[Session], tuple[Sequence[Session], Sequence[UnsafeRecord]]] | None = None,
) -> Plan:
    """Evaluate retention once per root conversation and its descendants."""
    if policy.older_than is None and policy.keep_latest is None:
        raise CleanupError("at least one retention selector is required")
    if now.tzinfo is None:
        raise CleanupError("the planning clock must be timezone-aware")

    current_cwd = normalize_invoking_cwd(policy.current_cwd)
    scoped_input = list(sessions)
    scoped_input.sort(key=_session_sort_key)
    scoped_by_id: dict[str, Session] = {}
    duplicate_roots: set[str] = set()
    for item in scoped_input:
        previous = scoped_by_id.get(item.id)
        if previous is not None:
            duplicate_roots.add(item.id)
            continue
        scoped_by_id[item.id] = item
    scoped = list(scoped_by_id.values())
    all_malformed_items = tuple(malformed)

    trees: list[Candidate] = []
    tree_issues: dict[str, list[UnsafeRecord]] = {root.id: [] for root in scoped}
    descendant_owners: dict[str, list[str]] = {
        root.id: [root.id] for root in scoped
    }
    for root in scoped:
        if root.id in duplicate_roots:
            tree_issues[root.id].append(UnsafeRecord(root.id, "duplicate root id"))
        descendants: Sequence[Session] = ()
        issues: Sequence[UnsafeRecord] = ()
        if descendant_lookup is not None:
            descendants, issues = descendant_lookup(root)
        ordered = _ordered_descendants(root, descendants)
        tree_issues[root.id].extend(issues)
        tree_issues[root.id].extend(validate_descendant_tree(root, ordered))
        trees.append(Candidate(root, ordered))
        for item in ordered:
            descendant_owners.setdefault(item.id, []).append(root.id)
            existing = next(
                (
                    known
                    for known_tree in trees[:-1]
                    for known in (known_tree.root, *known_tree.descendants)
                    if known.id == item.id
                ),
                None,
            )
            if existing is not None and existing != item:
                tree_issues[root.id].append(
                    UnsafeRecord(item.id, "session appeared with conflicting metadata")
                )

    for descendant_id, owners in descendant_owners.items():
        if len(set(owners)) > 1:
            for root_id in set(owners):
                tree_issues[root_id].append(
                    UnsafeRecord(descendant_id, "descendant appeared under multiple roots")
                )

    for item in all_malformed_items:
        if item.id is None:
            continue
        for tree in trees:
            if item.id in {member.id for member in (tree.root, *tree.descendants)}:
                tree_issues[tree.root.id].append(item)

    trees = [
        tree
        for tree in trees
        if policy.project == "all"
        or any(
            member.cwd == current_cwd
            for member in (tree.root, *tree.descendants)
        )
    ]
    scoped = [tree.root for tree in trees]
    represented_ids = {
        member.id
        for tree in trees
        for member in (tree.root, *tree.descendants)
    }
    malformed_items = tuple(
        item
        for item in all_malformed_items
        if policy.project == "all"
        or item.id in represented_ids
        or item.cwd is None
        or item.cwd == current_cwd
    )
    malformed_ids = {item.id for item in malformed_items if item.id is not None}

    cutoff = effective_cutoff(policy, now)
    fixed_policy = dataclasses.replace(
        policy,
        cutoff=cutoff,
        current_cwd=current_cwd,
    )

    exclusions: dict[str, tuple[str, ...]] = {}
    activities: dict[str, tuple[dt.datetime, str]] = {}
    for tree in trees:
        activities[tree.root.id] = _group_activity(tree)
        reasons: list[str] = []
        if tree.root.id in malformed_ids:
            reasons.append("metadata-malformed")
        reasons.extend(_issue_reason_code(issue) for issue in tree_issues[tree.root.id])
        if fixed_policy.project == "current" and any(
            item.cwd != current_cwd for item in (tree.root, *tree.descendants)
        ):
            reasons.append("related-outside-scope")
        if reasons:
            exclusions[tree.root.id] = _ordered_reason_codes(reasons)

    ranking_universe = sorted(
        (tree for tree in trees if tree.root.id not in exclusions),
        key=lambda tree: (-activities[tree.root.id][0].timestamp(), tree.root.id),
    )
    ranks = {
        tree.root.id: rank
        for rank, tree in enumerate(ranking_universe, start=1)
    }

    protected_recent: list[Session] = []
    protected_pinned: list[Session] = []
    protected_unsafe: list[UnsafeRecord] = []
    protections: list[Protection] = []
    groups: list[GroupEvaluation] = []
    for tree in trees:
        root = tree.root
        activity, activity_session_id = activities[root.id]
        issues = tuple(tree_issues[root.id])
        exclusion_reasons = exclusions.get(root.id, ())
        rank = ranks.get(root.id)

        retention_reasons: list[str] = []
        selection_reasons: list[str] = []
        if cutoff is not None:
            if activity < cutoff:
                selection_reasons.append("older-than")
            else:
                retention_reasons.append("newer-than-cutoff")
        if fixed_policy.keep_latest is not None:
            assert rank is not None or exclusion_reasons
            if rank is not None and rank <= fixed_policy.keep_latest:
                retention_reasons.append("within-latest")
            elif rank is not None:
                selection_reasons.append("outside-latest")

        members = (root, *tree.descendants)
        protection_reasons = _ordered_reason_codes(
            reason
            for member in members
            for reason in _runtime_reason_codes(member)
        )

        if exclusion_reasons:
            status = "excluded"
            reasons = exclusion_reasons
        elif protection_reasons:
            status = "protected"
            reasons = _ordered_reason_codes(
                (*protection_reasons, *retention_reasons)
            )
        elif retention_reasons:
            status = "retained"
            reasons = _ordered_reason_codes(retention_reasons)
        else:
            status = "selected"
            reasons = _ordered_reason_codes(selection_reasons)

        group = GroupEvaluation(
            tree=tree,
            status=status,
            reasons=reasons,
            rank=rank,
            activity=activity,
            activity_session_id=activity_session_id,
            issues=issues,
        )
        groups.append(group)

        if status == "selected":
            continue

        blocker = next(
            (
                member
                for member in members
                if _runtime_reason_codes(member)
                or member.id in {issue.id for issue in issues}
                or (
                    fixed_policy.project == "current"
                    and member.cwd != current_cwd
                )
            ),
            None,
        )
        reason_text = ", ".join(reasons)
        protections.append(
            Protection(
                reasons[0],
                reason_text,
                root.id,
                root.id,
                session=root,
                blocker=blocker if blocker is not root else None,
            )
        )
        if retention_reasons:
            protected_recent.append(root)
        if "pinned" in protection_reasons:
            protected_pinned.append(root)
        if status in ("protected", "excluded"):
            protected_unsafe.append(UnsafeRecord(root.id, reason_text))

    for item in malformed_items:
        if item.id is not None and item.id in represented_ids:
            continue
        protections.append(
            Protection("metadata-malformed", item.reason, item.id, item.id)
        )
        protected_unsafe.append(item)

    groups.sort(
        key=lambda group: (
            STATUS_ORDER[group.status],
            -group.activity.timestamp(),
            group.tree.root.id,
        )
    )
    candidates = sorted(
        (group.tree for group in groups if group.status == "selected"),
        key=lambda tree: _session_sort_key(tree.root),
    )

    return Plan(
        policy=fixed_policy,
        now=now,
        observed=tuple(scoped),
        malformed=malformed_items,
        protected_recent=tuple(protected_recent),
        protected_pinned=tuple(protected_pinned),
        protected_unsafe=tuple(protected_unsafe),
        trees=tuple(trees),
        candidates=tuple(candidates),
        protections=tuple(protections),
        groups=tuple(groups),
        ranking_universe=tuple(ranking_universe),
    )


def _session_snapshot(item: Session) -> tuple[Any, ...]:
    return (
        item.id,
        int(item.recency_at.timestamp()),
        item.cwd,
        item.pinned,
        item.ephemeral,
        item.status,
        item.active_flags,
        item.archived,
        item.parent_id,
        item.session_id,
        item.source_kind,
        item.loaded,
    )


def plan_snapshot(plan: Plan) -> tuple[Any, ...]:
    """Return all selection- and safety-critical state for stale checks."""
    observed = tuple(sorted((_session_snapshot(item) for item in plan.observed)))
    malformed = tuple(
        sorted(((item.id or "", item.reason, item.cwd or "") for item in plan.malformed))
    )
    trees = tuple(
        (
            _session_snapshot(tree.root),
            tuple(_session_snapshot(item) for item in tree.descendants),
        )
        for tree in plan.trees
    )
    candidates = tuple(
        (
            _session_snapshot(candidate.root),
            tuple(sorted(_session_snapshot(item) for item in candidate.descendants)),
        )
        for candidate in plan.candidates
    )
    return observed, malformed, trees, candidates


def require_fresh_plan(before: Plan, after: Plan) -> None:
    if plan_snapshot(before) != plan_snapshot(after):
        raise StaleStateError("session candidates or safety-critical metadata changed; run a fresh preview")


def candidate_snapshot(candidates: Sequence[Candidate]) -> tuple[Any, ...]:
    return tuple(
        (
            _session_snapshot(candidate.root),
            tuple(_session_snapshot(item) for item in candidate.descendants),
        )
        for candidate in candidates
    )


def require_candidate_match(
    expected: Sequence[Candidate], current: Sequence[Candidate]
) -> None:
    if candidate_snapshot(expected) != candidate_snapshot(current):
        raise StaleStateError(
            "approved candidates or affected descendants changed; generate a fresh preview"
        )


def _load_schema_by_title(root: Path, title: str) -> Mapping[str, Any]:
    for path in root.rglob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, Mapping) and value.get("title") == title:
            return value
    raise CompatibilityError(f"installed app-server schema does not contain {title}")


def validate_schema_contract(
    list_params: Mapping[str, Any],
    list_response: Mapping[str, Any],
    initialize_response: Mapping[str, Any],
    section_list_params: Mapping[str, Any],
    section_list_response: Mapping[str, Any],
    loaded_list_params: Mapping[str, Any],
    loaded_list_response: Mapping[str, Any],
) -> SchemaCapabilities:
    param_properties = list_params.get("properties", {})
    required_params = {
        "ancestorThreadId", "archived", "cursor", "cwd", "limit", "sectionId",
        "sortKey", "sortDirection", "sourceKinds", "useStateDbOnly",
    }
    missing_params = sorted(required_params - set(param_properties))

    definitions = list_response.get("definitions", {})
    thread_properties = definitions.get("Thread", {}).get("properties", {})
    required_thread = {
        "id", "name", "preview", "createdAt", "updatedAt", "recencyAt",
        "cwd", "ephemeral", "section", "sessionId", "parentThreadId",
        "source", "status",
    }
    missing_thread = sorted(required_thread - set(thread_properties))

    response_properties = list_response.get("properties", {})
    missing_response = sorted({"data", "nextCursor"} - set(response_properties))

    section_param_properties = section_list_params.get("properties", {})
    missing_section_params = sorted(
        {"cursor", "limit"} - set(section_param_properties)
    )
    section_response_properties = section_list_response.get("properties", {})
    missing_section_response = sorted(
        {"data", "nextCursor"} - set(section_response_properties)
    )
    section_definitions = section_list_response.get("definitions", {})
    section_properties = section_definitions.get("ThreadSection", {}).get(
        "properties", {}
    )
    missing_section = sorted({"id", "name"} - set(section_properties))

    loaded_param_properties = loaded_list_params.get("properties", {})
    missing_loaded_params = sorted({"cursor", "limit"} - set(loaded_param_properties))
    loaded_response_properties = loaded_list_response.get("properties", {})
    missing_loaded_response = sorted(
        {"data", "nextCursor"} - set(loaded_response_properties)
    )

    source_schema = list_params.get("definitions", {}).get("ThreadSourceKind", {})
    source_values = tuple(source_schema.get("enum", ()))
    if not source_values or not all(isinstance(item, str) for item in source_values):
        missing_params.append("ThreadSourceKind enum")

    missing = [
        *(f"ThreadListParams.{item}" for item in missing_params),
        *(f"ThreadListResponse.{item}" for item in missing_response),
        *(f"Thread.{item}" for item in missing_thread),
        *(f"ThreadSectionListParams.{item}" for item in missing_section_params),
        *(f"ThreadSectionListResponse.{item}" for item in missing_section_response),
        *(f"ThreadSection.{item}" for item in missing_section),
        *(f"ThreadLoadedListParams.{item}" for item in missing_loaded_params),
        *(f"ThreadLoadedListResponse.{item}" for item in missing_loaded_response),
    ]
    if "codexHome" not in initialize_response.get("properties", {}):
        missing.append("InitializeResponse.codexHome")
    if missing:
        raise CompatibilityError(
            "installed Codex does not expose required cleanup safety metadata: "
            + ", ".join(missing)
        )
    return SchemaCapabilities(all_source_kinds=source_values)


def inspect_installed_schema(
    codex_bin: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> SchemaCapabilities:
    """Generate and verify the schema belonging to the installed Codex binary."""
    with tempfile.TemporaryDirectory(prefix="codex-session-cleanup-schema-") as temp_dir:
        completed = runner(
            [codex_bin, "app-server", "generate-json-schema", "--experimental", "--out", temp_dir],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "schema generation failed").strip()
            raise CompatibilityError(f"could not generate the installed app-server schema: {detail}")

        root = Path(temp_dir)
        list_params = _load_schema_by_title(root, "ThreadListParams")
        list_response = _load_schema_by_title(root, "ThreadListResponse")
        initialize_response = _load_schema_by_title(root, "InitializeResponse")
        section_list_params = _load_schema_by_title(root, "ThreadSectionListParams")
        section_list_response = _load_schema_by_title(root, "ThreadSectionListResponse")
        loaded_list_params = _load_schema_by_title(root, "ThreadLoadedListParams")
        loaded_list_response = _load_schema_by_title(root, "ThreadLoadedListResponse")
        return validate_schema_contract(
            list_params,
            list_response,
            initialize_response,
            section_list_params,
            section_list_response,
            loaded_list_params,
            loaded_list_response,
        )


def inspect_mutation_commands(
    codex_bin: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    for command in ("archive", "delete"):
        completed = runner(
            [codex_bin, command, "--help"], capture_output=True, text=True, check=False
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode != 0 or "<SESSION>" not in output:
            raise CompatibilityError(f"installed Codex lacks a supported {command} SESSION command")
        if command == "delete" and "--force" not in output:
            raise CompatibilityError("installed Codex delete command lacks the required --force UUID mode")


class AppServerClient:
    """Minimal newline-delimited JSON-RPC client for Codex app-server."""

    def __init__(self, codex_bin: str, *, environment: Mapping[str, str] | None = None) -> None:
        self.codex_bin = codex_bin
        self.environment = dict(environment) if environment is not None else dict(os.environ)
        self.process: subprocess.Popen[str] | None = None
        self._stderr: Any = None
        self._next_id = 1
        self.codex_home: str | None = None

    def __enter__(self) -> "AppServerClient":
        self._stderr = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
        self.process = subprocess.Popen(
            [self.codex_bin, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            bufsize=1,
            env=self.environment,
        )
        result = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-session-cleanup",
                    "title": "Codex Session Cleanup",
                    "version": "1",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        self.codex_home = validate_codex_home(result, self.environment)
        self.notify("initialized", {})
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.process is not None:
            for stream in (self.process.stdin, self.process.stdout):
                if stream is not None:
                    stream.close()
        if self._stderr is not None:
            self._stderr.close()

    def _write(self, message: Mapping[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise ProtocolError("app-server is not running")
        try:
            self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ProtocolError("app-server closed its input") from exc

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._write({"method": method, "params": params})

    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._write({"method": method, "id": request_id, "params": params})
        if self.process is None or self.process.stdout is None:
            raise ProtocolError("app-server is not running")
        while True:
            line = self.process.stdout.readline()
            if not line:
                detail = ""
                if self._stderr is not None:
                    self._stderr.seek(0)
                    detail = self._stderr.read().strip()
                raise ProtocolError(f"app-server stopped before replying{': ' + detail if detail else ''}")
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProtocolError("app-server returned non-JSON output") from exc
            if not isinstance(message, Mapping):
                raise ProtocolError("app-server returned an invalid message")
            if message.get("id") != request_id:
                if "method" in message and "id" not in message:
                    continue
                raise ProtocolError("app-server returned an unexpected request or response")
            if "error" in message:
                raise ProtocolError(f"{method} failed: {format_protocol_error(message['error'])}")
            result = message.get("result")
            if not isinstance(result, Mapping):
                raise ProtocolError(f"{method} returned an invalid result")
            return result


def format_protocol_error(error: Any) -> str:
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str):
            return message
    return "unspecified app-server error"


def validate_codex_home(result: Mapping[str, Any], environment: Mapping[str, str]) -> str:
    """Trust the app-server's resolved home and verify an explicit override."""
    value = result.get("codexHome")
    if not isinstance(value, str) or not os.path.isabs(value):
        raise ProtocolError("initialize did not return an absolute codexHome")
    resolved = normalize_invoking_cwd(value)
    configured = environment.get("CODEX_HOME")
    if configured is not None:
        configured_path = os.path.abspath(os.path.expanduser(configured))
        if normalize_invoking_cwd(configured_path) != resolved:
            raise ProtocolError("app-server resolved a different home than CODEX_HOME")
    return resolved


class SessionDiscovery:
    def __init__(self, client: AppServerClient, capabilities: SchemaCapabilities) -> None:
        self.client = client
        self.capabilities = capabilities

    def _list_pages(self, params: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        records: list[Mapping[str, Any]] = []
        while True:
            page_params = dict(params)
            page_params["limit"] = 100
            if cursor is not None:
                page_params["cursor"] = cursor
            result = self.client.request("thread/list", page_params)
            data = result.get("data")
            if not isinstance(data, list) or not all(isinstance(item, Mapping) for item in data):
                raise ProtocolError("thread/list returned malformed data")
            records.extend(data)
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return records
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                raise ProtocolError("thread/list returned an invalid pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def _list_section_pages(self) -> list[Mapping[str, Any]]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        records: list[Mapping[str, Any]] = []
        while True:
            params: dict[str, Any] = {"limit": 100}
            if cursor is not None:
                params["cursor"] = cursor
            result = self.client.request("threadSection/list", params)
            data = result.get("data")
            if not isinstance(data, list) or not all(
                isinstance(item, Mapping) for item in data
            ):
                raise ProtocolError("threadSection/list returned malformed data")
            records.extend(data)
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return records
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor in seen_cursors
            ):
                raise ProtocolError(
                    "threadSection/list returned an invalid pagination cursor"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def discover_pinned_section(self) -> ThreadSection:
        sections: dict[str, ThreadSection] = {}
        for raw in self._list_section_pages():
            try:
                section = parse_thread_section(raw)
            except ValueError as exc:
                raise ProtocolError(
                    f"threadSection/list returned malformed data: {exc}"
                ) from exc
            if section.id in sections:
                raise ProtocolError(
                    "threadSection/list returned a duplicate section id"
                )
            sections[section.id] = section

        pinned = [
            section for section in sections.values()
            if section.name == PINNED_SECTION_NAME
        ]
        if len(pinned) != 1:
            raise CompatibilityError(
                "installed Codex did not expose exactly one built-in Pinned "
                "thread section"
            )
        return pinned[0]

    def discover_loaded_ids(self) -> set[str]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        loaded: set[str] = set()
        while True:
            params: dict[str, Any] = {"limit": 100}
            if cursor is not None:
                params["cursor"] = cursor
            result = self.client.request("thread/loaded/list", params)
            data = result.get("data")
            if not isinstance(data, list):
                raise ProtocolError("thread/loaded/list returned malformed data")
            for value in data:
                try:
                    loaded_id = canonical_uuid(value, "loaded thread id")
                except ValueError as exc:
                    raise ProtocolError(
                        f"thread/loaded/list returned malformed data: {exc}"
                    ) from exc
                if loaded_id in loaded:
                    raise ProtocolError(
                        "thread/loaded/list returned a duplicate thread id"
                    )
                loaded.add(loaded_id)
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return loaded
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor in seen_cursors
            ):
                raise ProtocolError(
                    "thread/loaded/list returned an invalid pagination cursor"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def discover_roots(
        self,
        policy: Policy,
        *,
        pinned_section_id: str,
        loaded_ids: set[str],
    ) -> tuple[list[Session], list[UnsafeRecord]]:
        archived_scopes = (False, True) if policy.action == "delete" else (False,)
        sessions: dict[str, Session] = {}
        duplicate_ids: set[str] = set()
        malformed: list[UnsafeRecord] = []
        for archived in archived_scopes:
            params: dict[str, Any] = {
                "archived": archived,
                "sortKey": "recency_at",
                "sortDirection": "desc",
                "sourceKinds": list(INTERACTIVE_SOURCES),
                "useStateDbOnly": True,
            }
            for raw in self._list_pages(params):
                raw_id = raw.get("id") if isinstance(raw.get("id"), str) else None
                raw_cwd = raw.get("cwd")
                unsafe_cwd = (
                    normalize_invoking_cwd(raw_cwd)
                    if isinstance(raw_cwd, str) and os.path.isabs(raw_cwd)
                    else None
                )
                try:
                    item = parse_thread(
                        raw,
                        archived=archived,
                        pinned_section_id=pinned_section_id,
                        loaded=raw_id in loaded_ids if raw_id is not None else False,
                    )
                    if item.source_kind not in INTERACTIVE_SOURCES or item.parent_id is not None:
                        continue
                    if item.id in duplicate_ids:
                        continue
                    existing = sessions.get(item.id)
                    if existing is not None:
                        malformed.append(
                            UnsafeRecord(
                                item.id,
                                "root appeared more than once",
                                item.cwd,
                            )
                        )
                        sessions.pop(item.id, None)
                        duplicate_ids.add(item.id)
                        continue
                    sessions[item.id] = item
                except ValueError as exc:
                    malformed.append(UnsafeRecord(raw_id, str(exc), unsafe_cwd))
                    if raw_id is not None:
                        try:
                            unsafe_id = canonical_uuid(raw_id)
                        except ValueError:
                            pass
                        else:
                            sessions.pop(unsafe_id, None)
                            duplicate_ids.add(unsafe_id)
        return list(sessions.values()), malformed

    def discover_descendants(
        self,
        root: Session,
        policy: Policy,
        *,
        pinned_section_id: str,
        loaded_ids: set[str],
    ) -> tuple[Sequence[Session], Sequence[UnsafeRecord]]:
        archived_scopes = (False, True) if policy.action == "delete" else (False,)
        descendants: dict[str, Session] = {}
        duplicate_ids: set[str] = set()
        malformed: list[UnsafeRecord] = []
        for archived in archived_scopes:
            params = {
                "ancestorThreadId": root.id,
                "archived": archived,
                "sortKey": "recency_at",
                "sortDirection": "desc",
                "sourceKinds": list(self.capabilities.all_source_kinds),
                "useStateDbOnly": True,
            }
            for raw in self._list_pages(params):
                raw_id = raw.get("id") if isinstance(raw.get("id"), str) else None
                raw_cwd = raw.get("cwd")
                unsafe_cwd = (
                    normalize_invoking_cwd(raw_cwd)
                    if isinstance(raw_cwd, str) and os.path.isabs(raw_cwd)
                    else None
                )
                try:
                    item = parse_thread(
                        raw,
                        archived=archived,
                        pinned_section_id=pinned_section_id,
                        loaded=raw_id in loaded_ids if raw_id is not None else False,
                    )
                    if item.id in duplicate_ids:
                        continue
                    if item.id in descendants:
                        descendants.pop(item.id, None)
                        duplicate_ids.add(item.id)
                        raise ValueError("descendant appeared more than once")
                    descendants[item.id] = item
                except ValueError as exc:
                    malformed.append(UnsafeRecord(raw_id, str(exc), unsafe_cwd))
        return tuple(descendants.values()), tuple(malformed)


def discover_plan(
    discovery: SessionDiscovery, policy: Policy, *, now: dt.datetime
) -> Plan:
    pinned_section = discovery.discover_pinned_section()
    loaded_ids = discovery.discover_loaded_ids()
    sessions, malformed = discovery.discover_roots(
        policy,
        pinned_section_id=pinned_section.id,
        loaded_ids=loaded_ids,
    )
    return build_plan(
        sessions,
        policy,
        now=now,
        malformed=malformed,
        descendant_lookup=lambda root: discovery.discover_descendants(
            root,
            policy,
            pinned_section_id=pinned_section.id,
            loaded_ids=loaded_ids,
        ),
    )


def short_id(session_id: str) -> str:
    return session_id[:8] + "..."


def format_duration(value: dt.timedelta | None) -> str:
    if value is None:
        return "not set"
    seconds = int(value.total_seconds())
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days} day{'s' if days != 1 else ''}"
    hours = seconds // 3600
    return f"{hours} hour{'s' if hours != 1 else ''}"


def format_age(now: dt.datetime, recency: dt.datetime) -> str:
    seconds = max(0, int((now - recency).total_seconds()))
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    return "<1h"


def session_record(session: Session) -> dict[str, Any]:
    return {
        "id": session.id,
        "title": session.title,
        "createdAt": int(session.created_at.timestamp()),
        "updatedAt": int(session.updated_at.timestamp()),
        "recencyAt": int(session.recency_at.timestamp()),
        "cwd": session.cwd,
        "pinned": session.pinned,
        "ephemeral": session.ephemeral,
        "loaded": session.loaded,
        "status": session.status,
        "activeFlags": list(session.active_flags),
        "archived": session.archived,
        "parentId": session.parent_id,
        "sessionId": session.session_id,
        "sourceKind": session.source_kind,
    }


def policy_record(policy: Policy) -> dict[str, Any]:
    return {
        "olderThanSeconds": (
            int(policy.older_than.total_seconds())
            if policy.older_than is not None
            else None
        ),
        "absoluteCutoff": (
            int(policy.cutoff.timestamp()) if policy.cutoff is not None else None
        ),
        "keepLatest": policy.keep_latest,
        "project": policy.project,
        "currentCwd": policy.current_cwd,
        "action": policy.action,
        "latestRankingUniverse": (
            "non-excluded root conversations within the selected project scope; "
            "protected conversations still rank"
        ),
    }


def protection_record(
    protection: Protection, descendants: Sequence[Session] = ()
) -> dict[str, Any]:
    return {
        "category": protection.category,
        "reason": protection.reason,
        "targetId": protection.target_id,
        "rootId": protection.root_id,
        "session": (
            session_record(protection.session)
            if protection.session is not None
            else None
        ),
        "blocker": (
            session_record(protection.blocker)
            if protection.blocker is not None
            else None
        ),
        "descendants": [session_record(item) for item in descendants],
    }


def plan_document(
    plan: Plan,
    codex_home: str,
    *,
    codex_version: str,
    state: str = "preview",
    plan_id: str | None = None,
) -> dict[str, Any]:
    candidate_descendants = sum(
        len(candidate.descendants) for candidate in plan.candidates
    )
    trees_by_root = {tree.root.id: tree for tree in plan.trees}
    protected_session_ids = set()
    protected_records: list[dict[str, Any]] = []
    for protection in plan.protections:
        if protection.session is not None:
            protected_session_ids.add(protection.session.id)
        tree = trees_by_root.get(protection.root_id or "")
        descendants = tree.descendants if tree is not None else ()
        if tree is not None:
            protected_session_ids.update(item.id for item in descendants)
        protected_records.append(protection_record(protection, descendants))
    document: dict[str, Any] = {
        "format": PLAN_FORMAT,
        "version": PLAN_VERSION,
        "planId": plan_id or str(uuid.uuid4()),
        "state": state,
        "createdAt": int(plan.now.timestamp()),
        "codexHome": normalize_invoking_cwd(codex_home),
        "codexVersion": sanitize_title(codex_version, limit=120),
        "policy": policy_record(plan.policy),
        "summary": {
            "examinedRoots": plan.examined_roots,
            "examinedDescendants": plan.examined_descendants,
            "candidateRoots": len(plan.candidates),
            "candidateDescendants": candidate_descendants,
            "protected": len(plan.protections),
            "protectedSessions": len(protected_session_ids),
            "malformed": len(plan.malformed),
        },
        "candidates": [
            {
                "root": session_record(candidate.root),
                "descendants": [
                    session_record(item) for item in candidate.descendants
                ],
            }
            for candidate in plan.candidates
        ],
        "protected": protected_records,
        "malformed": [
            {"id": item.id, "reason": item.reason} for item in plan.malformed
        ],
        "scope": {
            "rootFilter": (
                "all projects"
                if plan.policy.project == "all"
                else "exact root-conversation member cwd match"
            ),
            "outsideScopeRootsEnumerated": True,
            "descendantsOutsideScopeBlockRoot": True,
        },
        "changesMade": False,
        "application": None,
    }
    document["integrity"] = document_integrity(document)
    return document


def iso_timestamp(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise CleanupError("preview timestamps must be timezone-aware")
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _shared_session_record(
    session: Session,
    group: GroupEvaluation,
    *,
    relation: str,
    include_titles: bool,
    scope_path: str | None,
) -> dict[str, Any]:
    issue_codes = _ordered_reason_codes(
        _issue_reason_code(issue)
        for issue in group.issues
        if issue.id == session.id
    )
    if (
        group.status == "excluded"
        and "related-outside-scope" in group.reasons
        and scope_path is not None
        and session.cwd != scope_path
    ):
        issue_codes = _ordered_reason_codes((*issue_codes, "related-outside-scope"))
    return {
        "session_id": session.id,
        "relation": relation,
        "parent_session_id": session.parent_id,
        "title": session.title if include_titles and session.title else None,
        "title_source": session.title_source,
        "project_path": session.cwd,
        "project_identity": "verified",
        "created_at": iso_timestamp(session.created_at),
        "last_activity": iso_timestamp(session.recency_at),
        "last_activity_source": "recencyAt",
        "bytes": None,
        "issues": [
            {"code": code, "subject": session.id} for code in issue_codes
        ],
        "extensions": {
            "codex": {
                "session_id": session.session_id,
                "archived": session.archived,
                "status": session.status,
                "active_flags": list(session.active_flags),
                "loaded": session.loaded,
                "ephemeral": session.ephemeral,
                "source_kind": session.source_kind,
            }
        },
    }


def shared_preview_document(
    plan: Plan,
    codex_home: str,
    *,
    codex_version: str,
    include_titles: bool = False,
) -> dict[str, Any]:
    """Render the shared stable-v1 preview; never use it as mutation authority."""
    group_counts = {
        "examined": len(plan.groups),
        "selected": 0,
        "retained": 0,
        "protected": 0,
        "excluded": 0,
    }
    session_counts = {
        "examined": sum(
            1 + len(group.tree.descendants) for group in plan.groups
        ),
        "selected": 0,
        "retained": 0,
        "protected": 0,
        "excluded": 0,
    }
    reason_counts: dict[str, dict[str, int]] = {
        status: {} for status in ("selected", "retained", "protected", "excluded")
    }
    groups: list[dict[str, Any]] = []
    for group in plan.groups:
        group_counts[group.status] += 1
        session_counts[group.status] += 1 + len(group.tree.descendants)
        for reason in group.reasons:
            reason_counts[group.status][reason] = (
                reason_counts[group.status].get(reason, 0) + 1
            )
        sessions = [
            _shared_session_record(
                group.tree.root,
                group,
                relation="root",
                include_titles=include_titles,
                scope_path=(
                    plan.policy.current_cwd
                    if plan.policy.project == "current"
                    else None
                ),
            )
        ]
        sessions.extend(
            _shared_session_record(
                item,
                group,
                relation="descendant",
                include_titles=include_titles,
                scope_path=(
                    plan.policy.current_cwd
                    if plan.policy.project == "current"
                    else None
                ),
            )
            for item in group.tree.descendants
        )
        groups.append(
            {
                "group_id": group.tree.root.id,
                "status": group.status,
                "reasons": list(group.reasons),
                "rank": group.rank,
                "proposed_action": (
                    plan.policy.action if group.status == "selected" else None
                ),
                "last_activity": iso_timestamp(group.activity),
                "last_activity_session_id": group.activity_session_id,
                "bytes": None,
                "session_count": len(sessions),
                "sessions": sessions,
                "mutation_blockers": [],
                "extensions": {
                    "codex": {"root_thread_id": group.tree.root.id}
                },
            }
        )

    null_counts = {
        "examined": None,
        "selected": None,
        "retained": None,
        "protected": None,
        "excluded": None,
    }
    cutoff = effective_cutoff(plan.policy, plan.now)
    return {
        "schema": "session-cleanup/preview",
        "schema_version": 1,
        "agent": "codex",
        "agent_version": sanitize_title(codex_version, limit=120) or None,
        "tool": {"name": "codex-session-cleanup", "version": TOOL_VERSION},
        "mode": "preview",
        "changes_made": False,
        "evaluated_at": iso_timestamp(plan.now),
        "scope": {
            "mode": plan.policy.project,
            "path": plan.policy.current_cwd if plan.policy.project == "current" else None,
        },
        "policy": {
            "older_than_seconds": (
                int(plan.policy.older_than.total_seconds())
                if plan.policy.older_than is not None
                else None
            ),
            "cutoff": iso_timestamp(cutoff) if cutoff is not None else None,
            "keep_latest": plan.policy.keep_latest,
            "min_idle_seconds": None,
            "combination": "intersection",
            "ranking_unit": "root-group",
            "requested_action": plan.policy.action,
        },
        "activity_timestamp": {
            "source": "codex-app-server:recencyAt",
            "kind": "agent-reported",
            "resolution": "1 second",
            "conservative": False,
        },
        "capabilities": {
            "actions": {
                "archive": {
                    "supported": True,
                    "reversibility": "agent-native",
                    "requires_saved_plan": False,
                    "unsupported_reason": None,
                },
                "quarantine": {
                    "supported": False,
                    "reversibility": "tool-managed",
                    "requires_saved_plan": False,
                    "unsupported_reason": "not-offered-by-backend",
                },
                "delete": {
                    "supported": True,
                    "reversibility": "irreversible",
                    "requires_saved_plan": True,
                    "unsupported_reason": None,
                },
            },
            "sizes": False,
            "titles": True,
            "pinning": True,
            "active_session_detection": "agent-reported",
        },
        "totals": {
            "groups": group_counts,
            "sessions": session_counts,
            "bytes": null_counts,
            "reasons": reason_counts,
        },
        "groups": groups,
        "warnings": [
            {
                "code": "state-db-only-discovery",
                "subject": None,
            },
            {
                "code": "standalone-app-server-load-visibility",
                "subject": None,
            },
            {
                "code": "non-atomic-native-mutation",
                "subject": None,
            },
            *(
                {
                    "code": "metadata-malformed",
                    "subject": item.id,
                }
                for item in plan.malformed
                if item.id
                not in {
                    member.id
                    for group in plan.groups
                    for member in (group.tree.root, *group.tree.descendants)
                }
            ),
        ],
        "extensions": {
            "codex": {
                "codex_home": normalize_invoking_cwd(codex_home),
                "discovery_mode": "state-db-only",
                "use_state_db_only": True,
                "archived_scope": (
                    "active-and-archived" if plan.policy.action == "delete" else "active"
                ),
            }
        },
    }


def document_integrity(document: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in document.items() if key != "integrity"}
    canonical = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def render_plan_json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)


def _parse_iso_timestamp(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CleanupError(f"{field} is not a UTC preview timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise CleanupError(f"{field} is malformed") from exc
    return parsed


def render_shared_preview(
    document: Mapping[str, Any], *, details: bool = False, applying: bool = False
) -> str:
    policy = document.get("policy", {})
    scope = document.get("scope", {})
    totals = document.get("totals", {})
    evaluated_at = _parse_iso_timestamp(document.get("evaluated_at"), "evaluated_at")
    action = policy.get("requested_action") if isinstance(policy, Mapping) else None
    lines = [
        "Codex session cleanup apply check" if applying else "Codex session cleanup preview",
        "",
        "Policy:",
        f"  project: {scope.get('mode') if isinstance(scope, Mapping) else 'unknown'}",
        f"  exact cwd: {scope.get('path') if isinstance(scope, Mapping) and scope.get('path') else 'all projects'}",
        f"  older than: {format_duration(dt.timedelta(seconds=policy['older_than_seconds']) if isinstance(policy, Mapping) and isinstance(policy.get('older_than_seconds'), int) else None)}",
        f"  cutoff: {policy.get('cutoff') if isinstance(policy, Mapping) and policy.get('cutoff') else 'not set'}",
        f"  keep latest conversations: {policy.get('keep_latest') if isinstance(policy, Mapping) and policy.get('keep_latest') is not None else 'not set'}",
        f"  action: {action}",
        "  ranking unit: root conversation (root plus descendants)",
    ]
    group_totals = totals.get("groups", {}) if isinstance(totals, Mapping) else {}
    session_totals = totals.get("sessions", {}) if isinstance(totals, Mapping) else {}
    lines.extend(
        [
            "",
            f"Groups examined: {group_totals.get('examined', 0)}",
            f"Selected: {group_totals.get('selected', 0)}",
            f"Retained: {group_totals.get('retained', 0)}",
            f"Protected: {group_totals.get('protected', 0)}",
            f"Excluded: {group_totals.get('excluded', 0)}",
            f"Sessions represented: {session_totals.get('examined', 0)}",
        ]
    )
    if action == "delete":
        lines.extend(
            [
                "",
                "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
                "WARNING: PERMANENT DELETION IS IRREVERSIBLE.",
                "Deletion requires saving and then applying a reviewed plan.",
                "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
            ]
        )

    groups = document.get("groups", [])
    for status in ("selected", "retained", "protected", "excluded"):
        status_groups = [
            group
            for group in groups
            if isinstance(group, Mapping) and group.get("status") == status
        ] if isinstance(groups, list) else []
        lines.extend(["", f"{status.capitalize()} conversations:"])
        if not status_groups:
            lines.append("  (none)")
            continue
        for group in status_groups:
            group_id = group.get("group_id")
            identifier = (
                group_id
                if details and isinstance(group_id, str)
                else short_id(group_id)
                if isinstance(group_id, str)
                else "unknown"
            )
            rank = group.get("rank")
            reasons = ", ".join(group.get("reasons", []))
            lines.append(
                f"  {identifier} rank={rank if rank is not None else '-'} "
                f"last-used={group.get('last_activity')} reasons={reasons}"
            )
            sessions = group.get("sessions", [])
            if not isinstance(sessions, list):
                continue
            for member in sessions:
                if not isinstance(member, Mapping):
                    continue
                session_id = member.get("session_id")
                member_id = (
                    session_id
                    if details and isinstance(session_id, str)
                    else short_id(session_id)
                    if isinstance(session_id, str)
                    else "unknown"
                )
                activity = _parse_iso_timestamp(
                    member.get("last_activity"), "last_activity"
                )
                title = sanitize_title(member.get("title"), limit=120) or "(untitled)"
                lines.append(
                    f"    {member.get('relation', 'member'):<10} "
                    f"{member_id:<36} {format_age(evaluated_at, activity):<6} "
                    f"{title}"
                )
                lines.append(f"               project: {member.get('project_path')}")
                parent = member.get("parent_session_id")
                if isinstance(parent, str):
                    lines.append(
                        "               parent: "
                        + (parent if details else short_id(parent))
                    )
                if details:
                    extension = member.get("extensions", {})
                    codex = extension.get("codex", {}) if isinstance(extension, Mapping) else {}
                    lines.append(
                        "               "
                        f"archived={codex.get('archived')} status={codex.get('status')} "
                        f"loaded={codex.get('loaded')} ephemeral={codex.get('ephemeral')} "
                        f"source={codex.get('source_kind')}"
                    )
    lines.extend(
        [
            "",
            (
                "State will be refreshed again before every root mutation."
                if applying
                else "No changes made."
            ),
        ]
    )
    return "\n".join(lines)


def _record_identifier(record: Mapping[str, Any], *, details: bool) -> str:
    value = record.get("id")
    if not isinstance(value, str):
        return "unknown"
    return value if details else short_id(value)


def _record_time(value: Any) -> dt.datetime:
    return timestamp(value, "recencyAt")


def _render_session_record(
    record: Mapping[str, Any],
    *,
    now: dt.datetime,
    details: bool,
    role: str,
) -> list[str]:
    recency = _record_time(record.get("recencyAt"))
    used = recency.astimezone().strftime("%Y-%m-%d %H:%M")
    title_limit = 160 if details else 72
    title = sanitize_title(record.get("title"), limit=title_limit) or "(untitled)"
    archived = "archived" if record.get("archived") is True else "active"
    identifier = _record_identifier(record, details=details)
    line = (
        f"  {role:<10} {used}  {format_age(now, recency):<6} "
        f"{identifier:<36} [{archived}] {title}"
    )
    lines = [line.rstrip()]
    parent_id = record.get("parentId")
    if isinstance(parent_id, str):
        parent = parent_id if details else short_id(parent_id)
        lines.append(f"               parent: {parent}")
    project = sanitize_title(record.get("cwd"), limit=300) or "unknown"
    lines.append(f"               project: {project}")
    if details:
        flags = record.get("activeFlags") or []
        lines.append(
            "               "
            f"status: {record.get('status')} loaded={record.get('loaded')} "
            f"pinned={record.get('pinned')} source={record.get('sourceKind')} "
            f"flags={','.join(flags) if isinstance(flags, list) else 'invalid'}"
        )
    return lines


def render_plan(
    document: Mapping[str, Any], *, details: bool = False, applying: bool = False
) -> str:
    policy = document.get("policy", {})
    summary = document.get("summary", {})
    created_at = timestamp(document.get("createdAt"), "createdAt")
    action = policy.get("action")
    verb = "delete" if action == "delete" else "archive"
    older_seconds = policy.get("olderThanSeconds")
    older_duration = (
        dt.timedelta(seconds=older_seconds)
        if isinstance(older_seconds, int) and not isinstance(older_seconds, bool)
        else None
    )
    cutoff = policy.get("absoluteCutoff")
    cutoff_text = (
        timestamp(cutoff, "absoluteCutoff").isoformat()
        if isinstance(cutoff, int) and not isinstance(cutoff, bool)
        else "not set"
    )
    lines = [
        "Codex session cleanup apply check" if applying else "Codex session cleanup preview",
        "",
        "Policy:",
        f"  project: {policy.get('project')}",
        f"  exact cwd: {sanitize_title(policy.get('currentCwd'), limit=300) or 'unknown'}",
        f"  older than: {format_duration(older_duration)}",
        f"  absolute cutoff: {cutoff_text}",
        f"  keep latest: {policy.get('keepLatest') if policy.get('keepLatest') is not None else 'not set'}",
        f"  action: {action}",
        "  latest-N ranking: roots and descendants in the selected project scope",
        "",
        f"Examined roots: {summary.get('examinedRoots', 0)}",
        f"Examined descendants: {summary.get('examinedDescendants', 0)}",
        f"Selected roots: {summary.get('candidateRoots', 0)}",
        f"Cascaded descendants: {summary.get('candidateDescendants', 0)}",
        f"Protected/excluded records: {summary.get('protected', 0)}",
        f"Protected/excluded sessions: {summary.get('protectedSessions', 0)}",
        f"Malformed records: {summary.get('malformed', 0)}",
    ]
    if action == "delete":
        lines.extend(
            [
                "",
                "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
                "WARNING: PERMANENT DELETION IS IRREVERSIBLE.",
                "Deletion requires saving and then applying a reviewed plan.",
                "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
            ]
        )

    candidates = document.get("candidates", [])
    lines.extend(["", f"Selected for {verb}:"])
    if not candidates:
        lines.append("  (none)")
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        root = candidate.get("root")
        if isinstance(root, Mapping):
            lines.extend(
                _render_session_record(
                    root, now=created_at, details=details, role="root"
                )
            )
        descendants = candidate.get("descendants", [])
        if isinstance(descendants, list):
            for item in descendants:
                if isinstance(item, Mapping):
                    lines.extend(
                        _render_session_record(
                            item,
                            now=created_at,
                            details=details,
                            role="descendant",
                        )
                    )

    lines.extend(["", "Protected or excluded:"])
    protected = document.get("protected", [])
    if not protected:
        lines.append("  (none)")
    for item in protected:
        if not isinstance(item, Mapping):
            continue
        target_id = item.get("targetId")
        identifier = (
            target_id
            if details and isinstance(target_id, str)
            else short_id(target_id)
            if isinstance(target_id, str)
            else "unknown"
        )
        category = item.get("category", "unknown")
        reason = sanitize_title(item.get("reason"), limit=240)
        lines.append(f"  {identifier} [{category}] {reason}")
        session = item.get("session")
        if isinstance(session, Mapping):
            lines.extend(
                _render_session_record(
                    session,
                    now=created_at,
                    details=details,
                    role="protected",
                )
            )
        blocker = item.get("blocker")
        if isinstance(blocker, Mapping):
            blocker_id = _record_identifier(blocker, details=details)
            blocker_title = sanitize_title(blocker.get("title"), limit=80) or "(untitled)"
            lines.append(f"    blocker: {blocker_id} {blocker_title}")
        descendants = item.get("descendants", [])
        if isinstance(descendants, list):
            for descendant in descendants:
                if isinstance(descendant, Mapping):
                    lines.extend(
                        _render_session_record(
                            descendant,
                            now=created_at,
                            details=details,
                            role="descendant",
                        )
                    )

    scope = document.get("scope", {})
    if (
        isinstance(scope, Mapping)
        and scope.get("rootFilter") == "exact root-conversation member cwd match"
    ):
        lines.extend(
            [
                "",
                "Scope exclusion:",
                "  A conversation is in scope when any member exactly matches the cwd.",
                "  Any related member outside that cwd excludes the conversation.",
            ]
        )

    if applying:
        lines.extend(["", "State will be refreshed again before every root mutation."])
    else:
        lines.extend(["", "No changes made."])
    return "\n".join(lines)


DOCUMENT_KEYS = {
    "format",
    "version",
    "planId",
    "state",
    "createdAt",
    "codexHome",
    "codexVersion",
    "policy",
    "summary",
    "candidates",
    "protected",
    "malformed",
    "scope",
    "changesMade",
    "application",
    "integrity",
}
SESSION_RECORD_KEYS = {
    "id",
    "title",
    "createdAt",
    "updatedAt",
    "recencyAt",
    "cwd",
    "pinned",
    "ephemeral",
    "loaded",
    "status",
    "activeFlags",
    "archived",
    "parentId",
    "sessionId",
    "sourceKind",
}


def _require_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ManifestError(
            f"{label} fields are invalid (missing={missing}, extra={extra})"
        )


def _session_from_record(raw: Any, label: str) -> Session:
    if not isinstance(raw, Mapping):
        raise ManifestError(f"{label} is not an object")
    _require_keys(raw, SESSION_RECORD_KEYS, label)
    title = raw.get("title")
    if not isinstance(title, str) or sanitize_title(title) != title:
        raise ManifestError(f"{label}.title is not safely encoded")
    cwd = raw.get("cwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise ManifestError(f"{label}.cwd is not absolute")
    for field in ("pinned", "ephemeral", "loaded", "archived"):
        if not isinstance(raw.get(field), bool):
            raise ManifestError(f"{label}.{field} is not boolean")
    status_value = raw.get("status")
    if not isinstance(status_value, str) or not status_value:
        raise ManifestError(f"{label}.status is invalid")
    flags = raw.get("activeFlags")
    if not isinstance(flags, list) or not all(isinstance(item, str) for item in flags):
        raise ManifestError(f"{label}.activeFlags is invalid")
    source_value = raw.get("sourceKind")
    if not isinstance(source_value, str) or not source_value:
        raise ManifestError(f"{label}.sourceKind is invalid")
    try:
        return Session(
            id=canonical_uuid(raw.get("id"), f"{label}.id"),
            title=title,
            created_at=timestamp(raw.get("createdAt"), f"{label}.createdAt"),
            updated_at=timestamp(raw.get("updatedAt"), f"{label}.updatedAt"),
            recency_at=timestamp(raw.get("recencyAt"), f"{label}.recencyAt"),
            cwd=normalize_invoking_cwd(cwd),
            pinned=raw["pinned"],
            ephemeral=raw["ephemeral"],
            status=status_value,
            active_flags=tuple(flags),
            archived=raw["archived"],
            parent_id=optional_uuid(raw.get("parentId"), f"{label}.parentId"),
            session_id=canonical_uuid(raw.get("sessionId"), f"{label}.sessionId"),
            source_kind=source_value,
            loaded=raw["loaded"],
        )
    except (ValueError, CleanupError) as exc:
        raise ManifestError(str(exc)) from exc


def _policy_from_record(raw: Any) -> Policy:
    if not isinstance(raw, Mapping):
        raise ManifestError("policy is not an object")
    expected = {
        "olderThanSeconds",
        "absoluteCutoff",
        "keepLatest",
        "project",
        "currentCwd",
        "action",
        "latestRankingUniverse",
    }
    _require_keys(raw, expected, "policy")
    older_seconds = raw.get("olderThanSeconds")
    keep_latest = raw.get("keepLatest")
    cutoff_value = raw.get("absoluteCutoff")
    if older_seconds is not None and (
        isinstance(older_seconds, bool)
        or not isinstance(older_seconds, int)
        or older_seconds <= 0
    ):
        raise ManifestError("policy.olderThanSeconds is invalid")
    if keep_latest is not None and (
        isinstance(keep_latest, bool)
        or not isinstance(keep_latest, int)
        or keep_latest <= 0
    ):
        raise ManifestError("policy.keepLatest is invalid")
    if older_seconds is None and keep_latest is None:
        raise ManifestError("manifest has no retention selector")
    if (older_seconds is None) != (cutoff_value is None):
        raise ManifestError("policy absolute cutoff does not match age selector")
    cutoff = (
        timestamp(cutoff_value, "policy.absoluteCutoff")
        if cutoff_value is not None
        else None
    )
    project = raw.get("project")
    action = raw.get("action")
    cwd = raw.get("currentCwd")
    if project not in ("current", "all"):
        raise ManifestError("policy.project is invalid")
    if action not in ("archive", "delete"):
        raise ManifestError("policy.action is invalid")
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise ManifestError("policy.currentCwd is not absolute")
    accepted_rankings = {
        (
            "ordinary interactive roots and all discoverable spawned descendants "
            "within the selected project scope"
        ),
        (
            "non-excluded root conversations within the selected project scope; "
            "protected conversations still rank"
        ),
    }
    if raw.get("latestRankingUniverse") not in accepted_rankings:
        raise ManifestError("policy.latestRankingUniverse is unsupported")
    return Policy(
        older_than=(
            dt.timedelta(seconds=older_seconds) if older_seconds is not None else None
        ),
        keep_latest=keep_latest,
        project=project,
        action=action,
        current_cwd=normalize_invoking_cwd(cwd),
        cutoff=cutoff,
    )


def _manifest_candidates(raw: Any, policy: Policy) -> tuple[Candidate, ...]:
    if not isinstance(raw, list):
        raise ManifestError("candidates is not an array")
    candidates: list[Candidate] = []
    all_ids: set[str] = set()
    for index, value in enumerate(raw):
        if not isinstance(value, Mapping):
            raise ManifestError(f"candidates[{index}] is not an object")
        _require_keys(value, {"root", "descendants"}, f"candidates[{index}]")
        root = _session_from_record(value.get("root"), f"candidates[{index}].root")
        if root.parent_id is not None:
            raise ManifestError("candidate root has a parent")
        descendants_raw = value.get("descendants")
        if not isinstance(descendants_raw, list):
            raise ManifestError(f"candidates[{index}].descendants is not an array")
        descendants = tuple(
            _session_from_record(item, f"candidates[{index}].descendants[{child_index}]")
            for child_index, item in enumerate(descendants_raw)
        )
        issues = validate_descendant_tree(root, descendants)
        if issues:
            raise ManifestError(f"candidate descendant tree is invalid: {issues[0].reason}")
        ordered = _ordered_descendants(root, descendants)
        if descendants != ordered:
            raise ManifestError("candidate descendants are not deterministically ordered")
        for session in (root, *descendants):
            if session.id in all_ids:
                raise ManifestError("manifest repeats a candidate session id")
            all_ids.add(session.id)
            if _unsafe_reason(session) is not None:
                raise ManifestError("manifest contains an unsafe candidate")
            if policy.project == "current" and session.cwd != policy.current_cwd:
                raise ManifestError("manifest candidate crosses the exact project boundary")
        candidates.append(Candidate(root, descendants))
    if tuple(candidates) != tuple(sorted(candidates, key=lambda item: _session_sort_key(item.root))):
        raise ManifestError("candidate roots are not deterministically ordered")
    return tuple(candidates)


def _private_manifest_path(path: Path, *, must_exist: bool) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if must_exist:
        try:
            metadata = os.lstat(absolute)
        except OSError as exc:
            raise ManifestError(f"could not inspect plan manifest: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ManifestError("plan manifest must be a regular non-symlink file")
        if os.name != "nt" and metadata.st_mode & 0o077:
            raise ManifestError("plan manifest permissions must not allow group/other access")
    return absolute


def require_manifest_outside_codex_home(path: Path, codex_home: str) -> None:
    """Keep helper-owned plan files out of Codex-owned state directories."""
    manifest_real = normalize_invoking_cwd(
        os.path.realpath(os.path.abspath(os.fspath(path)))
    )
    home_real = normalize_invoking_cwd(
        os.path.realpath(normalize_invoking_cwd(codex_home))
    )
    try:
        inside_home = os.path.commonpath((manifest_real, home_real)) == home_real
    except ValueError:
        inside_home = False
    if inside_home:
        raise ManifestError("plan manifests must be stored outside Codex home")


def load_saved_plan(path: Path) -> SavedPlan:
    absolute = _private_manifest_path(path, must_exist=True)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(absolute, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ManifestError("plan manifest must be a regular file")
        if os.name != "nt" and metadata.st_mode & 0o077:
            raise ManifestError(
                "plan manifest permissions must not allow group/other access"
            )
        if metadata.st_size > 10 * 1024 * 1024:
            raise ManifestError("plan manifest is unexpectedly large")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = None
            serialized = handle.read(10 * 1024 * 1024 + 1)
        if len(serialized) > 10 * 1024 * 1024:
            raise ManifestError("plan manifest is unexpectedly large")
        raw = json.loads(serialized)
    except ManifestError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"could not read plan manifest: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(raw, dict):
        raise ManifestError("plan manifest is not a JSON object")
    _require_keys(raw, DOCUMENT_KEYS, "manifest")
    if raw.get("format") != PLAN_FORMAT or raw.get("version") != PLAN_VERSION:
        raise ManifestError("plan manifest format/version is unsupported")
    integrity = raw.get("integrity")
    if not isinstance(integrity, str) or integrity != document_integrity(raw):
        raise ManifestError(
            "plan manifest integrity check failed; generate a fresh preview"
        )
    try:
        canonical_uuid(raw.get("planId"), "planId")
        created_at = timestamp(raw.get("createdAt"), "createdAt")
    except ValueError as exc:
        raise ManifestError(str(exc)) from exc
    if raw.get("state") != "ready":
        raise ManifestError(
            "plan manifest is not ready or was already consumed; create a fresh preview"
        )
    if raw.get("changesMade") is not False or raw.get("application") is not None:
        raise ManifestError("ready plan manifest contains application state")
    codex_home = raw.get("codexHome")
    if not isinstance(codex_home, str) or not os.path.isabs(codex_home):
        raise ManifestError("manifest codexHome is not absolute")
    codex_version = raw.get("codexVersion")
    if (
        not isinstance(codex_version, str)
        or not codex_version
        or sanitize_title(codex_version, limit=120) != codex_version
    ):
        raise ManifestError("manifest codexVersion is invalid")
    for field in ("summary", "scope"):
        if not isinstance(raw.get(field), Mapping):
            raise ManifestError(f"manifest {field} is invalid")
    for field in ("protected", "malformed"):
        if not isinstance(raw.get(field), list):
            raise ManifestError(f"manifest {field} is invalid")
    policy = _policy_from_record(raw.get("policy"))
    if (
        policy.older_than is not None
        and policy.cutoff != created_at - policy.older_than
    ):
        raise ManifestError(
            "policy absolute cutoff is inconsistent with plan creation time"
        )
    candidates = _manifest_candidates(raw.get("candidates"), policy)
    summary = raw["summary"]
    if summary.get("candidateRoots") != len(candidates) or summary.get(
        "candidateDescendants"
    ) != sum(len(item.descendants) for item in candidates):
        raise ManifestError("manifest summary does not match its candidate set")
    return SavedPlan(
        path=absolute,
        document=raw,
        policy=policy,
        codex_home=normalize_invoking_cwd(codex_home),
        codex_version=codex_version,
        candidates=candidates,
    )


def save_plan_manifest(path: Path, document: Mapping[str, Any]) -> Path:
    absolute = _private_manifest_path(path, must_exist=False)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(absolute, flags, 0o600)
    except OSError as exc:
        raise ManifestError(f"could not create plan manifest: {exc}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise ManifestError(f"could not write plan manifest: {exc}") from exc
    return absolute


def update_plan_manifest(
    saved: SavedPlan,
    *,
    state: str,
    application: Mapping[str, Any],
) -> SavedPlan:
    if state not in ("applying", "consumed", "stale"):
        raise ManifestError("unsupported plan manifest state transition")
    document = dict(saved.document)
    document["state"] = state
    document["application"] = dict(application)
    document["changesMade"] = bool(application.get("successfulRoots"))
    document["integrity"] = document_integrity(document)
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".codex-session-cleanup-plan-",
            suffix=".tmp",
            dir=str(saved.path.parent),
        )
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, saved.path)
    except OSError as exc:
        raise ManifestError(f"could not update plan manifest state: {exc}") from exc
    return dataclasses.replace(saved, document=document)


class CleanupLock:
    """Non-blocking advisory lock for cooperating cleanup processes."""

    def __init__(self, codex_home: str) -> None:
        identity = hashlib.sha256(
            normalize_invoking_cwd(codex_home).encode("utf-8")
        ).hexdigest()[:24]
        self.path = Path(tempfile.gettempdir()) / f"codex-session-cleanup-{identity}.lock"
        self.descriptor: int | None = None

    def __enter__(self) -> "CleanupLock":
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            self.descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise LockError("could not open the cleanup advisory lock safely") from exc
        try:
            if not stat.S_ISREG(os.fstat(self.descriptor).st_mode):
                raise LockError("the cleanup advisory lock is not a regular file")
            if fcntl is not None:
                fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:  # pragma: no cover - Windows only
                if os.fstat(self.descriptor).st_size == 0:
                    os.write(self.descriptor, b"0")
                os.lseek(self.descriptor, 0, os.SEEK_SET)
                msvcrt.locking(self.descriptor, msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - unsupported platform
                raise LockError("this platform has no supported advisory locking API")
        except LockError:
            os.close(self.descriptor)
            self.descriptor = None
            raise
        except (BlockingIOError, OSError) as exc:
            os.close(self.descriptor)
            self.descriptor = None
            raise LockError(
                "another codex-session-cleanup apply operation is already running"
            ) from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.descriptor is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows only
                os.lseek(self.descriptor, 0, os.SEEK_SET)
                msvcrt.locking(self.descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(self.descriptor)
            self.descriptor = None


def mutate_candidates(
    candidates: Sequence[Candidate],
    action: str,
    codex_bin: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    revalidate: Callable[[Sequence[Candidate]], None] | None = None,
) -> MutationReport:
    if action not in ("archive", "delete"):
        raise CleanupError("unsupported mutation action")
    successful: list[str] = []
    skipped: list[TargetFailure] = []
    failed: list[TargetFailure] = []
    remaining = list(candidates)
    stopped_reason: str | None = None
    while remaining:
        candidate = remaining[0]
        if revalidate is not None:
            try:
                revalidate(tuple(remaining))
            except CleanupError as exc:
                stopped_reason = str(exc)
                skipped.extend(
                    TargetFailure(item.root.id, f"not attempted: {stopped_reason}")
                    for item in remaining
                )
                break
        try:
            session_id = canonical_uuid(candidate.root.id)
        except ValueError as exc:
            skipped.append(TargetFailure(candidate.root.id, str(exc)))
            stopped_reason = "candidate UUID validation failed"
            skipped.extend(
                TargetFailure(item.root.id, "not attempted after validation failure")
                for item in remaining[1:]
            )
            break
        command = [codex_bin, action, session_id]
        if action == "delete":
            command.append("--force")
        try:
            completed = runner(command, capture_output=True, text=True, check=False)
        except OSError as exc:
            failed.append(TargetFailure(session_id, str(exc)))
            stopped_reason = "Codex mutation command could not start"
            skipped.extend(
                TargetFailure(item.root.id, "not attempted after command failure")
                for item in remaining[1:]
            )
            break
        if completed.returncode == 0:
            successful.append(session_id)
            remaining.pop(0)
            continue
        detail = (completed.stderr or completed.stdout or "Codex command failed").strip()
        failed.append(TargetFailure(session_id, detail[:500]))
        stopped_reason = "Codex refused or failed a mutation; later targets were not attempted"
        skipped.extend(
            TargetFailure(item.root.id, "not attempted after prior mutation failure")
            for item in remaining[1:]
        )
        break
    return MutationReport(
        tuple(successful), tuple(skipped), tuple(failed), stopped_reason
    )


def render_mutation_report(report: MutationReport, action: str) -> str:
    lines = [
        "Codex session cleanup result",
        "",
        f"Action: {'irreversible delete' if action == 'delete' else 'archive'}",
        f"Successful targets: {len(report.successful)}",
        f"Skipped targets: {len(report.skipped)}",
        f"Failed targets: {len(report.failed)}",
    ]
    if report.stopped_reason:
        lines.append(f"Stopped: {sanitize_title(report.stopped_reason, limit=300)}")
    for label, failures in (("Skipped", report.skipped), ("Failed", report.failed)):
        if failures:
            lines.extend(["", f"{label}:"])
            lines.extend(
                f"  {short_id(item.session_id)} "
                f"{sanitize_title(item.error, limit=500)}"
                for item in failures
            )
    return "\n".join(lines)


def mutation_report_record(report: MutationReport, action: str) -> dict[str, Any]:
    return {
        "action": action,
        "successfulRoots": list(report.successful),
        "skipped": [dataclasses.asdict(item) for item in report.skipped],
        "failed": [dataclasses.asdict(item) for item in report.failed],
        "stoppedReason": report.stopped_reason,
    }


def find_codex(*, environment: Mapping[str, str] | None = None) -> str:
    environment = os.environ if environment is None else environment
    value = shutil.which("codex", path=environment.get("PATH"))
    if value is not None:
        return value

    package_root = environment.get("CODEX_MANAGED_PACKAGE_ROOT")
    if package_root:
        root = Path(package_root)
        executable_names = ("codex.exe",) if os.name == "nt" else ("codex", "codex.js")
        if root.is_absolute():
            for executable_name in executable_names:
                candidate = root / "bin" / executable_name
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)

    raise CompatibilityError(
        "codex executable was not found on PATH or at "
        "a supported $CODEX_MANAGED_PACKAGE_ROOT/bin entry point"
    )


def installed_codex_version(
    codex_bin: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    completed = runner(
        [codex_bin, "--version"], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise CompatibilityError("could not read installed Codex version")
    value = (completed.stdout or completed.stderr).strip()
    if not value:
        raise CompatibilityError("installed Codex returned an empty version")
    return sanitize_title(value, limit=120)


def _make_policy(args: argparse.Namespace, current_cwd: str) -> Policy:
    return Policy(
        older_than=args.older_than,
        keep_latest=args.keep_latest,
        project=args.project,
        action=args.action,
        current_cwd=current_cwd,
    )


def _manifest_application(
    *,
    started_at: dt.datetime,
    report: MutationReport | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "startedAt": int(started_at.timestamp()),
        "completedAt": int(dt.datetime.now(dt.timezone.utc).timestamp()),
        "successfulRoots": list(report.successful) if report is not None else [],
        "skipped": (
            [dataclasses.asdict(item) for item in report.skipped]
            if report is not None
            else []
        ),
        "failed": (
            [dataclasses.asdict(item) for item in report.failed]
            if report is not None
            else []
        ),
        "stoppedReason": report.stopped_reason if report is not None else reason,
    }
    return value


def _run_compatibility_check(
    args: argparse.Namespace,
    codex_bin: str,
    capabilities: SchemaCapabilities,
) -> int:
    version = installed_codex_version(codex_bin)
    with AppServerClient(codex_bin) as client:
        discovery = SessionDiscovery(client, capabilities)
        pinned = discovery.discover_pinned_section()
        loaded = discovery.discover_loaded_ids()
        result = {
            "compatible": True,
            "codexVersion": version,
            "codexHomeResolved": client.codex_home is not None,
            "pinnedSectionResolved": bool(pinned.id),
            "loadedThreadCount": len(loaded),
            "changesMade": False,
        }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Codex session cleanup compatibility check")
        print(f"Codex version: {version}")
        print("Schema contract: compatible")
        print("Pinned section: resolved")
        print("Loaded-thread API: available")
        print("Archive/delete commands: available")
        print("No changes made.")
    return 0


def _run_policy(
    args: argparse.Namespace,
    codex_bin: str,
    capabilities: SchemaCapabilities,
) -> int:
    current_cwd = normalize_invoking_cwd(os.getcwd())
    policy = _make_policy(args, current_cwd)
    with AppServerClient(codex_bin) as client:
        if client.codex_home is None:
            raise ProtocolError("app-server did not resolve Codex home")
        discovery = SessionDiscovery(client, capabilities)
        plan = discover_plan(discovery, policy, now=dt.datetime.now(dt.timezone.utc))
        codex_version = installed_codex_version(codex_bin)
        document_state = "ready" if args.save_plan is not None else "preview"
        manifest = plan_document(
            plan,
            client.codex_home,
            codex_version=codex_version,
            state=document_state,
        )
        preview = shared_preview_document(
            plan,
            client.codex_home,
            codex_version=codex_version,
            include_titles=args.include_titles,
        )
        saved_path: Path | None = None
        if args.save_plan is not None:
            require_manifest_outside_codex_home(args.save_plan, client.codex_home)
            saved_path = save_plan_manifest(args.save_plan, manifest)

        if not args.apply:
            if args.json:
                print(render_plan_json(preview))
            else:
                human_preview = shared_preview_document(
                    plan,
                    client.codex_home,
                    codex_version=codex_version,
                    include_titles=True,
                )
                print(render_shared_preview(human_preview, details=args.details))
                if saved_path is not None:
                    safe_path = sanitize_title(str(saved_path), limit=500)
                    print(f"\nSaved reviewable plan: {safe_path}")
                    print(
                        "Apply exactly this set with: "
                        "python3 scripts/cleanup_sessions.py --apply-plan "
                        f"{shlex.quote(safe_path)}"
                    )
            return 0

        human_preview = shared_preview_document(
            plan,
            client.codex_home,
            codex_version=codex_version,
            include_titles=True,
        )
        print(render_shared_preview(human_preview, details=args.details, applying=True))
        if not plan.candidates:
            print("\nNo matching sessions. No changes made.")
            return 0

        with CleanupLock(client.codex_home):
            refreshed = discover_plan(
                discovery,
                plan.policy,
                now=dt.datetime.now(dt.timezone.utc),
            )
            require_fresh_plan(plan, refreshed)

            def revalidate(remaining: Sequence[Candidate]) -> None:
                current = discover_plan(
                    discovery,
                    plan.policy,
                    now=dt.datetime.now(dt.timezone.utc),
                )
                require_candidate_match(remaining, current.candidates)

            report = mutate_candidates(
                plan.candidates,
                plan.policy.action,
                codex_bin,
                revalidate=revalidate,
            )

    print("\n" + render_mutation_report(report, plan.policy.action))
    return EXIT_ERROR if report.failed or report.skipped else 0


def _run_saved_plan(
    args: argparse.Namespace,
    codex_bin: str,
    capabilities: SchemaCapabilities,
) -> int:
    assert args.apply_plan is not None
    with AppServerClient(codex_bin) as client:
        if client.codex_home is None:
            raise ProtocolError("app-server did not resolve Codex home")
        require_manifest_outside_codex_home(args.apply_plan, client.codex_home)
        with CleanupLock(client.codex_home):
            saved = load_saved_plan(args.apply_plan)
            if saved.codex_home != normalize_invoking_cwd(client.codex_home):
                raise ManifestError(
                    "plan belongs to a different Codex home; generate a fresh preview"
                )
            codex_version = installed_codex_version(codex_bin)
            if saved.codex_version != codex_version:
                raise ManifestError(
                    "installed Codex version differs from the saved plan; "
                    "generate a fresh preview"
                )
            invoking_cwd = normalize_invoking_cwd(os.getcwd())
            if (
                saved.policy.project == "current"
                and invoking_cwd != saved.policy.current_cwd
            ):
                raise ManifestError(
                    "current directory differs from the approved exact-project cwd"
                )

            discovery = SessionDiscovery(client, capabilities)
            started_at = dt.datetime.now(dt.timezone.utc)
            current = discover_plan(discovery, saved.policy, now=started_at)
            try:
                require_candidate_match(saved.candidates, current.candidates)
            except StaleStateError as exc:
                update_plan_manifest(
                    saved,
                    state="stale",
                    application=_manifest_application(
                        started_at=started_at, reason=str(exc)
                    ),
                )
                raise

            print(
                render_shared_preview(
                    shared_preview_document(
                        current,
                        client.codex_home,
                        codex_version=codex_version,
                        include_titles=True,
                    ),
                    details=args.details,
                    applying=True,
                )
            )

            saved = update_plan_manifest(
                saved,
                state="applying",
                application={
                    "startedAt": int(started_at.timestamp()),
                    "completedAt": None,
                    "successfulRoots": [],
                    "skipped": [],
                    "failed": [],
                    "stoppedReason": None,
                },
            )

            def revalidate(remaining: Sequence[Candidate]) -> None:
                latest = discover_plan(
                    discovery,
                    saved.policy,
                    now=dt.datetime.now(dt.timezone.utc),
                )
                require_candidate_match(remaining, latest.candidates)

            report = mutate_candidates(
                saved.candidates,
                saved.policy.action,
                codex_bin,
                revalidate=revalidate,
            )
            saved = update_plan_manifest(
                saved,
                state="consumed",
                application=_manifest_application(
                    started_at=started_at, report=report
                ),
            )

    print("\n" + render_mutation_report(report, saved.policy.action))
    print(f"Plan state: {saved.document['state']}")
    return EXIT_ERROR if report.failed or report.skipped else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        codex_bin = find_codex()
        capabilities = inspect_installed_schema(codex_bin)
        inspect_mutation_commands(codex_bin)
        if args.check_compatibility:
            return _run_compatibility_check(args, codex_bin, capabilities)
        if args.apply_plan is not None:
            return _run_saved_plan(args, codex_bin, capabilities)
        return _run_policy(args, codex_bin, capabilities)
    except StaleStateError as exc:
        print(f"Stale plan: {sanitize_title(str(exc), limit=1000)}", file=sys.stderr)
        print("No further changes made. Generate and review a fresh preview.", file=sys.stderr)
        return EXIT_STALE_PLAN
    except ManifestError as exc:
        print(f"Invalid plan: {sanitize_title(str(exc), limit=1000)}", file=sys.stderr)
        print("No changes made.", file=sys.stderr)
        return EXIT_INVALID_PLAN
    except LockError as exc:
        print(f"Safety stop: {sanitize_title(str(exc), limit=1000)}", file=sys.stderr)
        print("No changes made.", file=sys.stderr)
        return EXIT_LOCKED
    except CleanupError as exc:
        print(f"Safety stop: {sanitize_title(str(exc), limit=1000)}", file=sys.stderr)
        print("No changes made.", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
