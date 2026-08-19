"""One-way archival of a finished plan: clean first, then compress the plan.

``assent archive PLAN`` retires a plan that is no longer needed for active
work.  Archival strictly contains ``clean``: it reuses ``clean``'s mechanical proof
and removal for any still-present source branch/worktree, then compresses
``.assent/<PLAN>/`` into ``.assent/_archive/<PLAN>.zip``, records the plan in
the ``.assent/_archived.toml`` roster (so a downstream ``after`` reference stays
resolvable long after the live directory is gone), and finally deletes the live
directory.  The reverse is one-directional: ``clean`` never archives, because
cleaning is a mechanical judgement (proof of integration) while archiving is a human
one (this plan will not be consulted again).

Crash-safety and idempotency.  The durable steps are: (1) compress the live directory
into a temporary zip; (2) register the plan in the roster; (3) publish the zip by
renaming the temporary file onto its final name; (4) delete the live directory.  The
roster entry is written *before* the final zip is published on purpose: the roster is
the single authority on "this plan's archive is underway/committed", so a final zip
that exists with *no* roster entry can only be a foreign file and is refused rather
than clobbered.  Re-running ``archive`` after an interruption at any step resolves the
current on-disk state against the roster and finishes the remaining steps without
repeating completed ones.

No later verification ever trusts a hash stored in the roster: the project may rewrite
git history, so a recorded ``main_tip`` is human-readable evidence only.
"""
from __future__ import annotations

import json
import os
import shutil
import tomllib
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from assent import AssentError, gitops
from assent.clean import (clean_locked, has_cleanup_target,
                          sweep_orphaned_temporary_branches,
                          validate_live_plan_selection)
from assent.config import (Config, list_task_plans, load_config,
                           validate_config, validate_tasks_name)
from assent.plandeps import infer_plan_completion
from assent.lockfile import (LOCK_NAME, LockBusy, hold_lock,
                             hold_integration_lock)

_ARCHIVE_DIRNAME = "_archive"
_ROSTER_NAME = "_archived.toml"


@dataclass(frozen=True)
class ArchiveResult:
    """The outcome of archiving one plan, for exit codes and the ``--all`` summary."""

    status: str  # "archived" | "skipped" | "error"
    reason: str = ""


def _toml_basic_str(value: str) -> str:
    """Render a string as a TOML basic string.

    ``json.dumps`` emits exactly the escapes TOML basic strings accept for the
    values we store here (plan names, ISO timestamps, hex hashes), so it is a
    safe, dependency-free encoder.
    """
    return json.dumps(value, ensure_ascii=False)


def _archive_dir(assent_dir: Path) -> Path:
    return assent_dir / _ARCHIVE_DIRNAME


def _zip_path(assent_dir: Path, plan_name: str) -> Path:
    return _archive_dir(assent_dir) / f"{plan_name}.zip"


def _roster_path(assent_dir: Path) -> Path:
    return assent_dir / _ROSTER_NAME


def read_roster(assent_dir: str | Path) -> list[dict]:
    """Read the archive roster, failing closed on any malformed content.

    A missing roster file means nothing has been archived yet.  Each entry must be
    a table carrying at least the string keys ``plan`` and ``archived_at``; extra
    keys (such as a human-readable ``main_tip``) are preserved but never interpreted.
    """
    path = _roster_path(Path(assent_dir))
    if not path.is_file():
        return []
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise AssentError(f"Archive roster {path} is not valid TOML: {e}") from e
    raw = data.get("archived", [])
    if not isinstance(raw, list):
        raise AssentError(f"Archive roster {path} field archived must be an array of tables")
    entries: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise AssentError(f"Archive roster {path} has an entry that is not a table")
        plan_name = item.get("plan")
        archived_at = item.get("archived_at")
        if not isinstance(plan_name, str) or not plan_name:
            raise AssentError(f"Archive roster {path} entry is missing a string 'plan' field")
        if not isinstance(archived_at, str) or not archived_at:
            raise AssentError(
                f"Archive roster {path} entry {plan_name!r} is missing a string archived_at")
        entries.append(dict(item))
    return entries


