"""Generate and safely upgrade the user home and a project's ``.assent``.

The two contracts and the shared settings belong to the machine's ``~/.assent``:
initialization refreshes the contracts to this installation's packaged text and
adds only missing active settings to the operator's configuration, never
replacing a stated value.  Adapter settings are generated in the sibling
``adapter.toml`` while an existing inline ``assent.toml`` remains supported.  A
project keeps only what is genuinely its own -- the verifier, whose test is
chosen once before a fresh skeleton is written, plus the AGENTS bridge and the
ignore entry.  An older project copy of a contract is removed only when it
matches the packaged text exactly, and an existing project ``assent.toml`` is
preserved untouched as a higher-priority override.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import tomllib
from collections import defaultdict
from collections.abc import Sequence

from assent import AssentError, contracts
from assent.user_home import user_assent_dir, user_config_path

_TEMPLATES = Path(__file__).resolve().parent / "templates"
_BRIDGE_MARKER = "<!-- assent-instructions -->"
_BRIDGE_LINE = (
    "- When using assent, first read `~/.assent/instructions.md`, the global "
    "working instructions shared by every project; a scheduled worktree "
    "session uses the absolute path the scheduler provides. An AI session "
    "never initiates the full suite or `.assent/verify.py`; the scheduler owns "
    "workflow `full_verify`, and an interactive session runs complete "
    "verification only when the human explicitly requests it. "
    f"{_BRIDGE_MARKER}"
)
_GITIGNORE_LINES = [".assent/"]
_DIRECT_API_DEFAULT = object()


@dataclass(frozen=True)
class _TestSelection:
    """The one project-test line activated in a generated verifier."""

    label: str
    verifier_line: str


_BUILTIN_TESTS = {
    "unittest": _TestSelection(
        "parallel unittest", "run_unittest_parallel()"),
    "pytest": _TestSelection("pytest", 'run("pytest")'),
    "npm": _TestSelection("npm test", 'run("npm", "test")'),
    "flutter": _TestSelection("Flutter test", 'run("flutter", "test")'),
    "dotnet": _TestSelection("dotnet test", 'run("dotnet", "test")'),
    "maven": _TestSelection("Maven test", 'run("mvn", "test")'),
    "gradle": _TestSelection("Gradle test", 'run("gradle", "test")'),
    "cmake-ctest": _TestSelection(
        "CMake/CTest",
        'run("ctest", "--test-dir", "build", "--output-on-failure")'),
    "make": _TestSelection("Make test", 'run("make", "test")'),
}
_BUILTIN_ALIASES = {
    "1": "unittest",
    "parallel-unittest": "unittest",
    "parallel unittest": "unittest",
    "unittest-parallel": "unittest",
    "parallel_unittest": "unittest",
    "python-unittest": "unittest",
    "python unittest": "unittest",
    "2": "pytest",
    "3": "npm",
    "npm-test": "npm",
    "npm test": "npm",
    "4": "flutter",
    "flutter-test": "flutter",
    "flutter test": "flutter",
    "5": "dotnet",
    "dotnet-test": "dotnet",
    "dotnet test": "dotnet",
    "6": "maven",
    "mvn": "maven",
    "mvn-test": "maven",
    "mvn test": "maven",
    "7": "gradle",
    "gradle-test": "gradle",
    "gradle test": "gradle",
    "8": "cmake-ctest",
    "cmake": "cmake-ctest",
    "ctest": "cmake-ctest",
    "cmake-test": "cmake-ctest",
    "cmake ctest": "cmake-ctest",
    "9": "make",
    "make-test": "make",
    "make test": "make",
}
_CUSTOM_ALIASES = {"0", "custom", "custom-command", "custom command"}
_MENU = (
    ("0", "Custom command (argv passed to run(...))"),
    ("1", "Parallel unittest (run_unittest_parallel())"),
    ("2", "pytest (run(\"pytest\"))"),
    ("3", "npm test (run(\"npm\", \"test\"))"),
    ("4", "Flutter test (run(\"flutter\", \"test\"))"),
    ("5", "dotnet test (run(\"dotnet\", \"test\"))"),
    ("6", "Maven test (run(\"mvn\", \"test\"))"),
    ("7", "Gradle test (run(\"gradle\", \"test\"))"),
    ("8", "CMake/CTest (run(\"ctest\", \"--test-dir\", \"build\", "
         "\"--output-on-failure\"))"),
    ("9", "Make test (run(\"make\", \"test\"))"),
)


def _template(name: str) -> str:
    path = _TEMPLATES / name
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        raise AssentError(
            f"Cannot read built-in template {name}: {e} (broken install?)") from e


def _read_file(path: Path, description: str) -> str:
    if path.exists() and not path.is_file():
        raise AssentError(f"{description} is not a file: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        raise AssentError(f"Cannot read {description} {path}: {e}") from e


def _normalise_choice(value: str) -> str:
    return " ".join(value.strip().casefold().replace("_", "-").split())


def _custom_selection(command: str | Sequence[str]) -> _TestSelection:
    """Parse a command into argv and render it without shell execution."""
    if isinstance(command, str):
        if not command.strip():
            raise AssentError("custom test command is empty")
        try:
            argv = shlex.split(command, posix=True)
        except ValueError as e:
            raise AssentError(
                f"custom test command is malformed: {e}") from e
    else:
        argv = list(command)

    if not argv or not isinstance(argv[0], str) or not argv[0].strip():
        raise AssentError("custom test command is empty")
    for argument in argv:
        if not isinstance(argument, str) or any(
                ord(char) < 0x20 or ord(char) == 0x7F
                for char in argument):
            raise AssentError(
                "custom test command contains a control character")

    rendered = ", ".join(json.dumps(argument, ensure_ascii=False)
                          for argument in argv)
    return _TestSelection("custom command", f"run({rendered})")


def _selection_from_values(values: Sequence[str]) -> _TestSelection:
    if not values:
        raise AssentError(
            "invalid test selection; choose 0-9, unittest, pytest, npm, "
            "flutter, dotnet, maven, gradle, cmake-ctest, make, or "
            "custom:<command>")
    if not all(isinstance(value, str) for value in values):
        raise AssentError("test selection values must be strings")

    first = values[0]
    normalised = _normalise_choice(first)
    builtin = _BUILTIN_ALIASES.get(normalised, normalised)
    if builtin in _BUILTIN_TESTS:
        if len(values) != 1:
            raise AssentError(
                f"test selection {first!r} does not accept a command")
        return _BUILTIN_TESTS[builtin]

    if normalised in _CUSTOM_ALIASES:
        if len(values) == 1:
            raise AssentError(
                "custom test selection requires a command; use "
                "--test custom:<command>")
        command_values = values[1:]
        if len(command_values) == 1:
            return _custom_selection(command_values[0])
        return _custom_selection(command_values)

    for prefix in ("custom:", "custom="):
        if normalised.startswith(prefix):
            command_text = first.strip()[len(prefix):]
            if len(values) > 1:
                command_text = " ".join((command_text, *values[1:]))
            return _custom_selection(command_text)

    # A multi-value form is a direct argv custom command.  A quoted command
    # line is also accepted for convenience, but a lone unknown word is an
    # invalid choice rather than silently becoming a custom test.
    if len(values) > 1:
        return _custom_selection(values)
    if any(char.isspace() for char in first.strip()):
        return _custom_selection(first)
    raise AssentError(
        f"invalid test selection {first!r}; choose 0-9, unittest, pytest, "
        "npm, flutter, dotnet, maven, gradle, cmake-ctest, make, or "
        "custom:<command>")


def _interactive_selection() -> _TestSelection:
    print("Choose the project's test command before assent writes its skeleton:")
    for number, description in _MENU:
        print(f"  {number}. {description}")
    try:
        choice = input("Test choice [0-9]: ").strip()
    except (EOFError, KeyboardInterrupt) as e:
        raise AssentError("test selection cancelled or reached EOF") from e

    if _normalise_choice(choice) in _CUSTOM_ALIASES:
        try:
            command = input("Custom test command: ")
        except (EOFError, KeyboardInterrupt) as e:
            raise AssentError("custom test command cancelled or reached EOF") from e
        return _custom_selection(command)
    return _selection_from_values([choice])


def _resolve_selection(test: str | Sequence[str] | None) -> _TestSelection:
    if test is None:
        return _interactive_selection()
    if isinstance(test, str):
        return _selection_from_values([test])
    return _selection_from_values(test)


def _render_verifier(template: str, selection: _TestSelection) -> str:
    """Activate exactly one commented project-test example in the template."""
    markers = (
        "# run_unittest_parallel()",
        '# run("pytest")',
        '# run("npm", "test")',
        '# run("flutter", "test")',
        '# run("dotnet", "test")',
        '# run("mvn", "test")',
        '# run("gradle", "test")',
        '# run("ctest", "--test-dir", "build", "--output-on-failure")',
        '# run("make", "test")',
    )
    active_markers = [marker for marker in markers if marker in template]
    if len(active_markers) != len(markers):
        raise AssentError(
            "built-in verifier template is missing a project-test example")
    marker = {
        "parallel unittest": "# run_unittest_parallel()",
        "pytest": '# run("pytest")',
        "npm test": '# run("npm", "test")',
        "Flutter test": '# run("flutter", "test")',
        "dotnet test": '# run("dotnet", "test")',
        "Maven test": '# run("mvn", "test")',
        "Gradle test": '# run("gradle", "test")',
        "CMake/CTest": '# run("ctest", "--test-dir", "build", "--output-on-failure")',
        "Make test": '# run("make", "test")',
    }.get(selection.label)
    if marker is None:
        # A custom command has no fixed marker; it replaces the Python unittest
        # example, leaving every other packaged example commented.
        marker = "# run_unittest_parallel()"
    if template.count(marker) != 1:
        raise AssentError(
            f"built-in verifier template has an ambiguous test example: {marker}")
    return template.replace(marker, selection.verifier_line, 1)


def _plan_file(path: Path, content: str, description: str
               ) -> tuple[str, str]:
    """Return the outcome for a managed file without changing it."""
    if path.exists():
        existing = _read_file(path, description)
        if existing == content:
            return "preserved", f"{path} (already current)"
        return "updated", str(path)
    return "created", str(path)


def _split_toml_path(raw: str) -> tuple[str, ...]:
    """Split a simple TOML dotted key/header path without a third party parser."""
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for char in raw.strip():
        if quote == '"':
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = None
            continue
        if quote == "'":
            current.append(char)
            if char == "'":
                quote = None
            continue
        if char in ('"', "'"):
            quote = char
            current.append(char)
        elif char == ".":
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if quote is not None:
        raise AssentError(f"invalid TOML key path: {raw!r}")
    parts.append("".join(current).strip())

    result: list[str] = []
    for part in parts:
        if not part:
            raise AssentError(f"invalid TOML key path: {raw!r}")
        if part[0] == part[-1] == '"':
            try:
                value = json.loads(part)
            except json.JSONDecodeError as e:
                raise AssentError(f"invalid TOML key path: {raw!r}") from e
        elif part[0] == part[-1] == "'":
            value = part[1:-1].replace("''", "'")
        else:
            value = part
        result.append(value)
    return tuple(result)


def _assignment_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("["):
        return None
    quote: str | None = None
    escaped = False
    for index, char in enumerate(stripped):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = None
        elif quote == "'":
            if char == "'":
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char == "=":
            lhs = stripped[:index].strip()
            return lhs or None
    return None


def _template_config_entries(text: str) -> list[tuple[tuple[str, ...], str]]:
    """List active template assignments as (table path, source block)."""
    current: tuple[str, ...] = ()
    entries: list[tuple[tuple[str, ...], str]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        if stripped.startswith("["):
            match = re.match(r"^\[([^\[].*?)\]\s*(?:#.*)?$", stripped)
            if not match:
                raise AssentError(
                    f"built-in config template has an invalid table: {line}")
            current = _split_toml_path(match.group(1))
            index += 1
            continue
        key = _assignment_key(line)
        if key is not None:
            block = [line.rstrip()]
            while True:
                try:
                    tomllib.loads("\n".join(block))
                    break
                except tomllib.TOMLDecodeError:
                    index += 1
                    if index >= len(lines):
                        raise AssentError(
                            f"built-in config template has an incomplete value: {line}")
                    block.append(lines[index].rstrip())
            entries.append((
                current + _split_toml_path(key), "\n".join(block)))
        index += 1
    return entries


def _header_path(line: str) -> tuple[str, ...] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("[["):
        return None
    match = re.match(r"^\[([^\[].*?)\]\s*(?:#.*)?$", stripped)
    if not match:
        return None
    return _split_toml_path(match.group(1))


def _lookup(data: object, path: tuple[str, ...]) -> bool:
    current = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    return True


def _format_toml_key(key: str) -> str:
    if re.match(r"^[A-Za-z0-9_-]+$", key):
        return key
    return json.dumps(key, ensure_ascii=False)


def _merge_config(existing: str, template: str, description: str
                  ) -> tuple[str, int]:
    """Add missing active template settings while retaining existing TOML text."""
    try:
        existing_data = tomllib.loads(existing)
    except tomllib.TOMLDecodeError as e:
        raise AssentError(f"{description} is not valid TOML: {e}") from e
    try:
        tomllib.loads(template)
    except tomllib.TOMLDecodeError as e:
        raise AssentError(f"built-in assent.toml is not valid TOML: {e}") from e

    template_entries = _template_config_entries(template)
    missing: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for full_path, source_line in template_entries:
        if not _lookup(existing_data, full_path):
            missing[full_path[:-1]].append(source_line)
    if not missing:
        return existing, 0

    lines = existing.splitlines()
    headers: list[tuple[int, tuple[str, ...]]] = []
    for index, line in enumerate(lines):
        path = _header_path(line)
        if path is not None:
            headers.append((index, path))
    header_positions = {path: index for index, path in headers}

    template_table_order: dict[tuple[str, ...], int] = {}
    for entry_index, (full_path, _source_line) in enumerate(template_entries):
        template_table_order.setdefault(full_path[:-1], entry_index)

    insertions: dict[int, list[tuple[tuple[int, int], list[str]]]] = defaultdict(list)
    for table, values in missing.items():
        if table in header_positions:
            insert_at = next(
                (index for index, _path in headers
                 if index > header_positions[table]),
                len(lines),
            )
            order = (0, header_positions[table])
            insertions[insert_at].append((order, values))
            continue

        descendants = [
            index for index, path in headers
            if len(path) > len(table) and path[:len(table)] == table
        ]
        insert_at = min(descendants, default=len(lines))
        block = [
            "[" + ".".join(_format_toml_key(key) for key in table) + "]",
            *values,
        ]
        order = (1, template_table_order.get(table, len(template_entries)))
        insertions[insert_at].append((order, block))

    for index in sorted(insertions, reverse=True):
        block: list[str] = []
        for _order, group in sorted(insertions[index], key=lambda item: item[0]):
            if block and block[-1] != "":
                block.append("")
            block.extend(group)
        if index > 0 and lines[index - 1].strip() and block and block[0] != "":
            block = [""] + block
        lines[index:index] = block

    merged = "\n".join(lines) + "\n"
    try:
        tomllib.loads(merged)
    except tomllib.TOMLDecodeError as e:
        raise AssentError(
            f"the merged {description} is not valid TOML: {e}") from e
    return merged, sum(len(values) for values in missing.values())


def _agents_plan(root: Path) -> tuple[str, tuple[str, str]]:
    """Return the AGENTS.md content and outcome, bridge line included once."""
    target = root / "AGENTS.md"
    if not target.exists():
        content = _template("AGENTS.md").rstrip() + "\n\n" + _BRIDGE_LINE + "\n"
        return content, ("created", str(target))
    existing = _read_file(target, "AGENTS.md")
    if _BRIDGE_MARKER in existing:
        # An older init wrote a bridge pointing at the project copy of the
        # instructions; replace that one line in place and leave every other
        # line of the project's own AGENTS.md exactly as its author wrote it.
        updated = "\n".join(
            _BRIDGE_LINE if _BRIDGE_MARKER in line else line
            for line in existing.splitlines())
        if existing.endswith("\n"):
            updated += "\n"
    else:
        updated = existing.rstrip() + "\n\n" + _BRIDGE_LINE + "\n"
    if updated == existing:
        return existing, ("preserved",
                          f"{target} (instructions bridge already current)")
    return updated, ("updated", str(target))


def _gitignore_plan(root: Path) -> tuple[str, tuple[str, str]]:
    """Return the .gitignore content and outcome, assent entry included once."""
    target = root / ".gitignore"
    if target.exists() and not target.is_file():
        raise AssentError(f".gitignore is not a file: {target}")
    existing = _read_file(target, ".gitignore") if target.exists() else ""
    lines = existing.splitlines()
    have = {line.strip() for line in lines}
    missing = [line for line in _GITIGNORE_LINES if line not in have]
    if not missing:
        return existing, ("preserved", f"{target} (assent entry already present)")
    if lines and lines[-1]:
        lines.append("")
    if lines:
        lines.append("# assent management surface and runtime output")
    lines.extend(missing)
    content = "\n".join(lines) + "\n"
    return content, ("updated" if target.exists() else "created", str(target))


def _write(path: Path, content: str) -> None:
    """Write one managed file atomically, so no reader sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.assent-new-{os.getpid()}")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    except OSError as e:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise AssentError(f"Cannot write {path}: {e}") from e


