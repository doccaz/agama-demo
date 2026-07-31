# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Unattended SLES 16 install demo using [Agama](https://github.com/agama-project/agama), driven
end-to-end in a local QEMU VM. Boots the installer ISO with `inst.auto=` pointing at a canned
jsonnet profile, or drives an already-booted installer live over its HTTP API.

## Commands

```bash
./launch-demo.sh [--manual] [atm-slim|atm-full|atm-full-16.0] [iso-path]
```
One-command unattended install: starts `swtpm`, serves `profiles/*.jsonnet` over HTTP, extracts
kernel/initrd from the ISO (cached in `vm/cache/`), creates `vm/agama-demo.qcow2` if missing, boots
QEMU. `--manual` appends `inst.install=0` so Agama loads config but never auto-installs, leaving it
sitting in the `Config` phase for a live API demo. Serial console is attached to the terminal.

```bash
./agama-demo.py <host:port> <root-password> [--sles-version 16.0|16.1]
```
Drives a running Agama installer over its REST API: authenticates, picks a storage profile from a
menu, applies hostname/root-password/software/storage config, re-probes, pauses before triggering
install, then polls to `Finish`. Used both against `launch-demo.sh --manual` VMs
(`./agama-demo.py <guest-ip>:443 DemoSecurity2026! --sles-version 16.1`) and any other reachable
installer.

```bash
./agama-console.py <installer-ip> [--password DemoSecurity2026!]
```
Category-based menu of atomic Agama API actions, for demoing the API surface piece by piece rather
than `agama-demo.py`'s scripted flow. Built by reading route tables straight out of
`agama-server/src/*/web.rs` on the `SLE-16` branch — there's no self-served OpenAPI listing on a
running installer (`aide` generates one, but only via the `agama-web-server doc` CLI subcommand on
the server, which isn't even compiled into the SLES 16.0 GM build).

Categories: **Manager** (status, probe/probe_sync/reprobe_sync, trigger install, watch progress,
**finish** — `POST manager/finish` with body `"reboot"`/`"halt"`/`"stop"`/`"poweroff"`, default
`reboot`, to reboot/halt/poweroff the installed target — list logs), **Software** (products, config,
patterns, **repositories** — `PUT software/config {"extraRepositories": [...]}`; adding a repo
Agama can actually reach makes it synchronously try to refresh it, which can block the request for
a long time on a large/slow one — the config layer echoes back the `enabled` you requested, but
`GET software/repositories` reflects the real zypper state and flips to `enabled:false,
loaded:false` if the repo failed to load — verified live both ways), **Storage**
(disks, config, storage-layout presets applied on-the-fly, bundled full-profile load, probe/
reprobe/reactivate, device/action listings), **Network**, **Localization** (l10n), **Users** (root,
first user, password check), **Questions** (list/answer — LUKS passphrase prompts get a *new*
question id on every re-probe, answering one doesn't stick for the next — plus setting the
auto/user answer policy), **Hostname**, and **Scripts** (`POST scripts` adds a `Script{type, name,
content, chroot?}`, `POST scripts/run` with a bare JSON string group name executes every script in
that group — real command execution: `pre`/`postPartitioning` run in the live installer env,
`post` runs chroot'd into `/mnt` by default, `init` is written now but only runs on the target's
first boot; output lands in `/run/agama/scripts/<group>/<name>.{log,err,out}`; `DELETE scripts`
`remove_dir_all`s the whole group tree including those logs — verified live, including that
ordering gotcha. Only `pre` is auto-run by Agama itself (right after profile load, in
`agama-lib/src/store.rs`) — `postPartitioning`/`post`/`init` are never auto-triggered anywhere in
the Rust codebase, so they only run when the API client (this console, or the web UI) explicitly
calls `POST scripts/run`. The chroot isolation was verified end-to-end against a completed
`atm-slim` install: a `post` script's marker file landed at `/mnt/root/<file>` from the live
installer's view and was absent from the live environment's own `/root` — genuine `chroot /mnt`,
not just execution in the live env. No endpoint reads stdout back over HTTP — only the filesystem
does — and there's no dedicated "run one command" endpoint either, so "Run one ad-hoc command now"
collapses add+run into one step for a single command; verified live). Three top-level extras: a live event stream over the `/ws` websocket (needs
`pip install websockets`, or `zypper install python313-websockets`; degrades to a clear error if
missing), log download (`GET manager/logs/store` streamed to a `.tar.gz`), and a raw
GET/POST/PUT/PATCH/DELETE call for anything not covered by a dedicated action.

Network/Localization/Users actions beyond what `agama-demo.py` already exercises are wired in from
source but not all hand-tested against a live installer — if one 404s/400s, use the raw-call action
to explore live instead of trusting the guessed shape.

```bash
./serve-profiles.sh [port]   # stand-alone HTTP server for profiles/, default port 8000
./start-tpm.sh                # stand-alone swtpm launcher (state in /tmp/mytpm), independent of launch-demo.sh
python3 -m py_compile agama-demo.py      # sanity-check the script parses
python3 -m py_compile agama-console.py   # sanity-check the script parses
bash -n launch-demo.sh                # sanity-check the script parses
jsonnet profiles/atm-slim.jsonnet     # validate/expand a profile
```

`agama-api-profiles-demo.py` is a stale early sketch (targets a `/api/v0` prefix and an old
storage/security schema) — superseded by `agama-demo.py`; don't extend it.

## Requirements / environment assumptions

- `qemu-system-x86_64` + KVM, `swtpm`, `isoinfo` (cdrkit/cdrtools), OVMF at
  `/usr/share/qemu/ovmf-x86_64-4m.bin`.
- SLES install ISO expected at `/data/iso/SLES-16.0-Full-x86_64-GM.install.iso` by default.
- A libvirt network named `SUSE`, bridged as `virbr-suse` on `192.168.110.0/24` with DHCP — the
  VM's NIC attaches to this bridge (not QEMU usermode networking), so the guest gets a real
  routable IP reachable by anything else on that network. `/etc/qemu/bridge.conf` needs
  `allow virbr-suse`.
- If firewalld is active, its `libvirt` zone (bound to `virbr-suse`) only allowlists
  `dhcp dhcpv6 dns http https ssh tftp` by default plus whatever ports were added — it does NOT
  allow `serve-profiles.sh`'s default port 8000/tcp, so the guest's `inst.auto=` fetch gets
  connection-refused (ICMP/ping still works, which makes this look like a DNS/routing problem
  instead of a firewall one). Fix once with:
  `firewall-cmd --zone=libvirt --add-port=8000/tcp --permanent && firewall-cmd --reload`
  (adjust the port if `HTTP_PORT`/`serve-profiles.sh` is invoked with a different one).

## Architecture

### Two independent root passwords — the recurring source of confusion

- **Profile's `root.password`** (`DemoSecurity2026!` in all profiles) configures the **installed
  target system**. Only relevant after install completes and the guest reboots — that's the
  login-prompt password on the serial console at that point.
- **Live installer environment's** own root account (the running ISO, before/during install) is
  separate, used for SSH and the Agama HTTP API's PAM-based remote auth. Set via the
  `live.password=` kernel arg in `launch-demo.sh` (also defaults to `DemoSecurity2026!`, overridable
  via `LIVE_PASSWORD` env var). Without it, the live installer generates a random password shown
  only on console/VNC. `doc/security.md` in the agama repo: local/loopback API access can skip
  auth, remote access always needs PAM auth as this account.

These share a default value in this repo, which is convenient but easy to misread as one setting —
they configure different machines' root accounts (or the same machine at different lifecycle
stages).

