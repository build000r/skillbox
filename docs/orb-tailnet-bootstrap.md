# Amp Orb Tailnet Bootstrap

This runbook joins a project-backed Amp Orb to the tailnet as an ephemeral
`tag:orb` node, then points the Orb's `sbp` client at `sbpd` on the operator
box. It contains identifiers and placeholders only. Do not add auth keys, API
tokens, OIDC tokens, or copied command output containing them.

## Security baseline

- Preferred: Amp OIDC workload identity -> Tailscale federated trust
  credential. No shared Tailscale auth key enters Amp.
- Transitional key delivery, ranked: Amp project secret > SSH standard input >
  never put a key in an agent prompt, thread message, command literal, tracked
  file, or normal log.
- Orb nodes stay `ephemeral`, `preauthorized`, and restricted to `tag:orb`.
  Tailnet grants remain the authorization boundary.
- Disable shell tracing around every credential operation. Unset shell
  variables and remove mode-0600 token files immediately after use.
- Treat any key pasted into a thread as compromised even if Amp redacted the
  rendered view. Revoke it; deleting or archiving the thread is not rotation.

## Proven E2B join recipe

Evidence: Amp thread `T-019fb489-4d06-741c-9120-c32a641b1d58`, 2026-07-30.
The E2B Orb's `eth0` had only `169.254.0.21/30`, with its default route through
`169.254.0.22`. Tailscale netmon excluded that link-local address, reported the
network down, and paused control-plane dials. `tailscale up` therefore stayed
in `NeedsLogin` even though HTTPS and the `ts2021` handshake worked.

The proven repair was to add one non-link-local `/32` before restarting
`tailscaled`:

```bash
set -euo pipefail

ip -4 address show dev eth0
ip -4 route show default

if ! ip -4 address show dev eth0 | grep -q '10\.254\.254\.254/32'; then
  sudo ip address add 10.254.254.254/32 dev eth0
fi

sudo systemctl restart tailscaled
sudo timeout 60s tailscale up \
  --auth-key="${TAILSCALE_AUTHKEY:?credential was not delivered}" \
  --advertise-tags=tag:orb \
  --hostname=amp-orb \
  --accept-routes=false \
  --accept-dns=false
unset TAILSCALE_AUTHKEY

tailscale ip -4
tailscale status
```

Expected proof:

- `tailscale up` exits `0`, not timeout exit `124`.
- `tailscale ip -4` prints one `100.x` address.
- The admin/device view reports `tag:orb` and an ephemeral node.
- The Orb can reach only ports granted to `tag:orb`; an ungranted TCP/22 probe
  remains blocked.

The `/32` is a netmon signal, not a routed service address. Do not publish or
bind services to it.

For a durable fresh-Orb setup, Amp now documents a systemd drop-in using
`TS_ASSUME_NETWORK_UP_FOR_TEST=true` for E2B's link-local-only interface. Use
Tailscale 1.90.1 or later, install the drop-in before restarting `tailscaled`,
and validate the same `100.x` plus ACL proof before replacing the locally
proven `/32` workaround:

```bash
sudo install -d /etc/systemd/system/tailscaled.service.d
sudo tee /etc/systemd/system/tailscaled.service.d/amp-orb.conf >/dev/null <<'EOF'
[Service]
Environment=TS_ASSUME_NETWORK_UP_FOR_TEST=true
EOF
sudo systemctl daemon-reload
sudo systemctl enable tailscaled
sudo systemctl restart tailscaled
```

