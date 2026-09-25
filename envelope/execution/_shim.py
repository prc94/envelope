# pyright: basic, reportAttributeAccessIssue=false
"""POSIX launch shim (run as ``python -I -S -c <this source> STATUS_FD PARENT_PID -- ARGV``).

The shim sits between envelope and the shell so a command's whole process tree
can be killed, including after envelope itself dies:

* Linux: the shim is a child subreaper (orphaned descendants re-parent to it,
  so they stay findable) and has PDEATHSIG=SIGTERM (it learns when envelope
  dies). Killing walks /proc for every descendant of the shim.
* macOS: a kqueue NOTE_EXIT watch on envelope's PID replaces PDEATHSIG, and
  the kill uses the process group (descendants that setsid() away escape; this
  is the documented macOS limitation).

Status lines written to STATUS_FD: ``pid N``, ``error ERRNO MESSAGE``,
``exit CODE``, ``signal N`` (shell finished) and ``done`` (shim exiting).
SIGTERM to the shim means "terminate the tree now".
"""

import os
import signal
import sys


class _Terminate(Exception):
    pass


def _status(fd, text):
    try:
        os.write(fd, (text + "\n").encode())
    except OSError:
        pass


def _descendants_linux(root):
    children = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/" + entry + "/stat", "rb") as handle:
                stat = handle.read()
        except OSError:
            continue
        # Field 4 (ppid) follows the parenthesized command name, which may contain spaces.
        fields = stat[stat.rfind(b")") + 2 :].split()
        children.setdefault(int(fields[1]), []).append(int(entry))
    found = []
    stack = [root]
    while stack:
        for child in children.get(stack.pop(), ()):
            found.append(child)
            stack.append(child)
    return found


def _reap_nohang():
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return False
        if pid == 0:
            return True


def _kill_tree(linux):
    """Kill every descendant. On macOS this also kills the shim itself."""
    if not linux:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.killpg(0, signal.SIGKILL)
        return
    import time

    me = os.getpid()
    for _ in range(1000):
        victims = _descendants_linux(me)
        if not victims:
            return
        for pid in victims:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        _reap_nohang()
        time.sleep(0.002)


def main():
    status_fd = int(sys.argv[1])
    parent = int(sys.argv[2])
    argv = sys.argv[4:]
    linux = sys.platform.startswith("linux")

    terminating = []

    def on_term(signum, frame):
        # Raise only once: a second SIGTERM (it happens, e.g. parent death
        # plus an explicit terminate) must not abort the tree kill in progress.
        if not terminating:
            terminating.append(signum)
            raise _Terminate()

    # SIGTERM stays blocked until the wait loop below can handle it: a
    # termination request (or parent death) arriving earlier stays pending
    # instead of raising outside the try block and skipping the tree kill.
    term = {signal.SIGTERM}
    signal.pthread_sigmask(signal.SIG_BLOCK, term)
    signal.signal(signal.SIGTERM, on_term)
    if linux:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER
        libc.prctl(1, int(signal.SIGTERM), 0, 0, 0)  # PR_SET_PDEATHSIG
    if os.getppid() != parent:
        _status(status_fd, "error 0 envelope exited before the command started")
        _status(status_fd, "done")
        os._exit(1)

    err_r, err_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(err_r)
            os.close(status_fd)
            os.set_inheritable(err_w, False)
            # Undo Python's startup changes that would otherwise survive exec.
            for number in (signal.SIGTERM, signal.SIGPIPE, signal.SIGXFSZ):
                signal.signal(number, signal.SIG_DFL)
            signal.pthread_sigmask(signal.SIG_UNBLOCK, term)  # the mask survives exec
            os.execv(argv[0], argv)
        except OSError as exc:
            os.write(err_w, ("%d %s" % (exc.errno or 0, exc.strerror or exc)).encode())
        os._exit(127)
    os.close(err_w)
    failure = b""
    while True:
        chunk = os.read(err_r, 4096)
        if not chunk:
            break
        failure += chunk
    os.close(err_r)
    if failure:
        os.waitpid(pid, 0)
        _status(status_fd, "error " + failure.decode(errors="replace"))
        _status(status_fd, "done")
        os._exit(1)
    # The shell (and only the shell) needs stdout/stderr from here; it got its own
    # copies at fork, preserved across exec. Closing ours means the pipes reach EOF
    # once every process actually holding them exits, not whenever the shim itself
    # does -- which as a subreaper can be much later than the shell, e.g. while
    # reaping a backgrounded, reparented descendant that redirected its own output.
    os.close(1)
    os.close(2)
    _status(status_fd, "pid %d" % pid)

    if not linux:
        import threading

        def watch_parent():
            import select

            kq = select.kqueue()
            event = select.kevent(parent, filter=select.KQ_FILTER_PROC, fflags=select.KQ_NOTE_EXIT)
            try:
                kq.control([event], 0, 0)
                kq.control(None, 1, None)
            except OSError:
                pass  # parent already gone
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=watch_parent, daemon=True).start()

    shell_status = None
    try:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, term)
        while True:
            try:
                wpid, wstatus = os.wait()
            except ChildProcessError:
                break
            if wpid == pid:
                if os.WIFSIGNALED(wstatus):
                    shell_status = "signal %d" % os.WTERMSIG(wstatus)
                else:
                    shell_status = "exit %d" % os.waitstatus_to_exitcode(wstatus)
                _status(status_fd, shell_status)
    except _Terminate:
        if shell_status is None:
            _status(status_fd, "killed")
        _status(status_fd, "done")
        _kill_tree(linux)
        os._exit(0)
    _status(status_fd, "done")
    os._exit(0)


main()
