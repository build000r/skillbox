# Oracle enrollment preflight guard

On 2026-08-06 a Mac checkout of `oracle-enroll-forward.sh` that predated the B4
hardening still had `DEFAULT_DISPLAY=:99` and `-nopw`. Running it would have
attached a **passwordless** VNC server to `:99` — an unrelated cypress-deps Xvfb
started with `-ac`, access control off — instead of the Oracle display `:97`.

Nothing errored. It would simply have done the wrong, open thing.

`scripts/lib/oracle_enroll_guard.py` is the preflight that makes that
impossible to do quietly. Run it before invoking the enrollment script and
refuse to invoke on a non-zero exit.

```bash
python3 scripts/lib/oracle_enroll_guard.py --format json          # default checkout
python3 scripts/lib/oracle_enroll_guard.py --script <path> --display :97
```

## Current status of the original report

The sync half of that report is **already resolved**. Both the skills checkout
and the mirrored copy under `workspace/skill-repos/` are at skills `3955fe3`
("integrate skillbox-invisible-oracle-subagent-hjuc"), which is the hardened
revision the report asked for: `DEFAULT_DISPLAY=":97"`, no `-nopw`, per-session
`-rfbauth` plus a 0600 MIT-MAGIC-COOKIE Xauthority. The guard confirms it:

```
$ python3 scripts/lib/oracle_enroll_guard.py --format json
{"state": "trusted", "declared_display": ":97",
 "pinned_as": "skills 3955fe3 (B4 hardening: :97, per-session rfbauth, Xauthority)", ...}
```

What was missing, and is what this guard adds, is the part that keeps it that
way: nothing previously stopped a *future* stale checkout from being run.

## Two layers, because either alone is weak

| Layer | Question it answers |
| --- | --- |
| **Identity** — SHA-256 against `PINNED_DIGESTS` | "Is this the file we reviewed?" |
| **Properties** — display, `-rfbauth`/`-auth`, no `-nopw` | "Even if I have never seen this file, is it the dangerous one?" |

A hash pin alone is brittle: any legitimate edit breaks it, which tempts an
operator to re-pin without reading — and a blind re-pin of a *stale* file would
bless the exact bug the pin exists to catch. So the properties are checked
independently of the pin.

## Verdicts

| State | Exit | Meaning |
| --- | --- | --- |
| `trusted` | 0 | Reviewed digest **and** safe properties. |
| `unpinned` | 1 | Safe properties, unrecognised digest. Refuses until the digest is named. |
| `unsafe` | 1 | A stale-script signature. **Never overridable.** |

`unsafe` has no escape hatch on purpose. Naming a digest accepts an
*unreviewed* script; it must never accept a *dangerous* one, and a test asserts
that even a forged pin table cannot force one through.

The requested display is checked separately, because the script defaulting to
`:97` does not help if the caller passes `--display :99`. Known-shared displays
are refused by name with the reason recorded.

## Re-pinning after a legitimate change

When the enrollment script legitimately changes, the guard reports `unpinned`
and prints the digest to paste:

```
$ python3 scripts/lib/oracle_enroll_guard.py --format json
{"state": "unpinned", "notes": ["safety properties hold; review the diff and
 re-pin, or re-run with --accept-digest <sha256>"], ...}
```

1. **Read the diff.** The properties passing means it is not the known-dangerous
   shape; it does not mean the change is good.
2. For a one-off run, pass `--accept-digest <the exact sha256>`. Naming it is
   the point — an operator must have looked at the file, not merely passed
   `--force`. A mismatched digest is refused.
3. To make it permanent, add the digest to `PINNED_DIGESTS` in
   `scripts/lib/oracle_enroll_guard.py` with the upstream commit and what makes
   that revision safe.

`tests/test_oracle_enroll_guard.py` asserts the pin matches the checkout on this
box, so a drifted checkout surfaces as a failing test rather than as a guard
that has quietly gone `unpinned` in the field.
