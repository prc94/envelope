# Spec Delta

## MODIFIED Requirements

### Requirement: Lever and claim tags
Each task SHALL declare at least one tag naming a lever (a tunable envelope default or wording: output budget, head/tail split, tool descriptions, hint wording, argument coercion, default wait) or a claim (a behaviour envelope asserts over native shell tools: output recovery, long-running commands, background processes, the user's shell, encodings, lockdown hints, process cleanup). Canary tasks SHALL carry the `canary` tag. A task without tags SHALL be rejected when the suite is loaded.

#### Scenario: Untagged task rejected
- **WHEN** a task declares no tags
- **THEN** suite loading reports it as invalid

#### Scenario: Background task tagged
- **WHEN** a task requires starting a server and probing it
- **THEN** it carries the `background-processes` claim tag and is eligible for arms that declare that tag

### Requirement: Suite coverage
The suite SHALL contain, for every lane, at least one canary task and tasks for large output, long-running commands, errors and hints, heavy scripts, cwd/env parameters, and background processes (starting a server and probing it while it runs, following a job that outlives one tool call, restarting a process after a change, a port already in use, and stopping what was started). The Windows lane SHALL additionally cover, with git-bash as the primary shell: git (history search, large logs, a merge that would open an editor), log digging (multi-file tracing, time windows, stack traces, UTF-16LE and CP1251 logs, rotated compressed logs), file wrangling (bulk rename with spaces and Cyrillic names, duplicates, CSV transformation, CP1251 to UTF-8 conversion, long paths), scripts and grep pipelines, MongoDB and Redis command-line clients against remote servers (large result sets, authentication, arguments starting with `/`, a client started without arguments), CP866 output from native Windows tools alone and mixed with UTF-8 output, CRLF in pipelines, an executable blocked by application control surfacing as exit 126, and PowerShell and cmd edge cases (Constrained Language Mode, cmd's command-line limit, shell choice). A coverage report SHALL list every required area per lane with its tasks and flag areas without one.

#### Scenario: Coverage check
- **WHEN** the suite is loaded for the Windows lane
- **THEN** the coverage report lists every required area with the tasks that cover it and flags any area without a task

#### Scenario: Background coverage per lane
- **WHEN** the suite is loaded for the Linux lane without any task tagged `background-processes`
- **THEN** the coverage report flags the background-processes area as uncovered
