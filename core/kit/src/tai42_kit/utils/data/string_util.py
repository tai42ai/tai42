import hashlib


def text_to_md5(string: str) -> str:
    """Return the hex MD5 digest of *string* (UTF-8 encoded).

    A non-cryptographic content fingerprint (cache keys, dedup ids), not for
    security use.
    """
    return hashlib.md5(string.encode("utf-8")).hexdigest()


def hash_api_key(key: str) -> str:
    """Return the hex SHA-256 digest of an API *key* (UTF-8 encoded).

    A one-way fingerprint so a raw key is never stored: callers persist and look
    up identities by this digest.
    """
    return hashlib.sha256(key.encode()).hexdigest()


def snake_to_pascal(string: str) -> str:
    """Convert a snake_case string to PascalCase.

    Each ``_``-separated word is capitalized with its tail lowercased, so
    ``"ABC_DEF"`` becomes ``"AbcDef"``.
    """
    return "".join(word.capitalize() for word in string.split("_"))
