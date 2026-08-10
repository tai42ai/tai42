import hashlib
import keyword
import string

_IDENTIFIER_CHARS = frozenset(string.ascii_letters + string.digits + "_")


def makefun_func_name(name: str) -> str:
    """A valid Python identifier for a ``makefun.create_function`` ``func_name``.

    A tool/preset/agent name is a wider charset than a Python identifier — it may
    carry ``-`` or lead with a digit, could be a keyword, or hold unicode — but
    makefun emits ``def <func_name>(...)`` and silently falls back to a BROKEN
    lambda form for a non-identifier name (any signature with a return annotation
    then fails to compile). ``func_name`` only sets the generated wrapper's
    cosmetic ``__name__``, so a normalized identifier is a faithful, safe stand-in.

    Every char outside ``[0-9A-Za-z_]`` is substituted IN PLACE with ``_`` (not
    collapsed to a single constant), then a leading digit / keyword result gets a
    ``_`` prefix — so distinct inputs stay distinct valid identifiers.
    """
    candidate = "".join(char if char in _IDENTIFIER_CHARS else "_" for char in name)
    if not candidate or candidate[0].isdigit() or keyword.iskeyword(candidate):
        candidate = f"_{candidate}"
    return candidate


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
