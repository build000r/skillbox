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

## Operator device access + key lifecycle

Run every API step in this section only from the trusted Mac lane. The examples
expect `TAILSCALE_API_KEY` to be loaded from the Mac's credential store before
the subshell starts. Never paste its value into this file, a command literal,
an Amp thread, or a result artifact.

### Operator device access to d3:8443

**Usually unnecessary — check before adding anything.** On this tailnet the
existing grant `build000r@github -> autogroup:self -> ip:["*"]` already gives
every operator-owned device full access to d3, verified 2026-07-31: a Mac
`curl http://100.79.193.34:8443/healthz` returned HTTP 200 with no new grant.
The tailnet has exactly one member (other identities are shared-in, not
members), so `autogroup:member` adds nothing here either.

Only if operator devices ever lose that blanket self-grant, append this object
to the top-level `grants` array (d3's tailnet IP is `100.79.193.34`):

```json
{
  "src": ["autogroup:member"],
  "dst": ["100.79.193.34"],
  "ip": ["tcp:8443"]
}
```

On a shared tailnet, replace `autogroup:member` with only the operator
devices' stable Tailscale IPs or host aliases. Do not add named devices
alongside `autogroup:member`; grant source arrays are unions, so that would
not narrow access.

Fetch the complete current policy, append the object, validate the candidate
without mutation, then apply it with the `ETag` returned by that same fetch.
The `If-Match` guard makes a concurrent policy edit fail with HTTP 412 instead
of overwriting it:

```bash
(
  set -euo pipefail
  set +x
  : "${TAILSCALE_API_KEY:?load TAILSCALE_API_KEY from the Mac credential store}"

  acl_url='https://api.tailscale.com/api/v2/tailnet/-/acl'
  tailnet_policy_tmp="$(mktemp -d)"
  headers_file="${tailnet_policy_tmp}/headers"
  policy_file="${tailnet_policy_tmp}/policy.hujson"
  validate_file="${tailnet_policy_tmp}/validate.json"
  updated_file="${tailnet_policy_tmp}/updated.hujson"

  cleanup() {
    rm -f -- \
      "$headers_file" \
      "$policy_file" \
      "$validate_file" \
      "$updated_file"
    rmdir -- "$tailnet_policy_tmp"
    unset TAILSCALE_API_KEY
  }
  trap cleanup EXIT

  tailscale_api() {
    printf 'header = "Authorization: Bearer %s"\n' "$TAILSCALE_API_KEY" |
      curl --config - \
        --silent \
        --show-error \
        --fail-with-body \
        "$@"
  }

  tailscale_api \
    --header 'Accept: application/hujson' \
    --dump-header "$headers_file" \
    --output "$policy_file" \
    "$acl_url"

  etag="$(
    python3 - "$headers_file" <<'PY'
import pathlib
import sys

for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    if line.lower().startswith("etag:"):
        print(line.split(":", 1)[1].strip())
        break
else:
    raise SystemExit("policy GET returned no ETag")
PY
  )"

  vi "$policy_file"

  tailscale_api \
    --request POST \
    --header 'Content-Type: application/hujson' \
    --data-binary "@${policy_file}" \
    --output "$validate_file" \
    "${acl_url}/validate"

  python3 - "$validate_file" <<'PY'
import json
import pathlib
import sys

result = json.loads(pathlib.Path(sys.argv[1]).read_text() or "{}")
if result:
    raise SystemExit(
        "policy validation did not pass cleanly: "
        + json.dumps(result, separators=(",", ":"))
    )
PY

  tailscale_api \
    --request POST \
    --header 'Accept: application/hujson' \
    --header 'Content-Type: application/hujson' \
    --header "If-Match: ${etag}" \
    --data-binary "@${policy_file}" \
    --output "$updated_file" \
    "$acl_url"

  printf 'policy validated and applied with If-Match %s\n' "$etag"
)
```

After applying, prove an operator device can reach
`http://100.79.193.34:8443/healthz` and that an ungranted port remains blocked.
Do not treat a clean `/acl/validate` response as live connectivity proof.

### Rotate the Orb bootstrap auth key

Target old auth-key ID: `REDACTEDOLDKEYIDCNTRL`. This checklist creates a 90-day
reusable, ephemeral, preauthorized `tag:orb` replacement, writes the returned
key directly into the Amp user-level secret `TAILSCALE_AUTHKEY`, then revokes
the old credential. Mint-before-revoke avoids losing bootstrap access when key
creation or secret delivery fails. The response file is mode 0600 and is
removed by the exit trap; never print or retain its `key` field.

```bash
(
  set -euo pipefail
  set +x
  umask 077
  : "${TAILSCALE_API_KEY:?load TAILSCALE_API_KEY from the Mac credential store}"

  old_key_id='REDACTEDOLDKEYIDCNTRL'
  keys_url='https://api.tailscale.com/api/v2/tailnet/-/keys'
  old_key_url="${keys_url}/${old_key_id}"
  key_rotation_tmp="$(mktemp -d)"
  request_file="${key_rotation_tmp}/request.json"
  response_file="${key_rotation_tmp}/response.json"

  cleanup() {
    rm -f -- "$request_file" "$response_file"
    rmdir -- "$key_rotation_tmp"
    unset TAILSCALE_API_KEY
  }
  trap cleanup EXIT

  tailscale_api_status() {
    method="$1"
    url="$2"
    output_file="${3:-/dev/null}"
    if [ "$#" -ge 3 ]; then
      shift 3
    else
      shift 2
    fi

    printf 'header = "Authorization: Bearer %s"\n' "$TAILSCALE_API_KEY" |
      curl --config - \
        --silent \
        --show-error \
        --output "$output_file" \
        --write-out '%{http_code}' \
        --request "$method" \
        "$@" \
        "$url"
  }

  cat >"$request_file" <<'JSON'
{
  "keyType": "auth",
  "description": "Amp Orb bootstrap",
  "expirySeconds": 7776000,
  "capabilities": {
    "devices": {
      "create": {
        "reusable": true,
        "ephemeral": true,
        "preauthorized": true,
        "tags": ["tag:orb"]
      }
    }
  }
}
JSON

  created="$(
    tailscale_api_status \
      POST \
      "$keys_url" \
      "$response_file" \
      --header 'Content-Type: application/json' \
      --data-binary "@${request_file}"
  )"
  test "$created" = 200 || {
    printf 'replacement-key POST failed: HTTP %s\n' "$created" >&2
    exit 1
  }

  python3 - "$response_file" <<'PY' |
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text())
key = payload.get("key")
if not isinstance(key, str) or not key:
    raise SystemExit("replacement response has no key")
sys.stdout.write(key)
PY
    amp secrets set TAILSCALE_AUTHKEY \
      --user \
      --secret \
      --data-file -

  deleted="$(tailscale_api_status DELETE "$old_key_url")"
  case "$deleted" in
    200|204) ;;
    *)
      printf 'old-key DELETE failed: HTTP %s\n' "$deleted" >&2
      exit 1
      ;;
  esac

  # Tailscale keeps deleted keys as audit tombstones: post-delete GET returns
  # HTTP 200 with invalid:true + revoked:<ts>, NOT 404 (asserting 404 fails
  # every successful rotation — observed live 2026-07-31 on REDACTEDOLDKEYIDCNTRL).
  after_invalid="$(curl -fsS -H "Authorization: Bearer ${TAILSCALE_API_KEY}" \
    "$old_key_url" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("invalid"))')"
  test "$after_invalid" = "True" || {
    printf 'post-delete GET expected invalid:true tombstone, got invalid=%s\n' "$after_invalid" >&2
    exit 1
  }

  new_key_id="$(
    python3 - "$response_file" <<'PY'
import json
import pathlib
import sys

print(json.loads(pathlib.Path(sys.argv[1]).read_text())["id"])
PY
  )"
  printf 'revoked %s; minted %s; refreshed Amp user secret\n' \
    "$old_key_id" \
    "$new_key_id"
)
```

The `tailscale_api_status` helper intentionally accepts extra `curl` arguments
after its output-file parameter. Confirm the Tailscale configuration audit log
records the deletion and creation, then run
`amp secrets list --user --json` and verify only that `TAILSCALE_AUTHKEY`
exists; secret-list output must not contain its value.

Deleting an auth key prevents future registrations but does not disconnect an
Orb that is still online. Amp pauses can remove an inactive ephemeral node
(~1 hour observed); that Orb then resumes in `NeedsLogin` and must run the full
join again. The re-join consumes the refreshed Amp user secret automatically.
Prove a fresh or resumed Orb gets a `100.x` address, reaches d3:8443, and remains
blocked from an ungranted port.

The policy endpoints are `GET|POST /api/v2/tailnet/:tailnet/acl` and
`POST /api/v2/tailnet/:tailnet/acl/validate`; the key endpoints are
`POST /api/v2/tailnet/:tailnet/keys` and
`DELETE /api/v2/tailnet/:tailnet/keys/:keyID`. `-` selects the API credential's
own tailnet. Prefer trust credentials scoped to `policy_file` and `auth_keys`
over a full-permission API access token. Sources:
[Tailscale grants syntax](https://tailscale.com/docs/reference/syntax/grants),
[Tailscale API authentication](https://tailscale.com/docs/reference/tailscale-api),
and
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

## Operational findings (2026-07-30 live E2E, thread T-019fb489)

- **Ephemeral node removal on pause.** Amp orbs pause between `-ox` rounds. A
  long pause (~1h observed) lets the control plane remove the ephemeral node:
  `tailscale status` shows `Logged out.` / `state: NeedsLogin`. Restarting
  tailscaled is NOT enough — resume requires the full re-join:
  dummy-addr step, `systemctl restart tailscaled`, then
  `tailscale up --authkey=...`. Wake preamble should be:
  `curl -s --max-time 8 http://<box>:8443/healthz || <full re-join>`.
- **Standalone client needs the bundle verifier.** `scripts/lib/sbp_client.py`
  cass verbs are stdlib-only, but `skill pull` lazily imports
  `runtime_manager.distribution.bundle` (pure-stdlib module, ~10KB) for
  verified unpack. Off-repo hosts must ship that module and set
  `PYTHONPATH` (package scaffold: `runtime_manager/__init__.py`,
  `runtime_manager/distribution/__init__.py`, `bundle.py`), or install the
  skillbox repo. Verified determinism cross-transport: box-local and orb pulls
  of the `sbp` skill both yield tree
  `ae33c56e8a204f52998363a339af4f1f835ebcb25b0a453d630eef00d5e94629`.
