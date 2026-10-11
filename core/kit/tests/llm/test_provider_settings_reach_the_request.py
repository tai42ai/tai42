"""Every setting handed to a provider build reaches that provider, or the build refuses it naming it.

Each provider's chat model and embedding model is built through the kit against a local stub of its
HTTP API, invoked once, and the request the stub received is read: a setting the provider sends shows in the request
body, a connection setting shows on the request (the credential header) or on the client, and a
setting the provider can carry neither way is refused at build. Each provider's package is an
optional extra; its cases skip visibly without it.
"""

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest

pytest.importorskip("langgraph")

from tai42_kit.llm import embedding, models

# google-genai (the google provider's client) subclasses ``aiohttp.ClientSession`` for its async
# session (``class AiohttpClientSession`` in ``google/genai/_api_client.py``), which aiohttp deprecates
# with a ``DeprecationWarning`` from that module; langchain-huggingface's ``ChatHuggingFace``
# validation imports langchain-community where it is installed, which raises its sunset
# ``DeprecationWarning``. Exactly those two warnings are ignored; every other warning stays an error.
pytestmark = [
    pytest.mark.filterwarnings(
        "ignore:Inheritance class AiohttpClientSession from ClientSession is discouraged"
        ":DeprecationWarning:google.genai._api_client"
    ),
    pytest.mark.filterwarnings(
        "ignore:`langchain-community` is being sunset and is no longer actively maintained:DeprecationWarning"
    ),
]