def archive_recovery_names(assent_dir: str | Path,
                           plan_names: Sequence[str]) -> set[str]:
    """Return selected names recognized by archive recovery artifacts.

    A malformed roster is left for the archive operation to report.  It must
    not make a valid live plan fail the identity gate before that existing
    archive diagnostic can run.
    """
    assent_dir = Path(assent_dir)
    try:
        recognized = {
            entry["plan"] for entry in read_roster(assent_dir)
        }
    except AssentError:
        recognized = set()
    for plan_name in plan_names:
        try:
            validate_tasks_name(plan_name, "Command-line plan")
        except AssentError:
            continue
        if _zip_path(assent_dir, plan_name).is_file():
            recognized.add(plan_name)
    return recognized


def _write_roster(assent_dir: Path, entries: list[dict]) -> None:
    """Rewrite the roster atomically; remove the file entirely when it becomes empty."""
    path = _roster_path(assent_dir)
    if not entries:
        if path.exists():
            path.unlink()
        return
    lines = [
        "# assent archive roster: finished plans compressed into _archive/.",
        "# A downstream after reference resolves through this roster once the live",
        "# directory is gone. Any hash below is human-readable evidence only; assent",
        "# never trusts a roster hash for verification (git history may be rewritten).",
        "",
    ]
    for entry in entries:
        lines.append("[[archived]]")
        lines.append(f"plan = {_toml_basic_str(entry['plan'])}")
        lines.append(f"archived_at = {_toml_basic_str(entry['archived_at'])}")
        main_tip = entry.get("main_tip")
        if isinstance(main_tip, str) and main_tip:
            lines.append(f"main_tip = {_toml_basic_str(main_tip)}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _in_roster(entries: list[dict], plan_name: str) -> bool:
    return any(entry["plan"] == plan_name for entry in entries)


def _compress_plan(src_dir: Path, tmp_zip: Path) -> None:
    """Compress every file under ``src_dir`` into ``tmp_zip`` (paths relative to it).

    The plan lock (``assent.lock``) is a runtime management artifact, not plan
    content: archive creates and holds it for the duration of the operation and it
    disappears with the deleted live directory, so it is excluded here rather than
    captured as a stale lock inside the archive.
    """
    tmp_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(src_dir.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(src_dir).as_posix()
            if relative == LOCK_NAME:
                continue
            archive.write(path, relative)


def _unarchived_live_content(live_dir: Path, zip_path: Path) -> str | None:
    """Name live files the published archive does not already hold, or None.

    A resumable archive was interrupted between publishing the zip and deleting
    the directory the zip was compressed from, so every file still present must
    also be in the zip byte for byte; a partly deleted directory is a subset.
    Anything else is a different plan reusing an archived name, and deleting it
    would discard work no archive ever captured.  ``assent.lock`` is a runtime
    artifact ``_compress_plan`` never stores, so it is ignored here too.
    """
    try:
        with zipfile.ZipFile(zip_path) as archive:
            archived = {name: archive.read(name) for name in archive.namelist()}
    except (OSError, zipfile.BadZipFile) as e:
        return f"{zip_path.name} cannot be read ({e})"

    unarchived: list[str] = []
    for path in sorted(live_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(live_dir).as_posix()
        if relative == LOCK_NAME:
            continue
        try:
            if archived.get(relative) == path.read_bytes():
                continue
        except OSError as e:
            return f"{relative} cannot be read ({e})"
        unarchived.append(relative)
    return ", ".join(unarchived) or None


def _delete_plan_dir(path: Path) -> None:
    """Remove the live plan directory; a failure surfaces so a re-run can retry."""
    try:
        shutil.rmtree(path)
    except OSError as e:
        raise AssentError(f"could not remove live plan directory {path}: {e}") from e


def _do_archive_new(cfg: Config, entries: list[dict]) -> ArchiveResult:
    """Prove-and-clean, then compress and register, holding the plan lock.

    On success the live directory still exists and the caller deletes it after
    releasing the plan lock, so the deletion never races the open lock handle on
    Windows.  Returns ``skipped`` for any unmet precondition and ``error`` for a real
    failure (a failed clean step or a compression error).
    """
    name = cfg.tasks_name
    assent_dir = cfg.assent_dir

    completion = infer_plan_completion(cfg.tasks_dir)
    if not completion.complete:
        return ArchiveResult("skipped", completion.reason)

    # Reuse clean's proof and removal for any source still present; a plan whose
    # source is already gone (accepted and cleaned) skips this step.  When a source
    # remains that clean cannot prove safe to remove, archive refuses rather than
    # compressing a plan whose work is not yet integrated.
    if has_cleanup_target(cfg):
        clean_path = gitops.worktree_path(cfg.root, name)
        if clean_locked(cfg, clean_path) != 0:
            return ArchiveResult("error", "clean step failed (see above)")
        if has_cleanup_target(cfg):
            return ArchiveResult(
                "skipped",
                "source branch/worktree is not yet integrated, so clean retained it "
                "(see above); archive refuses to compress before it is removed")

    tmp_zip = _archive_dir(assent_dir) / f"{name}.zip.tmp"
    try:
        _compress_plan(cfg.tasks_dir, tmp_zip)
    except OSError as e:
        tmp_zip.unlink(missing_ok=True)
        return ArchiveResult("error", f"compression failed: {e}")

    # Register the roster entry before publishing the zip: the roster, not the file,
    # is the authority on a committed archive (see the module docstring).
    entry = {
        "plan": name,
        "archived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    main_tip = gitops.head_ref(cfg.root)
    if main_tip:
        entry["main_tip"] = main_tip
    _write_roster(assent_dir, entries + [entry])

    os.replace(tmp_zip, _zip_path(assent_dir, name))
    return ArchiveResult("archived")


def _resume_or_finish(cfg: Config, entries: list[dict]) -> ArchiveResult:
    """Resolve the current on-disk state against the roster and finish the archive.

    Every interruption point resolves here:
    - already in the roster: the archive is committed; ensure the zip exists (re-
      publishing from a leftover temp if a crash landed between register and rename)
      and remove the live directory if it is still present;
    - not in the roster but a final zip already exists: that zip is foreign (our own
      flow always registers before publishing), so refuse instead of clobbering it;
    - not in the roster and no final zip: a fresh archive.
    """
    name = cfg.tasks_name
    assent_dir = cfg.assent_dir
    live_dir = cfg.tasks_dir
    zip_path = _zip_path(assent_dir, name)

    if _in_roster(entries, name):
        if not zip_path.exists():
            tmp_zip = _archive_dir(assent_dir) / f"{name}.zip.tmp"
            if live_dir.is_dir():
                # Crash between register and publish: rebuild and publish the zip.
                try:
                    _compress_plan(live_dir, tmp_zip)
                except OSError as e:
                    tmp_zip.unlink(missing_ok=True)
                    return ArchiveResult("error", f"compression failed: {e}")
                os.replace(tmp_zip, zip_path)
            else:
                return ArchiveResult(
                    "error",
                    f"roster lists {name} but neither {zip_path.name} nor the live "
                    "directory exists; inspect the archive manually")
        if live_dir.exists():
            unarchived = _unarchived_live_content(live_dir, zip_path)
            if unarchived is not None:
                return ArchiveResult(
                    "error",
                    f"{name} is already in the archive roster, but the live "
                    f"directory holds content {zip_path.name} does not: "
                    f"{unarchived}. This is a new plan reusing an archived name, "
                    "not an interrupted archive; rename it (or restore and merge "
                    "the archive) instead of archiving over the old one")
            _delete_plan_dir(live_dir)
            return ArchiveResult("archived")
        return ArchiveResult("archived", "already archived")

    if zip_path.exists():
        return ArchiveResult(
            "skipped",
            f"a foreign archive {zip_path} already exists but {name} is not in the "
            "roster; restore or remove it before archiving")

    if not live_dir.is_dir():
        return ArchiveResult("skipped", f"no live plan directory {live_dir} to archive")

    return _do_archive_new(cfg, entries)


def _archive_one(cfg: Config) -> ArchiveResult:
    """Archive one plan under the integration lock, printing a single result line."""
    name = cfg.tasks_name
    try:
        with hold_integration_lock(cfg.assent_dir):
            try:
                entries = read_roster(cfg.assent_dir)
            except AssentError as e:
                result = ArchiveResult("error", str(e))
                _print_result(name, result)
                return result

            # A committed archive (in the roster) or a foreign zip is resolved without
            # requiring the plan lock, because the live directory (and its lock file)
            # may already be gone.  Only a genuinely fresh archive must prove no run is
            # in progress, and it does the delete after releasing the lock.
            if _in_roster(entries, name) or _zip_path(cfg.assent_dir, name).exists() \
                    or not cfg.tasks_dir.is_dir():
                result = _resume_or_finish(cfg, entries)
                _print_result(name, result)
                return result

            # Acquire the plan lock, creating assent.lock when it is missing: a
            # missing lock file is positive proof nobody holds the plan (lockfile
            # distinguishes LockMissing from a live lock), so archive takes the lock
            # rather than refusing.  Holding a real lock across the prove/clean/
            # compress/register/publish work blocks concurrent run/reject/rework and
            # closes the probe-then-act window; the lock vanishes with the deleted
            # directory (excluded from the zip), so there is no separate release step.
            try:
                with hold_lock(cfg.tasks_dir, name):
                    result = _do_archive_new(cfg, entries)
            except LockBusy:
                result = ArchiveResult("skipped", "a run is in progress")
            except AssentError as e:
                result = ArchiveResult(
                    "error", f"plan lock could not be acquired: {e}")
            if result.status == "archived":
                # Plan lock released: safe to remove the live directory (whose lock
                # file we no longer hold open) as the final step.
                try:
                    _delete_plan_dir(cfg.tasks_dir)
                except AssentError as e:
                    result = ArchiveResult("error", str(e))
            _print_result(name, result)
            return result
    except LockBusy:
        result = ArchiveResult("skipped", "repository integration is in progress")
        _print_result(name, result)
        return result
    except AssentError as e:
        result = ArchiveResult(
            "error", f"integration lock could not be acquired: {e}")
        _print_result(name, result)
        return result


def _print_result(name: str, result: ArchiveResult) -> None:
    if result.status == "archived":
        if result.reason == "already archived":
            print(f"{name}: already archived")
        else:
            print(f"{name}: archived (plan compressed to _archive/{name}.zip, "
                  "roster updated, live directory removed)")
    elif result.status == "skipped":
        print(f"{name}: skipped ({result.reason})")
    else:
        print(f"{name}: archive error ({result.reason})")


def archive_plan(cfg: Config) -> int:
    """Archive one explicitly named plan; refuse (exit 1) on any unmet precondition."""
    if not validate_live_plan_selection(
            cfg.assent_dir, [cfg.tasks_name],
            recognized=archive_recovery_names(cfg.assent_dir, [cfg.tasks_name])):
        return 1
    result = _archive_one(cfg)
    return 0 if result.status == "archived" else 1


def archive_selected(config_path: str, plan_names: Sequence[str]) -> int:
    """Archive every explicitly named plan; anything not archived is a failure.

    This is the multi-plan form of ``archive_plan``, and it keeps that
    command's contract rather than ``--all``'s: the human named these plans,
    so a plan that is merely ineligible is still a refused request, not a
    skip.  Every selected plan is attempted, so one refusal does not hide the
    state of the rest.
    """
    try:
        assent_dir = validate_config(config_path)
    except AssentError as e:
        print(f"archive selection refused (config error: {e})")
        return 1
    if not validate_live_plan_selection(
            assent_dir, plan_names,
            recognized=archive_recovery_names(assent_dir, plan_names)):
        return 1

    archived: list[str] = []
    refused: list[str] = []
    for index, plan_name in enumerate(plan_names):
        if index:
            print()
        try:
            cfg = load_config(config_path, plan_name)
        except AssentError as e:
            print(f"{plan_name}: archive error (config error: {e})")
            refused.append(plan_name)
            continue
        if _archive_one(cfg).status == "archived":
            archived.append(plan_name)
        else:
            refused.append(plan_name)
    print()
    print(f"archive summary: {len(archived)} archived, "
          f"{len(refused)} not archived.")
    return 1 if refused else 0


def archive_all(config_path: str, assent_dir: Path) -> int:
    """Archive every eligible plan in lexicographic order; skips are not failures."""
    plan_names = list_task_plans(assent_dir)
    if not plan_names:
        print("No plan with a task file found.")
        return 1
    archived: list[str] = []
    skipped: list[str] = []
    errored: list[str] = []
    sweep_cfg: Config | None = None
    for index, plan_name in enumerate(plan_names):
        if index:
            print()
        try:
            cfg = load_config(config_path, plan_name)
        except AssentError as e:
            print(f"{plan_name}: archive error (config error: {e})")
            errored.append(plan_name)
            continue
        if sweep_cfg is None:
            sweep_cfg = cfg
        result = _archive_one(cfg)
        if result.status == "archived":
            archived.append(plan_name)
        elif result.status == "skipped":
            skipped.append(plan_name)
        else:
            errored.append(plan_name)

    # ``archive --all`` is a whole-project invocation, so it owns the
    # repository-global temporary namespace exactly as plan-less ``clean`` does.  It
    # delegates to clean's single implementation rather than growing a copy, and
    # calls it here -- after the per-plan loop -- because every ``_archive_one``
    # holds the integration lock the sweep must take for itself, and because a
    # plan that is skipped or errors must not skip the sweep.  A config that
    # only names the archived plan still carries the repository root and
    # ``.assent`` directory the sweep needs, and both outlive the archived plan.
    swept = 0
    if sweep_cfg is not None:
        swept = sweep_orphaned_temporary_branches(sweep_cfg)
    print()
    print(f"archive --all summary: {len(archived)} archived, "
          f"{len(skipped)} skipped, {len(errored)} error(s).")
    return 1 if errored or swept else 0


def restore_plan(cfg: Config) -> int:
    """Reverse an archive: extract the zip back to the live directory, deregister, delete the zip.

    Refuses when the live directory already exists (nothing to restore onto) or no
    archive zip exists.  Extraction lands atomically via a temporary directory so an
    interruption never leaves a half-written live directory.
    """
    name = cfg.tasks_name
    assent_dir = cfg.assent_dir
    live_dir = cfg.tasks_dir
    zip_path = _zip_path(assent_dir, name)
    try:
        with hold_integration_lock(assent_dir):
            if live_dir.exists():
                print(f"{name}: restore refused (live directory {live_dir} already "
                      "exists; remove it first if you really mean to overwrite)")
                return 1
            if not zip_path.is_file():
                print(f"{name}: restore refused (no archive {zip_path} to restore)")
                return 1
            try:
                entries = read_roster(assent_dir)
            except AssentError as e:
                print(f"{name}: restore error ({e})")
                return 1

            tmp_dir = assent_dir / f".{name}.restore.tmp"
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
            try:
                with zipfile.ZipFile(zip_path) as archive:
                    archive.extractall(tmp_dir)
                os.replace(tmp_dir, live_dir)
            except (OSError, zipfile.BadZipFile) as e:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                print(f"{name}: restore error (extraction failed: {e})")
                return 1

            _write_roster(assent_dir, [e for e in entries if e["plan"] != name])
            zip_path.unlink(missing_ok=True)
            print(f"{name}: restored (plan extracted to {cfg.rel(live_dir)}, "
                  "removed from roster, archive deleted)")
            return 0
    except LockBusy:
        print(f"{name}: restore refused (repository integration is in progress)")
        return 1
    except AssentError as e:
        print(f"{name}: restore error (integration lock could not be acquired: {e})")
        return 1
