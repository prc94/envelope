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

An oracle is a plain command, or a JSON list of steps using `run`, `output`, `stop`, or `processes`.
Each step has an `arguments` object. `{{CODE}}` stands for the instruction's first code block;
`{{PYTHON}}` selects `python3` on Linux or `python` on Windows; and `<<name>>` is filled from the
answer key's `params`. `$handle` refers to the latest output handle. A step can save its handle
with `"save_as": "server"`, then later steps can use `"handle": "$server"`. Use
`"answer_from": true` on the step whose result supplies the final answer when later steps clean
up. `"expect_exit"` and `"expect_wait"` make an oracle fail if a command or wait had the wrong
outcome. Known-gap
oracles show the recovery a model needs (`gitbash-cp866-dir` tries the native `dir`, then lists with
git-bash's own `ls`), so the task is solvable while the gap is open.

Background fixtures should be portable between the Linux `bash` and Windows `git-bash` lanes.
Start servers with unbuffered Python so readiness appears promptly; bind to `127.0.0.1`, print the
port, and use a named process or saved handle for later reads and shutdown. Put values that are
meant to be discovered by probing in a runtime HTTP response rather than embedding them in a
fixture. The seven `background-processes` tasks exercise a server probe, long jobs, a watcher,
restart, port ownership, and explicit cleanup. Their oracles demonstrate how to continue after a
hand-back, including a 120-second wait followed by a longer wait for the four-minute job.
