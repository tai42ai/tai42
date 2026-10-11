"""The huggingface provider builds from the kit's flat chat-model settings, against the real classes.

``LLMSettings`` carries a hosted model's shape (model id, token, base URL, timeout, sampling), so the
huggingface branch builds a hosted Inference endpoint wrapped in the chat model. These tests need the
``huggingface`` extra (``langchain-huggingface``) and skip visibly without it.
"""

from typing import Any

import pytest
from pydantic import SecretStr

pytest.importorskip("langgraph")
pytest.importorskip("langchain_huggingface")

import langchain_huggingface  # pyright: ignore[reportMissingImports]

from tai42_kit.llm import models
from tai42_kit.llm.settings import LLMSettings

# langchain-huggingface 1.2.2's ``ChatHuggingFace`` validation probes for langchain-community's
# ``HuggingFaceHub`` (``_is_huggingface_hub`` imports ``langchain_community.llms.huggingface_hub``);
# where langchain-community 0.4.2 is installed, its import raises its sunset ``DeprecationWarning``.
# Exactly that warning is ignored here; every other warning stays an error.
pytestmark = pytest.mark.filterwarnings(
    "ignore:`langchain-community` is being sunset and is no longer actively maintained:DeprecationWarning"
)


def _chat(**kwargs: Any) -> Any:
    """The huggingface chat model built through the kit (typed ``Any``: the extra's classes are optional)."""
    return models.get_llm("huggingface", **kwargs)


@pytest.fixture(autouse=True)
def _fresh_cache() -> Any:
    models._cached_llm.cache_clear()
    yield
    models._cached_llm.cache_clear()


def test_flat_settings_build_a_chat_model_over_a_hosted_endpoint() -> None:
    settings = LLMSettings(
        model="HuggingFaceH4/zephyr-7b-beta",
        api_key=SecretStr("hf-test-token"),
        max_tokens=64,
        temperature=0.2,
        top_p=0.9,
        timeout=30,
    )
    llm = _chat(**settings.model_dump(exclude_none=True))
    assert isinstance(llm, langchain_huggingface.ChatHuggingFace)
    assert llm.model_id == "HuggingFaceH4/zephyr-7b-beta"
    endpoint = llm.llm
    assert isinstance(endpoint, langchain_huggingface.HuggingFaceEndpoint)
    assert endpoint.repo_id == "HuggingFaceH4/zephyr-7b-beta"
    assert endpoint.endpoint_url is None
    assert endpoint.huggingfacehub_api_token == "hf-test-token"
    assert endpoint.timeout == 30
    # The generation settings travel in the chat model's own call parameters (what each
    # chat-completion request carries is asserted against a stub in
    # test_provider_settings_reach_the_request.py).
    assert llm.max_tokens == 64
    assert llm.model_kwargs == {"temperature": 0.2, "top_p": 0.9}


def test_a_base_url_selects_the_server_at_that_url() -> None:
    llm = _chat(model="my-org/served-model", base_url="http://127.0.0.1:8080")
    endpoint = llm.llm
    assert endpoint.endpoint_url == "http://127.0.0.1:8080"
    assert endpoint.repo_id is None
    assert llm.model_id == "http://127.0.0.1:8080"


def test_a_setting_the_chat_completion_does_not_take_is_refused_naming_it() -> None:
    with pytest.raises(ValueError, match=r"huggingface: 'repetition_penalty', 'top_k' cannot reach the provider"):
        _chat(model="m/x", top_k=2, repetition_penalty=1.1)
