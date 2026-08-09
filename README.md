# RepoScout

RepoScout is an AI-powered open-source project discovery and learning assistant. The repository
implements GitHub ingestion through FastAPI and Spark-based README chunk embedding into Databricks
Lakebase.

## API

- `GET /health` — liveness and active application environment.
- `POST /ingestions` — synchronously ingest a GitHub search. The request body is
  `{"search_query": "fastapi", "max_repositories": 30}`; the limit defaults to 30 and cannot
  exceed 100.
- `GET /ingestions/{run_id}` — inspect a recorded ingestion run.
- `POST /search/semantic` — retrieve up to ten distinct repository recommendations using README
  chunk similarity, with optional language and minimum-star filters.
- `POST /search/ask` — retrieve repository evidence and ask OpenRouter for a grounded answer that
  includes the evidence used.
- `POST /assistant/messages` — continue a bounded, session-scoped conversation through the
  configured Databricks Supervisor endpoint.
- `GET /saved-projects` — list the capstone user's saved repositories, statuses, and recent notes.
- `POST /indexing-requests` — record a natural-language corpus coverage need for later human or
  platform review; this never triggers ingestion automatically.

GitHub searches use GitHub's relevance/best-match ordering. README retrieval failures are isolated
per repository: `404` is stored as `missing`, exhausted transient failures are stored as `error`,
and the rest of the run continues.

### GitHub ingestion limits

