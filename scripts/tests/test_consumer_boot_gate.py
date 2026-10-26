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
from _consumer_boot_gate import consumers as cbg_consumers

# ------------------------------------------------------- candidate release set


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _commit(repo: Path, message: str) -> None:
    (repo / "f").write_text(message)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)


@pytest.fixture
def core_repo(tmp_path: Path) -> Path:
    """A git repo with a two-member release config: ``core/a`` releases ``a``, ``core/b`` releases ``b``."""
    import json

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    config = {"packages": {"core/a": {"package-name": "a"}, "core/b": {"package-name": "b"}}}
    (repo / "release-please-config.json").write_text(json.dumps(config))
    return repo


def _manifest(repo: Path, a: str, b: str) -> None:
    import json

    (repo / ".release-please-manifest.json").write_text(json.dumps({"core/a": a, "core/b": b}))


_CORE = ["core/a", "core/b"]


def test_candidate_bump_is_the_highest_class_among_released_core_packages(core_repo: Path):
    # `a` is published at 1.0.0 (tagged on an earlier commit); `b` 2.0.0 is released by
    # this tree (untagged) over b-v1.3.0: the candidate is a major though `a` is not released.
    _manifest(core_repo, "1.0.0", "1.3.0")
    _commit(core_repo, "c1")
    _git(core_repo, "tag", "a-v1.0.0")
    _git(core_repo, "tag", "b-v1.3.0")
    _manifest(core_repo, "1.0.0", "2.0.0")
    _commit(core_repo, "c2")
    candidate = gate.read_candidate(core_repo, _CORE)
    assert [(m.package, m.version, m.bump) for m in candidate.members] == [
        ("a", "1.0.0", None),
        ("b", "2.0.0", "major"),
    ]
    assert candidate.bump == "major"


def test_a_published_major_does_not_count_toward_the_candidate_bump(core_repo: Path):
    # `b` 2.0.0 was published on an earlier commit; only `a`'s minor is released by this
    # tree, so the candidate is a minor and a consumer break fails the gate.
    _manifest(core_repo, "1.0.0", "1.0.0")
    _commit(core_repo, "c1")
    for tag in ("a-v1.0.0", "b-v1.0.0"):
        _git(core_repo, "tag", tag)
    _manifest(core_repo, "1.0.0", "2.0.0")
    _commit(core_repo, "c2")
    _git(core_repo, "tag", "b-v2.0.0")
    _manifest(core_repo, "1.1.0", "2.0.0")
    _commit(core_repo, "c3")
    candidate = gate.read_candidate(core_repo, _CORE)
    assert [m.bump for m in candidate.members] == ["minor", None]
    assert candidate.bump == "minor"
    assert gate.break_is_accepted(candidate.bump) is False


def test_candidate_releasing_no_core_package_reads_none(core_repo: Path):
    _manifest(core_repo, "1.0.0", "1.0.0")
    _commit(core_repo, "c1")
    for tag in ("a-v1.0.0", "b-v1.0.0"):
        _git(core_repo, "tag", tag)
    _commit(core_repo, "c2")
    assert gate.read_candidate(core_repo, _CORE).bump == "none"


def test_a_tag_on_head_counts_as_released_by_the_tree(core_repo: Path):
    # The tag-time run checks out the commit a release's tags are minted on: a version
    # tagged at HEAD is released by this tree, read against its previous tag.
    _manifest(core_repo, "1.0.0", "1.0.0")
    _commit(core_repo, "c1")
    for tag in ("a-v1.0.0", "b-v1.0.0"):
        _git(core_repo, "tag", tag)
    _manifest(core_repo, "1.1.0", "1.0.0")
    _commit(core_repo, "c2")
    _git(core_repo, "tag", "a-v1.1.0")
    candidate = gate.read_candidate(core_repo, _CORE)
    assert [m.bump for m in candidate.members] == ["minor", None]
    assert candidate.bump == "minor"
    assert gate.release_bump_set(core_repo) == {"a"}


def test_a_first_release_reads_major(core_repo: Path):
    # No tag at all: an unbounded first release carries any surface.
    _manifest(core_repo, "0.1.0", "0.1.0")
    _commit(core_repo, "c1")
    candidate = gate.read_candidate(core_repo, _CORE)
    assert [m.bump for m in candidate.members] == ["major", "major"]
    assert candidate.bump == "major"