### `--manual` / non-auto-install flow

Per `rust/agama-autoinstall/src/main.rs` (SLE-16 branch) in the agama repo: the agama-autoinstall
helper always applies the `inst.auto=` profile (hostname, product, target-system root password,
storage, ...) first, then unconditionally calls `POST /api/manager/install` — unless
`inst.install=0` is also on the kernel command line. `launch-demo.sh --manual` sets that flag, so
config loads but install never auto-fires, letting `agama-demo.py` (or curl) drive the
Startup → Config → Install → Finish phase transitions live for a demo.

### Agama HTTP API shape (baked into `agama-demo.py`/`agama-console.py`; see their docstrings for full detail)

- No `/api/v0`/`/api/v1` prefix — every module is mounted directly under `/api/<module>` (e.g.
  `/api/storage`, `/api/manager`).
- Auth: `POST /api/auth {"password": ...}` sets an `agamaToken` cookie.
- Storage config uses the `drives`/`volumeGroups`/`mdRaids` schema, not a flat
  `{wipe, partitions: [...]}` shape.
- `GET /api/storage/config` wraps its body in a top-level `"storage"` key — `PUT` needs the same
  wrapper (`{"storage": {"drives": [...]}}`), not a bare `{"drives": [...]}`. The bare form 400s
  opaquely (`Invalid JSON config: {}`) instead of a clear schema error. `hostname/config` and
  `users/root` are flat, no such wrapper.
- Re-`PUT`ting the already-selected product on `/api/software/config` 400s with
  `"Product is already selected"` — bites any ISO offering only one product (auto-selected at
  boot). Always `GET` first and only `PUT` on an actual change.
- `encryption.luks2.tpm` (TPM2 auto-unlock) only exists since Agama 16.1.
- `GET /api/questions` lists blocking prompts the installer is waiting on — notably
  `storage.luks_activation`, raised whenever a probe/re-probe finds an already-encrypted device
  (including one Agama itself is mid-way through encrypting, e.g. after a service restart during
  install). Answer with `PUT /api/questions/<id>/answer
  {"generic": {"answer": "decrypt"}, "password": {"password": ...}}`. Each new probe raises a
  **new** question id — answering one doesn't pre-empt the next, and an unanswered question is why
  `manager/installer` can appear stuck at a phase with `isBusy: true` indefinitely.

### Profiles (`profiles/*.jsonnet`)

All set hostname `sles16-<profile>` and the installed system's root password to
`DemoSecurity2026!`.

| Profile | Storage | Version constraint |
|---|---|---|
| `atm-slim` | Single Btrfs root, no encryption | Works on SLES 16.0 and 16.1 |
| `atm-full` | `/boot` ext4 + Btrfs root on LUKS2, TPM2 auto-unlock | **16.1+ only** (`encryption.luks2.tpm` not recognized before 16.1) |
| `atm-full-16.0` | Same as `atm-full` minus the `tpm` field | For 16.0: still LUKS2-encrypted but passphrase must be typed at every boot |

Guest fetches its profile from the host over the bridge at
`http://192.168.110.1:<port>/<profile>.jsonnet` (`serve-profiles.sh`).

### `launch-demo.sh` kernel-arg note

SLES 16.0's customer-facing "Automated Installation Using Agama" guide documents `inst.auto=URL`;
upstream agama's `doc/boot_arguments.md` instead documents `inst.config_url=URL` for the same
purpose. This script uses `inst.auto` (the officially documented one) — swap to `inst.config_url`
in the `APPEND` line if a given build doesn't recognize it.

### Access points once the VM is up

VNC `localhost:5901` (`:1`); guest IP via
`virsh --connect qemu:///system net-dhcp-leases SUSE`; SSH and Agama HTTPS API
(`https://<guest-ip>:443/api/...`) both reachable directly on the bridged IP, no port forwarding —
and also reachable by anyone else on `192.168.110.0/24`.
