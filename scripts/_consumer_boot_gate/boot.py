"""The boot runtime: the shared infra coordinates, the serve manifest + child
env a consumer boots under, and booting one consumer to health."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from _consumer_boot_gate.boot_failure import BootFailure, parse_boot_failure
from _consumer_boot_gate.consumers import Consumer
from _consumer_boot_gate.provides import Provides
from _consumer_boot_gate.versioning import _fail

_DEFAULT_IDENTITY_PACKAGE = "tai42-identity-redis"
_IDENTITY_PROVIDER_NAME = "redis"
_IDENTITY_LIFECYCLE_MODULE = "tai42_identity_redis"

_HEALTH_DEADLINE_S = 90.0


def _db_binding_env(provides: Provides) -> dict[str, str]:
    """Pin a store-backed consumer's migration component to the default database so
    ``tai db migrate`` applies its chain and its boot-time schema gate passes (an
    unbound component is skipped by the migrator and then fails the gate at boot)."""
    if not provides.db_component:
        return {}
    slug = re.sub(r"[^A-Z0-9]", "_", provides.db_component.upper())
    return {f"TAI_DB_BINDING_{slug}": "default"}


def auth_providers(provides: Provides) -> list[str]:
    """The access-control auth-provider chain for a boot: the gate's own identity
    provider plus every provider the consumer registers (so the accounts-provider
    boot check passes for an accounts/identity plugin)."""
    return [_IDENTITY_PROVIDER_NAME, *dict.fromkeys(provides.providers)]


# The platform routers a served app always mounts, plus the access-control doors
# (api_keys/login) the on posture needs. Kept minimal and generic: the health and
# metrics probes, the tool/config surface, and the auth doors.
_CORE_ROUTERS = (
    "tai42_skeleton.routers.health",
    "tai42_skeleton.routers.metrics",
    "tai42_skeleton.routers.tools",
    "tai42_skeleton.routers.config",
    "tai42_skeleton.routers.api_keys",
    "tai42_skeleton.routers.login",
)


def build_manifest(provides: Provides) -> dict:
    """The serve manifest for one consumer: the core routers plus the consumer's
    declared surface — the additive modules (routers, tools, channels, extensions,
    lifecycle) AND the exclusive slots it selects (``backend_module`` /
    ``storage_module`` / ``sandbox_module`` / ``monitoring_module`` / ``agents``), so
    the plugin's module is imported and registers. The identity provider's lifecycle
    module is always present so access control resolves a provider."""
    lifecycle = [_IDENTITY_LIFECYCLE_MODULE, *provides.lifecycle]
    routers = [*_CORE_ROUTERS, *provides.routers]
    manifest: dict = {
        "default_routers": "none",
        "lifecycle_modules": lifecycle,
        "routers_modules": routers,
        "api_tools": {"enabled": False},
    }
    if provides.tools:
        manifest["tools"] = [{"title": module, "module": module} for module in provides.tools]
    if provides.channels:
        manifest["channel_modules"] = [module for _name, module in provides.channels]
    if provides.extensions:
        manifest["extensions_modules"] = provides.extensions
    if provides.backend_module:
        manifest["backend_module"] = provides.backend_module
    if provides.storage_module:
        manifest["storage_module"] = provides.storage_module
    if provides.sandbox_module:
        manifest["sandbox_module"] = provides.sandbox_module
    if provides.monitoring_module:
        manifest["monitoring_module"] = provides.monitoring_module
    if provides.agents:
        manifest["agents"] = [{"title": name, "module": module, "include": [name]} for name, module in provides.agents]
    return manifest


# The minimal placeholder config a consumer whose provider is BUILT EAGERLY at
# registration (a monitoring backend's ``register`` calls its factory, which reads
# its credentials at boot) needs to import and register. The values satisfy the
# provider's settings validation without reaching a live service — its client
# connects lazily on first use, never at boot — so health-ready proves the module
# imported and its registration ran. Keyed by distribution name; a provider that
# reads its settings lazily (storage/sandbox/backend register a class whose config
# is read at call time) needs no entry.
_BOOT_PLACEHOLDER_ENV: dict[str, dict[str, str]] = {
    "tai42-monitoring-langfuse": {
        "LANGFUSE_HOST": "http://127.0.0.1:1",
        "LANGFUSE_PUBLIC_KEY": "pk-boot-gate",
        "LANGFUSE_SECRET_KEY": "sk-boot-gate",
    },
}


def _slot_env(provides: Provides, dist_name: str, infra: Infra) -> dict[str, str]:
    """The env an exclusive-slot consumer's boot needs beyond the base: a registered
    task backend refuses to boot without the worker bus (the backend-runtime and
    server processes must converge on config reloads), so its queue Redis — the CI
    job's — is pinned; a provider built eagerly at registration gets its documented
    placeholder config."""
    env: dict[str, str] = {}
    if provides.backend_module:
        env["TAI_BUS_REDIS_URL"] = infra.redis_url
    env.update(_BOOT_PLACEHOLDER_ENV.get(dist_name, {}))
    return env


def _channel_env(provides: Provides, redis_url: str) -> dict[str, str]:
    """Each mounted channel binds its own Redis (``CHANNEL_<NAME>_REDIS_URL``) — a
    channel refuses to register its inbound doors without one, so a channel consumer
    could never reach the route-registration surface the gate exercises."""
    return {f"CHANNEL_{name.upper()}_REDIS_URL": redis_url for name, _module in provides.channels if name}


def _external_service_env(dist_name: str, blackhole_url: str) -> dict[str, str]:
    """The deployment config a network-declaring consumer needs to boot far enough to
    reach its external service, with every OUTBOUND endpoint pointed at ``blackhole_url``
    (a closed loopback port the gate allocates).

    A deployment supplies these credentials; the gate has no live messaging API or
    identity provider to point them at, so the endpoint is a black hole by design. The
    boot then exercises install -> import -> registration -> lifecycle up to the external
    boundary and surfaces a connection-class error there — the signal (see
    :func:`is_external_service_only`) that the plugin's startup needs an external service
    the gate cannot stand in for, distinct from a candidate-core break. This provisions
    config the same way :data:`_BOOT_PLACEHOLDER_ENV` and :func:`_slot_env` do; the
    install-only-vs-broken classification never keys on the distribution name, only on the
    declared network permission and the runtime error class. An OIDC issuer URL must be a
    loopback ``http`` origin or the discovery client refuses it before ever connecting, so
    the black hole is a loopback port.
    """
    import secrets

    if dist_name == "tai42-channel-telegram":
        return {
            "CHANNEL_TELEGRAM_BOT_TOKEN": "9900000000:consumer-boot-gate",
            "CHANNEL_TELEGRAM_WEBHOOK_SECRET": secrets.token_hex(16),
            "CHANNEL_TELEGRAM_PUBLIC_BASE_URL": blackhole_url,
            "CHANNEL_TELEGRAM_DEFAULT_RECIPIENT": "1",
            "CHANNEL_TELEGRAM_API_BASE_URL": blackhole_url,
        }
    if dist_name == "tai42-channel-slack":
        return {
            "CHANNEL_SLACK_BOT_USER_ID": "U0BOOTGATE",
            "CHANNEL_SLACK_BOT_TOKEN": "xoxb-consumer-boot-gate",
            "CHANNEL_SLACK_SIGNING_SECRET": secrets.token_hex(16),
            "CHANNEL_SLACK_API_BASE_URL": blackhole_url,
        }
    if dist_name == "tai42-identity-oidc":
        return {
            "TAI_IDENTITY_OIDC_ISSUER": blackhole_url,
            "TAI_IDENTITY_OIDC_AUDIENCE": "consumer-boot-gate",
        }
    if dist_name == "tai42-accounts-oidc":
        return {
            "TAI_ACCOUNTS_OIDC_STATE_KEY": secrets.token_hex(16),
            "TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL": blackhole_url,
            "TAI_ACCOUNTS_OIDC_PROVIDERS": json.dumps(
                [
                    {
                        "name": "boot",
                        "issuer": blackhole_url,
                        "client_id": "boot",
                        "client_secret": "boot",
                        "claim": "sub",
                    }
                ]
            ),
        }
    return {}


@dataclass
class Infra:
    redis_url: str
    pg_host: str
    pg_port: str
    pg_user: str
    pg_password: str


def _infra_from_args(args: argparse.Namespace) -> Infra:
    return Infra(
        redis_url=args.redis_url,
        pg_host=args.pg_host,
        pg_port=str(args.pg_port),
        pg_user=args.pg_user,
        pg_password=args.pg_password,
    )


def _boot_env(
    config_dir: Path, manifest_path: Path, venv_bin: Path, db_name: str, infra: Infra, providers: list[str]
) -> dict[str, str]:
    """The child environment a served app boots under: the file config source, one
    Postgres database serving every component (each component binds to ``default``),
    the shared Redis, and access control on with the ``providers`` chain selected."""
    return {
        "PATH": os.pathsep.join([str(venv_bin), "/usr/local/bin", "/usr/bin", "/bin"]),
        "HOME": os.environ.get("HOME", str(config_dir)),
        "TAI_CONFIG_MODE": "file",
        "TAI_CONFIG_DIR_PATH": str(config_dir),
        "TAI_MANIFEST_PATH": str(manifest_path),
        # Point the plugin-prefix scan at the boot venv so ``tai db migrate`` discovers
        # every installed consumer's migration chain (the discovery scans a prefix's
        # site dirs, and the venv is exactly such a prefix); without it a pip-installed
        # store-backed consumer's schema stays pending and its boot gate refuses.
        "TAI_PLUGINS_PREFIX": str(venv_bin.parent),
        "TAI_DATABASE_DEFAULT_PG_HOST": infra.pg_host,
        "TAI_DATABASE_DEFAULT_PG_PORT": infra.pg_port,
        "TAI_DATABASE_DEFAULT_PG_USER": infra.pg_user,
        "TAI_DATABASE_DEFAULT_PG_PASSWORD": infra.pg_password,
        "TAI_DATABASE_DEFAULT_PG_DB": db_name,
        "TAI_DEFAULT_REDIS_URL": infra.redis_url,
        "ACCESS_CONTROL_ENABLE": "true",
        "ACCESS_CONTROL_AUTH_PROVIDERS": json.dumps(providers),
        "ACCESS_CONTROL_REDIS_URL": infra.redis_url,
        "INTERACTIONS_REDIS_URL": infra.redis_url,
        "TAI_TOOL_RUNS_REDIS_URL": infra.redis_url,
        "HOOKS_REDIS_URL": infra.redis_url,
        "SUB_MCP_REDIS_URL": infra.redis_url,
        "CONNECTOR_STORE_REDIS_URL": infra.redis_url,
        "TAI_RATE_LIMIT_REDIS_URL": infra.redis_url,
    }


def _run_venv_py(venv_bin: Path, code: str, payload: dict) -> subprocess.CompletedProcess[str]:
    """Run a short Python snippet in the boot venv (which carries the Postgres and
    Redis clients), passing ``payload`` as a JSON argv. The gate's own runtime env
    therefore needs neither client — the infra writes ride the same interpreter the
    app boots under."""
    return subprocess.run([str(venv_bin / "python"), "-c", code, json.dumps(payload)], capture_output=True, text=True)


def _create_database(venv_bin: Path, infra: Infra) -> str:
    """A fresh, empty database for one boot, so the migration chain and the
    access-control seed always start clean and boots never collide."""
    code = (
        "import json,sys,secrets,psycopg\n"
        "p=json.loads(sys.argv[1])\n"
        "name='consumer_boot_'+secrets.token_hex(6)\n"
        "with psycopg.connect(host=p['h'],port=p['port'],user=p['u'],password=p['pw'],"
        "dbname='postgres',autocommit=True) as c:\n"
        "    c.execute('CREATE DATABASE \"'+name+'\"')\n"
        "sys.stdout.write(name)\n"
    )
    result = _run_venv_py(venv_bin, code, _pg_payload(infra))
    if result.returncode != 0 or not result.stdout.strip():
        _fail(f"could not create a fresh boot database: {result.stderr.strip()[-400:]}")
    return result.stdout.strip()


def _seed_access_control(venv_bin: Path, db_name: str, infra: Infra) -> None:
    """Seed the minimum access-control state a health probe needs to answer 200:
    a root key in the identity provider's store and a policy row, plus a route
    table pinning the readiness probes public and mapping every other path to a
    scope the root satisfies. Without it, access control answers /health 403 and
    readiness could never confirm a healthy boot."""
    code = (
        "import json,sys,secrets,hashlib,psycopg,redis\n"
        "p=json.loads(sys.argv[1])\n"
        "hashed=hashlib.sha256(('sk-'+secrets.token_urlsafe(24)).encode()).hexdigest()\n"
        "rc=redis.Redis.from_url(p['redis'],decode_responses=True)\n"
        "try:\n"
        "    rc.hset('ac:key:'+hashed,mapping={'user_id':'root','description':'boot-gate'})\n"
        "    rc.set('ac:management:key:root',hashed)\n"
        "finally:\n"
        "    rc.close()\n"
        "with psycopg.connect(host=p['h'],port=p['port'],user=p['u'],password=p['pw'],dbname=p['db']) as conn:\n"
        "    with conn.cursor() as cur:\n"
        "        cur.execute('INSERT INTO access_control_policies (user_id, scopes) VALUES (%s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET scopes = EXCLUDED.scopes',('root',['*']))\n"
        "        cur.executemany('INSERT INTO access_control_routes (url, scope_id, pattern) VALUES (%s, %s, %s) "
        "ON CONFLICT (url) DO UPDATE SET scope_id = EXCLUDED.scope_id, pattern = EXCLUDED.pattern',"
        "[('/health','public',None),('/metrics','public',None),"
        "('all-routes','all',r'^/(?!health$)(?!metrics$).*$')])\n"
        "    conn.commit()\n"
    )
    payload = {**_pg_payload(infra), "db": db_name, "redis": infra.redis_url}
    result = _run_venv_py(venv_bin, code, payload)
    if result.returncode != 0:
        _fail(f"could not seed access control for the boot: {result.stderr.strip()[-400:]}")


def _pg_payload(infra: Infra) -> dict:
    return {"h": infra.pg_host, "port": infra.pg_port, "u": infra.pg_user, "pw": infra.pg_password}


def _allocate_port() -> int:
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def boot_consumer(
    consumer: Consumer, venv_bin: Path, provides: Provides, workdir: Path, infra: Infra
) -> BootFailure | None:
    """Boot one consumer against the candidate core and wait for health. Returns
    ``None`` on a healthy boot, or the parsed :class:`BootFailure` when the serve
    process exits before health or never becomes ready."""
    import signal
    import time
    import urllib.error
    import urllib.request

    import yaml

    tai = venv_bin / "tai"
    config_dir = workdir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = config_dir / "manifest.yml"
    manifest_path.write_text(yaml.safe_dump(build_manifest(provides), sort_keys=False))

    db_name = _create_database(venv_bin, infra)
    blackhole_url = f"http://127.0.0.1:{_allocate_port()}"
    env = {
        **_boot_env(config_dir, manifest_path, venv_bin, db_name, infra, auth_providers(provides)),
        **_channel_env(provides, infra.redis_url),
        **_db_binding_env(provides),
        **_slot_env(provides, consumer.dist_name, infra),
        **(_external_service_env(consumer.dist_name, blackhole_url) if provides.network else {}),
    }

    migrate = subprocess.run([str(tai), "db", "migrate"], env=env, capture_output=True, text=True)
    if migrate.returncode != 0:
        return BootFailure(handlers=(), routes=(), detail=f"tai db migrate failed: {migrate.stderr.strip()[-500:]}")
    _seed_access_control(venv_bin, db_name, infra)

    port = _allocate_port()
    log_path = workdir / "serve.log"
    with log_path.open("w") as log_file:
        proc = subprocess.Popen(
            [
                str(tai),
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--workers",
                "1",
                "--manifest-path",
                str(manifest_path),
            ],
            env=env,
            cwd=str(config_dir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    health_url = f"http://127.0.0.1:{port}/health"
    ready = False
    deadline = time.monotonic() + _HEALTH_DEADLINE_S
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(health_url, timeout=3) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(1.0)
    finally:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    if ready:
        return None
    return parse_boot_failure(log_path.read_text())
