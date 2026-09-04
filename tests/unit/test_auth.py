"""Auth flows: login form, session guard, cookie handling, logout.

The ``app`` fixture sets APP_PASSWORD_HASH from the ``test_password`` fixture
("test-password"); SESSION_SECRET is fixed per test process.
"""

LOGIN_REDIRECT_TARGET = "/login"


def test_healthz_is_exempt(client):
    resp = client.get("/healthz", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_static_is_exempt(client):
    resp = client.get("/static/css/app.css", follow_redirects=False)
    assert resp.status_code == 200


def test_login_page_is_exempt(client):
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 200
    assert "password" in resp.text.lower()


def test_unauthenticated_root_redirects_to_login(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == LOGIN_REDIRECT_TARGET


def test_unauthenticated_logout_redirects_to_login(client):
    resp = client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == LOGIN_REDIRECT_TARGET


def test_wrong_password_renders_error_without_cookie(client):
    resp = client.post("/login", data={"password": "wrong-password"}, follow_redirects=False)
    assert resp.status_code == 401
    assert "invalid password" in resp.text.lower()
    assert "nm_review_session" not in client.cookies


def test_correct_password_sets_cookie_and_reaches_home(client, test_password):
    resp = client.post("/login", data={"password": test_password}, follow_redirects=True)
    assert resp.status_code == 200
    assert resp.url.path == "/"
    assert "nm_review_session" in client.cookies
    assert "Dashboard" in resp.text


def test_home_shows_not_configured_hint_when_settings_empty(client, test_password):
    client.post("/login", data={"password": test_password}, follow_redirects=False)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "not configured" in resp.text.lower()


def test_tampered_cookie_is_treated_as_unauthenticated(client):
    client.cookies.set("nm_review_session", "totally-forged-payload", path="/")
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == LOGIN_REDIRECT_TARGET


def test_api_docs_require_auth(client, test_password):
    resp = client.get("/openapi.json", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == LOGIN_REDIRECT_TARGET

    client.post("/login", data={"password": test_password}, follow_redirects=False)
    assert client.get("/openapi.json").status_code == 200


def test_logout_clears_cookie(client, test_password):
    client.post("/login", data={"password": test_password}, follow_redirects=False)
    assert "nm_review_session" in client.cookies

    resp = client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == LOGIN_REDIRECT_TARGET

    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == LOGIN_REDIRECT_TARGET
