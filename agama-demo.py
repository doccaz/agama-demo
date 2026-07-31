#!/usr/bin/env python3
"""Drives an already-booted Agama live installer over its HTTP API.

Verified against the `SLE-16` branch of https://github.com/agama-project/agama
(the branch that ships in both SLES 16.0 and 16.1). Key facts baked into this
script:

- There is NO "/api/v0" or "/api/v1" prefix. Every module is mounted directly
  under /api/<module> (e.g. /api/storage, /api/software, /api/manager).
- Auth is POST /api/auth {"password": ...} -> sets an "agamaToken" cookie
  (requests.Session carries it automatically for later calls).
- Config is always GET/PUT /api/<module>/config, one module at a time. GET
  /api/storage/config wraps its body in a top-level "storage" key -- PUT
  needs that same wrapper, e.g. {"storage": {"drives": [...]}}, NOT a bare
  {"drives": [...]} (that silently becomes an empty D-Bus config and 400s
  with "Invalid JSON config: {}"). hostname/config and users/root are flat,
  no such wrapper.
- Storage config uses the "drives" schema (drives/volumeGroups/mdRaids), not
  the old flat {wipe, partitions:[{mount_point, encrypt}]} shape.
- Re-selecting the currently-selected product via PUT /api/software/config
  400s ("Product is already selected") -- check GET first and only PUT on an
  actual change. This bites you whenever the ISO only offers one product
  (e.g. plain SLES media), since it auto-selects at boot.
- TPM2 auto-unlock (encryption.luks2.tpm) only exists since Agama 16.1 -- see
  ATM_FULL_PROFILE below and --sles-version.
"""
import argparse
import sys
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BLUE, GREEN, YELLOW, RED, BOLD, ENDC = "\033[94m", "\033[92m", "\033[93m", "\033[91m", "\033[1m", "\033[0m"


def log(msg):
    print(f"{BLUE}[*]{ENDC} {msg}")


def success(msg):
    print(f"{GREEN}[+]{ENDC} {msg}")


def warn(msg):
    print(f"{YELLOW}[!]{ENDC} {msg}")


def error(msg):
    print(f"{RED}[!] Error: {msg}{ENDC}")


# InstallationPhase, as returned by GET /api/manager/installer (serialized as
# an integer -- see rust/agama-lib/src/manager.rs InstallationPhase).
PHASES = {0: "Startup", 1: "Config", 2: "Install", 3: "Finish"}

ROOT_PASSWORD = "DemoSecurity2026!"

PROFILES = {
    "1": {
        "name": "atm-slim",
        "desc": "Btrfs, No Encryption",
        "storage": {
            "drives": [
                {"partitions": [{"filesystem": {"path": "/", "type": "btrfs"}}]}
            ]
        },
    },
    "2": {
        "name": "atm-full",
        "desc": "FDE + TPM2 (TPM auto-unlock requires SLES 16.1+)",
        "storage": {
            "drives": [
                {
                    "partitions": [
                        {"filesystem": {"path": "/boot", "type": "ext4"}, "size": "1 GiB"},
                        {
                            "filesystem": {"path": "/", "type": "btrfs"},
                            "encryption": {
                                "luks2": {"password": ROOT_PASSWORD, "tpm": True}
                            },
                        },
                    ]
                }
            ]
        },
    },
}


class AgamaClient:
    def __init__(self, ip, password):
        self.ip = ip
        self.password = password
        self.session = requests.Session()
        self.session.verify = False
        self.api_root = f"https://{ip}/api"

    def connect(self):
        auth_url = f"{self.api_root}/auth"
        log(f"Authenticating at {auth_url}...")
        r = self.session.post(auth_url, json={"password": self.password})
        if r.status_code not in (200, 201, 204):
            error(f"Auth failed: {r.status_code} {r.text}")
            sys.exit(1)
        success("Authenticated (agamaToken cookie set).")

        api_version = r.headers.get("X-API-Version", "unknown")
        log(f"Server X-API-Version: {api_version}")

        status = self.call("GET", "manager/installer")
        log(f"Installer phase: {PHASES.get(status.get('phase'), status.get('phase'))}, "
            f"busy={status.get('isBusy')}, canInstall={status.get('canInstall')}")

    def call(self, method, endpoint, data=None):
        url = f"{self.api_root}/{endpoint}"
        func = getattr(self.session, method.lower())
        r = func(url, json=data) if data is not None else func(url)
        if r.status_code not in (200, 201, 204):
            error(f"Call failed: {method} {url} ({r.status_code}) {r.text}")
            sys.exit(1)
        if r.text and "application/json" in r.headers.get("Content-Type", ""):
            return r.json()
        return {}


