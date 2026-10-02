# Spec Delta

## MODIFIED Requirements

### Requirement: Every request is decided before execution
Envelope SHALL construct a normalized request for every attempt to execute something — at least for `run` commands, including a command started by `restart`, and with the request model also able to represent text sent to the standard input of a running process — and SHALL obtain a policy decision before executing it. No execution path SHALL bypass the decision. For `restart`, the decision on the new command SHALL be obtained before the running process is stopped, and a refusal SHALL leave the running process untouched. Actions that end or inspect processes already started — `stop`, eviction by the live-process cap, deadline and lifetime terminations, `output`, `processes`, and a `run` that joins an already running named process — execute nothing, SHALL NOT be subject to a decision, and SHALL be recorded in the audit log.

#### Scenario: Run is decided
- **WHEN** `run` is called
- **THEN** a policy decision is recorded in the audit log before the shell process is started

#### Scenario: Restart denied by policy
- **WHEN** a process named `web` runs in `C:\work`, allowed roots are `["C:\\work"]`, and `run` is called with `name: "web"`, `restart: true` and `cwd: "C:\\Windows"`
- **THEN** the request is denied, the result is a tool error, and the process named `web` is still running

### Requirement: Normalized request contents
The normalized request SHALL include: request kind (`exec` or `input`), shell identifier and executable path, the full command text, resolved working directory, the environment variables explicitly supplied by the caller, whether stdin text was supplied, the timeout (or that none applies), the process name and whether the request restarts a running process, and client identity where known.

#### Scenario: Audit shows normalized request
- **WHEN** `run` is called with `shell: "gitbash"` and `cwd: "/c/src"`
- **THEN** the audit record shows shell `git-bash`, its executable path and the working directory `C:\src`

#### Scenario: Request without a timeout
- **WHEN** `run` is called with `name: "web"` and no `timeout_s`, and no default timeout is configured
- **THEN** the normalized request shows no timeout and the name `web`
