"""Validate the Skillbox application deploy overlay without applying it."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import secrets
import stat
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OVERLAY_SCHEMA = "skillbox.application-deploy-overlay/1"
RECEIPT_SCHEMA = "skillbox.application-deploy-preflight/1"
DEFAULT_OVERLAY = ROOT / "workspace/project-orb-deploy.json"
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
IDENTITY_FIELDS = ("client_id", "source_commit", "payload_tree_sha256", "archive_sha256")


def _box_module() -> Any:
    spec = importlib.util.spec_from_file_location("skillbox_orb_deploy_box", ROOT / "scripts/box.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the Skillbox deploy contract")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_overlay(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected_commands = {
        "dry_run": [
            "python3", "scripts/box.py", "upgrade", "${BOX_ID}",
            "--deploy-manifest", "${DEPLOY_MANIFEST}", "--dry-run", "--format", "json",
        ],
        "health_check": [
            "python3", "scripts/box.py", "status", "${BOX_ID}", "--format", "json",
        ],
        "rollback": [
            "python3", "scripts/box.py", "upgrade", "${BOX_ID}",
            "--deploy-manifest", "${PREVIOUS_DEPLOY_MANIFEST}", "--dry-run", "--format", "json",
        ],
    }
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != OVERLAY_SCHEMA
        or payload.get("application") != {
            "id": "skillbox",
            "owner": "build000r/skillbox",
            "repo_slug": "build000r/skillbox",
        }
        or payload.get("artifact", {}).get("manifest_schema_version") != 1
        or payload.get("artifact", {}).get("ref_policy") != "origin/main"
        or tuple(payload.get("artifact", {}).get("identity_fields", ())) != IDENTITY_FIELDS
        or payload.get("dry_run", {}).get("mutates") is not False
        or payload.get("receipt_store") != {
            "root": ".skillbox-state/project-orb/deploy-receipts",
            "file_mode": "0600",
        }
        or payload.get("authority") != {
            "ordinary_project_orb": "forbidden",
            "production_apply": "operator_only",
        }
        or any(payload.get(key, {}).get("command_template") != value for key, value in expected_commands.items())
    ):
        raise ValueError("application deploy overlay violates the project-Orb contract")
    names = payload.get("credential_preflight", {}).get("environment_names")
    if (
        not isinstance(names, list)
        or not names
        or any(not isinstance(name, str) or ENV_NAME.fullmatch(name) is None for name in names)
        or payload.get("credential_preflight", {}).get("receipt_policy")
        != "names_and_configured_booleans_only"
    ):
        raise ValueError("deploy credential preflight must contain names only")
    denied = set(payload.get("least_privilege", {}).get("denied", ()))
    if not {
        "deploy.production_apply",
        "infrastructure.admin",
        "box.provision",
        "box.destroy",
        "dns.admin",
        "tailnet.admin",
        "credential.rotate",
    }.issubset(denied):
        raise ValueError("deploy overlay does not deny administrative authority")
    return payload


def _artifact(release: Any) -> dict[str, str]:
    return {
        "client_id": release.client_id,
        "source_commit": release.source_commit,
        "payload_tree_sha256": release.payload_tree_sha256,
        "archive_sha256": release.archive_sha256,
    }


def collect(
    overlay_path: Path,
    deploy_manifest: Path,
    *,
    box_id: str,
    previous_deploy_manifest: Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    overlay = _load_overlay(overlay_path)
    box = _box_module()
    current = box.load_deploy_manifest(deploy_manifest, expected_client_id=box_id)
    previous = (
        box.load_deploy_manifest(previous_deploy_manifest, expected_client_id=box_id)
        if previous_deploy_manifest is not None
        else None
    )
    environment = os.environ if env is None else env
    credentials = [
        {"name": name, "configured": bool(environment.get(name, "").strip())}
        for name in overlay["credential_preflight"]["environment_names"]
    ]
    current_artifact = _artifact(current)
    previous_artifact = _artifact(previous) if previous is not None else None
    rollback_ready = previous_artifact is not None and previous_artifact != current_artifact
    credentials_ready = all(item["configured"] for item in credentials)
    if not rollback_ready:
        state, reason = "blocked", "ROLLBACK_UNPROVEN"
    elif not credentials_ready:
        state, reason = "blocked", "CREDENTIAL_UNAVAILABLE"
    else:
        state, reason = "configured", "LOCAL_CONTRACT_READY_APPLY_FORBIDDEN"
    return {
        "schema_version": RECEIPT_SCHEMA,
        "application": overlay["application"],
        "overlay_sha256": hashlib.sha256(overlay_path.read_bytes()).hexdigest(),
        "state": state,
        "reason_code": reason,
        "dry_run": True,
        "network_attempted": False,
        "production_apply": "forbidden",
        "target": {"box_id": box_id},
        "artifact": current_artifact,
        "credential_preflight": credentials,
        "steps": [
            {"id": "artifact", "state": "ready"},
            {"id": "credentials", "state": "ready" if credentials_ready else "blocked"},
            {"id": "health", "state": "planned", "command": overlay["health_check"]["command_template"]},
            {
                "id": "rollback",
                "state": "ready" if rollback_ready else "blocked",
                "artifact": previous_artifact,
                "command": overlay["rollback"]["command_template"],
            },
            {"id": "apply", "state": "forbidden", "authority": "operator_only"},
        ],
        "dry_run_command": overlay["dry_run"]["command_template"],
        "least_privilege": overlay["least_privilege"],
    }


def _open_private_directory(path: Path) -> int:
    absolute = path.absolute()
    if any(part in {"", ".", ".."} for part in absolute.parts[1:]):
        raise ValueError("deploy receipt path is invalid")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:]:
            try:
                next_fd = os.open(part, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                os.mkdir(part, mode=0o700, dir_fd=directory_fd)
                next_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        details = os.fstat(directory_fd)
        if details.st_uid != os.geteuid():
            raise ValueError("deploy receipt directory must be owned by this user")
        os.fchmod(directory_fd, 0o700)
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def _open_receipt_parent(store_root: Path, parent: Path) -> int:
    store = store_root.absolute()
    destination_parent = parent.absolute()
    try:
        relative = destination_parent.relative_to(store)
    except ValueError as exc:
        raise ValueError("deploy receipt output must remain in the declared receipt store") from exc
    if ".." in relative.parts:
        raise ValueError("deploy receipt output must remain in the declared receipt store")
    try:
        directory_fd = _open_private_directory(store)
    except OSError as exc:
        raise ValueError("deploy receipt store ancestors must be real directories") from exc
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        for part in relative.parts:
            if part in {"", ".", ".."}:
                raise ValueError("deploy receipt path is invalid")
            try:
                next_fd = os.open(part, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                os.mkdir(part, mode=0o700, dir_fd=directory_fd)
                next_fd = os.open(part, flags, dir_fd=directory_fd)
            except OSError as exc:
                raise ValueError("deploy receipt store ancestors must be real directories") from exc
            os.close(directory_fd)
            directory_fd = next_fd
            details = os.fstat(directory_fd)
            if details.st_uid != os.geteuid():
                raise ValueError("deploy receipt directory must be owned by this user")
            os.fchmod(directory_fd, 0o700)
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def write_receipt(
    path: Path,
    payload: dict[str, Any],
    *,
    store_root: Path | None = None,
) -> None:
    if not path.name or path.name in {".", ".."}:
        raise ValueError("deploy receipt destination must name a regular file")
    temporary = f".{path.name}.{secrets.token_hex(12)}"
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    directory_fd = _open_receipt_parent(store_root or path.parent, path.parent)
    file_fd = -1
    try:
        try:
            details = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(details.st_mode):
                raise ValueError("deploy receipt destination must be a regular file")
        file_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(file_fd, 0o600)
        view = memoryview(body)
        while view:
            written = os.write(file_fd, view)
            if written == 0:
                raise OSError("deploy receipt write made no progress")
            view = view[written:]
        os.fsync(file_fd)
        os.close(file_fd)
        file_fd = -1
        os.replace(
            temporary,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if file_fd >= 0:
            with suppress(OSError):
                os.close(file_fd)
        with suppress(OSError):
            os.unlink(temporary, dir_fd=directory_fd)
        with suppress(OSError):
            os.close(directory_fd)


def receipt_destination(
    overlay: dict[str, Any],
    payload: dict[str, Any],
    requested: Path | None,
) -> Path:
    store_relative = Path(overlay["receipt_store"]["root"])
    if store_relative.is_absolute() or ".." in store_relative.parts:
        raise ValueError("deploy receipt store must be repository-relative")
    store = (ROOT / store_relative).absolute()
    if requested is None:
        return store / f"preflight-{payload['artifact']['archive_sha256']}.json"
    destination = (requested if requested.is_absolute() else ROOT / requested).absolute()
    try:
        relative = destination.relative_to(store)
    except ValueError as exc:
        raise ValueError("deploy receipt output must remain in the declared receipt store") from exc
    if ".." in relative.parts:
        raise ValueError("deploy receipt output must remain in the declared receipt store")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overlay", type=Path, default=DEFAULT_OVERLAY)
    parser.add_argument("--deploy-manifest", type=Path, required=True)
    parser.add_argument("--previous-deploy-manifest", type=Path)
    parser.add_argument("--box-id", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        payload = collect(
            args.overlay,
            args.deploy_manifest,
            box_id=args.box_id,
            previous_deploy_manifest=args.previous_deploy_manifest,
        )
        overlay = _load_overlay(args.overlay)
        destination = receipt_destination(overlay, payload, args.output)
        store_root = ROOT / overlay["receipt_store"]["root"]
        write_receipt(destination, payload, store_root=store_root)
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0 if payload["state"] == "configured" else 2
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "schema_version": RECEIPT_SCHEMA,
                    "state": "blocked",
                    "reason_code": "DEPLOY_PREFLIGHT_INVALID",
                    "message": str(exc),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
