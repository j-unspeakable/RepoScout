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
    supervisor_instructions = (
        PROJECT_ROOT / "databricks" / "reposcout-supervisor-instructions.md"
    ).read_text()
    job_definition = (
        PROJECT_ROOT / "databricks" / "jobs" / "reposcout-embedding-job.json"
    ).read_text()
    expected_images = (
        "01-dark-landing-1920x1080.png",
        "04-discover-results.png",
        "18-ask-grounded-readme-evidence.png",
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
    assert "runs the embedding\nnotebook **once daily**" in readme
    assert "Repository collection is deliberately curated" in readme
    assert "manually reviews each" in readme
    assert "request at an approval gate" in readme
    assert "it has no path to GitHub, the ingestion service, or the Spark pipeline" in readme
    assert "RepoScout’s application renders repository metadata" in readme
    assert "typed\npresentation contract" in readme
    assert "backend applies a deterministic bounded fallback" in readme
    assert "databricks/reposcout-supervisor-instructions.md" in readme
    assert "You are RepoScout" in supervisor_instructions
    assert "Do not repeat GitHub URLs, repository IDs, stars, forks" in supervisor_instructions
    assert "Interested, To Try, In Progress, and Completed" in supervisor_instructions
    assert "set top_k to the exact number of projects requested" in supervisor_instructions
    assert "Do not over-fetch repositories for internal selection" in supervisor_instructions
    assert "Reuse valid repository IDs already established" in supervisor_instructions
    assert "save every required project first" in supervisor_instructions
    assert "Call each required write tool exactly once" in supervisor_instructions
    assert "mention each repository you are actually recommending exactly once" in (
        supervisor_instructions
    )
    assert "one concise paragraph of no more than 90 words" in supervisor_instructions
    assert "aim for around 220 words" in supervisor_instructions
    assert "aim for around 180 words" in supervisor_instructions
    assert "guidance, not hard limits" in supervisor_instructions
    assert "compact validated repository references" in supervisor_instructions
    assert "write one short confirmation sentence" in supervisor_instructions
    assert "numbered list, bullet list, table, or metadata catalogue" in supervisor_instructions
    assert "one concise final confirmation" in supervisor_instructions
    assert "deploy or update its serving" in supervisor_instructions
    assert "`POST /assistant/messages/stream`" in readme
    assert "`POST /assistant/turns/{turn_id}/cancel`" in readme
    assert "`SUPERVISOR_TASK_TIMEOUT_SECONDS`" in readme
    assert "60-second process-local completion tombstone" in readme
    assert "`DELETE /saved-projects/{repo_id}`" in readme
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" in readme
    assert "Saved-project removal is available only through the browser API" in readme
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

    assert "databricks/jobs/reposcout-embedding-job.json" in readme
    assert "databricks jobs create" in readme
    assert "databricks jobs reset" in readme
    assert '"task_key": "embed_github_repos"' in job_definition
    assert '"interval": 1' in job_definition
    assert '"unit": "DAYS"' in job_definition
    assert '"pause_status": "UNPAUSED"' in job_definition
    assert (
        '"notebook_path": '
        '"/Workspace/Users/famous.jt33@gmail.com/RepoScout/notebook/'
        'process_repository_embeddings"'
    ) in job_definition


def test_local_environment_template_requires_process_app_env() -> None:
    template_lines = (PROJECT_ROOT / ".env.example").read_text().splitlines()

    assert "APP_ENV=local" not in template_lines
    assert any("Export APP_ENV=local" in line for line in template_lines)
    assert "GITHUB_API_URL=https://api.github.com" in template_lines
    assert "DB_POOL_TIMEOUT_SECONDS=30" in template_lines
