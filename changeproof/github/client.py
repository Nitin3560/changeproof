import time
from urllib.parse import urlparse

import httpx
import jwt


class GitHubAPIError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, app_id, private_key, *, transport=None):
        self.app_id, self.private_key = app_id, private_key
        self.http = httpx.Client(timeout=15, transport=transport, follow_redirects=False)

    def close(self):
        self.http.close()

    def request(self, method, path, token, **kwargs):
        response = self.http.request(method, "https://api.github.com" + path,
            headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2026-03-10"}, **kwargs)
        if not response.is_success:
            raise GitHubAPIError(f"GitHub API returned HTTP {response.status_code}")
        return response.json()

    def installation_token(self, job):
        now = int(time.time())
        signed = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id},
                            self.private_key, algorithm="RS256")
        result = self.request("POST", f"/app/installations/{job['installation_id']}/access_tokens", signed,
            json={"repository_ids": [job["repository_id"]],
                  "permissions": {"contents": "read", "pull_requests": "read", "checks": "write"}})
        return result["token"]

    def current_pr(self, job, token):
        repo = self.request("GET", f"/repos/{job['repository']}", token)
        if repo["id"] != job["repository_id"] or repo["full_name"].lower() != job["repository"]:
            raise GitHubAPIError("Repository identity changed")
        return self.request("GET", f"/repos/{job['repository']}/pulls/{job['pull_number']}", token)

    def ensure_check(self, job, job_id, token):
        for page in range(1, 11):
            result = self.request("GET", f"/repos/{job['repository']}/commits/{job['head_sha']}/check-runs",
                                  token, params={"check_name": "ChangeProof", "per_page": 100, "page": page})
            checks = result["check_runs"]
            for check in checks:
                if check.get("external_id") == job_id and str(check.get("app", {}).get("id")) == self.app_id:
                    return check["id"]
            if len(checks) < 100:
                return self.request("POST", f"/repos/{job['repository']}/check-runs", token,
                    json={"name": "ChangeProof", "head_sha": job["head_sha"], "external_id": job_id,
                          "status": "in_progress"})["id"]
        raise GitHubAPIError("Check lookup exceeded pagination bound")

    def finish_check(self, job, check_id, token, conclusion, summary, text=""):
        return self.request("PATCH", f"/repos/{job['repository']}/check-runs/{check_id}", token,
            json={"status": "completed", "conclusion": conclusion,
                  "output": {"title": "ChangeProof: " + conclusion, "summary": summary[:60000], "text": text[:60000]}})

    def archive(self, job, token):
        path = f"https://api.github.com/repos/{job['repository']}/tarball/{job['head_sha']}"
        with self.http.stream("GET", path, headers={"Authorization": "Bearer " + token,
                "X-GitHub-Api-Version": "2026-03-10"}) as response:
            if response.status_code == 302:
                location = response.headers.get("location", "")
            elif response.status_code == 200:
                return self._read_archive(response)
            else:
                raise GitHubAPIError(f"Archive API returned HTTP {response.status_code}")
        parsed = urlparse(location)
        if parsed.scheme != "https" or parsed.hostname != "codeload.github.com" or parsed.port not in (None, 443) or parsed.username:
            raise GitHubAPIError("Unexpected archive redirect")
        # Never forward installation credentials to the redirect host.
        with self.http.stream("GET", location) as response:
            if response.status_code != 200:
                raise GitHubAPIError("Archive download failed")
            return self._read_archive(response)

    @staticmethod
    def _read_archive(response):
        data = bytearray()
        deadline = time.monotonic() + 30
        for chunk in response.iter_bytes():
            if time.monotonic() > deadline:
                raise GitHubAPIError("Archive transfer deadline exceeded")
            if len(data) + len(chunk) > 20_000_000:
                raise GitHubAPIError("Repository archive exceeds 20 MB")
            data.extend(chunk)
        return bytes(data)
