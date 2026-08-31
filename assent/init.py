"""Initialize Assent user contracts and project support files safely."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import tomllib

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
_PROJECT_TESTS_BEGIN = "# --- Project test commands begin (project-owned) ---"
_PROJECT_TESTS_END = "# --- Project test commands end ---"


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


def _verifier_parts(content: str) -> tuple[str, str] | None:
    """Return the framework and project-owned block of a valid verifier."""
    lines = content.splitlines(keepends=True)
    begins = [index for index, line in enumerate(lines)
              if line.rstrip("\r\n") == _PROJECT_TESTS_BEGIN]
    ends = [index for index, line in enumerate(lines)
            if line.rstrip("\r\n") == _PROJECT_TESTS_END]
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
        return None
    return (
        "".join(lines[:begins[0]] + lines[ends[0] + 1:]),
        "".join(lines[begins[0] + 1:ends[0]]),
    )


def _with_project_tests(template: str, project_tests: str) -> str:
    """Place an existing project-owned block in the current framework."""
    lines = template.splitlines(keepends=True)
    begin = next(index for index, line in enumerate(lines)
                 if line.rstrip("\r\n") == _PROJECT_TESTS_BEGIN)
    end = next(index for index, line in enumerate(lines)
               if line.rstrip("\r\n") == _PROJECT_TESTS_END)
    return "".join(lines[:begin + 1]) + project_tests + "".join(lines[end:])


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
                             project_config_content: str | None = None,
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
        if project_config_content is not None:
            (temporary_project / "assent.toml").write_text(
                project_config_content,
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


def init(path: str | Path = ".") -> int:
    """Initialize the user home and a Git project.

    Returns a CLI-style exit code.  The shared settings and contracts
    live in ``~/.assent``; the project keeps only what is genuinely its own.
    """
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
        verifier_parts = _verifier_parts(verify_template)
        if verifier_parts is None:
            raise AssentError(
                "built-in verifier template has invalid project-test markers")
        verifier_framework, _template_project_tests = verifier_parts
        if verifier_exists:
            if not verifier.is_file():
                raise AssentError(f".assent/verify.py is not a file: {verifier}")
            existing_verifier = _read_file(verifier, ".assent/verify.py")
            existing_parts = _verifier_parts(existing_verifier)
            if existing_parts is None:
                raise AssentError(
                    f"{verifier} has invalid or duplicate project-test markers; "
                    "repair the project-owned verifier before rerunning init")
            existing_framework, project_tests = existing_parts
            if existing_framework == verifier_framework:
                verifier_content = existing_verifier
                verifier_plan = (
                    "preserved", f"{verifier} (framework already current)")
            elif _confirm(
                    f"{verifier} has a different verifier framework. Back it up "
                    "and update the framework while preserving its project-test "
                    "commands?", default=False):
                verifier_content = _with_project_tests(
                    verify_template, project_tests)
                backup = _backup_target(verifier)
                project_backups.append(
                    (verifier, verifier.read_bytes(), backup))
                verifier_plan = (
                    "updated",
                    f"{verifier} (framework updated; project-test commands "
                    "preserved; prior file backed up)")
            else:
                verifier_content = existing_verifier
                verifier_plan = (
                    "preserved", f"{verifier} (framework update declined)")
                warnings.append(
                    f"{verifier} has a different verifier framework and remains "
                    "unchanged")
        else:
            verifier_plan = (
                "created", f"{verifier} (project verification unconfigured)")
            verifier_content = verify_template

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

        project_config = assent_dir / "assent.toml"
        project_adapter = assent_dir / "adapter.toml"
        project_config_content: str | None = None
        project_config_plan: tuple[str, str] | None = None
        if project_config.exists():
            if not project_config.is_file():
                raise AssentError(
                    f"project settings override is not a file: {project_config}")
            existing_project_config = _read_file(
                project_config, "the project assent.toml")
            project_config_content = existing_project_config
            project_config_plan = (
                "preserved", f"{project_config} (local override, unchanged)")

        project_override_removals: list[Path] = []
        if project_adapter.exists():
            if not project_adapter.is_file():
                raise AssentError(
                    f"project settings override is not a file: {project_adapter}")
            if _confirm(
                    f"{project_adapter} overrides the current shared settings. "
                    "Back it up and remove it so the shared template applies?",
                    default=False):
                backup = _backup_target(project_adapter)
                project_backups.append(
                    (project_adapter, project_adapter.read_bytes(), backup))
                project_override_removals.append(project_adapter)
            else:
                warnings.append(
                    f"{project_adapter} remains a project override and can "
                    "shadow the current shared settings")

        agents_content, agents_plan = _agents_plan(root)
        gitignore_content, gitignore_plan = _gitignore_plan(root)

        _validate_planned_config(
            root, user_config_content, user_adapter_content,
            project_config_content=project_config_content,
            removed_project_overrides=frozenset(project_override_removals))

        # Every validation and read above happens before the first
        # write, so an unparsable settings file leaves neither
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
        if project_config_plan is not None and project_config_content is not None:
            _apply(project_config, project_config_content, project_config_plan)
        for project_override in project_override_removals:
            try:
                project_override.unlink()
            except OSError as e:
                raise AssentError(
                    f"Cannot remove project override {project_override}: {e}") from e
            print(f"  Removed: {project_override} (backed up first)")
        for project_override in (project_adapter,):
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
    print("  2. Fill in AGENTS.md's project description and hard constraints")
    print(f"  3. Start an interactive planning session: read "
          f"{contracts.instructions_path()}")
    print("  4. Have the planning AI configure .assent/verify.py and each "
          "plan's _runtime_test.toml")
    print("  5. Once assent check passes, run assent run")
    return 0