def test_candidate_bump_honors_voided_tag_from_config(core_repo: Path):
    # The gate shares the api-gate's tag plumbing, so a tag the repo's api-gate config
    # voids is skipped as a baseline: without the void, 1.1.1 reads against a-v1.1.0
    # (patch); voiding a-v1.1.0 drops the baseline to a-v1.0.0, so the bump reads minor.
    _manifest(core_repo, "1.1.1", "1.0.0")
    _commit(core_repo, "c1")
    for tag in ("a-v1.0.0", "a-v1.1.0", "b-v1.0.0"):
        _git(core_repo, "tag", tag)
    _commit(core_repo, "c2")
    assert gate.read_candidate(core_repo, _CORE).bump == "patch"
    gh = core_repo / ".github"
    gh.mkdir()
    (gh / "api-gate.yml").write_text(
        "mode: label-honesty\nvoided_tags:\n  - tag: a-v1.1.0\n    reason: release job failed before publishing\n"
    )
    assert gate.read_candidate(core_repo, _CORE).bump == "minor"


def test_a_core_dir_without_a_release_config_entry_fails_loudly(core_repo: Path):
    _manifest(core_repo, "1.0.0", "1.0.0")
    _commit(core_repo, "c1")
    with pytest.raises(SystemExit):
        gate.read_candidate(core_repo, ["core/a", "core/c"])


def test_a_core_dir_without_a_manifest_version_fails_loudly(core_repo: Path):
    import json

    (core_repo / ".release-please-manifest.json").write_text(json.dumps({"core/a": "1.0.0"}))
    _commit(core_repo, "c1")
    with pytest.raises(SystemExit):
        gate.read_candidate(core_repo, _CORE)


def test_previous_release_is_the_newest_core_baseline_tag(core_repo: Path):
    # c1 publishes a 1.0.0 and b 1.0.0, c2 publishes b 1.1.0; HEAD releases a 1.0.1.
    # Every core package sits at its last published version on b-v1.1.0's commit.
    _manifest(core_repo, "1.0.0", "1.0.0")
    _commit(core_repo, "c1")
    for tag in ("a-v1.0.0", "b-v1.0.0"):
        _git(core_repo, "tag", tag)
    _manifest(core_repo, "1.0.0", "1.1.0")
    _commit(core_repo, "c2")
    _git(core_repo, "tag", "b-v1.1.0")
    _manifest(core_repo, "1.0.1", "1.1.0")
    _commit(core_repo, "c3")
    candidate = gate.read_candidate(core_repo, _CORE)
    assert [m.baseline_tag for m in candidate.members] == ["a-v1.0.0", "b-v1.1.0"]
    assert gate.previous_release_tag(candidate, core_repo) == "b-v1.1.0"


def test_previous_release_on_divergent_tags_fails_loudly(core_repo: Path):
    # No baseline tag descends from all the others: the gate cannot name the previous
    # released tree, so it fails rather than guess.
    _manifest(core_repo, "1.0.0", "1.0.0")
    _commit(core_repo, "c1")
    _git(core_repo, "checkout", "-qb", "side")
    _commit(core_repo, "side")
    _git(core_repo, "tag", "a-v1.0.0")
    _git(core_repo, "checkout", "-q", "-")
    _commit(core_repo, "c2")
    _git(core_repo, "tag", "b-v1.0.0")
    _commit(core_repo, "c3")
    candidate = gate.read_candidate(core_repo, _CORE)
    with pytest.raises(SystemExit):
        gate.previous_release_tag(candidate, core_repo)


@pytest.mark.parametrize(
    ("bump", "accepted"),
    [("major", True), ("minor", False), ("patch", False), ("none", False)],
)
def test_break_is_accepted_only_for_major(bump: str, accepted: bool):
    assert gate.break_is_accepted(bump) is accepted


# ------------------------------------------------------------- consumer specs