RepoScout deliberately limits each synchronous ingestion request to 100 repositories. GitHub's
[repository search endpoint](https://docs.github.com/en/rest/search/search#search-repositories)
returns at most 100 results per page, but GitHub can expose up to 1,000 results for one search query
through pagination. Therefore, 100 is a RepoScout operational safety boundary rather than the total
number of repositories GitHub permits for a query.

For an authenticated run of 100 repositories, RepoScout normally makes one search request and up
to 100 separate README requests, with additional requests only when retries are required. GitHub
currently permits up to 30 authenticated search requests per minute, while authenticated general
REST requests normally share a 5,000-request-per-hour allowance; see GitHub's
[search limits](https://docs.github.com/en/rest/search/search#rate-limit) and
[REST API rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api).

Keep the per-request maximum at 100 for the current synchronous API. The overall Lakebase corpus
may grow beyond 100 through additional, meaningfully distinct or partitioned searches. Repeating an
identical best-match query will generally refresh the same leading repositories rather than add a
new batch. The embedding notebook's `max_repositories` widget is a separate processing-batch limit;
it does not change the FastAPI ingestion maximum. On constrained Databricks compute, process the
stored corpus in smaller notebook batches even when a single ingestion collected 100 repositories.

## Local setup

Prerequisites:

1. Python 3.12 and `uv`.
2. A Databricks workspace with Lakebase Autoscaling.
3. The Databricks CLI authenticated as the developer with `databricks auth login`.
4. An OAuth-enabled PostgreSQL role for that developer with migration and DML privileges on the
   target database/schema.
5. A GitHub token. Public-only fine-grained tokens need repository metadata and contents read access.

Copy `.env.example` to `.env`, fill in the GitHub and Lakebase values, and set
`DATABRICKS_CONFIG_PROFILE` to the profile created by `databricks auth login`. Keep `APP_ENV=local`
in the process environment; `APP_ENV` itself is intentionally mandatory and is not bootstrapped
from `.env`.

```bash
uv sync --all-groups
export APP_ENV=local
uv run alembic upgrade head
uv run fastapi dev
```

Alembic obtains a short-lived Lakebase credential immediately before its migration connection.
FastAPI never creates or alters application tables during startup or requests.

## Environments

`APP_ENV` accepts exactly:

- `local`: reads process variables and optional `.env`; the Databricks SDK uses the developer's
  configured authentication context.
- `test`: ignores `.env`, requires no GitHub or Databricks configuration, and creates no external
  clients or database pool.
- `databricks`: ignores `.env`; the SDK uses the Databricks App service principal identity and the
  app reads only runtime environment/resource values.

Required outside tests: `GITHUB_TOKEN`, `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGSSLMODE`, and
`LAKEBASE_ENDPOINT`. Generated database credentials are never configuration values.

`LLM_API_KEY` is optional. Without it, semantic search remains available while `/search/ask`
returns `503` when qualifying evidence would require generation. OpenRouter defaults to
`https://openrouter.ai/api/v1` and the `openrouter/free` model router. Generation asks for a concise
answer while allowing up to `LLM_MAX_OUTPUT_TOKENS=2000` completion tokens so routed reasoning
models have enough headroom to finish without returning truncated output.

For a Databricks App, attach a Lakebase Autoscaling resource with key `postgres`, secret resources
with keys `github_token` and `openrouter_api_key`, and the deployed Supervisor serving endpoint
with key `supervisor_endpoint` and **Can query** permission. The included `app.yaml` receives the
Supervisor endpoint name through that resource; it never contains the endpoint name, a workspace
URL, or a personal token. Databricks supplies the `PG*` and service-principal variables. Run
`uv run alembic upgrade head` explicitly against the target before starting a version with new
migrations.

### Databricks App database permissions

`app.yaml` references the `postgres` resource but does not attach it. In the Databricks Apps UI,
attach the Lakebase Autoscaling database that contains the RepoScout tables, assign it the resource
key `postgres`, and grant **Can connect and create**. Attaching the resource creates a PostgreSQL
role whose name is the app service-principal client ID (also exposed to the running app as
`PGUSER`). Do not create that role manually. If the role is missing, confirm the resource targets
the correct Lakebase project, branch, endpoint, and database, then redeploy the app.

After the role exists, connect as the RepoScout table owner and replace
`<APP_SERVICE_PRINCIPAL_CLIENT_ID>` below with that client ID. Keep the double quotes because the
identifier normally contains hyphens.

```sql
GRANT USAGE ON SCHEMA public
TO "<APP_SERVICE_PRINCIPAL_CLIENT_ID>";

GRANT SELECT, INSERT, UPDATE
ON TABLE
    public.repositories,
    public.repository_readmes,
    public.ingestion_runs
TO "<APP_SERVICE_PRINCIPAL_CLIENT_ID>";

GRANT SELECT
ON TABLE public.repository_chunks
TO "<APP_SERVICE_PRINCIPAL_CLIENT_ID>";

GRANT SELECT, INSERT
ON TABLE public.indexing_requests
TO "<APP_SERVICE_PRINCIPAL_CLIENT_ID>";

GRANT SELECT, INSERT, UPDATE
ON TABLE public.saved_projects
TO "<APP_SERVICE_PRINCIPAL_CLIENT_ID>";

GRANT SELECT, INSERT
ON TABLE public.project_notes
TO "<APP_SERVICE_PRINCIPAL_CLIENT_ID>";
```

These are the current least-privilege application grants: ingestion can read and upsert Section 1
records, semantic retrieval can read repositories and embedded chunks, and the machine API can
read and write saved-project state. The FastAPI app does not modify `repository_chunks`; the
independently run Section 2 notebook owns that persistence.
Verify the effective table grants with:

```sql
SELECT grantee, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE grantee = '<APP_SERVICE_PRINCIPAL_CLIENT_ID>'
  AND table_schema = 'public'
ORDER BY table_name, privilege_type;
```

## Lakebase credential rotation

The application constructs psycopg's `AsyncConnectionPool` with `open=False`, then explicitly opens
and closes it in FastAPI lifespan. Its async connection-parameter callable runs before every new
physical connection, invokes the synchronous Databricks SDK through a worker thread, and supplies a
fresh OAuth database credential as the connection password. Physical connections are recycled
before the one-hour credential lifetime. Credentials are not stored in settings, logs, exceptions,
or API responses.

## Development checks

```bash
export APP_ENV=test
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run alembic upgrade head --sql
```

The automated suite uses injected fakes and HTTPX mock transports. It does not access GitHub,
Databricks, or Lakebase. A real Lakebase smoke test is intentionally an operator-run check after
applying migrations to an isolated development branch.

Spark, embeddings, pgvector, vector search, LLMs, agents, scheduled ingestion, and frontend
functionality are out of scope for Section 1.

## Section 2: README embeddings

Apply the current migration before running
[`notebook/process_repository_embeddings.ipynb`](notebook/process_repository_embeddings.ipynb):

```bash
export APP_ENV=local
uv run alembic upgrade head
```

The self-contained Databricks notebook accepts Lakebase endpoint and PostgreSQL connection values
through widgets (with runtime-environment fallbacks). It generates its own short-lived Lakebase
OAuth credentials, reads source rows with Spark JDBC, performs Spark cleaning and deterministic
800-character/100-overlap chunking, embeds chunks with normalized 384-dimensional
`sentence-transformers/all-MiniLM-L6-v2` vectors, and transactionally persists them with psycopg.

The notebook defaults to 20 changed repositories per run. A second unchanged
run should select zero repositories. `repository_chunks` uses standard pgvector `VECTOR(384)` and a
default-parameter cosine HNSW index.

## Section 3: Semantic search and grounded RAG

Both search endpoints embed the user query with the same normalized 384-dimensional
`sentence-transformers/all-MiniLM-L6-v2` model used by the notebook. Lakebase retrieves candidate
chunks with pgvector cosine distance and the existing HNSW-compatible ordering. RepoScout applies
an internal `SEARCH_MIN_SIMILARITY` threshold (default `0.25`), groups evidence by repository, keeps
at most two chunks per project, and returns up to `top_k` distinct projects. Metadata-filtered ANN
queries can legitimately return fewer projects than requested.

Example semantic request:

```json
{
  "query": "Open-source tools for reliable batch data pipelines",
  "top_k": 5,
  "filters": {
    "language": "Python",
    "minimum_stars": 100
  }
}
```

`/search/ask` sends the same ranked evidence to OpenRouter. The user query is the task; retrieved
README text is untrusted evidence and cannot supply behavioral instructions. The response includes
the selected projects and chunks so its repository claims can be checked. This endpoint remains a
stable internal API even though the user-facing Ask experience now uses the Supervisor integration
described below.

## Section 4: MCP tools

The independent [`mcp-server`](mcp-server/) project exposes five thin tools: semantic project
search, bounded project details, idempotent project saving, status updates, and project notes. It
calls RepoScout only through `/api/tools/*`; it has no Lakebase, psycopg, embedding, or vector-search
implementation of its own.

For local development, start RepoScout and configure the MCP application with its direct URL:

```bash
export REPOSCOUT_API_APP_URL=http://127.0.0.1:8000
cd mcp-server
uv sync --all-groups
uv run reposcout-mcp
```

For Databricks, deploy the second application as `mcp-reposcout`. Attach the existing RepoScout
application as a Databricks App Resource with **Can use** permission and resource key
`reposcout-api`. The included MCP `app.yaml` injects the target app name into
`REPOSCOUT_APP_NAME`. The MCP app resolves its URL through the Databricks SDK and generates
fresh service-principal authentication headers for every request; no bearer token is configured,
cached, or logged.

The capstone stores project state under one internal user key, `default`. That key is centralized
behind a backend dependency and is absent from HTTP and MCP contracts, allowing a real identity
resolver to replace it later. Notes and status changes require a prior save. Repeating a save
returns the existing record without changing its status or timestamps.

Run the independent MCP checks with:

```bash
cd mcp-server
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

## Final product integration

The user-facing Ask experience calls the configured Databricks Supervisor serving endpoint through
`POST /assistant/messages`. Local requests authenticate through the configured Databricks CLI
profile; deployed requests use the RepoScout App service principal. Attach the Supervisor endpoint
to the main App with **Can query** permission and resource key `supervisor_endpoint`.

Databricks Responses requests are stateless, so RepoScout keeps the complete response items needed
for follow-up turns in a bounded in-memory store. Only an opaque conversation ID and visible user
and assistant messages reach browser `sessionStorage`; MCP calls, arguments, and reasoning data are
never returned. Conversations retain up to twelve turns, expire after one hour, and intentionally
end when the application restarts.

If a request is cancelled or its final response cannot be confirmed, RepoScout does not retry it
automatically. A state-changing tool might already have completed—especially an append-only note—so
the user is directed to check My Projects before retrying.

`GET /saved-projects` powers the read-only My Projects view. Saving, status changes, and notes remain
agent-driven and continue to use the internal `default` capstone user boundary.

## Frontend

RepoScout includes a framework-free dark interface at the application root. It provides Discover
and conversational Ask views, a read-only My Projects view, expandable README evidence, and live
corpus-readiness metrics from `GET /corpus/summary`. Ask uses the configured Databricks Supervisor
endpoint and keeps bounded conversation history only in application memory; an opaque identifier
and visible messages are retained for the current browser tab. Users can also submit
natural-language indexing requests when the current corpus does not cover what they need. These
requests are review-only and do not invoke the ingestion pipeline. Run it locally with:

```bash
export APP_ENV=local
uv run fastapi dev
```

Then open the URL printed by FastAPI. The committed HTML, CSS, and JavaScript are served by FastAPI
without a separate frontend build. Assets and API requests use paths relative to the application
base, so the same files work locally and behind a Databricks Apps proxy. The normal interface does
not display embedding, vector-index, model-provider, OAuth, or database configuration details.
