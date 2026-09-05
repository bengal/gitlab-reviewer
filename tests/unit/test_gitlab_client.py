"""GitLab client unit tests with a fully mocked transport.

``requests.Session.request`` and ``time.sleep`` are monkeypatched: no network
and no real waiting. Covers the fetch-work-item.py pattern: PRIVATE-TOKEN
header, 0.2s inter-call delay, 429/Retry-After backoff (capped, max 3
retries), per_page=100 pagination, GitLabError on 4xx/5xx.
"""

import time
from datetime import UTC, datetime

import pytest
import requests

from app.config import get_settings
from app.models import SettingsRow
from app.security import encrypt_secret
from app.services.gitlab_client import GitLabClient, GitLabError

TEST_FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
TOKEN = "glpat-secret-token"


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
        self.text = text
        self.url = ""

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._json


@pytest.fixture()
def client_factory(monkeypatch):
    monkeypatch.setenv("SECRET_ENC_KEY", TEST_FERNET_KEY)
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    get_settings.cache_clear()

    def make(url="https://gitlab.example.com", project="group/proj", token=TOKEN):
        row = SettingsRow(
            id=1,
            gitlab_url=url,
            gitlab_project=project,
            gitlab_token=encrypt_secret(token),
        )
        return GitLabClient(row)

    yield make
    get_settings.cache_clear()


@pytest.fixture()
def transport(monkeypatch):
    """Queue of canned responses served from a patched Session.request."""
    calls: list[dict] = []
    sleeps: list[float] = []
    responses: list = []

    def fake_request(self, method, url, **kwargs):
        calls.append({"method": method, "url": url, "kwargs": kwargs})
        resp = responses.pop(0)
        if callable(resp):
            resp = resp(method, url, kwargs)
        resp.url = url
        return resp

    class FakeTransport:
        def queue(self, *resps):
            responses.extend(resps)

    fake = FakeTransport()
    fake.calls = calls
    fake.sleeps = sleeps
    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    return fake


def mr_item(iid, **over):
    item = {
        "iid": iid,
        "title": f"MR {iid}",
        "author": {"username": f"user{iid}", "name": f"User {iid}"},
        "source_branch": f"feature-{iid}",
        "target_branch": "main",
        "sha": f"cafe{iid:038d}",
        "web_url": f"https://gitlab.example.com/group/proj/-/merge_requests/{iid}",
        "state": "opened",
        "updated_at": "2026-01-02T03:04:05.000Z",
    }
    item.update(over)
    return item


def test_api_get_sends_private_token_and_delay(client_factory, transport):
    transport.queue(FakeResponse(200, json_data={"name": "proj"}))
    client = client_factory()
    client.api_get(client._project_path())

    assert client._session.headers["PRIVATE-TOKEN"] == TOKEN
    call = transport.calls[0]
    assert call["url"] == "https://gitlab.example.com/api/v4/projects/group%2Fproj"
    assert transport.sleeps[0] == pytest.approx(0.2)


def test_numeric_project_id_is_not_encoded(client_factory, transport):
    transport.queue(FakeResponse(200, json_data=[]))
    client = client_factory(project="123")
    client.list_open_merge_requests()
    assert transport.calls[0]["url"].endswith("/projects/123/merge_requests")


def test_api_get_requires_configured_url(client_factory, transport):
    client = client_factory(url="")
    with pytest.raises(GitLabError, match="not configured"):
        client.api_get("/projects/1")


def test_list_open_merge_requests_maps_snapshots(client_factory, transport):
    transport.queue(FakeResponse(200, json_data=[mr_item(1), mr_item(2)]))
    client = client_factory()

    snapshots = client.list_open_merge_requests()

    assert len(snapshots) == 2
    snap = snapshots[0]
    assert snap.iid == 1
    assert snap.title == "MR 1"
    assert snap.author == "user1"
    assert snap.source_branch == "feature-1"
    assert snap.target_branch == "main"
    assert snap.sha == f"cafe{1:038d}"
    assert snap.web_url.endswith("/merge_requests/1")
    assert snap.state == "opened"
    assert snap.updated_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    params = transport.calls[0]["kwargs"]["params"]
    assert params == {"state": "opened", "per_page": 100, "page": 1}


