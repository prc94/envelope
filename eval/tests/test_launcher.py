"""The containment launcher's logic, exercised with its POSIX stand-in unit.

The Windows Job Object implementation is checked on the VM by running
`python eval/launcher/verify.py` there (see design notes); these tests cover the shared logic:
leftover listing and killing, timeouts that take the whole tree, exit codes, byte-preserving
transcripts, and that nothing outside the unit is touched.
"""

from pathlib import Path

import pytest

from launcher import verify


@pytest.mark.skipif(not __import__("sys").platform.startswith("linux"), reason="needs /proc")
@pytest.mark.parametrize(
    "check", [verify.check_clean_exit, verify.check_leftover, verify.check_timeout]
)
def test_launcher_checks(check, tmp_path):
    check(tmp_path)


@pytest.mark.skipif(not __import__("sys").platform.startswith("linux"), reason="needs /proc")
def test_launcher_self_check_leaves_outside_processes_alone():
    assert verify.main() == 0


def test_launcher_delivers_stdin_bytes_without_changing_the_task(tmp_path):
    import sys

    from envelope_eval.targets.local import LocalTrialTarget

    target = LocalTrialTarget(tmp_path / "target", python=sys.executable)
    ws = target.prepare("stdin-check")
    Path(ws.workspace).mkdir()
    task = '  --help\nseq 1 20000; printf "готово\\n"\n'.encode()
    source = tmp_path / "instruction.txt"
    source.write_bytes(task)
    remote = ws.state + "/instruction.txt"
    output = ws.state + "/transcript.jsonl"
    target.upload(source, remote)
    result = target.run_unit(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        env=ws.env,
        cwd=ws.workspace,
        timeout=10,
        stdout_to=output,
        stdin_from=remote,
    )
    assert result.exit_code == 0 and not result.timed_out
    assert Path(output).read_bytes() == task
    target.cleanup(ws)
