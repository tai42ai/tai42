"""B2 support — a threaded fake Kubernetes CoreV1 API server + kubeconfig writer.

The ``config-k8s`` provider reads env from a K8s Secret and the manifest from a K8s
ConfigMap through the real ``kubernetes`` client (``manager.py`` requires an
in-cluster env or a kubeconfig — there is no config-less path). This module stands up
the SMALLEST apiserver surface that client speaks for the provider's calls, on a
loopback port in the pytest process (like the other threaded net fixtures), pointed at
by a generated HTTP (no-TLS) kubeconfig:

* ``GET /api/v1/namespaces/{ns}/secrets/{name}`` and ``.../configmaps/{name}`` —
  ``read_namespaced_secret`` / ``read_namespaced_config_map`` (``manager.py``).
* ``PATCH`` of each — ``write_env`` (Secret ``stringData`` merge) and the manifest
  write (ConfigMap PATCH under the ``resourceVersion`` optimistic-concurrency
  precondition, ``manager.py``). A ConfigMap PATCH whose body carries a
  ``resourceVersion`` other than the stored one is a ``409 Conflict``, exactly as a
  real apiserver rejects a stale write.

The store is in-memory and records every read/patch so a spec can assert the boot
really read the ConfigMap and a reload really PATCHed the store. Self-contained: it
needs no ``kubernetes`` dependency (it IS the server side); only the SUT process does."""

from __future__ import annotations

