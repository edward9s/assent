"""The canonical per-user assent directory.

User-wide settings live in ``~/.assent/assent.toml`` and apply to every project.
The location is resolved through this one helper so that a test, or any caller
that must not touch the developer's real home directory, can redirect it with
the ``ASSENT_HOME`` environment variable instead of monkeypatching ``Path.home``.
"""
from __future__ import annotations

import os
from pathlib import Path

# Set to an absolute directory to use it as ~/.assent; an empty value means "not set".
ASSENT_HOME_ENV = "ASSENT_HOME"
USER_CONFIG_NAME = "assent.toml"


def user_assent_dir() -> Path:
    """Return the user-wide ``.assent`` directory; the path need not exist."""
    override = os.environ.get(ASSENT_HOME_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".assent"


def user_config_path() -> Path:
    """Return the user-wide settings file; the path need not exist."""
    return user_assent_dir() / USER_CONFIG_NAME
