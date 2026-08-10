"""Direct GitHub REST client for the repository-webhook provisioning tools.

Speaks GitHub's REST v3 API through tai42-kit's curl client — no SDK. Creates, lists and deletes a
repository's webhooks under token auth, following the ``Link`` response header to page a list to
exhaustion under a hard page ceiling.

Facts pinned here are read from GitHub's published REST docs (https://docs.github.com/rest) and
nowhere else:

- Requests send ``Accept: application/vnd.github+json`` and pin ``X-GitHub-Api-Version`` so the
  response shape cannot drift with GitHub's default (docs.github.com/rest/overview/api-versions);
  auth is ``Authorization: Bearer <token>``.
- Create is ``POST /repos/{owner}/{repo}/hooks`` with ``config`` ``{url, content_type, secret}`` and
  an ``events`` list; the response carries ``id``, ``config.url``, ``events`` and ``active`` and
  never the secret (docs.github.com/rest/repos/webhooks#create-a-repository-webhook).
- List is ``GET /repos/{owner}/{repo}/hooks`` paginated by the ``Link`` header's ``rel="next"``
  URL (docs.github.com/rest/using-the-rest-api/using-pagination-in-the-rest-api).
- Delete is ``DELETE /repos/{owner}/{repo}/hooks/{hook_id}`` and succeeds with ``204 No Content``
  (docs.github.com/rest/repos/webhooks#delete-a-repository-webhook).
"""

from __future__ import annotations

import json
import re
from typing import Any, cast
from urllib.parse import quote

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict
from tai42_contract.app import tai42_app
from tai42_kit.clients.impl.curl import CurlClient
from tai42_kit.settings import TaiBaseSettings, settings_cache

# The GitHub REST API version every request pins. Direct REST carries no SDK-pinned version, and an
# unpinned request is served GitHub's current default -- so the response shape could change under
# the tools without a release. Read from GitHub's published api-versions docs.
GITHUB_API_VERSION = "2022-11-28"

# The media type GitHub's REST v3 responses are negotiated under.
GITHUB_ACCEPT = "application/vnd.github+json"

# ``owner/name``: a non-empty owner and a non-empty name, one slash, no whitespace anywhere. Path
# segments are percent-encoded per part, so this guards shape, not character set.
_REPO_RE = re.compile(r"^[^/\s]+/[^/\s]+$")

# One ``<url>; rel="next"`` element of a ``Link`` response header.
_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*[^,]*\brel="next"')

# The hard page ceiling the list loop follows ``Link``'s ``rel="next"`` under. Finite pagination is
# the server's promise, not the client's guarantee: a buggy or hostile ``Link`` header that always
# names a next page would loop forever, so the loop RAISES at the ceiling rather than spinning. A
# repository's webhook count is small, so a page far below this bound is the real terminal case.
_LIST_PAGE_CEILING = 100


class GithubToolsSettings(TaiBaseSettings):
    """GitHub provisioning-tool configuration, read from ``TOOLS_GITHUB_``-prefixed env."""

    model_config = SettingsConfigDict(env_prefix="TOOLS_GITHUB_")

    token: SecretStr | None = None
    api_base: str = "https://api.github.com"
    request_timeout_seconds: float = 20


@settings_cache
def github_tools_settings() -> GithubToolsSettings:
    return GithubToolsSettings()


def _token() -> str:
    """The configured GitHub token, revealed.

    Missing and EMPTY are the same failure and both raise naming ``TOOLS_GITHUB_TOKEN``: an empty
    env var parses to ``SecretStr("")`` (not ``None``), so a plain ``is None`` test would send
    ``Authorization: Bearer`` with no credential. This is the module's only read path for the value,
    so the error is identical on every path.
    """
    token = github_tools_settings().token
    if not (token and token.get_secret_value()):
        raise ValueError("TOOLS_GITHUB_TOKEN is not set (missing or empty)")
    return token.get_secret_value()


def _headers(*, json_body: bool) -> dict[str, str]:
    """The request headers for a GitHub REST call. The token rides ``Authorization`` and is never
    echoed into any raised text or return value.
    """
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Accept": GITHUB_ACCEPT,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _split_repo(repo: str) -> tuple[str, str]:
    """Validate ``repo`` as exactly ``owner/name`` and return ``(owner, name)``.

    Any other shape -- empty side, extra slash, whitespace, non-string -- raises loudly naming the
    value, so a malformed repo can never be interpolated into a request path.
    """
    if not (isinstance(repo, str) and _REPO_RE.match(repo)):
        raise ValueError(f"repo must be 'owner/name'; got {repo!r}")
    owner, name = repo.split("/")
    return owner, name