Source: [Amp OIDC/Tailscale Orb recipe](https://ampcode.com/manual/orbs/oidc).

## Preferred future join: Amp OIDC, no shared auth key

Configure this once from trusted admin surfaces:

1. In Tailscale Trust credentials, create an OpenID Connect credential.
2. Set issuer to `https://ampcode.com/api/workload-identity`.
3. Restrict the subject to the intended immutable Amp workspace and project
   IDs. Also require exact `workspace_id`, `project_id`, and
   `token_use=exchanged` claims.
4. Grant only `auth_keys` and allow only `tag:orb`.
5. Store the generated client ID and audience as non-secret Amp project
   environment variables `TAILSCALE_CLIENT_ID` and `TAILSCALE_AUDIENCE`.

Then join inside the Orb without putting its short-lived OIDC token in argv or
logs:

```bash
set -euo pipefail
umask 077

identity_file="$(mktemp)"
trap 'rm -f "$identity_file"' EXIT

amp orb id-token \
  --audience "${TAILSCALE_AUDIENCE:?missing project environment variable}" \
  >"$identity_file"

sudo tailscale up \
  --client-id="${TAILSCALE_CLIENT_ID:?missing project environment variable}?ephemeral=true&preauthorized=true" \
  --id-token="file:${identity_file}" \
  --advertise-tags=tag:orb \
  --hostname=amp-orb \
  --accept-routes=false \
  --accept-dns=false

rm -f "$identity_file"
trap - EXIT
tailscale ip -4
```

Amp's default ID-token lifetime is ten minutes. The audience is an identifier,
not authorization: Tailscale must verify issuer, signature, audience, expiry,
workspace, project, and `token_use` claims. See
[Amp OIDC workload identity](https://ampcode.com/manual/orbs/oidc) and
[Tailscale workload identity federation](https://tailscale.com/docs/features/workload-identity-federation).

If operator policy must be centralized in Skillbox instead, a later `sbpd`
endpoint can verify the same Amp JWT and mint a single-use, very short-lived,
ephemeral `tag:orb` join credential. That endpoint must never log the JWT or
returned credential and must bind issuance to immutable workspace/project
claims. Current `sbpd` v1 is intentionally read-only; this broker endpoint is
not part of the current cass/search client work.

## Transitional auth-key delivery

Use these only until OIDC federation is live.

### 1. Amp project secret

Set the key from a trusted local terminal through standard input. `amp secrets
set --data-file -` keeps the value out of argv and shell history:

```bash
set +x
read -rsp 'Tailscale Orb auth key: ' TAILSCALE_AUTHKEY
printf '\n'
printf '%s' "$TAILSCALE_AUTHKEY" |
  amp secrets set TAILSCALE_AUTHKEY \
    --project '<amp-project-id-or-name>' \
    --secret \
    --data-file -
unset TAILSCALE_AUTHKEY

amp secrets list --project '<amp-project-id-or-name>' --json
```

The Orb receives `TAILSCALE_AUTHKEY` as an environment variable. Consume it
without printing it, run the proven join recipe, then `unset` it. Project
secrets override workspace entries with the same name; keep this credential
project-scoped.

Source: [Amp Orb secrets and environment variables](https://ampcode.com/manual/orbs).

### 2. SSH standard input

If an authenticated direct SSH transport to the target environment exists,
stream the key to a non-interactive remote shell. Never place it in the SSH
command string:

```bash
set +x
read -rsp 'Tailscale Orb auth key: ' TAILSCALE_AUTHKEY
printf '\n'
printf '%s' "$TAILSCALE_AUTHKEY" |
  ssh '<orb-bootstrap-target>' '
    set +x
    TAILSCALE_AUTHKEY=$(cat)
    export TAILSCALE_AUTHKEY
    sudo tailscale up \
      --auth-key="$TAILSCALE_AUTHKEY" \
      --advertise-tags=tag:orb \
      --hostname=amp-orb \
      --accept-routes=false \
      --accept-dns=false
    unset TAILSCALE_AUTHKEY
  '
unset TAILSCALE_AUTHKEY
```

This avoids prompts, history, and tracked files, but the remote process still
temporarily receives the credential. Prefer the Amp secret or OIDC path.
Tailscale's own handling guidance likewise recommends standard input into an
environment variable rather than a command literal:
[secure auth-key handling](https://tailscale.com/docs/features/access-control/auth-keys/how-to/secure-auth-keys).

### 3. Prompt embedding is forbidden

Never ask an agent to run a command containing a credential value. Never paste
one into an Amp thread, NTM message, Bead, issue comment, result artifact,
setup log, or shell command literal. Automatic redaction is defense in depth,
not a delivery mechanism.

## Mac-lane API v2 rotation

Target auth-key ID: `REDACTEDOLDKEYIDCNTRL`.

Run this only from the trusted Mac lane. It expects a short-lived Tailscale API
access token already stored in macOS Keychain under service
`skillbox-tailscale-api-v2`. The token needs permission to read and delete auth
keys. The script passes the bearer header to `curl` through standard input, so
the token is absent from argv and shell history.

```bash
(
  set -euo pipefail
  set +x

  target_key_id='REDACTEDOLDKEYIDCNTRL'
  key_url="https://api.tailscale.com/api/v2/tailnet/-/keys/${target_key_id}"
  TAILSCALE_API_TOKEN="$(
    security find-generic-password \
      -s skillbox-tailscale-api-v2 \
      -w
  )"
  trap 'unset TAILSCALE_API_TOKEN' EXIT

  api_status() {
    method="$1"
    url="$2"
    printf 'header = "Authorization: Bearer %s"\n' "$TAILSCALE_API_TOKEN" |
      curl --config - \
        --silent \
        --show-error \
        --output /dev/null \
        --write-out '%{http_code}' \
        --request "$method" \
        "$url"
  }

  before="$(api_status GET "$key_url")"
  case "$before" in
    200) ;;
    404)
      printf 'auth key already absent: %s\n' "$target_key_id"
      exit 0
      ;;
    *)
      printf 'pre-delete GET failed: HTTP %s\n' "$before" >&2
      exit 1
      ;;
  esac

  deleted="$(api_status DELETE "$key_url")"
  case "$deleted" in
    200|204) ;;
    *)
      printf 'DELETE failed: HTTP %s\n' "$deleted" >&2
      exit 1
      ;;
  esac

  after="$(api_status GET "$key_url")"
  test "$after" = 404 || {
    printf 'post-delete GET expected HTTP 404, got %s\n' "$after" >&2
    exit 1
  }

  printf 'revoked auth-key ID %s; post-delete GET=404\n' "$target_key_id"
)
```

Afterward:

1. Confirm the Tailscale configuration audit log records the deletion.
2. Remove the compromised key from Amp project secrets if it was stored there:
   `amp secrets delete TAILSCALE_AUTHKEY --project '<amp-project-id-or-name>'`.
3. Start a fresh Orb through the OIDC path and repeat the `100.x` plus ACL
   proof. Existing ephemeral Orb nodes do not need the revoked bootstrap key
   to keep their current node session.

The API path is
`DELETE /api/v2/tailnet/:tailnet/keys/:keyID`; `-` selects the token's own
tailnet. Prefer a Tailscale trust credential scoped to `auth_keys` over a
full-permission API access token. Sources:
[Tailscale API authentication](https://tailscale.com/docs/reference/tailscale-api) and
[Tailscale trust-credential scopes](https://tailscale.com/docs/reference/trust-credentials).

## `sbpd` and `SBP_REMOTE` bootstrap

Once Tailscale returns a `100.x` address and the dedicated `sbpd` service is
healthy on its tailnet-only listener:

```bash
set -euo pipefail

install -d "$HOME/.local/bin"
ln -sfn "$PWD/scripts/sbp" "$HOME/.local/bin/sbp"
export PATH="$HOME/.local/bin:$PATH"

export SBP_REMOTE='http://<d3-tailnet-ip>:8443'
sbp cass status
sbp cass search 'tailnet'
```

`SBP_REMOTE` is an endpoint, not a credential. Access still depends on the
Orb's `tag:orb` identity and tailnet grants. Do not change `sbpd` to
`0.0.0.0`; bind it to loopback and/or the box's Tailscale address.
