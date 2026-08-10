"""GitHub-backed :class:`~tai42_contract.storage.Storage` provider.

Importing this package registers :class:`GithubStorage` as the active storage
provider via an import side-effect.
"""

from tai42_storage_github.storage import GithubStorage

__all__ = ["GithubStorage"]
