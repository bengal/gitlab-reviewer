"""Settings routes: GitLab connection, review defaults, scheduling knobs,
model profiles.

The GitLab token and model API keys are masked on read (the forms only show
that a value is set) and are only overwritten when the user submits a
non-empty value.
"""

import json
from pathlib import Path

import requests
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_settings_row
from app.deps import get_db
from app.models import ModelProfile, ScheduledJob, SettingsRow
from app.schemas.model_profile import ModelProfileForm
from app.schemas.settings import SettingsForm
from app.security import decrypt_secret, encrypt_secret
from app.services.gitlab_client import GitLabClient, GitLabError

router = APIRouter()

DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[2] / "deploy" / "prompts" / "default_review.md"


def _default_prompt() -> str:
    try:
        return DEFAULT_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def _templates(request: Request):
    return request.app.state.templates


def _list_profiles(db: Session) -> list[ModelProfile]:
    return list(
        db.scalars(
            select(ModelProfile).order_by(ModelProfile.is_default.desc(), ModelProfile.name)
        )
    )


def _profile_display(profile: ModelProfile) -> dict[str, object]:
    """One profile row for the UI. The API key is masked: only its presence
    (or the env-var name) is ever shown, never the decrypted value."""
    if profile.api_key_env:
        key_display = f"from env {profile.api_key_env}"
    elif profile.api_key:
        key_display = "••• (set)"
    else:
        key_display = "—"
    return {
        "id": profile.id,
        "name": profile.name,
        "provider": profile.provider,
        "model_id": profile.model_id,
        "base_url": profile.base_url,
        "key_display": key_display,
        "is_default": profile.is_default,
    }


def _render(
    request: Request,
    db: Session,
    row: SettingsRow,
    raw: dict[str, object],
    errors: list[str] | None,
    message: str | None,
    profile_raw: dict[str, object] | None = None,
    profile_errors: list[str] | None = None,
) -> HTMLResponse:
    """Render the settings form; ``raw`` (raw form strings) wins over the row."""
    has_raw = bool(raw)
    prompt = (
        raw["default_review_prompt"]
        if has_raw
        else (row.default_review_prompt or _default_prompt())
    )
    libraries = (
        raw["known_libraries"]
        if has_raw
        else (json.dumps(row.known_libraries, indent=2) if row.known_libraries else "")
    )
    context = {
        "gitlab_url": raw.get("gitlab_url", row.gitlab_url),
        "gitlab_project": raw.get("gitlab_project", row.gitlab_project),
        "token_set": bool(row.gitlab_token),
        "default_review_prompt": prompt,
        "known_libraries": libraries,
        "nightly_time": raw.get("nightly_time", row.nightly_time),
        "max_concurrent_reviews": raw.get("max_concurrent_reviews", row.max_concurrent_reviews),
        "poll_interval_seconds": raw.get("poll_interval_seconds", row.poll_interval_seconds),
        "post_results_to_gitlab": raw.get("post_results_to_gitlab", row.post_results_to_gitlab),
        "errors": errors,
        "message": message,
        "profiles": [_profile_display(p) for p in _list_profiles(db)],
        "profile_raw": profile_raw,
        "profile_errors": profile_errors,
    }
    return _templates(request).TemplateResponse(request, "settings/settings.html", context)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _render(request, db, get_settings_row(db), {}, None, None)


@router.post("/settings", response_class=HTMLResponse)
def settings_save(
    request: Request,
    db: Session = Depends(get_db),
    gitlab_url: str = Form(""),
    gitlab_project: str = Form(""),
    gitlab_token: str = Form(""),
    default_review_prompt: str = Form(""),
    known_libraries: str = Form(""),
    nightly_time: str = Form("02:30"),
    max_concurrent_reviews: str = Form("2"),
    poll_interval_seconds: str = Form("30"),
    post_results_to_gitlab: bool = Form(False),
) -> HTMLResponse:
    row = get_settings_row(db)
    raw = {
        "gitlab_url": gitlab_url,
        "gitlab_project": gitlab_project,
        "default_review_prompt": default_review_prompt,
        "known_libraries": known_libraries,
        "nightly_time": nightly_time,
        "max_concurrent_reviews": max_concurrent_reviews,
        "poll_interval_seconds": poll_interval_seconds,
        "post_results_to_gitlab": post_results_to_gitlab,
    }
    errors: list[str] | None = None
    try:
        max_reviews = int(max_concurrent_reviews)
        poll_seconds = int(poll_interval_seconds)
    except ValueError:
        errors = ["max_concurrent_reviews and poll_interval_seconds must be integers"]
    if errors is None:
        try:
            form = SettingsForm(
                gitlab_url=gitlab_url,
                gitlab_project=gitlab_project,
                gitlab_token=gitlab_token,
                default_review_prompt=default_review_prompt,
                known_libraries=known_libraries,
                nightly_time=nightly_time,
                max_concurrent_reviews=max_reviews,
                poll_interval_seconds=poll_seconds,
                post_results_to_gitlab=post_results_to_gitlab,
            )
        except ValidationError as exc:
            errors = [f"{('.'.join(str(part) for part in err['loc']))}: {err['msg']}" for err in exc.errors()]
    if errors is not None:
        return _render(request, db, row, raw, errors, None)

    row.gitlab_url = form.gitlab_url.strip()
    row.gitlab_project = form.gitlab_project.strip()
    if form.gitlab_token.strip():
        row.gitlab_token = encrypt_secret(form.gitlab_token.strip())
    row.default_review_prompt = form.default_review_prompt
    row.known_libraries = [lib.model_dump(exclude_none=True) for lib in form.known_libraries]
    row.nightly_time = form.nightly_time
    row.max_concurrent_reviews = form.max_concurrent_reviews
    row.poll_interval_seconds = form.poll_interval_seconds
    row.post_results_to_gitlab = form.post_results_to_gitlab
    db.commit()
    return _render(request, db, row, {}, None, "Settings saved.")


