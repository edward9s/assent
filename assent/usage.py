"""Best-effort provider token-usage evidence and report aggregation."""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from assent.adapters import TokenUsage


USAGE_NAME = "_usage.jsonl"
USAGE_LOCK_NAME = "_usage.lock"
USAGE_VERSION = 1
TOKEN_CATEGORIES = (
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
_THREAD_LOCK = threading.Lock()


if sys.platform == "win32":
    import msvcrt

    def _lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(handle) -> None:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _unlock(handle) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


@contextmanager
def _usage_lock(assent_dir: Path) -> Iterator[None]:
    path = Path(assent_dir) / USAGE_LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    handle = os.fdopen(os.open(str(path), flags, 0o644), "r+b")
    try:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
            handle.flush()
        _lock(handle)
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()


def new_invocation_id() -> str:
    """Return the scheduler-owned stable identity for one provider invocation."""
    return uuid.uuid4().hex


def _usage_dict(item: TokenUsage) -> dict[str, object]:
    record: dict[str, object] = {}
    if isinstance(item.provider_model, str) and item.provider_model.strip():
        record["provider_model"] = item.provider_model.strip()
    for name in TOKEN_CATEGORIES:
        value = getattr(item, name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            record[name] = value
    return record


def _existing_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    identities: set[str] = set()
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(item, dict) and isinstance(item.get("invocation_id"), str):
                identities.add(item["invocation_id"])
    return identities


def record_invocation(
        assent_dir: Path, *, invocation_id: str, adapter: str,
        requested_model: str | None, context_kind: str, context_id: str,
        plan_names: Iterable[str], evidence: tuple[TokenUsage, ...] | None,
        now: datetime | None = None) -> bool:
    """Append one completed invocation without allowing telemetry failure to escape."""
    try:
        directory = Path(assent_dir)
        directory.mkdir(parents=True, exist_ok=True)
        plan_list = list(dict.fromkeys(str(plan_name) for plan_name in plan_names))
        record = {
            "version": USAGE_VERSION,
            "invocation_id": invocation_id,
            "time": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
            "adapter": adapter,
            "requested_model": requested_model,
            "context": {"kind": context_kind, "id": context_id},
            "plans": plan_list,
            "models": [_usage_dict(item) for item in (evidence or ())],
        }
        encoded = json.dumps(
            record, ensure_ascii=True, separators=(",", ":")) + "\n"
        path = directory / USAGE_NAME
        with _THREAD_LOCK, _usage_lock(directory):
            if invocation_id in _existing_ids(path):
                return True
            with path.open("a+b") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell():
                    handle.seek(-1, os.SEEK_END)
                    if handle.read(1) != b"\n":
                        handle.seek(0, os.SEEK_END)
                        handle.write(b"\n")
                handle.seek(0, os.SEEK_END)
                handle.write(encoded.encode("utf-8"))
                handle.flush()
        return True
    except (OSError, TypeError, ValueError):
        return False


def _valid_counter(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def read_records(assent_dir: Path) -> tuple[list[dict], int]:
    """Read valid version-1 records, deduplicated by invocation identity."""
    path = Path(assent_dir) / USAGE_NAME
    if not path.exists():
        return [], 0
    records: list[dict] = []
    invalid = 0
    seen: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], 1
    for line in lines:
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            invalid += 1
            continue
        if not isinstance(item, dict) or item.get("version") != USAGE_VERSION:
            invalid += 1
            continue
        identity = item.get("invocation_id")
        plan_names = item.get("plans")
        models = item.get("models")
        context = item.get("context")
        if (not isinstance(identity, str) or not identity
                or not isinstance(item.get("adapter"), str)
                or not isinstance(plan_names, list)
                or not all(isinstance(plan_name, str) for plan_name in plan_names)
                or not isinstance(models, list)
                or not isinstance(context, dict)):
            invalid += 1
            continue
        if identity in seen:
            continue
        seen.add(identity)
        records.append(item)
    return records, invalid


def report_lines(assent_dir: Path, plan_name: str) -> list[str]:
    """Render model-aware usage totals for one plan without inventing values."""
    records, invalid = read_records(assent_dir)
    relevant = [item for item in records if plan_name in item["plans"]]
    lines = ["AI usage (provider-reported)"]
    if not relevant:
        lines.append(
            "  Unavailable: no provider usage evidence is recorded for this plan; "
            "historical pre-feature sessions are not reconstructed.")
        if invalid:
            lines.append(f"  Ignored malformed usage records: {invalid}")
        return lines

    groups: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for record in relevant:
        adapter = record["adapter"]
        models = record["models"]
        usable_models = [model for model in models if isinstance(model, dict)]
        if not usable_models:
            requested = record.get("requested_model")
            label = (f"requested:{requested}" if isinstance(requested, str)
                     and requested else "unknown")
            groups[(adapter, label)].append({})
            continue
        for model in usable_models:
            actual = model.get("provider_model")
            if isinstance(actual, str) and actual:
                label = actual
            else:
                requested = record.get("requested_model")
                label = (f"requested:{requested}" if isinstance(requested, str)
                         and requested else "unknown")
            groups[(adapter, label)].append(model)

    for (adapter, model), buckets in sorted(groups.items()):
        count = len(buckets)
        lines.append(f"  {adapter} / {model}: {count} session(s)")
        for category in TOKEN_CATEGORIES:
            values = [value for bucket in buckets
                      if (value := _valid_counter(bucket.get(category))) is not None]
            coverage = f"{len(values)}/{count} records"
            rendered = str(sum(values)) if values else "unavailable"
            lines.append(f"    {category}: {rendered} ({coverage})")
    lines.append(
        "  Coverage is per metric; missing or invalid provider counters are not "
        "estimated as zero, and usage never gates workflow outcomes.")
    if invalid:
        lines.append(f"  Ignored malformed usage records: {invalid}")
    return lines