def test_wheel_name_version_parses_hyphenated_name():
    assert gate.wheel_name_version(Path("some_consumer_tools-0.42.1-py3-none-any.whl")) == (
        "some-consumer-tools",
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


def _identity() -> gate.IdentityFixture:
    """The gate's identity fixture as read from a member dir ``plugins/id`` of the candidate tree."""
    return gate.IdentityFixture(
        dir="plugins/id", dist_name="id-plugin", providers=("id-provider",), lifecycle=("id_plugin.provider",)
    )


def test_auth_providers_chains_identity_plus_consumer_providers():
    assert gate.auth_providers(gate.Provides(providers=["pkg-provider"]), _identity()) == [
        "id-provider",
        "pkg-provider",
    ]


def test_auth_providers_names_a_provider_once_when_the_consumer_is_the_fixture():
    assert gate.auth_providers(gate.Provides(providers=["id-provider"]), _identity()) == ["id-provider"]


def test_build_manifest_mounts_channels_and_extensions():
    provides = gate.Provides(channels=[("web", "pkg.register")], extensions=["pkg.ext"])
    manifest = gate.build_manifest(provides, _identity())
    assert manifest["channel_modules"] == ["pkg.register"]
    assert manifest["extensions_modules"] == ["pkg.ext"]


def test_build_manifest_mounts_core_and_consumer_surface():
    manifest = gate.build_manifest(
        gate.Provides(routers=["pkg.routes"], tools=["pkg.tools"], lifecycle=["pkg.life"]), _identity()
    )
    assert manifest["routers_modules"][: len(gate._CORE_ROUTERS)] == list(gate._CORE_ROUTERS)
    assert "pkg.routes" in manifest["routers_modules"]
    assert manifest["lifecycle_modules"] == ["id_plugin.provider", "pkg.life"]
    assert manifest["tools"] == [
        *({"title": module, "module": module} for module in gate._CORE_TOOLS),
        {"title": "pkg.tools", "module": "pkg.tools"},
    ]
    assert "api_tools" not in manifest


def test_build_manifest_mounts_only_the_core_tools_when_none_declared():
    manifest = gate.build_manifest(gate.Provides(routers=["pkg.routes"]), _identity())
    assert [entry["module"] for entry in manifest["tools"]] == list(gate._CORE_TOOLS)


def test_core_tools_are_every_skeleton_builtin_module():
    builtin = Path(__file__).resolve().parents[2] / "core/skeleton/src/tai42_skeleton/tools/builtin"
    modules = (path.stem for path in builtin.glob("*.py") if path.stem != "__init__")
    shipped = sorted(f"tai42_skeleton.tools.builtin.{stem}" for stem in modules)
    assert sorted(gate._CORE_TOOLS) == shipped


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
    manifest = gate.build_manifest(
        gate.read_provides({"provides": [{"kind": kind, "name": "x", "module": module}]}), _identity()
    )
    assert manifest[slot] == value


def test_build_manifest_mounts_agents_as_one_entry_per_name():
    provides = gate.read_provides(
        {"provides": [{"kind": "agent", "name": "tools_agent", "module": "pkg_agents.tools_agent"}]}
    )
    manifest = gate.build_manifest(provides, _identity())
    assert manifest["agents"] == [
        {"title": "tools_agent", "module": "pkg_agents.tools_agent", "include": ["tools_agent"]}
    ]


def test_build_manifest_omits_scalar_slots_when_none_declared():
    manifest = gate.build_manifest(gate.Provides(routers=["pkg.routes"]), _identity())
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
    # The plugin's OTel writer refuses construction without these two at registration.
    assert env["OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED"] == "true"
    assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]


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
    _git(repo, "tag", "tai42-plug-a-v1.2.0")  # a is published; b's 2.0.0 is not tagged (being bumped)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "c2")
    assert gate.release_bump_set(repo) == {"tai42-plug-b"}


def test_enumerate_first_party_excludes_bump_set_and_skips_unpublished(tmp_path: Path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    _plugin_tree(
        repo,
        plugins={"plug-a": "tai42-plug-a", "plug-b": "tai42-plug-b", "plug-c": "tai42-plug-c"},
        manifest={"plugins/plug-a": "1.2.0", "plugins/plug-b": "2.0.0", "plugins/plug-c": "0.1.0"},
    )
    # enumerate_first_party calls these within its own module, so patch the definition
    # module (a facade-level patch would not reach the intra-module call).
    monkeypatch.setattr(cbg_consumers, "release_bump_set", lambda _root: {"tai42-plug-b"})
    pypi = {"tai42-plug-a": "1.2.0", "tai42-plug-c": None}
    monkeypatch.setattr(cbg_consumers, "latest_pypi_version", lambda name: pypi[name])
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
    log = "Traceback (most recent call last):\nRuntimeError: the consumer payload store is not configured\n"
    failure = gate.parse_boot_failure(log)
    assert failure.handlers == ()
    assert "consumer payload store is not configured" in failure.detail


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
    ],
)
def test_external_service_env_black_holes_endpoints(dist: str, expected_keys: set[str], endpoint_keys: set[str]):
    blackhole = "http://127.0.0.1:54321"
    env = gate._external_service_env(dist, blackhole)
    assert set(env) == expected_keys
    # Every outbound endpoint the config carries points at the black hole.
    for key in endpoint_keys:
        assert blackhole in env[key]


