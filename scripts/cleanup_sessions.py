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
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence
import uuid


INTERACTIVE_SOURCES = ("cli", "vscode")
SAFE_STATUSES = ("notLoaded", "idle")
DURATION_RE = re.compile(r"([1-9][0-9]*)([hd])\Z")


class CleanupError(RuntimeError):
    """Base class for safe operational failures."""


class CompatibilityError(CleanupError):
    """The installed Codex interface lacks a required safety contract."""


class ProtocolError(CleanupError):
    """The app-server returned an invalid or unsuccessful response."""


class StaleStateError(CleanupError):
    """Session state changed between the apply plan and final safety check."""


@dataclasses.dataclass(frozen=True)
class Policy:
    older_than: dt.timedelta | None
    keep_latest: int | None
    project: str
    action: str
    current_cwd: str


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

    @property
    def unsafe(self) -> bool:
        return self.ephemeral or self.status not in SAFE_STATUSES


@dataclasses.dataclass(frozen=True)
class UnsafeRecord:
    id: str | None
    reason: str


@dataclasses.dataclass(frozen=True)
class Candidate:
    root: Session
    descendants: tuple[Session, ...] = ()


@dataclasses.dataclass(frozen=True)
class Plan:
    policy: Policy
    now: dt.datetime
    observed: tuple[Session, ...]
    malformed: tuple[UnsafeRecord, ...]
    protected_recent: tuple[Session, ...]
    protected_pinned: tuple[Session, ...]
    protected_unsafe: tuple[UnsafeRecord, ...]
    candidates: tuple[Candidate, ...]

    @property
    def examined(self) -> int:
        return len(self.observed) + len(self.malformed)


@dataclasses.dataclass(frozen=True)
class SchemaCapabilities:
    all_source_kinds: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class TargetFailure:
    session_id: str
    error: str


@dataclasses.dataclass(frozen=True)
class MutationReport:
    successful: tuple[str, ...]
    skipped: tuple[TargetFailure, ...]
    failed: tuple[TargetFailure, ...]


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
    )
    parser.add_argument("--older-than", type=parse_duration, metavar="DURATION")
    parser.add_argument("--keep-latest", type=positive_int, metavar="N")
    parser.add_argument("--project", choices=("current", "all"), default="current")
    parser.add_argument("--action", choices=("archive", "delete"), default="archive")
    parser.add_argument("--apply", action="store_true", help="apply after a fresh safety check")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argument_parser()
    args = parser.parse_args(argv)
    if args.older_than is None and args.keep_latest is None:
        parser.error("at least one of --older-than or --keep-latest is required")
    return args


def normalize_invoking_cwd(path: str) -> str:
    if not os.path.isabs(path):
        raise CleanupError("the invoking working directory must be absolute")
    return os.path.normcase(os.path.normpath(path))


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


def parse_thread(raw: Mapping[str, Any], *, archived: bool) -> Session:
    """Validate a thread object without substituting missing safety metadata."""
    session_id = canonical_uuid(raw.get("id"))
    cwd = raw.get("cwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise ValueError("cwd is missing or is not absolute")
    cwd = normalize_invoking_cwd(cwd)

    pinned = raw.get("isPinned")
    if not isinstance(pinned, bool):
        raise ValueError("isPinned is missing or is not boolean")
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

    name = raw.get("name")
    preview = raw.get("preview")
    title = name if isinstance(name, str) and name else preview if isinstance(preview, str) else ""

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
    )


def _session_sort_key(session: Session) -> tuple[float, str]:
    return (-session.recency_at.timestamp(), session.id)


def build_plan(
    sessions: Iterable[Session],
    policy: Policy,
    *,
    now: dt.datetime,
    malformed: Iterable[UnsafeRecord] = (),
    descendant_lookup: Callable[[Session], tuple[Sequence[Session], Sequence[UnsafeRecord]]] | None = None,
) -> Plan:
    """Apply retention and safety rules to already validated metadata."""
    if policy.older_than is None and policy.keep_latest is None:
        raise CleanupError("at least one retention selector is required")
    if now.tzinfo is None:
        raise CleanupError("the planning clock must be timezone-aware")

    current_cwd = normalize_invoking_cwd(policy.current_cwd)
    scoped = [
        item
        for item in sessions
        if policy.project == "all" or item.cwd == current_cwd
    ]
    scoped.sort(key=_session_sort_key)
    malformed_items = tuple(malformed)

    latest_ids = {
        item.id for item in scoped[: policy.keep_latest]
    } if policy.keep_latest is not None else set()
    cutoff = now - policy.older_than if policy.older_than is not None else None

    protected_recent: list[Session] = []
    protected_pinned: list[Session] = []
    protected_unsafe: list[UnsafeRecord] = list(malformed_items)
    base_candidates: list[Session] = []

    for item in scoped:
        if item.pinned:
            protected_pinned.append(item)
            continue
        if item.unsafe:
            protected_unsafe.append(UnsafeRecord(item.id, f"unsafe runtime state: {item.status}"))
            continue
        expired = cutoff is None or item.recency_at < cutoff
        outside_latest = policy.keep_latest is None or item.id not in latest_ids
        if expired and outside_latest:
            base_candidates.append(item)
        else:
            protected_recent.append(item)

    candidates: list[Candidate] = []
    for root in base_candidates:
        descendants: Sequence[Session] = ()
        descendant_issues: Sequence[UnsafeRecord] = ()
        if descendant_lookup is not None:
            descendants, descendant_issues = descendant_lookup(root)

        blocking_reason: str | None = None
        if descendant_issues:
            blocking_reason = "descendant metadata is incomplete or malformed"
        else:
            for descendant in descendants:
                if descendant.pinned:
                    blocking_reason = f"pinned descendant {short_id(descendant.id)}"
                    break
                if descendant.unsafe:
                    blocking_reason = f"unsafe descendant {short_id(descendant.id)}"
                    break
                if policy.project == "current" and descendant.cwd != current_cwd:
                    blocking_reason = f"descendant outside current project {short_id(descendant.id)}"
                    break
        if blocking_reason is not None:
            protected_unsafe.append(UnsafeRecord(root.id, blocking_reason))
            continue
        candidates.append(Candidate(root, tuple(sorted(descendants, key=_session_sort_key))))

    return Plan(
        policy=policy,
        now=now,
        observed=tuple(scoped),
        malformed=malformed_items,
        protected_recent=tuple(protected_recent),
        protected_pinned=tuple(protected_pinned),
        protected_unsafe=tuple(protected_unsafe),
        candidates=tuple(candidates),
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
    )


