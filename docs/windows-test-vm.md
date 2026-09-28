# Windows test VM

Target for the Windows spikes (S1, S2, S4, S6) and Windows tasks of `add-envelope-core`.
The corp host reports `winver` 21H2 (build 19044), so the VM is
**Windows 10 Enterprise LTSC 2021 x64** from Microsoft's 90-day Evaluation Center ISO.

## Hardware and hosting

Actually provisioned on Proxmox (not the KVM/libvirt setup originally planned below): node `mercury`, VMID 113, name `win10-playground`, ZFS storage (`local-zfs`), QEMU guest agent installed and running (enables live quiesced snapshots). Reachable at `192.168.94.172`, hostname `WIN10-PLYGRND`. Managed by a separate Proxmox-operating agent, not directly from this machine.

Original plan (kept for reference, not what was built):
- qcow2 disk (cheap snapshots), 4 vCPU, 8 GB RAM, 80 GB disk; no TPM/Secure Boot needed.
- SATA disk + e1000e NIC (avoids VirtIO drivers during install).
- libvirt default NAT network (VM gets `192.168.122.x`, reachable from the host for SSH).

## Accounts

- `admin` (local administrator) for setup only.
- `envelope` (standard user) for all testing: corp users are not administrators, and
  AppLocker's default rules let administrators run anything.
- Local accounts only.

## Match the corp machine (check there first)

- Locale / code pages: `chcp` in cmd, `Get-WinSystemLocale` and `[Console]::OutputEncoding`
  in PowerShell; set the VM's "language for non-Unicode programs" to match (e.g. Russian →
  OEM 866 / ANSI 1251). S6 depends on it.
- Patch level: full build from `winver` (19044.xxxx); update the VM to that cumulative update.
- Git for Windows PATH option (`where git` on corp: `Git\cmd` only, or also `Git\usr\bin`).
- Keep Windows Defender (AMSI) enabled. Corp EDR cannot be reproduced; see task 13.2.

## Software (as admin, into Program Files)

- Python 3.12.10 for all users (last 3.12 release with a python.org Windows installer; corp
  runs 3.12.13).
