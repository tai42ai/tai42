"""Import lightness of the ``tai42_kit.utils.lc`` package.

The package and its signature helpers must import on the bare install; only the
lc_util re-exports pull ``langchain_core`` (the ``llm`` extra), resolved lazily
on attribute access. Each subprocess hides the langchain stack with a blocking
meta-path finder that raises ``ModuleNotFoundError`` exactly as a missing
install would, so the tests hold regardless of what this environment has
installed. In-process tests cover the lazy path with langchain present.
"""

import subprocess
import sys
import textwrap

import pytest

from tai42_kit.utils import lc

_BLOCK_LANGCHAIN = textwrap.dedent(
    """
    import sys

    class _BlockLangchain:
        def find_spec(self, name, path=None, target=None):
            if name.partition(".")[0].startswith("langchain"):
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)
            return None

    sys.meta_path.insert(0, _BlockLangchain())
    """
)


def _run_without_langchain(child: str) -> subprocess.CompletedProcess[str]:
    code = _BLOCK_LANGCHAIN + textwrap.dedent(child)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)


def test_signature_util_imports_without_langchain():
    result = _run_without_langchain(
        """
        from tai42_kit.utils.lc import add_signature_params, exclude_fastmcp_ctx_from_kwargs
        from tai42_kit.utils.lc.signature_util import add_signature_params

        assert "langchain_core" not in sys.modules, sorted(m for m in sys.modules if "langchain" in m)
        """
    )
    assert result.returncode == 0, result.stderr


def test_lc_util_reexport_raises_extra_hint_without_langchain():
    # Touching an lc_util re-export without langchain must raise the loud
    # ModuleNotFoundError naming the extra — never a silent AttributeError
    # (which getattr-with-default would swallow).
    result = _run_without_langchain(
        """
        import tai42_kit.utils.lc as lc

        try:
            lc.mcp_tool_to_lc_tool
        except ModuleNotFoundError as exc:
            assert not isinstance(exc, AttributeError), type(exc)
            assert "tai42-kit[llm]" in str(exc), str(exc)
            assert exc.name == "langchain_core", exc.name
        else:
            raise SystemExit("expected ModuleNotFoundError, got the attribute")

        try:
            from tai42_kit.utils.lc import mcp_tools_to_lc_tools
        except ModuleNotFoundError as exc:
            assert "tai42-kit[llm]" in str(exc), str(exc)
        else:
            raise SystemExit("expected ModuleNotFoundError, got the import")
        """
    )
    assert result.returncode == 0, result.stderr


def test_lc_util_direct_import_raises_module_not_found_without_langchain():
    # Importing the lc_util module itself (not via the package re-export) still
    # fails loudly, naming the missing langchain module.
    result = _run_without_langchain(
        """
        try:
            import tai42_kit.utils.lc.lc_util
        except ModuleNotFoundError as exc:
            assert exc.name == "langchain_core", exc.name
        else:
            raise SystemExit("expected ModuleNotFoundError, got the import")
        """
    )
    assert result.returncode == 0, result.stderr


def test_lazy_reexports_resolve_with_langchain_installed():
    from tai42_kit.utils.lc.lc_util import mcp_tool_to_lc_tool, mcp_tools_to_lc_tools

    assert lc.mcp_tool_to_lc_tool is mcp_tool_to_lc_tool
    assert lc.mcp_tools_to_lc_tools is mcp_tools_to_lc_tools


def test_dir_lists_lazy_reexports():
    # dir() must show the lazy lc_util names alongside the eager ones, so the
    # public surface is introspectable before first attribute access.
    listed = dir(lc)
    assert "mcp_tool_to_lc_tool" in listed
    assert "mcp_tools_to_lc_tools" in listed
    assert "add_signature_params" in listed


def test_unknown_attribute_raises_attribute_error():
    with pytest.raises(AttributeError, match="no attribute 'nonexistent'"):
        lc.nonexistent  # noqa: B018


def test_missing_langchain_is_translated_to_extra_hint(monkeypatch: pytest.MonkeyPatch):
    # The lazy loader translates a missing langchain module into the extra hint,
    # chained onto the original error.
    original = ModuleNotFoundError("No module named 'langchain_core'", name="langchain_core")

    def raise_missing(module_name: str):
        raise original

    monkeypatch.setattr(lc, "import_module", raise_missing)
    with pytest.raises(ModuleNotFoundError, match=r"tai42-kit\[llm\]") as excinfo:
        lc.mcp_tool_to_lc_tool  # noqa: B018
    assert excinfo.value.__cause__ is original


def test_missing_non_langchain_module_propagates_untouched(monkeypatch: pytest.MonkeyPatch):
    # Any other missing module is a real defect, not a missing extra: it must
    # propagate as-is, never be relabeled with the llm-extra hint.
    original = ModuleNotFoundError("No module named 'mcp'", name="mcp")

    def raise_missing(module_name: str):
        raise original

    monkeypatch.setattr(lc, "import_module", raise_missing)
    with pytest.raises(ModuleNotFoundError) as excinfo:
        lc.mcp_tools_to_lc_tools  # noqa: B018
    assert excinfo.value is original
