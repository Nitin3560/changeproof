import json
import os
from pathlib import Path
from typing import Annotated

from pydantic import ConfigDict, Field, SecretStr, model_validator

from changeproof.invariants.models import StrictModel
from .models import PositiveID, Repository


class RepositoryAccess(Repository):
    model_config = ConfigDict(strict=True, extra="forbid")
    installation_id: PositiveID


class GitHubSettings(StrictModel):
    webhook_secret: Annotated[SecretStr, Field(min_length=32)]
    repositories: Annotated[list[RepositoryAccess], Field(min_length=1, max_length=1000)]
    queue_path: Path
    max_body_bytes: Annotated[int, Field(ge=1, le=10_485_760)] = 1_048_576

    @model_validator(mode="after")
    def unique_repositories(self):
        identities = [(entry.installation_id, entry.id) for entry in self.repositories]
        names = [(entry.installation_id, entry.full_name) for entry in self.repositories]
        if len(set(identities)) != len(identities) or len(set(names)) != len(names):
            raise ValueError("Allowlist entries must be unique per installation")
        return self

    @classmethod
    def from_environment(cls):
        return cls(webhook_secret=SecretStr(os.environ["CHANGEPROOF_GITHUB_WEBHOOK_SECRET"]),
                   repositories=[RepositoryAccess.model_validate(entry) for entry in
                                 json.loads(os.environ["CHANGEPROOF_GITHUB_ALLOWLIST"])],
                   queue_path=Path(os.environ.get("CHANGEPROOF_GITHUB_QUEUE_PATH", "work/github-jobs.sqlite3")))
