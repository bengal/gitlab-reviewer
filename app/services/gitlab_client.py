"""GitLab REST client adapted from the proven fetch-work-item.py pattern:

- ``PRIVATE-TOKEN`` auth header on a persistent ``requests.Session``
- 429 responses sleep for ``Retry-After`` (capped at 30s, max 3 retries)
- 5xx responses are retried with the same budget, then raise
- a 0.2s delay between calls to stay well under rate limits
- ``per_page=100`` pagination following ``x-next-page``

All API errors surface as :class:`GitLabError` (never raw ``requests``
exceptions), so callers can show the message in the UI.
"""

import time
from urllib.parse import quote

import requests

from app.models import SettingsRow
from app.schemas.mr import MrSnapshot
from app.security import decrypt_secret

REQUEST_DELAY = 0.2  # seconds between calls
MAX_RETRIES = 3
MAX_RETRY_AFTER = 30  # cap on honored Retry-After values
DEFAULT_RETRY_AFTER = 60
_ERROR_SNIPPET = 300


class GitLabError(Exception):
    """A GitLab API call failed (HTTP 4xx/5xx after retries, or bad response)."""


class GitLabClient:
    """Thin client for the GitLab project REST API, built from a settings row.

    The token is decrypted from the (Fernet-encrypted) settings row at
    construction time and lives only in the session headers.
    """

    def __init__(self, settings: SettingsRow):
        self.base_url = settings.gitlab_url.strip().rstrip("/")
        self.project = settings.gitlab_project.strip()
        self._session = requests.Session()
        token = decrypt_secret(settings.gitlab_token)
        if token:
            self._session.headers["PRIVATE-TOKEN"] = token

    # -- transport ---------------------------------------------------------

    def _project_path(self) -> str:
        """URL-encoded project locator; accepts a numeric id or group/name."""
        return f"/projects/{quote(self.project, safe='')}"

    def api_get(self, path: str, **params: object) -> requests.Response:
        """GET ``path`` (relative to /api/v4) with rate-limit and retry handling."""
        if not self.base_url:
            raise GitLabError("GitLab URL is not configured")
        url = f"{self.base_url}/api/v4{path}"
        retries = 0
        while True:
            time.sleep(REQUEST_DELAY)
            resp = self._session.get(url, params=params or None)
            if resp.status_code == 429 or resp.status_code >= 500:
                if retries >= MAX_RETRIES:
                    raise GitLabError(
                        f"HTTP {resp.status_code} from {url} after {retries} retries: "
                        f"{resp.text[:_ERROR_SNIPPET]}"
                    )
                retries += 1
                time.sleep(self._retry_after(resp))
                continue
            if not resp.ok:
                raise GitLabError(f"HTTP {resp.status_code} from {url}: {resp.text[:_ERROR_SNIPPET]}")
            return resp

    @staticmethod
    def _retry_after(resp: requests.Response) -> float:
        raw = resp.headers.get("Retry-After")
        try:
            seconds = float(raw) if raw else DEFAULT_RETRY_AFTER
        except ValueError:
            seconds = DEFAULT_RETRY_AFTER
        return max(0.0, min(seconds, float(MAX_RETRY_AFTER)))

    def paginated_get(self, path: str, **params: object) -> list:
        """GET all pages of a list endpoint (per_page=100, follows x-next-page)."""
        params = dict(params)
        params.setdefault("per_page", 100)
        page = 1
        results: list = []
        while True:
            params["page"] = page
            resp = self.api_get(path, **params)
            data = resp.json()
            if not isinstance(data, list) or not data:
                break
            results.extend(data)
            if len(data) < int(params["per_page"]):
                break
            next_page = resp.headers.get("X-Next-Page") or resp.headers.get("x-next-page")
            try:
                page = int(next_page) if next_page else page + 1
            except ValueError:
                page += 1
        return results

    # -- merge requests ----------------------------------------------------

    def list_open_merge_requests(self) -> list[MrSnapshot]:
        """All open MRs of the configured project, as normalized snapshots."""
        data = self.paginated_get(self._project_path() + "/merge_requests", state="opened")
        return [MrSnapshot.from_gitlab(item) for item in data]

    def get_merge_request(self, iid: int) -> MrSnapshot:
        """One MR by iid as a normalized snapshot."""
        resp = self.api_get(f"{self._project_path()}/merge_requests/{iid}")
        return MrSnapshot.from_gitlab(resp.json())

    def post_note(self, iid: int, body: str) -> int:
        """Post an MR note; returns the created note id."""
        if not self.base_url:
            raise GitLabError("GitLab URL is not configured")
        time.sleep(REQUEST_DELAY)
        url = f"{self.base_url}/api/v4{self._project_path()}/merge_requests/{iid}/notes"
        resp = self._session.post(url, json={"body": body})
        if not resp.ok:
            raise GitLabError(f"HTTP {resp.status_code} from {url}: {resp.text[:_ERROR_SNIPPET]}")
        data = resp.json()
        try:
            return int(data["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GitLabError(f"unexpected note response: {str(data)[:_ERROR_SNIPPET]}") from exc

    # -- connection check ----------------------------------------------------

    def test_connection(self) -> tuple[bool, str]:
        """Probe the configured project; returns (ok, human-readable message)."""
        if not (self.base_url and self.project):
            return False, "GitLab URL and project are not configured."
        try:
            resp = self.api_get(self._project_path())
            data = resp.json()
            name = data.get("name_with_namespace") or data.get("name") or self.project
            return True, f"Connected to {name} on {self.base_url}."
        except GitLabError as exc:
            return False, str(exc)
