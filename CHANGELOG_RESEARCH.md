# Changelog Research

## Scope

- Requested window: full repository history.
- Repo: `build000r/skillbox`.
- Local head researched: `9157d0e` on 2026-04-29.
- Commit count at local head: 123 non-merge-inclusive commits.

## Evidence Sources

- `git log --reverse --date=short --pretty=format:'%h%x09%ad%x09%s' --no-merges`
- `git for-each-ref refs/tags --sort=creatordate`
- `gh release list --limit 100`
- `gh issue list --state all --limit 100`
- `README.md`
- `docs/VISION.md`
- `docs/ROADMAP.md`
- `AGENTS.md`
- changelog-md-workmanship `cluster-history.py --repo . --format markdown`
- changelog-md-workmanship `build-version-spine.py --repo . --format markdown`
- changelog-md-workmanship `extract-tracker-workstreams.py --repo . --format markdown`

## Version Spine

- Tags found: none.
- GitHub Releases found: none.
- GitHub Issues returned by `gh issue list`: none.
- Changelog shape selected: dated development timeline plus thematic capability
  waves.

## Coverage Ledger

| Chunk | Range | Status | Major Themes |
|-------|-------|--------|--------------|
| 01 | 2026-03-30 to 2026-03-31 | distilled | Starter repo, runtime manager, overlays, box lifecycle, focus, pulse, MCP, operator tools. |
| 02 | 2026-04-01 to 2026-04-02 | distilled | Client projection, runtime-manager package split, skill artifacts, runtime log, git-repo-native skill model. |
| 03 | 2026-04-03 to 2026-04-06 | distilled | Shared-jam, local runtime bridge, lifecycle modes, local-core cutover, parity ledger. |
| 04 | 2026-04-08 to 2026-04-09 | distilled | Upgrade-release, storage posture, MCP validation, ingress routing, bootstrap hardening, default skill promotion. |
| 05 | 2026-04-10 to 2026-04-17 | distilled | Verify hardening, resilient service starts, shared-box registration, correlated events. |
| 06 | 2026-04-22 to 2026-04-29 | distilled | Signed skill sync, pulse/runtime hardening, VPS portability, first-box hardening, activation packets, CLI refactor, service-startup errors. |

## Open Questions

- No formal release tags exist yet. The first tagged release should decide
  whether this changelog keeps the development timeline as pre-release history
  or folds it under an initial version.
- Several latest commits are ahead of `origin/main`; their GitHub commit URLs
  become reachable only after push.
