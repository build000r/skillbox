"""Fail-closed DCG health for ``manage.py doctor`` and ``make dev-sanity``.

The check this replaces was ``path_exists`` on the binary: it went green the
moment a file existed at ``$SKILLBOX_DCG_BIN``. That is presence, not
protection. A host could pass it while the binary was the wrong version, the
policy had lost ``fail_closed``, the Claude hook pointed at a path that no
longer existed, Codex had never trusted the hook, or the MCP bridge still spoke
the removed ``dcg mcp`` spelling — in every one of those states nothing was
actually guarding the agent's shell.

This module validates the whole required state instead, and it is **read-only**:
it reports, it never converges. Convergence belongs to
:mod:`runtime_manager.dcg_lifecycle`, which is what every remediation command
here points at.

Three rules:

**Nothing is advisory.** Every required subject that is absent, stale,
malformed, mis-pathed, non-executable, or untrusted produces a ``fail``. There
is no "DCG is optional here" branch, because a host that declares the dcg-bin
artifact has declared that its agents are supposed to be guarded.

**Protection is never inferred from presence.** The binary's *reported* version
and the pinned asset digest are checked, not just the inode. The hook's actual
command string is checked, not just that some hook exists. Codex's *persisted
trust hash* is checked, not just that a hooks file was written.

**Known gaps are printed, not hidden.** DCG intercepts the agent hook surfaces.
It does not intercept a command the operator types directly into a shell, and it
does not see inside a Codex ``unified_exec`` session. Those are reported as
explicit limitations on every run, including a healthy one, so nobody reads
"healthy" as "nothing can run unguarded".

One remediation, always. A broken host gets exactly one command to type,
selected by :data:`_REMEDIATION_LADDER` in dependency order — installing the
binary before trusting a hook that does not exist yet.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from . import dcg_distribution as _dist
from . import dcg_reconcile as _reconcile
from ._shared.errors import CheckResult
from .errors import SkillboxError

CHECK_ID = "dcg"
CHECK_CODE = "dcg"

# DCG-native verdicts. Deliberately the reconciler's vocabulary, not the doctor
# family's pass/warn/inco/fail: "needs-operator-action" is a distinct state that
# the family vocabulary cannot express, and collapsing it into "fail" at this
# layer would lose the reason an operator needs.
STATUS_HEALTHY = _reconcile.STATE_HEALTHY
STATUS_NEEDS_OPERATOR = _reconcile.STATE_NEEDS_OPERATOR
STATUS_FAILED = _reconcile.STATE_FAILED
STATUS_UNSUPPORTED = _reconcile.STATE_UNSUPPORTED

# -- reason codes ----------------------------------------------------------

DCG_DOCTOR_NOT_DECLARED = "DCG_DOCTOR_NOT_DECLARED"
DCG_DOCTOR_BINARY_ABSENT = "DCG_DOCTOR_BINARY_ABSENT"
DCG_DOCTOR_BINARY_NOT_EXECUTABLE = "DCG_DOCTOR_BINARY_NOT_EXECUTABLE"
DCG_DOCTOR_BINARY_VERSION_MISMATCH = "DCG_DOCTOR_BINARY_VERSION_MISMATCH"
DCG_DOCTOR_BINARY_WRONG_PATH = "DCG_DOCTOR_BINARY_WRONG_PATH"
DCG_DOCTOR_BINARY_UNVERIFIED = "DCG_DOCTOR_BINARY_UNVERIFIED"
DCG_DOCTOR_POLICY_ABSENT = "DCG_DOCTOR_POLICY_ABSENT"
DCG_DOCTOR_POLICY_MALFORMED = "DCG_DOCTOR_POLICY_MALFORMED"
DCG_DOCTOR_POLICY_FAIL_OPEN = "DCG_DOCTOR_POLICY_FAIL_OPEN"
DCG_DOCTOR_POLICY_DRIFT = "DCG_DOCTOR_POLICY_DRIFT"
DCG_DOCTOR_HOOK_UNHEALTHY = "DCG_DOCTOR_HOOK_UNHEALTHY"
DCG_DOCTOR_HOOK_DUPLICATE = "DCG_DOCTOR_HOOK_DUPLICATE"
DCG_DOCTOR_CODEX_TRUST_ABSENT = "DCG_DOCTOR_CODEX_TRUST_ABSENT"
DCG_DOCTOR_CODEX_TRUST_STALE = "DCG_DOCTOR_CODEX_TRUST_STALE"
DCG_DOCTOR_PERSISTENCE_MISSING = "DCG_DOCTOR_PERSISTENCE_MISSING"
DCG_DOCTOR_MCP_OBSOLETE_COMMAND = "DCG_DOCTOR_MCP_OBSOLETE_COMMAND"
DCG_DOCTOR_ADAPTER_FAIL_OPEN = "DCG_DOCTOR_ADAPTER_FAIL_OPEN"
DCG_DOCTOR_UNSUPPORTED_PLATFORM = "DCG_DOCTOR_UNSUPPORTED_PLATFORM"
DCG_DOCTOR_RECONCILE_ERROR = "DCG_DOCTOR_RECONCILE_ERROR"

_RECONCILE_CMD = "python3 .env-manager/manage.py dcg-reconcile --action apply --format json"
_VERIFY_CMD = "python3 .env-manager/manage.py dcg-reconcile --action verify --format json"
_INSTALL_CMD = (
    "python3 -c 'from runtime_manager import dcg_distribution as d; "
    "d.install_verified_binary()'"
)

#: Reason -> the ONE command to type. Order IS dependency order: a host with no
#: binary is told to install it, not to go trust a hook that does not exist yet.
_REMEDIATION_LADDER: tuple[tuple[str, str], ...] = (
    (DCG_DOCTOR_UNSUPPORTED_PLATFORM, _VERIFY_CMD),
    (DCG_DOCTOR_BINARY_ABSENT, _INSTALL_CMD),
    (DCG_DOCTOR_BINARY_NOT_EXECUTABLE, _INSTALL_CMD),
    (DCG_DOCTOR_BINARY_VERSION_MISMATCH, _INSTALL_CMD),
    (DCG_DOCTOR_BINARY_UNVERIFIED, _INSTALL_CMD),
    (DCG_DOCTOR_BINARY_WRONG_PATH, _RECONCILE_CMD),
    (DCG_DOCTOR_POLICY_ABSENT, _RECONCILE_CMD),
    (DCG_DOCTOR_POLICY_MALFORMED, _RECONCILE_CMD),
    (DCG_DOCTOR_POLICY_FAIL_OPEN, _RECONCILE_CMD),
    (DCG_DOCTOR_POLICY_DRIFT, _RECONCILE_CMD),
    (DCG_DOCTOR_HOOK_DUPLICATE, _RECONCILE_CMD),
    (DCG_DOCTOR_HOOK_UNHEALTHY, _RECONCILE_CMD),
    (DCG_DOCTOR_MCP_OBSOLETE_COMMAND, _RECONCILE_CMD),
    (DCG_DOCTOR_ADAPTER_FAIL_OPEN, _RECONCILE_CMD),
    (DCG_DOCTOR_PERSISTENCE_MISSING, "make down && make up"),
    (DCG_DOCTOR_CODEX_TRUST_STALE, _reconcile.CODEX_TRUST_ACTION),
    (DCG_DOCTOR_CODEX_TRUST_ABSENT, _reconcile.CODEX_TRUST_ACTION),
    (DCG_DOCTOR_RECONCILE_ERROR, _VERIFY_CMD),
    (DCG_DOCTOR_NOT_DECLARED, _VERIFY_CMD),
)

#: Interception the guard does NOT provide. Reported on every run, healthy
#: included, because "healthy" must not read as "nothing runs unguarded".
LIMITATIONS: tuple[dict[str, str], ...] = (
    {
        "surface": "direct-shell",
        "intercepted": "no",
        "detail": (
            "A command the operator types straight into a terminal never passes "
            "through an agent PreToolUse hook, so DCG never sees it."
        ),
    },
    {
        "surface": "codex-unified-exec",
        "intercepted": "partial",
        "detail": (
            "Codex unified_exec keeps a shell session open; DCG sees the hook "
            "invocation, not each subsequent command typed into that session."
        ),
    },
)

#: Home subtrees that must be on a persistent mount. Losing .config/dcg loses
#: the ledger binding Codex's trust hash to the exact hooks.json bytes, so a
#: converged home comes back looking fresh and untrusted after a recreate.
PERSISTED_SUBTREES: tuple[str, ...] = (
    ".claude",
    ".codex",
    ".grok",
    ".local",
    ".config/dcg",
)


def _model_target(model: dict[str, Any]) -> tuple[Path, Path] | None:
    from . import runtime_ops

    return runtime_ops.dcg_lifecycle_target(model)


def _binary_report(verify: dict[str, Any], binary: Path) -> tuple[dict[str, Any], list[str]]:
    state = dict(verify.get("binary_state") or {})
    link = dict(verify.get("binary_link") or {})
    failures: list[str] = []

    exists = binary.is_file()
    executable = exists and binary.stat().st_mode & 0o111 != 0
    installed = str(state.get("installed_version") or "")
    expected = str(state.get("expected_version") or _dist.DCG_VERSION)

    report = {
        "path": str(binary),
        "present": exists,
        "executable": bool(executable),
        # `.version` is the version that is ACTUALLY installed, so a jq asserting
        # `.binary.version == "v0.6.7"` is asserting reality, not the pin.
        "version": installed,
        "expected_version": expected,
        "asset": state.get("asset"),
        "sha256": state.get("sha256"),
        "minisign_key_id": state.get("minisign_key_id"),
        "state": state.get("state"),
        "detail": state.get("detail"),
        "at_managed_path": link.get("state") == _reconcile.STATE_HEALTHY,
    }

    if not exists:
        failures.append(DCG_DOCTOR_BINARY_ABSENT)
        return report, failures
    if not executable:
        failures.append(DCG_DOCTOR_BINARY_NOT_EXECUTABLE)
    if not installed:
        # The reconciler could not get a version out of it at all: an
        # unverifiable binary is not a guarded one.
        failures.append(DCG_DOCTOR_BINARY_UNVERIFIED)
    elif installed != expected:
        failures.append(DCG_DOCTOR_BINARY_VERSION_MISMATCH)
    if link and link.get("state") not in (None, _reconcile.STATE_HEALTHY):
        failures.append(DCG_DOCTOR_BINARY_WRONG_PATH)
    if state.get("state") == _reconcile.STATE_FAILED:
        failures.append(DCG_DOCTOR_BINARY_UNVERIFIED)
    return report, failures


def _policy_report(verify: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    policy = dict(verify.get("policy") or {})
    failures: list[str] = []
    path_text = str(policy.get("path") or "")
    path = Path(path_text) if path_text else None

    report: dict[str, Any] = {
        "path": path_text,
        "state": policy.get("state"),
        "detail": policy.get("detail"),
        "present": bool(path and path.is_file()),
        "parsed": False,
        # Absence is fail-open until proven otherwise. A doctor that defaulted
        # this to True would hand out exactly the false green this bead exists
        # to remove.
        "fail_closed": False,
    }

    if not report["present"]:
        failures.append(DCG_DOCTOR_POLICY_ABSENT)
        return report, failures

    assert path is not None
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        report["error"] = str(exc)
        failures.append(DCG_DOCTOR_POLICY_MALFORMED)
        return report, failures

    report["parsed"] = True
    general = document.get("general") if isinstance(document.get("general"), dict) else {}
    report["fail_closed"] = general.get("fail_closed") is True
    packs = document.get("packs") if isinstance(document.get("packs"), dict) else {}
    report["packs"] = list(packs.get("enabled") or [])

    if not report["fail_closed"]:
        failures.append(DCG_DOCTOR_POLICY_FAIL_OPEN)
    if policy.get("state") == _reconcile.STATE_FAILED:
        failures.append(DCG_DOCTOR_POLICY_MALFORMED)
    elif policy.get("state") not in (None, _reconcile.STATE_HEALTHY):
        # The reconciler would rewrite it: the on-disk policy is not the
        # policy this repo declares.
        failures.append(DCG_DOCTOR_POLICY_DRIFT)
    return report, failures


def _hooks_report(verify: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Per-agent hook health.

    ``hooks.codex`` carries the *trust* state rather than file health on
    purpose: a Codex hooks.json can be byte-perfect and still guard nothing,
    because Codex will not run a hook it has not trusted.
    """
    failures: list[str] = []
    report: dict[str, Any] = {}
    details: dict[str, Any] = {}

    for agent in verify.get("agents") or []:
        if not isinstance(agent, dict):
            continue
        name = str(agent.get("agent") or "")
        if not name:
            continue
        health = str(agent.get("health") or agent.get("state") or "")
        duplicates = int(agent.get("duplicates_removed") or 0)
        details[name] = {
            "path": agent.get("path"),
            "health": health,
            "file_state": agent.get("state"),
            "detail": agent.get("detail"),
            "duplicates_removed": duplicates,
        }

        if name == "codex":
            trust = agent.get("trust") if isinstance(agent.get("trust"), dict) else {}
            trust_state = str(trust.get("state") or verify.get("codex_trust") or "")
            report[name] = trust_state
            details[name]["trust"] = trust
            if trust_state == _reconcile.CODEX_TRUST_STALE:
                failures.append(DCG_DOCTOR_CODEX_TRUST_STALE)
            elif trust_state != _reconcile.CODEX_TRUST_TRUSTED:
                failures.append(DCG_DOCTOR_CODEX_TRUST_ABSENT)
            # File-level breakage still counts, independent of trust.
            if agent.get("state") not in (None, _reconcile.STATE_HEALTHY):
                failures.append(DCG_DOCTOR_HOOK_UNHEALTHY)
        else:
            report[name] = health
            if health != _reconcile.STATE_HEALTHY:
                failures.append(DCG_DOCTOR_HOOK_UNHEALTHY)

        if duplicates:
            failures.append(DCG_DOCTOR_HOOK_DUPLICATE)

    for required_agent in ("claude", "codex", "grok"):
        if required_agent not in report:
            report[required_agent] = "absent"
            failures.append(DCG_DOCTOR_HOOK_UNHEALTHY)

    return {**report, "details": details}, failures


