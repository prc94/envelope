# Spec Delta

## MODIFIED Requirements

### Requirement: Full output spooling
Envelope SHALL write the complete raw output of every `run` call to a per-user spool directory, up to a configurable cap per call (default 64 MiB), and SHALL assign the output a handle. The spool of a running process SHALL be readable through `output` while it grows. When the cap is exceeded envelope SHALL keep the first and last portions and record the number of bytes dropped; for a running process the last portion SHALL keep advancing with new output.

#### Scenario: Output larger than cap
- **WHEN** a command writes 200 MiB of output with a 64 MiB cap
- **THEN** the spool holds the beginning and end of the output and the result states that bytes were dropped from the middle of the spool

#### Scenario: Reading a running process
- **WHEN** a handed-back server has printed 50 lines and `output` is called with `tail: 10`
- **THEN** lines 41-50 are returned and the result states that the process is still running

#### Scenario: Long-running server exceeds the cap
- **WHEN** a handed-back server has written more than the spool cap
- **THEN** its startup output and its most recent output remain readable and the dropped middle is reported

### Requirement: The output tool
The `output` tool SHALL accept `handle` (required; an output handle or a process name) and at most one read mode: `tail` (last N lines), `offset` with optional `limit` (line range, 1-based), or `grep` (regular expression) with optional `context` lines; plus optional `stream` (`both`, `stdout`, `stderr`, default `both`), `wait_s` and `wait_for`. Without a read mode it SHALL return the unread output when there is any, and otherwise the tail. When `wait_s` or `wait_for` is given it SHALL first wait as defined by the background-processes capability, then read. Results SHALL be cleaned and budgeted like `run` output and SHALL state the line numbers returned, the total line count, whether the process is still running or its exit status, and for grep the number of matches and whether more matches exist beyond the budget.

#### Scenario: Grep with context
- **WHEN** `output` is called with `grep: "FAIL"` and `context: 2` on a spooled test log with 3 failures
- **THEN** the result shows the 3 matching lines with 2 lines of context each, labeled with line numbers

#### Scenario: Page through a range
- **WHEN** `output` is called with `offset: 1000` and `limit: 50`
- **THEN** lines 1000-1049 are returned with their line numbers and the total line count

#### Scenario: Invalid regex
- **WHEN** `output` is called with `grep: "(["`
- **THEN** envelope returns a tool error describing the invalid regular expression

#### Scenario: Wait then read new output
- **WHEN** `output` is called with `handle: "build"`, `wait_for: "exit"` and `wait_s: 120` on a running build that finishes after 40 seconds
- **THEN** the call returns after about 40 seconds with the build's exit status and the output it printed since the last read

### Requirement: Handle lifetime and retention
Output handles SHALL remain valid for the lifetime of the envelope process and for a configurable retention period afterwards (default 24 hours) as long as the spool file exists. The spool of a process that is still running SHALL NOT be deleted or evicted, by the envelope instance that owns it or by any other envelope instance sharing the spool directory, and its retention period SHALL start when the process finishes. Envelope SHALL delete spool files older than the retention period at startup and SHALL cap total spool size (default 1 GiB), counting live spools toward the cap but evicting only spools of finished processes, oldest first; the spool of a result being returned SHALL NOT be evicted before that result is rendered. An unknown or expired handle or name SHALL produce a tool error saying so.

#### Scenario: Expired handle
- **WHEN** `output` is called with a handle whose spool was evicted
- **THEN** envelope returns a tool error stating the handle is unknown or expired

#### Scenario: Running process survives spool pressure
- **WHEN** total spool size exceeds its cap while a server started an hour ago is still running
- **THEN** spools of finished processes are evicted and the server's spool remains readable

#### Scenario: Another envelope instance starts
- **WHEN** a quiet server handed back 30 hours ago by one envelope instance is still running and a second envelope instance starts with the same spool directory
- **THEN** the second instance's startup cleanup leaves the server's spool in place

## ADDED Requirements

### Requirement: Unterminated output of running processes
While a process runs, output that does not yet end with a line break (a prompt, a progress line updated with carriage returns) SHALL be visible within about 1 second after it was written: results SHALL show it as the last line, marked as unterminated, `regex:` waits and hint assessments SHALL consider it, and the stream's encoding SHALL be decided without waiting for a line break. When the line is later completed it SHALL be recorded once, as a complete line.

#### Scenario: Prompt without a line break
- **WHEN** a handed-back command prints `Continue? ` and nothing else
- **THEN** a result about it shows `Continue? ` as an unterminated last line

#### Scenario: Progress line
- **WHEN** a handed-back build rewrites a single progress line with carriage returns for 60 seconds
- **THEN** an `output` call during that time shows the current state of the progress line
