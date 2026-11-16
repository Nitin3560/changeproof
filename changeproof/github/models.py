from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PositiveID = Annotated[int, Field(gt=0)]
CommitSHA = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
RepositoryName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")]


class WebhookModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")


class Repository(WebhookModel):
    id: PositiveID
    full_name: RepositoryName

    @field_validator("full_name")
    @classmethod
    def canonical_name(cls, value):
        if value.split("/")[1] in {".", ".."}:
            raise ValueError("Invalid repository name")
        return value.lower()


class Installation(WebhookModel):
    id: PositiveID


class Revision(WebhookModel):
    sha: CommitSHA
    repo: Repository | None


class PullRequest(WebhookModel):
    number: PositiveID
    state: Literal["open", "closed"]
    draft: bool
    base: Revision
    head: Revision


class PullRequestEvent(WebhookModel):
    action: Annotated[str, Field(min_length=1, max_length=100)]
    number: PositiveID
    installation: Installation
    repository: Repository
    pull_request: PullRequest
