from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

import test_cleanup_sessions as canonical_tests


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_SCRIPT = ROOT / "scripts" / "cleanup_sessions.py"
BUILDER_PATH = ROOT / "tools" / "build_agensi.py"
FAKE_CODEX_SOURCE = ROOT / "tests" / "fake_codex.py"
PINNED_SECTION_ID = "10000000-0000-4000-8000-000000000900"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


builder = load_module(BUILDER_PATH, "codex_agensi_builder_tests")


class AgensiPackageBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="codex-agensi-package-")
        cls.root = Path(cls.temp.name)
        cls.first = builder.build(cls.root / "first")
        cls.second = builder.build(cls.root / "second")
        cls.extracted = cls.root / "extracted"
        with zipfile.ZipFile(cls.first) as archive:
            for info in archive.infolist():
                if info.filename.startswith("/") or ".." in Path(info.filename).parts:
                    raise AssertionError(f"unsafe archive name: {info.filename}")
            archive.extractall(cls.extracted)
        cls.package = cls.extracted / builder.TOP
        cls.script = cls.package / builder.SCRIPT

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_deterministic_archive(self) -> None:
        self.assertEqual(self.first.read_bytes(), self.second.read_bytes())
        self.assertEqual(
            hashlib.sha256(self.first.read_bytes()).hexdigest(),
            hashlib.sha256(self.second.read_bytes()).hexdigest(),
        )

    def test_exact_minimal_contents_and_metadata(self) -> None:
        expected = [
            f"{builder.TOP}/LICENSE",
            f"{builder.TOP}/SKILL.md",
            f"{builder.TOP}/references/safety.md",
            f"{builder.TOP}/scripts/cleanup_sessions.py",
        ]
        with zipfile.ZipFile(self.first) as archive:
            self.assertEqual(expected, archive.namelist())
            self.assertEqual(b"", archive.comment)
            for info in archive.infolist():
                self.assertEqual(builder.ZIP_TIME, info.date_time)
                self.assertEqual(zipfile.ZIP_STORED, info.compress_type)
                self.assertEqual(0o644, (info.external_attr >> 16) & 0o777)
                self.assertEqual(b"", info.extra)
                self.assertFalse(info.is_dir())

    def test_packaged_script_is_byte_identical_to_canonical(self) -> None:
        self.assertEqual(CANONICAL_SCRIPT.read_bytes(), self.script.read_bytes())
        self.assertEqual(114_047, self.script.stat().st_size)

    def test_frontmatter_matches_release(self) -> None:
        text = (self.package / "SKILL.md").read_text(encoding="utf-8")
        fields = builder.frontmatter_fields(text)
        self.assertEqual("codex-session-cleanup", fields["name"])
        self.assertEqual("1.0.0", fields["version"])
        self.assertEqual("MIT", fields["license"])
        self.assertIn("reviewed saved plan", fields["summary"])

    def test_script_is_standalone_and_reviewed(self) -> None:
        text = self.script.read_text(encoding="utf-8")
        tree = ast.parse(text)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(0, node.level)
                imported.add((node.module or "").split(".")[0])
            elif isinstance(node, ast.Name):
                self.assertNotEqual("__file__", node.id)
        self.assertLessEqual(imported, builder.ALLOWED_IMPORTS)
        self.assertFalse(imported & builder.NETWORK_IMPORTS)
        self.assertNotIn("os.system", text)
        self.assertNotRegex(text, r"shell\s*=")
        self.assertNotRegex(text, r"[A-Za-z0-9+/=]{200,}")
        self.assertNotIn("\r", text)

    def test_builder_rejects_unsafe_runtime_constructs(self) -> None:
        snippets = (
            "import socket\n",
            "import urllib.request\n",
            "import os\nos.system('true')\n",
            "import subprocess\nsubprocess.run(['x'], shell=True)\n",
            "eval('1')\n",
            "value = __file__\n",
        )
        for snippet in snippets:
            with self.subTest(snippet=snippet), self.assertRaises(builder.BuildError):
                builder.validate_script(snippet.encode("utf-8"))

    def test_package_has_no_private_data_or_generated_artifacts(self) -> None:
        actual_home = str(Path.home())
        for path in self.package.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(self.package)):
                self.assertNotIn(actual_home, text)
                self.assertNotRegex(text, r"/home/[a-z][^/\s]*/|/Users/[A-Za-z][^/\s]*/")
                self.assertNotRegex(
                    text,
                    r"(?i)(api[_-]?key|secret[_-]?key|token)\s*[:=]\s*['\"][^'\"]{8,}",
                )
                self.assertNotIn("ignore previous instructions", text.lower())
        self.assertFalse(any(path.name == "__pycache__" for path in self.package.rglob("*")))

    def test_lone_script_help_from_unrelated_cwd(self) -> None:
        agent_temp = self.root / "agent-temp"
        project = self.root / "synthetic-project"
        agent_temp.mkdir(exist_ok=True)
        project.mkdir(exist_ok=True)
        lone = agent_temp / "tmp-script.py"
        shutil.copyfile(self.script, lone)
        completed = subprocess.run(
            [sys.executable, "-I", str(lone), "--help"],
            cwd=project,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        for flag in ("--older-than", "--keep-latest", "--apply", "--save-plan", "--apply-plan"):
            self.assertIn(flag, completed.stdout)


@unittest.skipIf(os.name == "nt", "the isolated fake Codex launcher is POSIX-only")
class AgensiIsolatedScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="codex-agensi-run-")
        self.root = Path(self.temp.name)
        archive = builder.build(self.root / "out")
        extracted = self.root / "extracted"
        with zipfile.ZipFile(archive) as package:
            package.extractall(extracted)
        packaged = extracted / builder.TOP / builder.SCRIPT
        self.agent_temp = self.root / "agent-temp"
        self.agent_temp.mkdir()
        self.script = self.agent_temp / "tmp-executed-script.py"
        shutil.copyfile(packaged, self.script)
        self.project = self.root / "synthetic-project"
        self.other_project = self.root / "other-project"
        self.project.mkdir()
        self.other_project.mkdir()
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.fake_codex = self.bin_dir / "codex"
        shutil.copyfile(FAKE_CODEX_SOURCE, self.fake_codex)
        self.fake_codex.chmod(0o700)
        self.state_path = self.root / "state.json"
        self.state = self.make_state([])
        self.write_state()

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def session_id(number: int) -> str:
        return f"10000000-0000-4000-8000-{number:012d}"

    def thread(
        self,
        number: int,
        *,
        age_days: int,
        cwd: Path | None = None,
        parent: int | None = None,
        source: str = "cli",
        pinned: bool = False,
        status: str = "notLoaded",
        malformed: bool = False,
    ) -> dict[str, object]:
        now = int(time.time())
        value: dict[str, object] = {
            "id": self.session_id(number),
            "sessionId": self.session_id(number),
            "parentThreadId": self.session_id(parent) if parent is not None else None,
            "name": None,
            "preview": f"Fictional conversation {number}",
            "createdAt": now - (age_days + 1) * 86400,
            "updatedAt": now - age_days * 86400,
            "recencyAt": now - age_days * 86400,
            "cwd": str(cwd or self.project),
            "section": (
                {"id": PINNED_SECTION_ID, "name": "Pinned"}
                if pinned
                else None
            ),
            "ephemeral": False,
            "source": source,
            "status": {"type": status},
        }
        if malformed:
            value.pop("recencyAt")
        return value

    def make_state(self, active: list[dict[str, object]]) -> dict[str, object]:
        return {
            "codexHome": str(self.codex_home),
            "sections": [{"id": PINNED_SECTION_ID, "name": "Pinned"}],
            "loaded": [],
            "active": active,
            "archived": [],
            "mutations": [],
        }

    def write_state(self) -> None:
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")

    def read_state(self) -> dict[str, object]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def run_script(
        self,
        *args: str,
        script: Path | None = None,
        cwd: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        for key in tuple(environment):
            if key.startswith("PYTHON"):
                environment.pop(key)
        environment.update(
            {
                "PATH": str(self.bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                "CODEX_HOME": str(self.codex_home),
                "FAKE_CODEX_STATE": str(self.state_path),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        environment.update(extra_env or {})
        return subprocess.run(
            [sys.executable, "-I", str(script or self.script), *args],
            cwd=cwd or self.project,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def json_preview(self, *args: str, **kwargs: object) -> dict[str, object]:
        completed = self.run_script(*args, "--json", **kwargs)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def test_compatibility_check_and_current_cwd_are_independent_of_script_path(self) -> None:
        compatible = self.run_script("--check-compatibility")
        self.assertEqual(0, compatible.returncode, compatible.stderr)
        self.assertIn("No changes made.", compatible.stdout)
        self.assertNotEqual(self.agent_temp, self.project)

        self.state = self.make_state(
            [self.thread(1, age_days=45), self.thread(2, age_days=60, cwd=self.other_project)]
        )
        self.write_state()
        document = self.json_preview("--older-than", "30d", "--project", "current")
        self.assertEqual(str(self.project.resolve()), document["scope"]["path"])
        self.assertEqual([self.session_id(1)], [group["group_id"] for group in document["groups"]])
        self.assertEqual("selected", document["groups"][0]["status"])

    def test_age_latest_combined_all_projects_and_title_privacy(self) -> None:
        self.state = self.make_state(
            [
                self.thread(1, age_days=50),
                self.thread(2, age_days=20),
                self.thread(3, age_days=2),
                self.thread(4, age_days=70, cwd=self.other_project),
            ]
        )
        self.write_state()
        age = self.json_preview("--older-than", "30d")
        by_id = {group["group_id"]: group for group in age["groups"]}
        self.assertEqual("selected", by_id[self.session_id(1)]["status"])
        self.assertEqual("retained", by_id[self.session_id(2)]["status"])
        latest = self.json_preview("--keep-latest", "1")
        self.assertEqual(1, sum(group["rank"] == 1 for group in latest["groups"]))
        combined = self.json_preview("--older-than", "30d", "--keep-latest", "1")
        self.assertEqual("selected", {g["group_id"]: g for g in combined["groups"]}[self.session_id(1)]["status"])
        all_projects = self.json_preview("--older-than", "30d", "--project", "all")
        self.assertIn(self.session_id(4), {g["group_id"] for g in all_projects["groups"]})
        self.assertTrue(
            all(session["title"] is None for group in all_projects["groups"] for session in group["sessions"])
        )
        titled = self.json_preview("--older-than", "30d", "--include-titles")
        self.assertTrue(any(session["title"] for group in titled["groups"] for session in group["sessions"]))
        schema = json.loads((ROOT / "contract" / "preview-v1.schema.json").read_text(encoding="utf-8"))
        canonical_tests.validate_json_schema(all_projects, schema, schema)

    def test_pinned_loaded_active_and_malformed_groups_fail_closed(self) -> None:
        pinned = self.thread(1, age_days=60, pinned=True)
        loaded = self.thread(2, age_days=60)
        active = self.thread(3, age_days=60, status="active")
        malformed = self.thread(4, age_days=60, malformed=True)
        self.state = self.make_state([pinned, loaded, active, malformed])
        self.state["loaded"] = [self.session_id(2)]
        self.write_state()
        document = self.json_preview("--older-than", "30d")
        groups = {group["group_id"]: group for group in document["groups"]}
        self.assertEqual("protected", groups[self.session_id(1)]["status"])
        self.assertIn("pinned", groups[self.session_id(1)]["reasons"])
        self.assertEqual("protected", groups[self.session_id(2)]["status"])
        self.assertEqual("protected", groups[self.session_id(3)]["status"])
        self.assertIn(
            {"code": "metadata-malformed", "subject": self.session_id(4)},
            document["warnings"],
        )
        self.assertEqual([], self.read_state()["mutations"])

    def test_archive_preview_then_native_apply_preserves_archived_history(self) -> None:
        root = self.thread(1, age_days=50)
        child = self.thread(2, age_days=45, parent=1, source="subAgent")
        recent = self.thread(3, age_days=2)
        self.state = self.make_state([root, child, recent])
        self.write_state()
        preview = self.run_script("--older-than", "30d", "--action", "archive")
        self.assertEqual(0, preview.returncode, preview.stderr)
        self.assertIn("No changes made.", preview.stdout)
        self.assertEqual([], self.read_state()["mutations"])
        applied = self.run_script("--older-than", "30d", "--action", "archive", "--apply")
        self.assertEqual(0, applied.returncode, applied.stdout + applied.stderr)
        final = self.read_state()
        self.assertEqual([{"action": "archive", "sessionId": self.session_id(1)}], final["mutations"])
        self.assertEqual({self.session_id(1), self.session_id(2)}, {item["id"] for item in final["archived"]})
        self.assertEqual([self.session_id(3)], [item["id"] for item in final["active"]])

    def test_delete_requires_saved_plan_and_replay_is_refused(self) -> None:
        self.state = self.make_state([self.thread(1, age_days=50), self.thread(2, age_days=2)])
        self.write_state()
        rejected = self.run_script("--older-than", "30d", "--action", "delete", "--apply")
        self.assertEqual(2, rejected.returncode)
        self.assertEqual([], self.read_state()["mutations"])
        plan = self.root / "plans" / "delete.json"
        plan.parent.mkdir()
        saved = self.run_script(
            "--older-than", "30d", "--action", "delete", "--save-plan", str(plan)
        )
        self.assertEqual(0, saved.returncode, saved.stdout + saved.stderr)
        self.assertEqual(0o600, stat.S_IMODE(plan.stat().st_mode))
        applied = self.run_script("--apply-plan", str(plan))
        self.assertEqual(0, applied.returncode, applied.stdout + applied.stderr)
        self.assertEqual([{"action": "delete", "sessionId": self.session_id(1)}], self.read_state()["mutations"])
        replay = self.run_script("--apply-plan", str(plan))
        self.assertEqual(2, replay.returncode)
        self.assertIn("already consumed", replay.stderr)

    def test_stale_recency_and_new_descendant_delete_nothing(self) -> None:
        for scenario in ("recency", "descendant"):
            with self.subTest(scenario=scenario):
                self.state = self.make_state([self.thread(1, age_days=50)])
                self.write_state()
                plan = self.root / f"{scenario}.json"
                saved = self.run_script(
                    "--older-than", "30d", "--action", "delete", "--save-plan", str(plan)
                )
                self.assertEqual(0, saved.returncode, saved.stderr)
                if scenario == "recency":
                    self.state["active"][0]["recencyAt"] = int(time.time())
                else:
                    self.state["active"].append(
                        self.thread(9, age_days=1, parent=1, source="subAgent")
                    )
                self.write_state()
                applied = self.run_script("--apply-plan", str(plan))
                self.assertEqual(3, applied.returncode, applied.stdout + applied.stderr)
                self.assertEqual([], self.read_state()["mutations"])

    def test_missing_or_incompatible_codex_and_wrong_home_fail_closed(self) -> None:
        missing = self.run_script(
            "--older-than", "7d",
            extra_env={"PATH": "", "CODEX_MANAGED_PACKAGE_ROOT": ""},
        )
        self.assertEqual(1, missing.returncode)
        self.assertIn("codex executable was not found", missing.stderr)
        incompatible_schema = self.run_script(
            "--older-than", "7d", extra_env={"FAKE_CODEX_SCHEMA_MODE": "missing-recency"}
        )
        self.assertEqual(1, incompatible_schema.returncode)
        self.assertIn("required cleanup safety metadata", incompatible_schema.stderr)
        incompatible_command = self.run_script(
            "--older-than", "7d", extra_env={"FAKE_CODEX_HELP_MODE": "missing-force"}
        )
        self.assertEqual(1, incompatible_command.returncode)
        self.assertIn("lacks the required --force", incompatible_command.stderr)
        wrong_home = self.run_script(
            "--older-than", "7d", extra_env={"CODEX_HOME": str(self.root / "wrong-home")}
        )
        self.assertEqual(1, wrong_home.returncode)
        self.assertIn("different home", wrong_home.stderr)

    def test_lock_conflict_and_partial_native_failure_are_reported(self) -> None:
        self.state = self.make_state([self.thread(1, age_days=40)])
        self.write_state()
        with canonical_tests.cleanup.CleanupLock(str(self.codex_home)):
            locked = self.run_script("--older-than", "30d", "--action", "archive", "--apply")
        self.assertEqual(4, locked.returncode)
        self.assertEqual([], self.read_state()["mutations"])

        self.state = self.make_state([self.thread(1, age_days=40), self.thread(2, age_days=60)])
        self.state["failMutationIds"] = [self.session_id(2)]
        self.write_state()
        partial = self.run_script("--older-than", "30d", "--action", "archive", "--apply")
        self.assertEqual(1, partial.returncode)
        self.assertIn("Failed targets: 1", partial.stdout)
        self.assertEqual([{"action": "archive", "sessionId": self.session_id(1)}], self.read_state()["mutations"])

    @staticmethod
    def normalized_preview(value: object) -> object:
        if isinstance(value, dict):
            return {
                key: AgensiIsolatedScriptTests.normalized_preview(child)
                for key, child in value.items()
                if key not in {
                    "absoluteCutoff",
                    "age_seconds",
                    "createdAt",
                    "cutoff",
                    "evaluated_at",
                    "integrity",
                    "planId",
                }
            }
        if isinstance(value, list):
            return [AgensiIsolatedScriptTests.normalized_preview(child) for child in value]
        return value

    def test_canonical_and_packaged_json_and_saved_plans_agree(self) -> None:
        self.state = self.make_state(
            [self.thread(1, age_days=50), self.thread(2, age_days=5), self.thread(3, age_days=70, cwd=self.other_project)]
        )
        self.write_state()
        args = ("--older-than", "30d", "--keep-latest", "1", "--project", "all", "--json")
        packaged = self.run_script(*args)
        canonical = self.run_script(*args, script=CANONICAL_SCRIPT)
        self.assertEqual((0, 0), (packaged.returncode, canonical.returncode))
        self.assertEqual(
            self.normalized_preview(json.loads(canonical.stdout)),
            self.normalized_preview(json.loads(packaged.stdout)),
        )

        manifests = []
        for label, script in (("canonical", CANONICAL_SCRIPT), ("packaged", self.script)):
            path = self.root / f"{label}-plan.json"
            completed = self.run_script(
                "--older-than", "30d", "--project", "all", "--action", "delete",
                "--save-plan", str(path), script=script,
            )
            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            manifests.append(json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(
            self.normalized_preview(manifests[0]),
            self.normalized_preview(manifests[1]),
        )


if __name__ == "__main__":
    unittest.main()
