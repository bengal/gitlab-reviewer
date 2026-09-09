"""The model-profile create form: fields accept, validate, and display."""

from sqlalchemy import select

from app.models import ModelProfile


def _login(client, test_password) -> None:
    resp = client.post("/login", data={"password": test_password}, follow_redirects=False)
    assert resp.status_code == 303


def _form_data(**overrides):
    data = {
        "name": "form-model",
        "provider": "local",
        "model_id": "qwen3.8",
        "base_url": "http://llama.test:8080/v1",
        "api_key": "",
        "api_key_env": "",
        "extra_opencode_json": "{}",
        "context_window": "",
    }
    data.update(overrides)
    return data


def _profile(db, name: str) -> ModelProfile:
    db.expire_all()
    return db.scalars(select(ModelProfile).where(ModelProfile.name == name)).one()


def _create(client, db, **overrides) -> ModelProfile:
    data = _form_data(**overrides)
    client.post("/settings/models", data=data)
    return _profile(db, data["name"])


def test_form_stores_context_window(client, db, test_password):
    _login(client, test_password)
    _create(client, db, name="form-ctx", context_window="220000")

    profile = _profile(db, "form-ctx")
    assert profile.context_window == 220000
    page = client.get("/settings")
    assert page.status_code == 200
    assert "220000" in page.text


def test_form_empty_context_window_stores_null(client, db, test_password):
    _login(client, test_password)
    _create(client, db, name="form-noctx")

    profile = _profile(db, "form-noctx")
    assert profile.context_window is None


def test_form_rejects_invalid_context_window(client, db, test_password):
    _login(client, test_password)
    for bad in ("abc", "0"):
        resp = client.post(
            "/settings/models",
            data={
                "name": f"form-bad-{bad}",
                "provider": "local",
                "model_id": "qwen3.8",
                "base_url": "http://llama.test:8080/v1",
                "api_key": "",
                "api_key_env": "",
                "extra_opencode_json": "{}",
                "context_window": bad,
            },
        )
        assert "context_window" in resp.text
    assert db.scalar(select(ModelProfile).where(ModelProfile.name.like("form-bad-%"))) is None