- Git for Windows (default options or corp's PATH option).
- PowerShell 7 MSI (Windows PowerShell 5.1 and cmd are built in).
- OpenSSH Server: `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`, service
  automatic, firewall port 22; key for `admin` in
  `C:\ProgramData\ssh\administrators_authorized_keys`, for `envelope` in
  `C:\Users\envelope\.ssh\authorized_keys`.
- Optional: Sysinternals Process Explorer (Job Object membership for S2).
- Node.js 25.3.0 and Qwen Code, installed 2026-09-25 for task 13.2 (see design.md): the zip distribution to `C:\Program Files\nodejs` (SHA-256 verified; the MSI installer is itself blocked by WDAC once enforced), then `npm install -g @qwen-code/qwen-code`. Both, plus Qwen Code's bundled `rg.exe`/`OpenConsole.exe`, needed WDAC scan/merge passes before they would run — see the WDAC section below.

## Snapshots (one per lockdown layer)

1. `clean` — OS installed and patched to the corp level.
2. `tools` — software installed, no restrictions (FullLanguage baseline).
3. `clm-env` — machine env var `__PSLockdownPolicy=4` (quick Constrained Language Mode).
4. `applocker` — configured and verified 2026-09-24 (see design.md): default Exe/Script/Msi Allow rules (Program Files, Windows, Administrators) plus a Script Deny rule for `%TEMP%`, in enforce mode. Both PowerShell 5.1 and pwsh 7 run in ConstrainedLanguage as a result. **Gotcha:** the enforcing component is a third service, `applockerfltr`, which does not auto-start with `AppIDSvc` — run `sc config applockerfltr start= auto` and `sc start applockerfltr`, then reboot once. **Snapshotted:** Proxmox `mercury`/VMID 113, snapshot `snap-2026-09-24-applocker-verified` (quiesced via the QEMU guest agent, disk-only/no vmstate — rollback restores software and policy state, not RAM). This is also the VM's *first* snapshot, so it is the effective `clean`+`tools`+`applocker` baseline; there is no earlier rollback point to a pre-AppLocker or pre-software state.
5. `gpo-policy` — verified 2026-09-24 (see design.md): GPO ExecutionPolicy set via registry (`HKLM\SOFTWARE\Policies\Microsoft\Windows\PowerShell` and `...\PowerShellCore`), which genuinely overrides `-ExecutionPolicy Bypass`. Currently `RemoteSigned` (clean on both PowerShells); `AllSigned` was also tested and doesn't affect envelope (it never runs `.ps1` files) but breaks pwsh 7's own `Get-ExecutionPolicy -List`. **Snapshotted:** Proxmox `mercury`/VMID 113, snapshot `snap-2026-09-24-gpo-verified` (quiesced, filesystem-consistent, disk-only), child of `snap-2026-09-24-applocker-verified`. This is the rollback point before layer 6 (`wdac`) — a misconfigured enforced WDAC policy can make the VM unbootable, with no in-OS undo, so recovery there means reverting to this snapshot from the Proxmox side.
6. `wdac` — verified 2026-09-24 (see design.md): a real App Control policy, enforced, layered on top of AppLocker (still enforcing) and GPO `RemoteSigned` — the full corp lockdown stack from the design's threat model, verified together. `doctor` and all four shells' smoke tests pass; a `%TEMP%` binary is still refused. **Gotcha:** deploy to `CiPolicies\Active\{PolicyID}.cip`, not the legacy `SiPolicy.p7b` — the latter silently didn't load on this build. Remember to cover the QEMU guest agent (`C:\Program Files\Qemu-ga`) in the policy or its unsigned DLLs get blocked once enforced, breaking snapshot quiescing (confirmed fixed: the quiesced snapshot below was itself taken while WDAC enforcement was active, and the guest agent answered freeze/thaw normally). **Snapshotted:** Proxmox `mercury`/VMID 113, snapshot `snap-2026-09-24-wdac-verified` (quiesced, filesystem-consistent, disk-only), child of `snap-2026-09-24-gpo-verified`. This is the VM's most locked-down state and the current baseline — the full chain is `snap-2026-09-24-applocker-verified` → `...-gpo-verified` → `...-wdac-verified` (newest).

   Task 13.2 (2026-09-25) added Node.js and Qwen Code on top of this: each needed its own scan/merge pass added to the enforced policy (`New-CIPolicy -ScanPath` over `C:\Program Files\nodejs`, run again after installing Qwen Code to pick up its bundled `rg.exe`/`OpenConsole.exe`). **Gotcha:** `Merge-CIPolicy` re-adds `Enabled:Audit Mode` to the output whenever any input policy has it — every fresh `New-CIPolicy` scan defaults to audit mode — so `Set-RuleOption -Option 3 -Delete` must be re-run after *every* merge, not just the first one; caught this before deploying by checking the rule options each time, not after. This state (AppLocker + GPO + WDAC + Node/Qwen Code all present) is not yet snapshotted.

Layers 3–6 can be configured over SSH once `tools` exists.

### Evaluation harness state (tasks 7.1-7.3, 2026-09-26)

The snapshot chain continues past the layers above (all on Proxmox `mercury`, VMID 113, disk-only and quiesced):
`snap-2026-09-25-node-qwen-verified` (Node.js and Qwen Code) → `snap-2026-09-26-pre-locale` (taken by the
evaluation runner's operator right before the locale change; also holds the runner-created `C:\eval`) →
**`snap-2026-09-26-eval-p2`, the P2 baseline for the evaluation harness.** It adds, over the previous state:
system locale `ru-RU` set with `Set-WinSystemLocale` as `admin`, then a reboot (ACP 1251, OEMCP 866, locale
0419; `chcp` in the `envelope` user's SSH session prints 866), and `LongPathsEnabled = 1`. The AppLocker rule set,
the WDAC policy file and the Node/Qwen/Python/git binaries are byte-identical to before (checked by hash);
`envelope doctor` reports code pages 866/1251 and all four shells' smoke tests pass; all 26 Windows task oracles
pass. No OpenRouter key exists on the VM: a scan of 503,466 files under `C:\Users`, `C:\ProgramData`, `C:\eval`
and the Node directory plus the machine and admin environments found none (the model key lives only in the
gateway container, see `eval/gateway/README.md`). Autologon was not configured (only needed if the console
calibration, task 8.1, requires an interactive session).

Fingerprint of this state, as measured by `eval/launcher/fingerprint.py` through `SSHTarget.fingerprint()`
(SHA-256 of the facts: OS build, code pages, locale, versions, binary, WDAC and AppLocker hashes):
`203c004be4020683a7f5d64fca314bf8ae47f5abe8ba9c14c2ede0faa946228e`. Jobs declare the facts they require in `[target.vm.fingerprint]` and refuse to start on a mismatch.

### Database clients: mongosh and redis-cli (task 12.2, 2026-09-29)

Installed via `admin` SSH (`envelope` has no write access to `C:\Program Files`), verified against the real
MongoDB/Redis containers task 12.1 set up on `envelope-linux`:

- **mongosh 2.12.0** — official `mongosh-2.12.0-win32-x64.zip`, just `bin/mongosh.exe` extracted (not the
  `mongosh_crypt_v1.dll` client-side field-level-encryption library; not needed here), SHA-256 verified against
  the digest GitHub's release API reports for that asset. Installed to `C:\Program Files\mongosh\mongosh.exe`.
- **redis-cli 5.0.14.1** — `tporadowski/redis` (the de facto standard native Windows port; Microsoft's own
  fork is unmaintained), `Redis-x64-5.0.14.1.zip`, just `redis-cli.exe` extracted. This release predates
  GitHub's asset-digest API, and the project publishes no separate checksum file, so there is no independent
  hash to check against — downloaded over HTTPS from the project's own GitHub releases page (SHA-256 recorded
  below for future reproducibility, not verified against a second source). Installed to
  `C:\Program Files\redis\redis-cli.exe`.

**WDAC scan-and-merge** (same procedure as task 13.2's Node.js/Qwen Code pass, this time via `admin`'s
PowerShell over SSH, code sent on stdin to a non-interactive `powershell -Command -`, not as a `cmd`-quoted
one-liner): `New-CIPolicy -ScanPath` over each new install directory, `Merge-CIPolicy` with the current
`C:\wdac\enforced3.xml` listed first (so the merge keeps its `PolicyID`/`BasePolicyID`,
`{C7E7FBA8-466C-4BA8-A701-8800DBC0F860}` — confirmed by parsing the merged XML before deploying, not assumed),
`Set-RuleOption -Option 3 -Delete` (re-added by every merge, as task 13.2 already found), `ConvertFrom-CIPolicy`
to `enforced4.cip`, copied over the active policy file. **No reboot needed:** the deployed policy carries
`Enabled:Update Policy No Reboot`, so `Invoke-CimMethod -Namespace root\Microsoft\Windows\CI -ClassName
PS_UpdateAndCompareCIPolicy -MethodName Update -Arguments @{FilePath=<the .cip>}` refreshed it live (returned
0); the VM stayed reachable and responsive throughout. The previous active `.cip` was copied to
`C:\wdac\active-backup.cip` first, as an in-OS rollback path distinct from the Proxmox snapshot.

**Verified:** both binaries returned "The system cannot execute the specified program" as `envelope` before the
WDAC update and ran normally after; `redis-cli.exe -h 127.0.0.1 -p 1 PING` and `mongosh --eval
'db.adminCommand({ping:1})'` fail/succeed in the expected shape, confirming they are the real binaries, not a
silently-blocked no-op. **Through the tunnels:** a two-hop chain — this host forwards its own loopback
`16379`/`17017` to `envelope-linux`'s loopback-published container ports (task 12.1's `DatabaseAdmin.start()`,
fixed during this task to join the containers to a second, non-internal network so `-p` publishing actually
takes effect — see design.md), then reverse-tunnels those from this host into the VM's own loopback (same shape
`SSHTarget.open_reverse_tunnel` gives the gateway) — carries `redis-cli.exe -h 127.0.0.1 -p 16379 -a
<pw> PING` to `PONG` and `mongosh.exe --host 127.0.0.1 --port 17017 -u root -p <pw> --eval
'db.adminCommand({ping:1})'` to `{ ok: 1 }`, run from git-bash on the VM (`C:\Program Files\Git\bin\bash.exe`).

