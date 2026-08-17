"""One DCG lifecycle contract shared by every Skillbox setup and deploy path.

``install.sh``, ``first-box``, ``onboard``, ``runtime-sync``, and box deploy all
converge DCG through :func:`converge` here. They differ only in the
``entrypoint`` label they pass, so a behaviour change lands in one place instead
of five, and nothing re-implements convergence in shell.

Three rules this module exists to enforce:

**Scope is explicit.** ``scope`` is a required argument, never inferred. A host
home and a managed container home are different convergence targets with
different persistence, and guessing between them is how a container recreate
silently loses the reconcile ledger. ``home`` is likewise always passed in --
this module never reads ``$HOME``.

**Healthy is never an optional skip.** The pinned binary, the rendered policy,
and the agent hooks are required. A caller that cannot converge gets a nonzero
exit and a reason code, not a "skipped, carry on" line. The one exception is a
genuinely unsupported platform, which is its own terminal state.

**Codex trust is an operator gate.** A prepared-but-untrusted Codex hook is
``needs-operator-action`` and exits nonzero. It never reports healthy, and the
bypass flag is rejected here as well as in the reconciler leaf, so no caller can
route around the gate by going through the lifecycle layer.

The reconcile leaf itself (atomic writes, merge safety, backups, the ledger)
lives in :mod:`runtime_manager.dcg_reconcile`; this module only orchestrates it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import dcg_reconcile as _reconcile
from .errors import SkillboxError, ValidationError

# -- entrypoints -----------------------------------------------------------
#
# Every setup, deploy, and reconcile path in the repo. The bead contract is
# that all of them call this module; the tuple is what the tests assert against
# so a new entrypoint cannot be added without being registered here.

ENTRYPOINT_INSTALL = "install"
ENTRYPOINT_FIRST_BOX = "first-box"
ENTRYPOINT_ONBOARD = "onboard"
ENTRYPOINT_RUNTIME_SYNC = "runtime-sync"
ENTRYPOINT_BOX_DEPLOY = "box-deploy"

ENTRYPOINTS: tuple[str, ...] = (
    ENTRYPOINT_INSTALL,
    ENTRYPOINT_FIRST_BOX,
    ENTRYPOINT_ONBOARD,
    ENTRYPOINT_RUNTIME_SYNC,
    ENTRYPOINT_BOX_DEPLOY,
)

# -- scope -----------------------------------------------------------------

SCOPE_HOST = "host"
SCOPE_CONTAINER = "container"
SCOPES: tuple[str, ...] = (SCOPE_HOST, SCOPE_CONTAINER)

# -- actions ---------------------------------------------------------------

ACTION_APPLY = "apply"
ACTION_VERIFY = "verify"
ACTION_RELINQUISH = "relinquish"
ACTIONS: tuple[str, ...] = (ACTION_APPLY, ACTION_VERIFY, ACTION_RELINQUISH)

# -- markers ---------------------------------------------------------------
#
# Stable, greppable tokens. install.sh writes these to its log and the
# acceptance contract greps for DCG_(CHANGED|NEEDS_OPERATOR_ACTION|HEALTHY),
# so they must not be reworded without updating that contract.

MARKER_HEALTHY = "DCG_HEALTHY"
MARKER_CHANGED = "DCG_CHANGED"
MARKER_NEEDS_OPERATOR_ACTION = "DCG_NEEDS_OPERATOR_ACTION"
MARKER_REMOVED = "DCG_REMOVED"
MARKER_UNSUPPORTED = "DCG_UNSUPPORTED"
MARKER_FAILED = "DCG_FAILED"

_STATUS_MARKERS: dict[str, str] = {
    _reconcile.STATE_HEALTHY: MARKER_HEALTHY,
    _reconcile.STATE_CHANGED: MARKER_CHANGED,
    _reconcile.STATE_NEEDS_OPERATOR: MARKER_NEEDS_OPERATOR_ACTION,
    _reconcile.STATE_UNSUPPORTED: MARKER_UNSUPPORTED,
    _reconcile.STATE_FAILED: MARKER_FAILED,
}

# Re-exported so callers need only import this module.
STATE_HEALTHY = _reconcile.STATE_HEALTHY
STATE_CHANGED = _reconcile.STATE_CHANGED
STATE_NEEDS_OPERATOR = _reconcile.STATE_NEEDS_OPERATOR
STATE_UNSUPPORTED = _reconcile.STATE_UNSUPPORTED
STATE_FAILED = _reconcile.STATE_FAILED

EXIT_OK = _reconcile.EXIT_OK
EXIT_FAILED = _reconcile.EXIT_FAILED
EXIT_NEEDS_OPERATOR = _reconcile.EXIT_NEEDS_OPERATOR
EXIT_UNSUPPORTED = _reconcile.EXIT_UNSUPPORTED

BYPASS_FLAG = _reconcile.BYPASS_FLAG

# -- error codes -----------------------------------------------------------

DCG_LIFECYCLE_UNKNOWN_ENTRYPOINT = "DCG_LIFECYCLE_UNKNOWN_ENTRYPOINT"
DCG_LIFECYCLE_UNKNOWN_SCOPE = "DCG_LIFECYCLE_UNKNOWN_SCOPE"
DCG_LIFECYCLE_UNKNOWN_ACTION = "DCG_LIFECYCLE_UNKNOWN_ACTION"
DCG_LIFECYCLE_HOME_REQUIRED = "DCG_LIFECYCLE_HOME_REQUIRED"
DCG_LIFECYCLE_BYPASS_FORBIDDEN = "DCG_LIFECYCLE_BYPASS_FORBIDDEN"

_BYPASS_NEXT_ACTION = _reconcile.CODEX_TRUST_ACTION


def reject_bypass(values: Sequence[Any]) -> None:
    """Refuse the hook-trust bypass flag anywhere in *values*.

    The reconciler leaf rejects this too. Repeating the check at the lifecycle
    boundary means a caller cannot smuggle the flag in through an entrypoint's
    argument passthrough and reach a "healthy" verdict without operator trust.
    """
    for value in values:
        if BYPASS_FLAG in str(value):
            raise ValidationError(
                DCG_LIFECYCLE_BYPASS_FORBIDDEN,
                f"{BYPASS_FLAG} is forbidden as setup, proof, or remediation.",
                context={"flag": BYPASS_FLAG},
                next_actions=[_BYPASS_NEXT_ACTION],
                recoverable=False,
            )


def _require_choice(value: str, choices: Sequence[str], code: str, label: str) -> str:
    text = str(value or "").strip()
    if text not in choices:
        raise ValidationError(
            code,
            f"unknown DCG lifecycle {label}: {text!r}",
            context={label: text, "supported": list(choices)},
            next_actions=[f"pass one of: {', '.join(choices)}"],
            recoverable=False,
        )
    return text


def _require_home(home: Path | str | None) -> Path:
    text = str(home or "").strip()
    if not text:
        raise ValidationError(
            DCG_LIFECYCLE_HOME_REQUIRED,
            "DCG lifecycle requires an explicit home; it is never inferred.",
            context={"home": text},
            next_actions=["pass --home <managed home>"],
            recoverable=False,
        )
    return Path(text)


def marker(payload: Mapping[str, Any]) -> str:
    """The greppable token for a lifecycle payload."""
    if payload.get("result") == _reconcile.RESULT_REMOVED:
        return MARKER_REMOVED
    return _STATUS_MARKERS.get(str(payload.get("status")), MARKER_FAILED)


def exit_code(payload: Mapping[str, Any]) -> int:
    """Process exit code for a lifecycle payload.

    ``needs-operator-action`` is deliberately nonzero: a fresh setup that still
    needs Codex trust must not look like success to install --verify or to any
    caller's ``&&`` chain.
    """
    status = str(payload.get("status"))
    if status == _reconcile.STATE_FAILED:
        return EXIT_FAILED
    if status == _reconcile.STATE_UNSUPPORTED:
        return EXIT_UNSUPPORTED
    if status == _reconcile.STATE_NEEDS_OPERATOR:
        return EXIT_NEEDS_OPERATOR
    return EXIT_OK


def is_healthy(payload: Mapping[str, Any]) -> bool:
    return str(payload.get("status")) == _reconcile.STATE_HEALTHY


def _failure_payload(
    exc: SkillboxError,
    *,
    entrypoint: str,
    scope: str,
    action: str,
    home: Any,
) -> dict[str, Any]:
    payload = exc.to_payload()
    # SkillboxError nests the code under error.code. Lift it (and the message)
    # to the top level so every consumer of a lifecycle payload -- step_detail,
    # the text renderer, the sync error -- reads the same two keys regardless of
    # whether the payload came from a success or a failure path.
    error = payload.get("error") if isinstance(payload.get("error"), Mapping) else {}
    payload["code"] = str(error.get("code") or exc.code)
    payload["message"] = str(error.get("message") or exc.message)
    payload.update(
        {
            "action": action,
            "entrypoint": entrypoint,
            "scope": scope,
            "home": str(home),
            "result": _reconcile.RESULT_FAILED,
            "status": _reconcile.STATE_FAILED,
        }
    )
    payload["marker"] = MARKER_FAILED
    payload["exit_code"] = EXIT_FAILED
    payload["ok"] = False
    return payload


def converge(
    *,
    entrypoint: str,
    scope: str,
    home: Path | str,
    action: str = ACTION_APPLY,
    binary: Path | str | None = None,
    site_policy_paths: Sequence[Path | str] = (),
    adopt_policy: bool = False,
    dry_run: bool = False,
    purge: bool = False,
    platform: str | None = None,
) -> dict[str, Any]:
    """Converge (or verify, or relinquish) DCG for one explicitly scoped home.

    Returns a payload that always carries ``entrypoint``, ``scope``, ``marker``,
    ``exit_code``, and ``ok``, on both the success and the failure path, so a
    caller can branch on the result without a try/except of its own. Reconciler
    errors are captured into a failed payload rather than raised, because every
    caller's contract is "record the step, then exit nonzero".
    """
    entrypoint = _require_choice(
        entrypoint, ENTRYPOINTS, DCG_LIFECYCLE_UNKNOWN_ENTRYPOINT, "entrypoint"
    )
    scope = _require_choice(scope, SCOPES, DCG_LIFECYCLE_UNKNOWN_SCOPE, "scope")
    action = _require_choice(action, ACTIONS, DCG_LIFECYCLE_UNKNOWN_ACTION, "action")
    home_path = _require_home(home)
    reject_bypass([binary or "", *[str(p) for p in site_policy_paths]])

    try:
        if action == ACTION_APPLY:
            payload = _reconcile.apply(
                home_path,
                binary=binary,
                site_policy_paths=site_policy_paths,
                adopt_policy=adopt_policy,
                dry_run=dry_run,
                platform=platform,
            )
        elif action == ACTION_VERIFY:
            payload = _reconcile.verify(
                home_path,
                binary=binary,
                site_policy_paths=site_policy_paths,
                adopt_policy=adopt_policy,
                platform=platform,
            )
        else:
            payload = _reconcile.relinquish(
                home_path, binary=binary, dry_run=dry_run, purge=purge
            )
    except SkillboxError as exc:
        return _failure_payload(
            exc, entrypoint=entrypoint, scope=scope, action=action, home=home_path
        )

    payload = dict(payload)
    payload["entrypoint"] = entrypoint
    payload["scope"] = scope
    payload["marker"] = marker(payload)
    payload["exit_code"] = exit_code(payload)
    payload["ok"] = payload["exit_code"] == EXIT_OK
    return payload


def relinquish(
    *,
    entrypoint: str,
    scope: str,
    home: Path | str,
    binary: Path | str | None = None,
    dry_run: bool = False,
    purge: bool = False,
) -> dict[str, Any]:
    """Explicit removal path, backed by the reconciler's hook leaf.

    Idempotent by construction: the leaf removes only DCG-owned entries, so a
    second relinquish on an already-clean home is ``unchanged``, not an error.
    """
    return converge(
        entrypoint=entrypoint,
        scope=scope,
        home=home,
        action=ACTION_RELINQUISH,
        binary=binary,
        dry_run=dry_run,
        purge=purge,
    )


def action_text(payload: Mapping[str, Any]) -> str:
    """One-line summary for a runtime-sync action record."""
    home = str(payload.get("home") or "")
    scope = str(payload.get("scope") or "")
    status = str(payload.get("status") or "")
    result = str(payload.get("result") or "")
    verb = "dcg-reconcile"
    detail = f"{scope} scope, {status}"
    if result and result != status:
        detail = f"{scope} scope, {status}, {result}"
    return f"{verb}: {home} ({detail})"


def step_detail(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Compact detail block for a workflow step or a box deploy record.

    Deliberately small: the full reconciler payload carries per-agent file
    state, and a workflow step is not the place to republish it.
    """
    detail: dict[str, Any] = {
        "entrypoint": payload.get("entrypoint"),
        "scope": payload.get("scope"),
        "home": payload.get("home"),
        "action": payload.get("action"),
        "status": payload.get("status"),
        "result": payload.get("result"),
        "marker": payload.get("marker"),
        "exit_code": payload.get("exit_code"),
    }
    if payload.get("codex_trust"):
        detail["codex_trust"] = payload["codex_trust"]
    operator_actions = payload.get("operator_actions") or []
    if operator_actions:
        detail["operator_actions"] = list(operator_actions)
    if payload.get("code"):
        detail["code"] = payload["code"]
    if payload.get("message"):
        detail["message"] = payload["message"]
    return detail


