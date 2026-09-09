# RepoScout Agent Guide

Use this as the short operational guide and [README.md](README.md) as the full runbook. Verify the
current tree before editing; repository state is authoritative over historical plans.

## Purpose and architecture

RepoScout discovers, evaluates, and organizes open-source GitHub projects using indexed README evidence. Its two principal flows are:

1. GitHub best-match search -> FastAPI ingestion -> Lakebase -> Databricks Spark notebook ->
   cleaned/chunked README text -> normalized MiniLM embeddings -> pgvector HNSW search.
2. Browser -> main `repo-scout` App -> Databricks Supervisor -> `mcp-repo-scout` App -> five
   `/api/tools/*` capabilities -> search and saved state in Lakebase.

`POST /search/ask` is a separate OpenRouter-backed grounded RAG API. The normal browser Ask flow
uses the Supervisor endpoints under `/assistant`.

## Directory and entry-point map

- `app/main.py`: FastAPI lifespan, routers, and static serving. `pyproject.toml` declares `app.main:app`; root `app.yaml` runs one Uvicorn worker.
- `app/routers/`, `app/schemas/`, `app/services/`, `app/repositories/`: HTTP/error mapping, Pydantic
  contracts, orchestration, and parameterized psycopg SQL.
- `app/database/`: Lakebase OAuth credential generation and `AsyncConnectionPool` lifecycle.
- `alembic/`: handwritten revisions `0001` through `0004`; no ORM models or autogeneration metadata.
- `frontend/`: committed semantic HTML, CSS, SVG, and vanilla ES modules. There is no Node build.
- `notebook/process_repository_embeddings.ipynb`: self-contained Spark/JDBC processing,
  SentenceTransformer inference, validation, and psycopg2 vector persistence.
- `mcp-server/`: independent locked project and thin FastMCP HTTP adapter with entry point
  `reposcout-mcp = reposcout_mcp.server:main`; it owns no database or retrieval implementation.
- `databricks/`: daily embedding Job settings and version-controlled Supervisor instructions.
- `artifacts/databricks/` and `artifacts/demo-walkthrough-screenshots/`: committed deployment and
  product evidence; acceptance reports beneath `artifacts/` are ignored.
- `tests/`: root unit/API/source-contract tests. No CI workflows or project scripts are committed.

## Dependencies and local execution

Python is `>=3.12`. Root and MCP lockfiles are separate; synchronize each with its own pyproject.

```bash
# Main application
uv sync --all-groups
cp .env.example .env                 # populate local placeholders; never commit it
export APP_ENV=local                 # export before settings are loaded
uv run alembic upgrade head
uv run fastapi dev                   # [tool.fastapi] resolves app.main:app

# MCP adapter, after RepoScout is running
cd mcp-server
uv sync --all-groups
export REPOSCOUT_API_APP_URL=http://127.0.0.1:8000
uv run reposcout-mcp
```

There is no frontend build. Root `app.yaml` runs the main App; `mcp-server/app.yaml` runs the MCP App with `uv run --frozen reposcout-mcp`.

## Configuration and secrets

Treat [.env.example](.env.example), [app/config.py](app/config.py), and both `app.yaml` files as source of truth. Never expose real `.env` values.

- `APP_ENV` is mandatory: `local`, `test`, or `databricks`. Only `local` loads `.env`, and only
  after `APP_ENV=local` is already in the process environment. `test` needs no external settings.
- GitHub: `GITHUB_TOKEN`, `GITHUB_API_URL`, `GITHUB_API_VERSION`, `GITHUB_TIMEOUT_SECONDS`,
  `GITHUB_README_CONCURRENCY`, `GITHUB_RETRY_ATTEMPTS`.
- Lakebase: `LAKEBASE_ENDPOINT`, all `PG*` values, and all `DB_POOL_*` settings in `.env.example`.
- Retrieval/RAG: `SEARCH_MIN_SIMILARITY`, `LLM_API_BASE_URL`, optional `LLM_API_KEY`,
  `LLM_MODEL_NAME`, `LLM_REQUEST_TIMEOUT`, `LLM_MAX_OUTPUT_TOKENS`.
