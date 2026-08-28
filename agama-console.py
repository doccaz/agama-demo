#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Interactive menu for driving a running Agama live installer over its HTTP
API, one atomic action at a time. Also offers a scripted end-to-end flow
(Storage -> "Run full scripted install") that loads a predefined profile,
probes, and installs without leaving the menu -- so this one script covers
both fine-grained API exploration and the canned demo flow.

Verified against the `SLE-16` branch of https://github.com/agama-project/agama
(the branch that ships in both SLES 16.0 and 16.1). There is no self-served
OpenAPI/Swagger endpoint on a running installer -- `aide` generates one, but
only via the `agama-web-server doc` CLI subcommand on the server host itself,
and this subcommand isn't even present in the SLES 16.0 GM build. The route
tables below were extracted directly from `agama-server/src/*/web.rs` on that
branch instead of guessed or reverse-engineered from traffic.

Endpoints actually exercised live in this session (see docstrings on the
functions below for the rest, pulled from source but not all hand-tested):

- POST /api/auth {"password": ...} -> {"token": ...}, also sets an
  "agamaToken" cookie (requests.Session carries it automatically). The same
  token also works as `Authorization: Bearer <token>` -- used here for the
  websocket connection, which doesn't get the cookie jar.
- GET /api/manager/installer -> {"phase": 0-3, "isBusy": bool, "canInstall": bool}
  (0=Startup, 1=Config, 2=Install, 3=Finish).
- POST /api/manager/probe_sync -- re-probes disks/software; can take well
  over 2 minutes on an encrypted disk, blocks until done.
- POST /api/manager/install -- only succeeds once canInstall is true.
- GET/PUT /api/storage/config (PUT needs the response's top-level "storage"
  wrapper, e.g. {"storage": {...}} -- a bare {"drives": [...]} silently
  becomes an empty D-Bus config and 400s).
- GET /api/questions -> pending questions (e.g. LUKS passphrase prompts
  raised by storage probing/re-probing an already-encrypted device); answer
  with PUT /api/questions/<id>/answer
  {"generic": {"answer": "decrypt"}, "password": {"password": ...}}.
  Re-probing an encrypted disk raises a NEW question each time (new id) --
  it doesn't remember a previous answer.

Reboot/halt/poweroff the target after install: POST /api/manager/finish with
a bare JSON string body -- "reboot" (default if body omitted), "halt",
"stop", or "poweroff" (agama-lib/src/manager.rs FinishMethod).

Network, l10n, users, software and storage sub-endpoints beyond the ones
above are wired into the menu from the source route tables but were NOT all
exercised against a live installer this session -- if one 404s or 400s on
your Agama version, fall back to the raw API call action to explore it live.

Usage:
    ./agama-console.py <installer-ip> [--password DemoSecurity2026!]