import base64
import threading
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tai42_e2e._threaded import ThreadedServer
from tai42_e2e.ports import allocate_port


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class FakeKubernetes:
    """A recording loopback stub of the CoreV1 Secret + ConfigMap surface the
    ``config-k8s`` provider drives. One namespace, one Secret, one ConfigMap."""

    def __init__(self, *, namespace: str, secret_name: str, configmap_name: str, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = allocate_port()
        self.namespace = namespace
        self.secret_name = secret_name
        self.configmap_name = configmap_name
        self._lock = threading.Lock()
        # Each object's ``data`` map (Secret values base64, ConfigMap values plaintext)
        # and its integer resourceVersion (served as a string, bumped on every PATCH).
        self._secret_data: dict[str, str] = {}
        self._secret_rv = 1
        self._configmap_data: dict[str, str] = {}
        self._configmap_rv = 1
        # One-shot: when armed, the NEXT ConfigMap PATCH bumps the stored rv before its
        # precondition check, modelling a competing writer landing between the manager's read
        # and write — so the PATCH carries a now-stale rv and hits the 409 precondition.
        self._stale_conflict_armed = False
        # Recording surface for the spec's assertions.
        self.configmap_reads = 0
        self.secret_reads = 0
        self.secret_patches = 0
        self.configmap_patches = 0
        self.configmap_conflicts = 0
        self.last_configmap_patch_rv: str | None = None
        self._server = ThreadedServer(self._build_app(), host, self.port)

    # ---- lifecycle -------------------------------------------------------

    @property
    def server_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()

    # ---- seeding + inspection (test side) --------------------------------

    def seed_manifest(self, manifest_key: str, manifest_text: str) -> None:
        """Seed the ConfigMap with the deployment manifest YAML the SUT reads at boot."""
        with self._lock:
            self._configmap_data = {manifest_key: manifest_text}

    def seed_secret(self, data: dict[str, str] | None = None) -> None:
        """Seed the Secret's env (plaintext values, stored base64 as a real Secret is)."""
        with self._lock:
            self._secret_data = {key: _b64(value) for key, value in (data or {}).items()}

    def configmap_manifest_text(self, manifest_key: str) -> str:
        with self._lock:
            return self._configmap_data[manifest_key]

    def configmap_resource_version(self) -> str:
        with self._lock:
            return str(self._configmap_rv)

    def arm_stale_conflict(self) -> None:
        """Arm a one-shot competing write: the next ConfigMap PATCH finds the stored rv
        bumped out from under the rv it carries, so it 409s — driving the manager's
        ``409 → re-read → retry`` loop exactly once before the retry lands."""
        with self._lock:
            self._stale_conflict_armed = True

    def secret_env(self) -> dict[str, str]:
        """The Secret's env decoded back to plaintext — the value the reload round-trips."""
        with self._lock:
            return {key: base64.b64decode(value).decode("utf-8") for key, value in self._secret_data.items()}

    def write_kubeconfig(self, path: Path) -> None:
        """Write a minimal HTTP (no-TLS) kubeconfig pointing the client at this server,
        with the namespace's default context — the ``KUBECONFIG`` the SUT loads."""
        document = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [{"name": "fake", "cluster": {"server": self.server_url}}],
            "users": [{"name": "fake", "user": {}}],
            "contexts": [{"name": "fake", "context": {"cluster": "fake", "user": "fake", "namespace": self.namespace}}],
            "current-context": "fake",
        }
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    # ---- apiserver surface ----------------------------------------------

    def _secret_body(self) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {
                "name": self.secret_name,
                "namespace": self.namespace,
                "resourceVersion": str(self._secret_rv),
            },
            "data": dict(self._secret_data),
        }

    def _configmap_body(self) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": self.configmap_name,
                "namespace": self.namespace,
                "resourceVersion": str(self._configmap_rv),
            },
            "data": dict(self._configmap_data),
        }

    @staticmethod
    def _status(code: int, reason: str, message: str) -> JSONResponse:
        return JSONResponse(
            {
                "apiVersion": "v1",
                "kind": "Status",
                "status": "Failure",
                "code": code,
                "reason": reason,
                "message": message,
            },
            status_code=code,
        )

    def _build_app(self) -> FastAPI:
        app = FastAPI()
        base = "/api/v1/namespaces/{ns}"

        @app.get(base + "/secrets/{name}")
        async def read_secret(ns: str, name: str) -> JSONResponse:
            with self._lock:
                if ns != self.namespace or name != self.secret_name:
                    return self._status(404, "NotFound", f"secret {name} not found")
                self.secret_reads += 1
                return JSONResponse(self._secret_body())

        @app.get(base + "/configmaps/{name}")
        async def read_configmap(ns: str, name: str) -> JSONResponse:
            with self._lock:
                if ns != self.namespace or name != self.configmap_name:
                    return self._status(404, "NotFound", f"configmap {name} not found")
                self.configmap_reads += 1
                return JSONResponse(self._configmap_body())

        @app.patch(base + "/secrets/{name}")
        async def patch_secret(ns: str, name: str, request: Request) -> JSONResponse:
            body = await request.json()
            with self._lock:
                if ns != self.namespace or name != self.secret_name:
                    return self._status(404, "NotFound", f"secret {name} not found")
                # stringData is write-only plaintext merged into data as base64.
                for key, value in (body.get("stringData") or {}).items():
                    self._secret_data[key] = _b64(str(value))
                self._secret_rv += 1
                self.secret_patches += 1
                return JSONResponse(self._secret_body())

        @app.patch(base + "/configmaps/{name}")
        async def patch_configmap(ns: str, name: str, request: Request) -> JSONResponse:
            body = await request.json()
            with self._lock:
                if ns != self.namespace or name != self.configmap_name:
                    return self._status(404, "NotFound", f"configmap {name} not found")
                sent_rv = (body.get("metadata") or {}).get("resourceVersion")
                self.last_configmap_patch_rv = None if sent_rv is None else str(sent_rv)
                if self._stale_conflict_armed:
                    # A competing writer lands between the manager's read and THIS write:
                    # bump the stored rv so the rv the manager carries is now stale and 409s.
                    # One-shot, so the manager's re-read + retry then lands. Bumping on the
                    # PATCH (not on a read) keeps the injection independent of how many times
                    # the manager reads the ConfigMap per replace.
                    self._stale_conflict_armed = False
                    self._configmap_rv += 1
                # The optimistic-concurrency precondition: a stale resourceVersion is a 409,
                # exactly as a real apiserver rejects a lost update.
                if sent_rv is not None and str(sent_rv) != str(self._configmap_rv):
                    self.configmap_conflicts += 1
                    return self._status(409, "Conflict", "the object has been modified; please apply your changes")
                for key, value in (body.get("data") or {}).items():
                    self._configmap_data[key] = value
                self._configmap_rv += 1
                self.configmap_patches += 1
                return JSONResponse(self._configmap_body())

        return app
