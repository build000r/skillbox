# Duel Context: `sbp test` — first-class distributed test workflow for Skillbox

This file is the shared brief for a dueling-idea-wizards round. It is untracked
planning material — do not commit it. Read it fully before ideating.

## The product thesis under debate

`sbp test` should mean:

> Run this repository's existing test system on the best available compute,
> without making the repository understand boxes, providers, SSH, setup,
> cleanup, or artifact transport.

Design principle: **normalize the infrastructure, not the tests.** Projects keep
`make`/`pytest`/Vitest/Cargo/Xcode/etc. A small version-controlled manifest
(`.skillbox/test.yaml`) declares test *units* (command + requirements + services
+ timeout + artifacts) and *groups* (default, full). `sbp test` compiles that to
an execution DAG, snapshots the (possibly dirty) working tree, resolves worker
capabilities, enforces cost/policy ceilings, fans units out across the compute
fabric (local machine, owned Mac/Linux boxes, DO droplets), streams logs,
harvests exit codes + artifacts, and writes one immutable run receipt proving
exactly what tree was tested where.

Extension model: `sbp test` stays a thin stable entrance that execs `sbp-test`
(generic `sbp <command>` → `sbp-<command>` lookup), so the compiler evolves and
tests independently.

Deliberately OUT of v1: automatic test discovery, cross-machine pytest
test-case sharding, duration prediction, job coalescing, a custom test
framework, a GitHub-Actions-complexity config language. Later, `sbp test init`
may *propose* a manifest from Makefile/package.json/pyproject/etc.

## Current machinery (traced through box.py + bootstrap scripts)

Spin-up pipeline for `box up <id> --profile dev-small` (box.py:3686):
create droplet (~40–60s) → block volume + mount /srv/skillbox (~20–30s) →
bootstrap 01-bootstrap-do.sh: apt, Node, Docker engine, swap, app user
(~2–5 min) → Tailscale enroll (~20–30s) → lockdown to tailnet-only (seconds) →
deploy (1–10 min) → onboard/acceptance (~1 min).

Deploy fork (box.py:294, :3193):
- Default: git clone + `make build && make up` — full Docker image build on a
  2-vCPU droplet. Total wall clock **10–15 min**.
- Pinned-release (`--deploy-manifest`, built by 07-build-and-push-binary.sh):
  scp prebuilt archive, sha256-verified install. Total **~4–6 min**, dominated
  by apt+Docker in bootstrap.
- Half-planned third lever: the operator MCP provision default names
  `image: skillbox-worker` — a golden DO snapshot image with Docker+deps
  pre-baked would collapse bootstrap+deploy to **~1–2 min to ready**.

Deps are declared in three places: profile YAML (size/region/image/repo/branch),
bootstrap script (OS baseline), onboard blueprint + set_vars (project-level).
There is deliberately no per-job "also apt-install X" hook.

Spin-down: drain → tailnet remove → droplet delete → read-after-delete
confirmation (destroy-pending state if DO still lists it — teardown never lies
about billing) → volume delete (~30–60s). NOTE: the block volume, despite being
the durable mount, IS deleted on `box down` (_cleanup_box_volume, box.py:4216).

**The gap:** zero snapshot machinery. No droplet-action snapshot, no
volume-snapshot calls anywhere. State model is "cattle all the way down":
anything that matters must leave via git push / beads issues.jsonl / worker
receipts before teardown. Current real options: keep it running (DO bills
powered-off droplets at full price), or destroy + rebuild (pay 4–15 min).
Missing: snapshot-then-destroy / restore-from-snapshot — "pause without
paying" (~$0.06/GB/mo, ~1–2 min resume). The fabric contract already models
provider lifecycle as explicit capabilities (provision/snapshot/restore/
destroy), and a decision record requires restored machines to get fresh
machine_id (no silent ancestor impersonation).

## The sequencing proposal on the table (to attack/defend/improve)

0. Push the already-landed edge-fabric slice (placement, `box place`, worker
   path, tests, docs are done locally).
1. **Live remote dispatch + harvest** (`skillbox-fabric-remote-dispatch-os9j`,
   an existing P1 bead). Today placement decides but only executes locally.
   This is the hard gate — without it `sbp test` is a local runner with extra
   steps. result_unavailable semantics already built.
2. **Fast clean workers**: bake the `skillbox-worker` golden image + default to
   the release-archive deploy path. Claimed to be the real prerequisite (not
   snapshot/restore) — makes "3 ephemeral boxes for one test run" sane
   (~1–2 min vs 10–15 min). 2b (opportunistic sibling): snapshot/restore verbs
   + keep-volume-on-down for the stateful-dev-box "pause without paying" case —
   shares the doctl seam and state-machine entries but does NOT gate sbp test
   (test workers want to be born clean from image and die; restore works
   against clean-environment correctness).
3. **Dirty-working-tree snapshot + digest transport** — genuinely new machinery
   (deploy only knows git clone today). Without it remote tests silently test
   the wrong tree and the receipt can't prove what was tested.
4. **Services + artifact retrieval on workers** — per-unit postgres/redis is
   compose-on-the-box; artifacts ride the harvest channel from step 1.
5. **Observation prober + static cost table** — a static per-size price table
   satisfies max_cost_usd deterministically; no billing API in v1.
6. **`sbp test` itself** — thin at that point: `.skillbox/test.yaml` →
   per-unit execution requests → existing placement authority + worker broker.
   The ExecutionPlan core half-exists as placement-needs + worker-run objects;
   sbp test is the first compiler into it, not a new scheduler.

Standing constraint from a previous duel: keep sbp test's coordinator as the
single placement authority per run; machine-lease/fencing only becomes
mandatory when two concurrent coordinators can exist.

## The operator's two-prong framing (important)

The operator sees this as two distinct problems:

1. **SBP handles where things run and why** — the infra/placement/transport/
   receipt half above.
2. **The repo's test suite must be properly configured for parallelization** —
   every repo's suite is shaped differently. The happy path exemplar is
   `../../sweet-potato`, which already has an ideal root test surface (make,
   pytest, Vitest, browser E2E, Passkeys, Stripe verification, docs/contract
   checks, release checks, exact-tree receipt logic). The wish: running the
   suite ships with (a) a light deterministic test/lint that scores whether a
   repo's suite is well-factored for unit-level parallel execution, and
   (b) a skill that teaches an agent how to properly adjust a repo's test
   suite to become well-factored (split monolithic targets, isolate service
   deps, make units independent and idempotent).

## What the operator wants out of this duel

An **epic** (beads graph) that gets from today's state to a working `sbp test`,
sequenced no-ragrets style — plus your best ideas that improve, correct, or
attack the plan above. Ideate on: sequencing correctness, missing pieces, the
manifest/compiler design, the receipt/trust story, the two-prong repo-readiness
lane (deterministic scorer + skill), failure modes, and what should be
deliberately cut from v1.
