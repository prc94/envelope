# pyright: basic, reportAttributeAccessIssue=false
"""Windows ctrl-break helper (design B7, spike E2): run as
``python -I -S -c <this source> PID [PID...]``, outside any job and with
``DETACHED_PROCESS`` so it holds no console of its own. ``AttachConsole``
only works for a process with no console already, which is why this must be
a separate short-lived process rather than code running in envelope itself.

Attaches to the first PID whose console it can attach to (the run's root
process, then any other live member of its job -- the root may already have
exited), enables ctrl-event handling for itself (a process ignores its own
console events by default), sends ``CTRL_BREAK_EVENT`` to the first PID
given (the run's process-group id, since the group was created with
``CREATE_NEW_PROCESS_GROUP``), and exits.

Exit codes: 0 = delivered; 1 = could not attach to any candidate's console;
2 = attached but ``GenerateConsoleCtrlEvent`` itself failed.
"""

import ctypes
import sys

CTRL_BREAK_EVENT = 1


def main():
    pids = [int(p) for p in sys.argv[1:]]
    if not pids:
        sys.exit(1)
    root = pids[0]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    attached = False
    for pid in pids:
        if kernel32.AttachConsole(pid):
            attached = True
            break
    if not attached:
        sys.exit(1)
    kernel32.SetConsoleCtrlHandler(None, True)
    ok = kernel32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, root)
    kernel32.FreeConsole()
    sys.exit(0 if ok else 2)


main()
