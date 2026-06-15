"""Typer CLI for running and managing Bob.

The CLI is split into one module per subapp under this package; this file is
the entry point that wires them all together. Console-script hook:
``bob = "bob_server.cli:app"``.

For backwards compatibility with callers (and tests) that did
``from bob_server import cli`` and accessed ``cli.serve``, ``cli.urlopen``,
etc., this module re-exports the helpers from :mod:`._helpers` and the
service-command functions from :mod:`.service_cmds`.
"""

from __future__ import annotations

import typer

# Bring all helpers + stdlib re-exports into the cli namespace so legacy
# `cli.X` references keep working.
from bob_server.cli._helpers import *  # noqa: F403,F405
from bob_server.cli._helpers import __all__ as _helpers_all
from bob_server.cli.service_cmds import (  # noqa: F401
    install, uninstall, start, stop, restart, status, logs, serve,
    register as _register_service_commands,
)


app = typer.Typer(help="Bob - Bob's memory and communication service.")
_register_service_commands(app)


# Subapp registration
from bob_server.cli.contacts import app as contact_app  # noqa: E402
from bob_server.cli.memory_cmds import app as memory_app  # noqa: E402
from bob_server.cli.session_routes import app as session_route_app  # noqa: E402
from bob_server.cli.calendars import app as calendar_app  # noqa: E402
from bob_server.cli.events import app as event_app  # noqa: E402
from bob_server.cli.context_cmds import app as context_app  # noqa: E402
from bob_server.cli.webhooks import app as webhook_app  # noqa: E402
from bob_server.cli.email_cmds import app as email_app  # noqa: E402
from bob_server.cli.calls import app as phone_app  # noqa: E402
from bob_server.cli.openai_cmds import app as openai_app  # noqa: E402
from bob_server.cli.eval_cmds import app as eval_app  # noqa: E402
from bob_server.cli.whatsapp_cmds import app as whatsapp_app  # noqa: E402

app.add_typer(contact_app, name="contact")
app.add_typer(memory_app, name="memory")
app.add_typer(session_route_app, name="session-route")
app.add_typer(calendar_app, name="calendar")
app.add_typer(event_app, name="event")
app.add_typer(context_app, name="context")
app.add_typer(webhook_app, name="webhook")
app.add_typer(email_app, name="email")
app.add_typer(phone_app, name="call")
app.add_typer(openai_app, name="openai")
app.add_typer(eval_app, name="eval")
app.add_typer(whatsapp_app, name="whatsapp")


def main() -> int:
    """CLI entry point for `python -m bob.cli`."""
    app()
    return 0


__all__ = list(_helpers_all) + [
    "app", "main",
    "install", "uninstall", "start", "stop", "restart", "status", "logs", "serve",
]
