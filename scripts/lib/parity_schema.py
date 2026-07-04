from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


PARITY_LEDGER_INVALID = "PARITY_LEDGER_INVALID"

PARITY_SURFACE_TYPES = frozenset({"service", "env_target", "helper", "flag"})
PARITY_LEDGER_ACTIONS = frozenset({"declare", "bridge", "build", "drop"})
PARITY_OWNERSHIP_STATES = frozenset({"covered", "bridge-only", "deferred", "external"})

LOCAL_RUNTIME_ERROR_CODES = frozenset(
    {
        "LOCAL_RUNTIME_ENV_BRIDGE_FAILED",
        "LOCAL_RUNTIME_ENV_OUTPUT_MISSING",
        "LOCAL_RUNTIME_PROFILE_UNKNOWN",
        "LOCAL_RUNTIME_START_BLOCKED",
        "LOCAL_RUNTIME_PORT_MISMATCH",
        "LOCAL_RUNTIME_SERVICE_DEFERRED",
        "LOCAL_RUNTIME_MODE_UNSUPPORTED",
        "LOCAL_RUNTIME_COVERAGE_GAP",
        "LOCAL_RUNTIME_DEPENDENCY_CYCLE",
        "LOCAL_RUNTIME_DEPENDENCY_UNKNOWN",
    }
)

_LEGACY_SURFACE_TYPE_ALIASES = {"bridge": "helper"}


@dataclass(frozen=True)
class ParityLedgerWarning:
    field: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "message": self.message}


class ParityLedgerSchemaError(ValueError):
    code = PARITY_LEDGER_INVALID

    def __init__(
        self,
        message: str,
        *,
        issues: list[str],
        provenance: dict[str, Any],
    ) -> None:
        super().__init__(message)
        self.issues = tuple(issues)
        self.provenance = dict(provenance)
        self.context = {
            "issues": list(self.issues),
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True)
class ParityLedgerRow:
    id: str
    surface_type: str
    action: str
    ownership_state: str
    intended_profiles: tuple[str, ...]
    bridge_dependency: str | None
    request_error: str
    legacy_surface: str = ""
    notes: str = ""
    profiles: tuple[str, ...] = ()
    client: str = ""
    warnings: tuple[ParityLedgerWarning, ...] = field(default_factory=tuple)
    provenance: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def surface_id(self) -> str:
        return self.legacy_surface or self.id

    @property
    def is_service_row(self) -> bool:
        return self.surface_type == "service"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "legacy_surface": self.legacy_surface,
            "surface_type": self.surface_type,
            "action": self.action,
            "ownership_state": self.ownership_state,
            "intended_profiles": list(self.intended_profiles),
            "bridge_dependency": self.bridge_dependency,
            "request_error": self.request_error,
            "notes": self.notes,
            "profiles": list(self.profiles),
            "client": self.client,
        }


@dataclass(frozen=True)
class ServiceSurfaceRow(ParityLedgerRow):
    pass


@dataclass(frozen=True)
class EnvTargetRow(ParityLedgerRow):
    pass


@dataclass(frozen=True)
class HelperRow(ParityLedgerRow):
    pass


@dataclass(frozen=True)
class FlagRow(ParityLedgerRow):
    pass


_ROW_TYPES: dict[str, type[ParityLedgerRow]] = {
    "service": ServiceSurfaceRow,
    "env_target": EnvTargetRow,
    "helper": HelperRow,
    "flag": FlagRow,
}


def _clean_string(raw: Any, field_name: str, errors: list[str], *, default: str = "") -> str:
    if raw is None:
        return default
    if not isinstance(raw, str):
        errors.append(f"{field_name} must be a string")
        return default
    return raw.strip()


def _optional_clean_string(raw: Any, field_name: str, errors: list[str]) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        errors.append(f"{field_name} must be a string or null")
        return None
    value = raw.strip()
    return value or None


def _clean_string_list(raw: Any, field_name: str, errors: list[str]) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        errors.append(f"{field_name} must be a list of strings")
        return ()
    values: list[str] = []
    for index, value in enumerate(raw):
        if not isinstance(value, str):
            errors.append(f"{field_name}[{index}] must be a string")
            continue
        cleaned = value.strip()
        if cleaned:
            values.append(cleaned)
    return tuple(values)


def _row_provenance(row: Mapping[str, Any], index: int | None, source: str) -> dict[str, Any]:
    provenance: dict[str, Any] = {"section": "parity_ledger"}
    if index is not None:
        provenance["row_index"] = index
    if source:
        provenance["source_file"] = source
    for field_name in ("id", "client", "surface_type"):
        value = row.get(field_name)
        if isinstance(value, str) and value.strip():
            provenance[field_name] = value.strip()
    return provenance