def _apply(target: Path, content: str, plan: tuple[str, str]) -> None:
    """Carry out one planned file outcome and report it under its section."""
    state, description = plan
    if state == "preserved":
        print(f"  Preserved: {description}")
        return
    _write(target, content)
    print(f"  {state.title()}: {description}")


def init(path: str | Path = ".",
         test: str | Sequence[str] | None | object = _DIRECT_API_DEFAULT) -> int:
    """Initialize or upgrade the user home and a Git project.

    Returns a CLI-style exit code.  The shared settings and the two contracts
    live in ``~/.assent``; the project keeps only what is genuinely its own.
    """
    # The command-line dispatcher passes ``None`` when --test is omitted and
    # therefore gets the required interactive menu.  Keep direct library calls
    # made by older integrations deterministic instead of unexpectedly reading
    # their stdin; callers that want the menu can pass ``test=None`` explicitly.
    direct_api_default = test is _DIRECT_API_DEFAULT
    root = Path(path).resolve()
    if not root.is_dir():
        print(f"Error: directory does not exist: {root}")
        return 1
    if not (root / ".git").exists():
        print("This project has no git repository yet; run git init first")
        return 1

    assent_dir = root / ".assent"
    verifier = assent_dir / "verify.py"
    try:
        verifier_exists = verifier.exists()
        if verifier_exists and test is not None and not direct_api_default:
            raise AssentError(
                "refusing --test because .assent/verify.py already exists; "
                "repeat init preserves the existing project verifier")

        verify_template = _template("verify.py")
        if verifier_exists:
            if not verifier.is_file():
                raise AssentError(f".assent/verify.py is not a file: {verifier}")
            verifier_plan = ("preserved", f"{verifier} (project verifier preserved)")
            verifier_content = _read_file(verifier, ".assent/verify.py")
        else:
            selection = (_BUILTIN_TESTS["unittest"] if direct_api_default
                         else _resolve_selection(test))
            verifier_plan = ("created", f"{verifier} ({selection.label} selected)")
            verifier_content = _render_verifier(verify_template, selection)

        # The user home: settings the operator owns, contracts assent owns.
        user_dir = user_assent_dir()
        user_config = user_config_path()
        config_template = _template("assent.toml")
        adapter_template = _template("adapter.toml")
        user_adapter = user_config.with_name("adapter.toml")
        user_adapter_content: str | None = None
        user_adapter_plan: tuple[str, str] | None = None
        if user_config.exists():
            existing_user_config = _read_file(user_config, "the user assent.toml")
            user_config_content, added = _merge_config(
                existing_user_config,
                config_template, str(user_config))
            user_config_plan = (
                "updated", f"{user_config} (added {added} packaged setting(s))"
            ) if added else (
                "preserved", f"{user_config} (your settings preserved)")
            if "adapter" in tomllib.loads(existing_user_config):
                # Compatibility layout: do not move or duplicate an author's
                # existing inline adapter settings.
                if user_adapter.exists():
                    user_adapter_plan = (
                        "preserved",
                        f"{user_adapter} (existing adapter settings preserved)")
            elif user_adapter.exists():
                user_adapter_content, added = _merge_config(
                    _read_file(user_adapter, "the user adapter.toml"),
                    adapter_template, str(user_adapter))
                user_adapter_plan = (
                    "updated", f"{user_adapter} (added {added} packaged setting(s))"
                ) if added else (
                    "preserved", f"{user_adapter} (your settings preserved)")
            else:
                user_adapter_content = adapter_template
                user_adapter_plan = ("created", str(user_adapter))
        else:
            user_config_content = config_template
            user_config_plan = ("created", str(user_config))
            user_adapter_content = adapter_template
            user_adapter_plan = ("created", str(user_adapter))

        contract_plans = []
        for name in contracts.CONTRACT_NAMES:
            target = contracts.contract_path(name)
            contract_plans.append((
                name, _plan_file(target, contracts.installed_contract_text(name),
                                 f"the global {name}")))

        # The project: its own verifier, plus whatever an older layout left in
        # .assent that now belongs to the user home.
        warnings: list[str] = []
        legacy_plans: list[tuple[Path, bool, str]] = []
        for name in contracts.CONTRACT_NAMES:
            legacy = assent_dir / name
            if not legacy.exists():
                continue
            existing = _read_file(legacy, f".assent/{name}")
            global_path = contracts.contract_path(name)
            if existing == contracts.installed_contract_text(name):
                legacy_plans.append((
                    legacy, True,
                    f"{legacy} (an exact managed copy; {global_path} now applies)"))
            else:
                legacy_plans.append((
                    legacy, False,
                    f"{legacy} (differs from the packaged {name}, so it is kept)"))
                warnings.append(
                    f"{legacy} differs from this installation's {name} and was "
                    f"not removed; sessions read {global_path}, so delete the "
                    "local copy once you have moved anything you still want")

        project_config = assent_dir / "assent.toml"
        if project_config.exists():
            if not project_config.is_file():
                raise AssentError(
                    f".assent/assent.toml is not a file: {project_config}")
            warnings.append(
                f"{project_config} is kept byte-for-byte as a compatibility "
                f"override; it outranks {user_config} and can shadow later "
                "edits there")

        agents_content, agents_plan = _agents_plan(root)
        gitignore_content, gitignore_plan = _gitignore_plan(root)

        # Every validation, merge, and read above happens before the first
        # write, so a bad selection or an unparsable TOML file leaves neither
        # the user home nor the project half-upgraded.
        try:
            user_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise AssentError(f"Cannot create the user assent home {user_dir}: {e}") from e
        print(f"User home {user_dir}:")
        for name, (state, description) in contract_plans:
            if state == "preserved":
                print(f"  Preserved: {description}")
                continue
            contracts.install_contract(name)
            print(f"  {state.title()}: {description}")
        _apply(user_config, user_config_content, user_config_plan)
        if user_adapter_content is not None and user_adapter_plan is not None:
            _apply(user_adapter, user_adapter_content, user_adapter_plan)

        print(f"Project {root}:")
        _apply(verifier, verifier_content, verifier_plan)
        _apply(root / "AGENTS.md", agents_content, agents_plan)
        _apply(root / ".gitignore", gitignore_content, gitignore_plan)
        if project_config.exists():
            print(f"  Preserved: {project_config} (local override, unchanged)")
        for legacy, removable, description in legacy_plans:
            if not removable:
                print(f"  Preserved: {description}")
                continue
            try:
                legacy.unlink()
            except OSError as e:
                raise AssentError(f"Cannot remove {legacy}: {e}") from e
            print(f"  Removed: {description}")
    except (AssentError, OSError) as e:
        print(f"Refused: {e}")
        return 1

    for warning in warnings:
        print(f"Warning: {warning}")
    print()
    print("Next steps:")
    print(f"  1. Review the shared settings in {user_config}; every project on "
          "this machine reads them")
    print("  2. Add the selected project's tests and keep the generated "
          ".assent/verify.py check enabled")
    print("  3. Fill in AGENTS.md's project description and hard constraints")
    print(f"  4. Start an AI meeting: read {contracts.instructions_path()} and "
          "begin an assent planning meeting")
    print("  5. Once assent check passes, run assent run")
    return 0
