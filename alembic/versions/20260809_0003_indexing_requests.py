"""Create natural-language corpus indexing requests.

Revision ID: 20260809_0003
Revises: 20260808_0002
Create Date: 2026-08-09
"""

from alembic import op

revision: str = "20260809_0003"
down_revision: str | None = "20260808_0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE indexing_requests (
            request_id UUID PRIMARY KEY,
            search_query TEXT NOT NULL,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'NEW',
            created_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT ck_indexing_requests_query
                CHECK (
                    btrim(search_query) <> ''
                    AND char_length(search_query) <= 500
                ),
            CONSTRAINT ck_indexing_requests_notes
                CHECK (
                    notes IS NULL
                    OR (
                        btrim(notes) <> ''
                        AND char_length(notes) <= 2000
                    )
                ),
            CONSTRAINT ck_indexing_requests_status
                CHECK (status IN ('NEW', 'REVIEWED', 'COVERED', 'DECLINED'))
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE indexing_requests")