def workflow_status(payload: Mapping[str, Any]) -> str:
    """Map a lifecycle payload onto a workflow step status.

    ``needs-operator-action`` is a ``fail`` for the step: the bead contract is
    that a fresh setup awaiting Codex trust never reports healthy and never lets
    its caller exit zero.
    """
    return "ok" if payload.get("ok") else "fail"


DCG_LIFECYCLE_MODEL_TARGET_MISSING = "DCG_LIFECYCLE_MODEL_TARGET_MISSING"


def target_from_model(repo_root: Path | str) -> tuple[Path, Path]:
    """Resolve ``(home, binary)`` from a repo's runtime model.

    This is still an *explicit* resolution -- the caller opted in with
    ``--from-model`` and named the repo -- rather than an inferred ``$HOME``.
    Imported lazily because :mod:`runtime_manager.runtime_ops` imports this
    module back, and the package facade would otherwise re-enter its loader.
    """
    from lib.runtime_model import build_runtime_model

    from . import runtime_ops

    model = build_runtime_model(Path(repo_root))
    target = runtime_ops.dcg_lifecycle_target(model)
    if target is None:
        raise ValidationError(
            DCG_LIFECYCLE_MODEL_TARGET_MISSING,
            f"runtime model at {repo_root} declares no resolvable dcg-bin artifact",
            context={"repo_root": str(repo_root)},
            next_actions=[
                "declare a dcg-bin artifact in workspace/runtime.yaml, or pass --home explicitly",
            ],
            recoverable=False,
        )
    return target


