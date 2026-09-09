"""The model-profile create and edit forms: fields accept, validate,
display, and the API key stays masked (keep-or-replace on edit)."""

from sqlalchemy import func, select

from app.models import ModelProfile
from app.security import decrypt_secret


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
    # The update route commits in its own session; drop stale identity-map
    # objects so the next read reflects the committed state.
    db.expire_all()
    return db.scalars(select(ModelProfile).where(ModelProfile.name == name)).one()


def _create(client, db, **overrides) -> ModelProfile:
    data = _form_data(**overrides)
    client.post("/settings/models", data=data)
    return _profile(db, data["name"])


def _update(client, profile_id, **overrides):
    return client.post(f"/settings/models/{profile_id}", data=_form_data(**overrides))


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


def test_edit_updates_fields(client, db, test_password):
    _login(client, test_password)
    profile = _create(client, db, name="edit-me", context_window="100000")

    resp = _update(
        client,
        profile.id,
        name="edited",
        model_id="qwen4",
        base_url="http://llama.test:9090/v1",
        extra_opencode_json='{"temperature": 0.2}',
        context_window="220000",
    )
    assert resp.status_code == 200
    assert "edited" in resp.text

    edited = _profile(db, "edited")
    assert edited.model_id == "qwen4"
    assert edited.base_url == "http://llama.test:9090/v1"
    assert edited.context_window == 220000
    assert edited.extra_opencode_json == {"temperature": 0.2}
    assert db.scalar(select(ModelProfile).where(ModelProfile.name == "edit-me")) is None


def test_edit_keeps_api_key_when_left_empty(client, db, test_password):
    _login(client, test_password)
    profile = _create(client, db, name="key-keep", api_key="secret-one")

    _update(client, profile.id, name="key-keep", model_id="qwen3.8")

    assert decrypt_secret(_profile(db, "key-keep").api_key) == "secret-one"


def test_edit_overwrites_api_key_when_set(client, db, test_password):
    _login(client, test_password)
    profile = _create(client, db, name="key-swap", api_key="secret-one")

    _update(client, profile.id, name="key-swap", model_id="qwen3.8", api_key="secret-two")

    assert decrypt_secret(_profile(db, "key-swap").api_key) == "secret-two"


def test_edit_clears_context_window(client, db, test_password):
    _login(client, test_password)
    profile = _create(client, db, name="ctx-clear", context_window="220000")

    _update(client, profile.id, name="ctx-clear")

    assert _profile(db, "ctx-clear").context_window is None


def test_edit_switches_default(client, db, test_password):
    _login(client, test_password)
    first = _create(client, db, name="default-a", is_default="true")
    second = _create(client, db, name="default-b")
    assert _profile(db, "default-a").is_default
    assert not _profile(db, "default-b").is_default

    _update(client, second.id, name="default-b", is_default="true")
    assert _profile(db, "default-a").is_default is False
    assert _profile(db, "default-b").is_default is True

    _update(client, first.id, name="default-a")  # unchecked -> no longer default
    assert _profile(db, "default-a").is_default is False


def test_edit_name_collision_rejected(client, db, test_password):
    _login(client, test_password)
    _create(client, db, name="taken")
    profile = _create(client, db, name="rename-me")

    resp = _update(client, profile.id, name="taken")

    assert "already exists" in resp.text
    assert _profile(db, "rename-me").model_id == "qwen3.8"
    assert db.scalar(select(func.count(ModelProfile.id))) == 2


def test_edit_validation_error_preserves_input(client, db, test_password):
    _login(client, test_password)
    profile = _create(client, db, name="edit-err", model_id="qwen9")

    resp = _update(client, profile.id, name="edit-err", model_id="qwen9", context_window="abc")

    assert "context_window" in resp.text
    assert 'value="qwen9"' in resp.text  # submitted input re-rendered
    assert _profile(db, "edit-err").context_window is None
    assert _profile(db, "edit-err").model_id == "qwen9"


def test_edit_form_renders_prefilled_and_masks_key(client, db, test_password):
    _login(client, test_password)
    profile = _create(
        client, db, name="edit-show", model_id="qwen3.8", api_key="topsecret", context_window="220000"
    )

    resp = client.get(f"/settings/models/{profile.id}/edit")

    assert resp.status_code == 200
    assert 'value="edit-show"' in resp.text
    assert 'value="qwen3.8"' in resp.text
    assert 'value="220000"' in resp.text
    assert "leave empty to keep it" in resp.text
    assert "topsecret" not in resp.text


def test_edit_unknown_profile(client, db, test_password):
    _login(client, test_password)
    resp = _update(client, 999, name="ghost")
    assert "No profile with id 999" in resp.text
    resp = client.get("/settings/models/999/edit")
    assert "No profile with id 999" in resp.text


def test_models_section_route(client, db, test_password):
    _login(client, test_password)
    _create(client, db, name="section-check")

    resp = client.get("/settings/models/section")

    assert resp.status_code == 200
    assert "section-check" in resp.text
    assert "Edit" in resp.text