def plan_snapshot(plan: Plan) -> tuple[Any, ...]:
    """Return all selection- and safety-critical state for stale checks."""
    observed = tuple(sorted((_session_snapshot(item) for item in plan.observed)))
    malformed = tuple(sorted(((item.id or "", item.reason) for item in plan.malformed)))
    candidates = tuple(
        (
            _session_snapshot(candidate.root),
            tuple(sorted(_session_snapshot(item) for item in candidate.descendants)),
        )
        for candidate in plan.candidates
    )
    return observed, malformed, candidates


def require_fresh_plan(before: Plan, after: Plan) -> None:
    if plan_snapshot(before) != plan_snapshot(after):
        raise StaleStateError("session candidates or safety-critical metadata changed; run a fresh preview")


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
) -> SchemaCapabilities:
    param_properties = list_params.get("properties", {})
    required_params = {
        "ancestorThreadId", "archived", "cursor", "cwd", "sortKey",
        "sortDirection", "sourceKinds", "useStateDbOnly",
    }
    missing_params = sorted(required_params - set(param_properties))

    definitions = list_response.get("definitions", {})
    thread_properties = definitions.get("Thread", {}).get("properties", {})
    required_thread = {
        "id", "name", "preview", "createdAt", "updatedAt", "recencyAt",
        "cwd", "ephemeral", "isPinned", "sessionId", "parentThreadId",
        "source", "status",
    }
    missing_thread = sorted(required_thread - set(thread_properties))
    if "codexHome" not in initialize_response.get("properties", {}):
        missing_thread.append("InitializeResponse.codexHome")

    source_schema = list_params.get("definitions", {}).get("ThreadSourceKind", {})
    source_values = tuple(source_schema.get("enum", ()))
    if not source_values:
        missing_params.append("ThreadSourceKind enum")

    missing = [*(f"ThreadListParams.{item}" for item in missing_params),
               *(f"Thread.{item}" for item in missing_thread)]
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
        return validate_schema_contract(list_params, list_response, initialize_response)


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
                "clientInfo": {"name": "codex-session-cleanup", "version": "1"},
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

    def discover_roots(self, policy: Policy) -> tuple[list[Session], list[UnsafeRecord]]:
        archived_scopes = (False, True) if policy.action == "delete" else (False,)
        sessions: list[Session] = []
        malformed: list[UnsafeRecord] = []
        for archived in archived_scopes:
            params: dict[str, Any] = {
                "archived": archived,
                "sortKey": "recency_at",
                "sortDirection": "desc",
                "sourceKinds": list(INTERACTIVE_SOURCES),
                "useStateDbOnly": True,
            }
            if policy.project == "current":
                params["cwd"] = policy.current_cwd
            for raw in self._list_pages(params):
                raw_id = raw.get("id") if isinstance(raw.get("id"), str) else None
                try:
                    item = parse_thread(raw, archived=archived)
                    if item.source_kind not in INTERACTIVE_SOURCES or item.parent_id is not None:
                        continue
                    sessions.append(item)
                except ValueError as exc:
                    malformed.append(UnsafeRecord(raw_id, str(exc)))
        return sessions, malformed

    def discover_descendants(
        self, root: Session, policy: Policy
    ) -> tuple[Sequence[Session], Sequence[UnsafeRecord]]:
        archived_scopes = (False, True) if policy.action == "delete" else (False,)
        descendants: dict[str, Session] = {}
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
                try:
                    item = parse_thread(raw, archived=archived)
                    if item.id in descendants and descendants[item.id] != item:
                        raise ValueError("descendant appeared with conflicting metadata")
                    descendants[item.id] = item
                except ValueError as exc:
                    malformed.append(UnsafeRecord(raw_id, str(exc)))
        return tuple(descendants.values()), tuple(malformed)


