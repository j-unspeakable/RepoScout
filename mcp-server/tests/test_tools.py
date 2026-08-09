from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from reposcout_mcp import server, tools
from reposcout_mcp.client import RepoScoutClientError


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def search_projects(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("search_projects", *args))
        return {"projects": []}

    def get_project_details(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("get_project_details", *args))
        return {"repo_id": args[0]}

    def save_project(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("save_project", *args))
        return {"repo_id": args[0]}

    def update_project_status(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("update_project_status", *args))
        return {"status": args[1]}

    def add_project_note(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("add_project_note", *args))
        return {"note": args[1]}


def test_tool_functions_are_thin_http_client_delegates(monkeypatch) -> None:
    client = FakeClient()
    monkeypatch.setattr(tools, "get_client", lambda: client)

    tools.search_projects("pipelines", 3, "Python", 100)
    tools.get_project_details(42, 2)
    tools.save_project(42)
    tools.update_project_status(42, "COMPLETED")
    tools.add_project_note(42, "Try it")

    assert client.calls == [
        ("search_projects", "pipelines", 3, "Python", 100),
        ("get_project_details", 42, 2),
        ("save_project", 42),
        ("update_project_status", 42, "COMPLETED"),
        ("add_project_note", 42, "Try it"),
    ]


def test_safe_tool_error_contains_no_raw_failure() -> None:
    def fail() -> dict[str, Any]:
        raise RepoScoutClientError("unavailable", "RepoScout unavailable", retryable=True)

    assert tools.safe_call(fail) == {
        "error": {
            "code": "unavailable",
            "message": "RepoScout unavailable",
            "retryable": True,
        }
    }


@pytest.mark.asyncio
async def test_fastmcp_exposes_exactly_five_typed_tools() -> None:
    async with Client(server.mcp) as client:
        discovered = await client.list_tools()

    assert {tool.name for tool in discovered} == {
        "search_projects",
        "get_project_details",
        "save_project",
        "update_project_status",
        "add_project_note",
    }
    status_tool = next(tool for tool in discovered if tool.name == "update_project_status")
    schema = str(status_tool.inputSchema["properties"]["status"])
    for status in ("INTERESTED", "TO_TRY", "IN_PROGRESS", "COMPLETED"):
        assert status in schema

    by_name = {tool.name: tool for tool in discovered}
    search_schema = by_name["search_projects"].inputSchema["properties"]
    assert search_schema["query"]["minLength"] == 1
    assert search_schema["query"]["maxLength"] == 500
    assert search_schema["top_k"]["minimum"] == 1
    assert search_schema["top_k"]["maximum"] == 10
    details_schema = by_name["get_project_details"].inputSchema["properties"]
    assert details_schema["repo_id"]["minimum"] == 1
    assert details_schema["evidence_limit"]["maximum"] == 5
    note_schema = by_name["add_project_note"].inputSchema["properties"]
    assert note_schema["note"]["maxLength"] == 2000


def test_mcp_source_has_no_backend_or_database_implementation() -> None:
    source_root = Path(__file__).parents[1] / "src" / "reposcout_mcp"
    source = "\n".join(path.read_text() for path in source_root.glob("*.py"))
    forbidden = (
        "psycopg",
        "sentence_transformers",
        "pgvector",
        "repository_chunks",
        "app.repositories",
        "app.services",
        "lakebase",
    )
    assert all(term not in source.lower() for term in forbidden)
