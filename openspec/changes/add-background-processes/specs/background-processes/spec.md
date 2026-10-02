# Spec Delta

## Purpose

Lets a command outlive the tool call that started it: the model gets a handle back instead of a blocked call, can wait for a condition, read new output, list and stop processes, and envelope bounds how many such processes live and how long.

## ADDED Requirements

### Requirement: Yielding a running command
`run` SHALL wait for the command for at most `wait_s` seconds (default configurable, 30 s; clamped to a configurable maximum, default 300 s; `0` returns as soon as the command has started). A command that finishes within the wait SHALL produce the same result as a command that was never handed back. A command still running when the wait ends SHALL keep running, and `run` SHALL return a result stating that the command is still running and must not be started again, its handle, the elapsed time, the output so far (budgeted like any result, including an unterminated last line), and the exact `output` and `stop` calls that continue from there. Returning a running result SHALL NOT be a tool error. When `run` starts an unnamed command while a live process with the same command text, shell and working directory exists, the result SHALL note that process's handle.

#### Scenario: Short command is unaffected
- **WHEN** `run` executes `echo hi` with the default wait
- **THEN** the result reports exit code 0 and `hi`, with no handle continuation text

#### Scenario: Long build is handed back
- **WHEN** `run` executes a command that prints progress for 90 seconds, with the default 30-second wait
- **THEN** the call returns after about 30 seconds with a running result naming the handle and the progress printed so far, and the command continues

#### Scenario: Start without waiting
- **WHEN** `run` executes `python -m http.server 8000` with `wait_s: 0`
- **THEN** the call returns immediately with a running result and the server keeps running

#### Scenario: Requested wait above the maximum
- **WHEN** `run` is called with `wait_s: 5000` and the configured maximum is 300
- **THEN** the call waits at most 300 seconds and the result notes the clamping

#### Scenario: Same command started again
- **WHEN** `npm install` was handed back as `out_1a2b` and is still running, and `run` executes `npm install` again in the same shell and directory
- **THEN** the second command starts and its result notes that `out_1a2b` is still running the same command

### Requirement: Process handles and names
The output handle of a `run` call SHALL also identify its process for `output`, `stop` and `processes`. `run` SHALL accept an optional `name` (1 to 40 characters from letters, digits, `.`, `_` and `-`, not of the form of an output handle). While a process with that name is running, a `run` call with the same name SHALL NOT start a new process; it SHALL treat the call as a wait on the running process (honouring `wait_s` and `wait_for`) and return its status and handle, noting when the requested command differs from the running one. With `restart: true` such a call SHALL first validate and authorize the requested command exactly like a new `run`; only if that succeeds SHALL it stop the running process as `stop` does and then start the requested command under the same name, and if it fails the running process SHALL be left untouched. When no process with the name is running, the call SHALL start the command and assign the name to it. Concurrent calls naming the same process SHALL behave as if executed one after another: at most one process SHALL run under a name at any time. `output` and `stop` SHALL accept a name wherever they accept a handle; a name refers to the most recent process started under it.

#### Scenario: Idempotent start
- **WHEN** `run` is called twice with `name: "web"` and `command: "npm run dev"` while the first process is still running
- **THEN** only one dev server process exists and the second result reports the first process's handle and status

#### Scenario: Parallel idempotent starts
- **WHEN** a client issues two `run` calls with `name: "web"` in parallel and no process named `web` exists
- **THEN** exactly one process is started and both results report its handle

#### Scenario: Restart with a changed command
- **WHEN** a process named `web` is running and `run` is called with `name: "web"`, `restart: true` and a different command
- **THEN** the old process tree is stopped, the new command starts under the name `web`, and the result reports the new handle

#### Scenario: Denied restart leaves the process running
- **WHEN** a process named `web` is running and a `restart: true` call names a working directory that does not exist
- **THEN** the result is a tool error about the directory and the process named `web` is still running

#### Scenario: Name after exit
- **WHEN** a process named `tests` exited with code 1 and `output` is called with `handle: "tests"`
- **THEN** the output of that process is returned with its exit status

