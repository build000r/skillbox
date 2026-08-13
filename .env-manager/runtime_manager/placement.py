"""Pure machine-placement decision.

Snapshot in, decision out. ``decide()`` does no I/O, no filesystem, and no
network. ``gather_observations()`` is the only helper that interprets live
inventory state, and it still takes explicit arguments.

Provider identity is out of scope: this module never branches on a provider
string and never calls a provision function. When nothing eligible exists and
policy allows, the decision carries a ``box.py up … --dry-run`` next-action.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .machines import MachinesConfig, is_closed_cap

KIND = "machine-placement/v1"
DECISIONS = frozenset({"selected", "no_match", "provision_proposed", "denied"})
EXCLUDED_BOX_STATES = frozenset(
    {"draining", "destroy-pending", "volume-cleanup-failed", "destroyed"}
)
TRUST_RANK = {"local": 3, "allowlisted": 2, "explicit": 1}
PROVISION_TRUST = "explicit"
SOURCE_SELF = "self"
SOURCE_INVENTORY = "inventory-state"


def decide(
    needs: dict[str, Any] | None,
    config: MachinesConfig,
    boxes: Iterable[Any],
    observations: Mapping[str, Any] | None,
    profiles: Mapping[str, Any] | Iterable[Any] | None,
    current_id: str | None,
) -> dict[str, Any]:
    """Rank declared machines ∪ boxes and return a typed placement decision.

    Candidates exclude box states in ``EXCLUDED_BOX_STATES``. A declared
    machine whose box is in an excluded state stays a candidate (declared
    identity only). Eligibility fails closed on missing caps, trust floor,
    known-unreachable, and missing observations (unless ``allow_unverified``).
    """
    normalized = _normalize_needs(needs)
    profile_list = _profiles_in_order(profiles)
    box_map = _index_boxes(boxes)
    candidates = _collect_candidates(config, box_map, profile_list)

    rejected: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    for row in candidates:
        reasons = _reject_reasons(row, normalized, observations)
        if reasons:
            rejected.append({"id": row["id"], "reasons": reasons})
        else:
            eligible.append(row)

    need_rank = _trust_rank(normalized["trust"])
    if eligible:
        chosen = _rank_eligible(eligible, current_id, profile_list)[0]
        return _payload(
            decision="selected",
            needs=normalized,
            reasons=_selection_reasons(chosen, current_id, normalized),
            rejected=rejected,
            next_actions=[],
            machine_id=chosen["id"],
            box_id=chosen.get("box_id"),
        )

    provision_profile = _provisionable_profile(normalized, profile_list, need_rank)
    if normalized["allow_provision"] and provision_profile is not None:
        profile_id = str(_attr(provision_profile, "id") or "")
        return _payload(
            decision="provision_proposed",
            needs=normalized,
            reasons=["no_eligible_machine", "provision_proposed"],
            rejected=_reject_all(candidates, normalized, observations),
            next_actions=[_provision_next_action(profile_id)],
            machine_id=None,
            box_id=None,
        )

    trust_floor_met = any(_trust_ok(row, need_rank) for row in candidates)
    if normalized["trust"] and not trust_floor_met:
        return _payload(
            decision="denied",
            needs=normalized,
            reasons=["no_eligible_machine", "trust_below_floor"],
            rejected=_reject_all(candidates, normalized, observations),
            next_actions=[],
            machine_id=None,
            box_id=None,
        )

    return _payload(
        decision="no_match",
        needs=normalized,
        reasons=["no_eligible_machine"],
        rejected=_reject_all(candidates, normalized, observations),
        next_actions=[],
        machine_id=None,
        box_id=None,
    )


def machine_view(
    config: MachinesConfig,
    boxes: Iterable[Any],
    profiles: Mapping[str, Any] | Iterable[Any] | None = None,
) -> list[dict[str, Any]]:
    """Union machines.yaml identities with box instances by exact id join.

    Destroyed / draining boxes stay in the view (``box_state`` tells the
    truth). Placement candidates are a filtered subset of this union.
    """
    profile_list = _profiles_in_order(profiles)
    box_map = _index_boxes(boxes)
    ids: list[str] = []
    seen: set[str] = set()
    for machine_id in config.machines:
        ids.append(machine_id)
        seen.add(machine_id)
    for box_id in box_map:
        if box_id not in seen:
            ids.append(box_id)
            seen.add(box_id)

    rows: list[dict[str, Any]] = []
    for machine_id in ids:
        declared = config.machines.get(machine_id)
        box = box_map.get(machine_id)
        caps = _union_caps(
            declared.caps if declared is not None else (),
            _derived_box_caps(box, profile_list),
        )
        trust = declared.trust if declared is not None else None
        sources: list[str] = []
        if declared is not None:
            sources.append("machines.yaml")
        if box is not None:
            sources.append("boxes")
        rows.append(
            {
                "id": machine_id,
                "kind": _derived_kind(declared is not None, box, caps),
                "caps": list(caps),
                "trust": trust,
                "box_state": str(_attr(box, "state")) if box is not None else None,
                "sources": sources,
            }
        )
    return rows


def gather_observations(
    current_id: str | None,
    boxes: Iterable[Any],
) -> dict[str, dict[str, Any]]:
    """Build the observation snapshot ``decide()`` consumes.

    current machine → ``{reachable: True, source: self}``;
    box ``state==ready`` → ``{reachable: True, source: inventory-state}``;
    anything else is omitted (treated as unverified).
    """
    observations: dict[str, dict[str, Any]] = {}
    for box in boxes or ():
        box_id = str(_attr(box, "id") or "").strip()
        if not box_id:
            continue
        if str(_attr(box, "state") or "") == "ready":
            observations[box_id] = {
                "reachable": True,
                "source": SOURCE_INVENTORY,
            }
    current = str(current_id or "").strip()
    if current:
        observations[current] = {"reachable": True, "source": SOURCE_SELF}
    return observations


# ---------------------------------------------------------------------------
# Internals (pure)
# ---------------------------------------------------------------------------


def _normalize_needs(needs: dict[str, Any] | None) -> dict[str, Any]:
    raw = needs if isinstance(needs, dict) else {}
    caps: list[str] = []
    seen: set[str] = set()
    for item in raw.get("caps") or ():
        token = str(item).strip()
        if not token or token in seen:
            continue
        seen.add(token)
        caps.append(token)
    trust_raw = raw.get("trust")
    trust = str(trust_raw).strip() if trust_raw not in (None, "") else None
    if trust not in TRUST_RANK:
        trust = None
    return {
        "caps": caps,
        "trust": trust,
        "allow_unverified": bool(raw.get("allow_unverified", False)),
        "allow_provision": bool(raw.get("allow_provision", False)),
    }


def _payload(
    *,
    decision: str,
    needs: dict[str, Any],
    reasons: list[str],
    rejected: list[dict[str, Any]],
    next_actions: list[str],
    machine_id: str | None,
    box_id: str | None,
) -> dict[str, Any]:
    return {
        "kind": KIND,
        "decision": decision,
        "machine_id": machine_id,
        "box_id": box_id,
        "reasons": list(reasons),
        "rejected": sorted(rejected, key=lambda row: str(row.get("id") or "")),
        "needs": dict(needs),
        "next_actions": list(next_actions),
    }


def _collect_candidates(
    config: MachinesConfig,
    box_map: dict[str, Any],
    profiles: list[Any],
) -> list[dict[str, Any]]:
    ids: list[str] = []
    seen: set[str] = set()
    for machine_id in config.machines:
        ids.append(machine_id)
        seen.add(machine_id)
    for box_id, box in box_map.items():
        state = str(_attr(box, "state") or "")
        if state in EXCLUDED_BOX_STATES:
            continue
        if box_id not in seen:
            ids.append(box_id)
            seen.add(box_id)

    rows: list[dict[str, Any]] = []
    for machine_id in ids:
        declared = config.machines.get(machine_id)
        box = box_map.get(machine_id)
        if box is not None and str(_attr(box, "state") or "") in EXCLUDED_BOX_STATES:
            box = None
        caps = _union_caps(
            declared.caps if declared is not None else (),
            _derived_box_caps(box, profiles),
        )
        size = ""
        if box is not None:
            size = str(_attr(box, "size") or "")
            if not size:
                profile = _lookup_profile(profiles, _attr(box, "profile"))
                size = str(_attr(profile, "size") or "")
        rows.append(
            {
                "id": machine_id,
                "caps": caps,
                "trust": declared.trust if declared is not None else None,
                "declared": declared is not None,
                "durable": declared is not None or "durable" in caps,
                "box_id": str(_attr(box, "id")) if box is not None else None,
                "size": size,
            }
        )
    return rows


def _reject_reasons(
    row: dict[str, Any],
    needs: dict[str, Any],
    observations: Mapping[str, Any] | None,
) -> list[str]:
    reasons: list[str] = []
    have = set(row.get("caps") or ())
    for token in needs["caps"]:
        if token not in have:
            reasons.append(f"missing_caps:{token}")
    if not _trust_ok(row, _trust_rank(needs["trust"])):
        reasons.append("trust_below_floor")
    observation = _observation(observations, row["id"])
    if observation is None or observation.get("reachable") is None:
        if not needs["allow_unverified"]:
            reasons.append("unverified")
    elif observation.get("reachable") is False:
        reasons.append("unreachable")
    return reasons


def _reject_all(
    candidates: list[dict[str, Any]],
    needs: dict[str, Any],
    observations: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    return [
        {"id": row["id"], "reasons": _reject_reasons(row, needs, observations)}
        for row in candidates
    ]


def _selection_reasons(
    row: dict[str, Any],
    current_id: str | None,
    needs: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    if current_id and row["id"] == current_id:
        reasons.append("prefer_current")
    if row.get("declared") or row.get("durable"):
        reasons.append("declared")
    if needs["caps"]:
        reasons.append("caps_match")
    if not reasons:
        reasons.append("eligible")
    return reasons


def _rank_eligible(
    eligible: list[dict[str, Any]],
    current_id: str | None,
    profiles: list[Any],
) -> list[dict[str, Any]]:
    size_order = _size_order(profiles)

    def key(row: dict[str, Any]) -> tuple[int, int, int, str]:
        current_rank = 0 if current_id and row["id"] == current_id else 1
        durable_rank = 0 if row.get("declared") or row.get("durable") else 1
        size = str(row.get("size") or "")
        if size in size_order:
            size_rank = size_order[size]
        elif size:
            size_rank = len(size_order)
        else:
            size_rank = -1
        return (current_rank, durable_rank, size_rank, row["id"])

    return sorted(eligible, key=key)


def _provisionable_profile(
    needs: dict[str, Any],
    profiles: list[Any],
    need_rank: int,
) -> Any | None:
    if _trust_rank(PROVISION_TRUST) < need_rank:
        return None
    required = list(needs["caps"])
    for profile in profiles:
        caps = set(_caps_from_image_size(_attr(profile, "image"), _attr(profile, "size")))
        if all(token in caps for token in required):
            return profile
    return None


def _provision_next_action(profile_id: str) -> str:
    return (
        f"python3 scripts/box.py up {profile_id} --profile {profile_id} "
        "--dry-run --format json"
    )


def _derived_box_caps(box: Any, profiles: list[Any]) -> tuple[str, ...]:
    if box is None:
        return ()
    profile = _lookup_profile(profiles, _attr(box, "profile"))
    image = _attr(profile, "image")
    size = _attr(box, "size") or _attr(profile, "size")
    return _caps_from_image_size(image, size)


def _caps_from_image_size(image: Any, size: Any) -> tuple[str, ...]:
    image_text = str(image or "")
    size_text = str(size or "")
    caps: list[str] = []
    if image_text.startswith("ubuntu-"):
        caps.append("os:linux")
    if image_text.endswith("x64") or "amd" in size_text:
        caps.append("arch:amd64")
    return tuple(caps)


def _union_caps(*groups: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for group in groups:
        for token in group or ():
            text = str(token).strip()
            if not text or text in seen:
                continue
            if not is_closed_cap(text):
                continue
            seen.add(text)
            ordered.append(text)
    return tuple(ordered)


def _derived_kind(declared: bool, box: Any, caps: Iterable[str]) -> str:
    if box is None and any(token == "os:darwin" or token.startswith("os:darwin") for token in caps):
        return "physical"
    management = str(_attr(box, "management_mode") or "") if box is not None else ""
    if declared or management == "external":
        return "persistent"
    return "ephemeral"


def _trust_rank(trust: str | None) -> int:
    if not trust:
        return 0
    return int(TRUST_RANK.get(trust, 0))


def _trust_ok(row: dict[str, Any], need_rank: int) -> bool:
    if need_rank <= 0:
        return True
    return _trust_rank(row.get("trust")) >= need_rank


def _observation(
    observations: Mapping[str, Any] | None,
    machine_id: str,
) -> dict[str, Any] | None:
    if not observations:
        return None
    raw = observations.get(machine_id)
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return dict(raw)
    return None


def _index_boxes(boxes: Iterable[Any] | None) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    for box in boxes or ():
        box_id = str(_attr(box, "id") or "").strip()
        if box_id and box_id not in indexed:
            indexed[box_id] = box
    return indexed


def _profiles_in_order(
    profiles: Mapping[str, Any] | Iterable[Any] | None,
) -> list[Any]:
    if not profiles:
        return []
    if isinstance(profiles, Mapping):
        ordered: list[Any] = []
        for key, value in profiles.items():
            if _attr(value, "id") in (None, ""):
                if isinstance(value, dict):
                    merged = dict(value)
                    merged.setdefault("id", key)
                    ordered.append(merged)
                else:
                    ordered.append(value)
            else:
                ordered.append(value)
        return ordered
    return list(profiles)


def _lookup_profile(profiles: list[Any], profile_id: Any) -> Any | None:
    wanted = str(profile_id or "").strip()
    if not wanted:
        return None
    for profile in profiles:
        if str(_attr(profile, "id") or "") == wanted:
            return profile
    return None


def _size_order(profiles: list[Any]) -> dict[str, int]:
    order: dict[str, int] = {}
    for profile in profiles:
        size = str(_attr(profile, "size") or "").strip()
        if size and size not in order:
            order[size] = len(order)
    return order


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)
