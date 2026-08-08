"""Create embedded repository README chunks.

Revision ID: 20260808_0002
Revises: 20260808_0001
Create Date: 2026-08-08
"""

from alembic import op

revision: str = "20260808_0002"
down_revision: str | None = "20260808_0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        """
        CREATE TABLE repository_chunks (
            chunk_id CHAR(64) PRIMARY KEY,
            repo_id BIGINT NOT NULL
                REFERENCES repository_readmes(repo_id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
            chunk_text TEXT NOT NULL CHECK (btrim(chunk_text) <> ''),
            source_content_hash CHAR(64) NOT NULL,
            embedding VECTOR(384) NOT NULL,
            embedding_model TEXT NOT NULL,
            processing_config_hash CHAR(64) NOT NULL,
            processed_at TIMESTAMPTZ NOT NULL,
            UNIQUE (repo_id, chunk_index)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_repository_chunks_embedding_hnsw
        ON repository_chunks
        USING hnsw (embedding vector_cosine_ops)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE repository_chunks")
