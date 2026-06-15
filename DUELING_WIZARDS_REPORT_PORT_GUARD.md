# Dueling Idea Wizards Report: skillbox port-guard enforcement

**Question:** How should skillbox BLOCK a project from ever coming up on the wrong port in development mode — given agents bypass `sbp up` with direct `npm run dev`, Vite silently auto-increments ports, and the live box showed four duplicate dev servers (127.0.0.1:5173, 100.79.193.34:5174, 100.79.193.34:5176, 0.0.0.0:5177 — the last violating tailnet_only posture)?

**Date:** 2026-06-11 · **Session:** `opensource-skillbox--port-guard-duel`

## Executive Summary

Three models (Claude Opus pane, Codex gpt-5.5 pane, Grok sidecar) each generated 15 enforcement designs, winnowed to 5, then adversarially cross-scored all rivals 0-1000 with codebase verification. 15 ranked designs → 30 cross-scores → 3 reaction/concession rounds. Two designs were **killed with technical proof** (nftables-as-bind-enforcement; LD_PRELOAD), one was **demoted by its own author** (kernel-first eBPF), and all three models **converged on the same five-layer stack** in nearly the same build order. The duel's core finding: enforce at three moments — **attempt** (hook + shims), **bind** (post-start verification + strictPort), **drift** (pulse reaper) — all driven by one machine-readable port registry, with every block surface emitting the same `sbp up <profile> <service>` remediation.

## Methodology

- Agents: Claude Code (Opus, NTM pane 0), Codex (gpt-5.5 xhigh, NTM pane 1), Grok (headless sidecar)
- All three studied the repo first; all cross-scores were verified against actual code (functions, line numbers, Makefile targets, settings.json)
- Phases: study → ideate (15→5 each) → 3-way cross-score (6 score files) → reveal/reaction (3 files) → synthesis

## Consensus Winners (build these)

| # | Design | Cross-scores | Verdict |
|---|--------|-------------|---------|
| 1 | **Machine-readable port registry** (`port_registry.py`, `manage.py port-resolve/port-registry`, doctor collision + docs-parity checks) | foundation inside every 700+ idea | Unanimous prerequisite — "not a ranked idea, Phase 0" (Grok concession) |
| 2 | **Dev-command intercept** — PATH shims for npm/npx/vite/next/pnpm/yarn + Claude Code PreToolUse `guard-dev-port.sh` | Intercept stack: 770 (CC), 850 (COD) · Hook: 830 (CC), 745 (COD), 692-720 (GROK) | Highest-average design of the duel (810). **Block, don't silently rewrite** — rewriting normalizes bypass (3-way consensus) |
| 3 | **Post-bind/post-start verification** in `_start_service()` — on ALL result paths (healthy, timeout, reused-existing, failed); kill the process group on wrong-port bind; verify PID ownership before reporting "already running" | 845 (CC on COD's version), 835 (COD on CC's version), 680-718 (GROK) | The sharpest single code gap found: `_healthy_service_start_result()` (runtime_ops.py:3518) never proves the spawned PID owns the declared socket. Grok's catch: `_reused_existing_service_result()` can bless a rogue as "already running" |
| 4 | **Pulse rogue-listener sentinel** — scan listeners each cycle, kill non-whitelisted holders of canonical ports and any `0.0.0.0` dev bind under tailnet_only; process-group/ancestry whitelist, 10-15s HMR grace, report-only first cycle, kill reason surfaced in `sbp status --json` | 770 (COD), 736 (GROK), 745-795 for Grok's version | Only design covering ALL bypass paths. Would have killed all four live rogues. Claude conceded it under-ranked this (#4→#3, 750→780) |
| 5 | **strictPort + env-driven port contracts** via focus (`.skillbox-port.env`, overlay-synced config fragments — NOT in-repo package.json edits) | 585-628 standalone; consensus as complement | Turns Vite's silent 5173→5174 hop into a hard crash; doctor should FAIL (not warn) for port-hopping declared web services |

## Contested → Resolved by Concession