def test_external_service_env_points_the_docker_engine_at_the_black_hole():
    env = gate._external_service_env("tai42-sandbox-docker", "http://127.0.0.1:54321")
    # The engine endpoint takes the provider's documented tcp:// shape.
    assert env == {"SANDBOX_DOCKER_HOST": "tcp://127.0.0.1:54321"}


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
    failure = gate.parse_boot_failure("RuntimeError: the consumer payload store is not configured\n")
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
    with pytest.raises(gate._ResolutionConflictError) as excinfo:
        gate._install_venv(tmp_path, ["core/contract"], _identity(), consumers, tmp_path / "venv")
    assert "No solution found when resolving dependencies" in excinfo.value.resolver_stderr


def test_install_venv_fails_on_non_resolution_error(tmp_path: Path, monkeypatch):
    # A build/network error is a hard failure regardless of the bump — the bump is never
    # consulted here, so a non-resolution failure can never be accepted as a major break.
    monkeypatch.setattr(gate.subprocess, "run", _fake_run_factory(1, "error: failed to build wheel for some-consumer"))
    consumers = [
        gate.Consumer(dist_name="some-consumer", label="some-consumer 0.44.0", install_arg="some-consumer==0.44.0")
    ]
    with pytest.raises(SystemExit):
        gate._install_venv(tmp_path, ["core/contract"], _identity(), consumers, tmp_path / "venv")


