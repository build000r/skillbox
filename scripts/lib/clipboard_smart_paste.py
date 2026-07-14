#!/usr/bin/env python3
"""One-gesture, exact-pane smart paste orchestration."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import clipboard_session
from . import clipboard_snapshot
from . import clipboard_transfer

SCHEMA_VERSION = 1
NOTICE_MS = 2500
PROGRESS_DELAY_SECONDS = 0.5
TRANSFER_TIMEOUT_SECONDS = 3.0


class SmartPasteError(RuntimeError):
    """A safe smart-paste rejection."""


def default_runtime_root() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache).expanduser() if cache else Path.home() / ".cache"
    return base / "skillbox" / "smart-paste"


def _private_runtime(root: Path | None = None) -> Path:
    raw = (root or default_runtime_root()).expanduser()
    if raw.is_symlink():
        raise SmartPasteError("smart-paste runtime root must not be a symlink")
    raw.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = raw.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise SmartPasteError("smart-paste runtime root must be an owned directory")
    os.chmod(raw, 0o700)
    return raw.resolve(strict=True)


def _text_from_payload(payload: Mapping[str, Any]) -> str:
    for item in payload.get("items", []):
        if isinstance(item, dict) and item.get("uti") in clipboard_snapshot.TEXT_UTIS:
            return str(item.get("text", ""))
    raise SmartPasteError("typed text snapshot did not contain text bytes")


def capture(
    *,
    runtime_root: Path,
    payload_capture: Callable[
        [], dict[str, Any]
    ] = clipboard_snapshot.capture_operator_payload,
) -> tuple[clipboard_snapshot.ClipboardSnapshot, str | None]:
    payload = payload_capture()
    snapshot = clipboard_snapshot.snapshot_from_payload(
        payload, output_dir=runtime_root / "snapshots"
    )
    text = _text_from_payload(payload) if snapshot.kind == "text" else None
    return snapshot, text


def _run(
    command: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    completed = runner(
        list(command),
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        error = completed.stderr.decode("utf-8", "replace").strip()
        raise SmartPasteError(error or f"command failed: {command[0]}")
    return completed


def tmux_option(
    pane: str,
    option: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> str:
    return (
        _run(["tmux", "show-option", "-p", "-v", "-t", pane, option], runner=runner)
        .stdout.decode("utf-8", "strict")
        .strip()
    )


def verify_route_ownership(
    *,
    pane: str,
    client: str,
    route_path: Path,
    generation: str,
    now: float | None = None,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> dict[str, Any]:
    current_path = tmux_option(pane, clipboard_session.TMUX_ROUTE_OPTION, runner=runner)
    current_generation = tmux_option(
        pane, clipboard_session.TMUX_GENERATION_OPTION, runner=runner
    )
    if current_path != str(route_path) or current_generation != generation:
        raise SmartPasteError("focused route changed before paste completed")
    try:
        return clipboard_session.load_record(
            route_path,
            now=now,
            expected_generation=generation,
            expected_pane=pane,
            expected_client=client,
        )
    except clipboard_session.SessionError as exc:
        raise SmartPasteError(str(exc)) from exc


def mark_latest_gesture(runtime_root: Path, pane: str, gesture_id: str) -> Path:
    key = __import__("hashlib").sha256(pane.encode()).hexdigest()[:24]
    path = runtime_root / f"pane-{key}.generation"
    fd, raw = tempfile.mkstemp(prefix=".gesture-", dir=runtime_root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(gesture_id + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(raw, path)
        os.chmod(path, 0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(raw)
    return path


def gesture_is_latest(path: Path, gesture_id: str) -> bool:
    try:
        return path.read_text(encoding="utf-8").strip() == gesture_id
    except OSError:
        return False


def inject_bracketed_paste(
    *,
    pane: str,
    data: bytes,
    gesture_id: str,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> None:
    if not data:
        return
    buffer_name = f"skillbox-paste-{gesture_id}"
    _run(
        ["tmux", "load-buffer", "-b", buffer_name, "-"], runner=runner, input_bytes=data
    )
    try:
        _run(
            ["tmux", "paste-buffer", "-p", "-d", "-b", buffer_name, "-t", pane],
            runner=runner,
        )
    except BaseException:
        with contextlib.suppress(SmartPasteError):
            _run(["tmux", "delete-buffer", "-b", buffer_name], runner=runner)
        raise


def notify(
    message: str,
    *,
    pane: str,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> None:
    safe = " ".join(message.split())[:160]
    with contextlib.suppress(SmartPasteError):
        _run(
            ["tmux", "display-message", "-d", str(NOTICE_MS), "-t", pane, safe],
            runner=runner,
        )


def write_receipt(runtime_root: Path, receipt: Mapping[str, Any]) -> Path:
    receipts = runtime_root / "receipts"
    receipts.mkdir(mode=0o700, exist_ok=True)
    os.chmod(receipts, 0o700)
    path = receipts / f"{receipt['gesture_id']}.json"
    fd, raw = tempfile.mkstemp(prefix=".receipt-", dir=receipts)
    try:
        os.fchmod(fd, 0o600)
        payload = (
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(raw, path)
        os.chmod(path, 0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(raw)
    return path


def error_code(exc: BaseException) -> str:
    message = str(exc).lower()
    if "cleanup" in message:
        return "cleanup_failed"
    if "timed out" in message or "offline" in message:
        return "target_offline"
    if "permission denied" in message or "authentication" in message:
        return "auth_failed"
    if "too_large" in message or "exceeds" in message:
        return "artifact_too_large"
    if "clipboard changed" in message:
        return "clipboard_changed"
    if "stale" in message or "generation" in message or "route changed" in message:
        return "stale_route"
    if "focused" in message or "newer paste" in message:
        return "focus_changed"
    if (
        "unsupported" in message
        or "not pasteable" in message
        or "does not support" in message
    ):
        return "unsupported_type"
    if "paste failed" in message or "injection" in message:
        return "injection_failed"
    return "paste_rejected"


def _smart_paste_impl(
    *,
    pane: str,
    client: str,
    route_path: Path | None = None,
    generation: str | None = None,
    runtime_root: Path | None = None,
    now: Callable[[], float] = time.time,
    capture_fn: Callable[
        ..., tuple[clipboard_snapshot.ClipboardSnapshot, str | None]
    ] = capture,
    change_count_fn: Callable[[], int] = clipboard_snapshot.current_operator_generation,
    transfer_fn: Callable[..., dict[str, Any]] = clipboard_transfer.transfer_artifact,
    cleanup_fn: Callable[..., dict[str, Any]] = clipboard_transfer.delete_remote_artifact,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    inject_text: bool = False,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    started = now()
    runtime_root = _private_runtime(runtime_root)
    gesture_id = str(uuid.uuid4())
    latest_path = mark_latest_gesture(runtime_root, pane, gesture_id)
    record: dict[str, Any] | None = None
    if route_path is not None:
        if not generation:
            raise SmartPasteError("registered route requires a generation")
        record = verify_route_ownership(
            pane=pane,
            client=client,
            route_path=route_path,
            generation=generation,
            runner=runner,
        )

    snapshot, text = capture_fn(runtime_root=runtime_root)
    if expected_sha256 is not None and snapshot.sha256 != expected_sha256:
        raise SmartPasteError(
            "clipboard digest does not match the explicitly expected proof artifact"
        )
    if not snapshot.ok:
        code = snapshot.error["code"] if snapshot.error else "unsupported"
        raise SmartPasteError(f"clipboard is not pasteable ({code})")
    if snapshot.kind == "empty":
        outcome = "empty"
        injected = None
        transfer_receipt = None
    elif snapshot.kind == "text":
        if text is None:
            raise SmartPasteError("text snapshot lost its private payload")
        if inject_text:
            if change_count_fn() != snapshot.change_count:
                raise SmartPasteError("clipboard changed before text injection")
            if not gesture_is_latest(latest_path, gesture_id):
                raise SmartPasteError("a newer paste superseded this gesture")
            if record is not None:
                verify_route_ownership(
                    pane=pane,
                    client=client,
                    route_path=route_path,
                    generation=generation,
                    runner=runner,
                )
            inject_bracketed_paste(
                pane=pane,
                data=text.encode("utf-8"),
                gesture_id=gesture_id,
                runner=runner,
            )
            outcome = "text"
            injected = {"kind": "text", "byte_size": len(text.encode("utf-8"))}
        else:
            # Cmd+V chains Ghostty's native paste action after the private
            # image probe. Never duplicate or reinterpret text on that path.
            outcome = "native_text"
            injected = None
        transfer_receipt = None
    elif snapshot.kind == "files":
        # Finder file clipboards often also expose a native text/file-url
        # representation, which Ghostty owns through the chained native paste.
        # A parallel upload would duplicate or cross-deliver content. Keep the
        # ordered names in the receipt and make no helper injection.
        outcome = "native_files"
        injected = None
        transfer_receipt = None
    elif snapshot.kind in {"image", "document"}:
        if not snapshot.artifact or not snapshot.sha256:
            raise SmartPasteError(
                "image snapshot did not materialize a private artifact"
            )
        local_path = Path(snapshot.artifact)
        if record is None or record.get("ssh_target") is None:
            remote_path = str(local_path)
            transfer_receipt = None
        else:
            inbound = record.get("capabilities", {}).get("inbound", {})
            if not inbound.get("smart_path_paste"):
                raise SmartPasteError(
                    "registered route does not support smart image paste"
                )
            progress = threading.Timer(
                PROGRESS_DELAY_SECONDS,
                notify,
                args=("Pasting image…",),
                kwargs={"pane": pane, "runner": runner},
            )
            progress.daemon = True
            progress.start()
            try:
                transfer_receipt = transfer_fn(
                    local_path,
                    ssh_target=record["ssh_target"],
                    extension=local_path.suffix,
                    remote_command=(
                        f"{record['remote_home']}/.local/bin/clipboard-artifact-receive"
                    ),
                    timeout_seconds=TRANSFER_TIMEOUT_SECONDS,
                )
            finally:
                progress.cancel()
            remote_path = str(transfer_receipt["path"])
        try:
            if change_count_fn() != snapshot.change_count:
                raise SmartPasteError("clipboard changed during image transfer")
            if not gesture_is_latest(latest_path, gesture_id):
                raise SmartPasteError("a newer paste superseded this gesture")
            if record is not None:
                verify_route_ownership(
                    pane=pane,
                    client=client,
                    route_path=route_path,
                    generation=generation,
                    runner=runner,
                )
            inject_bracketed_paste(
                pane=pane,
                data=remote_path.encode("utf-8"),
                gesture_id=gesture_id,
                runner=runner,
            )
        except BaseException as original:
            if (
                record is not None
                and transfer_receipt is not None
                and transfer_receipt.get("reused") is False
                and transfer_receipt.get("sha256")
            ):
                try:
                    cleanup_fn(
                        ssh_target=record["ssh_target"],
                        sha256=transfer_receipt["sha256"],
                        extension=local_path.suffix,
                        remote_command=(
                            f"{record['remote_home']}/.local/bin/clipboard-artifact-receive"
                        ),
                        timeout_seconds=TRANSFER_TIMEOUT_SECONDS,
                    )
                except BaseException as cleanup_error:
                    raise SmartPasteError(
                        f"{original}; remote cleanup also failed: {cleanup_error}"
                    ) from original
            raise
        outcome = "image_path" if snapshot.kind == "image" else "document_path"
        injected = {
            "kind": "path",
            "media_kind": snapshot.kind,
            "sha256": snapshot.sha256,
            "path": remote_path,
        }
    else:
        raise SmartPasteError(f"clipboard kind {snapshot.kind!r} is not enabled yet")

    finished = now()
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "gesture_id": gesture_id,
        "outcome": outcome,
        "pane": pane,
        "client": client,
        "route_id": record.get("route_id") if record else None,
        "route_generation": record.get("generation") if record else None,
        "clipboard": {**snapshot.public_dict(), "artifact": None},
        "transfer": transfer_receipt,
        "injected": injected,
        "started_at": started,
        "finished_at": finished,
        "latency_ms": round((finished - started) * 1000, 3),
    }
    receipt_path = write_receipt(runtime_root, receipt)
    receipt["receipt_path"] = str(receipt_path)
    return receipt


def smart_paste(
    *,
    capture_fn: Callable[
        ..., tuple[clipboard_snapshot.ClipboardSnapshot, str | None]
    ] = capture,
    route_path: Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run smart paste and destroy transient snapshots after remote use."""
    captured: list[Path] = []

    def tracked_capture(
        **capture_kwargs: Any,
    ) -> tuple[clipboard_snapshot.ClipboardSnapshot, str | None]:
        snapshot, text = capture_fn(**capture_kwargs)
        if snapshot.artifact:
            captured.append(Path(snapshot.artifact))
        return snapshot, text

    try:
        return _smart_paste_impl(
            capture_fn=tracked_capture,
            route_path=route_path,
            **kwargs,
        )
    finally:
        # Local agents still need their local path. A remote route owns an
        # independent verified copy, so its Mac-side snapshot must never linger.
        if route_path is not None:
            for artifact in captured:
                with contextlib.suppress(OSError):
                    artifact.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pane", required=True)
    parser.add_argument("--client", required=True)
    parser.add_argument("--route-path", type=Path)
    parser.add_argument("--generation")
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--cancel", action="store_true")
    parser.add_argument("--inject-text", action="store_true")
    parser.add_argument("--expected-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cancel:
        runtime_root = _private_runtime(args.runtime_root)
        mark_latest_gesture(runtime_root, args.pane, str(uuid.uuid4()))
        notify("Paste cancelled", pane=args.pane)
        return 0
    try:
        receipt = smart_paste(
            pane=args.pane,
            client=args.client,
            route_path=args.route_path,
            generation=args.generation,
            runtime_root=args.runtime_root,
            inject_text=args.inject_text,
            expected_sha256=args.expected_sha256,
        )
    except (
        OSError,
        SmartPasteError,
        clipboard_snapshot.SnapshotError,
        clipboard_transfer.TransferError,
    ) as exc:
        code = error_code(exc)
        notify(
            f"Paste stopped [{code}]: {exc}; press Cmd+V to retry or run clipboard-paste doctor",
            pane=args.pane,
        )
        runtime_root = _private_runtime(args.runtime_root)
        failure = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "gesture_id": str(uuid.uuid4()),
            "outcome": "rejected",
            "pane": args.pane,
            "client": args.client,
            "error": {"code": code, "message": str(exc)[:240]},
            "repair": "press Cmd+V to retry or run clipboard-paste doctor",
            "finished_at": time.time(),
        }
        receipt_path = write_receipt(runtime_root, failure)
        if args.json:
            json.dump(
                {**failure, "receipt_path": str(receipt_path)},
                sys.stdout,
                sort_keys=True,
                separators=(",", ":"),
            )
            sys.stdout.write("\n")
        else:
            print(f"clipboard-smart-paste: {exc}", file=sys.stderr)
        return 1
    if args.json:
        json.dump(receipt, sys.stdout, sort_keys=True, separators=(",", ":"))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
