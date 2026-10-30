from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "cleanup_sessions.py"
SPEC = importlib.util.spec_from_file_location("cleanup_sessions", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
cleanup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cleanup
SPEC.loader.exec_module(cleanup)

NOW = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.timezone.utc)


def fictional_path(label: str) -> str:
    """Return a normalized, host-native absolute path for synthetic data."""
    value = Path(tempfile.gettempdir()) / "codex-session-cleanup-tests" / label
    return cleanup.normalize_invoking_cwd(str(value))


def fixture_project_path(value: object) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]
    return fictional_path(f"fixture-project-{digest}")


PROJECT = fictional_path("project")
PROJECT_CHILD = cleanup.normalize_invoking_cwd(os.path.join(PROJECT, "child"))
PROJECT_SIBLING = fictional_path("project-sibling")
OTHER_PROJECT = fictional_path("other-project")
CODEX_HOME = fictional_path("codex-home")
DIFFERENT_CODEX_HOME = fictional_path("different-codex-home")
PINNED_SECTION_ID = "00000000-0000-4000-8000-000000000900"


def session_id(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def make_session(
    number: int,
    *,
    age_days: float,
    cwd: str = PROJECT,
    pinned: bool = False,
    ephemeral: bool = False,
    status: str = "notLoaded",
    archived: bool = False,
    parent_id: str | None = None,
    source_kind: str = "cli",
    loaded: bool = False,
) -> cleanup.Session:
    recency = NOW - dt.timedelta(days=age_days)
    return cleanup.Session(
        id=session_id(number),
        title=f"Fictional session {number}",
        created_at=recency - dt.timedelta(days=1),
        updated_at=recency,
        recency_at=recency,
        cwd=cwd,
        pinned=pinned,
        ephemeral=ephemeral,
        status=status,
        active_flags=("waitingOnUserInput",) if status == "active" else (),
        archived=archived,
        parent_id=parent_id,
        session_id=session_id(number),
        source_kind=source_kind,
        loaded=loaded,
        title_source="preview",
    )


def make_policy(
    *,
    older_than: dt.timedelta | None = None,
    keep_latest: int | None = None,
    project: str = "current",
    action: str = "archive",
) -> cleanup.Policy:
    return cleanup.Policy(older_than, keep_latest, project, action, PROJECT)


def raw_thread(number: int, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": session_id(number),
        "sessionId": session_id(number),
        "parentThreadId": None,
        "name": None,
        "preview": f"Fictional preview {number}",
        "createdAt": int((NOW - dt.timedelta(days=10)).timestamp()),
        "updatedAt": int((NOW - dt.timedelta(days=9)).timestamp()),
        "recencyAt": int((NOW - dt.timedelta(days=8)).timestamp()),
        "cwd": PROJECT,
        "section": None,
        "ephemeral": False,
        "source": "cli",
        "status": {"type": "notLoaded"},
    }
    value.update(overrides)
    return value


def valid_schema_documents() -> tuple[dict[str, object], ...]:
    list_params = {
        "properties": {
            name: {} for name in (
                "ancestorThreadId", "archived", "cursor", "cwd", "limit",
                "sectionId", "sortKey", "sortDirection", "sourceKinds",
                "useStateDbOnly",
            )
        },
        "definitions": {"ThreadSourceKind": {"enum": ["cli", "vscode"]}},
    }
    thread_fields = {
        name: {} for name in (
            "id", "name", "preview", "createdAt", "updatedAt", "recencyAt",
            "cwd", "ephemeral", "section", "sessionId", "parentThreadId",
            "source", "status",
        )
    }
    list_response = {
        "properties": {"data": {}, "nextCursor": {}},
        "definitions": {"Thread": {"properties": thread_fields}},
    }
    initialize_response = {"properties": {"codexHome": {}}}
    section_list_params = {"properties": {"cursor": {}, "limit": {}}}
    section_list_response = {
        "properties": {"data": {}, "nextCursor": {}},
        "definitions": {
            "ThreadSection": {"properties": {"id": {}, "name": {}}}
        },
    }
    loaded_list_params = {"properties": {"cursor": {}, "limit": {}}}
    loaded_list_response = {
        "properties": {"data": {}, "nextCursor": {}}
    }
    return (
        list_params,
        list_response,
        initialize_response,
        section_list_params,
        section_list_response,
        loaded_list_params,
        loaded_list_response,
    )


def plan_document(plan: cleanup.Plan) -> dict[str, object]:
    return cleanup.plan_document(
        plan,
        CODEX_HOME,
        codex_version="codex-cli 0.test",
    )


def validate_json_schema(
    value: object,
    schema: dict[str, object],
    root: dict[str, object],
    path: str = "$",
) -> None:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        if not reference.startswith("#/"):
            raise AssertionError(f"{path}: unsupported schema reference {reference}")
        target: object = root
        for component in reference[2:].split("/"):
            if not isinstance(target, dict) or component not in target:
                raise AssertionError(f"{path}: unresolved schema reference {reference}")
            target = target[component]
        if not isinstance(target, dict):
            raise AssertionError(f"{path}: schema reference is not an object")
        validate_json_schema(value, target, root, path)

    if "const" in schema and value != schema["const"]:
        raise AssertionError(f"{path}: expected constant {schema['const']!r}")
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise AssertionError(f"{path}: value {value!r} is not in {enum!r}")

    expected_types = schema.get("type")
    if isinstance(expected_types, str):
        expected_types = [expected_types]
    if isinstance(expected_types, list):
        matches = {
            "null": value is None,
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
        }
        if not any(matches.get(item, False) for item in expected_types):
            raise AssertionError(f"{path}: expected type {expected_types}, got {type(value).__name__}")

    minimum = schema.get("minimum")
    if isinstance(minimum, int) and isinstance(value, int) and value < minimum:
        raise AssertionError(f"{path}: {value} is below minimum {minimum}")
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and isinstance(value, str) and re.fullmatch(pattern, value) is None:
        raise AssertionError(f"{path}: {value!r} does not match {pattern!r}")

    if isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            raise AssertionError(f"{path}: array has fewer than {min_items} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                validate_json_schema(item, item_schema, root, f"{path}[{index}]")

    if isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            missing = set(required) - set(value)
            if missing:
                raise AssertionError(f"{path}: missing required fields {sorted(missing)}")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            properties = {}
        for key, child in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, dict):
                validate_json_schema(child, child_schema, root, f"{path}.{key}")
                continue
            additional = schema.get("additionalProperties", True)
            if additional is False:
                raise AssertionError(f"{path}: unexpected field {key!r}")
            if isinstance(additional, dict):
                validate_json_schema(child, additional, root, f"{path}.{key}")


class ContractIntegrityTests(unittest.TestCase):
    def test_vendored_contract_matches_sha256sums(self) -> None:
        contract_dir = ROOT / "contract"
        sums_path = contract_dir / "SHA256SUMS"
        listed: set[str] = set()
        for line in sums_path.read_text(encoding="utf-8").splitlines():
            digest, relative_name = line.split("  ", 1)
            relative = Path(relative_name)
            self.assertFalse(relative.is_absolute())
            self.assertNotIn("..", relative.parts)
            path = contract_dir / relative
            self.assertTrue(path.is_file(), relative_name)
            self.assertEqual(
                digest,
                hashlib.sha256(path.read_bytes()).hexdigest(),
                relative_name,
            )
            listed.add(relative.as_posix())

        present = {
            path.relative_to(contract_dir).as_posix()
            for path in contract_dir.rglob("*")
            if path.is_file() and path.name != "SHA256SUMS"
        }
        self.assertEqual(listed, present)


class ContractConformanceTests(unittest.TestCase):
    def _fixture_plan(
        self, fixture: dict[str, object]
    ) -> tuple[cleanup.Plan, dict[str, str]]:
        evaluated_at = dt.datetime.fromisoformat(
            str(fixture["evaluated_at"]).replace("Z", "+00:00")
        )
        raw_sessions = fixture["sessions"]
        assert isinstance(raw_sessions, list)
        labels = sorted(str(item["id"]) for item in raw_sessions)
        ids = {
            label: f"11111111-1111-4111-8111-{index:012d}"
            for index, label in enumerate(labels, start=1)
        }
        sessions: dict[str, cleanup.Session] = {}
        malformed: list[cleanup.UnsafeRecord] = []
        for raw in raw_sessions:
            assert isinstance(raw, dict)
            label = str(raw["id"])
            parent = raw.get("parent")
            recency = evaluated_at - dt.timedelta(hours=int(raw["hours_ago"]))
            state = str(raw["state"])
            item = cleanup.Session(
                id=ids[label],
                title=f"Contract fixture {label}",
                created_at=recency - dt.timedelta(hours=1),
                updated_at=recency,
                recency_at=recency,
                cwd=fixture_project_path(raw["project"]),
                pinned=state == "pinned",
                ephemeral=state == "ephemeral",
                status="active" if state == "running" else "idle",
                active_flags=("processing",) if state == "running" else (),
                archived=False,
                parent_id=ids[str(parent)] if parent is not None else None,
                session_id=ids[label],
                source_kind="cli" if parent is None else "subAgent",
                loaded=state == "loaded",
                title_source="preview",
            )
            sessions[label] = item
            if raw["metadata"] == "malformed" and parent is None:
                malformed.append(
                    cleanup.UnsafeRecord(item.id, "fixture metadata is malformed")
                )

        children: dict[str, list[str]] = {}
        for raw in raw_sessions:
            assert isinstance(raw, dict)
            parent = raw.get("parent")
            if parent is not None:
                children.setdefault(str(parent), []).append(str(raw["id"]))

        def descendants(
            root: cleanup.Session,
        ) -> tuple[list[cleanup.Session], list[cleanup.UnsafeRecord]]:
            root_label = next(label for label, value in ids.items() if value == root.id)
            found: list[cleanup.Session] = []
            issues: list[cleanup.UnsafeRecord] = []
            pending = list(children.get(root_label, []))
            while pending:
                label = pending.pop(0)
                item = sessions[label]
                found.append(item)
                raw = next(
                    entry
                    for entry in raw_sessions
                    if isinstance(entry, dict) and entry["id"] == label
                )
                if raw["metadata"] == "malformed":
                    issues.append(
                        cleanup.UnsafeRecord(item.id, "fixture metadata is malformed")
                    )
                pending.extend(children.get(label, []))
            return found, issues

        scope = fixture["scope"]
        policy_data = fixture["policy"]
        assert isinstance(scope, dict) and isinstance(policy_data, dict)
        older_hours = policy_data["older_than_hours"]
        policy = cleanup.Policy(
            dt.timedelta(hours=int(older_hours)) if older_hours is not None else None,
            policy_data["keep_latest"],
            str(scope["mode"]),
            "archive",
            fixture_project_path(scope["path"] or "/work/repo"),
        )
        roots = [item for item in sessions.values() if item.parent_id is None]
        return (
            cleanup.build_plan(
                roots,
                policy,
                now=evaluated_at,
                malformed=malformed,
                descendant_lookup=descendants,
            ),
            ids,
        )

    def test_all_shared_fixtures_match_status_rank_and_reasons(self) -> None:
        fixture_paths = sorted((ROOT / "contract" / "fixtures").glob("*.json"))
        self.assertEqual(15, len(fixture_paths))
        for fixture_path in fixture_paths:
            with self.subTest(fixture=fixture_path.name):
                fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
                plan, ids = self._fixture_plan(fixture)
                actual = {group.tree.root.id: group for group in plan.groups}
                self.assertEqual(
                    {group.tree.root.id for group in plan.groups if group.status == "selected"},
                    {candidate.root.id for candidate in plan.candidates},
                )
                self.assertTrue(
                    all(
                        group.rank is None
                        if group.status == "excluded"
                        else isinstance(group.rank, int)
                        for group in plan.groups
                    )
                )
                expected_records = fixture["expected"]
                self.assertEqual(len(expected_records), len(actual))
                for expected in expected_records:
                    group = actual[ids[expected["group"]]]
                    self.assertEqual(expected["status"], group.status)
                    self.assertEqual(expected["rank"], group.rank)
                    self.assertTrue(
                        set(expected["reasons_include"]).issubset(group.reasons),
                        (fixture_path.name, expected, group),
                    )

    def test_shared_preview_validates_and_titles_are_opt_in(self) -> None:
        fixture = json.loads(
            (ROOT / "contract" / "fixtures" / "08-latest-n-counts-groups.json").read_text(
                encoding="utf-8"
            )
        )
        plan, _ = self._fixture_plan(fixture)
        schema = json.loads(
            (ROOT / "contract" / "preview-v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        without_titles = cleanup.shared_preview_document(
            plan,
            CODEX_HOME,
            codex_version="codex-cli 0.test",
        )
        validate_json_schema(without_titles, schema, schema)
        self.assertTrue(
            all(
                session["title"] is None
                for group in without_titles["groups"]
                for session in group["sessions"]
            )
        )
        with_titles = cleanup.shared_preview_document(
            plan,
            CODEX_HOME,
            codex_version="codex-cli 0.test",
            include_titles=True,
        )
        validate_json_schema(with_titles, schema, schema)
        self.assertTrue(
            all(
                session["title"]
                for group in with_titles["groups"]
                for session in group["sessions"]
            )
        )
        self.assertEqual(
            [group["group_id"] for group in without_titles["groups"]],
            [group["group_id"] for group in with_titles["groups"]],
        )
        self.assertEqual(
            cleanup.render_plan_json(without_titles),
            cleanup.render_plan_json(
                cleanup.shared_preview_document(
                    plan,
                    CODEX_HOME,
                    codex_version="codex-cli 0.test",
                )
            ),
        )


class RetentionPolicyTests(unittest.TestCase):
    def test_age_only_selection(self) -> None:
        sessions = [make_session(1, age_days=8), make_session(2, age_days=6)]
        plan = cleanup.build_plan(
            sessions, make_policy(older_than=dt.timedelta(days=7)), now=NOW
        )
        self.assertEqual([session_id(1)], [item.root.id for item in plan.candidates])
        self.assertEqual([session_id(2)], [item.id for item in plan.protected_recent])

    def test_keep_latest_n(self) -> None:
        sessions = [make_session(number, age_days=number) for number in range(1, 5)]
        plan = cleanup.build_plan(sessions, make_policy(keep_latest=2), now=NOW)
        self.assertEqual(
            [session_id(3), session_id(4)],
            [item.root.id for item in plan.candidates],
        )
        self.assertEqual(2, len(plan.protected_recent))

    def test_age_and_keep_n_use_intersection(self) -> None:
        sessions = [
            make_session(1, age_days=1),
            make_session(2, age_days=5),
            make_session(3, age_days=10),
            make_session(4, age_days=20),
        ]
        plan = cleanup.build_plan(
            sessions,
            make_policy(older_than=dt.timedelta(days=7), keep_latest=2),
            now=NOW,
        )
        self.assertEqual(
            [session_id(3), session_id(4)],
            [item.root.id for item in plan.candidates],
        )

    def test_current_project_requires_exact_cwd(self) -> None:
        sessions = [
            make_session(1, age_days=10),
            make_session(2, age_days=10, cwd=PROJECT_CHILD),
            make_session(3, age_days=10, cwd=PROJECT_SIBLING),
        ]
        plan = cleanup.build_plan(
            sessions, make_policy(older_than=dt.timedelta(days=7)), now=NOW
        )
        self.assertEqual([session_id(1)], [item.root.id for item in plan.candidates])
        self.assertEqual(1, plan.examined)

    def test_current_project_excludes_group_with_related_root_outside_scope(self) -> None:
        root = make_session(1, age_days=30, cwd=OTHER_PROJECT)
        descendant = make_session(
            2,
            age_days=20,
            cwd=PROJECT,
            parent_id=root.id,
            source_kind="subAgent",
        )
        plan = cleanup.build_plan(
            [root],
            make_policy(older_than=dt.timedelta(days=7)),
            now=NOW,
            descendant_lookup=lambda _: ([descendant], []),
        )
        self.assertFalse(plan.candidates)
        self.assertEqual(1, len(plan.groups))
        self.assertEqual("excluded", plan.groups[0].status)
        self.assertIsNone(plan.groups[0].rank)
        self.assertIn("related-outside-scope", plan.groups[0].reasons)

    def test_known_out_of_scope_malformed_root_is_not_reported_in_current_scope(self) -> None:
        plan = cleanup.build_plan(
            [],
            make_policy(older_than=dt.timedelta(days=7)),
            now=NOW,
            malformed=[
                cleanup.UnsafeRecord(
                    session_id(9),
                    "fixture metadata is malformed",
                    OTHER_PROJECT,
                )
            ],
        )
        self.assertFalse(plan.groups)
        self.assertFalse(plan.malformed)
        self.assertFalse(plan.protections)

    def test_all_projects_includes_each_exact_cwd(self) -> None:
        sessions = [
            make_session(1, age_days=10),
            make_session(2, age_days=10, cwd=OTHER_PROJECT),
        ]
        plan = cleanup.build_plan(
            sessions,
            make_policy(older_than=dt.timedelta(days=7), project="all"),
            now=NOW,
        )
        self.assertEqual(2, len(plan.candidates))

    def test_pinned_session_is_protected(self) -> None:
        plan = cleanup.build_plan(
            [make_session(1, age_days=30, pinned=True)],
            make_policy(older_than=dt.timedelta(days=7)),
            now=NOW,
        )
        self.assertFalse(plan.candidates)
        self.assertEqual(1, len(plan.protected_pinned))

    def test_active_session_is_protected(self) -> None:
        plan = cleanup.build_plan(
            [make_session(1, age_days=30, status="active")],
            make_policy(older_than=dt.timedelta(days=7)),
            now=NOW,
        )
        self.assertFalse(plan.candidates)
        self.assertEqual(1, len(plan.protected_unsafe))

    def test_zero_matches(self) -> None:
        plan = cleanup.build_plan(
            [make_session(1, age_days=1)],
            make_policy(older_than=dt.timedelta(days=7)),
            now=NOW,
        )
        self.assertFalse(plan.candidates)
        rendered = cleanup.render_plan(plan_document(plan))
        self.assertIn("Selected roots: 0", rendered)
        self.assertIn("Fictional session 1", rendered)


class MetadataAndHomeTests(unittest.TestCase):
    def test_schema_with_section_pin_state_is_accepted(self) -> None:
        capabilities = cleanup.validate_schema_contract(*valid_schema_documents())
        self.assertEqual(("cli", "vscode"), capabilities.all_source_kinds)

    def test_schema_without_section_pin_state_fails_closed(self) -> None:
        schemas = valid_schema_documents()
        schemas[1]["definitions"]["Thread"]["properties"].pop("section")
        with self.assertRaisesRegex(cleanup.CompatibilityError, "Thread.section"):
            cleanup.validate_schema_contract(*schemas)

    def test_schema_without_loaded_thread_api_fails_closed(self) -> None:
        schemas = valid_schema_documents()
        schemas[-1]["properties"].pop("data")
        with self.assertRaisesRegex(
            cleanup.CompatibilityError, "ThreadLoadedListResponse.data"
        ):
            cleanup.validate_schema_contract(*schemas)

    def test_alternate_codex_home_is_taken_from_initialize(self) -> None:
        alternate = CODEX_HOME
        resolved = cleanup.validate_codex_home(
            {"codexHome": alternate}, {"CODEX_HOME": alternate}
        )
        self.assertEqual(cleanup.normalize_invoking_cwd(alternate), resolved)
        with self.assertRaises(cleanup.ProtocolError):
            cleanup.validate_codex_home(
                {"codexHome": alternate}, {"CODEX_HOME": DIFFERENT_CODEX_HOME}
            )

    def test_malformed_or_missing_metadata_fails_safely(self) -> None:
        for field in ("recencyAt", "section", "cwd", "status"):
            with self.subTest(field=field):
                raw = raw_thread(1)
                raw.pop(field)
                with self.assertRaises(ValueError):
                    cleanup.parse_thread(
                        raw,
                        archived=False,
                        pinned_section_id=PINNED_SECTION_ID,
                    )

        malformed = cleanup.UnsafeRecord(session_id(1), "recencyAt is missing")
        plan = cleanup.build_plan(
            [],
            make_policy(older_than=dt.timedelta(days=7)),
            now=NOW,
            malformed=[malformed],
        )
        self.assertEqual(1, plan.examined)
        self.assertEqual(1, len(plan.protected_unsafe))

        identified = make_session(2, age_days=30)
        identified_plan = cleanup.build_plan(
            [identified],
            make_policy(older_than=dt.timedelta(days=7)),
            now=NOW,
            malformed=[cleanup.UnsafeRecord(identified.id, "duplicate metadata")],
        )
        self.assertFalse(identified_plan.candidates)

    def test_recency_at_is_required_and_not_replaced_by_updated_at(self) -> None:
        raw = raw_thread(1, recencyAt=None)
        with self.assertRaisesRegex(ValueError, "recencyAt"):
            cleanup.parse_thread(
                raw,
                archived=False,
                pinned_section_id=PINNED_SECTION_ID,
            )

    def test_thread_pin_state_comes_from_the_builtin_pinned_section(self) -> None:
        pinned = cleanup.parse_thread(
            raw_thread(
                1,
                section={"id": PINNED_SECTION_ID, "name": "Pinned"},
            ),
            archived=False,
            pinned_section_id=PINNED_SECTION_ID,
        )
        custom = cleanup.parse_thread(
            raw_thread(
                2,
                section={"id": session_id(901), "name": "Later"},
            ),
            archived=False,
            pinned_section_id=PINNED_SECTION_ID,
        )
        unsectioned = cleanup.parse_thread(
            raw_thread(3),
            archived=False,
            pinned_section_id=PINNED_SECTION_ID,
        )
        self.assertTrue(pinned.pinned)
        self.assertFalse(custom.pinned)
        self.assertFalse(unsectioned.pinned)

    def test_conflicting_thread_section_metadata_fails_closed(self) -> None:
        raw = raw_thread(
            1,
            section={"id": session_id(901), "name": "Pinned"},
        )
        with self.assertRaisesRegex(ValueError, "conflicts"):
            cleanup.parse_thread(
                raw,
                archived=False,
                pinned_section_id=PINNED_SECTION_ID,
            )

    def test_app_server_pagination_is_followed(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def request(
                self, method: str, params: dict[str, object]
            ) -> dict[str, object]:
                self.calls.append(params)
                if "cursor" not in params:
                    return {"data": [raw_thread(1)], "nextCursor": "fictional-cursor"}
                return {"data": [raw_thread(2)], "nextCursor": None}

        client = FakeClient()
        discovery = cleanup.SessionDiscovery(
            client, cleanup.SchemaCapabilities(("cli", "vscode", "subAgent"))
        )
        records = discovery._list_pages({"sourceKinds": ["cli", "vscode"]})
        self.assertEqual(2, len(records))
        self.assertEqual("fictional-cursor", client.calls[1]["cursor"])

    def test_current_scope_root_discovery_does_not_hide_related_groups(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def request(
                self, method: str, params: dict[str, object]
            ) -> dict[str, object]:
                self.calls.append(params)
                return {
                    "data": [
                        raw_thread(1, cwd=OTHER_PROJECT)
                    ],
                    "nextCursor": None,
                }

        client = FakeClient()
        discovery = cleanup.SessionDiscovery(
            client, cleanup.SchemaCapabilities(("cli", "vscode", "subAgent"))
        )
        roots, malformed = discovery.discover_roots(
            make_policy(older_than=dt.timedelta(days=7)),
            pinned_section_id=PINNED_SECTION_ID,
            loaded_ids=set(),
        )
        self.assertEqual(1, len(roots))
        self.assertFalse(malformed)
        self.assertTrue(all("cwd" not in call for call in client.calls))

    def test_invalid_thread_pagination_cursor_fails_closed(self) -> None:
        class FakeClient:
            def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
                return {"data": [], "nextCursor": "repeated"}

        discovery = cleanup.SessionDiscovery(
            FakeClient(), cleanup.SchemaCapabilities(("cli", "vscode"))
        )
        with self.assertRaises(cleanup.ProtocolError):
            discovery._list_pages({})

    def test_loaded_thread_pagination_and_validation(self) -> None:
        class FakeClient:
            def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
                if "cursor" not in params:
                    return {"data": [session_id(1)], "nextCursor": "next"}
                return {"data": [session_id(2)], "nextCursor": None}

        discovery = cleanup.SessionDiscovery(
            FakeClient(), cleanup.SchemaCapabilities(("cli", "vscode"))
        )
        self.assertEqual(
            {session_id(1), session_id(2)}, discovery.discover_loaded_ids()
        )

        class BadClient:
            def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
                return {"data": ["not-a-uuid"], "nextCursor": None}

        bad = cleanup.SessionDiscovery(
            BadClient(), cleanup.SchemaCapabilities(("cli", "vscode"))
        )
        with self.assertRaises(cleanup.ProtocolError):
            bad.discover_loaded_ids()

        class DuplicateClient:
            def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
                return {
                    "data": [session_id(1), session_id(1)],
                    "nextCursor": None,
                }

        duplicate = cleanup.SessionDiscovery(
            DuplicateClient(), cleanup.SchemaCapabilities(("cli", "vscode"))
        )
        with self.assertRaisesRegex(cleanup.ProtocolError, "duplicate"):
            duplicate.discover_loaded_ids()

    def test_delete_discovers_active_and_archived_roots(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.archived_values: list[bool] = []

            def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
                archived = bool(params["archived"])
                self.archived_values.append(archived)
                number = 2 if archived else 1
                return {"data": [raw_thread(number)], "nextCursor": None}

        client = FakeClient()
        discovery = cleanup.SessionDiscovery(
            client, cleanup.SchemaCapabilities(("cli", "vscode"))
        )
        roots, malformed = discovery.discover_roots(
            make_policy(older_than=dt.timedelta(days=7), action="delete"),
            pinned_section_id=PINNED_SECTION_ID,
            loaded_ids=set(),
        )
        self.assertFalse(malformed)
        self.assertEqual([False, True], client.archived_values)
        self.assertEqual([False, True], [item.archived for item in roots])

    def test_pinned_section_discovery_is_paginated(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
                self.assert_method(method)
                self.calls.append(params)
                if "cursor" not in params:
                    return {
                        "data": [{"id": session_id(901), "name": "Later"}],
                        "nextCursor": "fictional-section-cursor",
                    }
                return {
                    "data": [{"id": PINNED_SECTION_ID, "name": "Pinned"}],
                    "nextCursor": None,
                }

            @staticmethod
            def assert_method(method: str) -> None:
                if method != "threadSection/list":
                    raise AssertionError(method)

        client = FakeClient()
        discovery = cleanup.SessionDiscovery(
            client, cleanup.SchemaCapabilities(("cli", "vscode", "subAgent"))
        )
        section = discovery.discover_pinned_section()
        self.assertEqual(PINNED_SECTION_ID, section.id)
        self.assertEqual("fictional-section-cursor", client.calls[1]["cursor"])

    def test_bad_pinned_section_discovery_fails_closed(self) -> None:
        cases = (
            ([{"id": session_id(901), "name": "Later"}], cleanup.CompatibilityError),
            (
                [
                    {"id": PINNED_SECTION_ID, "name": "Pinned"},
                    {"id": session_id(902), "name": "Pinned"},
                ],
                cleanup.CompatibilityError,
            ),
            (
                [
                    {"id": PINNED_SECTION_ID, "name": "Pinned"},
                    {"id": PINNED_SECTION_ID, "name": "Pinned"},
                ],
                cleanup.ProtocolError,
            ),
            ([{"id": "not-a-uuid", "name": "Pinned"}], cleanup.ProtocolError),
        )

        for sections, error in cases:
            with self.subTest(sections=sections):
                class FakeClient:
                    def request(
                        self, method: str, params: dict[str, object]
                    ) -> dict[str, object]:
                        return {"data": sections, "nextCursor": None}

                discovery = cleanup.SessionDiscovery(
                    FakeClient(), cleanup.SchemaCapabilities(("cli", "vscode"))
                )
                with self.assertRaises(error):
                    discovery.discover_pinned_section()

    def test_duplicate_root_or_descendant_records_fail_closed(self) -> None:
        class DuplicateClient:
            def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
                return {
                    "data": [raw_thread(1), raw_thread(1)],
                    "nextCursor": None,
                }

        discovery = cleanup.SessionDiscovery(
            DuplicateClient(), cleanup.SchemaCapabilities(("cli", "vscode"))
        )
        roots, root_issues = discovery.discover_roots(
            make_policy(older_than=dt.timedelta(days=7)),
            pinned_section_id=PINNED_SECTION_ID,
            loaded_ids=set(),
        )
        self.assertFalse(roots)
        self.assertIn("more than once", root_issues[0].reason)

        child = raw_thread(
            2,
            parentThreadId=session_id(1),
            source="subAgent",
        )

        class DuplicateChildClient:
            def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
                return {"data": [child, child], "nextCursor": None}

        child_discovery = cleanup.SessionDiscovery(
            DuplicateChildClient(),
            cleanup.SchemaCapabilities(("cli", "vscode", "subAgent")),
        )
        descendants, child_issues = child_discovery.discover_descendants(
            make_session(1, age_days=30),
            make_policy(older_than=dt.timedelta(days=7)),
            pinned_section_id=PINNED_SECTION_ID,
            loaded_ids=set(),
        )
        self.assertFalse(descendants)
        self.assertIn("more than once", child_issues[0].reason)

    def test_malformed_duplicate_id_cannot_leave_a_valid_root_candidate(self) -> None:
        malformed = raw_thread(1)
        malformed.pop("recencyAt")

        class MixedClient:
            def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
                return {
                    "data": [raw_thread(1), malformed],
                    "nextCursor": None,
                }

        discovery = cleanup.SessionDiscovery(
            MixedClient(), cleanup.SchemaCapabilities(("cli", "vscode"))
        )
        roots, issues = discovery.discover_roots(
            make_policy(older_than=dt.timedelta(days=7)),
            pinned_section_id=PINNED_SECTION_ID,
            loaded_ids=set(),
        )
        self.assertFalse(roots)
        self.assertIn("recencyAt", issues[0].reason)

    @mock.patch.object(cleanup.shutil, "which", return_value="/path/to/codex")
    def test_codex_on_path_is_preferred(self, _: mock.Mock) -> None:
        result = cleanup.find_codex(
            environment={
                "PATH": "/path/to/bin",
                "CODEX_MANAGED_PACKAGE_ROOT": "/path/to/managed-package",
            }
        )
        self.assertEqual("/path/to/codex", result)

    @unittest.skipIf(os.name == "nt", "POSIX executable mode test")
    @mock.patch.object(cleanup.shutil, "which", return_value=None)
    def test_managed_codex_binary_is_a_narrow_fallback(self, _: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            candidate = bin_dir / "codex.js"
            candidate.write_text("fictional executable", encoding="utf-8")
            candidate.chmod(0o700)
            result = cleanup.find_codex(
                environment={
                    "PATH": "",
                    "CODEX_MANAGED_PACKAGE_ROOT": str(root),
                }
            )
        self.assertEqual(str(candidate), result)


class DescendantSafetyTests(unittest.TestCase):
    def test_old_root_with_recent_descendant_is_protected(self) -> None:
        root = make_session(1, age_days=45)
        descendant = make_session(2, age_days=1, parent_id=root.id)
        plan = cleanup.build_plan(
            [root],
            make_policy(older_than=dt.timedelta(days=30), action="delete"),
            now=NOW,
            descendant_lookup=lambda _: ([descendant], []),
        )
        self.assertFalse(plan.candidates)
        group = plan.groups[0]
        self.assertEqual("retained", group.status)
        self.assertEqual(("newer-than-cutoff",), group.reasons)
        self.assertEqual(descendant.id, group.activity_session_id)

    def test_descendant_in_latest_n_protects_old_root(self) -> None:
        root = make_session(1, age_days=45)
        descendant = make_session(2, age_days=10, parent_id=root.id)
        other = make_session(3, age_days=1)

        def descendants(item: cleanup.Session) -> tuple[list[cleanup.Session], list[cleanup.UnsafeRecord]]:
            return ([descendant], []) if item.id == root.id else ([], [])

        plan = cleanup.build_plan(
            [root, other],
            make_policy(keep_latest=2, action="delete"),
            now=NOW,
            descendant_lookup=descendants,
        )
        self.assertFalse(any(item.root.id == root.id for item in plan.candidates))
        group = next(item for item in plan.groups if item.tree.root.id == root.id)
        self.assertEqual("retained", group.status)
        self.assertEqual(2, group.rank)
        self.assertIn("within-latest", group.reasons)

    def test_combined_rules_protect_latest_descendant_even_when_old(self) -> None:
        root = make_session(1, age_days=45)
        descendant = make_session(2, age_days=35, parent_id=root.id)
        plan = cleanup.build_plan(
            [root],
            make_policy(
                older_than=dt.timedelta(days=30),
                keep_latest=1,
                action="delete",
            ),
            now=NOW,
            descendant_lookup=lambda _: ([descendant], []),
        )
        self.assertFalse(plan.candidates)
        self.assertEqual("retained", plan.groups[0].status)
        self.assertIn("within-latest", plan.groups[0].reasons)

    def test_nested_recent_descendant_blocks_root(self) -> None:
        root = make_session(1, age_days=45)
        child = make_session(2, age_days=40, parent_id=root.id)
        grandchild = make_session(3, age_days=1, parent_id=child.id)
        plan = cleanup.build_plan(
            [root],
            make_policy(older_than=dt.timedelta(days=30), action="delete"),
            now=NOW,
            descendant_lookup=lambda _: ([grandchild, child], []),
        )
        self.assertFalse(plan.candidates)
        self.assertEqual(grandchild.id, plan.groups[0].activity_session_id)
        self.assertEqual("retained", plan.groups[0].status)
        self.assertEqual((child, grandchild), plan.trees[0].descendants)

    def test_malformed_nested_ancestry_blocks_root(self) -> None:
        root = make_session(1, age_days=45)
        descendant = make_session(2, age_days=40, parent_id=session_id(999))
        plan = cleanup.build_plan(
            [root],
            make_policy(older_than=dt.timedelta(days=30), action="delete"),
            now=NOW,
            descendant_lookup=lambda _: ([descendant], []),
        )
        self.assertFalse(plan.candidates)
        self.assertEqual("excluded", plan.groups[0].status)
        self.assertIn("ancestry-ambiguous", plan.groups[0].reasons)
        self.assertTrue(
            any("parent is absent" in issue.reason for issue in plan.groups[0].issues)
        )

    def test_mixed_archive_state_descendants_are_retention_checked_for_delete(self) -> None:
        root = make_session(1, age_days=45)
        descendant = make_session(
            2, age_days=40, parent_id=root.id, archived=True
        )
        plan = cleanup.build_plan(
            [root],
            make_policy(older_than=dt.timedelta(days=30), action="delete"),
            now=NOW,
            descendant_lookup=lambda _: ([descendant], []),
        )
        self.assertEqual((descendant,), plan.candidates[0].descendants)

    def test_loaded_descendant_blocks_root(self) -> None:
        root = make_session(1, age_days=45)
        descendant = make_session(2, age_days=40, parent_id=root.id, loaded=True)
        plan = cleanup.build_plan(
            [root],
            make_policy(older_than=dt.timedelta(days=30)),
            now=NOW,
            descendant_lookup=lambda _: ([descendant], []),
        )
        self.assertFalse(plan.candidates)
        self.assertEqual("protected", plan.groups[0].status)
        self.assertIn("running", plan.groups[0].reasons)

    def test_descendant_cascade_is_reported(self) -> None:
        root = make_session(1, age_days=30)
        descendant = make_session(
            2,
            age_days=20,
            parent_id=root.id,
            source_kind="subAgent",
        )
        plan = cleanup.build_plan(
            [root],
            make_policy(older_than=dt.timedelta(days=7)),
            now=NOW,
            descendant_lookup=lambda _: ([descendant], []),
        )
        self.assertEqual(1, len(plan.candidates))
        self.assertEqual((descendant,), plan.candidates[0].descendants)
        rendered = cleanup.render_plan(plan_document(plan))
        self.assertIn("Cascaded descendants: 1", rendered)
        self.assertIn("descendant", rendered)

    def test_pinned_descendant_blocks_root(self) -> None:
        root = make_session(1, age_days=30)
        descendant = make_session(2, age_days=20, pinned=True, parent_id=root.id)
        plan = cleanup.build_plan(
            [root],
            make_policy(older_than=dt.timedelta(days=7)),
            now=NOW,
            descendant_lookup=lambda _: ([descendant], []),
        )
        self.assertFalse(plan.candidates)
        self.assertEqual("protected", plan.groups[0].status)
        self.assertIn("pinned", plan.groups[0].reasons)

    def test_active_descendant_blocks_root(self) -> None:
        root = make_session(1, age_days=30)
        descendant = make_session(2, age_days=20, status="active", parent_id=root.id)
        plan = cleanup.build_plan(
            [root],
            make_policy(older_than=dt.timedelta(days=7)),
            now=NOW,
            descendant_lookup=lambda _: ([descendant], []),
        )
        self.assertFalse(plan.candidates)
        self.assertEqual("protected", plan.groups[0].status)
        self.assertIn("running", plan.groups[0].reasons)

    def test_outside_project_descendant_blocks_root(self) -> None:
        root = make_session(1, age_days=30)
        descendant = make_session(
            2, age_days=20, cwd=OTHER_PROJECT, parent_id=root.id
        )
        plan = cleanup.build_plan(
            [root],
            make_policy(older_than=dt.timedelta(days=7)),
            now=NOW,
            descendant_lookup=lambda _: ([descendant], []),
        )
        self.assertFalse(plan.candidates)
        self.assertEqual("excluded", plan.groups[0].status)
        self.assertIsNone(plan.groups[0].rank)
        self.assertIn("related-outside-scope", plan.groups[0].reasons)

    def test_unknown_descendant_impact_blocks_root(self) -> None:
        root = make_session(1, age_days=30)
        issue = cleanup.UnsafeRecord(None, "missing descendant cwd")
        plan = cleanup.build_plan(
            [root],
            make_policy(older_than=dt.timedelta(days=7), action="delete"),
            now=NOW,
            descendant_lookup=lambda _: ([], [issue]),
        )
        self.assertFalse(plan.candidates)
        self.assertEqual("excluded", plan.groups[0].status)
        self.assertIn("metadata-malformed", plan.groups[0].reasons)


class ApplySafetyTests(unittest.TestCase):
    def test_stale_preview_detection(self) -> None:
        policy = make_policy(older_than=dt.timedelta(days=7))
        before = cleanup.build_plan([make_session(1, age_days=30)], policy, now=NOW)
        after = cleanup.build_plan([make_session(1, age_days=29)], policy, now=NOW)
        with self.assertRaises(cleanup.StaleStateError):
            cleanup.require_fresh_plan(before, after)

    def test_archive_apply_uses_exact_uuid_argument_array(self) -> None:
        candidate = cleanup.Candidate(make_session(1, age_days=30))
        commands: list[list[str]] = []

        def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        report = cleanup.mutate_candidates([candidate], "archive", "codex", runner=runner)
        self.assertEqual([["codex", "archive", session_id(1)]], commands)
        self.assertEqual((session_id(1),), report.successful)

    def test_permanent_delete_requires_explicit_action_and_force(self) -> None:
        default_args = cleanup.parse_args(["--older-than", "30d"])
        self.assertEqual("archive", default_args.action)
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cleanup.parse_args(
                    ["--older-than", "30d", "--action", "delete", "--apply"]
                )

        commands: list[list[str]] = []

        def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        cleanup.mutate_candidates(
            [cleanup.Candidate(make_session(1, age_days=30))],
            "delete",
            "codex",
            runner=runner,
        )
        self.assertEqual(
            [["codex", "delete", session_id(1), "--force"]], commands
        )

    def test_partial_codex_command_failure_is_reported(self) -> None:
        candidates = [
            cleanup.Candidate(make_session(1, age_days=30)),
            cleanup.Candidate(make_session(2, age_days=40)),
            cleanup.Candidate(make_session(3, age_days=50)),
        ]
        commands: list[list[str]] = []

        def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            if command[2] == session_id(1):
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                command, 1, stdout="", stderr="thread is in use"
            )

        report = cleanup.mutate_candidates(candidates, "archive", "codex", runner=runner)
        self.assertEqual((session_id(1),), report.successful)
        self.assertEqual(1, len(report.failed))
        self.assertEqual("thread is in use", report.failed[0].error)
        self.assertEqual(1, len(report.skipped))
        self.assertEqual(session_id(3), report.skipped[0].session_id)
        self.assertEqual(2, len(commands))

    def test_failed_revalidation_makes_zero_mutation_calls(self) -> None:
        candidate = cleanup.Candidate(make_session(1, age_days=30))
        commands: list[list[str]] = []

        def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        def stale(_: object) -> None:
            raise cleanup.StaleStateError("changed")

        report = cleanup.mutate_candidates(
            [candidate], "archive", "codex", runner=runner, revalidate=stale
        )
        self.assertFalse(commands)
        self.assertEqual(1, len(report.skipped))
        self.assertIn("changed", report.stopped_reason)

    def test_cleanup_lock_rejects_overlapping_invocations(self) -> None:
        with cleanup.CleanupLock(CODEX_HOME):
            with self.assertRaises(cleanup.LockError):
                with cleanup.CleanupLock(CODEX_HOME):
                    self.fail("second lock unexpectedly succeeded")


class ManifestAndRenderingTests(unittest.TestCase):
    def make_delete_plan(self) -> cleanup.Plan:
        root = make_session(1, age_days=45)
        child = make_session(2, age_days=40, parent_id=root.id)
        return cleanup.build_plan(
            [root],
            make_policy(older_than=dt.timedelta(days=30), action="delete"),
            now=NOW,
            descendant_lookup=lambda _: ([child], []),
        )

    def test_saved_manifest_is_private_and_round_trips(self) -> None:
        plan = self.make_delete_plan()
        document = cleanup.plan_document(
            plan,
            CODEX_HOME,
            codex_version="codex-cli 0.test",
            state="ready",
            plan_id=session_id(700),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cleanup-plan.json"
            cleanup.save_plan_manifest(path, document)
            if os.name != "nt":
                self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            saved = cleanup.load_saved_plan(path)
        self.assertEqual(session_id(700), saved.document["planId"])
        self.assertEqual(
            cleanup.candidate_snapshot(plan.candidates),
            cleanup.candidate_snapshot(saved.candidates),
        )
        self.assertEqual(plan.policy.cutoff, saved.policy.cutoff)
        self.assertEqual("codex-cli 0.test", saved.codex_version)

    def test_legacy_ranking_description_remains_loadable_and_revalidated(self) -> None:
        document = cleanup.plan_document(
            self.make_delete_plan(),
            CODEX_HOME,
            codex_version="codex-cli 0.test",
            state="ready",
        )
        document["policy"]["latestRankingUniverse"] = (
            "ordinary interactive roots and all discoverable spawned descendants "
            "within the selected project scope"
        )
        document["integrity"] = cleanup.document_integrity(document)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy-plan.json"
            cleanup.save_plan_manifest(path, document)
            saved = cleanup.load_saved_plan(path)
        self.assertEqual("ready", saved.document["state"])
        self.assertEqual("delete", saved.policy.action)

    def test_manifest_paths_inside_codex_home_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "codex-home"
            home.mkdir()
            with self.assertRaisesRegex(cleanup.ManifestError, "outside Codex home"):
                cleanup.require_manifest_outside_codex_home(
                    home / "cleanup-plan.json", str(home)
                )
            cleanup.require_manifest_outside_codex_home(
                Path(temp_dir) / "plans" / "cleanup-plan.json", str(home)
            )

    def test_manifest_with_extra_or_modified_fields_is_rejected(self) -> None:
        document = cleanup.plan_document(
            self.make_delete_plan(),
            CODEX_HOME,
            codex_version="codex-cli 0.test",
            state="ready",
        )
        document["unexpected"] = True
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cleanup-plan.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaises(cleanup.ManifestError):
                cleanup.load_saved_plan(path)

    def test_manifest_rejects_inconsistent_cutoff_even_with_updated_digest(self) -> None:
        document = cleanup.plan_document(
            self.make_delete_plan(),
            CODEX_HOME,
            codex_version="codex-cli 0.test",
            state="ready",
        )
        document["policy"]["absoluteCutoff"] += 1
        document["integrity"] = cleanup.document_integrity(document)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cleanup-plan.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaisesRegex(cleanup.ManifestError, "inconsistent"):
                cleanup.load_saved_plan(path)

    @unittest.skipIf(os.name == "nt", "POSIX permission test")
    def test_manifest_with_public_permissions_is_rejected(self) -> None:
        document = cleanup.plan_document(
            self.make_delete_plan(),
            CODEX_HOME,
            codex_version="codex-cli 0.test",
            state="ready",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cleanup-plan.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaisesRegex(cleanup.ManifestError, "permissions"):
                cleanup.load_saved_plan(path)

    def test_consumed_or_partially_consumed_manifest_cannot_replay(self) -> None:
        document = cleanup.plan_document(
            self.make_delete_plan(),
            CODEX_HOME,
            codex_version="codex-cli 0.test",
            state="ready",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cleanup-plan.json"
            cleanup.save_plan_manifest(path, document)
            saved = cleanup.load_saved_plan(path)
            cleanup.update_plan_manifest(
                saved,
                state="applying",
                application={
                    "startedAt": int(NOW.timestamp()),
                    "successfulRoots": [],
                },
            )
            with self.assertRaisesRegex(cleanup.ManifestError, "consumed"):
                cleanup.load_saved_plan(path)

    def test_candidate_comparison_detects_every_relevant_change(self) -> None:
        original = self.make_delete_plan().candidates
        root = original[0].root
        child = original[0].descendants[0]
        variants = (
            (),
            (cleanup.Candidate(root, (child, make_session(3, age_days=35, parent_id=root.id))),),
            (cleanup.Candidate(root, (dataclasses.replace(child, pinned=True),)),),
            (cleanup.Candidate(root, (dataclasses.replace(child, status="active"),)),),
            (
                cleanup.Candidate(
                    dataclasses.replace(root, recency_at=root.recency_at + dt.timedelta(hours=1)),
                    (child,),
                ),
            ),
        )
        for current in variants:
            with self.subTest(current=current):
                with self.assertRaises(cleanup.StaleStateError):
                    cleanup.require_candidate_match(original, current)

    def test_preview_json_and_saved_manifest_are_separate_representations(self) -> None:
        plan = self.make_delete_plan()
        manifest = cleanup.plan_document(
            plan,
            CODEX_HOME,
            codex_version="codex-cli 0.test",
            state="ready",
        )
        preview = cleanup.shared_preview_document(
            plan,
            CODEX_HOME,
            codex_version="codex-cli 0.test",
            include_titles=True,
        )
        parsed = json.loads(cleanup.render_plan_json(preview))
        rendered = cleanup.render_shared_preview(preview, details=True)
        self.assertEqual("codex-session-cleanup-plan", manifest["format"])
        self.assertEqual("session-cleanup/preview", parsed["schema"])
        self.assertNotIn("integrity", parsed)
        self.assertEqual(
            manifest["candidates"][0]["root"]["id"],
            parsed["groups"][0]["group_id"],
        )
        self.assertIn(session_id(1), rendered)
        self.assertIn(session_id(2), rendered)
        self.assertIn("PERMANENT DELETION IS IRREVERSIBLE", rendered)
        self.assertIn("parent:", rendered)

    def test_titles_are_sanitized_and_truncated(self) -> None:
        unsafe = "title\x1b[31m\n" + "x" * 300
        cleaned = cleanup.sanitize_title(unsafe)
        self.assertNotIn("\x1b", cleaned)
        self.assertNotIn("\n", cleaned)
        self.assertLessEqual(len(cleaned), 200)

    def test_candidate_ordering_is_deterministic(self) -> None:
        roots = [
            make_session(3, age_days=30),
            make_session(1, age_days=10),
            make_session(2, age_days=20),
        ]
        plan = cleanup.build_plan(
            roots,
            make_policy(older_than=dt.timedelta(days=7)),
            now=NOW,
            descendant_lookup=lambda _: ([], []),
        )
        self.assertEqual(
            [session_id(1), session_id(2), session_id(3)],
            [item.root.id for item in plan.candidates],
        )


@unittest.skipIf(os.name == "nt", "fake Codex launcher is POSIX-only")
class IsolatedFakeCodexIntegrationTests(unittest.TestCase):
    def make_fake_codex(self, base: Path) -> Path:
        fake_codex = base / "codex"
        fake_codex.write_text(
            (ROOT / "tests" / "fake_codex.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        fake_codex.chmod(0o700)
        return fake_codex

    def test_cli_json_preview_matches_shared_schema_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            fake_codex = self.make_fake_codex(base)
            fake_home = base / "codex-home"
            fake_home.mkdir()
            state_path = base / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "codexHome": str(fake_home),
                        "sections": [{"id": PINNED_SECTION_ID, "name": "Pinned"}],
                        "loaded": [],
                        "active": [raw_thread(1)],
                        "archived": [],
                        "mutations": [],
                    }
                ),
                encoding="utf-8",
            )
            capabilities = cleanup.SchemaCapabilities(("cli", "vscode", "subAgent"))
            output = io.StringIO()
            with (
                mock.patch.object(cleanup, "find_codex", return_value=str(fake_codex)),
                mock.patch.object(
                    cleanup, "inspect_installed_schema", return_value=capabilities
                ),
                mock.patch.object(cleanup, "inspect_mutation_commands"),
                mock.patch.object(cleanup.os, "getcwd", return_value=PROJECT),
                mock.patch.dict(
                    os.environ,
                    {
                        "CODEX_HOME": str(fake_home),
                        "FAKE_CODEX_STATE": str(state_path),
                    },
                    clear=False,
                ),
                redirect_stdout(output),
            ):
                exit_code = cleanup.main(["--older-than", "1d", "--json"])

            self.assertEqual(0, exit_code)
            document = json.loads(output.getvalue())
            schema = json.loads(
                (ROOT / "contract" / "preview-v1.schema.json").read_text(
                    encoding="utf-8"
                )
            )
            validate_json_schema(document, schema, schema)
            self.assertEqual("session-cleanup/preview", document["schema"])
            self.assertTrue(
                all(
                    session["title"] is None
                    for group in document["groups"]
                    for session in group["sessions"]
                )
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertFalse(state["mutations"])

    def test_app_server_discovery_and_multi_root_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            fake_codex = self.make_fake_codex(base)
            fake_home = base / "codex-home"
            fake_home.mkdir()
            state_path = base / "state.json"
            state = {
                "codexHome": str(fake_home),
                "sections": [{"id": PINNED_SECTION_ID, "name": "Pinned"}],
                "loaded": [],
                "active": [
                    raw_thread(
                        1,
                        recencyAt=int((NOW - dt.timedelta(days=45)).timestamp()),
                    ),
                    raw_thread(
                        2,
                        parentThreadId=session_id(1),
                        source="subAgent",
                        recencyAt=int((NOW - dt.timedelta(days=40)).timestamp()),
                    ),
                    raw_thread(
                        3,
                        recencyAt=int((NOW - dt.timedelta(days=35)).timestamp()),
                    ),
                ],
                "archived": [],
                "mutations": [],
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")
            environment = dict(os.environ)
            environment.update(
                {
                    "CODEX_HOME": str(fake_home),
                    "FAKE_CODEX_STATE": str(state_path),
                }
            )

            with cleanup.AppServerClient(
                str(fake_codex), environment=environment
            ) as client:
                discovery = cleanup.SessionDiscovery(
                    client,
                    cleanup.SchemaCapabilities(("cli", "vscode", "subAgent")),
                )
                policy = cleanup.Policy(
                    dt.timedelta(days=30), None, "current", "archive", PROJECT
                )
                plan = cleanup.discover_plan(discovery, policy, now=NOW)
                self.assertEqual(
                    [session_id(3), session_id(1)],
                    [item.root.id for item in plan.candidates],
                )
                self.assertEqual(
                    (session_id(2),),
                    tuple(item.id for item in plan.candidates[1].descendants),
                )

                def runner(
                    command: list[str], **kwargs: object
                ) -> subprocess.CompletedProcess[str]:
                    return subprocess.run(command, env=environment, **kwargs)

                def revalidate(remaining: object) -> None:
                    current = cleanup.discover_plan(discovery, plan.policy, now=NOW)
                    cleanup.require_candidate_match(remaining, current.candidates)

                report = cleanup.mutate_candidates(
                    plan.candidates,
                    "archive",
                    str(fake_codex),
                    runner=runner,
                    revalidate=revalidate,
                )

            final_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual((session_id(3), session_id(1)), report.successful)
            self.assertFalse(final_state["active"])
            self.assertEqual(2, len(final_state["mutations"]))
            self.assertEqual(
                {session_id(1), session_id(2), session_id(3)},
                {item["id"] for item in final_state["archived"]},
            )

    def test_saved_delete_plan_applies_exact_fake_candidate_set(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            fake_codex = self.make_fake_codex(base)
            fake_home = base / "codex-home"
            fake_home.mkdir()
            state_path = base / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "codexHome": str(fake_home),
                        "sections": [
                            {"id": PINNED_SECTION_ID, "name": "Pinned"}
                        ],
                        "loaded": [],
                        "active": [
                            raw_thread(
                                1,
                                recencyAt=int(
                                    (NOW - dt.timedelta(days=45)).timestamp()
                                ),
                            ),
                            raw_thread(
                                2,
                                parentThreadId=session_id(1),
                                source="subAgent",
                                recencyAt=int(
                                    (NOW - dt.timedelta(days=40)).timestamp()
                                ),
                            ),
                        ],
                        "archived": [],
                        "mutations": [],
                    }
                ),
                encoding="utf-8",
            )
            manifest = base / "cleanup-plan.json"
            environment = {
                "CODEX_HOME": str(fake_home),
                "FAKE_CODEX_STATE": str(state_path),
            }
            capabilities = cleanup.SchemaCapabilities(
                ("cli", "vscode", "subAgent")
            )
            patches = (
                mock.patch.object(cleanup, "find_codex", return_value=str(fake_codex)),
                mock.patch.object(
                    cleanup, "inspect_installed_schema", return_value=capabilities
                ),
                mock.patch.object(cleanup, "inspect_mutation_commands"),
                mock.patch.object(cleanup.os, "getcwd", return_value=PROJECT),
                mock.patch.dict(os.environ, environment, clear=False),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                with redirect_stdout(io.StringIO()):
                    preview_exit = cleanup.main(
                        [
                            "--older-than",
                            "30d",
                            "--action",
                            "delete",
                            "--save-plan",
                            str(manifest),
                        ]
                    )
                    apply_exit = cleanup.main(["--apply-plan", str(manifest)])

            self.assertEqual(0, preview_exit)
            self.assertEqual(0, apply_exit)
            final_state = json.loads(state_path.read_text(encoding="utf-8"))
            final_manifest = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertFalse(final_state["active"])
            self.assertEqual(
                [{"action": "delete", "sessionId": session_id(1)}],
                final_state["mutations"],
            )
            self.assertEqual("consumed", final_manifest["state"])
            self.assertTrue(final_manifest["changesMade"])

    def test_new_descendant_makes_saved_plan_stale_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            fake_codex = self.make_fake_codex(base)
            fake_home = base / "codex-home"
            fake_home.mkdir()
            state_path = base / "state.json"
            old_recency = int(
                (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=45)).timestamp()
            )
            state = {
                "codexHome": str(fake_home),
                "sections": [{"id": PINNED_SECTION_ID, "name": "Pinned"}],
                "loaded": [],
                "active": [raw_thread(1, recencyAt=old_recency)],
                "archived": [],
                "mutations": [],
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")
            manifest = base / "cleanup-plan.json"
            environment = {
                "CODEX_HOME": str(fake_home),
                "FAKE_CODEX_STATE": str(state_path),
            }
            capabilities = cleanup.SchemaCapabilities(
                ("cli", "vscode", "subAgent")
            )
            patches = (
                mock.patch.object(cleanup, "find_codex", return_value=str(fake_codex)),
                mock.patch.object(
                    cleanup, "inspect_installed_schema", return_value=capabilities
                ),
                mock.patch.object(cleanup, "inspect_mutation_commands"),
                mock.patch.object(cleanup.os, "getcwd", return_value=PROJECT),
                mock.patch.dict(os.environ, environment, clear=False),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                with redirect_stdout(io.StringIO()):
                    preview_exit = cleanup.main(
                        [
                            "--older-than",
                            "30d",
                            "--action",
                            "delete",
                            "--save-plan",
                            str(manifest),
                        ]
                    )

                state["active"].append(
                    raw_thread(
                        2,
                        parentThreadId=session_id(1),
                        source="subAgent",
                        recencyAt=int(
                            (
                                dt.datetime.now(dt.timezone.utc)
                                - dt.timedelta(days=1)
                            ).timestamp()
                        ),
                    )
                )
                state_path.write_text(json.dumps(state), encoding="utf-8")

                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    apply_exit = cleanup.main(["--apply-plan", str(manifest)])

            final_state = json.loads(state_path.read_text(encoding="utf-8"))
            final_manifest = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(0, preview_exit)
            self.assertEqual(cleanup.EXIT_STALE_PLAN, apply_exit)
            self.assertFalse(final_state["mutations"])
            self.assertEqual("stale", final_manifest["state"])
            self.assertFalse(final_manifest["changesMade"])


class InterfaceValidationTests(unittest.TestCase):
    def test_duration_parsing_and_invalid_durations(self) -> None:
        self.assertEqual(dt.timedelta(hours=48), cleanup.parse_duration("48h"))
        self.assertEqual(dt.timedelta(days=7), cleanup.parse_duration("7d"))
        for value in ("", "0h", "1.5d", "7days", "-1d", " 7d", "7D"):
            with self.subTest(value=value):
                with self.assertRaises(cleanup.argparse.ArgumentTypeError):
                    cleanup.parse_duration(value)

    def test_no_selector_is_refused(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cleanup.parse_args([])
        with self.assertRaises(cleanup.CleanupError):
            cleanup.build_plan([], make_policy(), now=NOW)

    def test_keep_latest_zero_is_refused(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cleanup.parse_args(["--keep-latest", "0"])

    def test_apply_plan_needs_no_policy_but_rejects_policy_flags(self) -> None:
        args = cleanup.parse_args(["--apply-plan", "/tmp/fictional-plan.json"])
        self.assertEqual(Path("/tmp/fictional-plan.json"), args.apply_plan)
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cleanup.parse_args(
                    [
                        "--apply-plan",
                        "/tmp/fictional-plan.json",
                        "--older-than",
                        "30d",
                    ]
                )

    def test_compatibility_check_needs_no_selector(self) -> None:
        args = cleanup.parse_args(["--check-compatibility", "--json"])
        self.assertTrue(args.check_compatibility)

    def test_save_plan_cannot_apply_in_same_command(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cleanup.parse_args(
                    ["--older-than", "30d", "--save-plan", "plan.json", "--apply"]
                )

    def test_shared_json_is_preview_only_and_titles_are_explicit(self) -> None:
        args = cleanup.parse_args(
            ["--older-than", "30d", "--json", "--include-titles"]
        )
        self.assertTrue(args.json)
        self.assertTrue(args.include_titles)
        for arguments in (
            ["--older-than", "30d", "--include-titles"],
            ["--older-than", "30d", "--json", "--apply"],
            ["--apply-plan", "/tmp/fictional-plan.json", "--json"],
        ):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cleanup.parse_args(arguments)


if __name__ == "__main__":
    unittest.main()
