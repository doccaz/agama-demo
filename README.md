# agama-demo

Unattended SLES 16 install demo using [Agama](https://github.com/agama-project/agama),
driven end-to-end in a local QEMU VM. Boots the installer ISO with
`inst.auto=` pointing at a canned jsonnet profile — no clicking through the
Agama web UI required.

## Requirements

- `qemu-system-x86_64` with KVM
- `swtpm` (TPM 2.0 emulation, needed for the `atm-full` TPM auto-unlock profile)
- `isoinfo` (from `cdrkit`/`cdrtools`) to extract the installer kernel/initrd
- OVMF UEFI firmware (default path: `/usr/share/qemu/ovmf-x86_64-4m.bin`)
- A SLES 16.x install ISO (default expected at
  `/data/iso/SLES-16.0-Full-x86_64-GM.install.iso`)
- A libvirt network named `SUSE` (`virsh --connect qemu:///system net-list --all`),
  bridged as `virbr-suse` on `192.168.110.0/24` with DHCP — the VM's NIC is
  attached to this bridge instead of QEMU usermode networking. `/etc/qemu/bridge.conf`
  needs an `allow virbr-suse` line so the setuid `qemu-bridge-helper` can attach to it.
- If firewalld is active, its `libvirt` zone (bound to `virbr-suse`) must allow the profile
  server's port (default 8000/tcp), or the guest's `inst.auto=` fetch fails with a connection
  reset while ping/DNS still work — easy to misdiagnose as a network issue:
  ```bash
  sudo firewall-cmd --zone=libvirt --add-port=8000/tcp --permanent
  sudo firewall-cmd --reload
  ```

## Quick start

```bash
./launch-demo.sh [--manual] [profile] [iso-path]
```

- `--manual` — load the profile but don't auto-install; see
  [Live API demo](#live-api-demo) below.
- `profile` — one of `atm-slim`, `atm-full`, `atm-full-16.0` (default: `atm-slim`)
- `iso-path` — path to the SLES install ISO (default: see above)

This single command:
1. Starts `swtpm` (emulated TPM 2.0) if not already running.
2. Serves `./profiles/*.jsonnet` over HTTP (`serve-profiles.sh`) so the guest
   can fetch its profile via `inst.auto=`.
3. Extracts the installer `linux`/`initrd` from the ISO (cached under
   `vm/cache/`).
4. Creates a 20G qcow2 disk at `vm/agama-demo.qcow2` if one doesn't exist.
5. Boots QEMU with the profile URL on the kernel command line.

The VM's serial console is attached to your terminal (`-serial stdio`), so
installer and boot logs print directly there. Once the unattended install
finishes and the guest reboots, **that same console becomes the installed
system's login prompt** — log in as `root` with the password baked into the
profile: `DemoSecurity2026!`.

There are two, independent root passwords in play, easy to conflate:
- The **profile's** `root.password` (`DemoSecurity2026!`) configures the
  **installed target system** — that's the password for the login prompt
  above, after install+reboot.
- The **live installer environment itself** (the running ISO, before/during
  install) has its own separate root account, used for SSH and for the
  Agama HTTP API's PAM-based remote auth. `launch-demo.sh` sets it via the
  `live.password=` kernel argument (also `DemoSecurity2026!` by default —
  overridable with the `LIVE_PASSWORD` env var), matching the
  `live-password` boot script baked into the Agama live media. Without this,
  the live installer generates a random password shown only on the local
  console/VNC, which you'd have to read off the screen for remote access.

Other access points while the VM is running:
- VNC: `localhost:5901` (display `:1`) — graphical console
- The guest is bridged onto the libvirt `SUSE` network (`virbr-suse`,
  `192.168.110.0/24`) and gets an IP via DHCP — find it with
  `virsh --connect qemu:///system net-dhcp-leases SUSE`.
- SSH: `<guest-ip>:22` (`root` / the live installer password,
  `DemoSecurity2026!` by default)
- HTTPS: `<guest-ip>:443` (Agama's own HTTP API, while the installer is
  still up — same live installer password)

Because the guest has a real routable IP on that network (rather than being
NAT'd behind QEMU usermode networking), anyone else who can reach
`192.168.110.0/24` can also hit it directly, firewall permitting.

## Live API demo

To leave the installer sitting at its **web UI/API with nothing installed
yet**, so you can drive it live over the API and show off the phase
transitions (`Startup` → `Config` → `Install` → `Finish`) instead of watching
an unattended install run to completion on its own:

```bash
./launch-demo.sh --manual atm-slim
```

`--manual` appends `inst.install=0` to the kernel command line alongside
`inst.auto=`. Per Agama's own auto-install helper
(`rust/agama-autoinstall/src/main.rs`, `SLE-16` branch), that helper always
applies the `inst.auto=` profile first (hostname, product, target-system
root password, storage, ...) and only calls `POST /api/manager/install`
afterwards, unless `inst.install=0` is present. With that flag, config load
happens but install never auto-fires, so Agama sits in the `Config` phase
indefinitely.

Remote (non-localhost) API access requires PAM auth as `root` on the **live
installer environment** — not the profile's `root.password` (see the two
separate passwords called out above). `launch-demo.sh` always sets the live
installer's password via `live.password=`, so this works out of the box;
without it, you'd need to read a random password off the console/VNC before
you could authenticate remotely (`doc/security.md` in the agama repo:
local/loopback access can skip auth, remote access always needs it).

Once the console (or VNC) shows the installer has booted and settled, drive
it from another machine (or the same one) with:

```bash
./agama-demo.py <guest-ip>:443 DemoSecurity2026! --sles-version 16.1
```

(that password is the live installer's `live.password=`, not the profile's
`root.password` — they happen to share the same default value in this repo,
which is convenient but easy to misread as the same setting).

`agama-demo.py` authenticates, shows the current phase, lets you pick a
storage profile from a menu, applies hostname/root-password/software/storage
config over the API (re-probing so it's picked up), then pauses on
"Press ENTER to trigger INSTALL..." — a natural break point for narrating
what's about to happen before you hit enter and watch the phase counter
advance to `Finish`.

Anything shown mid-demo (e.g. `GET /api/manager/installer` for the raw phase
JSON, or `GET /api/storage/devices/system`) can also just be curled directly
against `https://<host>:8443/api/...` with `-k` and the same auth cookie, if
you want to narrate raw API responses instead of the scripted flow.

## Profiles (`profiles/*.jsonnet`)

| Profile | Storage | Notes |
|---|---|---|
| `atm-slim` | Single Btrfs root, no encryption | Works unchanged on SLES 16.0 and 16.1 |
| `atm-full` | `/boot` ext4 + Btrfs root on LUKS2 with TPM2 auto-unlock | **SLES 16.1+ only** — `encryption.luks2.tpm` isn't recognized before 16.1 |
| `atm-full-16.0` | Same as `atm-full` but no `tpm` field | For SLES 16.0: disk is still LUKS2-encrypted, but the passphrase must be typed by hand at every boot |

All profiles set hostname `sles16-<profile>` and the **installed system's**
root password to `DemoSecurity2026!` — this is only what you log in with
after install+reboot, not the live installer's own root password (see
[Live API demo](#live-api-demo) above).

## Driving a running installer over its HTTP API

Instead of (or in addition to) `inst.auto=`, `agama-demo.py` can drive an
**already-booted** Agama installer interactively over its REST API:

```bash
./agama-demo.py <installer-ip> <root-password> [--sles-version 16.0|16.1]
```

It authenticates, lists disks and products, lets you pick `atm-slim` or
`atm-full` from a menu, applies hostname/root-password/software/storage
config, re-probes, and triggers the install — polling until it reaches the
`Finish` phase.

Key facts about the Agama API (baked into this script; see its docstring for
detail):
- No `/api/v0` or `/api/v1` prefix — every module is mounted directly under
  `/api/<module>` (e.g. `/api/storage`, `/api/manager`).
- Auth is `POST /api/auth {"password": ...}`, which sets an `agamaToken`
  cookie.
- Storage config uses the `drives`/`volumeGroups`/`mdRaids` schema, not a
  flat `{wipe, partitions: [...]}` shape.
- `GET /api/storage/config` wraps its body in a top-level `"storage"` key —
  `PUT` needs that same wrapper (`{"storage": {"drives": [...]}}`), not a
  bare `{"drives": [...]}`. The bare form doesn't error clearly: it silently
  turns into an empty D-Bus config and 400s with `Invalid JSON config: {}`.
  `hostname/config` and `users/root` are flat, no such wrapper.
- Re-selecting the currently-selected product via `PUT /api/software/config`
  400s with `"Product is already selected"`. Bites you on any ISO that only
  offers one product (e.g. plain SLES media, which auto-selects it at boot)
  — check `GET` first and only `PUT` on an actual change.
- TPM2 auto-unlock (`encryption.luks2.tpm`) only exists since Agama 16.1.

> `agama-api-profiles-demo.py` is an **older/stale sketch** of the same idea
> (hits a `/api/v0` prefix and a different storage/security schema that no
> longer matches the current Agama API). Prefer `agama-demo.py`.

## Fine-grained API control (`agama-console.py`)

```bash
./agama-console.py <installer-ip> [--password DemoSecurity2026!]
```

Where `agama-demo.py` runs one scripted end-to-end flow, `agama-console.py` is
a category-based menu of atomic API actions for demoing the API surface piece
by piece. There's no self-served OpenAPI/Swagger listing on a running
installer to build this from — Agama's `aide`-generated spec is only
reachable via `agama-web-server doc` on the server host, and that subcommand
isn't even compiled into the SLES 16.0 GM build — so the menu was built by
reading the route tables straight out of `agama-server/src/*/web.rs` on the
`SLE-16` branch.

Categories: **Manager** (status, probe/probe_sync/reprobe_sync, trigger
install, watch progress, **finish install** — reboot/halt/stop/poweroff the
target, list logs), **Software** (products, config, patterns, **list/add/
clear repositories** — `PUT software/config extraRepositories`; adding a repo
whose metadata Agama can actually reach makes it try to synchronously refresh
it, which can block the request for a long time on a large/slow repo — the
config layer echoes back whatever `enabled` you asked for, but `GET
software/repositories` shows the real zypper-backend state, which flips to
`enabled:false, loaded:false` if the repo couldn't be loaded, licenses,
proposal, probe, registration status), **Storage** (disks, config,
storage-layout presets applied on-the-fly, bundled full-profile load, probe/
reprobe/reactivate, raw device/action listings), **Network**, **Localization**
(keymaps/locales/timezones/config), **Users** (root, first user, password
check), **Questions** (list/answer pending questions — e.g. the LUKS
passphrase prompt raised whenever probing an already-encrypted disk; each
re-probe raises a *new* question id, answering one doesn't pre-empt the next
— and setting the auto/user answer policy), **Hostname**, and **Scripts**
(add/run/clear user-defined scripts — genuine command execution against
whatever the API reaches: `pre`/`postPartitioning` scripts run in the live
installer environment, `post` scripts run chroot'd into the installed target
by default, `init` scripts are written now but only run on the target's
first boot; verified live this session, stdout/stderr/exit status land in
`/run/agama/scripts/<group>/<name>.{log,err,out}`. Only `pre` is auto-run by
Agama itself, right after profile load — `postPartitioning`/`post`/`init`
are never auto-triggered anywhere in the Rust codebase, so they only run
when explicitly invoked over the API. Chroot isolation for `post` was
verified end-to-end: a marker file written by the script landed at
`/mnt/root/<file>` from the live installer's view and was absent from the
live environment's own `/root` — genuine `chroot /mnt`, not the live env).
Three top-level extras
round it out: a live event stream over the `/ws` websocket (needs
`pip install websockets`, or on openSUSE/SLE `zypper install
python313-websockets`; degrades to a clear error if missing), log download
(`GET manager/logs/store`, streamed straight to a `.tar.gz` on disk), and a
raw GET/POST/PUT/PATCH/DELETE call for anything not wired into a dedicated
action.

The Network/Localization/Users endpoints beyond what `agama-demo.py` already
exercises are wired in from the source route tables but not all hand-tested
against a live installer — if one 404s/400s on your Agama version, fall back
to the raw API call action to explore it live instead.

## Other scripts

- `serve-profiles.sh [port]` — stand-alone HTTP server for `./profiles`
  (default port 8000). From the guest, bridged onto the `SUSE` libvirt
  network, profiles are reachable at `http://192.168.110.1:<port>/<profile>.jsonnet`.
- `start-tpm.sh` — minimal stand-alone `swtpm` launcher (state in
  `/tmp/mytpm`). `launch-demo.sh` already manages its own TPM instance, so
  this is only needed if you want swtpm running independent of the demo
  script.

## Layout

```
agama-demo/
├── launch-demo.sh                  one-command unattended install + boot
├── serve-profiles.sh               HTTP server for profiles/
├── start-tpm.sh                    stand-alone swtpm launcher
├── agama-demo.py                   drives a running installer over its HTTP API
├── agama-console.py                interactive menu of atomic Agama API actions
├── agama-api-profiles-demo.py      stale early sketch — superseded by agama-demo.py
├── profiles/
│   ├── atm-slim.jsonnet
│   ├── atm-full.jsonnet
│   └── atm-full-16.0.jsonnet
└── vm/
    ├── agama-demo.qcow2            demo disk image (created on first run)
    └── cache/                      extracted installer kernel/initrd, per ISO
```
