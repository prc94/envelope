# model-gateway Specification

## Purpose

Gives every harness in every lane one model endpoint and a per-trial local token instead of real provider credentials, and records every model request with its trial identity, so evaluation traffic is manageable, attributable and reproducible while the provider key stays in one isolated process.

## Requirements

### Requirement: Key isolation
The gateway SHALL be the only process holding provider credentials. It SHALL run on a separate host from the runner, in an unprivileged container, under a dedicated operating-system user, with its key file readable only by that user, and SHALL use no third-party code. Harnesses, targets and the runner SHALL receive only gateway tokens.

#### Scenario: Runner cannot read the key
- **WHEN** the runner's user, or the account the runner uses on the gateway host, tries to read the gateway's key file
- **THEN** access is denied

#### Scenario: Runner account is limited to the gateway
- **WHEN** the runner's key is used to open a session on the gateway host
- **THEN** the only command it can run is the relay to the admin socket, and the only port it can forward is the gateway's

### Requirement: Local tokens bound to trials and models
The gateway SHALL authenticate requests with tokens it issues through an administrative interface that only the runner can reach (a Unix socket on the gateway host, reached through a relay that the runner's SSH key is restricted to). Each token SHALL carry the identity of its lane, job and trial and the set of models the trial may use (the model under test and any declared auxiliary models). A request with a missing, unknown or revoked token SHALL be rejected with HTTP 401 and SHALL NOT be forwarded. A request for a model outside the token's set SHALL be rejected, recorded and flagged.

#### Scenario: Unknown token
- **WHEN** a request arrives with a token the gateway did not issue
- **THEN** the gateway returns 401 and makes no upstream request

#### Scenario: Harness substitutes a model
- **WHEN** a harness requests a model that is not in its trial token's allowed set
- **THEN** the gateway rejects the request, records it with the requested model and flags the trial

### Requirement: Model availability under provider guardrails
The provider key may be restricted by guardrails (model and provider allowlists) set on the provider side. The gateway SHALL learn which models the key may use from the provider, as the key, and cache that list. It SHALL refuse to issue a token that allows a model outside the list, with an error naming the model, and SHALL offer an operation that returns the list and splits a given set of models into allowed and blocked. When the list cannot be obtained, nothing SHALL be blocked on that basis and the issue reply SHALL say the check was unavailable. A provider error that says the guardrail excluded every endpoint SHALL be recorded with the end reason `guardrail_blocked`, SHALL flag the trial and SHALL invalidate the cached list.

#### Scenario: Blocked model at token issue
- **WHEN** the runner asks for a token for a model the key's guardrail excludes
- **THEN** the gateway refuses and names the model, and the trial is reported `infra` with that reason

#### Scenario: Guardrail changed after the list was cached
- **WHEN** a request for a model in the cached list is rejected by the provider because of the guardrail
- **THEN** the request is recorded as `guardrail_blocked`, the trial is flagged, and the next token request for that model is checked against a fresh list

### Requirement: Upstream routing by API family
The gateway SHALL route requests by API family to a configured upstream and credential. The OpenAI Chat Completions family routed to OpenRouter SHALL be supported. Other families (Anthropic Messages, OpenAI Responses) and other upstreams SHALL be addable without changing the recording format; a request for an unconfigured family SHALL be rejected with an error naming the family.

#### Scenario: Unconfigured family
- **WHEN** a harness sends an Anthropic Messages request and no upstream is configured for that family
- **THEN** the gateway rejects it with an error naming the family and records the rejection

### Requirement: Streaming semantics
The gateway SHALL relay streamed responses incrementally as upstream data arrives. It SHALL distinguish content from keep-alive comments when timing a stream, and SHALL detect errors delivered inside a stream after a successful HTTP status. It SHALL request usage reporting from the upstream and capture it from the stream; when usage or other metadata is unavailable, it SHALL record the field as unknown with a reason rather than a default value.

#### Scenario: Keep-alive only
- **WHEN** an upstream stream sends only keep-alive comments for two minutes
- **THEN** the stream is recorded as idle for that time, although bytes were received

#### Scenario: Error inside a stream
- **WHEN** an upstream stream returns HTTP 200 and later sends an error event
- **THEN** the request is recorded as failed with the error, not as successful

### Requirement: Request recording
For every request the gateway SHALL record: timestamp, token identity, API family, whether it is a main or auxiliary request (auxiliary: a declared auxiliary model, or a request without tools while the trial's main requests carry tools), requested model, served model and provider as reported upstream, sampling parameters, hashes of the tool definitions and system prompt, prompt, completion, cached and reasoning tokens, cost when reported, time to first content, time of last content, total duration, end reason and error. Request and response bodies SHALL be recorded only when the job enables it, with secrets redacted. Credentials and authorization headers SHALL never be recorded.

#### Scenario: Title-generation call
- **WHEN** a harness makes one title-generation request with its declared small model and three tool-calling requests
- **THEN** the record has one auxiliary and three main requests, and turn counts use the three main requests

### Requirement: Transient upstream errors
The gateway SHALL absorb transient upstream errors before any byte of the response reaches the harness: rate limits (429), server errors (500, 502, 503, 504) and connections that fail before a response, by retrying with bounded exponential backoff that honours Retry-After, within a per-request limit on attempts and on total waiting. Every absorbed attempt SHALL be recorded (attempt count, statuses, waiting time). When the limits are exhausted, the last upstream response SHALL be relayed unchanged. Errors that are not transient (client errors, guardrail rejections) SHALL NOT be retried, and an error delivered inside a stream after content started SHALL NOT be retried.

#### Scenario: Rate limit absorbed
- **WHEN** the upstream answers 429 twice and then succeeds
- **THEN** the harness receives the success, and the record shows three attempts, the two statuses and the time waited

#### Scenario: Retries exhausted
- **WHEN** the upstream keeps answering 503 past the retry limit
- **THEN** the harness receives that 503 and the record shows every attempt

### Requirement: Provider pinning
When a job pins providers for a model, the gateway SHALL add the provider order with fallbacks disabled to each upstream request for that model, so a request fails rather than being served by an unpinned provider.

#### Scenario: Pinned provider unavailable
- **WHEN** the pinned provider for a model is down
- **THEN** the upstream request fails, the error is returned to the harness and recorded, and no other provider serves it

### Requirement: Served-model verification
The gateway SHALL flag any response whose served model differs from the requested model after the job's declared aliases.

#### Scenario: Upstream substitution
- **WHEN** an upstream serves a different model than requested
- **THEN** the request record is flagged as a model mismatch

### Requirement: Trial state signal
The gateway SHALL expose to the runner, per token: whether a request is in flight; for an in-flight request, the time since its last content; and otherwise the time since the last request finished. This distinguishes an idle upstream (request in flight, no content) from an idle harness (no request in flight).

#### Scenario: Idle harness
- **WHEN** a harness has made no request for five minutes and none is in flight
- **THEN** the runner reads "no request in flight" and the time since the last one for that trial's token

### Requirement: Loop guard
The gateway SHALL enforce a per-trial cap on main requests, configurable per job; requests beyond it SHALL be rejected with an error the harness shows to the model and SHALL be recorded as a runaway.

#### Scenario: Runaway trial
- **WHEN** a trial's model keeps issuing tool calls past the cap
- **THEN** further requests are rejected and the trial record shows the runaway

### Requirement: Network exposure
The gateway SHALL listen only on loopback of its own host and the runner SHALL reach it through an SSH forward bound to loopback on the runner host (and, for Docker targets, on the Docker bridge address). Every server the evaluation starts (for example MongoDB and Redis) SHALL listen only on loopback or the Docker bridge address of the runner host. `ssh` targets SHALL reach these only through reverse tunnels bound to loopback on the target, which fail at setup if the forward cannot be established.

#### Scenario: LAN connection refused
- **WHEN** another machine on the LAN connects to the gateway port of the gateway host, or to the database ports of the runner host or of a target
- **THEN** the connection is not accepted

### Requirement: Credential scan
The gateway SHALL provide an operation that scans a directory for its provider credentials and reports the files and offsets of any occurrence without revealing the credential. Because the results live on the runner host, the runner SHALL send the directory to the gateway as a stream and the gateway SHALL scan it without extracting it.

#### Scenario: Key found
- **WHEN** a result directory contains the provider key in a log file
- **THEN** the scan reports that file and offset and the runner fails the job