- **Kernel-first cgroup/eBPF bind guard (Codex's #1):** scored 420 (CC) / 410 (GROK). Both rivals: right mechanism, wrong first move for a single-tenant thin box. **Codex's final position: "Kernel-first: no. Portable-first: yes."** Real `BPF_CGROUP_INET4_BIND` (never nftables) stays on the shelf as Phase 5 behind `SKILLBOX_PORT_CAGE=1`, only if telemetry shows userspace enforcement leaking.
- **Hook vs shims rank:** Claude values hook UX (pre-execution block, 1-turn recovery, proven `guard-destructive-op.sh` template with tests); Codex/Grok value shim breadth (Codex doesn't consume `.claude/settings.json`). Resolution: ship both in the same slice; hook is the Claude-surface layer, shims are the everything-else layer.

## Killed Ideas (do not build)

| Idea | Scores | Cause of death |
|------|--------|----------------|
| **nftables "port cage" as bind enforcement** (proposed independently by BOTH Claude and Grok) | 150-305 | Technically refuted: nftables filters packets, never consults `bind(2)`. A rogue still binds, holds the port, Vite still auto-increments — it just goes deaf. Both authors formally retracted; Claude: "the intellectual dishonesty of ranking it #5 while knowing the mechanism was questionable is the worst part." Salvage: optional tailnet *reachability* hardening only |
| **LD_PRELOAD bind guard** | 430/445 | Native .so in a stdlib-Python repo; trivially bypassed (`env -u LD_PRELOAD`, static binaries); marginal value over shims+pulse |
| **Proxy-owned canonical ports** | 385/385 | Contradicts the documented 2026-06-04 pivot to port-per-app direct Tailnet ingress (tailnet-ingress.md:169-189) without addressing why that pivot happened (HMR, websockets, auth callbacks) |
| **Doc/CI-only enforcement** | cut in winnowing | Already the status quo that failed |

## Full Score Matrix

| Idea (origin) | Self-rank | CC score | COD score | GROK score | Avg | Verdict |
|---|---|---|---|---|---|---|
| Registry + post-bind verify (CC) | 1 | — | 835 | 718 | 777 | WIN |
| PreToolUse hook (CC) | 2 | — | 745 | 692 | 719 | WIN |
| PATH shims + strictPort (CC) | 3 | — | 815 | 628 | 722 | WIN |
| Pulse sentinel (CC) | 4 | — | 770 | 736 | 753 | WIN (promoted by author to #3) |
| nftables cage (CC) | 5 | — | 180 | 298 | 239 | KILLED (retracted) |
| eBPF bind guard (COD) | 1 | 420 | — | 410 | 415 | DEFERRED to hardened mode (author conceded) |
| Portable stack (COD) | 2 | 695 | — | 745 | 720 | WIN (umbrella) |
| Post-start verification (COD) | 3 | 845 | — | 680 | 763 | WIN (author re-scored 780-820 as foundation) |
| LD_PRELOAD (COD) | 4 | 430 | — | 445 | 438 | KILLED |
| Proxy-owned ports (COD) | 5 | 385 | — | 385 | 385 | KILLED |
| Intercept stack (GROK) | 1 | 770 | 850 | — | 810 | WIN (highest avg) |
| Pulse sentinel (GROK) | 2 | 745 | 795 | — | 770 | WIN |
| PreToolUse hook (GROK) | 3 | 830 | 720 | — | 775 | WIN |
| nftables cage (GROK) | 4 | 305 | 150 | — | 228 | KILLED (withdrawn by author) |
| Focus port contract (GROK) | 5 | 585 | 625 | — | 605 | COMPLEMENT only |

## Recommended Build Order (3-way consensus)

1. **Phase 0 — Port registry** (~1 PR): `runtime_manager/port_registry.py` derived from the merged runtime model (`_service_declared_listen_port()` + `_service_command_bind_url()`); `manage.py port-resolve --cwd` and `port-registry --format json`; doctor checks for duplicate ports, registry↔`docs/tailnet-ingress.md` parity (or generate the doc from the registry); collision failure at render time.
2. **Phase 1 — Intercept slice** (~1 PR series): `scripts/guard-dev-port.sh` PreToolUse hook (clone `guard-destructive-op.sh`, tests like `test_operational_scripts.py:197-293`); PATH shims via extended `make wrappers-install` + focus/NTM PATH injection; behavior = block with `BLOCKED: unclawg-web must listen on 100.79.193.34:5174 → sbp up local-all unclawg-web`; fail-open w/ logged warning only while registry is unpopulated.
3. **Phase 2 — `sbp up` start contract**: post-start listener verification on all four result paths; kill process group on wrong bind; `wrong_port_bound` / `wildcard_bind_blocked` structured reasons; PID-ownership check in reused-existing; actionable text upgraded to `sbp up <profile> <service>`.
4. **Phase 3 — Pulse sentinel**: `rogue_listener_scan()` with registry + ancestry whitelist; one report-only cycle then enforce; kill events to runtime.log, pulse.state.json, `sbp status --json`; dev-sanity check for rogues; immediately clears the current 4-rogue backlog.
5. **Phase 4 — Repo contracts**: `.skillbox-port.env` + overlay-synced strictPort fragments (no package.json mutation); doctor fails for declared web services that can port-hop.
6. **Phase 5 (optional, flag-gated)** — real cgroup/eBPF `BPF_CGROUP_INET4/6_BIND` with sbp-issued leases, behind `SKILLBOX_PORT_CAGE=1`, only if Phase 1-4 telemetry shows leaks. nftables only ever as reachability defense-in-depth.

**Success criteria (from Grok's reaction, endorsed by all):** `npm run dev` in tmux → blocked in one turn with the exact sbp command; full-path `node …/vite` bypass → strictPort crash or pulse kill within one interval; no silent port hops; no multi-day rogue accumulation; no surviving `0.0.0.0` dev listeners under tailnet_only.

## Meta-Analysis

- **Bias fingerprints:** Claude optimized runtime-manager correctness and agent UX (registry/hook first, kernel last); Codex optimized literal semantics ("never" → kernel) then conceded pragmatics; Grok optimized matching enforcement moments to observed agent behavior. Codex showed the strongest code-verification discipline (caught fixture-vs-feature on strictPort); Claude wrote the deepest mechanism refutation (nftables bind() walkthrough); Grok produced the cleanest final synthesis and concession ledger.
- **Adversarial pressure worked:** both nftables proposals died only because a *rival* checked the mechanism; Codex's kernel-first ranking survived its own writeup but not two independent "wrong box, wrong threat model" critiques; Claude's success-path-only verification gap was a critique it had made of Codex and failed to apply to itself until called out.
- **Independent convergence is the strongest signal:** all three arrived at registry + intercept + verify + reap without seeing each other's ideation, and all three reactions end at the same build order.

## Artifacts

All in repo root: `WIZARD_IDEAS_{CC,COD,GROK}.md`, `WIZARD_SCORES_{CC_ON_COD,CC_ON_GROK,COD_ON_CC,COD_ON_GROK,GROK_ON_CC,GROK_ON_COD}.md`, `WIZARD_REACTIONS_{CC,COD,GROK}.md`. (April files with `_R1/_R2` suffixes belong to an earlier, unrelated duel.)
