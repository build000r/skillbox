# Private coding-agent workstation: raw VPS vs. DevPod vs. Coder

Last reviewed: 2026-08-13

A private coding-agent workstation is an operator-owned machine that keeps agent
homes, repositories, logs, and project context available across sessions. It is
different from an ephemeral sandbox, which optimizes temporary isolation, and a
remote-development control plane, which optimizes managed team workspaces.

This guide is for independent operators, consultants, and small agencies
deciding where Claude Code, Codex, or similar terminal agents should run. It is
a category-level decision guide, not a performance benchmark.

## The short answer

- Choose a **raw VPS** when customization and minimum machinery matter more than
  drift control or a repeatable handoff.
- Choose **environment or thin-remote tooling** when reproducible development
  environments and IDE access are the main job.
- Choose a **remote-development platform** when team tenancy, RBAC, policy, and
  managed workspace lifecycle are requirements.
- Choose an **agent sandbox/runtime** when isolation and ephemeral execution are
  the product requirement.
- Evaluate **Skillbox** when one operator wants one durable, private,
  agent-oriented workstation with explicit runtime declarations and client
  overlays.

## What job are you hiring the machine to do?

The useful decision is not “which tool has more features?” Start with the job:

1. Should agent homes and logs survive a rebuild?
2. Do several clients or repositories need different services, skills, or
   context on the same operator-owned machine?
3. Is one operator responsible for the box, or does a team need tenancy and
   policy controls?
4. Is long-lived state desirable, or is destroying the environment after each
   task the safety model?
5. Must a browser IDE be the primary surface?

Skillbox is designed for “yes” to the first two and “one operator” for the
third. Different answers point to a different category.

## Category comparison

| Category | Representative examples | Optimizes for | Trade-off for one durable agent workstation |
|---|---|---|---|
| Raw host setup | VPS + shell scripts | Direct control and minimal abstraction | The operator owns drift, state layout, checks, and handoff conventions |
| Environment / thin remote | Devbox, DevPod | Reproducible environments and remote-editor workflows | Does not necessarily define agent homes, client overlays, logs, and box operations as one system |
| Remote-dev platform | Coder, Gitpod | Team workspaces, IDE integration, tenancy, and policy | More control-plane scope than one operator may need |
| Agent runtime / sandbox | Daytona, E2B | Isolated or ephemeral agent execution | Durable personal state is not the primary optimization |
| Private agent workstation | Skillbox | One operator's durable homes, repos, overlays, runtime graph, and checks | You operate the host; there is no hosted service or team control plane |

The examples describe product categories using the project's [vision and market
map](VISION.md). They do not claim that Skillbox is faster, cheaper, or more
secure than those products.

## What Skillbox keeps durable

Skillbox stores its working state under `.skillbox-state/` and mounts the
relevant parts back into the workspace. The intended durable surface includes:

- Claude and Codex homes
- repository roots
- client overlays and focused context
- runtime logs and declared checks
- skill selections and locks

Durability does not remove the need for backups, host maintenance, or recovery
testing. It means persistence is part of the declared workstation model rather
than an accidental property of one server.

## How client overlays reduce context switching

A consultant can keep one core machine while declaring which repositories,
services, checks, and skills belong to each client. Focusing a client projects
that context into the agent workspace. The goal is not to hide configuration;
it is to make the current configuration inspectable and repeatable.

That matters when the expensive failure is not a slow command but an agent
working against the wrong repository, stale context, or an undeclared service.

## What “private” does and does not mean

Skillbox's managed-box posture is Tailnet-first. Public SSH is intended as a
temporary enrollment aperture, after which host access and cloud firewall rules
are restricted to Tailnet access. Services should use loopback or Tailnet binds,
and posture commands report the checks they can perform.

This is an architecture and operating model, not an independent certification.
It does not prove that every deployment is secure. Review the
[Tailnet-only lifecycle](tailnet-only-lifecycle.md), secret boundaries, bind
configuration, and recovery path for your own box.

## Rights boundary

This repository is published for reading and inspection. It currently grants
no public right to install, execute, modify, or redistribute Skillbox. The
captured proof below documents owner-run behavior; it is not an invitation to
reproduce it.

## Proof available today

The [captured first-box walkthrough](../examples/first-box-demo.md) records a
clean-clone run from 2026-07-05 at commit `e6f21b0`. It resolved four repos,
eleven services, seven logs, and eighteen checks before startup; focused a demo
client with two running services; kept the demo app on loopback; and finished
cleanup with 15 checks passed, one warning, and zero failures.

This is captured operator evidence. It is not a benchmark, an independent
customer result, or proof that current `main` behaves identically. No
permissioned customer field report exists yet, so there is no adoption or
retention evidence.

## Cost and offer

Skillbox has no paid plan, free trial, hosted service, or support SLA. The
operator supplies and pays for the compatible host, domain-specific services,
and any third-party tools. The repository is source-available rather than
OSI-licensed, and it defines no public installation or execution grant. The
reference design assumes an operator-managed compatible Docker host and uses a
Tailnet-first access model.

## Decision checklist

Skillbox is worth a closer look when all of these are true:

- one operator owns the machine and its risk;
- Claude Code, Codex, or similar terminal agents are primary users;
- durable homes, repos, logs, and client context matter;
- SSH/Tailscale is preferable to a browser-first product;
- operating a host is acceptable;
- inspectable manifests and checks are preferable to hidden automation.

Choose another category when team tenancy, hosted support, untrusted-code
sandboxing, or a browser IDE is the main requirement.

## Next step

Read the [first-box proof](../examples/first-box-demo.md) and inspect the
[runtime architecture](ARCHITECTURE.md). If the model fits, identify the one
missing check that would prevent you from trusting the workstation model.
