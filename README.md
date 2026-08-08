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

For a Databricks App, attach a Lakebase Autoscaling resource with key `postgres`, attach a secret
resource with key `github_token`, and use the included `app.yaml`. Databricks supplies the `PG*` and
service-principal variables. Run `uv run alembic upgrade head` explicitly against the target before
starting a version with new migrations.

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

The notebook defaults to five changed repositories for the initial smoke test. A second unchanged
run should select zero repositories. `repository_chunks` uses standard pgvector `VECTOR(384)` and a
default-parameter cosine HNSW index. Retrieval and similarity-query behavior remain out of scope
until Section 3.
