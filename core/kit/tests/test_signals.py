"""The composed signal chain: one asyncio handler per signal, many subscribers.

The behaviors under test are the ones that make composition safe where raw
``add_signal_handler`` is not — LIFO delivery of every subscriber, isolation of a
raising one, and an uninstall that waits for the LAST subscriber to leave rather
than stripping the signal from the ones that stayed.

Signals are delivered by calling the chain's dispatcher directly: the real
kernel path adds nothing to test here and would race the test runner.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import threading

import pytest

from tai42_kit.signals import SignalChain, signal_chain


def _sigterm_installed() -> bool:
    """Whether an asyncio SIGTERM handler is live in this process. ``asyncio``
    installs a C-level trampoline on add and restores ``SIG_DFL`` on remove."""
    return signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL


async def test_every_subscriber_fires_innermost_first() -> None:
    chain = SignalChain()
    fired: list[str] = []
    outer = chain.add(signal.SIGTERM, lambda: fired.append("outer"), name="outer")
    inner = chain.add(signal.SIGTERM, lambda: fired.append("inner"), name="inner")
    try:
        chain._dispatch(signal.SIGTERM)
    finally:
        inner.cancel()
        outer.cancel()

    # LIFO: the most recent subscriber is the innermost scope, so it stops first.
    assert fired == ["inner", "outer"]


async def test_a_raising_subscriber_does_not_swallow_the_rest(caplog: pytest.LogCaptureFixture) -> None:
    chain = SignalChain()
    fired: list[str] = []

    def _boom() -> None:
        raise RuntimeError("subscriber exploded")

    outer = chain.add(signal.SIGTERM, lambda: fired.append("outer"), name="cancel-main-task")
    inner = chain.add(signal.SIGTERM, _boom, name="drain")
    try:
        with caplog.at_level(logging.ERROR, logger="tai42_kit.signals"):
            chain._dispatch(signal.SIGTERM)
    finally:
        inner.cancel()
        outer.cancel()

    # The launcher's teardown-guaranteeing subscriber still ran, and the failure
    # is reported rather than silently absorbed.
    assert fired == ["outer"]
    assert "'drain' raised on SIGTERM" in caplog.text
    assert "subscriber exploded" in caplog.text


async def test_cancelling_one_of_two_keeps_the_handler_installed() -> None:
    chain = SignalChain()
    first = chain.add(signal.SIGTERM, lambda: None, name="first")
    second = chain.add(signal.SIGTERM, lambda: None, name="second")
    try:
        second.cancel()

        assert _sigterm_installed() is True
        assert chain.subscribers(signal.SIGTERM) == ("first",)
    finally:
        first.cancel()


async def test_cancelling_the_last_subscriber_uninstalls_the_handler() -> None:
    chain = SignalChain()
    sub = chain.add(signal.SIGTERM, lambda: None, name="only")
    assert _sigterm_installed() is True

    sub.cancel()

    assert _sigterm_installed() is False
    assert chain.subscribers(signal.SIGTERM) == ()


async def test_one_handler_serves_every_subscriber() -> None:
    # Two subscribers, ONE asyncio install: the second must not overwrite the
    # first, which is what raw add_signal_handler would do.
    loop = asyncio.get_running_loop()
    installs: list[tuple[object, ...]] = []
    real_add = loop.add_signal_handler

    def _record(sig, callback, *args):
        installs.append((sig, callback, *args))
        real_add(sig, callback, *args)

    chain = SignalChain()
    loop.add_signal_handler = _record  # pyright: ignore[reportAttributeAccessIssue]
    try:
        first = chain.add(signal.SIGTERM, lambda: None, name="first")
        second = chain.add(signal.SIGTERM, lambda: None, name="second")
    finally:
        loop.add_signal_handler = real_add  # pyright: ignore[reportAttributeAccessIssue]
    second.cancel()
    first.cancel()

    # ONE install, and it hands the KERNEL the chain's own dispatcher bound to the
    # signal — the arrow from a real delivery to the subscriber walk. Registering
    # anything else here would leave every test below passing while no signal ever
    # reached a subscriber.
    assert installs == [(signal.SIGTERM, chain._dispatch, signal.SIGTERM)]


async def test_cancel_is_idempotent() -> None:
    chain = SignalChain()
    sub = chain.add(signal.SIGTERM, lambda: None, name="only")

    sub.cancel()
    sub.cancel()

    assert _sigterm_installed() is False


async def test_dispatch_tolerates_a_subscriber_that_cancels_itself() -> None:
    chain = SignalChain()
    fired: list[str] = []
    outer = chain.add(signal.SIGTERM, lambda: fired.append("outer"), name="outer")

    def _leave() -> None:
        fired.append("inner")
        inner.cancel()

    inner = chain.add(signal.SIGTERM, _leave, name="inner")
    try:
        chain._dispatch(signal.SIGTERM)
        remaining = chain.subscribers(signal.SIGTERM)
    finally:
        outer.cancel()

    # The walk runs over a snapshot, so the mutation cannot skip the outer one.
    assert fired == ["inner", "outer"]
    assert remaining == ("outer",)


async def test_add_propagates_an_install_failure() -> None:
    # A component that believes it is signal-wired and is not must find out here.
    loop = asyncio.get_running_loop()
    real_add = loop.add_signal_handler

    def _refuse(sig, callback, *args):
        raise NotImplementedError("signal handling unsupported here")

    chain = SignalChain()
    loop.add_signal_handler = _refuse  # pyright: ignore[reportAttributeAccessIssue]
    try:
        with pytest.raises(NotImplementedError, match="unsupported"):
            chain.add(signal.SIGTERM, lambda: None, name="drain")
    finally:
        loop.add_signal_handler = real_add  # pyright: ignore[reportAttributeAccessIssue]

    assert chain.subscribers(signal.SIGTERM) == ()


async def test_a_refused_install_leaves_the_chain_unbound() -> None:
    # The refused add must leave NO half-state: an entry for the signal would read
    # as "already owned" and make the retry skip the install it still needs, and a
    # recorded loop would make the retry look like a second-loop collision.
    loop = asyncio.get_running_loop()
    real_add = loop.add_signal_handler
    refusals = {"remaining": 1}

    def _refuse_once(sig, callback, *args):
        if refusals["remaining"]:
            refusals["remaining"] -= 1
            raise RuntimeError("add_signal_handler() can only be called from the main thread")
        real_add(sig, callback, *args)

    chain = SignalChain()
    loop.add_signal_handler = _refuse_once  # pyright: ignore[reportAttributeAccessIssue]
    try:
        with pytest.raises(RuntimeError, match="main thread"):
            chain.add(signal.SIGTERM, lambda: None, name="drain")

        assert chain._subs == {}
        assert chain._loop is None

        # The retry installs for real rather than joining a phantom entry.
        sub = chain.add(signal.SIGTERM, lambda: None, name="drain")
    finally:
        loop.add_signal_handler = real_add  # pyright: ignore[reportAttributeAccessIssue]

    assert _sigterm_installed() is True
    sub.cancel()
    assert _sigterm_installed() is False


def test_add_without_a_running_loop_raises() -> None:
    with pytest.raises(RuntimeError, match="no running event loop"):
        SignalChain().add(signal.SIGTERM, lambda: None, name="drain")


async def test_add_from_a_second_live_loop_raises() -> None:
    # Two running loops would each need their own handler for the same signal —
    # the collision the chain exists to prevent, so it is refused loudly.
    chain = SignalChain()
    sub = chain.add(signal.SIGTERM, lambda: None, name="first")
    raised: list[BaseException] = []

    def _other_loop() -> None:
        async def _add() -> None:
            chain.add(signal.SIGTERM, lambda: None, name="second")

        try:
            asyncio.run(_add())
        except BaseException as exc:
            raised.append(exc)

    thread = threading.Thread(target=_other_loop, name="second-loop")
    thread.start()
    try:
        await asyncio.to_thread(thread.join)
    finally:
        sub.cancel()

    assert len(raised) == 1
    assert isinstance(raised[0], RuntimeError)
    assert "different running event loop" in str(raised[0])


def test_a_dead_loop_is_dropped_for_the_next_one() -> None:
    # A loop that stopped can never deliver to its subscribers again, so its table
    # is stale rather than a conflict: the next loop starts clean.
    chain = SignalChain()
    stale: list[object] = []

    async def _first() -> None:
        stale.append(chain.add(signal.SIGTERM, lambda: None, name="first"))

    async def _second() -> None:
        sub = chain.add(signal.SIGTERM, lambda: None, name="second")
        # The first loop's subscriber is gone rather than still queued ahead.
        assert chain.subscribers(signal.SIGTERM) == ("second",)
        sub.cancel()

    asyncio.run(_first())
    asyncio.run(_second())

    # Cancelling the stranded subscription is a no-op — its table is long gone.
    stale[0].cancel()  # pyright: ignore[reportAttributeAccessIssue]
    assert _sigterm_installed() is False


def test_cancelling_after_the_loop_closed_does_not_reach_it() -> None:
    # ``asyncio``'s own close() already uninstalled the handler; touching the
    # closed loop from a late teardown would raise instead of unwinding.
    chain = SignalChain()
    held: list[object] = []

    async def _subscribe() -> None:
        held.append(chain.add(signal.SIGTERM, lambda: None, name="only"))

    asyncio.run(_subscribe())

    held[0].cancel()  # pyright: ignore[reportAttributeAccessIssue]
    assert chain.subscribers(signal.SIGTERM) == ()


async def test_two_signals_are_wired_independently() -> None:
    chain = SignalChain()
    fired: list[str] = []
    term = chain.add(signal.SIGTERM, lambda: fired.append("term"), name="term")
    intr = chain.add(signal.SIGINT, lambda: fired.append("int"), name="int")
    try:
        chain._dispatch(signal.SIGINT)

        assert fired == ["int"]
        assert chain.subscribers(signal.SIGTERM) == ("term",)
    finally:
        intr.cancel()
        term.cancel()


async def test_subscription_repr_names_the_subscriber_and_signal() -> None:
    chain = SignalChain()
    sub = chain.add(signal.SIGTERM, lambda: None, name="drain")
    try:
        assert repr(sub) == "<SignalSubscription 'drain' on SIGTERM>"
    finally:
        sub.cancel()


def test_the_module_singleton_is_the_process_owner() -> None:
    assert isinstance(signal_chain, SignalChain)