def _raise_schema_error(issues: list[str], provenance: dict[str, Any]) -> None:
    summary = "; ".join(issues)
    raise ParityLedgerSchemaError(
        f"Invalid parity_ledger row: {summary}",
        issues=issues,
        provenance=provenance,
    )


def _known_local_runtime_code(raw_code: str, known_error_codes: frozenset[str]) -> bool:
    return raw_code.startswith("LOCAL_RUNTIME_") and raw_code in known_error_codes


def _cross_field_warnings(
    *,
    ownership_state: str,
    intended_profiles: tuple[str, ...],
    bridge_dependency: str | None,
    request_error: str,
    known_error_codes: frozenset[str],
) -> list[ParityLedgerWarning]:
    warnings: list[ParityLedgerWarning] = []
    if ownership_state == "bridge-only" and not bridge_dependency:
        warnings.append(
            ParityLedgerWarning(
                "bridge_dependency",
                "bridge-only parity-ledger rows should declare bridge_dependency",
            )
        )
    if ownership_state == "deferred" and not _known_local_runtime_code(request_error, known_error_codes):
        warnings.append(
            ParityLedgerWarning(
                "request_error",
                "deferred parity-ledger rows should declare a known LOCAL_RUNTIME_* request_error",
            )
        )
    if ownership_state == "covered" and not intended_profiles:
        warnings.append(
            ParityLedgerWarning(
                "intended_profiles",
                "covered parity-ledger rows should declare non-empty intended_profiles",
            )
        )
    return warnings


def parse_ledger_row(
    row: dict[str, Any],
    *,
    index: int | None = None,
    source: str = "",
    known_error_codes: frozenset[str] | set[str] | None = None,
    strict_cross_fields: bool = False,
) -> ParityLedgerRow:
    if not isinstance(row, dict):
        provenance = {"section": "parity_ledger"}
        if index is not None:
            provenance["row_index"] = index
        _raise_schema_error(["row must be an object"], provenance)

    known_codes = frozenset(known_error_codes or LOCAL_RUNTIME_ERROR_CODES)
    provenance = _row_provenance(row, index, source)
    errors: list[str] = []

    row_id = _clean_string(row.get("id"), "id", errors)
    if not row_id:
        errors.append("id is required")
    surface_type = _clean_string(row.get("surface_type"), "surface_type", errors, default="service") or "service"
    surface_type = _LEGACY_SURFACE_TYPE_ALIASES.get(surface_type, surface_type)
    if surface_type not in PARITY_SURFACE_TYPES:
        errors.append(
            "surface_type must be one of: "
            + ", ".join(sorted(PARITY_SURFACE_TYPES))
        )

    action = _clean_string(row.get("action"), "action", errors, default="build") or "build"
    if action not in PARITY_LEDGER_ACTIONS:
        errors.append(
            "action must be one of: "
            + ", ".join(sorted(PARITY_LEDGER_ACTIONS))
        )

    ownership_state = (
        _clean_string(row.get("ownership_state"), "ownership_state", errors, default="deferred")
        or "deferred"
    )
    if ownership_state not in PARITY_OWNERSHIP_STATES:
        errors.append(
            "ownership_state must be one of: "
            + ", ".join(sorted(PARITY_OWNERSHIP_STATES))
        )

    intended_profiles = _clean_string_list(row.get("intended_profiles", []), "intended_profiles", errors)
    profiles = _clean_string_list(row.get("profiles", []), "profiles", errors)
    bridge_dependency = _optional_clean_string(row.get("bridge_dependency"), "bridge_dependency", errors)
    request_error = _clean_string(row.get("request_error"), "request_error", errors)
    legacy_surface = _clean_string(row.get("legacy_surface"), "legacy_surface", errors)
    notes = _clean_string(row.get("notes"), "notes", errors)
    client = _clean_string(row.get("client"), "client", errors)

    if request_error and not _known_local_runtime_code(request_error, known_codes):
        errors.append("request_error must be a known LOCAL_RUNTIME_* code")

    warnings = _cross_field_warnings(
        ownership_state=ownership_state,
        intended_profiles=intended_profiles,
        bridge_dependency=bridge_dependency,
        request_error=request_error,
        known_error_codes=known_codes,
    )
    if strict_cross_fields:
        errors.extend(warning.message for warning in warnings)

    if errors:
        _raise_schema_error(errors, provenance)

    row_type = _ROW_TYPES[surface_type]
    return row_type(
        id=row_id,
        legacy_surface=legacy_surface,
        surface_type=surface_type,
        action=action,
        ownership_state=ownership_state,
        intended_profiles=intended_profiles,
        bridge_dependency=bridge_dependency,
        request_error=request_error,
        notes=notes,
        profiles=profiles,
        client=client,
        warnings=tuple(warnings),
        provenance=provenance,
    )
