from app.repositories.ingestion import IngestionRepository


def test_repository_sql_uses_postgres_conflict_upserts() -> None:
    repository_sql = IngestionRepository._REPOSITORY_UPSERT
    readme_sql = IngestionRepository._README_UPSERT

    assert "ON CONFLICT (repo_id) DO UPDATE" in repository_sql
    assert "ON CONFLICT (repo_id) DO UPDATE" in readme_sql
    assert "EXCLUDED.retrieval_status = 'error'" in readme_sql
    assert "THEN repository_readmes.raw_content" in readme_sql
