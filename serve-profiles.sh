#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
# Serves ./profiles over plain HTTP so a booting VM can fetch one via
# inst.auto=http://<host>:<port>/<profile>.jsonnet on the kernel command line.
#
# launch-demo.sh bridges the guest onto the libvirt "SUSE" network
# (virbr-suse, 192.168.110.0/24), so from inside the VM the host -- and this
# server -- is reachable at the bridge's host-side IP:
#   http://192.168.110.1:8000/atm-slim.jsonnet
set -euo pipefail

PORT="${1:-8000}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/profiles"

echo "[*] Serving $DIR on 0.0.0.0:$PORT"
echo "[*] From the VM (SUSE bridge net): http://192.168.110.1:$PORT/<profile>.jsonnet"
exec python3 -m http.server "$PORT" --bind 0.0.0.0 --directory "$DIR"
