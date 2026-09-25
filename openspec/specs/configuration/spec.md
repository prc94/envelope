# Configuration Specification

## Purpose

Defines how envelope is configured and how it records what it does: configuration sources and precedence, validation, the audit log that records every request and decision, and diagnostic logging.

## Requirements

### Requirement: Configuration sources and precedence
Envelope SHALL read configuration from, in increasing precedence: built-in defaults; a user TOML file at the OS-standard per-user config location (or a path given by `--config` or `ENVELOPE_CONFIG`); environment variables prefixed `ENVELOPE_`; command-line flags. Envelope SHALL run with built-in defaults when no configuration file exists.

#### Scenario: Flag overrides file
- **WHEN** the config file sets the default timeout to 60 and envelope is started with `--default-timeout 30`
- **THEN** `run` calls without `timeout_s` use 30 seconds

#### Scenario: No config file
- **WHEN** envelope starts with no config file present
- **THEN** it runs with built-in defaults and logs the searched config paths at debug level

### Requirement: Configuration validation
Envelope SHALL validate configuration at startup. Unknown keys SHALL produce a warning naming the key; invalid values (wrong type, out of range, unknown shell identifiers, malformed patterns) SHALL cause envelope to exit with a non-zero status and an error on stderr naming the key, value and expected form.

#### Scenario: Invalid value
- **WHEN** the config sets `max_timeout_s = "forever"`
- **THEN** envelope exits with an error stating that `max_timeout_s` must be a positive number

#### Scenario: Unknown key
- **WHEN** the config contains `defualt_shell = "pwsh"`
- **THEN** envelope starts and logs a warning about the unknown key `defualt_shell`

### Requirement: Configurable settings
Configuration SHALL cover at least: default shell, custom shell definitions, default working directory, default and maximum timeout, concurrency limit, environment defaults and scrubbing patterns, output budget (characters and lines), spool cap, spool retention and total spool size, policy limits and audit-only mode, audit log location and enablement, and log level.

#### Scenario: Output budget configured
- **WHEN** the config sets the output budget to 6,000 characters
- **THEN** `run` results render at most 6,000 characters of output

### Requirement: Audit log
Envelope SHALL append one JSON object per line to an audit log (enabled by default, at an OS-standard per-user state location, configurable) for every tool call, containing: timestamp, client identity, tool name, arguments after coercion, coercions applied, normalized request, policy decision, and outcome (exit status, duration, output sizes, handle, hints, or error). Audit logging failures SHALL be reported on stderr and SHALL NOT fail the tool call.

#### Scenario: Audit record for a run
- **WHEN** `run` executes `echo hi`
- **THEN** the audit log gains one line containing the command, the decision `allow`, exit code 0 and the duration

#### Scenario: Audit log not writable
- **WHEN** the audit log path is not writable
- **THEN** envelope logs an error on stderr once and tool calls continue to succeed

### Requirement: Diagnostic logging
Envelope SHALL write diagnostic logs to stderr with a configurable level (`error`, `warn`, `info`, `debug`; default `warn`), and SHALL NOT emit MCP logging notifications.

#### Scenario: Debug level
- **WHEN** envelope is started with log level `debug`
- **THEN** shell detection details and protocol traffic summaries appear on stderr
