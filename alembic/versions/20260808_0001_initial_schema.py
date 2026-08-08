"""Create the RepoScout ingestion tables.

Revision ID: 20260808_0001
Revises: None
Create Date: 2026-08-08
"""

from alembic import op

revision: str = "20260808_0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE repositories (
            repo_id BIGINT PRIMARY KEY,
            name TEXT NOT NULL,
            full_name TEXT NOT NULL,
            owner TEXT NOT NULL,
            description TEXT,
            html_url TEXT NOT NULL,
            primary_language TEXT,
            stars INTEGER NOT NULL CHECK (stars >= 0),
            forks INTEGER NOT NULL CHECK (forks >= 0),
            open_issues INTEGER NOT NULL CHECK (open_issues >= 0),
            topics TEXT[] NOT NULL DEFAULT '{}',
            license TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            pushed_at TIMESTAMPTZ,
            ingested_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_repositories_full_name ON repositories (full_name)")
    op.execute(
        """
        CREATE TABLE repository_readmes (
            repo_id BIGINT PRIMARY KEY
                REFERENCES repositories(repo_id) ON DELETE CASCADE,
            raw_content TEXT,
            content_hash CHAR(64),
            retrieved_at TIMESTAMPTZ NOT NULL,
            retrieval_status TEXT NOT NULL,
            CONSTRAINT ck_repository_readmes_status
                CHECK (retrieval_status IN ('available', 'missing', 'error')),
            CONSTRAINT ck_repository_readmes_content
                CHECK (
                    (retrieval_status = 'available'
                        AND raw_content IS NOT NULL
                        AND content_hash IS NOT NULL)
                    OR retrieval_status IN ('missing', 'error')
                )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE ingestion_runs (
            run_id UUID PRIMARY KEY,
            search_query TEXT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL,
            completed_at TIMESTAMPTZ,
            repositories_found INTEGER NOT NULL DEFAULT 0
                CHECK (repositories_found >= 0),
            repositories_inserted INTEGER NOT NULL DEFAULT 0
                CHECK (repositories_inserted >= 0),
            repositories_updated INTEGER NOT NULL DEFAULT 0
                CHECK (repositories_updated >= 0),
            status TEXT NOT NULL,
            error_message TEXT,
            CONSTRAINT ck_ingestion_runs_status
                CHECK (status IN ('running', 'completed', 'failed')),
            CONSTRAINT ck_ingestion_runs_completion
                CHECK (
                    (status = 'running' AND completed_at IS NULL)
                    OR (status IN ('completed', 'failed') AND completed_at IS NOT NULL)
                )
        )
        """
    )
    op.execute("CREATE INDEX ix_ingestion_runs_started_at ON ingestion_runs (started_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE ingestion_runs")
    op.execute("DROP TABLE repository_readmes")
    op.execute("DROP TABLE repositories")
