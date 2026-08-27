"""Initialize Assent user contracts and project support files safely."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import tempfile
import tomllib
from collections.abc import Sequence

from assent import AssentError, contracts
from assent.user_home import user_assent_dir, user_config_path

_TEMPLATES = Path(__file__).resolve().parent / "templates"
_BRIDGE_MARKER_PREFIX = "<!-- assent-instructions"
_BRIDGE_BEGIN = "<!-- assent-instructions begin -->"
_BRIDGE_END = "<!-- assent-instructions end -->"
_BRIDGE_LINE = (
    "- When using assent, first read `~/.assent/instructions.md`, the global "
    "working instructions shared by every project; a scheduled worktree "
    "session uses the absolute path the scheduler provides."
)
_BRIDGE_BLOCK = "\n".join((_BRIDGE_BEGIN, _BRIDGE_LINE, _BRIDGE_END))
_GITIGNORE_LINES = [".assent/"]
_DIRECT_API_DEFAULT = object()
_PROJECT_TESTS_BEGIN = "# --- Project test commands begin (project-owned) ---"
_PROJECT_TESTS_END = "# --- Project test commands end ---"


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


def _verifier_framework(content: str) -> str | None:
    """Return verifier content outside its project-owned command block."""
    lines = content.splitlines(keepends=True)
    begins = [index for index, line in enumerate(lines)
              if line.rstrip("\r\n") == _PROJECT_TESTS_BEGIN]
    ends = [index for index, line in enumerate(lines)
            if line.rstrip("\r\n") == _PROJECT_TESTS_END]
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
        return None
    return "".join(lines[:begins[0]] + lines[ends[0] + 1:])


def _plan_file(path: Path, content: str, description: str
               ) -> tuple[str, str]:
    """Return the outcome for a managed file without changing it."""
    if path.exists():
        existing = _read_file(path, description)
        if existing == content:
            return "preserved", f"{path} (already current)"
        return "updated", str(path)
    return "created", str(path)


def _confirm(prompt: str, *, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{prompt} {suffix} ").strip().casefold()
    except (EOFError, KeyboardInterrupt) as e:
        raise AssentError("initialization choice cancelled or reached EOF") from e
    if not answer:
        return default
    if answer in {"y", "yes"}:
        return True
    if answer in {"n", "no"}:
        return False
    raise AssentError(f"expected y or n, got {answer!r}")


def _backup_target(path: Path) -> Path:
    """Return the single sibling backup replaced by each update."""
    return path.with_name(f"{path.name}.bak")


def _validate_planned_config(root: Path, user_config_content: str,
                             user_adapter_content: str | None, *,
                             removed_project_overrides: frozenset[Path] = frozenset()
                             ) -> None:
    """Load the exact planned user settings plus preserved project overrides."""
    from assent.config import load_config
    from assent.user_home import ASSENT_HOME_ENV

    project_config = root / ".assent" / "assent.toml"
    project_adapter = root / ".assent" / "adapter.toml"
    with tempfile.TemporaryDirectory(prefix="assent-init-validate-") as raw_temp:
        temporary = Path(raw_temp)
        temporary_user = temporary / "user"
        temporary_project = temporary / "project" / ".assent"
        temporary_user.mkdir(parents=True)
        temporary_project.mkdir(parents=True)
        (temporary_user / "assent.toml").write_text(
            user_config_content, encoding="utf-8", newline="\n")
        if user_adapter_content is not None:
            (temporary_user / "adapter.toml").write_text(
                user_adapter_content, encoding="utf-8", newline="\n")
        if (project_config.is_file()
                and project_config not in removed_project_overrides):
            (temporary_project / "assent.toml").write_text(
                _read_file(project_config, "the project assent.toml"),
                encoding="utf-8", newline="\n")
        if (project_adapter.is_file()
                and project_adapter not in removed_project_overrides):
            (temporary_project / "adapter.toml").write_text(
                _read_file(project_adapter, "the project adapter.toml"),
                encoding="utf-8", newline="\n")

        previous = os.environ.get(ASSENT_HOME_ENV)
        os.environ[ASSENT_HOME_ENV] = str(temporary_user)
        try:
            load_config(temporary_project / "assent.toml", "init-validation")
        finally:
            if previous is None:
                os.environ.pop(ASSENT_HOME_ENV, None)
            else:
                os.environ[ASSENT_HOME_ENV] = previous


def _agents_plan(root: Path) -> tuple[str, tuple[str, str]]:
    """Return the AGENTS.md plan with one Assent-owned bridge block."""
    target = root / "AGENTS.md"
    if not target.exists():
        content = _template("AGENTS.md").rstrip() + "\n\n" + _BRIDGE_BLOCK + "\n"
        return content, ("created", str(target))

    existing = _read_file(target, "AGENTS.md")
    lines = existing.splitlines()
    marker_lines = [
        index for index, line in enumerate(lines)
        if _BRIDGE_MARKER_PREFIX in line]
    begin_lines = [
        index for index, line in enumerate(lines) if _BRIDGE_BEGIN in line]
    end_lines = [
        index for index, line in enumerate(lines) if _BRIDGE_END in line]

    if marker_lines:
        valid_block = (
            len(marker_lines) == 2
            and len(begin_lines) == len(end_lines) == 1
            and lines[begin_lines[0]] == _BRIDGE_BEGIN
            and lines[end_lines[0]] == _BRIDGE_END
            and begin_lines[0] < end_lines[0]
        )
        if not valid_block:
            raise AssentError(
                f"{target} contains an invalid or duplicate Assent instructions "
                "bridge block; keep exactly one begin/end pair")
        updated_lines = (
            lines[:begin_lines[0]]
            + _BRIDGE_BLOCK.splitlines()
            + lines[end_lines[0] + 1:]
        )
        updated = "\n".join(updated_lines)
        if existing.endswith("\n"):
            updated += "\n"
        if updated == existing:
            return existing, (
                "preserved", f"{target} (instructions bridge already current)")
        return updated, ("updated", str(target))

    updated = existing.rstrip() + "\n\n" + _BRIDGE_BLOCK + "\n"
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


def _write_bytes(path: Path, content: bytes) -> None:
    """Write a byte-exact recovery copy atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.assent-new-{os.getpid()}")
    try:
        temporary.write_bytes(content)
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
    """Initialize the user home and a Git project.

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
        warnings: list[str] = []
        user_backups: list[tuple[Path, bytes, Path]] = []
        project_backups: list[tuple[Path, bytes, Path]] = []
        verifier_exists = verifier.exists()
        verify_template = _template("verify.py")
        verifier_framework = _verifier_framework(verify_template)
        if verifier_framework is None:
            raise AssentError(
                "built-in verifier template has invalid project-test markers")
        if verifier_exists:
            if not verifier.is_file():
                raise AssentError(f".assent/verify.py is not a file: {verifier}")
            existing_verifier = _read_file(verifier, ".assent/verify.py")
            explicit_selection = test is not None and not direct_api_default
            if explicit_selection:
                selection = _resolve_selection(test)
                verifier_content = _render_verifier(verify_template, selection)
                if verifier_content == existing_verifier:
                    verifier_plan = (
                        "preserved", f"{verifier} (already current)")
                else:
                    backup = _backup_target(verifier)
                    project_backups.append(
                        (verifier, verifier.read_bytes(), backup))
                    verifier_plan = (
                        "updated",
                        f"{verifier} ({selection.label} selected; prior file backed up)")
            else:
                existing_framework = _verifier_framework(existing_verifier)
                if existing_framework == verifier_framework:
                    verifier_content = existing_verifier
                    verifier_plan = (
                        "preserved", f"{verifier} (framework already current)")
                elif _confirm(
                        f"{verifier} has a different verifier framework. "
                        "Back it up and replace it?", default=False):
                    selection = _resolve_selection(None)
                    verifier_content = _render_verifier(
                        verify_template, selection)
                    backup = _backup_target(verifier)
                    project_backups.append(
                        (verifier, verifier.read_bytes(), backup))
                    verifier_plan = (
                        "updated",
                        f"{verifier} ({selection.label} selected; prior file backed up)")
                else:
                    verifier_content = existing_verifier
                    verifier_plan = (
                        "preserved", f"{verifier} (replacement declined)")
                    warnings.append(
                        f"{verifier} has a different verifier framework "
                        "and remains unchanged")
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
            if existing_user_config == config_template:
                user_config_content = existing_user_config
                user_config_plan = (
                    "preserved", f"{user_config} (already current)")
            elif _confirm(
                    f"{user_config} differs from the current shared settings "
                    "template. Back it up and replace it?", default=False):
                user_config_content = config_template
                backup = _backup_target(user_config)
                user_backups.append(
                    (user_config, user_config.read_bytes(), backup))
                user_config_plan = (
                    "updated",
                    f"{user_config} (replaced; prior file backed up)")
            else:
                user_config_content = existing_user_config
                user_config_plan = (
                    "preserved", f"{user_config} (replacement declined)")
                warnings.append(
                    f"{user_config} differs from the current shared settings "
                    "template and may retain an older workflow")

            try:
                inline_adapter = "adapter" in tomllib.loads(user_config_content)
            except tomllib.TOMLDecodeError:
                inline_adapter = False
            if user_adapter.exists():
                existing_user_adapter = _read_file(
                    user_adapter, "the user adapter.toml")
                if existing_user_adapter == adapter_template:
                    user_adapter_content = existing_user_adapter
                    user_adapter_plan = (
                        "preserved", f"{user_adapter} (already current)")
                elif _confirm(
                        f"{user_adapter} differs from the current shared adapter "
                        "template. Back it up and replace it?", default=False):
                    user_adapter_content = adapter_template
                    backup = _backup_target(user_adapter)
                    user_backups.append(
                        (user_adapter, user_adapter.read_bytes(), backup))
                    user_adapter_plan = (
                        "updated",
                        f"{user_adapter} (replaced; prior file backed up)")
                else:
                    user_adapter_content = existing_user_adapter
                    user_adapter_plan = (
                        "preserved", f"{user_adapter} (replacement declined)")
                    warnings.append(
                        f"{user_adapter} differs from the current shared adapter "
                        "template and remains unchanged")
            elif not inline_adapter:
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

        project_override_removals: list[Path] = []
        project_config = assent_dir / "assent.toml"
        project_adapter = assent_dir / "adapter.toml"
        for project_override in (project_config, project_adapter):
            if not project_override.exists():
                continue
            if not project_override.is_file():
                raise AssentError(
                    f"project settings override is not a file: {project_override}")
            if _confirm(
                    f"{project_override} overrides the current shared settings. "
                    "Back it up and remove it so the shared template applies?",
                    default=False):
                backup = _backup_target(project_override)
                project_backups.append(
                    (project_override, project_override.read_bytes(), backup))
                project_override_removals.append(project_override)
            else:
                warnings.append(
                    f"{project_override} remains a project override and can "
                    "shadow the current shared settings")

        agents_content, agents_plan = _agents_plan(root)
        gitignore_content, gitignore_plan = _gitignore_plan(root)

        _validate_planned_config(
            root, user_config_content, user_adapter_content,
            removed_project_overrides=frozenset(project_override_removals))

        # Every validation and read above happens before the first
        # write, so a bad selection or an unparsable TOML file leaves neither
        # the user home nor the project partially initialized.
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
        for source, content, backup in user_backups:
            _write_bytes(backup, content)
            print(f"  Backed up: {source} -> {backup}")
        _apply(user_config, user_config_content, user_config_plan)
        if user_adapter_content is not None and user_adapter_plan is not None:
            _apply(user_adapter, user_adapter_content, user_adapter_plan)

        print(f"Project {root}:")
        for source, content, backup in project_backups:
            _write_bytes(backup, content)
            print(f"  Backed up: {source} -> {backup}")
        _apply(verifier, verifier_content, verifier_plan)
        _apply(root / "AGENTS.md", agents_content, agents_plan)
        _apply(root / ".gitignore", gitignore_content, gitignore_plan)
        for project_override in project_override_removals:
            try:
                project_override.unlink()
            except OSError as e:
                raise AssentError(
                    f"Cannot remove project override {project_override}: {e}") from e
            print(f"  Removed: {project_override} (backed up first)")
        for project_override in (project_config, project_adapter):
            if (project_override.exists()
                    and project_override not in project_override_removals):
                print(f"  Preserved: {project_override} (local override, unchanged)")
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
    print(f"  4. Start an interactive planning session: read "
          f"{contracts.instructions_path()}")
    print("  5. Once assent check passes, run assent run")
    return 0
