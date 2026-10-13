"""Whether the REAL configured provider accepts a history that opens with an assistant turn (creds host only).

``requires_leading_user_turn(provider)`` records, per hosted provider, whether the provider refuses a
request whose first non-system message is an assistant turn. No provider package decides it before
sending, so the provider's own answer is the proof: on the real ``llm`` leg
(``REAL_E2E_LLM_PROVIDER`` / ``REAL_E2E_LLM_MODEL``) this sends that history BARE and checks the
outcome matches the recorded value, and sends the same history led by ``lead_with_user`` and checks
the provider accepts it. A mismatch is a red: the recorded value is then corrected. The harness
selects only hosted providers (``tai42_e2e.settings.LLM_PROVIDERS``). Skipped on the mock leg; it
never runs in CI.
"""

from __future__ import annotations

import os

import httpx
import pytest
from langchain_core.exceptions import ModelInvalidRequestError
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from pydantic import SecretStr
from tai42_kit.llm import get_llm
from tai42_kit.llm.middleware.leading_user import lead_with_user
from tai42_kit.llm.models import requires_leading_user_turn

from tai42_e2e.settings import HarnessSettings, real_llm_provider

# One event loop for the module. langchain-anthropic and langchain-openai hand every chat model one
# process-wide pooled HTTP client, and google-genai keeps one aiohttp session per client per event
# loop; either way a keep-alive connection belongs to the loop that opened it. On one loop the
# second request reuses the first's connection instead of orphaning it on a closed loop.
#
# google-genai 2.25.0 (and every release up to 2.29.0) opens its aiohttp session by subclassing
# ``aiohttp.ClientSession`` (``class AiohttpClientSession`` in ``google/genai/_api_client.py``),
# which aiohttp deprecates with a ``DeprecationWarning`` raised from that module. The filter below
# ignores exactly that warning from exactly that module; every other warning stays an error.
pytestmark = [
    pytest.mark.backendless,
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.filterwarnings(
        "ignore:Inheritance class AiohttpClientSession from ClientSession is discouraged"
        ":DeprecationWarning:google.genai._api_client"
    ),
    pytest.mark.skipif(
        not HarnessSettings().is_real("llm"),
        reason="real-provider history-order leg; runs on the e2e creds host, not the mock CI leg",
    ),
]

_ASSISTANT_FIRST: list[AnyMessage] = [SystemMessage("s"), AIMessage("opener"), HumanMessage("hi")]


def _model():  # type: ignore[no-untyped-def]
    provider = real_llm_provider(os.environ)
    model = os.environ.get("REAL_E2E_LLM_MODEL", provider.default_llm_model)
    llm = get_llm(provider.provider, model=model, api_key=SecretStr(os.environ[provider.api_key_env]))
    return provider.provider, llm


def _assert_invalid_request(error: BaseException) -> None:
    """The provider answered the request with an invalid-request refusal (HTTP 400).

    The LangChain provider packages raise ``ModelInvalidRequestError`` for it; langchain-mistralai
    raises ``httpx.HTTPStatusError`` carrying the response, so its status is checked here.
    """
    if isinstance(error, httpx.HTTPStatusError):
        assert error.response.status_code == httpx.codes.BAD_REQUEST, error


async def test_the_led_history_is_accepted() -> None:
    _provider, llm = _model()
    led = lead_with_user(list(_ASSISTANT_FIRST))
    assert led is not None
    answer = await llm.ainvoke(led)
    assert isinstance(answer, AIMessage)


async def test_the_bare_assistant_first_history_matches_the_recorded_value() -> None:
    provider, llm = _model()
    if requires_leading_user_turn(provider):
        # Recorded True: the provider refuses the bare history as an invalid request. Accepted here
        # means record False; any other failure (auth, network, a client-side error) is not a refusal.
        with pytest.raises((ModelInvalidRequestError, httpx.HTTPStatusError)) as refused:
            await llm.ainvoke(list(_ASSISTANT_FIRST))
        _assert_invalid_request(refused.value)
    else:
        # Recorded False: the provider accepts the bare history. Refused here means record True.
        answer = await llm.ainvoke(list(_ASSISTANT_FIRST))
        assert isinstance(answer, AIMessage)