@router.post("/settings/test-connection")
def settings_test_connection(
    request: Request,
    db: Session = Depends(get_db),
    gitlab_url: str = Form(""),
    gitlab_project: str = Form(""),
    gitlab_token: str = Form(""),
):
    """Probe the connection (submitted values fall back to the saved ones) and
    return an inline result partial for HTMX."""
    row = get_settings_row(db)
    url = gitlab_url.strip() or row.gitlab_url
    project = gitlab_project.strip() or row.gitlab_project
    token = gitlab_token.strip() or decrypt_secret(row.gitlab_token)
    probe = SettingsRow(
        id=1,
        gitlab_url=url,
        gitlab_project=project,
        gitlab_token=encrypt_secret(token),
    )
    try:
        ok, message = GitLabClient(probe).test_connection()
    except (GitLabError, requests.RequestException) as exc:
        ok, message = False, str(exc)
    return _templates(request).TemplateResponse(
        request,
        "settings/partials/test_result.html",
        {"ok": ok, "message": message},
    )


def _render_profiles(
    request: Request,
    db: Session,
    delete_error: str | None = None,
    profile_raw: dict[str, object] | None = None,
    profile_errors: list[str] | None = None,
) -> HTMLResponse:
    """Render just the model-profiles section (HTMX swap target)."""
    return _templates(request).TemplateResponse(
        request,
        "settings/partials/model_profiles.html",
        {
            "profiles": [_profile_display(p) for p in _list_profiles(db)],
            "delete_error": delete_error,
            "profile_raw": profile_raw,
            "profile_errors": profile_errors,
        },
    )


@router.post("/settings/models", response_class=HTMLResponse)
def settings_model_create(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(""),
    provider: str = Form("anthropic"),
    model_id: str = Form(""),
    base_url: str = Form(""),
    api_key: str = Form(""),
    api_key_env: str = Form(""),
    is_default: bool = Form(False),
    extra_opencode_json: str = Form("{}"),
) -> HTMLResponse:
    row = get_settings_row(db)
    raw = {
        "name": name,
        "provider": provider,
        "model_id": model_id,
        "base_url": base_url,
        "api_key_env": api_key_env,
        "is_default": is_default,
        "extra_opencode_json": extra_opencode_json,
    }
    errors: list[str] | None = None
    try:
        form = ModelProfileForm(
            name=name,
            provider=provider,
            model_id=model_id,
            base_url=base_url,
            api_key=api_key,
            api_key_env=api_key_env,
            is_default=is_default,
            extra_opencode_json=extra_opencode_json,
        )
    except ValidationError as exc:
        errors = [f"{('.'.join(str(part) for part in err['loc']))}: {err['msg']}" for err in exc.errors()]
    if errors is not None:
        return _render(request, db, row, {}, None, None, profile_raw=raw, profile_errors=errors)
    if is_default:
        for other in db.scalars(select(ModelProfile).where(ModelProfile.is_default.is_(True))):
            other.is_default = False
    db.add(
        ModelProfile(
            name=form.name,
            provider=form.provider,
            model_id=form.model_id,
            base_url=form.base_url or None,
            api_key=encrypt_secret(form.api_key) if form.api_key.strip() else None,
            api_key_env=form.api_key_env.strip() or None,
            is_default=form.is_default,
            extra_opencode_json=form.extra_opencode_json,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raw_no_key = dict(raw, api_key="")
        return _render(
            request,
            db,
            row,
            {},
            None,
            None,
            profile_raw=raw_no_key,
            profile_errors=[f"name: a profile named {form.name!r} already exists"],
        )
    return _render(request, db, row, {}, None, f"Model profile {form.name!r} added.")


@router.post("/settings/models/{profile_id}/delete", response_class=HTMLResponse)
def settings_model_delete(request: Request, profile_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    """Delete a profile (HTMX swap target is the profiles section)."""
    profile = db.get(ModelProfile, profile_id)
    if profile is None:
        return _render_profiles(request, db, delete_error=f"No profile with id {profile_id}.")
    in_use = db.scalar(
        select(func.count(ScheduledJob.id)).where(ScheduledJob.model_profile_id == profile.id)
    )
    if in_use:
        return _render_profiles(
            request,
            db,
            delete_error=f"Profile {profile.name!r} is used by {in_use} job(s) and can't "
            "be deleted while jobs reference it (job rows are kept as history).",
        )
    db.delete(profile)
    db.commit()
    return _render_profiles(request, db)
