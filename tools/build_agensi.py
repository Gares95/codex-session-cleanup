#!/usr/bin/env python3
"""Build the deterministic Agensi distribution from canonical tracked files.

Agensi writes each packaged script unchanged to a temporary file and executes
it independently.  The canonical Codex helper is already a standalone script,
so this builder copies it byte-for-byte instead of generating or transforming
another implementation.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
from pathlib import Path
import re
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
TOP = "codex-session-cleanup"
SCRIPT = "scripts/cleanup_sessions.py"
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o100644

PACKAGE_SOURCES = {
    "SKILL.md": ROOT / "packaging" / "agensi" / "SKILL.md",
    "LICENSE": ROOT / "LICENSE",
    "references/safety.md": ROOT / "packaging" / "agensi" / "references" / "safety.md",
    SCRIPT: ROOT / SCRIPT,
}

ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "argparse",
        "dataclasses",
        "datetime",
        "fcntl",
        "hashlib",
        "json",
        "msvcrt",
        "os",
        "pathlib",
        "re",
        "shlex",
        "shutil",
        "stat",
        "subprocess",
        "sys",
        "tempfile",
        "typing",
        "unicodedata",
        "uuid",
    }
)
NETWORK_IMPORTS = frozenset(
    {"aiohttp", "ftplib", "http", "requests", "smtplib", "socket", "ssl", "urllib"}
)
FORBIDDEN_CALLS = frozenset({"eval", "exec", "compile", "__import__"})
FORBIDDEN_OS_CALLS = frozenset(
    {"system", "popen", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe"}
)
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)


class BuildError(RuntimeError):
    """The package cannot be built without violating its release contract."""


def read_source(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise BuildError(f"package source is not a regular file: {path.relative_to(ROOT)}")
    data = path.read_bytes()
    if b"\r" in data:
        raise BuildError(f"package source must use LF line endings: {path.relative_to(ROOT)}")
    data.decode("utf-8")
    return data


def tool_version(script: bytes | None = None) -> str:
    tree = ast.parse((script or read_source(ROOT / SCRIPT)).decode("utf-8"), SCRIPT)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if [getattr(target, "id", None) for target in node.targets] == ["TOOL_VERSION"]:
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
                return value
    raise BuildError("canonical script has no valid TOOL_VERSION")


def frontmatter_fields(text: str) -> dict[str, str]:
    match = FRONTMATTER_RE.match(text)
    if match is None:
        raise BuildError("Agensi SKILL.md has no YAML frontmatter")
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if line.startswith((" ", "-")) or ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def validate_skill(data: bytes, version: str) -> None:
    fields = frontmatter_fields(data.decode("utf-8"))
    expected = {
        "name": TOP,
        "version": version,
        "license": "MIT",
        "docs_url": "https://github.com/Gares95/codex-session-cleanup",
    }
    for key, value in expected.items():
        if fields.get(key) != value:
            raise BuildError(f"Agensi SKILL.md must declare {key}: {value}")
    if not fields.get("description") or not fields.get("summary"):
        raise BuildError("Agensi SKILL.md needs description and summary")


def validate_script(data: bytes) -> None:
    text = data.decode("utf-8")
    tree = ast.parse(text, SCRIPT)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise BuildError(f"{SCRIPT}:{node.lineno}: relative import")
            imports.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Name) and node.id == "__file__":
            raise BuildError(f"{SCRIPT}:{node.lineno}: runtime script-directory dependency")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                raise BuildError(f"{SCRIPT}:{node.lineno}: call to {node.func.id}")
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
                and node.func.attr in FORBIDDEN_OS_CALLS
            ):
                raise BuildError(f"{SCRIPT}:{node.lineno}: call to os.{node.func.attr}")
            for keyword in node.keywords:
                if keyword.arg == "shell":
                    raise BuildError(f"{SCRIPT}:{node.lineno}: subprocess shell argument")
    unexpected = imports - ALLOWED_IMPORTS
    if unexpected:
        raise BuildError(f"unreviewed script imports: {sorted(unexpected)}")
    if imports & NETWORK_IMPORTS:
        raise BuildError(f"network imports are not allowed: {sorted(imports & NETWORK_IMPORTS)}")
    if re.search(r"[A-Za-z0-9+/=]{200,}", text):
        raise BuildError("encoded-looking source is not allowed")
    compile(text, SCRIPT, "exec", dont_inherit=True)


def package_files() -> tuple[str, dict[str, bytes]]:
    values = {name: read_source(path) for name, path in PACKAGE_SOURCES.items()}
    version = tool_version(values[SCRIPT])
    validate_skill(values["SKILL.md"], version)
    validate_script(values[SCRIPT])
    return version, values


def zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(f"{TOP}/{name}", ZIP_TIME)
    info.create_system = 3
    info.external_attr = FILE_MODE << 16
    info.compress_type = zipfile.ZIP_STORED
    info.extra = b""
    info.comment = b""
    return info


def build(output_dir: str | Path | None = None) -> Path:
    version, files = package_files()
    destination = Path(output_dir) if output_dir is not None else ROOT / "dist" / "agensi"
    destination.mkdir(parents=True, exist_ok=True)
    archive_path = destination / f"{TOP}-{version}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.comment = b""
        for name in sorted(files):
            archive.writestr(zip_info(name), files[name])
    return archive_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="write the ZIP here instead of dist/agensi")
    args = parser.parse_args(argv)
    try:
        archive_path = build(args.output_dir)
    except (BuildError, OSError, UnicodeError, SyntaxError) as exc:
        print(f"Agensi package build failed: {exc}", file=sys.stderr)
        return 1
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
    print(archive_path)
    print(f"size: {archive_path.stat().st_size} bytes")
    print(f"sha256: {digest}")
    print("contents:")
    for name in names:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
