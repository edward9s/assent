"""The global AI contracts: one installed assent, one pair of contract texts.

``instructions.md`` (how an assent session behaves) and ``format.md`` (the plan
format) describe the tool, not any one project, so they are installed once into
the user-wide ``~/.assent`` directory and every project's session reads them
from there.  A project's own ``.assent`` keeps what genuinely belongs to it:
task folders, journals, receipts, the verifier, and the optional settings
override.

Both files are copies of this installation's packaged templates, so the only
questions worth asking are whether they are there and whether they belong to
this version.  This module answers exactly that and never merges, patches, or
rewrites what it finds: it reports, and ``assent init`` installs.
"""
from __future__ import annotations

from pathlib import Path

from assent import AssentError
from assent.user_home import user_assent_dir

INSTRUCTIONS_NAME = "instructions.md"
FORMAT_NAME = "format.md"
CONTRACT_NAMES = (INSTRUCTIONS_NAME, FORMAT_NAME)
CONTRACT_REMEDY = "run `assent init` to install the current global contracts"

_TEMPLATES = Path(__file__).resolve().parent / "templates"


def contract_dir() -> Path:
    """The user-wide directory holding the global contracts; it need not exist."""
    return user_assent_dir()


def contract_path(name: str) -> Path:
    """Absolute path of one global contract; the file need not exist."""
    if name not in CONTRACT_NAMES:
        raise AssentError(
            f"unknown global contract: {name!r}"
            f" (known: {', '.join(CONTRACT_NAMES)})")
    return contract_dir() / name


def instructions_path() -> Path:
    """Absolute path of the working instructions every session is pointed at."""
    return contract_path(INSTRUCTIONS_NAME)


def installed_contract_text(name: str) -> str:
    """The packaged text of one contract, as shipped by this installed assent."""
    path = _TEMPLATES / contract_path(name).name
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        raise AssentError(
            f"Cannot read the built-in {name} contract template: {e}"
            " (broken install?)") from e


def contract_errors() -> list[str]:
    """Name every global contract that is absent, unreadable, or out of date.

    Text mode reads both sides with universal newlines, so a file a Windows
    editor rewrote with CRLF still counts as the same contract; only the text
    itself decides.
    """
    errors: list[str] = []
    for name in CONTRACT_NAMES:
        path = contract_path(name)
        installed = installed_contract_text(name)
        if not path.exists():
            errors.append(f"{path} is missing")
            continue
        try:
            current = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as e:
            errors.append(f"{path} cannot be read: {e}")
            continue
        if current != installed:
            errors.append(
                f"{path} is stale (it differs from this installation's {name})")
    return errors


def require_contracts() -> None:
    """Fail closed unless both global contracts are present and current."""
    errors = contract_errors()
    if errors:
        raise AssentError("; ".join(errors) + f"; {CONTRACT_REMEDY}")
