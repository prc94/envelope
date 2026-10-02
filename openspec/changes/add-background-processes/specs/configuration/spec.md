# Spec Delta

## MODIFIED Requirements

### Requirement: Configurable settings
Configuration SHALL cover at least: default shell, custom shell definitions, default working directory, default timeout (none unless set) and maximum timeout, default and maximum wait, the live-process cap and its eviction policy (`lru-unnamed` or `refuse`), the maximum lifetime of a process (none unless set), the default and maximum stop grace period, the quiet period before a process is assessed for the quiet-process and console-prompt hints, the number of entries `processes` lists, environment defaults and scrubbing patterns, output budget (characters and lines), spool cap, spool retention and total spool size, policy limits and audit-only mode, audit log location and enablement, and log level. The former concurrency-limit key `max_concurrency` SHALL be accepted as a deprecated alias of the live-process cap `max_running`, with a warning naming the new key: a value given under the alias SHALL be treated as a value for `max_running` from the same source, so the usual precedence of sources applies; giving both keys in the same source SHALL be a configuration error.

#### Scenario: Output budget configured
- **WHEN** the config sets the output budget to 6,000 characters
- **THEN** `run` results render at most 6,000 characters of output

#### Scenario: Wait and cap configured
- **WHEN** the config sets `wait_default_s = 10` and `max_running = 4`
- **THEN** `run` calls without `wait_s` return a running result after about 10 seconds, and starting a fifth concurrent process evicts or refuses according to the eviction policy

#### Scenario: Deprecated concurrency key
- **WHEN** the config file sets `max_concurrency = 12` and nothing sets `max_running`
- **THEN** envelope starts with a live-process cap of 12 and logs a warning that `max_concurrency` is deprecated in favour of `max_running`

#### Scenario: Alias and new key from different sources
- **WHEN** the config file sets `max_concurrency = 12` and the environment sets `ENVELOPE_MAX_RUNNING=6`
- **THEN** the live-process cap is 6

#### Scenario: Alias and new key in one source
- **WHEN** the config file sets both `max_concurrency = 12` and `max_running = 6`
- **THEN** envelope exits with an error naming both keys

### Requirement: Audit log
Envelope SHALL append one JSON object per line to an audit log (enabled by default, at an OS-standard per-user state location, configurable) for every tool call, containing: timestamp, client identity, tool name, arguments after coercion, coercions applied, normalized request, policy decision, and outcome (process state and end reason, exit status, duration, output sizes, handle and name, wait condition and outcome, evicted handles, hints, or error). Tool calls that execute nothing (`output`, `stop`, `processes`, and a `run` that joined an already running named process) SHALL record that no policy decision applies instead of a decision. Envelope SHALL also append a `process_end` record whenever a handed-back process ends outside any tool call (exit, timeout, lifetime limit, eviction, shutdown), with its handle, name, end reason, exit status and duration, and SHALL flush pending records before it exits. Audit logging failures SHALL be reported on stderr and SHALL NOT fail the tool call.

#### Scenario: Audit record for a run
- **WHEN** `run` executes `echo hi`
- **THEN** the audit log gains one line containing the command, the decision `allow`, exit code 0 and the duration

#### Scenario: Audit log not writable
- **WHEN** the audit log path is not writable
- **THEN** envelope logs an error on stderr once and tool calls continue to succeed

#### Scenario: Handed-back process ends
- **WHEN** a handed-back build exits with code 2 while no call is waiting on it
- **THEN** the audit log gains a `process_end` record with the build's handle, end reason `exit`, exit code 2 and its duration

#### Scenario: Shutdown with live processes
- **WHEN** the client closes envelope's stdin while two handed-back servers run
- **THEN** the audit log contains a `process_end` record with end reason `shutdown` for each server before envelope exits
