"""Unit tests for scripts/consumer_boot_gate.py — the behavioural consumer boot
gate. Hermetic: the pure decision, parsing and manifest helpers are tested
directly, and the bump verdict against a throwaway git repo built under
``tmp_path``. No venv, no live boot, no real infra, tag or PyPI is touched — the
end-to-end boot is exercised out of band, not from this suite."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import consumer_boot_gate as gate

# ------------------------------------------------------------------ bump verdict


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def tagged_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "f").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "c")
    for tag in ("pkg-v1.0.0", "pkg-v1.1.0"):
        _git(repo, "tag", tag)
    return repo


@pytest.mark.parametrize(
    ("version", "expected"),
    [("2.0.0", "major"), ("1.2.0", "minor"), ("1.1.1", "patch")],
)
def test_governing_bump_against_previous_tag(tagged_repo: Path, version: str, expected: str):
    assert gate.governing_bump("pkg", version, tagged_repo) == expected


def test_governing_bump_first_release_is_major(tagged_repo: Path):
    # A package with no prior tag: an unbounded first release carries any surface.
    assert gate.governing_bump("other", "0.1.0", tagged_repo) == "major"


def test_governing_bump_unbumped_reads_last_release_class(tagged_repo: Path):
    # Not bumped on a train: the version is the last released one, so the bump reads
    # as that last release's class (here minor: 1.0.0 -> 1.1.0), never major.
    assert gate.governing_bump("pkg", "1.1.0", tagged_repo) == "minor"


@pytest.mark.parametrize(
    ("bump", "accepted"),
    [("major", True), ("minor", False), ("patch", False)],
)
def test_break_is_accepted_only_for_major(bump: str, accepted: bool):
    assert gate.break_is_accepted(bump) is accepted


# ------------------------------------------------------------- consumer specs


def test_wheel_name_version_parses_hyphenated_name():
    assert gate.wheel_name_version(Path("some_consumer_flows-0.42.1-py3-none-any.whl")) == (
        "some-consumer-flows",
        "0.42.1",
    )


def test_wheel_name_version_rejects_unparseable_filename():
    with pytest.raises(SystemExit):
        gate.wheel_name_version(Path("not-a-wheel.txt"))


@pytest.mark.parametrize(
    ("req", "name"),
    [
        ("some-plugin==1.2.0", "some-plugin"),
        ("some-plugin>=1,<2", "some-plugin"),
        ("some-plugin[extra]", "some-plugin"),
    ],
)
def test_req_dist_name(req: str, name: str):
    assert gate._req_dist_name(req) == name


def test_collect_consumers_missing_wheel_raises(tmp_path: Path):
    # A supplied wheel that is absent (an upstream download that failed) is a hard
    # error, never a silently skipped consumer that reads as a pass.
    with pytest.raises(SystemExit):
        gate.collect_consumers([str(tmp_path / "absent-1.0.0-py3-none-any.whl")], [])


def test_collect_consumers_from_req():
    consumers = gate.collect_consumers([], ["some-plugin==2.0.0"])
    assert consumers == [
        gate.Consumer(dist_name="some-plugin", label="some-plugin==2.0.0", install_arg="some-plugin==2.0.0")
    ]


# ------------------------------------------------------------- plugin manifest


def test_read_provides_collects_routers_tools_lifecycle():
    provides = gate.read_provides(
        {
            "provides": [
                {"kind": "router", "name": "a", "module": "pkg.routes_a"},
                {"kind": "router", "name": "b", "module": "pkg.routes_b"},
                {"kind": "tool", "name": "t1", "module": "pkg.tools"},
                {"kind": "tool", "name": "t2", "module": "pkg.tools"},
                {"kind": "studio-plugin", "name": "s", "module": "pkg"},
            ],
            "lifecycle_modules": ["pkg.lifecycle"],
        }
    )
    assert provides.routers == ["pkg.routes_a", "pkg.routes_b"]
    assert provides.tools == ["pkg.tools"]  # de-duplicated across the two tool entries
    assert provides.lifecycle == ["pkg.lifecycle"]


def test_read_provides_ignores_entries_without_a_module():
    provides = gate.read_provides({"provides": [{"kind": "router", "name": "x"}]})
    assert provides.routers == []


def test_read_provides_channels_extensions_providers_and_migrations():
    provides = gate.read_provides(
        {
            "provides": [
                {"kind": "channel", "name": "web", "module": "pkg.register"},
                {"kind": "extension", "name": "e", "module": "pkg.ext"},
                {"kind": "identity", "name": "pkg-provider", "module": "pkg.provider"},
                {"kind": "webhook-verifier", "name": "v", "module": "pkg.verifier"},
            ],
            "migrations": "migrations",
            "package": "some-store-plugin",
        }
    )
    assert provides.channels == [("web", "pkg.register")]
    assert provides.extensions == ["pkg.ext"]
    assert provides.providers == ["pkg-provider"]
    assert provides.lifecycle == ["pkg.provider", "pkg.verifier"]  # provider + verifier mounted
    assert provides.db_component == "some-store-plugin"  # defaults to the package name


def test_read_provides_migrations_component_override():
    provides = gate.read_provides({"migrations": "m", "migrations_component": "custom-store", "package": "p"})
    assert provides.db_component == "custom-store"


def test_channel_env_binds_each_channel_redis():
    env = gate._channel_env(gate.Provides(channels=[("web", "m1"), ("telegram", "m2")]), "redis://r:1")
    assert env == {"CHANNEL_WEB_REDIS_URL": "redis://r:1", "CHANNEL_TELEGRAM_REDIS_URL": "redis://r:1"}


def test_db_binding_env_pins_component_to_default():
    env = gate._db_binding_env(gate.Provides(db_component="tai42-accounts-postgres"))
    assert env == {"TAI_DB_BINDING_TAI42_ACCOUNTS_POSTGRES": "default"}
    assert gate._db_binding_env(gate.Provides()) == {}


def test_auth_providers_chains_identity_plus_consumer_providers():
    assert gate.auth_providers(gate.Provides(providers=["pkg-provider"])) == [
        gate._IDENTITY_PROVIDER_NAME,
        "pkg-provider",
    ]


def test_build_manifest_mounts_channels_and_extensions():
    manifest = gate.build_manifest(gate.Provides(channels=[("web", "pkg.register")], extensions=["pkg.ext"]))
    assert manifest["channel_modules"] == ["pkg.register"]
    assert manifest["extensions_modules"] == ["pkg.ext"]


def test_build_manifest_mounts_core_and_consumer_surface():
    manifest = gate.build_manifest(gate.Provides(routers=["pkg.routes"], tools=["pkg.tools"], lifecycle=["pkg.life"]))
    assert manifest["routers_modules"][: len(gate._CORE_ROUTERS)] == list(gate._CORE_ROUTERS)
    assert "pkg.routes" in manifest["routers_modules"]
    assert manifest["lifecycle_modules"] == [gate._IDENTITY_LIFECYCLE_MODULE, "pkg.life"]
    assert manifest["tools"] == [{"title": "pkg.tools", "module": "pkg.tools"}]
    assert manifest["api_tools"] == {"enabled": False}


def test_build_manifest_omits_tools_when_none_declared():
    manifest = gate.build_manifest(gate.Provides(routers=["pkg.routes"]))
    assert "tools" not in manifest


# --------------------------------------------------- exclusive-slot consumers


@pytest.mark.parametrize(
    ("kind", "module", "slot", "expected"),
    [
        ("backend", "pkg_backend.core.backend", "backend_module", "pkg_backend"),
        ("storage", "pkg_storage", "storage_module", "pkg_storage"),
        ("sandbox", "pkg_sandbox.provider", "sandbox_module", "pkg_sandbox"),
        ("monitoring", "pkg_monitor.register", "monitoring_module", "pkg_monitor"),
    ],
)
def test_read_provides_scalar_slot_names_top_level_package(kind: str, module: str, slot: str, expected: str):
    # The scalar slot names the plugin's TOP-LEVEL package (not the descriptor's impl
    # submodule) so the whole package imports and every module under it is whitelisted.
    provides = gate.read_provides({"provides": [{"kind": kind, "name": "x", "module": module}]})
    assert getattr(provides, slot) == expected
    assert provides.has_boot_surface() is True


def test_read_provides_agent_entries_kept_name_and_module():
    provides = gate.read_provides(
        {
            "provides": [
                {"kind": "agent", "name": "tools_agent", "module": "pkg_agents.tools_agent"},
                {"kind": "agent", "name": "deep_agent", "module": "pkg_agents.deep_agent"},
            ]
        }
    )
    assert provides.agents == [("tools_agent", "pkg_agents.tools_agent"), ("deep_agent", "pkg_agents.deep_agent")]
    assert provides.has_boot_surface() is True


def test_read_provides_config_kind_is_install_only_not_bootable():
    # A config provider is selected before the manifest loads; it cannot be mounted
    # into a boot, so it is recorded install-only and has no boot surface.
    provides = gate.read_provides({"provides": [{"kind": "config", "name": "vault", "module": "pkg_config.manager"}]})
    assert [kind for kind, _reason in provides.install_only] == ["config"]
    assert provides.install_only[0][1]  # a non-empty reason accompanies it
    assert provides.backend_module is None
    assert provides.has_boot_surface() is False


def test_has_boot_surface_false_when_nothing_declared():
    assert gate.Provides().has_boot_surface() is False


@pytest.mark.parametrize(
    ("kind", "module", "slot", "value"),
    [
        ("backend", "pkg_backend.backend", "backend_module", "pkg_backend"),
        ("storage", "pkg_storage", "storage_module", "pkg_storage"),
        ("sandbox", "pkg_sandbox.provider", "sandbox_module", "pkg_sandbox"),
        ("monitoring", "pkg_monitor.register", "monitoring_module", "pkg_monitor"),
    ],
)
def test_build_manifest_mounts_each_scalar_slot(kind: str, module: str, slot: str, value: str):
    manifest = gate.build_manifest(gate.read_provides({"provides": [{"kind": kind, "name": "x", "module": module}]}))
    assert manifest[slot] == value


def test_build_manifest_mounts_agents_as_one_entry_per_name():
    provides = gate.read_provides(
        {"provides": [{"kind": "agent", "name": "tools_agent", "module": "pkg_agents.tools_agent"}]}
    )
    manifest = gate.build_manifest(provides)
    assert manifest["agents"] == [
        {"title": "tools_agent", "module": "pkg_agents.tools_agent", "include": ["tools_agent"]}
    ]


def test_build_manifest_omits_scalar_slots_when_none_declared():
    manifest = gate.build_manifest(gate.Provides(routers=["pkg.routes"]))
    for slot in ("backend_module", "storage_module", "sandbox_module", "monitoring_module", "agents"):
        assert slot not in manifest


def test_slot_env_pins_bus_for_a_backend():
    infra = gate.Infra(redis_url="redis://r:1", pg_host="h", pg_port="1", pg_user="u", pg_password="p")
    env = gate._slot_env(gate.Provides(backend_module="pkg_backend"), "tai42-pkg-backend", infra)
    assert env == {"TAI_BUS_REDIS_URL": "redis://r:1"}


def test_slot_env_supplies_placeholder_config_for_an_eager_provider():
    infra = gate.Infra(redis_url="redis://r:1", pg_host="h", pg_port="1", pg_user="u", pg_password="p")
    provides = gate.Provides(monitoring_module="tai42_monitoring_langfuse")
    env = gate._slot_env(provides, "tai42-monitoring-langfuse", infra)
    assert env["LANGFUSE_HOST"]
    assert env["LANGFUSE_PUBLIC_KEY"]
    assert env["LANGFUSE_SECRET_KEY"]


def test_slot_env_empty_for_a_lazy_provider():
    infra = gate.Infra(redis_url="redis://r:1", pg_host="h", pg_port="1", pg_user="u", pg_password="p")
    assert gate._slot_env(gate.Provides(storage_module="pkg_storage"), "tai42-pkg-storage", infra) == {}


# --------------------------------------------------- first-party enumeration


def _plugin_tree(repo: Path, plugins: dict[str, str], manifest: dict[str, str]) -> None:
    """Write a synthetic first-party layout: a ``plugins/<dir>/pyproject.toml`` per
    plugin, the release-please manifest, and the config mapping paths to names."""
    import json

    config_packages = {}
    for dir_name, dist in plugins.items():
        d = repo / "plugins" / dir_name
        d.mkdir(parents=True)
        (d / "pyproject.toml").write_text(f'[project]\nname = "{dist}"\nversion = "0.0.0"\n')
        config_packages[f"plugins/{dir_name}"] = {"package-name": dist}
    (repo / ".release-please-manifest.json").write_text(json.dumps(manifest))
    (repo / "release-please-config.json").write_text(json.dumps({"packages": config_packages}))


def test_release_bump_set_flags_untagged_manifest_versions(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _plugin_tree(
        repo,
        plugins={"plug-a": "tai42-plug-a", "plug-b": "tai42-plug-b"},
        manifest={"plugins/plug-a": "1.2.0", "plugins/plug-b": "2.0.0"},
    )
    (repo / "f").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "c")
    _git(repo, "tag", "tai42-plug-a-v1.2.0")  # a is released; b's 2.0.0 is not tagged (being bumped)
    assert gate.release_bump_set(repo) == {"tai42-plug-b"}


def test_enumerate_first_party_excludes_bump_set_and_skips_unpublished(tmp_path: Path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    _plugin_tree(
        repo,
        plugins={"plug-a": "tai42-plug-a", "plug-b": "tai42-plug-b", "plug-c": "tai42-plug-c"},
        manifest={"plugins/plug-a": "1.2.0", "plugins/plug-b": "2.0.0", "plugins/plug-c": "0.1.0"},
    )
    monkeypatch.setattr(gate, "release_bump_set", lambda _root: {"tai42-plug-b"})
    pypi = {"tai42-plug-a": "1.2.0", "tai42-plug-c": None}
    monkeypatch.setattr(gate, "latest_pypi_version", lambda name: pypi[name])
    consumers, notices = gate.enumerate_first_party(repo)
    # plug-b excluded (bump set); plug-c skipped (no PyPI, a notice not a failure); plug-a booted.
    assert [c.install_arg for c in consumers] == ["tai42-plug-a==1.2.0"]
    assert any("tai42-plug-c" in n and "no PyPI release" in n for n in notices)


# ------------------------------------------------------------- failure parsing


def test_parse_boot_failure_names_handlers_and_route():
    log = (
        "INFO some boot line\n"
        "RuntimeError: lifecycle handlers failed: seed_roles: ValueError('route GET /bindings declares no "
        "response_model and no no_body_reason'), check_raw_path_routes_resolvable: ValueError('route GET /bindings "
        "declares no response_model and no no_body_reason')\n"
        "ERROR:    Application startup failed. Exiting.\n"
    )
    failure = gate.parse_boot_failure(log)
    assert "check_raw_path_routes_resolvable" in failure.handlers
    assert "seed_roles" in failure.handlers
    assert failure.routes == ("GET /bindings",)


def test_parse_boot_failure_without_lifecycle_line_keeps_error_detail():
    log = "Traceback (most recent call last):\nRuntimeError: the flow payload store is not configured\n"
    failure = gate.parse_boot_failure(log)
    assert failure.handlers == ()
    assert "flow payload store is not configured" in failure.detail


def test_boot_failure_summary_lists_routes_then_handlers():
    summary = gate.BootFailure(handlers=("h1", "h2"), routes=("GET /x",), detail="d").summary()
    assert summary == "route(s): GET /x; failing handler(s): h1, h2"


# ------------------------------------------ external-service classification


def test_read_provides_reads_network_permission():
    assert gate.read_provides({"permissions": {"network": True}}).network is True
    assert gate.read_provides({"permissions": {"network": False}}).network is False
    assert gate.read_provides({}).network is False


@pytest.mark.parametrize(
    ("dist", "expected_keys", "endpoint_keys"),
    [
        (
            "tai42-channel-telegram",
            {
                "CHANNEL_TELEGRAM_BOT_TOKEN",
                "CHANNEL_TELEGRAM_WEBHOOK_SECRET",
                "CHANNEL_TELEGRAM_PUBLIC_BASE_URL",
                "CHANNEL_TELEGRAM_DEFAULT_RECIPIENT",
                "CHANNEL_TELEGRAM_API_BASE_URL",
            },
            {"CHANNEL_TELEGRAM_API_BASE_URL", "CHANNEL_TELEGRAM_PUBLIC_BASE_URL"},
        ),
        (
            "tai42-channel-slack",
            {
                "CHANNEL_SLACK_BOT_USER_ID",
                "CHANNEL_SLACK_BOT_TOKEN",
                "CHANNEL_SLACK_SIGNING_SECRET",
                "CHANNEL_SLACK_API_BASE_URL",
            },
            {"CHANNEL_SLACK_API_BASE_URL"},
        ),
        (
            "tai42-identity-oidc",
            {"TAI_IDENTITY_OIDC_ISSUER", "TAI_IDENTITY_OIDC_AUDIENCE"},
            {"TAI_IDENTITY_OIDC_ISSUER"},
        ),
        (
            "tai42-accounts-oidc",
            {"TAI_ACCOUNTS_OIDC_STATE_KEY", "TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL", "TAI_ACCOUNTS_OIDC_PROVIDERS"},
            {"TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL"},
        ),
    ],
)
def test_external_service_env_black_holes_endpoints(dist: str, expected_keys: set[str], endpoint_keys: set[str]):
    blackhole = "http://127.0.0.1:54321"
    env = gate._external_service_env(dist, blackhole)
    assert set(env) == expected_keys
    # Every outbound endpoint the config carries points at the black hole (loopback so an
    # OIDC discovery client attempts the connection rather than refusing a non-https URL).
    for key in endpoint_keys:
        assert blackhole in env[key]


def test_external_service_env_empty_for_an_unknown_distribution():
    assert gate._external_service_env("tai42-some-other-plugin", "http://127.0.0.1:1") == {}


def test_parse_boot_failure_captures_per_handler_error_text():
    log = (
        'RuntimeError: lifecycle handlers failed: probe_identity_provider: JwksFetchError("Transport error '
        "fetching 'http://127.0.0.1:45097/.well-known/openid-configuration': All connection attempts failed\")\n"
    )
    failure = gate.parse_boot_failure(log)
    assert failure.handlers == ("probe_identity_provider",)
    assert len(failure.handler_errors) == 1
    name, text = failure.handler_errors[0]
    assert name == "probe_identity_provider"
    assert "Transport error fetching" in text


def test_connection_error_only_boot_is_external_service():
    # A network-declaring consumer whose sole startup failure is a connection-class error
    # reaching the configured (black-holed) endpoint: it needs an external service.
    log = (
        "RuntimeError: lifecycle handlers failed: "
        "_register_telegram_webhook: ConnectError('All connection attempts failed')\n"
    )
    failure = gate.parse_boot_failure(log)
    assert gate.is_external_service_only(failure) is True
    assert gate.external_service_handlers(failure) == ("_register_telegram_webhook",)


def test_guard_error_boot_is_not_external_service():
    # A response-model guard raised at startup is a candidate-core break, never an
    # external-service miss — it must stay a BOOT FAILED.
    log = (
        "RuntimeError: lifecycle handlers failed: seed_roles: ValueError('route PUT /users/me/password declares "
        "no response_model and no no_body_reason')\n"
    )
    failure = gate.parse_boot_failure(log)
    assert gate.is_external_service_only(failure) is False
    assert gate.external_service_handlers(failure) == ()


def test_mixed_connection_and_guard_failure_is_not_external_service():
    # One connection-class error next to one guard error: a real break rides alongside the
    # external miss, so the boot must not be reclassified as install-only.
    log = (
        "RuntimeError: lifecycle handlers failed: probe_identity_provider: ConnectError('All connection attempts "
        "failed'), check_route_actions: ValueError('gated route failed the action-class audit')\n"
    )
    failure = gate.parse_boot_failure(log)
    assert gate.is_external_service_only(failure) is False


def test_non_lifecycle_failure_is_not_external_service():
    # No structured lifecycle line (an import/migrate failure): never install-only.
    failure = gate.parse_boot_failure("RuntimeError: the flow payload store is not configured\n")
    assert gate.is_external_service_only(failure) is False


# ---------------------------------------------------- unresolvable-install verdict


_NO_SOLUTION_STDERR = (
    "No solution found when resolving dependencies:\n"
    "  Because only some-consumer==0.44.0 is available and some-consumer==0.44.0 depends on\n"
    "  tai42-contract>=8.1,<9, we can conclude that your requirements are unsatisfiable.\n"
)


def _fake_run_factory(install_returncode: int, install_stderr: str):
    def _run(args, **kwargs):
        if args[:2] == ["uv", "venv"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, install_returncode, "", install_stderr)

    return _run


def test_install_venv_raises_conflict_on_no_solution(tmp_path: Path, monkeypatch):
    # uv's no-solution failure is a consumer range excluding the candidate: surfaced as a
    # classifiable conflict, not a hard failure, so the caller can accept it under a major bump.
    monkeypatch.setattr(gate.subprocess, "run", _fake_run_factory(1, _NO_SOLUTION_STDERR))
    consumers = [
        gate.Consumer(dist_name="some-consumer", label="some-consumer 0.44.0", install_arg="some-consumer==0.44.0")
    ]
    with pytest.raises(gate._ResolutionConflict) as excinfo:
        gate._install_venv(tmp_path, ["core/contract"], "id-pkg", consumers, tmp_path / "venv")
    assert "No solution found when resolving dependencies" in excinfo.value.resolver_stderr


def test_install_venv_fails_on_non_resolution_error(tmp_path: Path, monkeypatch):
    # A build/network error is a hard failure regardless of the bump — the bump is never
    # consulted here, so a non-resolution failure can never be accepted as a major break.
    monkeypatch.setattr(gate.subprocess, "run", _fake_run_factory(1, "error: failed to build wheel for some-consumer"))
    consumers = [
        gate.Consumer(dist_name="some-consumer", label="some-consumer 0.44.0", install_arg="some-consumer==0.44.0")
    ]
    with pytest.raises(SystemExit):
        gate._install_venv(tmp_path, ["core/contract"], "id-pkg", consumers, tmp_path / "venv")


def test_is_resolution_conflict_distinguishes_no_solution_from_other_errors():
    assert gate._is_resolution_conflict(_NO_SOLUTION_STDERR) is True
    assert gate._is_resolution_conflict("error: failed to build wheel for some-consumer") is False


def test_report_unresolvable_major_bump_passes(capsys):
    # A resolution conflict under a major bump: every supplied consumer is an accepted
    # break, the resolver's conclusion is quoted, and the gate passes (no raise).
    consumers = [
        gate.Consumer(dist_name="some-consumer", label="some-consumer 0.44.0", install_arg="some-consumer==0.44.0")
    ]
    gate._report_unresolvable_consumers("gate: pkg 12.0.0 (major bump)", "major", consumers, _NO_SOLUTION_STDERR)
    out = capsys.readouterr().out
    assert "ACCEPTED as a major-bump break: some-consumer 0.44.0" in out
    assert "accepted break under a major bump" in out
    assert "gate passes" in out
    assert "your requirements are unsatisfiable" in out


def test_report_unresolvable_minor_bump_fails(capsys):
    # The same conflict under a non-major bump fails, with the resolver output in the message.
    consumers = [
        gate.Consumer(dist_name="some-consumer", label="some-consumer 0.44.0", install_arg="some-consumer==0.44.0")
    ]
    with pytest.raises(SystemExit):
        gate._report_unresolvable_consumers("gate: pkg 11.5.0 (minor bump)", "minor", consumers, _NO_SOLUTION_STDERR)
    err = capsys.readouterr().err
    assert "boot venv install failed" in err
    assert "No solution found when resolving dependencies" in err
