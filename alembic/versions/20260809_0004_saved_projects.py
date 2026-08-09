"""Create saved projects and project notes.

Revision ID: 20260809_0004
Revises: 20260809_0003
Create Date: 2026-08-09
"""

from alembic import op

revision: str = "20260809_0004"
down_revision: str | None = "20260809_0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE saved_projects (
            saved_project_id UUID PRIMARY KEY,
            user_key TEXT NOT NULL,
            repo_id BIGINT NOT NULL
                REFERENCES repositories(repo_id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'INTERESTED',
            saved_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_saved_projects_user_repo UNIQUE (user_key, repo_id),
            CONSTRAINT ck_saved_projects_user_key
                CHECK (btrim(user_key) <> '' AND char_length(user_key) <= 200),
            CONSTRAINT ck_saved_projects_status
                CHECK (status IN ('INTERESTED', 'TO_TRY', 'IN_PROGRESS', 'COMPLETED'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE project_notes (
            note_id UUID PRIMARY KEY,
            saved_project_id UUID NOT NULL
                REFERENCES saved_projects(saved_project_id) ON DELETE CASCADE,
            note_text TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT ck_project_notes_text
                CHECK (
                    btrim(note_text) <> ''
                    AND char_length(note_text) <= 2000
                )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_project_notes_saved_project_created
        ON project_notes (saved_project_id, created_at DESC, note_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE project_notes")
    op.execute("DROP TABLE saved_projects")
