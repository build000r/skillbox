"""Fail-closed preflight for the Oracle enrollment script.

Operator report, 2026-08-06: a Mac checkout of ``oracle-enroll-forward.sh`` that
predated the B4 hardening still had ``DEFAULT_DISPLAY=:99`` and ``-nopw``.
Running it would have attached a **passwordless** VNC server to ``:99`` — an
unrelated cypress-deps Xvfb started with ``-ac``, access control off — instead
of the Oracle display. Nothing failed; it simply did the wrong, open thing.

This module is the guard that makes that impossible to do silently. It is a
preflight: run it before invoking the enrollment script, and refuse to invoke on
anything but a clean verdict.

Two layers, because either alone is weak:

* **Identity** — the script's SHA-256 must be one this repo has reviewed
  (:data:`PINNED_DIGESTS`). That answers "is this the file we audited?".
* **Properties** — the script must declare the Oracle display, must carry the
  per-session ``-rfbauth`` and Xauthority markers, and must contain no
  passwordless flag. That answers "and even if I have never seen this file, is
  it the dangerous one?".

A hash pin alone is brittle: any legitimate edit breaks it, which tempts an
operator to re-pin without reading, and a blind re-pin of a *stale* file would
bless the exact bug. So an ``unsafe`` verdict is **never** overridable, while an
``unpinned`` one can be accepted only by naming the observed digest — you cannot
wave it through without having looked at it.

The requested display is checked too. The script defaulting to ``:97`` does not
help if the caller passes ``--display :99``, so known-shared displays are
refused by name with the reason recorded.

Standard library only, and no side effects: this module reads one file and
returns a verdict. It starts nothing and connects to nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ORACLE_ENROLL_GUARD_SCHEMA = "skillbox.oracle-enroll-guard.v1"

#: The Oracle host's own Xvfb display. The enrollment script must default here.
ORACLE_DISPLAY = ":97"

#: Displays that are known to be shared with other tooling and therefore unsafe
#: to attach a VNC server to. `:99` is the cypress-deps Xvfb, started with `-ac`
#: (access control OFF) — the display the 2026-08-06 incident actually hit.
UNSAFE_DISPLAYS: dict[str, str] = {
    ":99": "cypress-deps Xvfb runs here with -ac (access control off)",
}

DISPLAY_PATTERN = re.compile(r"^:[0-9]{1,3}$")

#: Flags that turn VNC authentication off. Presence is disqualifying.
FORBIDDEN_FLAGS = ("-nopw",)

#: Markers proving per-session VNC auth and an X cookie are actually wired up.
REQUIRED_MARKERS = ("-rfbauth", "-auth")

#: Reviewed revisions of oracle-enroll-forward.sh.
#:
#: 3abe5c46… is skills 3955fe3 "fix(invisible-oracle): integrate
#: skillbox-invisible-oracle-subagent-hjuc" — DEFAULT_DISPLAY=":97", no -nopw,
#: per-session -rfbauth plus a 0600 MIT-MAGIC-COOKIE Xauthority.
PINNED_DIGESTS: dict[str, str] = {
    "3abe5c4660e2ad3c498a919587243baab414dd861b97ef38780e1959d04ed746": (
        "skills 3955fe3 (B4 hardening: :97, per-session rfbauth, Xauthority)"
    ),
}

MAX_SCRIPT_BYTES = 1024 * 1024

STATE_TRUSTED = "trusted"
STATE_UNPINNED = "unpinned"
STATE_UNSAFE = "unsafe"
STATES = frozenset({STATE_TRUSTED, STATE_UNPINNED, STATE_UNSAFE})

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2

DEFAULT_SCRIPT_CANDIDATES = (
    "~/repos/opensource/skills/deep-research-prompt/assets/scripts/oracle-enroll-forward.sh",
    "~/.claude/skills/deep-research-prompt/assets/scripts/oracle-enroll-forward.sh",
)


class EnrollGuardError(RuntimeError):
    """Stable, non-sensitive guard refusal."""

    def __init__(self, code: str) -> None:
        super().__init__("oracle enroll guard: refused")
        self.code = code


def _refuse(code: str) -> Any:
    raise EnrollGuardError(code)


def _strip_comments(source: str) -> str:
    """Drop full-line comments before scanning for flags.

    A script that documents "we no longer pass -nopw" must not be refused for
    saying so. Only whole-line comments are removed; a trailing comment after
    real code is left alone, because that is where a disabled flag could hide.
    """

    return "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )


@dataclass(frozen=True)
class EnrollScriptVerdict:
    """What the guard concluded, and why."""

    state: str
    path: str
    digest: str
    declared_display: str
    reasons: tuple[str, ...] = ()
    pinned_as: str = ""
    notes: tuple[str, ...] = field(default=())

    @property
    def safe(self) -> bool:
        """True when nothing dangerous was found. Not the same as trusted."""

        return self.state != STATE_UNSAFE

    @property
    def trusted(self) -> bool:
        return self.state == STATE_TRUSTED

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": ORACLE_ENROLL_GUARD_SCHEMA,
            "state": self.state,
            "safe": self.safe,
            "trusted": self.trusted,
            "path": self.path,
            "digest": self.digest,
            "declared_display": self.declared_display,
            "reasons": list(self.reasons),
            "pinned_as": self.pinned_as,
            "notes": list(self.notes),
        }


def read_script(path: Any) -> tuple[str, str]:
    """Return (source, sha256) for a readable, regular, bounded script."""

    if isinstance(path, Path):
        target = path
    elif isinstance(path, str) and path:
        target = Path(os.path.expanduser(path))
    else:
        _refuse("script_path_invalid")
    try:
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        _refuse("script_missing")
    except OSError:
        _refuse("script_unreadable")
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _refuse("script_unreadable")
        if metadata.st_size > MAX_SCRIPT_BYTES:
            _refuse("script_unreadable")
        raw = os.read(descriptor, MAX_SCRIPT_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_SCRIPT_BYTES:
        _refuse("script_unreadable")
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError:
        _refuse("script_unreadable")
    return source, hashlib.sha256(raw).hexdigest()


def declared_display(source: str) -> str:
    """The script's own DEFAULT_DISPLAY, or "" when it declares none."""

    match = re.search(
        r'^\s*(?:readonly\s+)?DEFAULT_DISPLAY=["\']?(:[0-9]{1,3})["\']?\s*$',
        source,
        re.MULTILINE,
    )
    return match.group(1) if match else ""


