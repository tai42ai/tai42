"""How Flower signals it is alive.

Flower is an HTTP process: an orchestrator probes ``/healthcheck``, which Flower answers
without authentication.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

from tai42_contract.backend.runtime import HttpLiveness

from tai42_backend_celery.core.backend import CeleryFlowerRuntime


def test_flower_declares_http_liveness_on_healthcheck() -> None:
    assert CeleryFlowerRuntime.liveness == HttpLiveness("/healthcheck")


def test_flower_maps_healthcheck_to_its_monitor_handler() -> None:
    from flower import urls
    from flower.views import monitor

    assert (r"/healthcheck", monitor.Healthcheck) in urls.handlers


def test_flower_answers_healthcheck_without_auth_while_basic_auth_guards_its_pages() -> None:
    """The vendor contract the probe path relies on, from a real Flower app with basic auth on.

    Run in a child interpreter: Flower reads its options from the process-wide tornado option
    set, and setting basic auth there must not reach other tests.
    """
    script = textwrap.dedent(
        """
        import asyncio

        import celery
        from flower.app import Flower
        from flower.options import default_options
        from tornado.httpclient import AsyncHTTPClient
        from tornado.httpserver import HTTPServer
        from tornado.ioloop import IOLoop
        from tornado.testing import bind_unused_port


        async def main():
            default_options.basic_auth = ["u:p"]
            app = Flower(options=default_options, capp=celery.Celery(broker="memory://"), io_loop=IOLoop.current())
            sock, port = bind_unused_port()
            server = HTTPServer(app)
            server.add_sockets([sock])
            client = AsyncHTTPClient()
            for path in ("/healthcheck", "/"):
                response = await client.fetch(f"http://127.0.0.1:{port}{path}", raise_error=False)
                print(path, response.code, response.body.decode())
            server.stop()


        asyncio.run(main())
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=60)

    assert result.stdout.splitlines() == ["/healthcheck 200 OK", "/ 401 Access denied"]