- Supervisor: optional `SUPERVISOR_ENDPOINT_NAME` and both `SUPERVISOR_*_TIMEOUT_SECONDS` settings;
  the endpoint name is not a secret.
- Local SDK calls may use `DATABRICKS_CONFIG_PROFILE`; main App resource keys are `postgres`,
  `github_token`, `openrouter_api_key`, and `supervisor_endpoint`.
- MCP: local `REPOSCOUT_API_APP_URL`; deployed `REPOSCOUT_APP_NAME` from resource key `reposcout`;
  plus `REPOSCOUT_API_TIMEOUT_SECONDS`, `MCP_PORT`, and runtime `DATABRICKS_APP_PORT`.

Generated Lakebase passwords must never enter settings, URLs, logs, responses, or exceptions. The pool uses an async connection-parameters callable to refresh credentials; do not move injection post-connect.

## Established code and data contracts

- Preserve the layering: routers handle HTTP, schemas validate public data, services orchestrate,
  repositories own SQL, and `database/` owns connection/OAuth concerns. Add a layer only for a
  concrete need.
- Prefer `Annotated` FastAPI parameters/dependencies, typed response models, HTTPX for HTTP, and
  Asyncer for synchronous SDK/model calls inside async code.
- Keep parameterized psycopg SQL and repository protocols. Do not add ORM metadata merely for
  autogeneration. Map failures to stable public errors without raw bodies, SQL, credentials, or prompts.
- Ingestion uses GitHub relevance/best match, not forced star sorting. Requests default to 30 and
  cap at 100. README `missing` clears stale content/hash; `error` preserves the last successful
  content/hash. A per-README failure does not fail the run.
- `repository_readmes.repo_id` is its PK/FK; states are `available`, `missing`, `error`. Chunks use
  `VECTOR(384)`, fixed MiniLM, content/config hashes, deterministic IDs/order, and cosine HNSW.
- Retrieval applies the similarity threshold before grouping, keeps at most two evidence chunks per
  project, and may return fewer than `top_k`, especially with metadata-filtered HNSW.
- Coverage requests are natural-language review signals only. Statuses are `NEW`, `REVIEWED`,
  `COVERED`, `DECLINED`; duplicates are useful. Never trigger ingestion or Spark from feedback.
- Saved statuses: `INTERESTED`, `TO_TRY`, `IN_PROGRESS`, `COMPLETED`. Save is idempotent and preserves
  status/timestamps; status/note writes require a save. Browser removal cascades notes only, is absent
  from MCP, and never deletes indexed repository data.
- The application user key is the shared `default`, centralized only in
  `app.dependencies.get_project_user_key`; never add it to browser or MCP contracts.
- MCP exposes exactly `search_projects`, `get_project_details`, `save_project`,
  `update_project_status`, and `add_project_note`. Cache only the resolved App URL, never auth headers;
  keep database, embedding, vector SQL, and FastAPI internals in the main App.
- Supervisor writes require explicit intent and only the five tools are approved. Raw items remain
  in bounded backend history; browser output is cards, references, or text. Never auto-retry an
  uncertain write, especially an append-only note.
- OpenRouter HTTP 200 responses may contain provider errors. Treat typed/in-body errors,
  `finish_reason="error"`, partial, empty, or malformed completions as failures.
- Keep frontend assets/APIs relative to `APPLICATION_BASE_URL`; never add localhost, root-absolute
  paths, deployment URLs, CDNs, `innerHTML`, or inline handlers. Preserve `fallback=None`, safe links,
  themes, reduced motion, focus/cancellation, and `textContent` rendering.

## Database and notebook changes

Create schema changes only as a new handwritten revision linked to the current Alembic head. Never
migrate at FastAPI startup or request time. Online migration is disabled for `APP_ENV=test` and
uses one fresh short-lived credential with `NullPool`; offline SQL generation is supported.

