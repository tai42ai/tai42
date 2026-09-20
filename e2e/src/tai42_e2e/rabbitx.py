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

    def __init__(self, management_url: str) -> None:
        """``management_url`` carries the credentials inline
        (``http://guest:guest@host:15672``); they are split out into HTTP basic
        auth and the base URL is rebuilt without the userinfo."""
        parsed = urlparse(management_url)
        if parsed.hostname is None or parsed.port is None:
            raise ValueError(f"rabbitmq_management_url must include host and port: {management_url!r}")
        username = parsed.username or "guest"
        password = parsed.password or "guest"
        self._base = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        self._auth = httpx.BasicAuth(username, password)

    def check_reachable(self) -> None:
        """Hit the management overview so an absent or unreachable broker fails
        loudly at session start rather than cryptically at first dispatch."""
        resp = httpx.get(f"{self._base}/api/overview", auth=self._auth, timeout=5.0)
        resp.raise_for_status()

    def vhost_exists(self, name: str) -> bool:
        """Whether the named vhost is present on the broker. A 404 means absent;
        any other non-2xx is a real management-API failure and raises loudly."""
        resp = httpx.get(f"{self._base}/api/vhosts/{name}", auth=self._auth, timeout=5.0)
        if resp.status_code == httpx.codes.NOT_FOUND:
            return False
        resp.raise_for_status()
        return True

    def create_vhost(self, name: str) -> None:
        """Create the vhost and grant the ``guest`` user full permissions on it
        (configure/write/read ``.*``) so the SUT's celery app can declare its
        control exchange and queues. If the permissions grant fails after the
        vhost is created, the half-created vhost is deleted before propagating so
        a partial failure leaves no orphan behind — symmetric to the leak-free
        teardown contract."""
        put = httpx.put(f"{self._base}/api/vhosts/{name}", auth=self._auth, timeout=5.0)
        put.raise_for_status()
        try:
            perms = httpx.put(
                f"{self._base}/api/permissions/{name}/guest",
                auth=self._auth,
                json={"configure": ".*", "write": ".*", "read": ".*"},
                timeout=5.0,
            )
            perms.raise_for_status()
        except Exception:
            self.delete_vhost(name)
            raise

    def delete_vhost(self, name: str) -> None:
        """Delete the vhost and confirm it is gone. A vhost still present after
        the delete is a broker-side leak — raised loudly, symmetric to a leaked
        Postgres database in stack teardown."""
        resp = httpx.delete(f"{self._base}/api/vhosts/{name}", auth=self._auth, timeout=5.0)
        # 404 means it was already gone; any other non-2xx is a real failure.
        if resp.status_code != httpx.codes.NOT_FOUND:
            resp.raise_for_status()
        check = httpx.get(f"{self._base}/api/vhosts/{name}", auth=self._auth, timeout=5.0)
        if check.status_code != httpx.codes.NOT_FOUND:
            raise RuntimeError(f"vhost {name!r} still present after delete (leak): HTTP {check.status_code}")


def broker_url_for(base_amqp_url: str, vhost: str) -> str:
    """The per-stack AMQP URL: ``base_amqp_url`` with its vhost path replaced by
    ``vhost`` (e.g. ``amqp://guest:guest@127.0.0.1:5672`` + ``tai42_e2e_ab12cd`` ->
    ``amqp://guest:guest@127.0.0.1:5672/tai42_e2e_ab12cd``)."""
    split = urlsplit(base_amqp_url)
    return f"{split.scheme}://{split.netloc}/{vhost}"
