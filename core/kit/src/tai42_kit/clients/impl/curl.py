import anyio
from curl_cffi import requests
from curl_cffi.requests.exceptions import SessionClosed

from tai42_kit.clients.base import PooledClient, is_loop_bound_runtime_error, reject_unknown_connection_kwargs

_ALLOWED_KWARGS = frozenset({"session_params"})


class CurlClient(PooledClient[requests.AsyncSession]):
    """Manages reusable curl_cffi sessions.

    Every primitive in ``session_params`` maps onto the ``AsyncSession``, giving
    full control over the network layer.
    """

    async def _create(self, **kwargs) -> requests.AsyncSession:
        reject_unknown_connection_kwargs("Curl client", kwargs, _ALLOWED_KWARGS)
        # Copy before mutating — never alter the caller's dict.
        session_params = {k: v for k, v in kwargs.get("session_params", {}).items() if k != "session_key"}
        return requests.AsyncSession(**session_params)

    async def _close(self, client: requests.AsyncSession):
        if client:
            await client.close()

    def _disconnection_exceptions(self) -> tuple[type[Exception], ...]:
        return (
            # The async resource is dead (socket / loop closed)
            anyio.ClosedResourceError,
            anyio.BrokenResourceError,
            # The curl_cffi session object is explicitly closed / invalid
            SessionClosed,
        )

    def _is_disconnection_error(self, exc: BaseException) -> bool:
        # curl_cffi/anyio surface a dead loop-bound session as a plain
        # RuntimeError ("Event loop is closed" / "... attached to a different
        # loop"); match those by message so an unrelated RuntimeError raised by
        # caller code never tears down the pool.
        return super()._is_disconnection_error(exc) or is_loop_bound_runtime_error(exc)