```bash
uv run alembic heads
uv run alembic history
uv run alembic upgrade head --sql     # offline; no external credential
export APP_ENV=local
uv run alembic upgrade head           # live Lakebase; only when explicitly intended
```

For notebook edits, preserve Spark, config invalidation, missing/error rules, hash-race checks,
normalized 384-dimensional vectors, and transactional per-repository replacement. Python contract
tests inspect source/JSON but do not execute Spark; notebook assertions are runtime checks.

## Verification commands

```bash
# Root project: no external services
APP_ENV=test uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
node --check frontend/assets/app.js
node --check frontend/assets/theme.js
jq empty notebook/process_repository_embeddings.ipynb
jq empty databricks/jobs/reposcout-embedding-job.json

# Independent MCP project
cd mcp-server
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

Useful focused tests: `tests/test_supervisor.py` for Ask/SSE/cancellation;
`tests/test_frontend.py` plus Node syntax for UI; `tests/test_api.py` and
`tests/test_ingestion_service.py` for ingestion; `tests/test_search_api.py`,
`tests/test_retrieval_service.py`, and `tests/test_search_repository_contract.py` for retrieval;
`tests/test_project_tools.py` for saved state; `tests/test_migrations.py` and
`tests/test_embedding_notebook_contract.py` for schema/notebook changes.

Root tests use fakes, HTTPX mock/ASGI transports, and source assertions; they do not call external
services. MCP tests use fake Workspace/App clients and in-process FastMCP discovery. Frontend tests
are contracts, not visual tests. Live migration, Spark/model execution, deployed proxy/auth,
GitHub/OpenRouter, MCP/Supervisor, and browser acceptance are operator-run checks.

When reporting completion, list commands actually run and their results. State clearly when a check
was skipped, unavailable, or requires external credentials; never imply mocked tests validated a
live deployment.

## Skills

- Read [`.agents/skills/fastapi/SKILL.md`](.agents/skills/fastapi/SKILL.md) fully for FastAPI,
  Pydantic, native SSE, or frontend-serving changes, then use its relevant references. RepoScout's
  explicit psycopg design still takes precedence.
- Read [`.agents/skills/library-skills/SKILL.md`](.agents/skills/library-skills/SKILL.md) for skill
  discovery/repair. `.agents/skills/fastapi` is package-managed; do not edit it manually. Use
  `uvx library-skills --check` as the documented non-mutating check when relevant.

## Verified caveats and documentation discrepancies

- README currently describes My Projects as read-only, but `GET` plus browser-only
  `DELETE /saved-projects/{repo_id}` are implemented. MCP and Supervisor still cannot remove saves.
- README describes notebook connection values as environment-provided/required, but the notebook has
  workspace-specific non-secret widget defaults and its contract test expects nonblank defaults.
- `databricks/jobs/reposcout-embedding-job.json` contains a user-specific workspace notebook path;
  change it before deployment elsewhere. README already calls out that portability requirement.
- The notebook uninstalls psycopg2 packages, later imports `psycopg2`, and does not reinstall it in
  that cell; availability depends on the Databricks runtime unless deliberately corrected/revalidated.
- README's Main App resource table repeats `postgres`; root `app.yaml` is the runtime source of truth.

Do not casually rewrite the notebook to resolve these caveats. Make narrow edits and validate on
Databricks whenever notebook runtime behavior changes.

## Deeper references

- [README.md](README.md): complete setup, architecture, grants, APIs, operations, and limitations.
- [databricks/reposcout-supervisor-instructions.md](databricks/reposcout-supervisor-instructions.md):
  deployed Supervisor behavior; update the configured endpoint after changing this file.
- [databricks/jobs/reposcout-embedding-job.json](databricks/jobs/reposcout-embedding-job.json): daily
  Job settings; adapt its workspace path before reuse.
- [alembic/versions/](alembic/versions/): authoritative table, constraint, FK, and index definitions.
- [notebook/process_repository_embeddings.ipynb](notebook/process_repository_embeddings.ipynb):
  operational incremental embedding pipeline and notebook-native assertions.
