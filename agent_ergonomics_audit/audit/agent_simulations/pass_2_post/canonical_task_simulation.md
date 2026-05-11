# Pass 2 Fresh-Context Canonical Task Simulation

Date: 2026-05-11

Goal: prove a fresh agent can discover the daily Skillbox status, doctor, box dry-run/status, SBP skill/MCP recalibration, and client/distribution preview path from first-try machine contracts without external docs.

## Start From Contracts

Command:

```bash
python3 .env-manager/manage.py capabilities --json
```

Outcome: JSON contains outer entrypoints and safe previews, including:

- `python3 scripts/04-reconcile.py capabilities --json`
- `python3 scripts/box.py capabilities --json`
- `scripts/sbp capabilities --json`
- `scripts/sbo capabilities --json`
- `python3 .env-manager/manage.py client-project <client> --dry-run --format json`
- `python3 .env-manager/manage.py distribution-preview --manifest-path <manifest.json> --public-key <public-key.pem> --format json`

## Doctor

Command:

```bash
python3 scripts/04-reconcile.py doctor --format json --skip-compose --skip-skill-sync
```

Outcome: parseable JSON on stdout; command completed successfully as a safe fast-doctor path.

## Box Dry-Run And Status

Commands:

```bash
python3 scripts/box.py up sim-agent --profile dev-small --dry-run --format json
python3 scripts/box.py status --format json
```

Outcome: both commands produced parseable JSON; the dry-run path did not create infrastructure.

## SBP Runtime, Skills, And MCP

Commands:

```bash
bash scripts/sbp status --json
bash scripts/sbp skills --issues-only --json
bash scripts/sbp mcp --json
```

Outcome: all three commands produced parseable JSON. These cover runtime status plus the skill/MCP recalibration read-side checks a fresh agent needs before mutating repo-local state.

## Client And Distribution Preview Discovery

Command:

```bash
python3 .env-manager/manage.py capabilities --json
```

Outcome: `safe_previews` advertises parser-valid, non-mutating client and distribution preview command shapes. The simulation asserted both `client-project <client> --dry-run` and `distribution-preview --manifest-path <manifest.json> --public-key <public-key.pem>` preview entries were present.
