"""The containment launcher's logic, exercised with its POSIX stand-in unit.

The Windows Job Object implementation is checked on the VM by running
`python eval/launcher/verify.py` there (see design notes); these tests cover the shared logic:
leftover listing and killing, timeouts that take the whole tree, exit codes, byte-preserving
transcripts, and that nothing outside the unit is touched.
"""

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
