# Shared Jam — Collaborator Access Guide

Share your skillbox with trusted devs. They SSH in and start working immediately, with their identity attached to git commits, tmux sessions, and shell history. If they also want local operator MCP access from their own clone, register the shared box instead of hand-editing `workspace/boxes.json`.

---

## How It Works

- Everyone SSHs as the shared `sandbox` user
- Tailscale identity is resolved automatically on login via `tailscale whois`
- Git author, tmux session, and command history are attributed to the actual dev
- Access = Tailnet membership. No secondary auth layer

---

## Operator Quick-Start

### Invite a dev

```bash
sudo ./scripts/03-shared-jam.sh invite alice@example.com
```

The script shares the Tailscale node and prints the SSH command the dev should use.
It also prints the exact `box register` command they can run from their local `skillbox` clone to populate operator inventory.

### Revoke access

```bash
sudo ./scripts/03-shared-jam.sh revoke alice@example.com
```

The dev can no longer create new SSH connections. To immediately end active sessions: `tmux kill-session -t alice`.

### List who has access

```bash
sudo ./scripts/03-shared-jam.sh list
```

### Check who's active

```bash
sudo ./scripts/03-shared-jam.sh status
```

Shows active tmux sessions and the last 20 lines of shared command history.

---

## Collaborator Quick-Start

### 1. Accept the Tailscale share

The operator will share the node with you. Accept it in your Tailscale client.

### 2. Register the shared box locally if you want operator MCP

From your own `skillbox` clone, run the `box register` command printed by `03-shared-jam.sh invite`. Example:

```bash
python3 scripts/box.py register alice --host skillbox-dev.tailnet.ts.net --ssh-user skillbox
```

This writes a local inventory entry for `operator_boxes`, `operator_box_status`, and `operator_box_exec` without touching `workspace/boxes.json` by hand.

### 3. SSH in

```bash
ssh skillbox@skillbox-dev
```

(The operator will tell you the exact hostname and login user. The default shared login user is `skillbox`, but boxes installed with `SSH_LOGIN_USER=sandbox` will print `sandbox` instead.)

### 4. What happens automatically

On login, the system:

1. Resolves your Tailscale identity
2. Sets `GIT_AUTHOR_NAME` and `GIT_AUTHOR_EMAIL` to your Tailscale profile
3. Creates (or reattaches) a tmux session named after you
4. Starts logging your commands to the shared history

You don't need to configure anything.

### 5. Pair programming

To join someone else's terminal:

```bash
tmux attach -t alice
```

Both of you see the same terminal in real-time.

### 6. Disconnect

Detach from tmux with `Ctrl-b d` or just close the terminal. Your tmux session persists — reconnect anytime with the same SSH command.

---

## What's Shared

Everything. Same Linux user, same repos, same Docker containers, same `.claude/` config, same services. This is by design — skillbox is single-tenant, and collaborators are trusted.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| "could not resolve Tailscale identity" warning | You'll be logged in as `unknown`. Check that Tailscale is running on both ends |
| `scp` or `rsync` not working | These work through ForceCommand — they skip tmux but transfer normally |
| Can't SSH after being invited | Make sure you accepted the node share in your Tailscale client |
| Git commits show wrong author | Check `echo $GIT_AUTHOR_NAME` — if it says `unknown`, Tailscale identity resolution failed |
