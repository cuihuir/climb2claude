#!/usr/bin/env bash
# Purpose: keep a Magpie SSH tunnel with traffic and Claude 5H/Weekly quota.
# Dependencies: bash, OpenSSH, Python 3.11+; sibling magpie_tunnel.py only.
# Effects: opens loopback ports, reconnects, reads quota; optional Claude settings update.
# Usage: no arguments to start; status to query a running tunnel; --no-usage for traffic only.
# First setup: --configure-claude adds gateway/models and a status line, preserving custom status lines.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$script_dir/magpie_tunnel.py" "$@"
