# Pulse regression proof + timing packet

- generated_at_utc: `2026-07-12T04:49:24+00:00`
- python: `3.12.3`
- behavioral_fingerprint_sha256: `fa79c867c1baca0967ff7f33d6ce66fe056ad6466ec8f17acb06ce327f3216da`

## Behavioral proof (no-infra)

- **Service transitions** — service crash then auto-heal: final={'web': 'running'}, events=1, heals=1
- **Check transitions** — check fail then recover: final={'disk': True}, events=2
- **Pressure advisory** — pressure advisory raise then clear (read-only): final_warnings=[], events=2, mutates=False
- **State file shape** — keys: ['active_clients', 'active_profiles', 'auto_restart', 'auto_sync', 'check_states', 'config_hash', 'cycle_count', 'events_emitted', 'heals', 'interval', 'pid', 'port_sentinel', 'pressure_advisory', 'pressure_warnings', 'restart_attempts', 'service_states', 'unhealthy_for_seconds', 'unhealthy_grace_seconds', 'updated_at']

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
  port sentinel: observe, seen 0, reaped 0, active 0
  port guard counters: hook 0, shim 0, post-bind 0, wildcard 0, first never, last never
  pressure/offload warnings:
    ! disk free below 10 GiB
```

## Cycle/timing baseline

- cycles: 5
- avg: 6.1877 ms/cycle, p50: 6.0172 ms, p95: 7.0648 ms, max: 7.0648 ms

## Blocked conditions

- none — proof runs without infrastructure or live services
