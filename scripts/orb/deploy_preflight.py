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
    credential_env = os.environ if env is None else env
    credentials = [
        {"name": name, "configured": bool(credential_env.get(name, "").strip())}
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
    if ".." in absolute.parts:
        raise ValueError("deploy receipt path cannot contain parent traversal")
    directory_fd = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(absolute.parts[1:]):
            created = False
            try:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                os.mkdir(part, mode=0o700, dir_fd=directory_fd)
                created = True
                os.chmod(part, 0o700, dir_fd=directory_fd)
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
            os.close(directory_fd)
            directory_fd = next_fd
            details = os.fstat(directory_fd)
            mode = stat.S_IMODE(details.st_mode)
            is_final = index == len(absolute.parts[1:]) - 1
            if details.st_uid not in {0, os.geteuid()}:
                raise ValueError("deploy receipt ancestors must have trusted ownership")
            if mode & 0o022 and not (
                details.st_uid == 0 and details.st_mode & stat.S_ISVTX
            ):
                raise ValueError("deploy receipt ancestors cannot be group/world writable")
            if is_final:
                if details.st_uid != os.geteuid():
                    raise ValueError("deploy receipt parent must be owned by this user")
                if created:
                    os.fchmod(directory_fd, 0o700)
                if stat.S_IMODE(os.fstat(directory_fd).st_mode) != 0o700:
                    raise ValueError("deploy receipt parent must have mode 0700")
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def write_receipt(path: Path, payload: dict[str, Any]) -> None:
    if not path.name or path.name in {".", ".."}:
        raise ValueError("deploy receipt destination must name a file")
    directory_fd = _open_private_directory(path.parent)
    temporary = f".{path.name}.{secrets.token_hex(8)}"
    file_fd: int | None = None
    try:
        try:
            existing = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ValueError("deploy receipt destination must be a regular file")
        file_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(file_fd, 0o600)
        with os.fdopen(file_fd, "w", encoding="utf-8") as stream:
            file_fd = None
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temporary,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        try:
            if file_fd is not None:
                os.close(file_fd)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        finally:
            os.close(directory_fd)


def receipt_destination(
    overlay: dict[str, Any],
    payload: dict[str, Any],
    requested: Path | None,
) -> Path:
    store_relative = Path(overlay["receipt_store"]["root"])
    if ".." in store_relative.parts or (requested is not None and ".." in requested.parts):
        raise ValueError("deploy receipt path cannot contain parent traversal")
    raw_root = (ROOT / store_relative).absolute()
    cursor = ROOT
    for part in store_relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError("deploy receipt store cannot contain symlinks")
    store = raw_root
    if requested is None:
        return store / f"preflight-{payload['artifact']['archive_sha256']}.json"
    candidate = requested if requested.is_absolute() else ROOT / requested
    destination = candidate.absolute()
    if destination.parent != store and store not in destination.parents:
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
        write_receipt(receipt_destination(overlay, payload, args.output), payload)
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