def pick_target_disk(devices):
    """GET /api/storage/devices/system returns every Device (disks, partitions,
    filesystems, LVs...). Only entries with a non-null "drive" field are usable
    as a whole-disk install target."""
    disks = [d for d in devices if d.get("drive") is not None]
    if not disks:
        error("No usable disk found in /api/storage/devices/system response.")
        sys.exit(1)
    return disks[0]["deviceInfo"]["name"]


def run_demo(ip, password, sles_version):
    client = AgamaClient(ip, password)
    client.connect()

    devices = client.call("GET", "storage/devices/system")
    products = client.call("GET", "software/products")
    target_disk = pick_target_disk(devices)
    product_id = products[0]["id"] if products else "SLES"

    log(f"Target: {BOLD}{target_disk}{ENDC} | Product: {BOLD}{product_id}{ENDC}")

    print(f"\n{YELLOW}{BOLD}--- SLES {sles_version} AGAMA DEMO MENU ---{ENDC}")
    for key, p in PROFILES.items():
        print(f"{key}) {p['name']:<10} - {p['desc']}")

    choice = input("\nSelect profile [1-2]: ")
    profile = PROFILES.get(choice)
    if not profile:
        error("Invalid choice.")
        sys.exit(1)

    storage_config = profile["storage"]
    storage_config["drives"][0]["search"] = target_disk

    if profile["name"] == "atm-full" and sles_version == "16.0":
        warn("TPM2 auto-unlock (encryption.luks2.tpm) needs SLES 16.1+.")
        warn("Dropping 'tpm' on 16.0: disk will still be LUKS2-encrypted, "
             "but the passphrase must be entered manually at boot.")
        del storage_config["drives"][0]["partitions"][-1]["encryption"]["luks2"]["tpm"]

    log("Applying configuration...")
    client.call("PUT", "hostname/config", {"static": f"sles16-{profile['name']}"})
    client.call("PATCH", "users/root", {"password": password})

    # Agama rejects re-selecting a product that's already selected (e.g. the
    # single-product SLES ISO auto-selects it at boot), so only PUT this if
    # it would actually change something.
    current_software = client.call("GET", "software/config")
    if current_software.get("product") != product_id:
        client.call("PUT", "software/config", {"product": product_id})

    client.call("PUT", "storage/config", {"storage": storage_config})

    log("Re-probing so the new config is picked up before installing...")
    client.call("POST", "manager/probe_sync")

    input(f"\n{YELLOW}[!] Press ENTER to trigger INSTALL...{ENDC}")
    client.call("POST", "manager/install")

    while True:
        status = client.call("GET", "manager/installer")
        phase = PHASES.get(status.get("phase"), status.get("phase"))
        print(f"\rPhase: {phase:<10} busy={status.get('isBusy')}", end="")
        if phase == "Finish" and not status.get("isBusy"):
            break
        time.sleep(2)
    print()
    success("Installation finished.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Drive a running Agama installer over its HTTP API.")
    parser.add_argument("ip", help="IP address of the machine running the Agama live installer")
    parser.add_argument("password", help="root password to set (also used to authenticate)")
    parser.add_argument("--sles-version", choices=["16.0", "16.1"], default="16.1",
                         help="Target SLES release, used to gate 16.1-only API fields (default: 16.1)")
    args = parser.parse_args()
    run_demo(args.ip, args.password, args.sles_version)
