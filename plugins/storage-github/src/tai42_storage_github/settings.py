"""Settings for the GitHub storage backend (``STORAGE_GITHUB_`` env vars).

The token is a :class:`~pydantic.SecretStr` so it never surfaces in a repr, log,
or traceback.
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings, settings_cache


class GithubStorageSettings(TaiBaseSettings):
    """``STORAGE_GITHUB_*`` credentials, repo target, REST base URLs, and connection pool sizing."""

    model_config = SettingsConfigDict(env_prefix="STORAGE_GITHUB_")

    username: str | None = None
    repo: str | None = None
    branch: str = "main"
    token: SecretStr | None = None
    # Base-URL templates for the four REST and raw surfaces the provider speaks,
    # defaulting to the real GitHub hosts. Overridable (like ``S3Settings.endpoint``)
    # so a test can point the plugin at a hermetic stand-in. Each carries
    # ``{username}``/``{repo}`` placeholders the provider fills per request; the ref
    # and trees templates also ``{branch}``, and the raw template ``{commit}``, the
    # branch head commit a ranged read resolves first.
    raw_base_url: str = "https://raw.githubusercontent.com/{username}/{repo}/{commit}"
    contents_api_url: str = "https://api.github.com/repos/{username}/{repo}/contents"
    ref_api_url: str = "https://api.github.com/repos/{username}/{repo}/git/ref/heads/{branch}"
    trees_api_url: str = "https://api.github.com/repos/{username}/{repo}/git/trees/{branch}"
    timeout_total: float = 15.0
    max_connections: int = 200
    max_keepalive_connections: int = 50
    keepalive_expiry: float = 300.0


@settings_cache
def github_storage_settings() -> GithubStorageSettings:
    """The cached :class:`GithubStorageSettings` for this process."""
    return GithubStorageSettings()
