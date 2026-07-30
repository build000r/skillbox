#!/usr/bin/env bash

# Join an Amp Orb to the Skillbox tailnet without exposing the auth key.
# Exit codes: 0 joined/healthy, 10 missing key, 20 install failure,
# 30 join or argument failure.

set -uo pipefail
set +x

readonly EXIT_NO_KEY=10
readonly EXIT_INSTALL_FAIL=20
readonly EXIT_JOIN_FAIL=30
readonly DEFAULT_BOX_HEALTH_URL='http://100.100.1.3:8443/healthz'
readonly NETMON_ADDRESS='10.254.254.254/32'
readonly DEFAULT_NETMON_DEVICE='eth0'

usage() {
  printf '%s\n' \
    'Usage: join-tailnet.sh [--resume] [--box-health-url URL]' \
    '' \
    '  --resume               Keep the current join when the box health check passes;' \
    '                         otherwise perform a full rejoin.' \
    "  --box-health-url URL   Health endpoint (default: ${DEFAULT_BOX_HEALTH_URL})."
}

notice() {
  printf 'join-tailnet: %s\n' "$1" >&2
}

run_privileged() {
  if (( EUID == 0 )); then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    return 126
  fi
}

box_is_healthy() {
  local health_url="$1"

  command -v curl >/dev/null 2>&1 &&
    curl --fail --silent --show-error \
      --connect-timeout 5 \
      --max-time 10 \
      --output /dev/null \
      "$health_url" >/dev/null 2>&1
}

tailnet_is_joined() {
  command -v tailscale >/dev/null 2>&1 &&
    tailscale ip -4 2>/dev/null | grep -Eq '^100\.'
}

install_tailscale() {
  command -v tailscale >/dev/null 2>&1 && return 0
  command -v curl >/dev/null 2>&1 || return 1

  notice 'tailscale is absent; installing it'
  if (( EUID == 0 )); then
    curl --fail --silent --show-error --location \
      https://tailscale.com/install.sh | sh
  elif command -v sudo >/dev/null 2>&1; then
    curl --fail --silent --show-error --location \
      https://tailscale.com/install.sh | sudo sh
  else
    return 1
  fi

  command -v tailscale >/dev/null 2>&1
}

netmon_device() {
  local device

  if command -v ip >/dev/null 2>&1 &&
    ip link show dev "$DEFAULT_NETMON_DEVICE" >/dev/null 2>&1; then
    printf '%s\n' "$DEFAULT_NETMON_DEVICE"
    return 0
  fi

  device="$(
    ip -4 route show default 2>/dev/null |
      awk 'NR == 1 { for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i + 1); exit } }'
  )"
  [[ -n "$device" ]] || return 1
  printf '%s\n' "$device"
}

apply_netmon_fix() {
  local device

  command -v ip >/dev/null 2>&1 || return 1
  device="$(netmon_device)" || return 1

  if ip -4 address show dev "$device" 2>/dev/null |
    grep -Fq "$NETMON_ADDRESS"; then
    return 0
  fi

  notice "adding the Orb netmon address on ${device}"
  run_privileged ip address add "$NETMON_ADDRESS" dev "$device"
}

restart_tailscaled() {
  if command -v systemctl >/dev/null 2>&1; then
    run_privileged systemctl restart tailscaled
  elif command -v service >/dev/null 2>&1; then
    run_privileged service tailscaled restart
  else
    return 1
  fi
}

join_tailnet() {
  local join_key="$1"
  local join_status
  local -a join_command=(
    sh -c
    'IFS= read -r join_key || exit 1; exec "$@" "--auth-key=${join_key}"'
    sh
    tailscale up
    --advertise-tags=tag:orb
    --hostname=amp-orb
    --accept-routes=false
    --accept-dns=false
  )

  if command -v timeout >/dev/null 2>&1; then
    if printf '%s\n' "$join_key" |
      run_privileged timeout 60s "${join_command[@]}"; then
      join_status=0
    else
      join_status=$?
    fi
  elif printf '%s\n' "$join_key" |
    run_privileged "${join_command[@]}"; then
    join_status=0
  else
    join_status=$?
  fi

  unset join_key
  return "$join_status"
}

main() {
  local resume=false
  local box_health_url="$DEFAULT_BOX_HEALTH_URL"
  local join_key

  while (( $# > 0 )); do
    case "$1" in
      --resume)
        resume=true
        shift
        ;;
      --box-health-url)
        if (( $# < 2 )) || [[ -z "$2" ]]; then
          usage >&2
          return "$EXIT_JOIN_FAIL"
        fi
        box_health_url="$2"
        shift 2
        ;;
      --box-health-url=*)
        box_health_url="${1#*=}"
        if [[ -z "$box_health_url" ]]; then
          usage >&2
          return "$EXIT_JOIN_FAIL"
        fi
        shift
        ;;
      -h|--help)
        usage
        return 0
        ;;
      *)
        usage >&2
        return "$EXIT_JOIN_FAIL"
        ;;
    esac
  done

  if [[ "$resume" == true ]] && box_is_healthy "$box_health_url"; then
    notice 'box health check passed; keeping the current tailnet join'
    return 0
  fi

  if [[ "$resume" == false ]] && tailnet_is_joined; then
    notice 'tailnet is already joined'
    return 0
  fi

  if [[ -z "${TAILSCALE_AUTHKEY:-}" ]]; then
    notice 'TAILSCALE_AUTHKEY is required to join'
    return "$EXIT_NO_KEY"
  fi
  join_key="$TAILSCALE_AUTHKEY"
  unset TAILSCALE_AUTHKEY

  if ! install_tailscale; then
    unset join_key
    notice 'tailscale installation failed'
    return "$EXIT_INSTALL_FAIL"
  fi

  if ! apply_netmon_fix; then
    unset join_key
    notice 'could not apply the Orb netmon address'
    return "$EXIT_JOIN_FAIL"
  fi

  if [[ "$resume" == true ]]; then
    run_privileged tailscale logout >/dev/null 2>&1 || true
  fi

  if ! restart_tailscaled; then
    unset join_key
    notice 'could not restart tailscaled'
    return "$EXIT_JOIN_FAIL"
  fi

  if ! join_tailnet "$join_key"; then
    unset join_key
    notice 'tailscale join failed'
    return "$EXIT_JOIN_FAIL"
  fi
  unset join_key

  if ! tailnet_is_joined; then
    notice 'tailscale join completed without a tailnet IPv4 address'
    return "$EXIT_JOIN_FAIL"
  fi

  notice 'tailnet join complete'
  return 0
}

main "$@"
