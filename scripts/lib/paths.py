"""Typed host/box path translation from persistence bindings."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


PATH_UNMAPPED = "PATH_UNMAPPED"

Pathish = str | os.PathLike[str]


@dataclass(frozen=True)
class HostPath:
    """Host filesystem path wrapper."""

    path: Pathish

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path).expanduser())

    def __fspath__(self) -> str:
        return str(self.path)

    def __repr__(self) -> str:
        return str(self)

    def __str__(self) -> str:
        return str(self.path)


@dataclass(frozen=True)
class BoxPath:
    """In-box filesystem path wrapper."""

    path: Pathish

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", PurePosixPath(str(self.path)))

    def __fspath__(self) -> str:
        return str(self.path)

    def __repr__(self) -> str:
        return str(self)

    def __str__(self) -> str:
        return str(self.path)


class PathTranslationError(RuntimeError):
    """Raised when a path is outside every declared persistence binding."""

    def __init__(self, *, code: str, path: object, direction: str) -> None:
        self.code = code
        self.path = str(path)
        self.direction = direction
        super().__init__(f"{code}: cannot translate {direction} path {path}")


@dataclass(frozen=True)
class _PathBinding:
    id: str
    host_root: Path
    box_root: PurePosixPath


class PathTranslator:
    """Translate HostPath and BoxPath values through persistence bindings."""

    def __init__(self, bindings: Sequence[_PathBinding]) -> None:
        if not bindings:
            raise ValueError("PathTranslator requires at least one binding")
        normalized = tuple(
            _PathBinding(
                id=binding.id,
                host_root=Path(binding.host_root).expanduser().resolve(),
                box_root=PurePosixPath(str(binding.box_root)),
            )
            for binding in bindings
        )
        self._host_bindings = tuple(
            sorted(normalized, key=lambda binding: len(binding.host_root.parts), reverse=True)
        )
        self._box_bindings = tuple(
            sorted(normalized, key=lambda binding: len(binding.box_root.parts), reverse=True)
        )

    @classmethod
    def from_storage(cls, storage: Mapping[str, Any]) -> PathTranslator:
        """Build a translator from a compiled runtime_model storage summary."""
        bindings: list[_PathBinding] = []
        for index, raw_binding in enumerate(storage.get("bindings") or [], start=1):
            if not isinstance(raw_binding, Mapping):
                raise ValueError(f"storage bindings[{index}] must be a mapping")
            binding_id = str(raw_binding.get("id") or f"binding-{index}").strip()
            box_root = str(raw_binding.get("runtime_path") or "").strip()
            host_root = str(raw_binding.get("resolved_host_path") or "").strip()
            if not box_root or not host_root:
                raise ValueError(f"storage binding {binding_id!r} must include runtime_path and resolved_host_path")
            bindings.append(
                _PathBinding(
                    id=binding_id,
                    host_root=Path(host_root),
                    box_root=PurePosixPath(box_root),
                )
            )
        return cls(bindings)

    @classmethod
    def from_persistence(cls, root_dir: Pathish, env_values: Mapping[str, str] | None = None) -> PathTranslator:
        """Compile workspace/persistence.yaml and build its translator."""
        from lib.runtime_model import compile_persistence_summary

        root_path = Path(root_dir).expanduser().resolve()
        storage = compile_persistence_summary(root_path, dict(env_values or {}))
        return cls.from_storage(storage)

    @classmethod
    def from_model(cls, model: Mapping[str, Any]) -> PathTranslator:
        """Build from model storage, falling back to legacy env mappings for test models."""
        root_dir = Path(str(model.get("root_dir") or ".")).expanduser().resolve()
        env_values = {
            str(key): str(value)
            for key, value in (model.get("env") or {}).items()
        }

        storage = model.get("storage")
        if isinstance(storage, Mapping) and storage.get("bindings"):
            try:
                return cls.from_storage(storage)
            except ValueError:
                pass

        try:
            return cls.from_persistence(root_dir, env_values)
        except (ImportError, RuntimeError):
            return cls._from_legacy_env(root_dir, env_values)

    @classmethod
    def _from_legacy_env(cls, root_dir: Path, env_values: Mapping[str, str]) -> PathTranslator:
        try:
            from lib.runtime_model import (
                client_configs_host_root,
                client_configs_runtime_root,
                host_path_to_absolute_path,
            )
        except ImportError:
            def client_configs_runtime_root(values: Mapping[str, str]) -> str:
                return values.get("SKILLBOX_CLIENTS_ROOT") or "/workspace/clients"

            def client_configs_host_root(root: Path, _values: Mapping[str, str]) -> Path:
                return root / "workspace" / "clients"

            def host_path_to_absolute_path(root: Path, raw: str) -> Path:
                return (root / raw).resolve()

        workspace_root = PurePosixPath(str(env_values.get("SKILLBOX_WORKSPACE_ROOT") or "/workspace"))
        home_root = PurePosixPath(str(env_values.get("SKILLBOX_HOME_ROOT") or "/home/sandbox"))
        monoserver_root = PurePosixPath(str(env_values.get("SKILLBOX_MONOSERVER_ROOT") or "/monoserver"))
        clients_root = PurePosixPath(str(client_configs_runtime_root(dict(env_values))))
        clients_host_root = client_configs_host_root(root_dir, dict(env_values))
        monoserver_host_root = host_path_to_absolute_path(
            root_dir,
            str(env_values.get("SKILLBOX_MONOSERVER_HOST_ROOT") or ".."),
        )
        return cls([
            _PathBinding("clients-root", clients_host_root, clients_root),
            _PathBinding("workspace-root", root_dir, workspace_root),
            _PathBinding("home-root", root_dir / "home", home_root),
            _PathBinding("monoserver-root", monoserver_host_root, monoserver_root),
        ])

    def to_box(self, host: HostPath | Pathish) -> BoxPath:
        """Translate a host path to its in-box path."""
        candidate = self._host_candidate(host)
        for binding in self._host_bindings:
            try:
                relative = candidate.relative_to(binding.host_root)
            except ValueError:
                continue
            if not relative.parts:
                return BoxPath(binding.box_root)
            return BoxPath(binding.box_root.joinpath(*relative.parts))
        raise PathTranslationError(code=PATH_UNMAPPED, path=host, direction="host-to-box")

    def to_host(self, box: BoxPath | Pathish) -> HostPath:
        """Translate an in-box path to its host path."""
        candidate = self._box_candidate(box)
        for binding in self._box_bindings:
            try:
                relative = candidate.relative_to(binding.box_root)
            except ValueError:
                continue
            if not relative.parts:
                return HostPath(binding.host_root)
            return HostPath((binding.host_root / Path(*relative.parts)).resolve())
        raise PathTranslationError(code=PATH_UNMAPPED, path=box, direction="box-to-host")

    @staticmethod
    def _host_candidate(host: HostPath | Pathish) -> Path:
        if isinstance(host, HostPath):
            return Path(host.path).expanduser().resolve()
        return Path(host).expanduser().resolve()

    @staticmethod
    def _box_candidate(box: BoxPath | Pathish) -> PurePosixPath:
        if isinstance(box, BoxPath):
            return PurePosixPath(str(box.path))
        return PurePosixPath(str(box))
