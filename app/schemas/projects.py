from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.tools import ProjectNoteResponse, ProjectStatus


class SavedProjectListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    repo_id: int
    name: str
    full_name: str
    owner: str
    description: str | None
    html_url: str
    primary_language: str | None
    stars: int
    forks: int
    open_issues: int
    topics: list[str]
    license: str | None
    status: ProjectStatus
    saved_at: datetime
    updated_at: datetime
    notes: list[ProjectNoteResponse]


class SavedProjectsResponse(BaseModel):
    projects: list[SavedProjectListItem]
