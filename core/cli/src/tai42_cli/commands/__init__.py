"""Remote command groups — thin clients over a tai42 server's ``/api/*`` routes.

Each module exposes a ``typer.Typer`` app named ``app`` that the root CLI
registers as a subcommand group. The command bodies are added per domain.
"""
