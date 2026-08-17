# `sbp test score --probe` fixtures

Fixtures for the bounded probe mode (`skillbox-sbp-test-probe-mode-sz4d`).

`consumer/` is a **consumer tree stand-in**: a minimal repo with a real
`.skillbox/test.yaml` so the probe path can compile declared units, plus a
source file and a test file whose bytes the tests hash before and after every
probe run. Nothing in this directory is ever executed. The suite copies it into
a temporary directory first, so the checked-in fixture cannot be mutated even if
a probe misbehaved.

`workspace_marker.json` is the shape of an admitted disposable capsule
workspace marker (`probe-workspace/v1`). It is kept here as documentation of the
on-disk contract; the tests write markers programmatically through
`sbp_test_probe.write_workspace_marker` so a drift in the writer cannot pass by
matching a stale fixture.

No fixture here contains a command that could reach a service or the network.
The probe tests drive a bounded fake runner and never launch a process.
