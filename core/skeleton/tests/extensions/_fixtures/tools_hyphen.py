"""Fixture tools registered under NON-IDENTIFIER names, for exercising an
extension branch whose raw ``__name__`` (``<tool>_monitor``) is not a valid
Python identifier: a hyphenated name and a leading-digit name. Invoking such a
branch drives its makefun validation wrapper, which must normalize the cosmetic
func name rather than emit an uncompilable ``def``."""

from tai42_contract.app import tai42_app


@tai42_app.tools.tool(name="my-tool")
def my_tool(text: str) -> str:
    """Echo the text."""
    return text


@tai42_app.tools.tool(name="2fa")
def two_fa(text: str) -> str:
    """Echo the text."""
    return text
