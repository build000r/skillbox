# Pulse regression proof + timing packet

- generated_at_utc: `2026-05-29T17:21:49+00:00`
- python: `3.12.13`
- behavioral_fingerprint_sha256: `4b8db16b0f02adb21e5ce162f935f30107d35751d63af3e794dc5e34af5d2ed3`

## Behavioral proof (no-infra)

- **Service transitions** — service crash then auto-heal: final={'web': 'running'}, events=1, heals=1
- **Check transitions** — check fail then recover: final={'disk': True}, events=2
- **Pressure advisory** — pressure advisory raise then clear (read-only): final_warnings=[], events=2, mutates=False
- **State file shape** — keys: ['active_clients', 'active_profiles', 'auto_restart', 'auto_sync', 'check_states', 'config_hash', 'cycle_count', 'events_emitted', 'heals', 'interval', 'pid', 'pressure_advisory', 'pressure_warnings', 'restart_attempts', 'service_states', 'unhealthy_for_seconds', 'unhealthy_grace_seconds', 'updated_at']

### `pulse.py status` rendering

```
pulse: stopped (pid 4242)
  interval:  30s
  cycles:    3
  heals:     1
  events:    4
  last tick: <age>
  services:
    - api: down
    + web: running
  failed checks: tls
  pressure/offload warnings:
    ! disk free below 10 GiB
```

## Cycle/timing baseline

- cycles: 200
- avg: 0.3347 ms/cycle, p50: 0.3288 ms, p95: 0.3731 ms, max: 0.6281 ms

## Blocked conditions

- none — proof runs without infrastructure or live services
