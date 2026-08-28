#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
# One-command Agama demo: starts swtpm, serves the profile files, extracts
# the installer kernel/initrd from the SLES ISO, and boots QEMU with
# inst.auto= pointing at the chosen profile -- fully unattended, no manual
# clicking through the Agama web UI.
#
# Usage: ./launch-demo.sh [--manual] [atm-slim|atm-full|atm-full-16.0] [iso-path]
#
# --manual: load the profile (root password, product, ...) but do NOT
#   auto-trigger the install. Confirmed by reading
#   rust/agama-autoinstall/src/main.rs (SLE-16 branch): the agama-autoinstall
#   helper always applies the inst.auto= profile, then calls
#   manager.install() unconditionally UNLESS the kernel command line also has
#   inst.install=0. With install suppressed, the web UI/API comes up and sits
#   in the Config phase with a known root password, ready to be driven
#   remotely via agama-console.py to demo the Startup/Config/Install/Finish
#   states live. See README.md "Live API demo" section.
#
# Kernel parameter note: the customer-facing SLES 16.0 "Automated
# Installation Using Agama" guide documents `inst.auto=URL` for this. The
# agama upstream repo's own doc/boot_arguments.md instead documents
# `inst.config_url=URL` for the same purpose. This script uses `inst.auto`
# (the officially documented one); if your build doesn't recognize it, swap
# in `inst.config_url` in the APPEND line below.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MANUAL=0
if [ "${1:-}" = "--manual" ]; then
    MANUAL=1
    shift
fi

PROFILE="${1:-atm-slim}"
ISO="${2:-/data/iso/SLES-16.0-Full-x86_64-GM.install.iso}"
DISK="${DISK:-$HERE/vm/agama-demo.qcow2}"
DISK_SIZE="${DISK_SIZE:-20G}"
MEM="${MEM:-4096}"
SMP="${SMP:-4}"
HTTP_PORT="${HTTP_PORT:-8000}"
BRIDGE="${BRIDGE:-virbr-suse}"
BRIDGE_HOST_IP="${BRIDGE_HOST_IP:-192.168.110.1}"
LIVE_PASSWORD="${LIVE_PASSWORD:-DemoSecurity2026!}"
TPM_DIR="${TPM_DIR:-/tmp/mytpm}"
TPM_SOCK="$TPM_DIR/swtpm-sock"
OVMF="${OVMF:-/usr/share/qemu/ovmf-x86_64-4m.bin}"
CACHE="$HERE/vm/cache"

PROFILE_FILE="$PROFILE.jsonnet"
if [ ! -f "$HERE/profiles/$PROFILE_FILE" ]; then
    echo "[!] Unknown profile '$PROFILE'. Available: $(ls "$HERE"/profiles/*.jsonnet | xargs -n1 basename | sed 's/\.jsonnet$//' | tr '\n' ' ')"
    exit 1
fi
if [ ! -f "$ISO" ]; then
    echo "[!] ISO not found: $ISO"
    exit 1
fi

mkdir -p "$HERE/vm" "$CACHE"

if ! virsh --connect qemu:///system net-info SUSE >/dev/null 2>&1; then
    echo "[!] libvirt network 'SUSE' not found (virsh --connect qemu:///system net-list --all)"
    exit 1
fi
if ! virsh --connect qemu:///system net-list --name | grep -qx SUSE; then
    echo "[*] Starting libvirt network 'SUSE'..."
    virsh --connect qemu:///system net-start SUSE
fi

SWTPM_PID=""
HTTPD_PID=""
cleanup() {
    [ -n "$HTTPD_PID" ] && kill "$HTTPD_PID" 2>/dev/null
    [ -n "$SWTPM_PID" ] && kill "$SWTPM_PID" 2>/dev/null
}
trap cleanup EXIT

# --- 1. swtpm --------------------------------------------------------------
if [ -S "$TPM_SOCK" ] && pgrep -f "swtpm socket.*$TPM_DIR" >/dev/null; then
    echo "[*] swtpm already running at $TPM_SOCK"
