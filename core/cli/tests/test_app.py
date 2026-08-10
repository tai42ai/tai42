"""The ``tai`` client app: the remote groups render, the root callback builds the
shared context, a raised :class:`ApiError` surfaces cleanly, and the ``tai.commands``
entry-point seam contributes commands deterministically (a failing contribution is
never swallowed). A slim install — no contributor — carries only the remote client
plus the native ``completion``/``version`` commands.
"""

from __future__ import annotations

import json

import click
import httpx
import pytest
from click.testing import CliRunner

from tai42_cli import app as app_module
from tai42_cli.client import ApiClient, ApiError
from tai42_cli.context import AppContext

# The remote client groups tai42-cli owns outright, plus the two native commands.
_REMOTE_GROUPS = {
    "tools",
    "presets",
    "agents",
    "extensions",
    "connectors",
    "hooks",
    "manifest",
    "mcp",
    "sub-mcp",
    "templates",
    "config",
    "keys",
    "scopes",
    "backup",
    "schedules",
    "obs",
    "traces",
    "interactions",
    "completion",
    "version",
}
# Only a contributor (the server package) supplies these; a slim install has none.
_CONTRIBUTED = {"serve", "db", "backend", "metrics", "doctor", "catalog", "openapi"}


class _FakeEP:
    """A stand-in ``importlib.metadata`` entry point: a name and a ``load()``."""

    def __init__(self, name: str, register) -> None:
        self.name = name
        self._register = register

    def load(self):
        return self._register


def _slim(monkeypatch: pytest.MonkeyPatch) -> click.Group:
    """Rebuild the app with NO contributors discovered — the slim-install tree."""
    monkeypatch.setattr(app_module, "entry_points", lambda group: [])
    return app_module._build_app()


def test_remote_groups_render() -> None:
    result = CliRunner().invoke(app_module.app, ["--help"])
    assert result.exit_code == 0, result.output
    for name in _REMOTE_GROUPS:
        assert name in result.output, f"missing group in --help: {name}"


def test_callback_populates_app_context(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from tai42_cli import context

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv(context.SERVER_URL_ENV, "http://probe-host")

    group = _slim(monkeypatch)
    captured: dict[str, object] = {}

    @click.command("probe")
    @click.pass_context
    def probe(ctx: click.Context) -> None:
        captured["obj"] = ctx.obj

    group.add_command(probe, "probe")
    result = CliRunner().invoke(group, ["--json", "probe"])

    assert result.exit_code == 0, result.output
    obj = captured["obj"]
    assert isinstance(obj, AppContext)
    assert obj.json_output is True
    assert obj.server_url == "http://probe-host"


def test_trailing_json_flag_on_remote_leaf_renders_json(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``tai tools list --json`` (flag AFTER the leaf command) renders JSON just like
    # the flag-first ``tai --json tools list`` form.
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"data": ["alpha", "beta"]}))
    monkeypatch.setattr(
        AppContext,
        "client",
        lambda self: ApiClient(self.server_url, self.api_key, transport=transport),
    )
    monkeypatch.setenv("TAI_API_KEY", "test-key")
    monkeypatch.setenv("TAI_SERVER_URL", "http://testserver")

    result = CliRunner().invoke(app_module.app, ["tools", "list", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == ["alpha", "beta"]


def test_trailing_no_json_flag_forces_table(monkeypatch: pytest.MonkeyPatch) -> None:
    # The trailing ``--no-json`` overrides a flag-first ``--json`` back to the table.
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"data": ["alpha"]}))
    monkeypatch.setattr(
        AppContext,
        "client",
        lambda self: ApiClient(self.server_url, self.api_key, transport=transport),
    )
    monkeypatch.setenv("TAI_API_KEY", "test-key")
    monkeypatch.setenv("TAI_SERVER_URL", "http://testserver")

    result = CliRunner().invoke(app_module.app, ["--json", "tools", "list", "--no-json"])

    assert result.exit_code == 0, result.output
    assert "name" in result.output
    assert not result.output.lstrip().startswith("[")


def test_api_error_renders_as_clean_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    group = _slim(monkeypatch)

    @click.command("boom")
    def boom() -> None:
        raise ApiError("server refused the request", status_code=409)

    group.add_command(boom, "boom")
    result = CliRunner().invoke(group, ["boom"])

    assert result.exit_code != 0
    assert "server refused the request" in result.output


def test_entry_point_discovery_mounts_a_registered_command(monkeypatch: pytest.MonkeyPatch) -> None:
    def register(group: click.Group) -> None:
        @click.command("probe-ext")
        def probe_ext() -> None:  # pragma: no cover - only its registration matters
            pass

        group.add_command(probe_ext, "probe-ext")

    monkeypatch.setattr(app_module, "entry_points", lambda group: [_FakeEP("z-ext", register)])
    built = app_module._build_app()
    assert "probe-ext" in built.commands


