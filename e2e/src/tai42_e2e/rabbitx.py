"""Harness-side RabbitMQ management client: reachability probe plus per-stack
vhost create/delete over the management HTTP API.

The celery backend variant isolates each stack on its own AMQP vhost, the broker
counterpart of the per-stack Postgres database clone and Redis logical DB. This
client owns that isolation; it never speaks AMQP itself (the SUT's celery app
does), only the management API that provisions and reaps the vhost.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlsplit

import httpx


class RabbitAdmin:
    """A synchronous management-API client for one RabbitMQ broker. Provisions
    and reaps the per-stack vhosts the celery variant leases."""

    def __init__(self, management_url: str, *, timeout: float) -> None:
        """``management_url`` carries the credentials inline
        (``http://tai42:tai42@host:15672``); they are split out into HTTP basic
        auth and the base URL is rebuilt without the userinfo; a URL without a
        user and a password is refused.

        ``timeout`` bounds each management call, in seconds. The callers reuse
        the harness's readiness deadline (``HarnessSettings.boot_timeout``) for
        it; that deadline is sized for the serve stack's imports at boot, not
        for the broker. The reuse suffices because the bound only has to exceed
        the broker's service time: the broker serves a delete synchronously (it
        deletes the vhost's queues and exchanges and stops its message stores
        before it answers) and aborts a delete whose client hung up, leaving the
        vhost half-deleted, so a bound below the service time is worse than
        slow, while a long bound only delays the loud failure on a hung broker.
        The broker's measured delete time under a loaded host stays below a
        second, far inside that deadline."""
        parsed = urlparse(management_url)
        if parsed.hostname is None or parsed.port is None:
            raise ValueError(f"rabbitmq_management_url must include host and port: {management_url!r}")
        if not parsed.username or not parsed.password:
            raise ValueError("rabbitmq_management_url must include a user and a password")
        self._base = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        self.user = parsed.username
        self._auth = httpx.BasicAuth(parsed.username, parsed.password)
        self._timeout = timeout

    def check_reachable(self) -> None:
        """Hit the management overview so an absent or unreachable broker fails
        loudly at session start rather than cryptically at first dispatch."""
        resp = httpx.get(f"{self._base}/api/overview", auth=self._auth, timeout=self._timeout)
        resp.raise_for_status()

    def vhost_exists(self, name: str) -> bool:
        """Whether the named vhost is present on the broker. A 404 means absent;
        any other non-2xx is a real management-API failure and raises loudly."""
        resp = httpx.get(f"{self._base}/api/vhosts/{name}", auth=self._auth, timeout=self._timeout)
        if resp.status_code == httpx.codes.NOT_FOUND:
            return False
        resp.raise_for_status()
        return True

    def create_vhost(self, name: str, *, user: str) -> None:
        """Create the vhost and grant ``user`` — the user the SUT's celery app
        connects as — full permissions on it (configure/write/read ``.*``) so the
        app can declare its control exchange and queues. If the permissions grant fails after the
        vhost is created, the half-created vhost is deleted before propagating so
        a partial failure leaves no orphan behind — symmetric to the leak-free
        teardown contract."""
        put = httpx.put(f"{self._base}/api/vhosts/{name}", auth=self._auth, timeout=self._timeout)
        put.raise_for_status()
        try:
            perms = httpx.put(
                f"{self._base}/api/permissions/{name}/{user}",
                auth=self._auth,
                json={"configure": ".*", "write": ".*", "read": ".*"},
                timeout=self._timeout,
            )
            perms.raise_for_status()
        except Exception:
            self.delete_vhost(name)
            raise

    def delete_vhost(self, name: str) -> None:
        """Delete the vhost and confirm it is gone. A vhost still present after
        the delete is a broker-side leak — raised loudly, symmetric to a leaked
        Postgres database in stack teardown."""
        try:
            resp = httpx.delete(f"{self._base}/api/vhosts/{name}", auth=self._auth, timeout=self._timeout)
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"vhost {name!r}: the broker did not answer its delete within {self._timeout}s; RabbitMQ "
                "aborts a delete whose client hung up, so the vhost is left half-deleted (leak)"
            ) from exc
        # 404 means it was already gone; any other non-2xx is a real failure.
        if resp.status_code != httpx.codes.NOT_FOUND:
            resp.raise_for_status()
        check = httpx.get(f"{self._base}/api/vhosts/{name}", auth=self._auth, timeout=self._timeout)
        if check.status_code != httpx.codes.NOT_FOUND:
            raise RuntimeError(f"vhost {name!r} still present after delete (leak): HTTP {check.status_code}")


def broker_url_for(base_amqp_url: str, vhost: str) -> str:
    """The per-stack AMQP URL: ``base_amqp_url`` with its vhost path replaced by
    ``vhost`` (e.g. ``amqp://tai42:tai42@127.0.0.1:5672`` + ``tai42_e2e_ab12cd`` ->
    ``amqp://tai42:tai42@127.0.0.1:5672/tai42_e2e_ab12cd``)."""
    split = urlsplit(base_amqp_url)
    return f"{split.scheme}://{split.netloc}/{vhost}"


def broker_user(amqp_url: str) -> str:
    """The user ``amqp_url`` connects as, the one a per-stack vhost is granted to;
    a URL without a user is refused."""
    user = urlsplit(amqp_url).username
    if not user:
        raise ValueError("rabbitmq_url must include a user")
    return user