_OPENAI_REPLY = {
    "id": "c1",
    "object": "chat.completion",
    "created": 1,
    "model": "m",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
_ANTHROPIC_REPLY = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "m",
    "content": [{"type": "text", "text": "hi"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
_GEMINI_REPLY = {
    "candidates": [{"content": {"role": "model", "parts": [{"text": "hi"}]}, "finishReason": "STOP", "index": 0}],
    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
}
_OPENAI_EMBEDDING_REPLY = {
    "object": "list",
    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
    "model": "m",
    "usage": {"prompt_tokens": 1, "total_tokens": 1},
    "id": "e1",
}
_GEMINI_EMBEDDING_REPLY = {"embeddings": [{"values": [0.1, 0.2]}]}
_OLLAMA_EMBEDDING_REPLY = {"model": "m", "embeddings": [[0.1, 0.2]]}
_OLLAMA_REPLY = {
    "model": "m",
    "created_at": "2026-01-01T00:00:00Z",
    "message": {"role": "assistant", "content": "hi"},
    "done": True,
    "done_reason": "stop",
    "prompt_eval_count": 1,
    "eval_count": 1,
}


@dataclass
class _Seen:
    path: str
    # Every header line, in the order sent (a field sent twice shows twice).
    lines: list[tuple[str, str]]
    body: dict[str, Any] = field(default_factory=dict)

    @property
    def headers(self) -> dict[str, str]:
        """The header fields by lower-case name (the stub fixture has checked each is sent once)."""
        return {name.lower(): value for name, value in self.lines}

    def repeated_fields(self) -> list[str]:
        """The header field names sent on more than one line (HTTP names compared without letter case)."""
        names = [name.lower() for name, _ in self.lines]
        return sorted({name for name in names if names.count(name) > 1})


class _Stub:
    """A local HTTP server answering each provider's chat endpoint and recording every request."""

    def __init__(self) -> None:
        self.requests: list[_Seen] = []
        stub = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                return

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length", 0)))
                stub.requests.append(_Seen(self.path, list(self.headers.items()), json.loads(raw or b"{}")))
                ndjson = self.path.endswith("/api/chat")
                if self.path.endswith("/embeddings"):
                    reply: dict[str, Any] = _OPENAI_EMBEDDING_REPLY
                elif ":batchEmbedContents" in self.path:
                    reply = _GEMINI_EMBEDDING_REPLY
                elif self.path.endswith("/api/embed"):
                    reply = _OLLAMA_EMBEDDING_REPLY
                elif self.path.endswith("/chat/completions"):
                    reply = _OPENAI_REPLY
                elif self.path.endswith("/messages"):
                    reply = _ANTHROPIC_REPLY
                elif ":generateContent" in self.path:
                    reply = _GEMINI_REPLY
                else:
                    reply = _OLLAMA_REPLY
                data = (json.dumps(reply) + ("\n" if ndjson else "")).encode()
                self.send_response(200)
                self.send_header("content-type", "application/x-ndjson" if ndjson else "application/json")
                self.send_header("content-length", str(len(data)))
                # Each client closes its connection after the reply, so no test leaves a socket open.
                self.send_header("connection", "close")
                self.close_connection = True
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


@pytest.fixture
def stub() -> Iterator[_Stub]:
    server = _Stub()
    models._cached_llm.cache_clear()
    embedding._cached_embedding.cache_clear()
    yield server
    models._cached_llm.cache_clear()
    embedding._cached_embedding.cache_clear()
    server.close()
    # A header field sent on two lines reaches a first-value server with only one of them.
    assert [(seen.path, seen.repeated_fields()) for seen in server.requests if seen.repeated_fields()] == []


# provider -> (its package, the kwargs that point it at the stub, the credential header the stub sees)
_PROVIDERS: dict[str, tuple[str, Any, tuple[str, str] | None]] = {
    "openai": (
        "langchain_openai",
        lambda url: {"base_url": f"{url}/v1", "api_key": "k"},
        ("authorization", "Bearer k"),
    ),
    "xai": ("langchain_xai", lambda url: {"xai_api_base": f"{url}/v1", "api_key": "k"}, ("authorization", "Bearer k")),
    "mistral": (
        "langchain_mistralai",
        lambda url: {"base_url": f"{url}/v1", "api_key": "k"},
        ("authorization", "Bearer k"),
    ),
    "anthropic": ("langchain_anthropic", lambda url: {"base_url": url, "api_key": "k"}, ("x-api-key", "k")),
    "google": ("langchain_google_genai", lambda url: {"base_url": url, "api_key": "k"}, ("x-goog-api-key", "k")),
    "ollama": ("langchain_ollama", lambda url: {"base_url": url, "api_key": "k"}, ("authorization", "Bearer k")),
    "huggingface": (
        "langchain_huggingface",
        lambda url: {"base_url": url, "api_key": "k"},
        ("authorization", "Bearer k"),
    ),
}

_REFUSED = object()
# The setting selects the request's URL path (the API version segment).
_IN_PATH = object()

# (provider, setting, value) -> where the value shows in the request body, or _REFUSED.
_EXPECTED: dict[str, dict[str, Any]] = {
    "openai": {
        "temperature": ("temperature",),
        "max_tokens": ("max_completion_tokens",),
        "top_p": ("top_p",),
        "top_k": _REFUSED,
        "n": ("n",),
        "api_version": _REFUSED,
        "reasoning_effort": ("reasoning_effort",),
        "seed": ("seed",),
        "frobnicate": _REFUSED,
        "max_output_tokens": _REFUSED,
        "instructions": _REFUSED,
    },
    "xai": {
        "temperature": ("temperature",),
        "max_tokens": ("max_tokens",),
        "top_p": ("top_p",),
        "top_k": _REFUSED,
        "n": ("n",),
        "api_version": _REFUSED,
        "reasoning_effort": ("reasoning_effort",),
        "seed": ("seed",),
        "frobnicate": _REFUSED,
        "max_output_tokens": _REFUSED,
        "instructions": _REFUSED,
    },
    "mistral": {
        "temperature": ("temperature",),
        "max_tokens": ("max_tokens",),
        "top_p": ("top_p",),
        "top_k": ("top_k",),
        "n": ("n",),
        "api_version": ("api_version",),
        "reasoning_effort": ("reasoning_effort",),
        "seed": ("seed",),
        "frobnicate": ("frobnicate",),
        "max_concurrent_requests": _REFUSED,
    },
    "anthropic": {
        "temperature": ("temperature",),
        "max_tokens": ("max_tokens",),
        "top_p": ("top_p",),
        "top_k": ("top_k",),
        "n": _REFUSED,
        "api_version": _REFUSED,
        "reasoning_effort": ("output_config", "effort"),
        "seed": _REFUSED,
        "frobnicate": _REFUSED,
        "speed": _REFUSED,
    },
    "google": {
        "temperature": ("generationConfig", "temperature"),
        "max_tokens": ("generationConfig", "maxOutputTokens"),
        "top_p": ("generationConfig", "topP"),
        "top_k": ("generationConfig", "topK"),
        "n": ("generationConfig", "candidateCount"),
        "api_version": _IN_PATH,
        "reasoning_effort": ("generationConfig", "thinkingConfig", "thinking_level"),
        "seed": ("generationConfig", "seed"),
        "frobnicate": _REFUSED,
    },
    "ollama": {
        "temperature": ("options", "temperature"),
        "max_tokens": ("options", "num_predict"),
        "top_p": ("options", "top_p"),
        "top_k": ("options", "top_k"),
        "n": _REFUSED,
        "api_version": _REFUSED,
        "reasoning_effort": ("think",),
        "seed": ("options", "seed"),
        "frobnicate": _REFUSED,
    },
    "huggingface": {
        "temperature": ("temperature",),
        "max_tokens": ("max_tokens",),
        "top_p": ("top_p",),
        "top_k": _REFUSED,
        "n": ("n",),
        "api_version": _REFUSED,
        "reasoning_effort": _REFUSED,
        "seed": ("seed",),
        "frobnicate": _REFUSED,
    },
}

_VALUES: dict[str, Any] = {
    "temperature": 0.25,
    "max_tokens": 7,
    "top_p": 0.5,
    "top_k": 3,
    "n": 2,
    "api_version": "v9",
    "api_key": "k",
    "reasoning_effort": "low",
    "seed": 42,
    "frobnicate": 3,
    "max_output_tokens": 7,
    "max_concurrent_requests": 1,
    "instructions": "be brief",
    "speed": "fast",
}

# What a sent value looks like where the provider encodes it differently.
_SENT_AS: dict[tuple[str, str], Any] = {
    ("anthropic", "reasoning_effort"): "low",
    ("google", "reasoning_effort"): "LOW",
    ("google", "top_k"): 3.0,
}


def _cases() -> list[Any]:
    return [
        pytest.param(provider, setting, id=f"{provider}-{setting}")
        for provider, table in _EXPECTED.items()
        for setting in table
    ]


def _build(provider: str, stub: _Stub, **settings: Any) -> Any:
    package, target, _ = _PROVIDERS[provider]
    pytest.importorskip(package)
    return models._build_llm(provider, model="m", **target(stub.url), **settings)


@pytest.mark.parametrize(("provider", "setting"), _cases())
def test_a_setting_reaches_the_request_or_is_refused_at_build(stub: _Stub, provider: str, setting: str) -> None:
    expected = _EXPECTED[provider][setting]
    value = _VALUES[setting]
    if expected is _REFUSED:
        pytest.importorskip(_PROVIDERS[provider][0])
        with pytest.raises(ValueError, match=rf"{provider}: .*'{setting}'"):
            _build(provider, stub, **{setting: value})
        assert stub.requests == []
        return
    _build(provider, stub, **{setting: value}).invoke("hello")
    assert len(stub.requests) == 1
    if expected is _IN_PATH:
        assert f"/{value}/" in stub.requests[0].path
        return
    sent: Any = stub.requests[0].body
    for key in expected:
        sent = sent[key]
    assert sent == _SENT_AS.get((provider, setting), value)


@pytest.mark.parametrize("provider", list(_PROVIDERS))
def test_the_connection_settings_reach_the_request(stub: _Stub, provider: str) -> None:
    _build(provider, stub).invoke("hello")
    assert len(stub.requests) == 1
    header = _PROVIDERS[provider][2]
    if header is not None:
        assert stub.requests[0].headers[header[0]] == header[1]


def test_huggingface_sends_no_generation_setting_nobody_configured(stub: _Stub) -> None:
    _build("huggingface", stub).invoke("hello")
    body = stub.requests[0].body
    assert {"max_tokens", "temperature", "top_p", "seed", "n"}.isdisjoint(body), body


@pytest.mark.parametrize("provider", ["openai", "xai"])
def test_a_model_kwargs_entry_the_sdk_call_does_not_take_is_refused_at_build(stub: _Stub, provider: str) -> None:
    with pytest.raises(ValueError, match=rf"{provider}: 'frobnicate' cannot reach the provider"):
        _build(provider, stub, model_kwargs={"frobnicate": 1})
    assert stub.requests == []


def test_a_model_kwargs_entry_the_sdk_call_takes_is_sent(stub: _Stub) -> None:
    _build("openai", stub, model_kwargs={"user": "u-1"}).invoke("hello")
    assert stub.requests[0].body["user"] == "u-1"


# provider -> how the built model exposes the request timeout its client applies.
_TIMEOUT_OF: dict[str, Any] = {
    "openai": lambda llm: llm.request_timeout,
    "xai": lambda llm: llm.request_timeout,
    "mistral": lambda llm: llm.timeout,
    "anthropic": lambda llm: llm.default_request_timeout,
    "google": lambda llm: llm.timeout,
    "ollama": lambda llm: llm.client_kwargs["timeout"],
    "huggingface": lambda llm: llm.llm.timeout,
}


@pytest.mark.parametrize("provider", list(_PROVIDERS))
def test_the_timeout_is_applied_by_the_client(stub: _Stub, provider: str) -> None:
    llm = _build(provider, stub, timeout=9)
    assert _TIMEOUT_OF[provider](llm) == 9


# -- embedding builders ------------------------------------------------------------------------------

# provider -> (its package, the kwargs that point it at the stub, the credential header the stub sees)
_EMBEDDING_PROVIDERS: dict[str, tuple[str, Any, tuple[str, str] | None]] = {
    "openai": (
        "langchain_openai",
        lambda url: {"base_url": f"{url}/v1", "api_key": "k"},
        ("authorization", "Bearer k"),
    ),
    "mistral": (
        "langchain_mistralai",
        lambda url: {"base_url": f"{url}/v1/", "api_key": "k"},
        ("authorization", "Bearer k"),
    ),
    "google": ("langchain_google_genai", lambda url: {"base_url": url, "api_key": "k"}, ("x-goog-api-key", "k")),
    "ollama": ("langchain_ollama", lambda url: {"base_url": url, "api_key": "k"}, ("authorization", "Bearer k")),
    # A local sentence-transformers model: no HTTP client, so every connection setting but the hub token
    # (``api_key``, see the dedicated test) is refused.
    "huggingface": ("langchain_huggingface", lambda url: {}, None),
}

# The request header a setting shows as.
_HEADER = object()

# (provider, setting) -> where the value shows in the request body, _HEADER, _IN_PATH, or _REFUSED.
_EMBEDDING_EXPECTED: dict[str, dict[str, Any]] = {
    "openai": {
        "user_agent": _HEADER,
        "api_version": _REFUSED,
        "task": _REFUSED,
        "dimensions": ("dimensions",),
        "model_kwargs": ("user",),
        "frobnicate": _REFUSED,
        "headers": _REFUSED,
        "retry_min_seconds": _REFUSED,
        "retry_max_seconds": _REFUSED,
    },
    "mistral": {
        "user_agent": _REFUSED,
        "api_version": _REFUSED,
        "task": _REFUSED,
        "dimensions": _REFUSED,
        "model_kwargs": _REFUSED,
        "frobnicate": _REFUSED,
        "max_concurrent_requests": _REFUSED,
    },
    "google": {
        "user_agent": _HEADER,
        "api_version": _IN_PATH,
        "timeout": _REFUSED,
        "task": _REFUSED,
        "dimensions": _REFUSED,
        "model_kwargs": _REFUSED,
        "request_options": _REFUSED,
        "frobnicate": _REFUSED,
    },
    "ollama": {
        "user_agent": _REFUSED,
        "api_version": _REFUSED,
        "task": _REFUSED,
        "dimensions": ("dimensions",),
        "model_kwargs": _REFUSED,
        "frobnicate": _REFUSED,
    },
    "huggingface": {
        "base_url": _REFUSED,
        "timeout": _REFUSED,
        "user_agent": _REFUSED,
        "api_version": _REFUSED,
        "task": _REFUSED,
        "frobnicate": _REFUSED,
    },
}

_EMBEDDING_VALUES: dict[str, Any] = {
    "base_url": "http://127.0.0.1:1",
    "api_key": "k",
    "timeout": 9,
    "user_agent": "ua-x",
    "api_version": "v9",
    "task": "feature-extraction",
    "dimensions": 4,
    "model_kwargs": {"user": "u-1"},
    "request_options": {"timeout": 9},
    "frobnicate": 3,
    "headers": {"x-tenant": "a"},
    "retry_min_seconds": 1,
    "retry_max_seconds": 2,
    "max_concurrent_requests": 1,
}
_EMBEDDING_SENT_AS: dict[tuple[str, str], Any] = {("openai", "model_kwargs"): "u-1"}


def _build_embedding(provider: str, stub: _Stub, **settings: Any) -> Any:
    package, target, _ = _EMBEDDING_PROVIDERS[provider]
    pytest.importorskip(package)
    return embedding._build_embedding(provider, model="m", **{**target(stub.url), **settings})


def _embedding_cases() -> list[Any]:
    return [
        pytest.param(provider, setting, id=f"{provider}-{setting}")
        for provider, table in _EMBEDDING_EXPECTED.items()
        for setting in table
    ]


@pytest.mark.parametrize(("provider", "setting"), _embedding_cases())
def test_an_embedding_setting_reaches_the_request_or_is_refused_at_build(
    stub: _Stub, provider: str, setting: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if provider == "huggingface":
        # The local model's loader decides what the class carries; a recording stand-in for it.
        _install_sentence_transformers(monkeypatch)
    expected = _EMBEDDING_EXPECTED[provider][setting]
    value = _EMBEDDING_VALUES[setting]
    if expected is _REFUSED:
        pytest.importorskip(_EMBEDDING_PROVIDERS[provider][0])
        with pytest.raises(ValueError, match=rf"{provider}: .*'{setting}'"):
            _build_embedding(provider, stub, **{setting: value})
        assert stub.requests == []
        return
    _build_embedding(provider, stub, **{setting: value}).embed_query("hello")
    assert len(stub.requests) == 1
    seen = stub.requests[0]
    if expected is _IN_PATH:
        assert f"/{value}/" in seen.path
        return
    if expected is _HEADER:
        # The SDK may lead with its own product token; the configured agent is sent after it.
        assert seen.headers["user-agent"].endswith(value)
        return
    sent: Any = seen.body
    for key in expected:
        sent = sent[key]
    assert sent == _EMBEDDING_SENT_AS.get((provider, setting), value)


@pytest.mark.parametrize("provider", [name for name, spec in _EMBEDDING_PROVIDERS.items() if spec[2] is not None])
def test_the_embedding_connection_settings_reach_the_request(stub: _Stub, provider: str) -> None:
    _build_embedding(provider, stub).embed_query("hello")
    assert len(stub.requests) == 1
    header = _EMBEDDING_PROVIDERS[provider][2]
    assert header is not None
    assert stub.requests[0].headers[header[0]] == header[1]


# provider -> how the built embedding model exposes the request timeout its client applies.
_EMBEDDING_TIMEOUT_OF: dict[str, Any] = {
    "openai": lambda emb: emb.request_timeout,
    "mistral": lambda emb: emb.timeout,
    "ollama": lambda emb: emb.client_kwargs["timeout"],
}


@pytest.mark.parametrize("provider", list(_EMBEDDING_TIMEOUT_OF))
def test_the_embedding_timeout_is_applied_by_the_client(stub: _Stub, provider: str) -> None:
    emb = _build_embedding(provider, stub, timeout=9)
    assert _EMBEDDING_TIMEOUT_OF[provider](emb) == 9


class _RecordingSentenceTransformer:
    """Stands in for ``sentence_transformers.SentenceTransformer``: records how the model is loaded."""

    loads: ClassVar[list[dict[str, Any]]] = []

    def __init__(
        self,
        model_name_or_path: str | None = None,
        *,
        device: str | None = None,
        cache_folder: str | None = None,
        revision: str | None = None,
        token: bool | str | None = None,
        model_kwargs: dict[str, Any] | None = None,
    ) -> None:
        loaded = {"device": device, "revision": revision, "token": token, "model_kwargs": model_kwargs}
        load = {"name": model_name_or_path, "cache_folder": cache_folder}
        type(self).loads.append(load | {key: value for key, value in loaded.items() if value is not None})


def _install_sentence_transformers(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    pytest.importorskip("langchain_huggingface")
    import sys
    import types

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _RecordingSentenceTransformer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setattr(_RecordingSentenceTransformer, "loads", [])
    return _RecordingSentenceTransformer.loads


def test_the_huggingface_embedding_key_loads_the_model_as_the_hub_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import SecretStr

    from tai42_kit.llm.settings import EmbeddingSettings

    loads = _install_sentence_transformers(monkeypatch)
    embedding._cached_embedding.cache_clear()
    # The harness's real embedding leg: ``EMBEDDING_API_KEY`` carries the hub token.
    settings = EmbeddingSettings(model="org/private-model", api_key=SecretStr("hf-k"))
    embedding.get_embedding("huggingface", **settings.model_dump(exclude_none=True))
    embedding._cached_embedding.cache_clear()
    assert loads == [{"name": "org/private-model", "cache_folder": None, "token": "hf-k"}]


def test_the_huggingface_embedding_key_given_twice_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    loads = _install_sentence_transformers(monkeypatch)
    with pytest.raises(
        ValueError, match=r"huggingface: 'api_key' and \"model_kwargs\['token'\]\" are the same setting given twice"
    ):
        embedding._build_embedding("huggingface", model="m", api_key="k", model_kwargs={"token": "other"})
    assert loads == []


# -- a setting given twice ----------------------------------------------------------------------------

# (settings, the two names the refusal gives) for an Ollama credential or client option set two ways.
_OLLAMA_TWICE: list[Any] = [
    pytest.param({"base_url_user": True, "api_key": "k"}, "'api_key' and 'base_url'", id="key-in-url-and-api_key"),
    pytest.param(
        {"base_url_user": "schemeless", "api_key": "k"}, "'api_key' and 'base_url'", id="key-in-schemeless-url"
    ),
    pytest.param(
        {"api_key": "k", "client_kwargs": {"headers": {"Authorization": "Bearer other"}}},
        "'api_key' and \"client_kwargs\\['headers'\\]\\['Authorization'\\]\"",
        id="header-any-letter-case",
    ),
    pytest.param(
        {"api_key": "k", "sync_client_kwargs": {"headers": {"Authorization": "Bearer other"}}},
        "'api_key' and \"sync_client_kwargs\\['headers'\\]\\['Authorization'\\]\"",
        id="per-client-header",
    ),
    pytest.param(
        {"timeout": 9, "async_client_kwargs": {"timeout": 3}},
        "'timeout' and \"async_client_kwargs\\['timeout'\\]\"",
        id="per-client-timeout",
    ),
]


def _ollama_settings(stub: _Stub, settings: dict[str, Any]) -> dict[str, Any]:
    settings = dict(settings)
    user = settings.pop("base_url_user", False)
    if user == "schemeless":
        base = stub.url.replace("http://", "user:pass@")
    elif user:
        base = stub.url.replace("http://", "http://user:pass@")
    else:
        base = stub.url
    return {"base_url": base, **settings}


@pytest.mark.parametrize(("settings", "names"), _OLLAMA_TWICE)
def test_an_ollama_chat_option_set_twice_is_refused_naming_both(
    stub: _Stub, settings: dict[str, Any], names: str
) -> None:
    pytest.importorskip("langchain_ollama")
    with pytest.raises(ValueError, match=rf"ollama: {names} are the same setting given twice"):
        models._build_llm("ollama", model="m", **_ollama_settings(stub, settings))
    assert stub.requests == []


@pytest.mark.parametrize(("settings", "names"), _OLLAMA_TWICE)
def test_an_ollama_embedding_option_set_twice_is_refused_naming_both(
    stub: _Stub, settings: dict[str, Any], names: str
) -> None:
    pytest.importorskip("langchain_ollama")
    with pytest.raises(ValueError, match=rf"ollama: {names} are the same setting given twice"):
        embedding._build_embedding("ollama", model="m", **_ollama_settings(stub, settings))
    assert stub.requests == []


def test_a_mistral_embedding_base_url_given_twice_is_refused_naming_both(stub: _Stub) -> None:
    pytest.importorskip("langchain_mistralai")
    with pytest.raises(ValueError, match=r"mistral: 'base_url' and 'endpoint' are the same setting given twice"):
        embedding._build_embedding("mistral", model="m", api_key="k", base_url=stub.url, endpoint=stub.url)
    assert stub.requests == []


# -- a header given twice in another letter case --------------------------------------------------------

_HEADER_TWICE: list[Any] = [
    pytest.param("openai", "default_headers", id="openai-default_headers"),
    pytest.param("google", "additional_headers", id="google-additional_headers"),
]


@pytest.mark.parametrize(("provider", "container"), _HEADER_TWICE)
def test_an_embedding_user_agent_given_twice_in_any_letter_case_is_refused(
    stub: _Stub, provider: str, container: str
) -> None:
    pytest.importorskip(_EMBEDDING_PROVIDERS[provider][0])
    with pytest.raises(
        ValueError,
        match=rf"{provider}: 'user_agent' and \"{container}\['User-Agent'\]\" are the same setting given twice",
    ):
        _build_embedding(provider, stub, user_agent="ua-flat", **{container: {"User-Agent": "ua-other"}})
    assert stub.requests == []


def test_the_huggingface_embedding_key_and_a_flat_token_are_refused_naming_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads = _install_sentence_transformers(monkeypatch)
    with pytest.raises(ValueError, match=r"huggingface: 'api_key' and 'token' are the same setting given twice"):
        embedding._build_embedding("huggingface", model="m", api_key="k", token="other")
    assert loads == []


@pytest.mark.parametrize(
    ("settings", "place"),
    [
        pytest.param({"extra_headers": {"User-Agent": "ua-xh"}}, "extra_headers\\['User-Agent'\\]", id="flat"),
        pytest.param({"extra_headers": {"user-agent": "ua-xh"}}, "extra_headers\\['user-agent'\\]", id="lower"),
        pytest.param(
            {"model_kwargs": {"extra_headers": {"USER-AGENT": "ua-xh"}}},
            "model_kwargs\\['extra_headers'\\]\\['USER-AGENT'\\]",
            id="in-model_kwargs",
        ),
    ],
)
def test_an_openai_embedding_user_agent_also_in_extra_headers_is_refused(
    stub: _Stub, settings: dict[str, Any], place: str
) -> None:
    pytest.importorskip("langchain_openai")
    with pytest.raises(ValueError, match=rf"openai: 'user_agent' and \"{place}\" are the same setting given twice"):
        _build_embedding("openai", stub, user_agent="ua-flat", **settings)
    assert stub.requests == []


def test_an_unread_field_inside_model_kwargs_is_refused(stub: _Stub) -> None:
    pytest.importorskip("langchain_mistralai")
    with pytest.raises(ValueError, match=r"mistral: 'max_concurrent_requests' cannot reach the provider"):
        _build("mistral", stub, model_kwargs={"max_concurrent_requests": 2})
    assert stub.requests == []
