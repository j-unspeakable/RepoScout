import os

from fastmcp import FastMCP

from reposcout_mcp.config import get_settings
from reposcout_mcp.tools import (
    add_project_note,
    get_project_details,
    save_project,
    search_projects,
    update_project_status,
)

mcp = FastMCP(
    "RepoScout",
    instructions=(
        "Discover open-source projects from RepoScout's indexed README evidence. "
        "Use repository IDs returned by search for details and project-list actions. "
        "Save a project before changing its status or adding a note."
    ),
)

mcp.tool()(search_projects)
mcp.tool()(get_project_details)
mcp.tool()(save_project)
mcp.tool()(update_project_status)
mcp.tool()(add_project_note)


def main() -> None:
    settings = get_settings()
    port = int(os.getenv("DATABRICKS_APP_PORT") or settings.mcp_port)
    mcp.run(transport="http", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
