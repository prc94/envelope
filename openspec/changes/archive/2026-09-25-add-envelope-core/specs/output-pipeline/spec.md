## Purpose

Turns raw process output of any size and encoding into clean text, keeps the complete output retrievable on disk, and lets the model page, tail and search it through the `output` tool without flooding its context.

## ADDED Requirements

### Requirement: Separate capture with arrival ordering
Envelope SHALL capture stdout and stderr separately while recording the order in which chunks arrive, so that output can be rendered interleaved in arrival order or per stream.

#### Scenario: Interleaved rendering
- **WHEN** a command writes "a" to stdout, then "b" to stderr, then "c" to stdout, with pauses between
- **THEN** the rendered output lists a, b, c in that order with "b" marked as stderr

### Requirement: Decoding to UTF-8
Envelope SHALL decode output to Unicode text: UTF-8 by default; UTF-16 when a byte-order mark or a UTF-16 byte pattern is detected; the console OEM or ANSI code page for cmd and Windows PowerShell output that is not valid UTF-8. Undecodable bytes SHALL be replaced rather than causing failure, and the result SHALL note that replacement occurred.

#### Scenario: UTF-16 output
- **WHEN** a Windows tool writes UTF-16LE text with a byte-order mark
- **THEN** the rendered output shows the text correctly

#### Scenario: OEM code page output
- **WHEN** a cmd command outputs "Größe" encoded in code page 850
- **THEN** the rendered output shows "Größe"

### Requirement: Binary detection
When output appears to be binary (e.g. contains NUL bytes or a high proportion of non-text bytes), envelope SHALL NOT render it as text; it SHALL report the byte count and a detected type where recognizable, and keep the raw bytes in the spool.

#### Scenario: Command prints a PNG
- **WHEN** `run` executes `cat image.png`
- **THEN** the result states binary output of the given size (PNG) instead of rendering bytes

### Requirement: Output cleanup
Before rendering, envelope SHALL strip ANSI escape sequences, resolve carriage-return overwrites so only the final state of each line remains, apply backspaces, and fold runs of 3 or more identical consecutive lines into one line with a repeat count. The spool SHALL keep the raw, uncleaned bytes.

#### Scenario: Progress bar
- **WHEN** a command prints a progress bar updated 500 times with `\r`
- **THEN** the rendered output contains only the final progress line

#### Scenario: Repeated lines
- **WHEN** a command prints the same warning line 312 times consecutively
- **THEN** the rendered output shows that line once with a note that it repeated 312 times

### Requirement: Full output spooling
Envelope SHALL write the complete raw output of every `run` call to a per-user spool directory, up to a configurable cap per call (default 64 MiB), and SHALL assign the output a handle. When the cap is exceeded envelope SHALL keep the first and last portions and record the number of bytes dropped.

#### Scenario: Output larger than cap
- **WHEN** a command writes 200 MiB of output with a 64 MiB cap
- **THEN** the spool holds the beginning and end of the output and the result states that bytes were dropped from the middle of the spool

### Requirement: Budgeted truncation
The output rendered into a `run` result SHALL fit within a budget of at most 12,000 characters and 400 lines by default (configurable). When the cleaned output exceeds the budget envelope SHALL render the first lines and the last lines (favoring the tail), and SHALL insert an omission marker stating the number of lines and bytes omitted and the exact `output` call that retrieves them.

#### Scenario: Large test log
- **WHEN** a command prints 18,000 lines
- **THEN** the result shows head and tail lines within the budget and a marker such as `[... 17,640 lines omitted -- output(handle="out_91c2", offset=41, limit=200) or output(handle="out_91c2", grep="FAIL") ...]`

#### Scenario: Small output
- **WHEN** a command prints 20 lines
- **THEN** all 20 lines are rendered with no omission marker

### Requirement: The output tool
The `output` tool SHALL accept `handle` (required) and exactly one read mode: `tail` (last N lines), `offset` with optional `limit` (line range, 1-based), or `grep` (regular expression) with optional `context` lines; plus optional `stream` (`both`, `stdout`, `stderr`, default `both`). Without a read mode it SHALL return the tail. Results SHALL be cleaned and budgeted like `run` output and SHALL state the line numbers returned, the total line count, and for grep the number of matches and whether more matches exist beyond the budget.

#### Scenario: Grep with context
- **WHEN** `output` is called with `grep: "FAIL"` and `context: 2` on a spooled test log with 3 failures
- **THEN** the result shows the 3 matching lines with 2 lines of context each, labeled with line numbers

#### Scenario: Page through a range
- **WHEN** `output` is called with `offset: 1000` and `limit: 50`
- **THEN** lines 1000-1049 are returned with their line numbers and the total line count

#### Scenario: Invalid regex
- **WHEN** `output` is called with `grep: "(["`
- **THEN** envelope returns a tool error describing the invalid regular expression

### Requirement: Handle lifetime and retention
Output handles SHALL remain valid for the lifetime of the envelope process and for a configurable retention period afterwards (default 24 hours) as long as the spool file exists. Envelope SHALL delete spool files older than the retention period at startup and SHALL cap total spool size (default 1 GiB), evicting the oldest spools first. An unknown or expired handle SHALL produce a tool error saying so.

#### Scenario: Expired handle
- **WHEN** `output` is called with a handle whose spool was evicted
- **THEN** envelope returns a tool error stating the handle is unknown or expired
