import os
from pathlib import Path, PurePosixPath
from typing import Annotated

from pydantic import Field, field_validator

from changeproof.invariants.models import StrictModel
from .models import CommitSHA
from .settings import RepositoryAccess


class ApprovedTarget(RepositoryAccess):
    approved_heads: Annotated[list[CommitSHA], Field(min_length=1, max_length=100)]
    adapter_path: str
    source_package: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")] = "examples"
    config_path: Path

    @field_validator("source_package")
    @classmethod
    def protect_runtime(cls, value):
        if value in {"changeproof", "sys", "os", "site", "builtins"}:
            raise ValueError("Source package cannot replace the trusted runtime")
        return value

    @field_validator("adapter_path")
    @classmethod
    def relative_path(cls, value):
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not value or "\\" in value:
            raise ValueError("Adapter path must stay inside the repository")
        return value


class WorkerPolicy(StrictModel):
    targets: Annotated[list[ApprovedTarget], Field(min_length=1, max_length=100)]
    runtime_image: str = "changeproof-worker:local"
    database_image: str = "postgres:16-alpine"
    timeout_seconds: Annotated[int, Field(ge=10, le=300)] = 120

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        policy = cls.model_validate_json(path.read_text())
        for target in policy.targets:
            target.config_path = (path.parent / target.config_path).resolve()
        return policy

    def target_for(self, job):
        return next((target for target in self.targets if target.installation_id == job["installation_id"]
                     and target.id == job["repository_id"] and target.full_name == job["repository"]
                     and job["head_sha"] in target.approved_heads), None)


def app_credentials():
    app_id = os.environ["CHANGEPROOF_GITHUB_APP_ID"]
    if not app_id.isdigit() or int(app_id) <= 0:
        raise ValueError("GitHub App ID must be a positive integer")
    key = Path(os.environ["CHANGEPROOF_GITHUB_PRIVATE_KEY_PATH"]).read_text()
    return app_id, key