**Fingerprint after this state:** only the `wdac` fact changed (AppLocker, code pages, locale, Python/git/qwen
hashes unchanged, checked): `.cip` SHA-256 `300eb4e96bfdfe5d5f010576223670311bce7efd15b63d56420f41300688a3d7`,
whole-fingerprint digest `eaecc3821dab2944338a7dfca3e52e2d5716c19e191123c64ac7dce2496c580e`.
**Snapshotted:** Proxmox `mercury`/VMID 113, `snap-2026-09-29-mongosh-redis` (quiesced, disk-only), child of
`snap-2026-09-26-eval-p2`.

Console calibration (task 8.1, result in design E8): to compare an interactive session with SSH-launched runs,
autologon was set for `envelope` temporarily (`AutoAdminLogon`, `DefaultUserName`, `DefaultDomainName`,
`DefaultPassword` under `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon`, with the account's password
reset to a random value that was never stored), the VM rebooted, `python -m envelope_eval calibrate` run, and then
the values removed and the session logged off. **The `envelope` account's password is therefore unknown to everyone**;
SSH key login is unaffected. To log in interactively again, reset it as `admin` (`net user envelope <new>`).

Licence: Windows Enterprise LTSC evaluation, installed 2026-09-24 (registry `InstallDate`), so it ends about
**2026-12-23** (90 days; `slmgr` only reports "Notification" for this build, not a date). Job files declare
`expires` for the target and refuse to start within 3 days of it. Reverting a snapshot does not reset the clock.
**Rebuild procedure:** install the evaluation ISO, create `admin` and `envelope` (standard user), install Python
3.12, Git for Windows, PowerShell 7 and the QEMU guest agent, enable OpenSSH with the keys above, apply the
AppLocker, GPO and WDAC layers in the order listed above (each verified and snapshotted), install Node.js and
Qwen Code with their WDAC scan/merge passes, then apply this section's locale and long-path changes, reboot, and
run a job's fingerprint check and `python -m envelope_eval` oracle gates before trusting it.



## Caveats

- SSH sessions have no interactive console; envelope started by Qwen Code in a terminal
  does. Repeat console/code-page-dependent checks in an interactive session (RDP, or a
  scheduled task running as the logged-in user).
- The evaluation expires 90 days after install; reverting a snapshot does not reset it.
  Keep provisioning scripted so a rebuild is quick.