#### Scenario: Invalid name
- **WHEN** `run` is called with `name: "my server!"` or `name: "out_1234abcd"`
- **THEN** envelope returns a tool error stating the allowed names

### Requirement: Wait conditions
`run` and `output` SHALL accept `wait_for` with one of: `exit` (the process has finished), `regex:<pattern>` (a line, including an unterminated last line, matching the regular expression appears on either stream), `port:<n>` (a process of this command's tree listens on TCP port n on any local address), or `idle:<seconds>` (no output has arrived for that many seconds, measured from the later of the start of the wait and the last output). Common spellings SHALL be coerced and the coercion recorded: `port 3000`, `:3000`, `localhost:3000` and `http://localhost:3000` as `port:3000`, and a bare regular expression prefixed with `regex` and a space. The default condition SHALL be `exit`. A wait SHALL end as soon as its condition is met, the process finishes, or `wait_s` elapses, whichever comes first, and the result SHALL state which of the three occurred. For `run`, `regex:` SHALL consider all output of the command; for `output`, it SHALL consider the output unread when the call starts and any output arriving during the wait. Where the platform does not reveal which process owns a listening socket, `port:` SHALL be met by a successful TCP connection to the local host on port n and the result SHALL state that ownership was not verified. A malformed condition or regular expression SHALL produce a tool error; a condition that was not met SHALL NOT.

#### Scenario: Wait for a server to listen
- **WHEN** `run` executes a dev server with `wait_for: "port:3000"` and the server starts listening after 4 seconds
- **THEN** the call returns after about 4 seconds, states that the condition was met, and the server keeps running

#### Scenario: Wait for a log line
- **WHEN** `output` is called on a running watcher with `wait_for: "regex:compiled (successfully|with errors)"` and `wait_s: 60`, and the line appears after 12 seconds
- **THEN** the call returns after about 12 seconds with the unread output up to and including that line

#### Scenario: Matching line already unread
- **WHEN** a watcher printed `compiled successfully` after the last read and `output` is then called with `wait_for: "regex:compiled"`
- **THEN** the call returns immediately with the condition met

#### Scenario: Idle measured within the wait
- **WHEN** a server has printed nothing for 5 minutes and `output` is called with `wait_for: "idle:10"`
- **THEN** the call returns after about 10 seconds with the condition met

#### Scenario: Process exits before the condition
- **WHEN** `run` executes a server with `wait_for: "port:3000"` and the server exits with an error after 2 seconds
- **THEN** the call returns after about 2 seconds with the exit status, the output, and a statement that the process finished before the condition was met

#### Scenario: Wait expires
- **WHEN** `output` is called with `wait_for: "exit"` and `wait_s: 10` on a build that needs another minute
- **THEN** the call returns after about 10 seconds with the new output, states that the wait expired, and is not a tool error

#### Scenario: Port owned by another process
- **WHEN** port 3000 is already held by a process outside this command's tree and `run` starts a server with `wait_for: "port:3000"` on a platform that reports socket owners
- **THEN** the condition is not met by the foreign listener

#### Scenario: Coerced port spelling
- **WHEN** `run` is called with `wait_for: "http://localhost:5173"`
- **THEN** the wait uses `port:5173` and the audit log records the coercion

### Requirement: Reading unread output
Envelope SHALL keep, per process, a read position marking the output already returned to the model by `run` or `output` results. `output` on a handle SHALL, when no read mode (`tail`, `offset`, `grep`) is given and unread output exists, return the unread lines and advance the read position; when nothing is unread it SHALL return the tail as before. Explicit read modes SHALL NOT move the read position. The read position SHALL advance to the last line a result shows; lines omitted from the middle of a budgeted result are not returned again as unread output, and the result's omission marker SHALL state how to retrieve them. When unread lines were dropped from the spool, the result SHALL state how many were dropped and continue with the first retained line, and no omission marker SHALL point into dropped lines.

#### Scenario: Follow a running build
- **WHEN** a build was handed back after printing 120 lines and `output` is called later, after 80 more lines were printed
- **THEN** the result contains lines 121-200 and states the line numbers returned

#### Scenario: Nothing new
- **WHEN** `output` is called twice in a row on a finished process without read modes
- **THEN** the second result shows the tail of the output

#### Scenario: Unread lines dropped
- **WHEN** a chatty server's unread output exceeded the spool's tail portion so that lines 500-9,000 were dropped
- **THEN** the unread result states that lines 500-9,000 were dropped and shows lines from 9,001 on

### Requirement: The stop tool
The `stop` tool SHALL accept `handle` (a handle or a name, required) and optional `grace_s` (default configurable, 5 s; clamped to a configurable maximum, default 60 s). For a running process it SHALL, where the platform can deliver one, request termination of every process in the command's tree (SIGTERM on POSIX; on Windows a CTRL+BREAK console event, only where envelope can deliver it through its launch chain), wait up to `grace_s` for the tree to exit, then forcibly terminate whatever remains; where no termination request can be delivered it SHALL terminate the tree forcibly at once. The result SHALL state the final exit status, whether the stop was graceful or forced, and the unread output (budgeted). For a process that already finished it SHALL return its exit status without error. A `stop` whose call is cancelled SHALL still complete. While a stop is in progress, envelope SHALL NOT report the tree's remaining processes as background processes killed at completion.

#### Scenario: Graceful stop on POSIX
- **WHEN** `stop` is called on a running server on Linux that exits cleanly on SIGTERM within 1 second
- **THEN** the result reports the server's exit status, states that the stop was graceful, and no process of its tree remains

#### Scenario: Unresponsive process
- **WHEN** `stop` is called with `grace_s: 2` on a process that ignores the termination request
- **THEN** after about 2 seconds the whole tree is forcibly terminated and the result states so

#### Scenario: Forced stop where no request can be delivered
- **WHEN** `stop` is called on Windows for a command whose tree cannot receive a CTRL+BREAK event from envelope
- **THEN** the tree is forcibly terminated at once, the result states that the stop was forced, and no process of the tree remains

#### Scenario: Already finished
- **WHEN** `stop` is called on a handle whose process exited with code 0
- **THEN** the result reports exit code 0 and is not a tool error

### Requirement: The processes tool
The `processes` tool SHALL list processes started by envelope in this server lifetime: by default those still running and those that finished without the model having seen their final status; with `all: true`, every one whose output handle is still valid. Running processes SHALL be listed first, then the most recently finished; the listing SHALL be limited to a configurable number of entries (default 20) and SHALL state how many were left out. Each entry SHALL show the handle, the name if any, the state (running, or exit status and duration), the uptime, the TCP ports its tree listens on where the platform reveals them, the command (shortened) and the last output line (shortened). The tool SHALL be read-only and SHALL be recorded in the audit log.

#### Scenario: Server and finished test run
- **WHEN** a server named `web` is running and listening on port 3000, and a test run handed back earlier exited with code 1 unseen
- **THEN** `processes` lists `web` as running with port 3000 and the test run with exit code 1

#### Scenario: Nothing running
- **WHEN** `processes` is called and no process was handed back
- **THEN** the result states that no processes are running

#### Scenario: Long session
- **WHEN** 60 processes finished during the session and `processes` is called with `all: true` and the default limit
- **THEN** 20 entries are listed and the result states that 40 more exist

### Requirement: Completion notices
When a process that was handed back finishes and the model has not seen its final status, exactly one later result of an envelope tool SHALL carry a note naming its handle, its name if any, its command (shortened), how it ended (exit status, timeout, lifetime limit, eviction) and its duration, even when several tool calls complete concurrently. A process whose final status the model saw through `run`, `output`, `stop` or `processes`, or whose eviction was reported in a result, SHALL NOT produce a notice.

#### Scenario: Notice on an unrelated call
- **WHEN** a handed-back test run exits with code 1 and the model then calls `run` with `git status`
- **THEN** the `git status` result carries one note stating that the test run exited with code 1, and later results carry no such note

#### Scenario: Concurrent results
- **WHEN** a handed-back process finishes while two tool calls return at the same moment
- **THEN** exactly one of the two results carries its notice

### Requirement: Live-process cap
Envelope SHALL allow at most a configurable number of live processes at once (default 16), counting every started command until its whole tree has exited, including processes being stopped. When starting a command would exceed the cap, envelope SHALL, with the default eviction policy, stop the least recently used running process that has no name and on which no call is waiting (as `stop` does), wait for its tree to exit, then start the new command and note the evicted handle in the new command's result; if no process is evictable, or the eviction policy is `refuse`, the new command SHALL NOT start and the result SHALL be a tool error listing the running processes and how to stop one. Concurrent starts SHALL NOT together exceed the cap. A process is used when it is started, read, waited on (at the start and at the end of the wait) or restarted; listing it does not count as use.

#### Scenario: Oldest unnamed process evicted
- **WHEN** 16 unnamed processes are running, none waited on, and `run` starts another command
- **THEN** the least recently used of them is stopped, the new command starts after its tree has exited, and the result names the evicted handle

#### Scenario: Only named or awaited processes running
- **WHEN** 16 processes are running, each either named or being waited on by an `output` call, and `run` starts another command
- **THEN** the command does not start and the tool error lists the 16 processes

#### Scenario: Concurrent starts at the cap
- **WHEN** the cap is 2, one process is running, and two `run` calls start commands at the same moment
- **THEN** at no time do more than 2 processes of started commands exist

### Requirement: Lifetime of handed-back processes
A handed-back process SHALL run until it exits, is stopped, is evicted, reaches its explicit `timeout_s`, reaches the configured maximum lifetime (default: none), or envelope exits. When a timeout or the maximum lifetime ends a process, its tree SHALL be terminated and the completion notice SHALL state the reason.

#### Scenario: No implicit timeout
- **WHEN** a server is started with `wait_s: 0` and no `timeout_s` and runs for 20 minutes
- **THEN** it is still running and listed by `processes`

#### Scenario: Explicit deadline on a handed-back build
- **WHEN** a build is started with `timeout_s: 60`, handed back after 30 seconds, and still running at 60 seconds
- **THEN** its tree is terminated at about 60 seconds and the next result carries a notice that it timed out

### Requirement: Quiet-process and console-prompt hints
When a running process has produced no output for a configurable period (default 10 s), results about it (`run`, `output`, `processes`) SHALL be able to carry at most one of these hints, each only when all of its conditions are observed, and SHALL carry none when a condition cannot be observed:
- **no output yet:** the process has produced no output since it started, uses no CPU over the quiet period, and listens on no port — noting that its output may be held in a buffer because it is not attached to a terminal;
- **console prompt (Windows):** the text of the command's console, where envelope can read it, ends with a line shaped like a prompt (for example ending in `:`, `?` or `[y/N]`, or containing `password`), and the process uses no CPU over the quiet period — quoting that line and stating that the process appears to wait for console input that nobody can type.

On POSIX no waiting-for-input hint SHALL be given, since commands can neither read a terminal nor block on standard input. Envelope SHALL NOT stop or signal a process because of these assessments.

#### Scenario: Buffered output
- **WHEN** a handed-back program that block-buffers its output has printed nothing, uses no CPU and listens on no port for 10 seconds
- **THEN** the next `output` result carries the no-output-yet hint mentioning buffering, and the process keeps running

#### Scenario: Console password prompt on Windows
- **WHEN** a handed-back `ssh` command writes `user@host's password:` to its console and waits, and envelope can read the console text
- **THEN** the next `output` result quotes that line in a hint that the process appears to wait for console input

#### Scenario: Quiet server
- **WHEN** a server listening on port 8000 has printed nothing for 5 minutes
- **THEN** no quiet-process or console-prompt hint is shown for it
