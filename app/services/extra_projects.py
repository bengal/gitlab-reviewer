"""Normalization of extra-projects entries (the ``{url, ref, path}`` dicts).

Extra library projects are stored exactly as the user typed them — ``{url,
ref?, path?}`` (see ``app/routers/queue.py::_parse_extra_projects`` and
``app/routers/mrs.py``). When only a URL was given, the ``path`` under which
the checkout is mounted at ``/work/lib/<path>`` is derived from the last
non-empty path segment of the URL (a trailing ``.git`` stripped).

Derivation happens at consumption time (prompt, container env, library
checkouts) — never at enqueue/store time: the stored row keeps what the user
typed, and every consumer derives the same path, so the prompt, the bind
mounts and the review-runner entrypoint all agree on ``/work/lib/<path>``.
"""

import hashlib
from urllib.parse import urlsplit

_GIT_SUFFIX = ".git"


def derive_path(url: str) -> str:
    """The default ``/work/lib``-relative path for a repo URL: the last
    non-empty segment of the URL's path component, with a trailing ``.git``
    stripped ("" when the URL has no path, e.g. a bare host)."""
    path = urlsplit(str(url or "")).path
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return ""
    name = segments[-1]
    if name.endswith(_GIT_SUFFIX):
        name = name[: -len(_GIT_SUFFIX)]
    return name


def normalize_extra_projects(entries):
    """A copy of ``entries`` with a usable ``path`` on every dict entry that
    has a ``url``.

    - an explicit non-empty ``path`` always wins and is left untouched;
    - a missing path is derived from the URL; when two different URLs derive
      the same path, the later one gets a short hash suffix, so two
      libraries can never collide on one checkout dir;
    - entries without a url (or whose URL yields no name) pass through
      unchanged — the consumers skip them, as before;
    - non-dict entries pass through unchanged;
    - the input is never mutated.
    """
    result: list = []
    owner: dict[str, str] = {}  # derived path -> the url that owns it
    for entry in entries or []:
        if not isinstance(entry, dict):
            result.append(entry)
            continue
        url = str(entry.get("url") or "").strip()
        path = str(entry.get("path") or "").strip().strip("/")
        if not url or path:
            result.append(entry)
            continue
        name = derive_path(url)
        if not name or ".." in name.split("/"):
            result.append(entry)
            continue
        if name in owner and owner[name] != url:
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:6]
            name = f"{name}-{digest}"
        owner[name] = url
        normalized = dict(entry)
        normalized["path"] = name
        result.append(normalized)
    return result
