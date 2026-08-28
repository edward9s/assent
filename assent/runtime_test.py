"""Current source-bound runtime-test evidence gates."""

from __future__ import annotations

import hashlib
from pathlib import Path

from assent import AssentError
from assent.plan import (RUNTIME_TEST_CONTRACT_NAME, parse_runtime_test_contract,
                         read_runtime_test_workflow_state)


def evidence_identity(source_commit: str, command: str) -> str:
    """Identify the exact source commit and command one runtime pass tested."""
    command_sha256 = hashlib.sha256(command.encode("utf-8")).hexdigest()
    return f"{source_commit}:{command_sha256}"


def after_plan_gate_problem(plan_dir: Path, source_commit: str) -> str | None:
    """Return why an after-plan runtime gate is not currently satisfied."""
    if not (Path(plan_dir) / RUNTIME_TEST_CONTRACT_NAME).is_file():
        return None
    contract = parse_runtime_test_contract(plan_dir)
    if contract.execution != "after_plan":
        return None
    assert contract.command is not None
    state = read_runtime_test_workflow_state(plan_dir)
    if state is None:
        return "runtime PASSED evidence is missing"
    if state.action != "runtime_test" or state.action_status != "PASSED":
        status = state.action_status or "missing"
        return f"runtime evidence status is {status}"
    if len(state.action_evidence) != 2:
        raise AssentError("Runtime action evidence is unreadable")
    expected = evidence_identity(source_commit, contract.command)
    if state.action_source_tree != expected:
        return "runtime PASSED evidence is stale for the current source or command"
    if state.action_evidence[0] != contract.command:
        return "runtime PASSED evidence names a different command"
    return None
