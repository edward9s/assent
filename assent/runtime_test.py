"""Current source-bound runtime-test evidence gates."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from assent import AssentError
from assent.plan import (parse_runtime_test_contract,
                         parse_runtime_action_results,
                         read_runtime_test_workflow_state)


def evidence_identity(source_commit: str, commands: tuple[str, ...]) -> str:
    """Identify the exact source commit and ordered commands one pass tested."""
    encoded = json.dumps(
        commands, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    command_sha256 = hashlib.sha256(encoded).hexdigest()
    return f"{source_commit}:{command_sha256}"


def after_plan_gate_problem(plan_dir: Path, source_commit: str) -> str | None:
    """Return why an after-plan runtime gate is not currently satisfied."""
    contract = parse_runtime_test_contract(plan_dir)
    if contract.execution != "after_plan":
        return None
    assert contract.commands is not None
    state = read_runtime_test_workflow_state(plan_dir)
    if state is None:
        return "runtime PASSED evidence is missing"
    if state.action != "runtime_test" or state.action_status != "PASSED":
        status = state.action_status or "missing"
        return f"runtime evidence status is {status}"
    if len(state.action_evidence) != 2:
        raise AssentError("Runtime action evidence is unreadable")
    expected = evidence_identity(source_commit, contract.commands)
    if state.action_source_tree != expected:
        return "runtime PASSED evidence is stale for the current source or commands"
    results = parse_runtime_action_results(state.action_evidence[0])
    if tuple(command for command, _exit_code in results) != contract.commands:
        return "runtime PASSED evidence names different commands"
    return None
