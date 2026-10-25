from __future__ import annotations

import datetime as dt
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "cleanup_sessions.py"
SPEC = importlib.util.spec_from_file_location("cleanup_sessions", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
cleanup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cleanup
SPEC.loader.exec_module(cleanup)

NOW = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.timezone.utc)
PROJECT = "/home/user/project"
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
    return (
        list_params,
        list_response,
        initialize_response,
        section_list_params,
        section_list_response,
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
            make_session(2, age_days=10, cwd=PROJECT + "/child"),
            make_session(3, age_days=10, cwd="/home/user/project-sibling"),
        ]
        plan = cleanup.build_plan(
            sessions, make_policy(older_than=dt.timedelta(days=7)), now=NOW
        )
        self.assertEqual([session_id(1)], [item.root.id for item in plan.candidates])
        self.assertEqual(1, plan.examined)

    def test_all_projects_includes_each_exact_cwd(self) -> None:
        sessions = [
            make_session(1, age_days=10),
            make_session(2, age_days=10, cwd="/path/to/other-project"),
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
        self.assertIn("Would archive: 0 root sessions", cleanup.render_plan(plan))


class MetadataAndHomeTests(unittest.TestCase):
    def test_schema_with_section_pin_state_is_accepted(self) -> None:
        capabilities = cleanup.validate_schema_contract(*valid_schema_documents())
        self.assertEqual(("cli", "vscode"), capabilities.all_source_kinds)

    def test_schema_without_section_pin_state_fails_closed(self) -> None:
        schemas = valid_schema_documents()
        schemas[1]["definitions"]["Thread"]["properties"].pop("section")
        with self.assertRaisesRegex(cleanup.CompatibilityError, "Thread.section"):
            cleanup.validate_schema_contract(*schemas)

    def test_alternate_codex_home_is_taken_from_initialize(self) -> None:
        alternate = "/tmp/fictional-codex-home"
        resolved = cleanup.validate_codex_home(
            {"codexHome": alternate}, {"CODEX_HOME": alternate}
        )
        self.assertEqual(alternate, resolved)
        with self.assertRaises(cleanup.ProtocolError):
            cleanup.validate_codex_home(
                {"codexHome": alternate}, {"CODEX_HOME": "/tmp/different-home"}
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
        self.assertIn("Spawned descendants also affected: 1", cleanup.render_plan(plan))

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
        self.assertIn("pinned descendant", plan.protected_unsafe[0].reason)

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
        self.assertIn("unsafe descendant", plan.protected_unsafe[0].reason)

    def test_outside_project_descendant_blocks_root(self) -> None:
        root = make_session(1, age_days=30)
        descendant = make_session(
            2, age_days=20, cwd="/path/to/other-project", parent_id=root.id
        )
        plan = cleanup.build_plan(
            [root],
            make_policy(older_than=dt.timedelta(days=7)),
            now=NOW,
            descendant_lookup=lambda _: ([descendant], []),
        )
        self.assertFalse(plan.candidates)
        self.assertIn("outside current project", plan.protected_unsafe[0].reason)

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
        self.assertIn("descendant metadata", plan.protected_unsafe[0].reason)


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
        delete_args = cleanup.parse_args(
            ["--older-than", "30d", "--action", "delete", "--apply"]
        )
        self.assertEqual("delete", delete_args.action)
        self.assertTrue(delete_args.apply)

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
        ]

        def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            if command[2] == session_id(1):
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                command, 1, stdout="", stderr="thread is in use"
            )

        report = cleanup.mutate_candidates(candidates, "archive", "codex", runner=runner)
        self.assertEqual((session_id(1),), report.successful)
        self.assertEqual(1, len(report.failed))
        self.assertEqual("thread is in use", report.failed[0].error)


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


if __name__ == "__main__":
    unittest.main()
