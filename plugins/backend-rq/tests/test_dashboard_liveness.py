"""How the rq dashboard signals it is alive.

The dashboard is an HTTP process: an orchestrator probes ``/favicon.ico``, which the dashboard
serves without reading Redis.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

from tai42_contract.backend.runtime import HttpLiveness

from tai42_backend_rq.backend import RqDashboardRuntime


def test_the_dashboard_declares_http_liveness_on_favicon() -> None:
    assert RqDashboardRuntime.liveness == HttpLiveness("/favicon.ico")


def _favicon_status(username: str | None, password: str | None) -> int:
    """The status ``GET /favicon.ico`` gets from the dashboard app the dashboard CLI builds.

    Run in a child interpreter: rq-dashboard adds basic auth to its module-level blueprint, and
    Flask refuses a hook on a blueprint already registered, so each app needs a fresh import.
    The Redis URL points at a port nothing listens on, so a 200 shows the route reads no Redis.
    """
    script = textwrap.dedent(
        f"""
        from rq_dashboard.cli import make_flask_app
        from rq_dashboard.web import setup_rq_connection

        app = make_flask_app(None, {username!r}, {password!r}, "")
        app.config["RQ_DASHBOARD_REDIS_URL"] = "redis://127.0.0.1:1/0"
        setup_rq_connection(app)
        print(app.test_client().get("/favicon.ico").status_code)
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=60)
    return int(result.stdout.strip())


def test_the_dashboard_serves_its_probe_path_without_redis() -> None:
    assert _favicon_status(None, None) == 200


def test_the_dashboard_probe_path_asks_for_basic_auth_when_a_username_is_set() -> None:
    assert _favicon_status("u", "p") == 401
