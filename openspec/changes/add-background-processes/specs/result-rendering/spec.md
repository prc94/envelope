# Spec Delta

## MODIFIED Requirements

### Requirement: Text result layout
Every `run`, `output` and `stop` result SHALL contain a text content block consisting of: a one-line header with the process state (exit code, signal, timed out, cancelled, stopped, or `running` with the elapsed time and the handle), duration, shell identifier and working directory; for a wait, a line stating whether the condition was met, the process finished, or the wait expired; then, when output was truncated or spooled, a line stating total lines and size, what is shown, and the output handle; then the rendered output; then, for a running process, the exact calls that wait on, read and stop it; then zero or more `note:` lines with hints and completion notices. The text SHALL be self-sufficient: a client that discards `structuredContent` loses no information required to act on the result.

#### Scenario: Successful short command
- **WHEN** `run` executes `echo hi` in bash in `/home/u`
- **THEN** the text begins with a header like `exit 0 | 0.01s | bash | cwd /home/u` followed by `hi`

#### Scenario: Timed out command
- **WHEN** a command is terminated by timeout after 120 seconds
- **THEN** the header states `timed out after 120s` in place of the exit code and the partial output follows

#### Scenario: Empty output
- **WHEN** a command succeeds without printing anything
- **THEN** the text states `(no output)` after the header

#### Scenario: Handed-back command
- **WHEN** a build is still running when its 30-second wait ends
- **THEN** the text begins with a header like `running 30s | out_5d1e | bash | cwd /home/u`, shows the output so far, and ends with the `output` call that waits for it and the `stop` call that ends it

### Requirement: Structured metadata
Every `run`, `output`, `stop` and `processes` result SHALL include `structuredContent` containing only metadata: process state (`running` or `ended`) with the end reason (`exit`, `timeout`, `lifetime`, `stop`, `eviction`, `cancel`, `shutdown`), whether a stop was graceful or forced, exit code, signal, duration in milliseconds, shell identifier, resolved working directory, output handle and process name, wait outcome (`condition_met`, `finished`, `wait_expired`) where a wait took place, total/shown line and byte counts per stream, truncation flags, decoding notes, hint identifiers, the handles of completion notices and the policy decision; for `processes`, one such state record per listed process without output, limited to the listed entries. `structuredContent` SHALL NOT duplicate the rendered output text.

#### Scenario: Client that renders structured content as text
- **WHEN** a client appends `structuredContent` serialized as JSON to the model-visible text
- **THEN** the added JSON contains metadata only and is under 1,000 characters for a typical result

#### Scenario: Running result metadata
- **WHEN** `run` returns while its command is still running
- **THEN** `structuredContent` has state `running`, the handle, the elapsed time and wait outcome `wait_expired`, and no exit code

### Requirement: Deterministic hints
Envelope SHALL add a hint only when every condition the hint catalog defines for it is observed from exit status, signal, output patterns or process state; it SHALL NOT speculate, and SHALL NOT show a hint when one of its conditions cannot be observed. A hint whose explanation is a likely cause (for example output buffering, or an out-of-memory kill) SHALL word that cause as likely. The catalog SHALL include at least:
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
- stdin was closed and the command appears to have prompted for input;
- a process started in the background inside a command (a POSIX `&` job, `nohup`, `setsid` or `disown`; cmd `start`; PowerShell `Start-Process` or `Start-Job`) was terminated when the command finished, with how to keep it running;
- address already in use (e.g. `EADDRINUSE`, "address already in use", Windows error 10048), naming the process that listens on that port and its handle when envelope started it;
- a running process has produced no output yet, and a Windows process appears to wait for console input (the quiet-process and console-prompt hints of the background-processes capability).

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

#### Scenario: Port already in use by a handed-back server
- **WHEN** a server started earlier as `out_3f2a` named `web` listens on port 3000 and a new `run` starts a second server that fails with `EADDRINUSE` on port 3000
- **THEN** the result includes a note that port 3000 is held by `out_3f2a` (`web`) and how to stop or restart it
