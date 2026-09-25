## Purpose

Defines exactly what the model sees after a command: a compact, stable text result that carries all essential information in every MCP client, lean structured metadata for clients that consume it, and deterministic hints that explain common failures.

## ADDED Requirements

### Requirement: Text result layout
Every `run` result SHALL contain a text content block consisting of: a one-line header with exit status (exit code, signal, or timed out/cancelled), duration, shell identifier and working directory; then, when output was truncated or spooled, a line stating total lines and size, what is shown, and the output handle; then the rendered output; then zero or more `note:` lines with hints. The text SHALL be self-sufficient: a client that discards `structuredContent` loses no information required to act on the result.

#### Scenario: Successful short command
- **WHEN** `run` executes `echo hi` in bash in `/home/u`
- **THEN** the text begins with a header like `exit 0 | 0.01s | bash | cwd /home/u` followed by `hi`

#### Scenario: Timed out command
- **WHEN** a command is terminated by timeout after 120 seconds
- **THEN** the header states `timed out after 120s` in place of the exit code and the partial output follows

#### Scenario: Empty output
- **WHEN** a command succeeds without printing anything
- **THEN** the text states `(no output)` after the header

### Requirement: Structured metadata
Every `run` and `output` result SHALL include `structuredContent` containing only metadata: exit code, signal, timed-out and cancelled flags, duration in milliseconds, shell identifier, resolved working directory, output handle, total/shown line and byte counts per stream, truncation flags, decoding notes, hint identifiers and the policy decision. `structuredContent` SHALL NOT duplicate the rendered output text.

#### Scenario: Client that renders structured content as text
- **WHEN** a client appends `structuredContent` serialized as JSON to the model-visible text
- **THEN** the added JSON contains metadata only and is under 1,000 characters for a typical result

### Requirement: Deterministic hints
Envelope SHALL add a hint only when the condition is detected with certainty from exit status, signal or output patterns defined in a hint catalog; it SHALL NOT speculate. The catalog SHALL include at least:
- command not found (exit 127, or PowerShell/cmd "is not recognized"), naming other available shells in which the command exists;
- found but not executable (exit 126);
- terminated by signal (exit 128+N or signal), with SIGKILL noting a possible out-of-memory kill;
- Windows crash status codes (e.g. `0xC0000005` access violation, `0xC0000409` stack buffer overrun);
- timed out, including whether all processes were terminated;
- blocked by application control or group policy (Windows errors 1260, 4551, or AppLocker/WDAC messages);
- PowerShell Constrained Language Mode errors;
- PowerShell execution policy refusals;
- PowerShell errors reported while the exit code is 0 (non-terminating errors);
- binary output;
- output decoded with replacement characters;
- stdin was closed and the command appears to have prompted for input.

#### Scenario: Command missing in current shell but present in another
- **WHEN** `run` executes `jq .` in cmd, jq is not on cmd's PATH, but git-bash is available and jq exists in git-bash
- **THEN** the result includes a note that `jq` was not found in cmd and is available in git-bash

#### Scenario: Out-of-memory kill
- **WHEN** a command on Linux exits due to SIGKILL
- **THEN** the result includes a note that the process was killed by SIGKILL, possibly by the out-of-memory killer

#### Scenario: Non-terminating PowerShell error
- **WHEN** a pwsh command writes an error record but finishes with exit code 0
- **THEN** the result includes a note that PowerShell reported errors although the exit code is 0

#### Scenario: No hint without certainty
- **WHEN** a command exits with code 1 and generic output
- **THEN** the result contains no hint notes

### Requirement: Policy outcome rendering
When a request is denied or requires approval by the execution policy, the result SHALL be a tool error whose text states the decision, the rule identifier, the reasons, and what the user can do to allow it; the command SHALL NOT have been executed.

#### Scenario: Denied command
- **WHEN** the policy denies a command because its shell is not in the allowed shells list
- **THEN** the result is a tool error stating "denied by policy", the rule, the reason and the configuration setting that controls it