def _text_lines(payload: Mapping[str, Any]) -> list[str]:
    lines = [
        f"entrypoint: {payload.get('entrypoint')}",
        f"scope:      {payload.get('scope')}",
        f"home:       {payload.get('home')}",
        f"action:     {payload.get('action')}",
        f"result:     {payload.get('result')}",
        f"status:     {payload.get('status')}",
        f"marker:     {payload.get('marker')}",
    ]
    if payload.get("codex_trust"):
        lines.append(f"codex_trust: {payload['codex_trust']}")
    if payload.get("code"):
        lines.append(f"error:      [{payload['code']}] {payload.get('message', '')}")
    for text in payload.get("operator_actions") or []:
        lines.append(f"operator:   {text}")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """``python3 -m runtime_manager.dcg_lifecycle <action> --entrypoint E ...``."""
    import argparse
    import sys as _sys

    args_list = list(argv) if argv is not None else _sys.argv[1:]

    # Checked before argparse so an unparseable argv still cannot smuggle it in.
    try:
        reject_bypass(args_list)
    except ValidationError as exc:
        payload = _failure_payload(
            exc,
            entrypoint=ENTRYPOINT_INSTALL,
            scope=SCOPE_HOST,
            action=ACTION_VERIFY,
            home="",
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return EXIT_FAILED

    parser = argparse.ArgumentParser(prog="dcg_lifecycle")
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--entrypoint", required=True, choices=ENTRYPOINTS)
    parser.add_argument("--scope", required=True, choices=SCOPES)
    parser.add_argument("--home", default="", help="managed home (never inferred)")
    parser.add_argument(
        "--from-model",
        default="",
        metavar="REPO_ROOT",
        help="resolve --home/--binary from a repo's runtime model instead",
    )
    parser.add_argument("--binary", default="", help="pinned dcg binary path")
    parser.add_argument("--site-policy", action="append", default=[], metavar="PATH")
    parser.add_argument("--adopt-policy", action="store_true")
    parser.add_argument("--platform", default="", help="os/machine override")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--purge", action="store_true", help="relinquish only")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(args_list)

    home: Path | str = args.home
    binary = args.binary or None
    try:
        if args.from_model:
            if args.home:
                parser.error("pass either --home or --from-model, not both")
            home, resolved_binary = target_from_model(args.from_model)
            binary = binary or resolved_binary
        elif not args.home:
            parser.error("one of --home or --from-model is required")

        payload = converge(
            entrypoint=args.entrypoint,
            scope=args.scope,
            home=home,
            action=args.action,
            binary=binary,
            site_policy_paths=args.site_policy,
            adopt_policy=args.adopt_policy,
            dry_run=args.dry_run,
            purge=args.purge,
            platform=args.platform or None,
        )
    except SkillboxError as exc:
        payload = _failure_payload(
            exc,
            entrypoint=args.entrypoint,
            scope=args.scope,
            action=args.action,
            home=home or args.from_model,
        )

    if args.format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("\n".join(_text_lines(payload)))
    return int(payload.get("exit_code", EXIT_FAILED))


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(main())
