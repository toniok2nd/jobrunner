"""Shared helpers — importable from any job or endpoint:

    from demo_utils import current_host, shout

Managed from the GUI: Libraries page.
"""

import socket


def current_host() -> str:
    """Hostname of the container running the task."""
    return socket.gethostname()


def shout(text: str) -> str:
    return text.upper() + "!"