def _hooks_url(owner: str, name: str) -> str:
    """The ``/repos/{owner}/{name}/hooks`` collection URL with each path segment percent-encoded
    on its own (``safe=""`` encodes ``/ ? #``) so neither part can escape into a different path.
    """
    return f"{github_tools_settings().api_base}/repos/{quote(owner, safe='')}/{quote(name, safe='')}/hooks"


async def _http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    data: str | None = None,
) -> tuple[int, dict[str, str], str]:
    """Issue one request through a fresh curl session and return ``(status, lowercased headers, body
    text)``. Redirects are OFF: every request carries ``Authorization: Bearer <token>`` and libcurl
    replays custom headers across a redirect hop -- a 3xx off the pinned host would hand the token to
    another host. A non-2xx is returned as its status and body (never raised here) -- the caller
    decides what a status means.
    """
    session_ctx = tai42_app.clients.client_ctx(CurlClient, session_params={}, fresh=True)
    async with session_ctx as session:
        resp = await session.request(
            url=url,
            method=cast("Any", method),
            headers=headers,
            data=data,
            timeout=github_tools_settings().request_timeout_seconds,
            allow_redirects=False,
        )
        resp_headers = {key.lower(): value for key, value in resp.headers.items() if value is not None}
        return resp.status_code, resp_headers, resp.text


def _next_page_url(link_header: str | None) -> str | None:
    """The ``rel="next"`` URL from a ``Link`` response header, or ``None`` when the header is
    absent or names no next page (the last page)."""
    if not link_header:
        return None
    match = _LINK_NEXT_RE.search(link_header)
    return match.group(1) if match else None


async def create_webhook(repo: str, url: str, events: list[str], secret: str) -> dict[str, Any]:
    """POST a new repository webhook and return ``{id, url, events, active}``.

    ``secret`` is sent inside ``config`` and is NEVER returned or echoed into a raised message --
    GitHub does not return it, and the caller owns storing it. A non-2xx raises loudly with GitHub's
    status and body (never caught, never remapped); GitHub's error body carries neither the token
    nor the caller's secret.
    """
    owner, name = _split_repo(repo)
    body = json.dumps({"config": {"url": url, "content_type": "json", "secret": secret}, "events": events})
    status, _resp_headers, text = await _http_request(
        "POST", _hooks_url(owner, name), headers=_headers(json_body=True), data=body
    )
    if not 200 <= status < 300:
        raise ValueError(f"GitHub webhook create failed: HTTP {status} {text}")
    hook = json.loads(text)
    return {"id": hook["id"], "url": hook["config"]["url"], "events": hook["events"], "active": hook["active"]}


async def list_webhooks(repo: str) -> list[dict[str, Any]]:
    """GET every webhook on ``repo`` as ``{id, url, events, active}``, following the ``Link`` header
    to exhaustion under ``_LIST_PAGE_CEILING``.

    The loop terminates when GitHub names no ``rel="next"`` page, and RAISES at the page ceiling
    rather than following a self-referential ``Link`` header forever. A non-2xx on any page raises
    loudly with GitHub's status and body. GitHub never returns a hook's secret.
    """
    owner, name = _split_repo(repo)
    next_url: str | None = _hooks_url(owner, name)
    hooks: list[dict[str, Any]] = []
    pages = 0
    while next_url is not None:
        if pages >= _LIST_PAGE_CEILING:
            raise ValueError(f"GitHub webhook list exceeded its {_LIST_PAGE_CEILING}-page ceiling for {repo!r}")
        status, resp_headers, text = await _http_request("GET", next_url, headers=_headers(json_body=False))
        if not 200 <= status < 300:
            raise ValueError(f"GitHub webhook list failed: HTTP {status} {text}")
        for hook in json.loads(text):
            hooks.append(
                {"id": hook["id"], "url": hook["config"]["url"], "events": hook["events"], "active": hook["active"]}
            )
        pages += 1
        next_url = _next_page_url(resp_headers.get("link"))
    return hooks


async def delete_webhook(repo: str, hook_id: int) -> dict[str, Any]:
    """DELETE one webhook and return ``{"repo", "hook_id", "deleted": True}`` on GitHub's ``204``.

    Any status other than ``204`` -- a 404 for an unknown hook, any other non-2xx -- raises loudly
    with GitHub's status and body, never a silent success.
    """
    owner, name = _split_repo(repo)
    url = f"{_hooks_url(owner, name)}/{hook_id}"
    status, _resp_headers, text = await _http_request("DELETE", url, headers=_headers(json_body=False))
    if status != 204:
        raise ValueError(f"GitHub webhook delete failed: HTTP {status} {text}")
    return {"repo": repo, "hook_id": hook_id, "deleted": True}