else
    rm -rf "$TPM_DIR"
    mkdir -p "$TPM_DIR"
    echo "[*] Starting swtpm..."
    swtpm socket --tpmstate dir="$TPM_DIR" \
        --ctrl type=unixio,path="$TPM_SOCK" \
        --tpm2 \
        --log level=1 &
    SWTPM_PID=$!
    sleep 1
fi

# --- 2. profile HTTP server -------------------------------------------------
if pgrep -f "http.server $HTTP_PORT" >/dev/null; then
    echo "[*] Profile server already running on port $HTTP_PORT"
else
    echo "[*] Starting profile server on port $HTTP_PORT..."
    "$HERE/serve-profiles.sh" "$HTTP_PORT" >/tmp/agama-demo-profiles.log 2>&1 &
    HTTPD_PID=$!
    sleep 1
fi

# --- 3. extract installer kernel/initrd from the ISO ------------------------
ISO_TAG="$(basename "$ISO")"
LINUX="$CACHE/$ISO_TAG.linux"
INITRD="$CACHE/$ISO_TAG.initrd"
if [ ! -f "$LINUX" ] || [ ! -f "$INITRD" ]; then
    echo "[*] Extracting kernel/initrd from $ISO_TAG..."
    isoinfo -R -x /boot/x86_64/loader/linux -i "$ISO" > "$LINUX"
    isoinfo -R -x /boot/x86_64/loader/initrd -i "$ISO" > "$INITRD"
fi

# --- 4. demo disk ------------------------------------------------------------
if [ ! -f "$DISK" ]; then
    echo "[*] Creating demo disk $DISK ($DISK_SIZE)..."
    qemu-img create -f qcow2 "$DISK" "$DISK_SIZE"
fi

# NOTE: the profile's `root.password` only configures the *installed target*
# system's root account -- it has NO effect on the live installer's own PAM
# account, which Agama's HTTP API auths against for any non-localhost client.
# The live media sets that instead from the `live.password=` kernel arg (see
# live/live-root/usr/bin/live-password in the agama repo); without it, a
# random password is generated and only shown on the console/VNC. We always
# pass it so SSH/API access has a known, predictable password.
APPEND="inst.auto=http://$BRIDGE_HOST_IP:$HTTP_PORT/$PROFILE_FILE console=ttyS0 live.password=$LIVE_PASSWORD"
if [ "$MANUAL" -eq 1 ]; then
    APPEND="$APPEND inst.install=0"
fi

echo "[*] Booting profile '$PROFILE' against $ISO_TAG"
echo "[*] Network      -> bridged on $BRIDGE (libvirt 'SUSE' network, 192.168.110.0/24, DHCP)"
echo "[*] VNC display  -> localhost:5901 (display :1)"
echo "[*]   Guest IP is assigned by DHCP -- once booted, find it with:"
echo "[*]     virsh --connect qemu:///system net-dhcp-leases SUSE"
echo "[*]   Then SSH/HTTPS/API are reached directly at that IP (22 / 443), no port forwarding."
if [ "$MANUAL" -eq 1 ]; then
    echo "[*] MANUAL mode: profile config will load but install will NOT auto-trigger."
    echo "[*] Once the console shows the Agama service is up, drive it remotely with:"
    echo "[*]   ./agama-console.py <guest-ip>:443 --password $LIVE_PASSWORD"
    echo "[*]   (agama-console.py builds https://<ip[:port]>/api from that first argument;"
    echo "[*]   use Storage -> 'Run full scripted install' to drive a predefined profile end-to-end)"
fi

qemu-system-x86_64 \
    -enable-kvm \
    -machine q35 \
    -cpu host \
    -m "$MEM" \
    -smp "$SMP" \
    -bios "$OVMF" \
    -drive file="$DISK",if=virtio,format=qcow2 \
    -cdrom "$ISO" \
    -kernel "$LINUX" \
    -initrd "$INITRD" \
    -append "$APPEND" \
    -chardev socket,id=chrtpm,path="$TPM_SOCK" \
    -tpmdev emulator,id=tpm0,chardev=chrtpm \
    -device tpm-crb,tpmdev=tpm0 \
    -netdev bridge,id=net0,br="$BRIDGE" \
    -device virtio-net-pci,netdev=net0 \
    -vnc :1 \
    -serial stdio