def test_list_paginates_across_two_pages(client_factory, transport):
    page1 = [mr_item(i) for i in range(1, 101)]
    page2 = [mr_item(i) for i in range(101, 104)]
    transport.queue(
        FakeResponse(200, json_data=page1, headers={"X-Next-Page": "2"}),
        FakeResponse(200, json_data=page2),
    )
    client = client_factory()

    snapshots = client.list_open_merge_requests()

    assert [s.iid for s in snapshots] == list(range(1, 104))
    pages = [c["kwargs"]["params"]["page"] for c in transport.calls]
    assert pages == [1, 2]
    assert all(c["kwargs"]["params"]["per_page"] == 100 for c in transport.calls)


def test_429_retries_honoring_retry_after(client_factory, transport):
    transport.queue(
        FakeResponse(429, headers={"Retry-After": "7"}, text="rate limited"),
        FakeResponse(200, json_data=[mr_item(1)]),
    )
    client = client_factory()

    snapshots = client.list_open_merge_requests()

    assert len(snapshots) == 1
    assert len(transport.calls) == 2
    assert transport.sleeps == pytest.approx([0.2, 7.0, 0.2])


def test_429_retry_after_is_capped_at_30s(client_factory, transport):
    transport.queue(
        FakeResponse(429, headers={"Retry-After": "90"}, text="rate limited"),
        FakeResponse(200, json_data=[]),
    )
    client = client_factory()
    client.list_open_merge_requests()
    assert 30.0 in transport.sleeps


def test_429_with_missing_retry_after_defaults_capped(client_factory, transport):
    transport.queue(
        FakeResponse(429, text="rate limited"),
        FakeResponse(200, json_data=[]),
    )
    client = client_factory()
    client.list_open_merge_requests()
    assert transport.sleeps[1] == 30.0


def test_429_exhausts_retries_and_raises(client_factory, transport):
    transport.queue(*[FakeResponse(429, headers={"Retry-After": "1"}, text="limited") for _ in range(4)])
    client = client_factory()

    with pytest.raises(GitLabError, match="429"):
        client.list_open_merge_requests()

    assert len(transport.calls) == 4  # initial + 3 retries


def test_404_raises_gitlab_error_without_retry(client_factory, transport):
    transport.queue(FakeResponse(404, json_data={"message": "404 Not Found"}, text="404 Not Found"))
    client = client_factory()

    with pytest.raises(GitLabError, match="404"):
        client.get_merge_request(99)

    assert len(transport.calls) == 1


def test_5xx_retries_then_succeeds(client_factory, transport):
    transport.queue(
        FakeResponse(500, text="boom"),
        FakeResponse(503, text="boom"),
        FakeResponse(200, json_data=[mr_item(1)]),
    )
    client = client_factory()
    assert len(client.list_open_merge_requests()) == 1
    assert len(transport.calls) == 3


def test_5xx_exhausts_retries_and_raises(client_factory, transport):
    transport.queue(*[FakeResponse(500, text="boom") for _ in range(4)])
    client = client_factory()
    with pytest.raises(GitLabError, match="500"):
        client.get_merge_request(1)
    assert len(transport.calls) == 4


def test_get_merge_request_fetches_by_iid(client_factory, transport):
    transport.queue(FakeResponse(200, json_data=mr_item(7)))
    client = client_factory()

    snapshot = client.get_merge_request(7)

    assert snapshot.iid == 7
    assert transport.calls[0]["url"].endswith("/projects/group%2Fproj/merge_requests/7")


def test_post_note_returns_note_id(client_factory, transport):
    transport.queue(FakeResponse(201, json_data={"id": 4242, "body": "looks good"}))
    client = client_factory()

    note_id = client.post_note(7, "looks good")

    assert note_id == 4242
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/projects/group%2Fproj/merge_requests/7/notes")
    assert call["kwargs"]["json"] == {"body": "looks good"}
    assert client._session.headers["PRIVATE-TOKEN"] == TOKEN


def test_post_note_http_error_raises(client_factory, transport):
    transport.queue(FakeResponse(403, json_data={"message": "Forbidden"}, text="Forbidden"))
    client = client_factory()
    with pytest.raises(GitLabError, match="403"):
        client.post_note(7, "hi")


def test_test_connection_ok(client_factory, transport):
    transport.queue(FakeResponse(200, json_data={"name": "proj", "name_with_namespace": "group/proj"}))
    ok, message = client_factory().test_connection()
    assert ok is True
    assert "group/proj" in message


def test_test_connection_fail(client_factory, transport):
    transport.queue(FakeResponse(401, json_data={"message": "401 Unauthorized"}, text="401 Unauthorized"))
    ok, message = client_factory().test_connection()
    assert ok is False
    assert "401" in message


def test_test_connection_unconfigured(client_factory):
    ok, message = client_factory(url="", project="").test_connection()
    assert ok is False
    assert "not configured" in message
