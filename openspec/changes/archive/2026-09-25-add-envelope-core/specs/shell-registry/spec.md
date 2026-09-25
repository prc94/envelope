## Purpose

Discovers which shells are available on the host, chooses a default, lets users define additional shells, and defines how each shell is launched so commands behave predictably and non-interactively across Linux, macOS and Windows.

## ADDED Requirements

### Requirement: Shell detection
At startup envelope SHALL detect available shells and record for each its identifier, executable path and version where obtainable. Built-in identifiers SHALL be: `bash`, `sh`, `zsh`, `fish` (POSIX and wherever installed), `git-bash` (Windows), `pwsh` (PowerShell 7+, any OS), `powershell` (Windows PowerShell 5.1) and `cmd` (Windows). Detection SHALL complete within 5 seconds in total and a shell whose probe fails or times out SHALL be reported as unavailable with the reason.

#### Scenario: Linux host
- **WHEN** envelope starts on Linux with bash, sh and zsh installed
- **THEN** the registry lists `bash`, `sh` and `zsh` as available with their paths and versions

#### Scenario: Probe timeout
- **WHEN** a shell's version probe hangs
- **THEN** envelope marks that shell unavailable with reason "probe timed out" and starts normally

### Requirement: Git-bash discovery
On Windows envelope SHALL locate git-bash from the Git for Windows installation (registry install path, then the location of `git.exe` on PATH, then standard install directories) and SHALL use the Git for Windows `bin\bash.exe` launcher. Envelope SHALL NOT treat `C:\Windows\System32\bash.exe` (the WSL launcher) as git-bash.

#### Scenario: Git installed in default location
- **WHEN** Git for Windows is installed in `C:\Program Files\Git`
- **THEN** git-bash is registered with path `C:\Program Files\Git\bin\bash.exe`

#### Scenario: WSL present, Git absent
- **WHEN** only WSL's `bash.exe` exists on PATH
- **THEN** git-bash is reported unavailable

### Requirement: Default shell selection
Envelope SHALL choose a default shell used when `run` omits `shell`: on Windows `git-bash` if available, otherwise `pwsh`, otherwise `powershell`, otherwise `cmd`; on macOS and Linux the user's login shell if it is a supported shell, otherwise `bash`, otherwise `sh`. Configuration SHALL be able to override the default.

#### Scenario: Windows without Git
- **WHEN** envelope starts on Windows with pwsh installed and no Git for Windows
- **THEN** the default shell is `pwsh`

#### Scenario: Configured default
- **WHEN** configuration sets the default shell to `cmd`
- **THEN** `run` without `shell` executes in cmd

### Requirement: Custom shells
Configuration SHALL be able to define additional shells by identifier, executable path and argument template containing a script placeholder, and optionally a script file extension for large-script delivery. Custom shells SHALL appear in the registry, tool descriptions and server instructions like built-in shells.

#### Scenario: Nushell defined in config
- **WHEN** configuration defines shell `nu` with path to `nu` and arguments `["-c", "{script}"]`
- **THEN** `run` with `shell: "nu"` executes the command through nushell

### Requirement: Shell selection is restricted to available shells
The `run` tool's `shell` parameter SHALL accept only identifiers of available shells, and SHALL accept common aliases case-insensitively (`powershell7`/`pwsh7` → `pwsh`, `gitbash`/`git_bash` → `git-bash`, `bash` on Windows → `git-bash` when native bash is unavailable).

#### Scenario: Alias on Windows
- **WHEN** a model on Windows calls `run` with `shell: "bash"` and git-bash is available
- **THEN** the command runs in git-bash and the result header names `git-bash`

### Requirement: Non-interactive launch per shell
Each built-in shell SHALL be launched non-interactively without loading user profiles where the shell allows it: PowerShell with `-NoProfile -NonInteractive`, with progress output suppressed, UTF-8 output encoding and plain-text (non-ANSI) output rendering; cmd with AutoRun disabled and a UTF-8 code page; bash-family shells as non-login, non-interactive shells. PowerShell results SHALL report the effective exit status, using `$LASTEXITCODE` from the last native command when set and otherwise mapping failure (`$?` false) to exit code 1.

#### Scenario: PowerShell script with failing cmdlet
- **WHEN** `run` executes in pwsh `Get-Item C:\does-not-exist`
- **THEN** the result reports a non-zero exit code and includes the error message

#### Scenario: PowerShell native command exit code
- **WHEN** `run` executes in pwsh `cmd /c exit 7`
- **THEN** the result reports exit code 7

### Requirement: Large-script delivery
Envelope SHALL deliver commands to the shell in a way that is not limited by OS command-line length and does not rely on creating executable script files: for bash-family shells via a script file read as data by the shell; for PowerShell by passing the script text as data (never as a `.ps1` file executed as a script), with the caller's `stdin` text available to the script as `$input` and to programs it starts; where PowerShell runs in Constrained Language Mode, which permits no script-block construction from text, the script is passed as the `-Command` argument and is limited by cmd's command-line length (about 7,500 characters of command), and longer commands are rejected with a tool error explaining the limit and suggesting smaller calls or git-bash; for cmd via the command line, rejecting commands that exceed cmd's 8,191-character limit with a tool error explaining the limit. Envelope SHALL NOT use `-EncodedCommand`, `-ExecutionPolicy Bypass` or `Invoke-Expression` to deliver commands.

#### Scenario: 100 KB bash script
- **WHEN** `run` executes a 100 KB multi-line script in git-bash
- **THEN** the script runs completely and the result reflects its exit status

#### Scenario: Large PowerShell script under AppLocker script rules
- **WHEN** `run` executes a 50 KB PowerShell script on a host where AppLocker blocks `.ps1` files in the temp directory
- **THEN** the script runs, because no `.ps1` file is created

#### Scenario: Long PowerShell script under Constrained Language Mode
- **WHEN** `run` executes a 20 KB PowerShell script on a host where PowerShell runs in Constrained Language Mode
- **THEN** envelope returns a tool error stating the length limit in Constrained Language Mode and suggesting smaller calls or git-bash

#### Scenario: PowerShell reads caller stdin
- **WHEN** `run` executes `$input | ForEach-Object { "got: $_" }` in pwsh with `stdin` "бета\n"
- **THEN** the output is `got: бета`, in FullLanguage and in Constrained Language Mode

#### Scenario: Oversized cmd command
- **WHEN** `run` executes a 10,000-character command in cmd
- **THEN** envelope returns a tool error stating cmd's 8,191-character limit and suggesting pwsh or git-bash

### Requirement: Launch only real executables
Envelope SHALL launch shells by their real executable paths and SHALL NOT launch shells through `.bat` or `.cmd` wrappers.

#### Scenario: Shell found only as a batch shim
- **WHEN** the only `pwsh` found on PATH is a `pwsh.cmd` wrapper
- **THEN** envelope does not register it as `pwsh` unless the underlying executable is located
