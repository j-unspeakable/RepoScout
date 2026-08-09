import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1] / "alembic" / "versions" / "20260808_0001_initial_schema.py"
)
CHUNKS_MIGRATION_PATH = (
    Path(__file__).parents[1] / "alembic" / "versions" / "20260808_0002_repository_chunks.py"
)
INDEXING_REQUESTS_MIGRATION_PATH = (
    Path(__file__).parents[1] / "alembic" / "versions" / "20260809_0003_indexing_requests.py"
)
SAVED_PROJECTS_MIGRATION_PATH = (
    Path(__file__).parents[1] / "alembic" / "versions" / "20260809_0004_saved_projects.py"
)


def _load_migration(
    path: Path = MIGRATION_PATH, module_name: str = "initial_migration"
) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_initial_migration_is_handwritten_and_has_required_constraints(
    monkeypatch,
) -> None:
    migration = _load_migration()
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()
    sql = "\n".join(statements)

    assert "repo_id BIGINT PRIMARY KEY" in sql
    assert "CREATE TABLE repository_readmes" in sql
    assert "retrieval_status IN ('available', 'missing', 'error')" in sql
    assert "CREATE TABLE ingestion_runs" in sql
    assert "pgvector" not in sql.lower()


def test_migration_downgrade_drops_children_before_repositories(monkeypatch) -> None:
    migration = _load_migration()
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.downgrade()

    assert statements == [
        "DROP TABLE ingestion_runs",
        "DROP TABLE repository_readmes",
        "DROP TABLE repositories",
    ]


def test_repository_chunks_migration_uses_pgvector_hnsw(monkeypatch) -> None:
    migration = _load_migration(CHUNKS_MIGRATION_PATH, "repository_chunks_migration")
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()
    sql = "\n".join(statements)

    assert migration.revision == "20260808_0002"
    assert migration.down_revision == "20260808_0001"
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    assert "CREATE TABLE repository_chunks" in sql
    assert "REFERENCES repository_readmes(repo_id) ON DELETE CASCADE" in sql
    assert "embedding VECTOR(384) NOT NULL" in sql
    assert "UNIQUE (repo_id, chunk_index)" in sql
    assert "CREATE INDEX ix_repository_chunks_embedding_hnsw" in sql
    assert "USING hnsw (embedding vector_cosine_ops)" in sql
    assert "lakebase_ann" not in sql


def test_repository_chunks_downgrade_keeps_shared_extension(monkeypatch) -> None:
    migration = _load_migration(CHUNKS_MIGRATION_PATH, "repository_chunks_downgrade")
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.downgrade()

    assert statements == ["DROP TABLE repository_chunks"]


def test_indexing_requests_migration_is_minimal_and_uses_coverage_status(monkeypatch) -> None:
    migration = _load_migration(INDEXING_REQUESTS_MIGRATION_PATH, "indexing_requests_migration")
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()
    sql = "\n".join(statements)

    assert migration.revision == "20260809_0003"
    assert migration.down_revision == "20260808_0002"
    assert "CREATE TABLE indexing_requests" in sql
    assert "request_id UUID PRIMARY KEY" in sql
    assert "search_query TEXT NOT NULL" in sql
    assert "notes TEXT" in sql
    assert "status TEXT NOT NULL DEFAULT 'NEW'" in sql
    assert "created_at TIMESTAMPTZ NOT NULL" in sql
    assert "char_length(search_query) <= 500" in sql
    assert "char_length(notes) <= 2000" in sql
    assert "status IN ('NEW', 'REVIEWED', 'COVERED', 'DECLINED')" in sql
    assert "INDEXED" not in sql
    assert "repository_url" not in sql
    assert "CREATE INDEX" not in sql


def test_indexing_requests_downgrade_drops_only_feedback_table(monkeypatch) -> None:
    migration = _load_migration(INDEXING_REQUESTS_MIGRATION_PATH, "indexing_requests_downgrade")
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.downgrade()

    assert statements == ["DROP TABLE indexing_requests"]


def test_saved_projects_migration_has_user_state_constraints(monkeypatch) -> None:
    migration = _load_migration(SAVED_PROJECTS_MIGRATION_PATH, "saved_projects_migration")
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()
    sql = "\n".join(statements)

    assert migration.revision == "20260809_0004"
    assert migration.down_revision == "20260809_0003"
    assert "CREATE TABLE saved_projects" in sql
    assert "REFERENCES repositories(repo_id) ON DELETE CASCADE" in sql
    assert "UNIQUE (user_key, repo_id)" in sql
    assert "status TEXT NOT NULL DEFAULT 'INTERESTED'" in sql
    assert "'INTERESTED', 'TO_TRY', 'IN_PROGRESS', 'COMPLETED'" in sql
    assert "CREATE TABLE project_notes" in sql
    assert "REFERENCES saved_projects(saved_project_id) ON DELETE CASCADE" in sql
    assert "char_length(note_text) <= 2000" in sql


def test_saved_projects_downgrade_drops_notes_first(monkeypatch) -> None:
    migration = _load_migration(SAVED_PROJECTS_MIGRATION_PATH, "saved_projects_downgrade")
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.downgrade()

    assert statements == ["DROP TABLE project_notes", "DROP TABLE saved_projects"]