def verify_enroll_script(
    path: Any,
    *,
    expected_display: str = ORACLE_DISPLAY,
    pinned_digests: dict[str, str] | None = None,
) -> EnrollScriptVerdict:
    """Classify an enrollment script. ``unsafe`` is never overridable."""

    if type(expected_display) is not str or DISPLAY_PATTERN.fullmatch(expected_display) is None:
        _refuse("display_invalid")
    pins = PINNED_DIGESTS if pinned_digests is None else pinned_digests
    source, digest = read_script(path)
    scannable = _strip_comments(source)
    display = declared_display(source)

    reasons: list[str] = []
    if display != expected_display:
        # The precise incident: a stale default silently attaches the VNC
        # server to somebody else's Xvfb.
        reasons.append(
            f"declares display {display or '<none>'}, expected {expected_display}"
        )
    for flag in FORBIDDEN_FLAGS:
        if flag in scannable:
            reasons.append(f"contains passwordless VNC flag {flag}")
    for marker in REQUIRED_MARKERS:
        if marker not in scannable:
            reasons.append(f"missing per-session auth marker {marker}")

    rendered = str(Path(os.path.expanduser(path)) if not isinstance(path, Path) else path)
    if reasons:
        return EnrollScriptVerdict(
            state=STATE_UNSAFE,
            path=rendered,
            digest=digest,
            declared_display=display,
            reasons=tuple(reasons),
        )
    if digest in pins:
        return EnrollScriptVerdict(
            state=STATE_TRUSTED,
            path=rendered,
            digest=digest,
            declared_display=display,
            pinned_as=pins[digest],
        )
    return EnrollScriptVerdict(
        state=STATE_UNPINNED,
        path=rendered,
        digest=digest,
        declared_display=display,
        reasons=("digest is not a reviewed revision",),
        notes=(
            "safety properties hold; review the diff and re-pin, or re-run with "
            f"--accept-digest {digest}",
        ),
    )


def verify_requested_display(display: Any) -> str:
    """Refuse a display known to be shared with other tooling."""

    if type(display) is not str or DISPLAY_PATTERN.fullmatch(display) is None:
        _refuse("display_invalid")
    if display in UNSAFE_DISPLAYS:
        _refuse("display_unsafe")
    return display


def authorize_enrollment(
    path: Any,
    *,
    display: str = ORACLE_DISPLAY,
    accept_digest: str = "",
    pinned_digests: dict[str, str] | None = None,
) -> EnrollScriptVerdict:
    """Refuse unless it is safe to run this script against this display.

    Returns the verdict when enrollment may proceed and raises otherwise, so a
    caller that ignores the return value still cannot run on a bad script.
    """

    verify_requested_display(display)
    verdict = verify_enroll_script(
        path, expected_display=ORACLE_DISPLAY, pinned_digests=pinned_digests
    )
    if verdict.state == STATE_UNSAFE:
        # Deliberately not overridable. A stale script is the whole hazard.
        _refuse("script_unsafe")
    if verdict.state == STATE_UNPINNED:
        if not accept_digest:
            _refuse("script_unpinned")
        if type(accept_digest) is not str or accept_digest != verdict.digest:
            # Naming the digest is the point: an operator must have looked at
            # the file, not merely passed --force.
            _refuse("accept_digest_mismatch")
    return verdict


def resolve_default_script(candidates: Iterable[str] = DEFAULT_SCRIPT_CANDIDATES) -> str:
    for candidate in candidates:
        expanded = Path(os.path.expanduser(candidate))
        if expanded.is_file():
            return str(expanded)
    _refuse("script_missing")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="oracle-enroll-guard",
        description=(
            "Fail-closed preflight for oracle-enroll-forward.sh: refuses a stale "
            "script that would attach passwordless VNC to the wrong display."
        ),
    )
    parser.add_argument("--script", default=None, help="Path to oracle-enroll-forward.sh")
    parser.add_argument("--display", default=ORACLE_DISPLAY, help="Display to enroll on")
    parser.add_argument(
        "--accept-digest",
        default="",
        help="Accept an unpinned script by naming its exact sha256",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    try:
        script = args.script or resolve_default_script()
        verdict = authorize_enrollment(
            script, display=args.display, accept_digest=args.accept_digest
        )
    except EnrollGuardError as error:
        payload = {
            "schema": ORACLE_ENROLL_GUARD_SCHEMA,
            "ok": False,
            "error": {"code": error.code, "message": str(error)},
        }
        if args.script:
            payload["path"] = args.script
        if args.format == "json":
            json.dump(payload, sys.stdout, sort_keys=True)
            sys.stdout.write("\n")
        else:
            print(f"oracle-enroll-guard: refused ({error.code})", file=sys.stderr)
        return EXIT_REFUSED

    payload = {"ok": True, **verdict.to_payload()}
    if args.format == "json":
        json.dump(payload, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(f"{verdict.state}  {verdict.declared_display}  {verdict.digest[:12]}")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
