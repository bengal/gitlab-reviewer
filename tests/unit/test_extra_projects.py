"""Normalization of extra-projects entries (app/services/extra_projects.py)."""

from app.services.extra_projects import derive_path, normalize_extra_projects


def test_derive_path_from_url_shapes():
    assert derive_path("https://github.com/systemd/systemd.git") == "systemd"
    assert derive_path("https://gitlab.gnome.org/GNOME/glib.git") == "glib"
    assert derive_path("https://git.w1.fi/hostap.git") == "hostap"
    assert derive_path("https://git.kernel.org/pub/scm/network/wireless/iwd.git") == "iwd"
    assert derive_path("file:///srv/repos/liba.git") == "liba"
    assert derive_path("git@github.com:user/repo.git") == "repo"
    assert derive_path("https://example.com/repo.git?x=1#frag") == "repo"
    assert derive_path("https://example.com/") == ""
    assert derive_path("https://example.com/.git") == ""
    assert derive_path("") == ""


def test_normalize_fills_missing_path_from_url():
    entries = [{"url": "https://github.com/systemd/systemd.git", "ref": "main"}]
    assert normalize_extra_projects(entries) == [
        {"url": "https://github.com/systemd/systemd.git", "ref": "main", "path": "systemd"}
    ]
    # the stored input is never mutated
    assert "path" not in entries[0]


def test_normalize_keeps_explicit_paths_and_passthrough():
    entries = [
        {"url": "https://x.example.com/a/repo.git", "path": "custom"},
        {"path": "only-a-path"},  # no url: consumers skip it, as before
        "junk",  # malformed: passed through
        {"url": "https://x.example.com/.git"},  # url without a usable name
    ]
    assert normalize_extra_projects(entries) == entries
    assert normalize_extra_projects(None) == []
    assert normalize_extra_projects([]) == []


def test_normalize_collision_between_urls_gets_suffix():
    entries = [
        {"url": "https://a.example.com/grp/repo.git"},
        {"url": "https://b.example.com/other/repo.git"},
    ]
    normalized = normalize_extra_projects(entries)
    assert normalized[0]["path"] == "repo"
    assert normalized[1]["path"].startswith("repo-")
    assert normalized[0]["path"] != normalized[1]["path"]


def test_normalize_same_url_twice_shares_derived_path():
    entries = [{"url": "https://a.example.com/x/repo.git"}] * 2
    normalized = normalize_extra_projects(entries)
    assert normalized[0]["path"] == normalized[1]["path"] == "repo"
