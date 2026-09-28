# Authoring evaluation tasks

A task directory contains `task.toml`, `instruction.md`, `generate.py`, `collect.py`,
`judge.py`, and an executable `oracle`. Metadata declares lanes, shells, tags,
baseline eligibility, known gaps, modifiable fixture paths, and a timeout. Tags must
name a lever or claim; canaries also carry `canary`. Generators run on the host with
the trial seed and keep answer keys on the host. Only generated fixture bytes are
uploaded. Collect emits JSON on the target (`python -X utf8`); judge consumes it on
the host. Keep fixture hashes in the manifest and use `scratch/` for temporary files.
Baseline instructions are tool-neutral: put literal commands in fenced blocks or
fixture files, never prose. Declare known gaps by id and write an oracle that passes
its own judge before scheduling model trials.

Answer keys the agent must never see (an answer, a seed) can be declared by returning
`{"secrets": ["..."], ...}` from `generate`. Before the agent starts, the runner searches the
trial's directories on the target for those strings and fails the trial as `infra`, naming
files only, if any is found.

An oracle is a plain command, or a JSON list of steps `{"tool": "run"|"output", "arguments": {...}}`;
`{{CODE}}` stands for the instruction's first code block and `$handle` for the previous step's output
handle, and `<<name>>` is filled from the answer key's `params` like in the instruction. Known-gap
oracles show the recovery a model needs (`gitbash-cp866-dir` tries the native `dir`, then lists with
git-bash's own `ls`), so the task is solvable while the gap is open.