def _mcp_report(model: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The MCP bridge must speak ``mcp-server``; ``dcg mcp`` was removed in 0.6.7."""
    failures: list[str] = []
    declared: list[str] = []
    for service in model.get("services") or []:
        if not isinstance(service, dict):
            continue
        if str(service.get("artifact") or "") != _dist.ARTIFACT_ID:
            continue
        for key in ("command", "probe_command"):
            value = str(service.get(key) or "").strip()
            if value:
                declared.append(value)
        healthcheck = service.get("healthcheck")
        if isinstance(healthcheck, dict):
            probe = str(healthcheck.get("probe_command") or "").strip()
            if probe:
                declared.append(probe)

    obsolete = [
        text
        for text in declared
        if _trailing_subcommand(text) == _dist.DCG_OBSOLETE_MCP_COMMAND
    ]
    if obsolete:
        failures.append(DCG_DOCTOR_MCP_OBSOLETE_COMMAND)

    return (
        {
            "command": _dist.DCG_MCP_COMMAND,
            "obsolete_command": _dist.DCG_OBSOLETE_MCP_COMMAND,
            "declared": declared,
            "obsolete_declarations": obsolete,
            "state": STATUS_FAILED if obsolete else STATUS_HEALTHY,
        },
        failures,
    )


def _trailing_subcommand(command: str) -> str:
    parts = command.split()
    return parts[-1] if parts else ""


def _persistence_report(root_dir: Path) -> tuple[dict[str, Any], list[str]]:
    """Every subtree the reconciler writes must survive a container recreate."""
    compose = root_dir / "docker-compose.yml"
    failures: list[str] = []
    if not compose.is_file():
        return (
            {"compose": str(compose), "present": False, "missing": list(PERSISTED_SUBTREES)},
            [DCG_DOCTOR_PERSISTENCE_MISSING],
        )
    text = compose.read_text(encoding="utf-8")
    missing = [subtree for subtree in PERSISTED_SUBTREES if subtree not in text]
    if missing:
        failures.append(DCG_DOCTOR_PERSISTENCE_MISSING)
    return (
        {
            "compose": str(compose),
            "present": True,
            "required": list(PERSISTED_SUBTREES),
            "missing": missing,
            "state": STATUS_FAILED if missing else STATUS_HEALTHY,
        },
        failures,
    )


def _adapter_report() -> tuple[dict[str, Any], list[str]]:
    """The operator adapter's supported protocol is part of DCG health.

    A fail-OPEN adapter is the specific regression bead scpz removed: one that
    treats "no verdict" as permission. ``dcg_blocks_execution`` is the single
    predicate that makes that unexpressible, so the doctor asserts it directly
    rather than trusting that it is still wired that way.
    """
    failures: list[str] = []
    try:
        import sys

        scripts_dir = str(Path(__file__).resolve().parents[2] / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from lib import dcglib
    except ImportError as exc:  # pragma: no cover - adapter is in-repo
        return (
            {"state": STATUS_FAILED, "error": f"operator DCG adapter unavailable: {exc}"},
            [DCG_DOCTOR_ADAPTER_FAIL_OPEN],
        )

    # Anything that is not an explicit allow must block. Probe the predicate
    # with the three shapes a caller can actually hand it.
    fail_closed = (
        dcglib.dcg_blocks_execution(None)
        and dcglib.dcg_blocks_execution({"verdict": "unavailable"})
        and dcglib.dcg_blocks_execution({"verdict": "deny"})
        and not dcglib.dcg_blocks_execution({"verdict": "allow"})
    )
    if not fail_closed:
        failures.append(DCG_DOCTOR_ADAPTER_FAIL_OPEN)
    return (
        {
            "interface": dcglib.DCG_INTERFACE,
            "robot_schema_version": dcglib.DCG_ROBOT_SCHEMA_VERSION,
            "allow_decisions": sorted(dcglib.DCG_ALLOW_DECISIONS),
            "deny_decisions": sorted(dcglib.DCG_DENY_DECISIONS),
            "fail_closed": bool(fail_closed),
            "state": STATUS_HEALTHY if fail_closed else STATUS_FAILED,
        },
        failures,
    )


def _status_for(failures: list[str]) -> str:
    if not failures:
        return STATUS_HEALTHY
    trust_only = {DCG_DOCTOR_CODEX_TRUST_ABSENT, DCG_DOCTOR_CODEX_TRUST_STALE}
    if set(failures) <= trust_only:
        # Everything Skillbox can converge IS converged; what is left is a
        # human action in Codex's own UI.
        return STATUS_NEEDS_OPERATOR
    if DCG_DOCTOR_UNSUPPORTED_PLATFORM in failures:
        return STATUS_UNSUPPORTED
    return STATUS_FAILED


def _remediation(failures: list[str]) -> str:
    """Exactly one command, chosen in dependency order."""
    for reason, command in _REMEDIATION_LADDER:
        if reason in failures:
            return command
    return _VERIFY_CMD


def collect(model: dict[str, Any], root_dir: Path) -> dict[str, Any]:
    """The full DCG health report. Read-only; converges nothing."""
    target = _model_target(model)
    if target is None:
        failures = [DCG_DOCTOR_NOT_DECLARED]
        return {
            "id": CHECK_ID,
            "dcg_status": STATUS_FAILED,
            "home": "",
            "binary": {"present": False, "version": "", "expected_version": _dist.DCG_VERSION},
            "policy": {"fail_closed": False, "present": False},
            "hooks": {"claude": "absent", "codex": "absent", "grok": "absent"},
            "mcp": {"command": _dist.DCG_MCP_COMMAND},
            "limitations": [dict(item) for item in LIMITATIONS],
            "failures": failures,
            "operator_actions": [],
            "remediation": _remediation(failures),
            "message": "runtime declares no resolvable dcg-bin artifact to guard with",
        }

    home, binary = target
    try:
        verify = _reconcile.verify(home, binary=binary)
    except SkillboxError as exc:
        failures = [DCG_DOCTOR_RECONCILE_ERROR]
        return {
            "id": CHECK_ID,
            "dcg_status": STATUS_FAILED,
            "home": str(home),
            "binary": {"present": binary.is_file(), "version": "", "path": str(binary)},
            "policy": {"fail_closed": False, "present": False},
            "hooks": {"claude": "unknown", "codex": "unknown", "grok": "unknown"},
            "mcp": {"command": _dist.DCG_MCP_COMMAND},
            "limitations": [dict(item) for item in LIMITATIONS],
            "failures": failures,
            "operator_actions": [],
            "remediation": _remediation(failures),
            "message": f"DCG verify failed: [{exc.code}] {exc.message}",
        }

    failures: list[str] = []
    if verify.get("status") == _reconcile.STATE_UNSUPPORTED:
        failures.append(DCG_DOCTOR_UNSUPPORTED_PLATFORM)

    binary_report, binary_failures = _binary_report(verify, binary)
    policy_report, policy_failures = _policy_report(verify)
    hooks_report, hooks_failures = _hooks_report(verify)
    mcp_report, mcp_failures = _mcp_report(model)
    persistence_report, persistence_failures = _persistence_report(root_dir)
    adapter_report, adapter_failures = _adapter_report()

    failures.extend(binary_failures)
    failures.extend(policy_failures)
    failures.extend(hooks_failures)
    failures.extend(mcp_failures)
    failures.extend(persistence_failures)
    failures.extend(adapter_failures)
    # Stable order, no duplicates: the same broken host reports the same reasons
    # in the same order on every run.
    failures = sorted(set(failures))

    status = _status_for(failures)
    return {
        "id": CHECK_ID,
        "dcg_status": status,
        "home": str(home),
        "binary": binary_report,
        "policy": policy_report,
        "hooks": hooks_report,
        "mcp": mcp_report,
        "persistence": persistence_report,
        "adapter": adapter_report,
        "limitations": [dict(item) for item in LIMITATIONS],
        "failures": failures,
        "operator_actions": list(verify.get("operator_actions") or []),
        "remediation": _remediation(failures),
        "message": _message(status, failures),
    }


def _message(status: str, failures: list[str]) -> str:
    if status == STATUS_HEALTHY:
        return (
            "DCG is converged: pinned binary verified, policy fail-closed, "
            "Claude/Codex/Grok hooks healthy, Codex trust matches"
        )
    if status == STATUS_NEEDS_OPERATOR:
        return "DCG is converged but Codex has not trusted the generated hook"
    return "DCG is not protecting this host: " + ", ".join(failures)


def check_result(model: dict[str, Any], root_dir: Path) -> CheckResult:
    """The doctor finding.

    The family status is the family's own vocabulary (``pass``/``fail``); the
    DCG-native verdict rides along as ``dcg_status`` in the payload. Both
    ``needs-operator-action`` and an outright failure are ``fail``: an untrusted
    hook guards exactly as much as a missing one.
    """
    report = collect(model, root_dir)
    status = "pass" if report["dcg_status"] == STATUS_HEALTHY else "fail"
    # `details` is what the TEXT renderer prints line by line, so it stays
    # human-sized: the verdict, what broke, and the one command to type. The
    # full nested evidence goes to `extra`, which is JSON-only -- dumping the
    # binary/policy/hooks/adapter blocks into `make dev-sanity` output buried
    # the remediation under forty lines of dict.
    details = {
        "subject": CHECK_ID,
        "dcg_status": report["dcg_status"],
        "home": report.get("home", ""),
        "failures": report["failures"],
        "remediation": report["remediation"],
    }
    return CheckResult(
        status=status,
        code=CHECK_CODE,
        message=report["message"],
        details=details,
        fix_command=report["remediation"],
        # Promoted to the top level of the payload entry so a machine consumer
        # can assert `.checks[] | select(.id == "dcg") | .binary.version`
        # without reaching through `details`. Never `status`: that word belongs
        # to the doctor family vocabulary, and the DCG-native verdict (which has
        # a state the family cannot spell, `needs-operator-action`) rides as
        # `dcg_status` instead.
        extra={
            "id": report["id"],
            "dcg_status": report["dcg_status"],
            "binary": report["binary"],
            "policy": report["policy"],
            "hooks": report["hooks"],
            "mcp": report["mcp"],
            "persistence": report.get("persistence", {}),
            "adapter": report.get("adapter", {}),
            "limitations": report["limitations"],
            "operator_actions": report["operator_actions"],
            "failures": report["failures"],
        },
    )
