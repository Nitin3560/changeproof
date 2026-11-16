import hashlib
import hmac
import re

from .models import PullRequestEvent

ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


def valid_signature(body, signature, secret):
    if not signature or not re.fullmatch(r"sha256=[0-9a-f]{64}", signature):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def prepare_job(payload, settings):
    event = PullRequestEvent.model_validate(payload)
    repository, pull = event.repository, event.pull_request
    if event.number != pull.number or pull.base.repo is None:
        raise ValueError("Inconsistent pull request identity")
    if pull.base.repo.id != repository.id or pull.base.repo.full_name != repository.full_name:
        raise ValueError("Base repository does not match event repository")
    permitted = any(entry.installation_id == event.installation.id and entry.id == repository.id
                    and entry.full_name == repository.full_name for entry in settings.repositories)
    if not permitted:
        return None, "repository_not_allowed"
    if event.action not in ACTIONS:
        return None, "unsupported_action"
    if pull.state != "open" or pull.draft:
        return None, "pull_request_not_ready"
    if pull.head.repo is None or pull.head.repo.id != repository.id or pull.head.repo.full_name != repository.full_name:
        return None, "fork_or_missing_head_repository"
    return {"installation_id": event.installation.id, "repository_id": repository.id,
            "repository": repository.full_name, "pull_number": pull.number,
            "base_sha": pull.base.sha, "head_sha": pull.head.sha}, None
