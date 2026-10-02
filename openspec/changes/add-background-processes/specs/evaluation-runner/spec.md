# Spec Delta

## MODIFIED Requirements

### Requirement: Tool sets
Each arm SHALL define the complete tool set exposed to the model: the shell tool (envelope's tools in envelope arms; in the baseline, the harness's native shell tool together with, when the job declares them, the harness's own tools for waiting on and stopping processes that native shell tool started in the background) and the non-shell harness tools. The non-shell tool set SHALL be identical across arms of a job and SHALL default to none (file reading, searching, editing, web access and sub-agents disabled). Before each trial's agent phase, the runner SHALL verify from the harness's own report of available tools that exactly the configured tool set is exposed; a mismatch SHALL make the trial `infra` and SHALL NOT be scored.

#### Scenario: Built-in grep disabled
- **WHEN** a trial runs a log-digging task in an envelope arm with the default tool set
- **THEN** the harness exposes no file-search tool and the model can only solve the task through envelope

#### Scenario: Unexpected tool exposed
- **WHEN** a harness update re-enables a file-reading tool despite the configuration
- **THEN** the tool-set check fails, the trial is `infra`, and the report names the unexpected tool

#### Scenario: Native background tools in the baseline
- **WHEN** a job's Qwen Code baseline arm declares the harness's background-task stop and monitor tools for background-process tasks
- **THEN** those tools are exposed in that arm only, the tool-set check expects them, and envelope arms of the same job expose envelope's four tools

### Requirement: Invariants and lever metrics
For every trial, independently of the verdict, the runner SHALL record: leftover processes; fixture files created by the agent; whether the model answered without any tool call; tool calls referencing output handles envelope never issued; argument coercions and tool errors from envelope's audit log; for each shell tool result, whether it was truncated, whether the model then retrieved omitted output through `output` and whether that succeeded, which hints were shown and whether the next command changed, and the number of replacement characters; for envelope arms, the number of `run` results handed back while running, the waits requested with their conditions and outcomes, `stop` and `processes` calls, evictions, completion notices shown, and the envelope-managed processes still running when the agent phase ended; the number of tool-discovery calls before the first shell tool call and whether the first discovery call returned the shell tool, reported separately from model turns; and main-model turns and tokens from the gateway.

#### Scenario: Truncation recovery recorded
- **WHEN** a `run` result is truncated and the model then calls `output` with a grep that returns the needed line
- **THEN** the trial record shows one truncated result followed by one successful recovery

#### Scenario: Server left running
- **WHEN** the model starts a server through envelope, probes it, and ends without stopping it
- **THEN** the trial record shows one handed-back process still running at the end of the agent phase
