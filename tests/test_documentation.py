from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def test_documentation_matches_committed_databricks_resource_contracts() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text()
    main_app = (PROJECT_ROOT / "app.yaml").read_text()
    mcp_app = (PROJECT_ROOT / "mcp-server" / "app.yaml").read_text()

    for resource_key in ("postgres", "github_token", "openrouter_api_key", "supervisor_endpoint"):
        assert f"valueFrom: {resource_key}" in main_app
        assert f"`{resource_key}`" in readme

    assert "valueFrom: reposcout" in mcp_app
    assert "name: REPOSCOUT_APP_NAME" in mcp_app
    assert "`mcp-repo-scout`" in readme
    assert "resource key `reposcout`" in readme
    for stale_name in ("mcp-reposcout", "reposcout-api", "REPOSCOUT_API_APP_NAME"):
        assert stale_name not in readme


def test_readme_assets_and_operational_contracts_exist() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text()
    expected_images = (
        "01-dark-landing-1920x1080.png",
        "04-discover-results.png",
        "07a-ask-recommendations-start.png",
        "16-selected-project-to-try-note.png",
    )
    for image in expected_images:
        assert (PROJECT_ROOT / "artifacts" / "demo-walkthrough-screenshots" / image).is_file()
        assert f"artifacts/demo-walkthrough-screenshots/{image}" in readme

    assert "## Table of Contents" in readme
    assert "`max_repositories` | `50`" in readme
    assert "`INTERESTED`, `TO_TRY`, `IN_PROGRESS`, `COMPLETED`" not in readme
    assert "`Interested`, `To Try`, `In Progress`, or `Completed`" in readme
    assert "uv run alembic upgrade head --sql" in readme
    assert "shared key, `default`" in readme
    assert "UI-managed Job that runs the embedding notebook **once daily**" in readme
    assert "Repository collection is deliberately curated" in readme
    assert "manually reviews each" in readme
    assert "request at an approval gate" in readme
    assert "it has no path to GitHub, the ingestion service, or the Spark pipeline" in readme
    assert (
        "artifacts/**/ACCEPTANCE_REPORT.md"
        in (PROJECT_ROOT / ".gitignore").read_text().splitlines()
    )

    assert readme.count("```mermaid") == 1
    for boundary in (
        "Databricks App: repo-scout",
        "Databricks App: mcp-repo-scout",
        "Databricks serving endpoint",
        "Databricks Job / Spark notebook",
        "Databricks Lakebase",
        "External services",
    ):
        assert boundary in readme


def test_local_environment_template_requires_process_app_env() -> None:
    template_lines = (PROJECT_ROOT / ".env.example").read_text().splitlines()

    assert "APP_ENV=local" not in template_lines
    assert any("Export APP_ENV=local" in line for line in template_lines)
    assert "GITHUB_API_URL=https://api.github.com" in template_lines
    assert "DB_POOL_TIMEOUT_SECONDS=30" in template_lines