def test_run_gate_step_forwards_timeout_and_returns(monkeypatch):
    # The bound is passed through to subprocess.run and a completed step returns normally.
    seen: dict = {}

    def _run(args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(gate.subprocess, "run", _run)
    result = gate.run_gate_step(["true"], what="a bounded step", timeout=12.5, capture_output=True, text=True)
    assert result.returncode == 0
    assert seen["timeout"] == 12.5
    assert seen["capture_output"] is True


def test_run_gate_step_fails_loudly_on_timeout(monkeypatch, capsys):
    # A step that overruns its bound stops and fails the gate loudly, naming the step — never
    # a silent hang that runs the job to its host cancellation limit.
    def _raise(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs["timeout"])

    monkeypatch.setattr(gate.subprocess, "run", _raise)
    with pytest.raises(SystemExit):
        gate.run_gate_step(["sleep", "1"], what="a stalled step", timeout=0.01)
    err = capsys.readouterr().err
    assert "a stalled step" in err
    assert "did not finish within" in err


def test_run_gate_step_reports_the_stopped_steps_captured_output(monkeypatch, capsys):
    # The captured streams of a stopped step are the only record of what it was doing, so the
    # timeout report carries their tails — bytes (no ``text=True``) and str alike.
    def _raise(args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=args, timeout=kwargs["timeout"], output=b"Resolving dependencies...", stderr="waiting on index"
        )

    monkeypatch.setattr(gate.subprocess, "run", _raise)
    with pytest.raises(SystemExit):
        gate.run_gate_step(["sleep", "1"], what="a stalled step", timeout=0.01, capture_output=True)
    err = capsys.readouterr().err
    assert "Resolving dependencies..." in err
    assert "waiting on index" in err
    assert err.index("waiting on index") < err.index("did not finish within")


def test_install_venv_fails_loudly_when_install_hangs(tmp_path: Path, monkeypatch, capsys):
    # A stalled boot-venv install is a bounded, loud gate failure, not a hang: the venv step
    # succeeds and the install overruns its timeout.
    def _run(args, **kwargs):
        if args[:2] == ["uv", "venv"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs["timeout"])

    monkeypatch.setattr(gate.subprocess, "run", _run)
    consumers = [
        gate.Consumer(dist_name="some-consumer", label="some-consumer 0.44.0", install_arg="some-consumer==0.44.0")
    ]
    with pytest.raises(SystemExit):
        gate._install_venv(tmp_path, ["core/contract"], _identity(), consumers, tmp_path / "venv")
    assert "installing the boot venv" in capsys.readouterr().err


def test_is_resolution_conflict_distinguishes_no_solution_from_other_errors():
    assert gate._is_resolution_conflict(_NO_SOLUTION_STDERR) is True
    assert gate._is_resolution_conflict("error: failed to build wheel for some-consumer") is False


_PREVIOUS_NO_SOLUTION_STDERR = (
    "No solution found when resolving dependencies:\n"
    "  Because only some-consumer==0.44.0 is available and some-consumer==0.44.0 depends on\n"
    "  tai42-contract>=8.1,<9, we can conclude that your requirements are unsatisfiable.\n"
)


def _one_consumer() -> list[gate.Consumer]:
    return [gate.Consumer(dist_name="some-consumer", label="some-consumer 0.44.0", install_arg="some-consumer==0.44.0")]


def _candidate(bump: str) -> gate.Candidate:
    # One core package released by the tree at 12.0.1 (or 13.0.0 under a major) over pkg-v12.0.0.
    version = "13.0.0" if bump == "major" else "12.0.1"
    return gate.Candidate(members=(gate.CoreRelease("pkg", version, bump, "pkg-v12.0.0"),))


def _report_kwargs(tmp_path: Path) -> dict:
    return {
        "repo_root": tmp_path,
        "core_dirs": ["core/kit"],
        "identity": _identity(),
        "venv": tmp_path / "venv",
    }


def _reresolve_run_factory(dry_returncode: int, dry_stderr: str, calls: list | None = None):
    # Fake the worktree + dry-run seam of the previous-tag re-resolve: git worktree add/remove
    # succeed (recorded in ``calls``), the dry-run install returns the configured verdict, so no
    # real git or uv runs.
    if calls is None:
        calls = []

    def _run(args, **kwargs):
        if args[:3] in (["git", "worktree", "add"], ["git", "worktree", "remove"]):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["uv", "pip", "install"]:
            calls.append(args)
            return subprocess.CompletedProcess(args, dry_returncode, "", dry_stderr)
        return subprocess.CompletedProcess(args, 0, "", "")

    return _run


def test_report_unresolvable_major_bump_passes(tmp_path: Path, capsys):
    # A resolution conflict under a major bump: every supplied consumer is an accepted
    # break, the resolver's conclusion is quoted, and the gate passes (no raise, no
    # previous-release re-resolve).
    gate._report_unresolvable_consumers(
        "gate", _candidate("major"), _one_consumer(), _NO_SOLUTION_STDERR, **_report_kwargs(tmp_path)
    )
    out = capsys.readouterr().out
    assert "ACCEPTED as a major-bump break: some-consumer 0.44.0" in out
    assert "accepted break under a major bump" in out
    assert "gate passes" in out
    assert "your requirements are unsatisfiable" in out


def test_report_unresolvable_non_major_preexisting_conflict_passes(tmp_path: Path, monkeypatch, capsys):
    # Conflict + patch bump + the previous released tag ALSO finds no solution: the
    # incompatibility predates the candidate, so a notice names it and the gate passes.
    monkeypatch.setattr(gate.subprocess, "run", _reresolve_run_factory(1, _PREVIOUS_NO_SOLUTION_STDERR))
    gate._report_unresolvable_consumers(
        "gate", _candidate("patch"), _one_consumer(), _NO_SOLUTION_STDERR, **_report_kwargs(tmp_path)
    )
    out = capsys.readouterr().out
    assert "pre-existing incompatibility" in out
    assert "pkg-v12.0.0" in out
    assert "gate passes" in out
    assert "not introduced by this candidate" in out


def test_report_unresolvable_non_major_candidate_introduced_fails(tmp_path: Path, monkeypatch, capsys):
    # Conflict + patch bump + the previous released tree RESOLVES: the candidate introduced
    # the conflict, so it is a break not carried by a non-major bump — the gate fails, naming
    # the previous release it re-resolved against.
    calls: list = []
    monkeypatch.setattr(gate.subprocess, "run", _reresolve_run_factory(0, "", calls))
    with pytest.raises(SystemExit):
        gate._report_unresolvable_consumers(
            "gate", _candidate("patch"), _one_consumer(), _NO_SOLUTION_STDERR, **_report_kwargs(tmp_path)
        )
    added = [args for args in calls if args[:3] == ["git", "worktree", "add"]]
    assert len(added) == 1
    assert added[0][-1] == "pkg-v12.0.0"
    err = capsys.readouterr().err
    assert "resolve against the previous release (pkg-v12.0.0)" in err
    assert "not carried by a major bump" in err
    assert "some-consumer 0.44.0" in err
    assert "your requirements are unsatisfiable" in err


def test_report_unresolvable_previous_resolve_other_error_fails(tmp_path: Path, monkeypatch, capsys):
    # Conflict + patch bump + the previous-tag re-resolve fails for a NON-conflict reason:
    # the conflict cannot be classified, so the gate fails loudly (never a silent pass).
    monkeypatch.setattr(gate.subprocess, "run", _reresolve_run_factory(1, "error: failed to build wheel for kit"))
    with pytest.raises(SystemExit):
        gate._report_unresolvable_consumers(
            "gate", _candidate("patch"), _one_consumer(), _NO_SOLUTION_STDERR, **_report_kwargs(tmp_path)
        )
    err = capsys.readouterr().err
    assert "failed for a reason other than a dependency conflict" in err
    assert "failed to build wheel for kit" in err


# ------------------------------------------------- the gate's identity fixture


def _recording_install_run(calls: list, install_returncode: int = 0, install_stderr: str = ""):
    # The venv step succeeds; every ``uv pip install`` argv is recorded and returns the configured result.
    def _run(args, **kwargs):
        if args[:3] == ["uv", "pip", "install"]:
            calls.append(args)
            return subprocess.CompletedProcess(args, install_returncode, "", install_stderr)
        return subprocess.CompletedProcess(args, 0, "", "")

    return _run


def _editables(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "-e"]


def test_install_venv_installs_the_identity_fixture_editable_from_the_tree(tmp_path: Path, monkeypatch):
    # The fixture is a member dir of the candidate tree, installed editable beside the core —
    # never a published distribution name resolved from the index.
    calls: list = []
    monkeypatch.setattr(gate.subprocess, "run", _recording_install_run(calls))
    gate._install_venv(tmp_path, ["core/contract"], _identity(), _one_consumer(), tmp_path / "venv")
    (argv,) = calls
    assert _editables(argv) == [str(tmp_path / "core/contract"), str(tmp_path / "plugins/id")]
    assert "id-plugin" not in argv
    assert argv[-1] == "some-consumer==0.44.0"


@pytest.mark.parametrize("dist_name", ["id-plugin", "id_plugin", "ID.Plugin"])
def test_install_venv_lets_a_consumer_that_is_the_fixture_distribution_provide_it(
    tmp_path: Path, monkeypatch, dist_name: str
):
    # One distribution, one source: the published consumer is installed and the tree's fixture is not.
    calls: list = []
    monkeypatch.setattr(gate.subprocess, "run", _recording_install_run(calls))
    consumer = gate.Consumer(dist_name=dist_name, label=f"{dist_name} 1.0.0", install_arg=f"{dist_name}==1.0.0")
    gate._install_venv(tmp_path, ["core/contract"], _identity(), [consumer], tmp_path / "venv")
    (argv,) = calls
    assert _editables(argv) == [str(tmp_path / "core/contract")]
    assert f"{dist_name}==1.0.0" in argv


def test_reresolve_installs_the_previous_trees_core_and_identity_fixture(tmp_path: Path, monkeypatch):
    # The re-resolve is the same install set against the previous release's tree: its core
    # dirs and its own identity fixture, both from the worktree.
    calls: list = []
    monkeypatch.setattr(gate.subprocess, "run", _reresolve_run_factory(1, _PREVIOUS_NO_SOLUTION_STDERR, calls))
    gate._report_unresolvable_consumers(
        "gate", _candidate("patch"), _one_consumer(), _NO_SOLUTION_STDERR, **_report_kwargs(tmp_path)
    )
    (added,) = [args for args in calls if args[:3] == ["git", "worktree", "add"]]
    worktree = Path(added[-2])
    (argv,) = [args for args in calls if args[:3] == ["uv", "pip", "install"]]
    assert "--dry-run" in argv
    assert _editables(argv) == [f"{worktree / 'core/kit'}[{gate._KIT_EXTRAS}]", str(worktree / "plugins/id")]
    assert "id-plugin" not in argv


def _long_no_solution_stderr() -> str:
    # uv states the marker first, then enumerates every candidate version before its conclusion.
    explanation = "".join(
        f"  And because some-consumer=={n}.0.0 depends on tai42-kit>=0.{n},<0.{n + 1}, ...\n" for n in range(60)
    )
    return (
        "  \N{MULTIPLICATION SIGN} No solution found when resolving dependencies:\n"
        f"{explanation}"
        "  And because you require some-consumer, we can conclude that your requirements are unsatisfiable.\n"
    )


def test_install_venv_classifies_a_long_resolver_explanation_as_a_conflict(tmp_path: Path, monkeypatch):
    # The classification reads uv's whole output: a marker followed by a long explanation is
    # still a no-solution conflict, carried whole for the verdict.
    stderr = _long_no_solution_stderr()
    assert len(stderr) - stderr.index("No solution found") > 800
    monkeypatch.setattr(gate.subprocess, "run", _recording_install_run([], 1, stderr))
    with pytest.raises(gate._ResolutionConflictError) as excinfo:
        gate._install_venv(tmp_path, ["core/contract"], _identity(), _one_consumer(), tmp_path / "venv")
    assert excinfo.value.resolver_stderr == stderr.strip()


def test_reresolve_classifies_a_long_resolver_explanation_as_a_conflict(tmp_path: Path, monkeypatch, capsys):
    # The previous release's long no-solution output is a pre-existing incompatibility, not a hard failure.
    monkeypatch.setattr(gate.subprocess, "run", _reresolve_run_factory(1, _long_no_solution_stderr()))
    gate._report_unresolvable_consumers(
        "gate", _candidate("patch"), _one_consumer(), _NO_SOLUTION_STDERR, **_report_kwargs(tmp_path)
    )
    out = capsys.readouterr().out
    assert "pre-existing incompatibility" in out
    assert "gate passes" in out


def test_resolver_conclusion_quotes_the_end_of_a_long_explanation():
    conclusion = gate._resolver_conclusion(_long_no_solution_stderr())
    assert conclusion.endswith("we can conclude that your requirements are unsatisfiable.")
    assert len(conclusion) <= 802


def _write_identity_dir(root: Path, *, pyproject: bool = True, descriptor: dict | None = None) -> None:
    import yaml

    member = root / "plugins" / "id"
    member.mkdir(parents=True)
    if pyproject:
        (member / "pyproject.toml").write_text('[project]\nname = "id-plugin"\nversion = "1.0.0"\n')
    if descriptor is not None:
        (member / "tai-plugin.yml").write_text(yaml.safe_dump(descriptor))


_IDENTITY_DESCRIPTOR = {"provides": [{"kind": "identity", "name": "id-provider", "module": "id_plugin.provider"}]}


def test_read_identity_fixture_reads_the_member_dir(tmp_path: Path):
    _write_identity_dir(tmp_path, descriptor=_IDENTITY_DESCRIPTOR)
    fixture = gate.read_identity_fixture(tmp_path, "plugins/id")
    assert fixture == _identity()
    provides = gate.Provides(providers=["pkg-provider"], lifecycle=["pkg.life"])
    assert gate.auth_providers(provides, fixture) == ["id-provider", "pkg-provider"]
    assert gate.build_manifest(provides, fixture)["lifecycle_modules"] == ["id_plugin.provider", "pkg.life"]


@pytest.mark.parametrize(
    ("pyproject", "descriptor", "message"),
    [
        (False, _IDENTITY_DESCRIPTOR, "pyproject.toml"),
        (True, None, "tai-plugin.yml"),
        (True, {"provides": [{"kind": "tool", "module": "id_plugin.tools"}]}, "identity provider"),
    ],
)
def test_read_identity_fixture_fails_loudly_on_an_incomplete_dir(
    tmp_path: Path, capsys, pyproject: bool, descriptor: dict | None, message: str
):
    _write_identity_dir(tmp_path, pyproject=pyproject, descriptor=descriptor)
    with pytest.raises(SystemExit):
        gate.read_identity_fixture(tmp_path, "plugins/id")
    err = capsys.readouterr().err
    assert "plugins/id" in err
    assert message in err