def test_entry_point_discovery_propagates_a_failing_register(monkeypatch: pytest.MonkeyPatch) -> None:
    # A contributor that fails to register is a loud error, never swallowed.
    def register(group: click.Group) -> None:
        raise RuntimeError("register boom")

    monkeypatch.setattr(app_module, "entry_points", lambda group: [_FakeEP("bad", register)])
    with pytest.raises(RuntimeError, match="register boom"):
        app_module._build_app()


def test_entry_point_discovery_runs_in_sorted_name_order(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def make(name: str) -> _FakeEP:
        def register(group: click.Group) -> None:
            calls.append(name)

        return _FakeEP(name, register)

    eps = [make("charlie"), make("alpha"), make("bravo")]
    monkeypatch.setattr(app_module, "entry_points", lambda group: eps)
    app_module._build_app()
    assert calls == ["alpha", "bravo", "charlie"]


def test_slim_tree_has_no_contributed_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    built = _slim(monkeypatch)
    for name in _CONTRIBUTED:
        assert name not in built.commands, f"slim tree unexpectedly carries {name}"
    # The remote client + native commands still stand on their own.
    assert "tools" in built.commands
    assert "version" in built.commands
    assert "completion" in built.commands


def test_mount_launcher_adds_command_under_its_name(monkeypatch: pytest.MonkeyPatch) -> None:
    group = _slim(monkeypatch)

    @click.command("orig")
    def launcher() -> None:  # pragma: no cover - only its mounting matters
        pass

    app_module.mount_launcher(group, launcher, "mounted")
    assert "mounted" in group.commands
    assert group.commands["mounted"] is launcher
    assert launcher.name == "mounted"


def test_inject_json_flag_on_a_single_leaf() -> None:
    @click.command("leaf")
    def leaf() -> None:  # pragma: no cover - only its params matter
        pass

    app_module.inject_json_flag(leaf)
    assert any("--json" in (p.opts + p.secondary_opts) for p in leaf.params)


@pytest.mark.parametrize(
    ("raiser", "expected_code"),
    [
        (lambda: (_ for _ in ()).throw(click.exceptions.Exit(3)), 3),
        (lambda: (_ for _ in ()).throw(click.ClickException("bad usage")), 1),
    ],
)
def test_tai_group_translates_click_outcomes(monkeypatch: pytest.MonkeyPatch, raiser, expected_code: int) -> None:
    group = _slim(monkeypatch)

    @click.command("boom")
    def boom() -> None:
        raiser()

    group.add_command(boom, "boom")
    result = CliRunner().invoke(group, ["boom"])
    assert result.exit_code == expected_code


def test_tai_group_translates_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    group = _slim(monkeypatch)

    @click.command("boom")
    def boom() -> None:
        raise click.exceptions.Abort()

    group.add_command(boom, "boom")
    result = CliRunner().invoke(group, ["boom"])
    assert result.exit_code != 0


def _probe_group(monkeypatch: pytest.MonkeyPatch, calls: list[int]) -> click.Group:
    """A slim tree with ``load_dotenv`` stubbed to record calls, plus a no-op leaf
    so the root callback runs."""
    monkeypatch.setattr(app_module, "load_dotenv", lambda: calls.append(1))
    group = _slim(monkeypatch)

    @click.command("probe")
    def probe() -> None:  # pragma: no cover - only the callback's env gate matters
        pass

    group.add_command(probe, "probe")
    return group


def test_dotenv_loads_when_config_mode_file_or_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    group = _probe_group(monkeypatch, calls)

    monkeypatch.delenv("TAI_CONFIG_MODE", raising=False)
    assert CliRunner().invoke(group, ["probe"]).exit_code == 0

    monkeypatch.setenv("TAI_CONFIG_MODE", "file")
    assert CliRunner().invoke(group, ["probe"]).exit_code == 0

    assert calls == [1, 1]


def test_dotenv_skipped_under_k8s_after_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    group = _probe_group(monkeypatch, calls)

    for value in ("k8s", "K8S", " k8s "):
        monkeypatch.setenv("TAI_CONFIG_MODE", value)
        result = CliRunner().invoke(group, ["probe"])
        assert result.exit_code == 0, result.output

    assert calls == []


def test_invalid_config_mode_raises_naming_var_and_values(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    group = _probe_group(monkeypatch, calls)

    monkeypatch.setenv("TAI_CONFIG_MODE", "bogus")
    result = CliRunner().invoke(group, ["probe"])

    assert result.exit_code != 0
    assert "TAI_CONFIG_MODE" in result.output
    assert "file, k8s" in result.output
    assert calls == []
