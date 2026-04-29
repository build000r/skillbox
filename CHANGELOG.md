# Changelog

This is a synthesized, agent-facing changelog for the full current history of
`skillbox`.

Scope window: project inception on 2026-03-30 through local `main` at
[`9157d0e`](https://github.com/build000r/skillbox/commit/9157d0e) on
2026-04-29.

Method: rebuilt from local git history, tag metadata, GitHub release metadata,
GitHub issue metadata, README.md, docs/VISION.md, and docs/ROADMAP.md. This
repository currently has no git tags and `gh release list` returned no GitHub
Releases, so this changelog uses dated development waves instead of versioned
release buckets.

## Version Timeline

`Kind` distinguishes a published release from a plain git tag.

| Version | Kind | Date | Summary |
|---------|------|------|---------|
| None found | n/a | n/a | No git tags or GitHub Releases were present when rebuilt on 2026-04-29. |

## Development Timeline

| Window | Head Ref | Summary |
|--------|----------|---------|
| 2026-03-30 to 2026-03-31 | [`2c709eb`](https://github.com/build000r/skillbox/commit/2c709eb) | Starter repo, runtime manager, client overlays, box lifecycle, MCP surfaces, focus, pulse, and initial documentation landed. |
| 2026-04-01 to 2026-04-02 | [`1759f06`](https://github.com/build000r/skillbox/commit/1759f06) | Client bundle publishing, reconciliation tests, runtime-manager package split, skill scaffolding, first-class agent artifacts, and git-repo-native skill distribution replaced the packaged-skill pipeline. |
| 2026-04-03 to 2026-04-06 | [`6c8cc84`](https://github.com/build000r/skillbox/commit/6c8cc84) | Shared-jam access, local runtime bridges, `--mode` lifecycle controls, parity-ledger enforcement, and local-core documentation stabilized the daily local runtime path. |
| 2026-04-08 to 2026-04-09 | [`6a23797`](https://github.com/build000r/skillbox/commit/6a23797) | Upgrade-release, storage posture validation, MCP validation, ingress routing, default skill promotion, droplet bootstrap hardening, and explicit ingress origins landed. |
| 2026-04-10 to 2026-04-17 | [`19b4f0f`](https://github.com/build000r/skillbox/commit/19b4f0f) | Local verify paths, resilient service starts, remote shared-box registration, shared skill-source validation, and correlated runtime events were added. |
| 2026-04-22 to 2026-04-29 | [`9157d0e`](https://github.com/build000r/skillbox/commit/9157d0e) | Signed skill sync, pulse and runtime hardening, VPS-portable skill loading, remote first-box hardening, activation packets, CLI refactor, and local service-startup error handling landed. |

## Capability Waves

## 1. Private Box Foundation

The first wave made `skillbox` a concrete Tailnet-first dev box instead of a
loose set of scripts. The repo gained a Docker workspace, runtime declarations,
client overlays, workload profile filtering, and a README that explained the
private single-tenant shape.

### Delivered capability

- Cloneable starter repo with Docker runtime files and operator-facing
  `Makefile` commands.
- Runtime manager support for default skill installs, workload profiles, and
  client overlays.
- Workspace-local service support for `swimmers`.
- Initial reconciliation docs tying the outer model to generated runtime state.

### Closed workstreams

- GitHub Issues: none returned by `gh issue list --state all --limit 100`.
- Checked-in tracker files: none found.

### Representative commits

- [`fbcc658`](https://github.com/build000r/skillbox/commit/fbcc658) initialized the starter repo.
- [`c5ffcb4`](https://github.com/build000r/skillbox/commit/c5ffcb4) added the internal box manager.
- [`5cfbef7`](https://github.com/build000r/skillbox/commit/5cfbef7) introduced client overlay support.
- [`2ef309a`](https://github.com/build000r/skillbox/commit/2ef309a) added the workspace-local overlay.

## 2. Operator And Agent Control Surfaces

The next wave turned the box into an agent-operable environment. It added
structured errors, context generation, onboarding, MCP tools, focus, pulse, and
operator lifecycle commands for DigitalOcean and Tailscale-managed boxes.

### Delivered capability

- Structured runtime errors and generated agent context.
- `focus` workflow, pulse daemon, runtime event journal, and later runtime log
  replacement.
- Skillbox and operator MCP servers for native agent lifecycle operations.
- Destructive-operation guard wiring and connector declarations.

### Closed workstreams

- Runtime manager command surface and live status generation.
- Operator box lifecycle management.
- Agent-facing MCP tooling and safety guardrails.

### Representative commits

- [`2cb7a47`](https://github.com/build000r/skillbox/commit/2cb7a47) added structured errors, context generation, and onboarding.
- [`4f5d897`](https://github.com/build000r/skillbox/commit/4f5d897) added the DO and Tailscale box lifecycle manager.
- [`3decf32`](https://github.com/build000r/skillbox/commit/3decf32) exposed native agent tools through the skillbox MCP server.
- [`7b0fa4b`](https://github.com/build000r/skillbox/commit/7b0fa4b) added focus, pulse, and event journaling.
- [`9bc5bf7`](https://github.com/build000r/skillbox/commit/9bc5bf7) added the operator MCP server and destructive-op guard hook.

## 3. Client Projection And Runtime Packaging

This wave made client-specific state explicit. The runtime learned to publish,
diff, accept, and validate client bundles while keeping generated state out of
the public source tree. It also split the runtime manager into a Python package
so command behavior could be tested and extended more safely.

### Delivered capability

- Per-client isolated volume mounts and client bundle publishing.
- Client diff, acceptance, reconciliation, and open scaffolding flows.
- Runtime model tests and conflict classification for projection output.
- Runtime-manager package split under `.env-manager/runtime_manager/`.
- Documentation of the product thesis in docs/VISION.md.

### Closed workstreams

- Client overlay reconciliation.
- Generated-state privacy and OSS hygiene.
- Runtime manager maintainability.

### Representative commits

- [`f8a604b`](https://github.com/build000r/skillbox/commit/f8a604b) added per-client isolated volume mounts.
- [`4d4f073`](https://github.com/build000r/skillbox/commit/4d4f073) added client bundle publishing.
- [`f1c26db`](https://github.com/build000r/skillbox/commit/f1c26db) added client diff and acceptance.
- [`d162059`](https://github.com/build000r/skillbox/commit/d162059) added client reconciliation and runtime model tests.
- [`fb94ed6`](https://github.com/build000r/skillbox/commit/fb94ed6) split the runtime manager into a package.

## 4. Skill Distribution And Agent Artifacts

Skill handling moved from packaged bundles toward git-repo-native skill sources.
At the same time, core agent tools became first-class artifacts in the runtime
model so the box could install, validate, and expose them consistently.

### Delivered capability

- Skill builder scaffolds and planning/operator skills.
- First-class `cass`, `cm`, `apr`, and related artifacts.
- Plain-text runtime log replacing the earlier journal and acknowledgement
  system.
- Skill-repo-set model replacing packaged-skill-set behavior.
- Signed skill sync, effective skill visibility, activation packets, and safer
  distribution transport.

### Closed workstreams

- Agent procedural-memory and spec-refinement tooling.
- Git-native skill source model.
- Skill sync integrity, lockfiles, and default-skill visibility.

### Representative commits

- [`b3450bd`](https://github.com/build000r/skillbox/commit/b3450bd) added skill builder scaffolds.
- [`3f66303`](https://github.com/build000r/skillbox/commit/3f66303) added the cass-memory skill.
- [`9d37baf`](https://github.com/build000r/skillbox/commit/9d37baf) made cass and cm first-class runtime artifacts.
- [`1759f06`](https://github.com/build000r/skillbox/commit/1759f06) replaced the packaged-skill pipeline with a git-repo-native model.
- [`e59c17a`](https://github.com/build000r/skillbox/commit/e59c17a) added signed skill sync.
- [`78daf1c`](https://github.com/build000r/skillbox/commit/78daf1c) added effective skill visibility.
- [`4e5bbc1`](https://github.com/build000r/skillbox/commit/4e5bbc1) returned activation packets.

## 5. Local Runtime Profiles And Parity Ledger

The local-core work made the day-to-day runtime path explicit. `focus`, `up`,
`down`, `status`, `logs`, and `doctor` grew profile-aware behavior, mode
selection, env bridge reconciliation, dependency ordering, and parity-ledger
checks so local service graphs could replace shell folklore.

### Delivered capability

- Local runtime bridges and generated env hydration.
- `--mode reuse|prod|fresh` lifecycle selection.
- Focus plus bridge/env reconciliation for local-core.
- Mode-aware `up` orchestration and service graph ordering.
- Parity-ledger enforcement across lifecycle surfaces.
- Regression coverage for local-core graphs, bridge behavior, and unsupported
  service requests.

### Closed workstreams

- Local-core cutover.
- Env bridge correctness.
- Legacy surface deferral and parity-ledger validation.

### Representative commits

- [`d4cde09`](https://github.com/build000r/skillbox/commit/d4cde09) added local runtime bridge support.
- [`e2c50bb`](https://github.com/build000r/skillbox/commit/e2c50bb) added mode commands, parity ledger, and XOR bootstrap enforcement.
- [`b8e97fe`](https://github.com/build000r/skillbox/commit/b8e97fe) added the `--mode` selector to lifecycle surfaces.
- [`4ea9948`](https://github.com/build000r/skillbox/commit/4ea9948) added mode-aware up orchestration and focus reconciliation wiring.
- [`a78228b`](https://github.com/build000r/skillbox/commit/a78228b) enforced the parity ledger in `up`, `status`, `logs`, and `doctor`.
- [`9157d0e`](https://github.com/build000r/skillbox/commit/9157d0e) hardened local service-startup error handling.

## 6. Upgrade, Ingress, And First-Box Hardening

The April 8 wave widened the runtime from local orchestration into deployable
box operations. Storage posture checks, upgrade-release scripts, MCP surface
validation, ingress routing, bootstrap swap support, and SSH readiness all
landed together.

### Delivered capability

- Storage posture validation and upgrade-release workflow.
- MCP surface validation and first-box workflow hardening.
- Ingress route declarations, proxy script, idle state, stop support, and
  explicit service origin URLs.
- Swap setup and resilient bootstrap behavior for small droplets.
- SSH-ready state and target resolution for remote boxes.
- Binary delivery script and fallback binary acquisition for swimmers.

### Closed workstreams

- First-box setup and upgrade reliability.
- Ingress routing and reverse-proxy safety.
- Remote box bootstrap hardening.

### Representative commits

- [`c41dd26`](https://github.com/build000r/skillbox/commit/c41dd26) added storage posture validation and upgrade-release workflow.
- [`b6cf405`](https://github.com/build000r/skillbox/commit/b6cf405) added MCP surface validation and first-box workflow hardening.
- [`720b09d`](https://github.com/build000r/skillbox/commit/720b09d) added the ingress routing subsystem.
- [`f07f749`](https://github.com/build000r/skillbox/commit/f07f749) added the ingress proxy script.
- [`b49f24d`](https://github.com/build000r/skillbox/commit/b49f24d) added swap support for small droplets.
- [`682339e`](https://github.com/build000r/skillbox/commit/682339e) added SSH-ready state and resilient target resolution.

## 7. Shared Access, Documentation, And OSS Hygiene

As the project became more useful outside one shell, it added trusted
collaborator access, safer public documentation, and ignore rules for generated
or local-only state. This wave is why AGENTS.md, README.md, docs/ROADMAP.md,
and docs/shared-jam.md matter for future work.

### Delivered capability

- Shared-jam collaborator access via Tailscale identity.
- Documentation for local runtime profiles, state-root persistence, upgrade
  workflow, distribution limits, and the dev-to-prod roadmap.
- Public-repo hygiene for local agent files, generated workspace state,
  coverage output, and analysis artifacts.
- Agent instructions in AGENTS.md.

### Closed workstreams

- Trusted collaborator access.
- Durable docs for runtime operation and strategic scope.
- Privacy-preserving public repo posture.

### Representative commits

- [`918b939`](https://github.com/build000r/skillbox/commit/918b939) added shared-jam collaborator access.
- [`c1c242b`](https://github.com/build000r/skillbox/commit/c1c242b) documented shared-jam usage.
- [`c9981b7`](https://github.com/build000r/skillbox/commit/c9981b7) added the dev-to-prod transition roadmap.
- [`ce23562`](https://github.com/build000r/skillbox/commit/ce23562) ignored local agent files for OSS hygiene.
- [`78738ae`](https://github.com/build000r/skillbox/commit/78738ae) removed private service names from docs.
- [`1ee9d1f`](https://github.com/build000r/skillbox/commit/1ee9d1f) added AGENTS.md and updated ignores.

## 8. Reliability, Validation, And Runtime Recovery

The later history is mostly hardening. It tightened subprocess handling,
runtime verification, pulse state, sync transport, service artifact checks,
self-managed PID behavior, and CLI structure. These commits are the first place
to look when debugging lifecycle regressions.

### Delivered capability

- Bounded task and manage.py subprocess runtime.
- Atomic pulse PID persistence and reconcile state.
- Hardened env-manager subprocess and bundle handling.
- Guard coverage for stale dry-runs and inaccessible repos.
- VPS-portable core skill pack and workspace-resolved shipped skills.
- Service artifact checks and self-managed PID preservation.
- Compact agent status defaults and CLI parser refactor.

### Closed workstreams

- Runtime lifecycle reliability.
- Distribution and bundle safety.
- Pulse/service recovery correctness.
- CLI maintainability.

### Representative commits

- [`7a572b1`](https://github.com/build000r/skillbox/commit/7a572b1) bounded task and manage.py subprocess runtime.
- [`cab3aee`](https://github.com/build000r/skillbox/commit/cab3aee) atomically persisted pulse PID and reconcile state.
- [`23c8a35`](https://github.com/build000r/skillbox/commit/23c8a35) hardened subprocess and bundle handling.
- [`9b481c7`](https://github.com/build000r/skillbox/commit/9b481c7) hardened sync transport and bundle unpacking.
- [`cdb029b`](https://github.com/build000r/skillbox/commit/cdb029b) made the core skill pack VPS portable.
- [`ca8f76c`](https://github.com/build000r/skillbox/commit/ca8f76c) resolved shipped skills from the workspace.
- [`a7852cd`](https://github.com/build000r/skillbox/commit/a7852cd) preserved self-managed service PIDs.
- [`09c78db`](https://github.com/build000r/skillbox/commit/09c78db) hoisted CLI subparser helpers to module level.

## Notes For Agents

- Start with the release/tag timeline if you need versioning facts. There are
  no releases or tags yet.
- Use the development timeline for chronology and the capability sections for
  architecture.
- The most important runtime areas are `.env-manager/runtime_manager/`,
  `scripts/04-reconcile.py`, `scripts/box.py`, and
  `scripts/operator_mcp_server.py`.
- Generated and local state is intentionally ignored: `.skillbox-state/`,
  `logs/`, `invocations/`, `workspace/clients/`, `workspace/skill-repos/`,
  `workspace/.focus.json`, `workspace/boxes.json`, `sand/`, and `builds/`.
- Commits ahead of `origin/main` may not resolve on GitHub until pushed.
