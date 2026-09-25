# Execution Policy Specification

## Purpose

Provides a single decision point that every execution request passes through before anything runs, so that a rich, configurable security classifier can later be added without changing the rest of envelope; this change ships the seam, the request model and basic limits.

## Requirements

### Requirement: Every request is decided before execution
Envelope SHALL construct a normalized request for every attempt to execute something — at least for `run` commands, and with the request model also able to represent text sent to the standard input of a running process — and SHALL obtain a policy decision before executing it. No execution path SHALL bypass the decision.

#### Scenario: Run is decided
- **WHEN** `run` is called
- **THEN** a policy decision is recorded in the audit log before the shell process is started

### Requirement: Normalized request contents
The normalized request SHALL include: request kind (`exec` or `input`), shell identifier and executable path, the full command text, resolved working directory, the environment variables explicitly supplied by the caller, whether stdin text was supplied, timeout, and client identity where known.

#### Scenario: Audit shows normalized request
- **WHEN** `run` is called with `shell: "gitbash"` and `cwd: "/c/src"`
- **THEN** the audit record shows shell `git-bash`, its executable path and the working directory `C:\src`

### Requirement: Decision outcomes
A decision SHALL be one of `allow`, `ask` or `deny`, and SHALL carry the identifier of the rule that produced it and one or more human-readable reasons. `allow` SHALL execute the request. `deny` SHALL refuse it. `ask` SHALL refuse it in this change (clients in scope do not support user elicitation) and state that user approval is required and how to grant it through configuration.

#### Scenario: Ask without elicitation support
- **WHEN** a configured limit yields `ask` for a request
- **THEN** the command is not executed and the tool error explains that approval is required and which configuration change grants it

### Requirement: Default policy and basic limits
The default policy SHALL allow all requests, subject to these configurable limits which SHALL yield `deny` when violated: allowed shells (default: all available), allowed working-directory roots (default: unrestricted), and maximum command length in characters (default: 1,000,000).

#### Scenario: Working directory outside allowed roots
- **WHEN** allowed roots are `["C:\\work"]` and `run` is called with `cwd: "C:\\Windows"`
- **THEN** the request is denied with a reason naming the allowed roots

#### Scenario: Default configuration
- **WHEN** no policy configuration is present and `run` executes `rm -rf ./build`
- **THEN** the request is allowed

### Requirement: Audit-only mode
Configuration SHALL support an audit-only mode in which decisions are computed and logged but `ask` and `deny` do not block execution; results in this mode SHALL state the decision that would have applied.

#### Scenario: Audit-only denial
- **WHEN** audit-only mode is enabled and a request violates the allowed shells limit
- **THEN** the command executes and both the audit log and the result note state it would have been denied