"""
import argparse
import json
import ssl
import sys
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    import websockets
    HAVE_WEBSOCKETS = True
except ImportError:
    HAVE_WEBSOCKETS = False

BLUE, GREEN, YELLOW, RED, BOLD, ENDC = (
    "\033[94m", "\033[92m", "\033[93m", "\033[91m", "\033[1m", "\033[0m"
)

PHASES = {0: "Startup", 1: "Config", 2: "Install", 3: "Finish"}

DEFAULT_PASSWORD = "DemoSecurity2026!"

STORAGE_PRESETS = {
    "atm-slim": {
        "desc": "Single Btrfs root, no encryption",
        "build": lambda disk: {
            "drives": [{"search": disk, "partitions": [{"filesystem": {"path": "/", "type": "btrfs"}}]}]
        },
    },
    "atm-full": {
        "desc": "/boot ext4 + Btrfs root on LUKS2, TPM2 auto-unlock (needs SLES 16.1+)",
        "build": lambda disk: {
            "drives": [{
                "search": disk,
                "partitions": [
                    {"filesystem": {"path": "/boot", "type": "ext4"}, "size": "1 GiB"},
                    {
                        "filesystem": {"path": "/", "type": "btrfs"},
                        "encryption": {"luks2": {"password": DEFAULT_PASSWORD, "tpm": True}},
                    },
                ],
            }]
        },
    },
    "atm-full-16.0": {
        "desc": "Same as atm-full but no TPM auto-unlock (SLES 16.0-compatible)",
        "build": lambda disk: {
            "drives": [{
                "search": disk,
                "partitions": [
                    {"filesystem": {"path": "/boot", "type": "ext4"}, "size": "1 GiB"},
                    {
                        "filesystem": {"path": "/", "type": "btrfs"},
                        "encryption": {"luks2": {"password": DEFAULT_PASSWORD}},
                    },
                ],
            }]
        },
    },
}


def log(msg):
    print(f"{BLUE}[*]{ENDC} {msg}")


def success(msg):
    print(f"{GREEN}[+]{ENDC} {msg}")


def warn(msg):
    print(f"{YELLOW}[!]{ENDC} {msg}")


def error(msg):
    print(f"{RED}[!] Error: {msg}{ENDC}")


def pretty(obj):
    print(json.dumps(obj, indent=2))


def prompt_json(label):
    raw = input(f"{label} (JSON, blank for none): ").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        error(f"Invalid JSON: {exc}")
        return None


class AgamaClient:
    def __init__(self, ip, password):
        self.ip = ip
        self.password = password
        self.session = requests.Session()
        self.session.verify = False
        self.api_root = f"https://{ip}/api"
        self.authenticated = False
        self.token = None

    def authenticate(self):
        r = self.session.post(f"{self.api_root}/auth", json={"password": self.password})
        if r.status_code not in (200, 201, 204):
            error(f"Auth failed: {r.status_code} {r.text}")
            self.authenticated = False
            return False
        self.authenticated = True
        try:
            self.token = r.json().get("token")
        except ValueError:
            self.token = None
        success(f"Authenticated against {self.ip}.")
        return True

    def call(self, method, endpoint, data=None, timeout=180):
        """Never exits the process -- returns (ok, status_code, body) so the
        menu loop can report a failure and keep running."""
        url = f"{self.api_root}/{endpoint}"
        func = getattr(self.session, method.lower())
        try:
            r = func(url, json=data, timeout=timeout) if data is not None else func(url, timeout=timeout)
        except requests.RequestException as exc:
            error(f"{method} {endpoint}: {exc}")
            return False, None, None
        body = None
        if r.text:
            try:
                body = r.json()
            except ValueError:
                body = r.text
        ok = r.status_code in (200, 201, 204)
        if not ok:
            error(f"{method} {endpoint} -> {r.status_code}: {body}")
        return ok, r.status_code, body

    def get(self, endpoint, show=True):
        ok, _, body = self.call("GET", endpoint)
        if ok and show:
            pretty(body)
        return ok, body


# --- generic helpers used across categories ---------------------------------

def list_disks(client, show=True):
    ok, devices = client.get("storage/devices/system", show=False)
    if not ok:
        return []
    disks = [d for d in devices if d.get("drive") is not None]
    if show:
        for d in disks:
            info = d.get("deviceInfo", {})
            print(f"  {info.get('name')}  size={d.get('size')}")
    return [d["deviceInfo"]["name"] for d in disks]


def pick_disk(client):
    disks = list_disks(client)
    if not disks:
        error("No usable disk found.")
        return None
    return disks[0] if len(disks) == 1 else input(f"Target disk {disks}: ").strip()


# --- Manager -----------------------------------------------------------------

def mgr_status(client):
    ok, body = client.get("manager/installer", show=False)
    if not ok:
        return
    phase = PHASES.get(body.get("phase"), body.get("phase"))
    print(f"Phase: {BOLD}{phase}{ENDC}  isBusy={body.get('isBusy')}  canInstall={body.get('canInstall')}")
    ok, questions = client.get("questions", show=False)
    if ok and questions:
        warn(f"{len(questions)} pending question(s) -- see Questions menu.")


def mgr_probe(client):
    log("Probing (async, returns immediately)...")
    ok, _, _ = client.call("POST", "manager/probe")
    if ok:
        success("Probe requested.")


def mgr_probe_sync(client):
    log("Probing (sync, can take a couple of minutes on an encrypted disk)...")
    ok, _, _ = client.call("POST", "manager/probe_sync", timeout=300)
    if ok:
        success("Probe complete.")


def mgr_reprobe_sync(client):
    log("Re-probing (sync)...")
    ok, _, _ = client.call("POST", "manager/reprobe_sync", timeout=300)
    if ok:
        success("Reprobe complete.")


def mgr_trigger_install(client):
    ok, status = client.get("manager/installer", show=False)
    if ok and not status.get("canInstall"):
        warn("canInstall is false -- probe/config may be incomplete.")
    if input(f"{YELLOW}Trigger install now? [y/N]{ENDC} ").strip().lower() != "y":
        log("Cancelled.")
        return
    ok, _, _ = client.call("POST", "manager/install")
    if ok:
        success("Install triggered.")


def mgr_finish(client):
    """POST /api/manager/finish {method}. method is one of halt/reboot/stop/
    poweroff (bare JSON string, not an object) -- default is reboot."""
    print("  1) reboot (default)\n  2) halt\n  3) stop (do nothing)\n  4) poweroff")
    choice = input("Method [1-4, default 1]: ").strip() or "1"
    method = {"1": "reboot", "2": "halt", "3": "stop", "4": "poweroff"}.get(choice)
    if not method:
        error("Invalid choice.")
        return
    if input(f"{YELLOW}Confirm '{method}' the target system now? [y/N]{ENDC} ").strip().lower() != "y":
        log("Cancelled.")
        return
    ok, _, _ = client.call("POST", "manager/finish", method)
    if ok:
        success(f"'{method}' issued.")


def mgr_watch_progress(client):
    log("Polling every 2s. Ctrl-C to stop watching (install keeps running).")
    try:
        while True:
            ok, status = client.get("manager/installer", show=False)
            if not ok:
                return
            phase = PHASES.get(status.get("phase"), status.get("phase"))
            print(f"\rPhase: {phase:<10} busy={status.get('isBusy')}   ", end="", flush=True)
            ok, questions = client.get("questions", show=False)
            if ok and questions:
                print()
                warn(f"{len(questions)} pending question(s) -- use Questions menu to unblock.")
            if phase == "Finish" and not status.get("isBusy"):
                print()
                success("Installation finished.")
                return
            time.sleep(2)
    except KeyboardInterrupt:
        print()
        log("Stopped watching.")


def mgr_logs_list(client):
    client.get("manager/logs/list")


def mgr_logs_download(client):
    """GET /api/manager/logs/store streams a gzip tarball (Content-Disposition:
    attachment; filename="agama-logs.tar.gz"). Not JSON, so this bypasses
    client.call() and streams the response body straight to disk."""
    path = input("Save logs to [agama-logs.tar.gz]: ").strip() or "agama-logs.tar.gz"
    url = f"{client.api_root}/manager/logs/store"
    try:
        r = client.session.get(url, stream=True, timeout=120)
    except requests.RequestException as exc:
        error(str(exc))
        return
    if r.status_code != 200:
        error(f"GET manager/logs/store -> {r.status_code}: {r.text[:300]}")
        return
    size = 0
    with open(path, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)
            size += len(chunk)
    success(f"Logs saved to {path} ({size} bytes).")


MANAGER_MENU = [
    ("Show installer status", mgr_status),
    ("Probe (async)", mgr_probe),
    ("Probe (sync, blocks)", mgr_probe_sync),
    ("Reprobe (sync, blocks)", mgr_reprobe_sync),
    ("Trigger install", mgr_trigger_install),
    ("Watch install progress", mgr_watch_progress),
    ("Finish install: reboot/halt/stop/poweroff target", mgr_finish),
    ("List available logs", mgr_logs_list),
    ("Download logs (tar.gz)", mgr_logs_download),
]


# --- Software ------------------------------------------------------------

def sw_list_products(client):
    ok, products = client.get("software/products", show=False)
    if not ok:
        return
    for p in products:
        print(f"  {p.get('id')}: {p.get('name', '')}")


def sw_select_product(client):
    ok, products = client.get("software/products", show=False)
    if not ok or not products:
        return
    for p in products:
        print(f"  {p.get('id')}: {p.get('name', '')}")
    product_id = input("Product id to select: ").strip()
    if not product_id:
        return
    ok, current = client.get("software/config", show=False)
    if ok and current.get("product") == product_id:
        warn("Product already selected -- PUT skipped (Agama 400s on a no-op reselect).")
        return
    ok, _, _ = client.call("PUT", "software/config", {"product": product_id})
    if ok:
        success(f"Product set to {product_id}.")


def sw_show_config(client):
    client.get("software/config")


def sw_patterns(client):
    client.get("software/patterns")


def sw_repositories(client):
    client.get("software/repositories")


def sw_licenses(client):
    client.get("software/licenses")


def sw_proposal(client):
    client.get("software/proposal")


def sw_probe(client):
    log("Probing software (can take a while)...")
    ok, _, _ = client.call("POST", "software/probe", timeout=300)
    if ok:
        success("Software probe complete.")


def sw_registration(client):
    client.get("software/registration")


def sw_add_repository(client):
    """PUT /api/software/config {"extraRepositories": [RepositoryParams]}
    adds/overrides custom repos (alias/url required, name/productDir/enabled
    optional). This replaces the whole extraRepositories list, so existing
    custom repos are re-read first and the new one appended."""
    ok, current = client.get("software/config", show=False)
    existing = (current.get("extraRepositories") or []) if ok else []

    alias = input("Repo alias (unique id): ").strip()
    url = input("Repo URL: ").strip()
    if not alias or not url:
        error("alias and url are required.")
        return
    name = input("Repo display name (blank = alias): ").strip()
    enabled = input("Enabled? [Y/n]: ").strip().lower() != "n"

    repo = {"alias": alias, "url": url, "enabled": enabled}
    if name:
        repo["name"] = name

    body = {"extraRepositories": existing + [repo]}
    ok, _, _ = client.call("PUT", "software/config", body)
    if ok:
        success(f"Repository '{alias}' added ({len(existing) + 1} custom repo(s) total).")


def sw_remove_repositories(client):
    if input(f"{YELLOW}Clear ALL custom (extraRepositories) repos? [y/N]{ENDC} ").strip().lower() != "y":
        return
    ok, _, _ = client.call("PUT", "software/config", {"extraRepositories": []})
    if ok:
        success("Custom repositories cleared.")


SOFTWARE_MENU = [
    ("List products", sw_list_products),
    ("Select product", sw_select_product),
    ("Show software config", sw_show_config),
    ("Show patterns", sw_patterns),
    ("Show all active repositories", sw_repositories),
    ("Add a custom repository", sw_add_repository),
    ("Clear custom repositories", sw_remove_repositories),
    ("Show licenses", sw_licenses),
    ("Show proposal", sw_proposal),
    ("Probe software", sw_probe),
    ("Show registration status", sw_registration),
]


# --- Storage -------------------------------------------------------------

def st_list_disks(client):
    list_disks(client)


def st_show_config(client):
    client.get("storage/config")


def st_load_preset(client):
    names = list(STORAGE_PRESETS)
    for i, name in enumerate(names, 1):
        print(f"  {i}) {name} - {STORAGE_PRESETS[name]['desc']}")
    choice = input("Preset [number]: ").strip()
    try:
        preset = STORAGE_PRESETS[names[int(choice) - 1]]
    except (ValueError, IndexError, KeyError):
        error("Invalid choice.")
        return
    disk = pick_disk(client)
    if not disk:
        return
    ok, _, _ = client.call("PUT", "storage/config", {"storage": preset["build"](disk)})
    if ok:
        success(f"Storage layout applied against {disk}.")


def st_load_full_profile(client):
    """Bundles hostname + root password + product + storage, mirroring
    profiles/*.jsonnet -- the 'apply this whole profile now' action."""
    names = list(STORAGE_PRESETS)
    for i, name in enumerate(names, 1):
        print(f"  {i}) {name} - {STORAGE_PRESETS[name]['desc']}")
    choice = input("Profile [number]: ").strip()
    try:
        name = names[int(choice) - 1]
    except (ValueError, IndexError):
        error("Invalid choice.")
        return
    preset = STORAGE_PRESETS[name]

    disk = pick_disk(client)
    if not disk:
        return

    ok, products = client.get("software/products", show=False)
    product_id = products[0]["id"] if ok and products else "SLES"

    log(f"Applying profile '{name}' (hostname, root password, product={product_id}, storage on {disk})...")
    client.call("PUT", "hostname/config", {"static": f"sles16-{name}"})
    client.call("PATCH", "users/root", {"password": DEFAULT_PASSWORD})
    ok, current = client.get("software/config", show=False)
    if ok and current.get("product") != product_id:
        client.call("PUT", "software/config", {"product": product_id})
    ok, _, _ = client.call("PUT", "storage/config", {"storage": preset["build"](disk)})
    if ok:
        success(f"Profile '{name}' applied.")


def st_run_scripted_install(client):
    """End-to-end scripted flow: load a full profile, probe_sync, confirm,
    trigger install, then watch to Finish. This is agama-demo.py's old
    run_demo() sequence, folded in here so profile-driven installs and
    atomic API exploration live in one script."""
    names = list(STORAGE_PRESETS)
    for i, name in enumerate(names, 1):
        print(f"  {i}) {name} - {STORAGE_PRESETS[name]['desc']}")
    choice = input("Profile [number]: ").strip()
    try:
        name = names[int(choice) - 1]
    except (ValueError, IndexError):
        error("Invalid choice.")
        return
    preset = STORAGE_PRESETS[name]

    disk = pick_disk(client)
    if not disk:
        return

    ok, products = client.get("software/products", show=False)
    product_id = products[0]["id"] if ok and products else "SLES"

    log(f"Applying profile '{name}' (hostname, root password, product={product_id}, storage on {disk})...")
    client.call("PUT", "hostname/config", {"static": f"sles16-{name}"})
    client.call("PATCH", "users/root", {"password": DEFAULT_PASSWORD})
    ok, current = client.get("software/config", show=False)
    if ok and current.get("product") != product_id:
        client.call("PUT", "software/config", {"product": product_id})
    ok, _, _ = client.call("PUT", "storage/config", {"storage": preset["build"](disk)})
    if not ok:
        return
    success(f"Profile '{name}' applied.")

    log("Re-probing so the new config is picked up before installing...")
    ok, _, _ = client.call("POST", "manager/probe_sync", timeout=300)
    if not ok:
        return
    success("Probe complete.")

    ok, status = client.get("manager/installer", show=False)
    if ok and not status.get("canInstall"):
        warn("canInstall is false -- probe/config may be incomplete.")
    if input(f"{YELLOW}Trigger install now? [y/N]{ENDC} ").strip().lower() != "y":
        log("Cancelled.")
        return
    ok, _, _ = client.call("POST", "manager/install")
    if not ok:
        return
    success("Install triggered.")

    mgr_watch_progress(client)


def st_probe(client):
    log("Probing storage...")
    ok, _, _ = client.call("POST", "storage/probe", timeout=300)
    if ok:
        success("Storage probe complete.")


def st_reprobe(client):
    log("Re-probing storage...")
    ok, _, _ = client.call("POST", "storage/reprobe", timeout=300)
    if ok:
        success("Storage reprobe complete.")


def st_reactivate(client):
    ok, _, _ = client.call("POST", "storage/reactivate")
    if ok:
        success("Storage reactivated.")


def st_devices_system(client):
    client.get("storage/devices/system")


def st_devices_actions(client):
    client.get("storage/devices/actions")


def st_candidate_drives(client):
    client.get("storage/devices/candidate_drives")


STORAGE_MENU = [
    ("List disks", st_list_disks),
    ("Show storage config", st_show_config),
    ("Load storage layout preset (on-the-fly)", st_load_preset),
    ("Load full profile (hostname+root+product+storage)", st_load_full_profile),
    ("Run full scripted install (profile -> probe -> install -> wait for finish)", st_run_scripted_install),
    ("Probe storage", st_probe),
    ("Reprobe storage", st_reprobe),
    ("Reactivate storage", st_reactivate),
    ("Show all system devices (raw)", st_devices_system),
    ("Show proposed storage actions", st_devices_actions),
    ("Show candidate drives", st_candidate_drives),
]


# --- Network ---------------------------------------------------------------
# Verified against SLE-16 branch source (agama-server/src/network/web.rs),
# NOT hand-tested against a live installer this session -- fall back to the
# raw API call action if any of these don't match your Agama version.

def net_state(client):
    client.get("network/state")


def net_set_state(client):
    body = prompt_json("New state e.g. {\"wirelessEnabled\": false}")
    if body is None:
        return
    ok, _, _ = client.call("PUT", "network/state", body)
    if ok:
        success("Network state updated.")


def net_connections(client):
    client.get("network/connections")


def net_connection_by_id(client):
    cid = input("Connection id: ").strip()
    if cid:
        client.get(f"network/connections/{cid}")


def net_connect_disconnect(client):
    cid = input("Connection id: ").strip()
    if not cid:
        return
    action = input("connect/disconnect: ").strip().lower()
    if action not in ("connect", "disconnect"):
        error("Must be 'connect' or 'disconnect'.")
        return
    ok, _, _ = client.call("POST", f"network/connections/{cid}/{action}")
    if ok:
        success(f"{action} requested for {cid}.")


def net_devices(client):
    client.get("network/devices")


def net_wifi(client):
    client.get("network/wifi")


def net_apply(client):
    ok, _, _ = client.call("POST", "network/system/apply")
    if ok:
        success("Network config applied to the system.")


NETWORK_MENU = [
    ("Show network state", net_state),
    ("Set network state (raw JSON)", net_set_state),
    ("List connections", net_connections),
    ("Show connection by id", net_connection_by_id),
    ("Connect/disconnect a connection", net_connect_disconnect),
    ("List devices", net_devices),
    ("List wifi networks", net_wifi),
    ("Apply network config to the system", net_apply),
]


# --- Localization (l10n) ----------------------------------------------------

def l10n_keymaps(client):
    client.get("l10n/keymaps")


def l10n_locales(client):
    client.get("l10n/locales")


def l10n_timezones(client):
    client.get("l10n/timezones")


def l10n_show_config(client):
    client.get("l10n/config")


def l10n_set_config(client):
    locale = input("locales, comma-separated (blank to skip): ").strip()
    keymap = input("keymap (blank to skip): ").strip()
    timezone = input("timezone (blank to skip): ").strip()
    body = {}
    if locale:
        body["locales"] = [s.strip() for s in locale.split(",")]
    if keymap:
        body["keymap"] = keymap
    if timezone:
        body["timezone"] = timezone
    if not body:
        log("Nothing to set.")
        return
    ok, _, _ = client.call("PATCH", "l10n/config", body)
    if ok:
        success("l10n config updated.")


L10N_MENU = [
    ("List keymaps", l10n_keymaps),
    ("List locales", l10n_locales),
    ("List timezones", l10n_timezones),
    ("Show l10n config", l10n_show_config),
    ("Set l10n config (locale/keymap/timezone)", l10n_set_config),
]


# --- Users -------------------------------------------------------------

def users_root_show(client):
    client.get("users/root")


def users_root_set(client):
    pw = input(f"Root password [{DEFAULT_PASSWORD}]: ").strip() or DEFAULT_PASSWORD
    ok, _, _ = client.call("PATCH", "users/root", {"password": pw})
    if ok:
        success("Root password set.")


def users_first_show(client):
    client.get("users/first")


def users_first_set(client):
    full_name = input("Full name: ").strip()
    user_name = input("Username: ").strip()
    pw = input(f"Password [{DEFAULT_PASSWORD}]: ").strip() or DEFAULT_PASSWORD
    if not user_name:
        error("Username is required.")
        return
    body = {"fullName": full_name, "userName": user_name, "password": pw, "hashedPassword": False}
    ok, _, _ = client.call("PUT", "users/first", body)
    if ok:
        success(f"First user '{user_name}' set.")


def users_first_remove(client):
    if input(f"{YELLOW}Remove the first (non-root) user? [y/N]{ENDC} ").strip().lower() != "y":
        return
    ok, _, _ = client.call("DELETE", "users/first")
    if ok:
        success("First user removed.")


def users_password_check(client):
    pw = input("Password to check: ").strip()
    ok, _, body = client.call("POST", "users/password_check", {"password": pw})
    if ok:
        pretty(body)


USERS_MENU = [
    ("Show root config", users_root_show),
    ("Set root password", users_root_set),
    ("Show first-user config", users_first_show),
    ("Set first (non-root) user", users_first_set),
    ("Remove first user", users_first_remove),
    ("Check password strength/validity", users_password_check),
]


# --- Questions ---------------------------------------------------------

def q_list(client):
    ok, questions = client.get("questions", show=False)
    if not ok:
        return []
    if not questions:
        log("No pending questions.")
    for q in questions:
        g = q["generic"]
        print(f"  id={g['id']} [{g['class']}] {g['text']} options={g['options']} default={g.get('defaultOption')}")
    return questions


def q_answer(client):
    questions = q_list(client)
    if not questions:
        return
    qid = input("Question id to answer: ").strip()
    answer = input("Answer (e.g. decrypt/skip): ").strip()
    pw = input(f"Password (blank if not needed) [{DEFAULT_PASSWORD}]: ")
    payload = {"generic": {"answer": answer}}
    if pw or any(str(q["generic"]["id"]) == qid and q.get("withPassword") is not None for q in questions):
        payload["password"] = {"password": pw or DEFAULT_PASSWORD}
    ok, _, _ = client.call("PUT", f"questions/{qid}/answer", payload)
    if ok:
        success(f"Question {qid} answered.")


def q_set_policy(client):
    """QuestionsConfig{policy, answers} -- policy is 'auto' or 'user'.
    'auto' makes Agama answer with each question's defaultOption instead of
    blocking, which is handy to avoid the LUKS re-probe question loop during
    an unattended run."""
    print("  1) auto  (auto-answer with each question's default option)")
    print("  2) user  (block and wait for an explicit answer, the default)")
    choice = input("Policy [1-2]: ").strip()
    policy = {"1": "auto", "2": "user"}.get(choice)
    if not policy:
        error("Invalid choice.")
        return
    ok, _, _ = client.call("PUT", "questions/config", {"policy": policy})
    if ok:
        success(f"Questions policy set to '{policy}'.")


QUESTIONS_MENU = [
    ("List pending questions", q_list),
    ("Answer a question", q_answer),
    ("Set questions policy (auto/user)", q_set_policy),
]


# --- Hostname ------------------------------------------------------------

def hn_show(client):
    client.get("hostname/config")


def hn_set(client):
    name = input("Static hostname: ").strip()
    if not name:
        return
    ok, _, _ = client.call("PUT", "hostname/config", {"static": name})
    if ok:
        success(f"Hostname set to {name}.")


HOSTNAME_MENU = [
    ("Show hostname config", hn_show),
    ("Set hostname", hn_set),
]


# --- Scripts (real command execution on the target) -------------------------
# POST /api/scripts adds a Script{type, name, content|url, chroot?}; POST
# /api/scripts/run{group} executes every added script of that group and
# writes <name>.log/.err/.out under /run/agama/scripts/<group>/ on the
# machine running the live installer. Verified live this session: a "pre"
# script's stdout/stderr/exit status showed up exactly where documented.
#
# Groups (agama_lib::scripts::ScriptsGroup, camelCase over the wire):
#   pre               -- runs before the installation starts, in the live env
#   postPartitioning  -- runs right after partitioning, in the live env
#   post              -- runs after install finishes; chroot'd into /mnt
#                        (the target system) unless chroot=false
#   init              -- written out but NOT run by /run; it runs on first
#                        boot of the installed target instead
#
# This is genuine command execution against whatever the API reaches (the
# live installer environment for pre/postPartitioning, or the installed
# target's root for post/init) -- treat it with the same care as SSH access.

SCRIPT_GROUPS = {"1": "pre", "2": "postPartitioning", "3": "post", "4": "init"}


def scripts_list(client):
    client.get("scripts")


def scripts_add(client):
    print("  1) pre               - before install starts (runs in the live env)")
    print("  2) postPartitioning  - right after partitioning (live env)")
    print("  3) post              - after install finishes (chroot'd into target by default)")
    print("  4) init              - written now, runs on first boot of the target instead")
    choice = input("Script group [1-4]: ").strip()
    group = SCRIPT_GROUPS.get(choice)
    if not group:
        error("Invalid choice.")
        return
    name = input("Script name: ").strip()
    if not name:
        error("Name is required.")
        return
    print("Script content -- paste/type it, end with a single '.' on its own line:")
    lines = []
    while True:
        line = input()
        if line == ".":
            break
        lines.append(line)
    content = "\n".join(lines) + "\n"
    body = {"type": group, "name": name, "content": content}
    if group == "post":
        chroot = input("Run chrooted into the installed target? [Y/n]: ").strip().lower()
        body["chroot"] = chroot != "n"
    ok, _, _ = client.call("POST", "scripts", body)
    if ok:
        success(f"Script '{name}' added to group '{group}'.")


def scripts_adhoc(client):
    """Convenience wrapper: there's no dedicated 'run one command' endpoint in
    the Agama API -- /api/scripts is the only command-execution primitive, and
    it's file-based (add a script, then run its whole group). This collapses
    add+run into one step for a single command. There's also no API endpoint
    to read stdout back over HTTP -- only via the filesystem (SSH/console),
    same as any other script; the path is printed below."""
    print("  1) pre               - runs now, in the live installer env")
    print("  2) postPartitioning  - runs now, in the live installer env")
    print("  3) post              - runs now, chroot'd into the installed target by default")
    choice = input("Where to run it [1-3]: ").strip()
    group = {"1": "pre", "2": "postPartitioning", "3": "post"}.get(choice)
    if not group:
        error("Invalid choice.")
        return
    command = input("Command to run: ").strip()
    if not command:
        return
    name = f"adhoc-{int(time.time())}"
    body = {"type": group, "name": name, "content": f"#!/bin/bash\n{command}\n"}
    if group == "post":
        chroot = input("Run chrooted into the installed target? [Y/n]: ").strip().lower()
        body["chroot"] = chroot != "n"
    if input(f"{YELLOW}Run '{command}' now? [y/N]{ENDC} ").strip().lower() != "y":
        log("Cancelled.")
        return
    ok, _, _ = client.call("POST", "scripts", body)
    if not ok:
        return
    ok, _, _ = client.call("POST", "scripts/run", group)
    if ok:
        success(f"Ran. Output at /run/agama/scripts/{group}/{name}.{{log,err,out}} on the target "
                "(fetch via SSH/console -- the API has no stdout read-back endpoint).")


def scripts_run(client):
    print("  1) pre  2) postPartitioning  3) post  4) init (won't actually run -- see note above)")
    choice = input("Group to run [1-4]: ").strip()
    group = SCRIPT_GROUPS.get(choice)
    if not group:
        error("Invalid choice.")
        return
    if input(f"{YELLOW}Run all '{group}' scripts now? This executes real commands. [y/N]{ENDC} ").strip().lower() != "y":
        log("Cancelled.")
        return
    ok, _, _ = client.call("POST", "scripts/run", group)
    if ok:
        success(f"Ran scripts in group '{group}' (check .log/.err/.out under /run/agama/scripts/{group}/ on the target).")


def scripts_clear(client):
    if input(f"{YELLOW}Remove ALL defined scripts (every group)? [y/N]{ENDC} ").strip().lower() != "y":
        return
    ok, _, _ = client.call("DELETE", "scripts")
    if ok:
        success("All scripts removed.")


SCRIPTS_MENU = [
    ("List defined scripts", scripts_list),
    ("Run one ad-hoc command now", scripts_adhoc),
    ("Add a script", scripts_add),
    ("Run a script group (executes commands)", scripts_run),
    ("Remove all scripts", scripts_clear),
]


# --- Files (deploy content or download-to-target onto the installed target) -
# PUT /api/files/ queues a list of UserFile{content|url, destination,
# permissions, user, group}; POST /api/files/write actually writes them --
# always chroot'd, into /mnt/<destination> on the target (agama-lib/src/
# files/model.rs: UserFile::write() hardcodes "chroot /mnt install -d ..."
# then "chroot /mnt chown ..."). This only makes sense once /mnt holds a real
# target (same timing constraint as "post" scripts). FileSource is untagged
# content-or-url, so a "url" source makes Agama itself fetch and write it --
# effectively a download-to-target primitive. There is no read-back: like
# scripts, this is push-only.

def files_list(client):
    client.get("files")


def files_add(client):
    ok, existing = client.get("files", show=False)
    existing = existing if ok and isinstance(existing, list) else []

    destination = input("Destination path on target (e.g. /etc/myapp/config.conf): ").strip()
    if not destination:
        error("Destination is required.")
        return
    source_kind = input("Source: (c)ontent or (u)rl? [c]: ").strip().lower() or "c"
    if source_kind == "u":
        url = input("Source URL: ").strip()
        if not url:
            error("URL is required.")
            return
        entry = {"url": url}
    else:
        print("File content -- paste/type it, end with a single '.' on its own line:")
        lines = []
        while True:
            line = input()
            if line == ".":
                break
            lines.append(line)
        entry = {"content": "\n".join(lines) + "\n"}

    permissions = input("Permissions (octal) [0644]: ").strip() or "0644"
    user = input("Owner user [root]: ").strip() or "root"
    group = input("Owner group [root]: ").strip() or "root"
    entry.update({"destination": destination, "permissions": permissions, "user": user, "group": group})

    ok, _, _ = client.call("PUT", "files", existing + [entry])
    if ok:
        success(f"Queued '{destination}' ({len(existing) + 1} file(s) total). Use 'Write queued files' to deploy.")


def files_write(client):
    """Actually performs the chroot'd write -- real filesystem changes on the
    target, same caution level as running a 'post' script."""
    ok, queued = client.get("files", show=False)
    if ok and queued:
        for f in queued:
            print(f"  -> {f.get('destination')}")
    if input(f"{YELLOW}Write all queued files to the target now? [y/N]{ENDC} ").strip().lower() != "y":
        log("Cancelled.")
        return
    ok, _, _ = client.call("POST", "files/write")
    if ok:
        success("Files written to the target.")


def files_clear(client):
    if input(f"{YELLOW}Clear the queued files list? [y/N]{ENDC} ").strip().lower() != "y":
        return
    ok, _, _ = client.call("PUT", "files", [])
    if ok:
        success("Queued files cleared.")


FILES_MENU = [
    ("List queued files", files_list),
    ("Add a file (content or download-from-URL)", files_add),
    ("Write queued files to the target (executes now)", files_write),
    ("Clear queued files", files_clear),
]


# --- Live event stream (websocket) -----------------------------------------

async def _watch_events_async(client):
    uri = f"wss://{client.ip}/api/ws"
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    headers = {"Authorization": f"Bearer {client.token}"}
    log(f"Connecting to {uri} (Ctrl-C to stop)...")
    async with websockets.connect(uri, ssl=ssl_ctx, additional_headers=headers) as ws:
        async for message in ws:
            try:
                pretty(json.loads(message))
            except json.JSONDecodeError:
                print(message)


def watch_events(client):
    if not HAVE_WEBSOCKETS:
        error("The 'websockets' package isn't installed -- run: pip install websockets")
        return
    if not client.token:
        error("No auth token cached -- re-authenticate first.")
        return
    import asyncio
    try:
        asyncio.run(_watch_events_async(client))
    except KeyboardInterrupt:
        print()
        log("Stopped watching events.")
    except Exception as exc:  # noqa: BLE001 -- surfacing any websocket error to the demo presenter
        error(f"Websocket error: {exc}")


# --- Filtered package-install progress (websocket) --------------------------
# There is no polling-friendly "current package" field on GET
# /api/manager/installer -- it only reports coarse phase/isBusy/canInstall.
# Per-package detail (e.g. "Installing curl", step 698/742) only exists as
# ProgressChanged events on the /ws stream, one per D-Bus service
# (.../Manager1, .../Storage1, .../Software1). This filters that stream down
# to a single updating progress line instead of the raw event dump.

def _progress_bar(current, maximum, width=30):
    if not maximum:
        return "?" * width
    filled = int(width * current / maximum)
    return "#" * filled + "-" * (width - filled)


async def _watch_package_progress_async(client):
    uri = f"wss://{client.ip}/api/ws"
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    headers = {"Authorization": f"Bearer {client.token}"}
    log(f"Connecting to {uri} (Ctrl-C to stop)...")
    async with websockets.connect(uri, ssl=ssl_ctx, additional_headers=headers) as ws:
        async for raw in ws:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "InstallationPhaseChanged":
                phase = PHASES.get(event.get("phase"), event.get("phase"))
                print(f"\n{BOLD}{YELLOW}=== Phase: {phase} ==={ENDC}")
            elif etype == "ProgressChanged":
                service = (event.get("path") or "").rsplit("/", 1)[-1] or "?"
                current = event.get("currentStep", 0)
                maximum = event.get("maxSteps", 0)
                title = event.get("currentTitle") or ""
                if event.get("finished"):
                    print(f"\r[{service}] {GREEN}done{ENDC}" + " " * 60)
                else:
                    bar = _progress_bar(current, maximum)
                    print(f"\r[{service}] [{bar}] {current}/{maximum} {title:<50}", end="", flush=True)
            elif etype == "IssuesChanged" and event.get("issues"):
                print(f"\n{YELLOW}[!] Issues on {event.get('path')}: {event.get('issues')}{ENDC}")


def watch_package_progress(client):
    if not HAVE_WEBSOCKETS:
        error("The 'websockets' package isn't installed -- run: pip install websockets")
        return
    if not client.token:
        error("No auth token cached -- re-authenticate first.")
        return
    import asyncio
    try:
        asyncio.run(_watch_package_progress_async(client))
    except KeyboardInterrupt:
        print()
        log("Stopped watching progress.")
    except Exception as exc:  # noqa: BLE001 -- surfacing any websocket error to the demo presenter
        error(f"Websocket error: {exc}")


# --- Raw API call ------------------------------------------------------

def raw_call(client):
    method = input("Method [GET/POST/PUT/PATCH/DELETE]: ").strip().upper() or "GET"
    endpoint = input("Endpoint (relative to /api/, e.g. network/config): ").strip().lstrip("/")
    if not endpoint:
        return
    data = None
    if method in ("POST", "PUT", "PATCH"):
        raw = input("JSON body (blank for none): ").strip()
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                error(f"Invalid JSON: {exc}")
                return
    ok, status, body = client.call(method, endpoint, data)
    print(f"status: {status}")
    if body is not None:
        pretty(body) if isinstance(body, (dict, list)) else print(body)


def action_reauth(client):
    client.authenticate()


# --- top-level menu ----------------------------------------------------

CATEGORIES = [
    ("Manager (status/probe/install/finish)", MANAGER_MENU),
    ("Software", SOFTWARE_MENU),
    ("Storage", STORAGE_MENU),
    ("Network", NETWORK_MENU),
    ("Localization (l10n)", L10N_MENU),
    ("Users", USERS_MENU),
    ("Questions", QUESTIONS_MENU),
    ("Hostname", HOSTNAME_MENU),
    ("Scripts (executes real commands on the target)", SCRIPTS_MENU),
    ("Files (deploy content or download-to-target)", FILES_MENU),
]


def run_submenu(client, title, items):
    while True:
        print(f"\n{BOLD}{YELLOW}--- {title} ---{ENDC}")
        for i, (label, _) in enumerate(items, 1):
            print(f"  {i:2}) {label}")
        print("   0) Back")
        choice = input("\n> ").strip()
        if choice == "0":
            return
        try:
            idx = int(choice) - 1
            if idx < 0:
                raise ValueError
            _, func = items[idx]
        except (ValueError, IndexError):
            error("Invalid choice.")
            continue
        try:
            func(client)
        except KeyboardInterrupt:
            print()
            warn("Interrupted.")


def main():
    parser = argparse.ArgumentParser(description="Interactive menu for the Agama HTTP API.")
    parser.add_argument("ip", help="IP address of the machine running the Agama live installer")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="root/auth password (default: %(default)s)")
    args = parser.parse_args()

    client = AgamaClient(args.ip, args.password)
    if not client.authenticate():
        sys.exit(1)

    top_level_extra = [
        ("Watch live event stream (websocket, raw)", watch_events),
        ("Watch package install progress (filtered)", watch_package_progress),
        ("Re-authenticate", action_reauth),
        ("Raw API call (any endpoint)", raw_call),
    ]

    while True:
        print(f"\n{BOLD}{YELLOW}=== Agama API console: {args.ip} ==={ENDC}")
        n = 0
        for i, (label, _) in enumerate(CATEGORIES, 1):
            print(f"  {i:2}) {label}")
            n = i
        for j, (label, _) in enumerate(top_level_extra, n + 1):
            print(f"  {j:2}) {label}")
            n = j
        print("   0) Exit")
        choice = input("\n> ").strip()
        if choice == "0":
            break
        try:
            idx = int(choice) - 1
            if idx < 0:
                raise ValueError
        except ValueError:
            error("Invalid choice.")
            continue
        if idx < len(CATEGORIES):
            title, items = CATEGORIES[idx]
            run_submenu(client, title, items)
        elif idx < len(CATEGORIES) + len(top_level_extra):
            _, func = top_level_extra[idx - len(CATEGORIES)]
            try:
                func(client)
            except KeyboardInterrupt:
                print()
                warn("Interrupted.")
        else:
            error("Invalid choice.")


if __name__ == "__main__":
    main()
