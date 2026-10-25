"""Fixture tool module for the dry-run combo check: the base tool ``echo``."""

from tai42_contract.app import tai42_app


@tai42_app.tools.tool
def echo(text: str) -> str:
    """Echo the text back."""
    return text