def discover_plan(
    discovery: SessionDiscovery, policy: Policy, *, now: dt.datetime
) -> Plan:
    sessions, malformed = discovery.discover_roots(policy)
    return build_plan(
        sessions,
        policy,
        now=now,
        malformed=malformed,
        descendant_lookup=lambda root: discovery.discover_descendants(root, policy),
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


def render_plan(plan: Plan, *, applying: bool = False) -> str:
    verb = "delete" if plan.policy.action == "delete" else "archive"
    lines = [
        "Codex session cleanup apply check" if applying else "Codex session cleanup preview",
        "",
        "Policy:",
        f"  project: {plan.policy.project}",
        f"  older than: {format_duration(plan.policy.older_than)}",
        f"  keep latest: {plan.policy.keep_latest if plan.policy.keep_latest is not None else 'not set'}",
        f"  action: {plan.policy.action}",
        "",
        f"Examined: {plan.examined}",
        f"Protected as recent/latest: {len(plan.protected_recent)}",
        f"Protected because pinned: {len(plan.protected_pinned)}",
        f"Protected because active/unsafe: {len(plan.protected_unsafe)}",
        "",
        f"Would {verb}: {len(plan.candidates)} root sessions",
        "Spawned descendants also affected: "
        + str(sum(len(candidate.descendants) for candidate in plan.candidates)),
    ]
    if plan.policy.action == "delete":
        lines.extend(["", "WARNING: Permanent deletion is irreversible."])
    if plan.candidates:
        lines.extend(["", "LAST USED            AGE      SESSION        PROJECT"])
        for candidate in plan.candidates:
            root = candidate.root
            used = root.recency_at.astimezone().strftime("%Y-%m-%d %H:%M")
            lines.append(
                f"{used:<20} {format_age(plan.now, root.recency_at):<8} "
                f"{short_id(root.id):<14} {root.cwd}"
            )
    lines.extend(["", "A final state refresh is required before mutations." if applying else "No changes made."])
    return "\n".join(lines)


def mutate_candidates(
    candidates: Sequence[Candidate],
    action: str,
    codex_bin: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> MutationReport:
    if action not in ("archive", "delete"):
        raise CleanupError("unsupported mutation action")
    successful: list[str] = []
    skipped: list[TargetFailure] = []
    failed: list[TargetFailure] = []
    for candidate in candidates:
        try:
            session_id = canonical_uuid(candidate.root.id)
        except ValueError as exc:
            skipped.append(TargetFailure(candidate.root.id, str(exc)))
            continue
        command = [codex_bin, action, session_id]
        if action == "delete":
            command.append("--force")
        completed = runner(command, capture_output=True, text=True, check=False)
        if completed.returncode == 0:
            successful.append(session_id)
            continue
        detail = (completed.stderr or completed.stdout or "Codex command failed").strip()
        failed.append(TargetFailure(session_id, detail[:500]))
    return MutationReport(tuple(successful), tuple(skipped), tuple(failed))


def render_mutation_report(report: MutationReport, action: str) -> str:
    lines = [
        "Codex session cleanup result",
        "",
        f"Action: {'irreversible delete' if action == 'delete' else 'archive'}",
        f"Successful targets: {len(report.successful)}",
        f"Skipped targets: {len(report.skipped)}",
        f"Failed targets: {len(report.failed)}",
    ]
    for label, failures in (("Skipped", report.skipped), ("Failed", report.failed)):
        if failures:
            lines.extend(["", f"{label}:"])
            lines.extend(f"  {short_id(item.session_id)} {item.error}" for item in failures)
    return "\n".join(lines)


def find_codex() -> str:
    value = shutil.which("codex")
    if value is None:
        raise CompatibilityError("codex executable was not found on PATH")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        current_cwd = normalize_invoking_cwd(os.getcwd())
        policy = Policy(
            older_than=args.older_than,
            keep_latest=args.keep_latest,
            project=args.project,
            action=args.action,
            current_cwd=current_cwd,
        )
        codex_bin = find_codex()
        capabilities = inspect_installed_schema(codex_bin)
        inspect_mutation_commands(codex_bin)

        with AppServerClient(codex_bin) as client:
            discovery = SessionDiscovery(client, capabilities)
            now = dt.datetime.now(dt.timezone.utc)
            initial = discover_plan(discovery, policy, now=now)
            print(render_plan(initial, applying=args.apply))
            if not args.apply or not initial.candidates:
                if args.apply and not initial.candidates:
                    print("\nNo matching sessions. No changes made.")
                return 0
            refreshed = discover_plan(
                discovery, policy, now=dt.datetime.now(dt.timezone.utc)
            )
            require_fresh_plan(initial, refreshed)

        report = mutate_candidates(refreshed.candidates, policy.action, codex_bin)
        print("\n" + render_mutation_report(report, policy.action))
        return 1 if report.failed or report.skipped else 0
    except CleanupError as exc:
        print(f"Safety stop: {exc}", file=sys.stderr)
        print("No changes made.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
