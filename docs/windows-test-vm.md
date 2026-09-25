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

## Caveats

- SSH sessions have no interactive console; envelope started by Qwen Code in a terminal
  does. Repeat console/code-page-dependent checks in an interactive session (RDP, or a
  scheduled task running as the logged-in user).
- The evaluation expires 90 days after install; reverting a snapshot does not reset it.
  Keep provisioning scripted so a rebuild is quick.
