## Purpose

Gives users a single command to understand how envelope sees their machine — shells, versions, restrictions and effective configuration — which is essential for setting it up on locked-down corporate hosts.

## ADDED Requirements

### Requirement: Doctor command
`python envelope.pyz doctor` SHALL print, without starting the MCP server: envelope version; Python version and path; OS and architecture; the effective configuration file path(s) and any validation warnings; every built-in and custom shell with availability, path, version or the reason it is unavailable; the default shell; and the spool and audit log locations with their writability.

#### Scenario: Doctor on Linux
- **WHEN** a user runs `python envelope.pyz doctor` on Linux
- **THEN** it lists detected shells with versions, the default shell and the spool and audit locations, and exits with status 0

### Requirement: Windows restriction report
On Windows, `doctor` SHALL additionally report for each available PowerShell: its language mode (e.g. FullLanguage, ConstrainedLanguage) and effective execution policy; the console input and output code pages; and the result of a live smoke test per shell (running a trivial command, running a multi-line script through the large-script delivery path, and reading back UTF-8 non-ASCII output), each reported as pass or fail with the error.

#### Scenario: Constrained Language Mode detected
- **WHEN** `doctor` runs on a machine where WDAC places PowerShell in Constrained Language Mode
- **THEN** the report shows `ConstrainedLanguage` for that PowerShell and the smoke tests show which delivery paths work

#### Scenario: Smoke test failure is reported, not fatal
- **WHEN** the large-script smoke test fails for `powershell` 5.1
- **THEN** `doctor` shows the failure with the error text, continues with other checks, and exits with a non-zero status

### Requirement: Machine-readable output
`doctor --json` SHALL print the same information as a single JSON document.

#### Scenario: JSON report
- **WHEN** a user runs `python envelope.pyz doctor --json`
- **THEN** stdout contains one valid JSON object with shells, restrictions, smoke test results and paths
