#!/usr/bin/env python3
"""Isolated fake Codex CLI/app-server used only by integration-style tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any


def state_path() -> Path:
    value = os.environ.get("FAKE_CODEX_STATE")
    if not value:
        raise RuntimeError("FAKE_CODEX_STATE is required")
    return Path(value)


def load_state() -> dict[str, Any]:
    return json.loads(state_path().read_text(encoding="utf-8"))


def save_state(value: dict[str, Any]) -> None:
    state_path().write_text(json.dumps(value), encoding="utf-8")


def descendants(state: dict[str, Any], root_id: str) -> set[str]:
    records = [*state.get("active", []), *state.get("archived", [])]
    affected = {root_id}
    changed = True
    while changed:
        changed = False
        for record in records:
            if record.get("parentThreadId") in affected and record["id"] not in affected:
                affected.add(record["id"])
                changed = True
    return affected


def mutate(action: str, session_id: str) -> int:
    state = load_state()
    if session_id in state.get("failMutationIds", []):
        print(f"fake Codex refused {action} for {session_id}", file=sys.stderr)
        return 1
    affected = descendants(state, session_id)
    if action == "archive":
        moved = [item for item in state.get("active", []) if item["id"] in affected]
        state["active"] = [
            item for item in state.get("active", []) if item["id"] not in affected
        ]
        state.setdefault("archived", []).extend(moved)
    else:
        for scope in ("active", "archived"):
            state[scope] = [
                item for item in state.get(scope, []) if item["id"] not in affected
            ]
    state.setdefault("mutations", []).append(
        {"action": action, "sessionId": session_id}
    )
    save_state(state)
    return 0


def is_descendant(records: list[dict[str, Any]], item: dict[str, Any], root_id: str) -> bool:
    by_id = {record["id"]: record for record in records}
    parent_id = item.get("parentThreadId")
    visited: set[str] = set()
    while parent_id is not None and parent_id not in visited:
        if parent_id == root_id:
            return True
        visited.add(parent_id)
        parent = by_id.get(parent_id)
        parent_id = parent.get("parentThreadId") if parent else None
    return False


def list_threads(state: dict[str, Any], params: dict[str, Any]) -> list[dict[str, Any]]:
    records = list(state["archived" if params.get("archived") else "active"])
    ancestor = params.get("ancestorThreadId")
    if ancestor:
        records = [item for item in records if is_descendant(records, item, ancestor)]
    cwd = params.get("cwd")
    if cwd:
        records = [item for item in records if item.get("cwd") == cwd]
    source_kinds = params.get("sourceKinds")
    if source_kinds:
        records = [item for item in records if item.get("source") in source_kinds]
    section_id = params.get("sectionId")
    if section_id:
        records = [item for item in records if item.get("section") == section_id]

    def order(item: dict[str, Any]) -> tuple[int, str]:
        recency = item.get("recencyAt")
        return (-(recency if isinstance(recency, int) else -1), str(item.get("id", "")))

    return sorted(records, key=order)


def schema_documents() -> list[dict[str, Any]]:
    thread_fields = {
        name: {}
        for name in (
            "id", "name", "preview", "createdAt", "updatedAt", "recencyAt",
            "cwd", "ephemeral", "section", "sessionId", "parentThreadId",
            "source", "status",
        )
    }
    documents: list[dict[str, Any]] = [
        {
            "title": "ThreadListParams",
            "properties": {
                name: {}
                for name in (
                    "ancestorThreadId", "archived", "cursor", "cwd", "limit",
                    "sectionId", "sortKey", "sortDirection", "sourceKinds",
                    "useStateDbOnly",
                )
            },
            "definitions": {
                "ThreadSourceKind": {"enum": ["cli", "vscode", "subAgent"]}
            },
        },
        {
            "title": "ThreadListResponse",
            "properties": {"data": {}, "nextCursor": {}},
            "definitions": {"Thread": {"properties": thread_fields}},
        },
        {"title": "InitializeResponse", "properties": {"codexHome": {}}},
        {
            "title": "ThreadSectionListParams",
            "properties": {"cursor": {}, "limit": {}},
        },
        {
            "title": "ThreadSectionListResponse",
            "properties": {"data": {}, "nextCursor": {}},
            "definitions": {
                "ThreadSection": {"properties": {"id": {}, "name": {}}}
            },
        },
        {
            "title": "ThreadLoadedListParams",
            "properties": {"cursor": {}, "limit": {}},
        },
        {
            "title": "ThreadLoadedListResponse",
            "properties": {"data": {}, "nextCursor": {}},
        },
    ]
    if os.environ.get("FAKE_CODEX_SCHEMA_MODE") == "missing-recency":
        del documents[1]["definitions"]["Thread"]["properties"]["recencyAt"]
    return documents


def write_schema(output_dir: str) -> int:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    for index, document in enumerate(schema_documents()):
        (root / f"schema-{index}.json").write_text(
            json.dumps(document), encoding="utf-8"
        )
    return 0


def serve() -> int:
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method = message.get("method")
        params = message.get("params") or {}
        state = load_state()
        if method == "initialize":
            result: dict[str, Any] = {
                "codexHome": state["codexHome"],
                "platformFamily": "unix",
                "platformOs": "linux",
                "userAgent": "fake-codex/1",
            }
        elif method == "threadSection/list":
            result = {"data": state.get("sections", []), "nextCursor": None}
        elif method == "thread/loaded/list":
            result = {"data": state.get("loaded", []), "nextCursor": None}
        elif method == "thread/list":
            result = {"data": list_threads(state, params), "nextCursor": None}
        else:
            response = {
                "id": message["id"],
                "error": {"message": f"unsupported fake method: {method}"},
            }
            print(json.dumps(response), flush=True)
            continue
        print(json.dumps({"id": message["id"], "result": result}), flush=True)
    return 0


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["--version"]:
        print(os.environ.get("FAKE_CODEX_VERSION", "codex-cli 0.fake"))
        return 0
    if arguments == ["archive", "--help"]:
        print("Usage: codex archive <SESSION>")
        return 0
    if arguments == ["delete", "--help"]:
        suffix = " --force" if os.environ.get("FAKE_CODEX_HELP_MODE") != "missing-force" else ""
        print(f"Usage: codex delete <SESSION>{suffix}")
        return 0
    if (
        len(arguments) == 5
        and arguments[:3] == ["app-server", "generate-json-schema", "--experimental"]
        and arguments[3] == "--out"
    ):
        return write_schema(arguments[4])
    if len(arguments) >= 2 and arguments[0] in ("archive", "delete"):
        return mutate(arguments[0], arguments[1])
    if arguments[:2] == ["app-server", "--stdio"]:
        return serve()
    print("unsupported fake Codex invocation", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
