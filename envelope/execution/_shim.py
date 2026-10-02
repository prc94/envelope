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
SIGTERM to the shim means "terminate the tree now". SIGUSR1 means "send
SIGTERM to every descendant except the shim" (design B7: graceful `stop`) --
on macOS the forked child gets its own process group, separate from the
shim's, so a group-wide signal never reaches the shim itself.
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


def _kill_tree(linux, child):
    """Forcibly kill every descendant of ``child`` (the shell). On macOS this
    targets the shell's own process group (the shim isolated itself from it
    at fork, so the shim is not a member and survives to exit normally)."""
    if not linux:
        try:
            os.killpg(child, signal.SIGKILL)
        except OSError:
            pass
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


def _graceful_kill(linux, child):
    """SIGTERM every descendant except the shim (design B7): the shell
    itself plus everything under it on Linux (PID-tree walk from the shim,
    which already excludes the shim); the shell's whole process group on
    macOS, which the shim deliberately is not a member of."""
    if linux:
        # `_descendants_linux(<shim pid>)` walks the shim's own children and
        # down, which already starts at `child` (the shell) and excludes the
        # shim itself.
        for pid in _descendants_linux(os.getpid()):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    else:
        try:
            os.killpg(child, signal.SIGTERM)
        except OSError:
            pass


def main():
    status_fd = int(sys.argv[1])
    parent = int(sys.argv[2])
    argv = sys.argv[4:]
    linux = sys.platform.startswith("linux")

    terminating = []
    pid_holder = []  # set once the shell is forked; read by on_usr1 below

    def on_term(signum, frame):
        # Raise only once: a second SIGTERM (it happens, e.g. parent death
        # plus an explicit terminate) must not abort the tree kill in progress.
        if not terminating:
            terminating.append(signum)
            raise _Terminate()

    def on_usr1(signum, frame):
        # Graceful `stop` (design B7): unlike SIGTERM, this does not raise --
        # the shim keeps waiting normally, only the tree below it is signalled.
        if pid_holder:
            _graceful_kill(linux, pid_holder[0])

    # SIGTERM/SIGUSR1 stay blocked until the wait loop below can handle them:
    # a request arriving earlier (there is no child to signal yet, or a
    # termination is already in flight) stays pending instead of running the
    # handler too early or interrupting the tree kill in progress.
    term = {signal.SIGTERM, signal.SIGUSR1}
    signal.pthread_sigmask(signal.SIG_BLOCK, term)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGUSR1, on_usr1)
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
            if not linux:
                # Isolate the shell into its own process group so a
                # group-wide graceful SIGTERM (design B7) never reaches the
                # shim, which stays in its original (session) group.
                os.setpgid(0, 0)
            # Undo Python's startup changes that would otherwise survive exec.
            for number in (signal.SIGTERM, signal.SIGUSR1, signal.SIGPIPE, signal.SIGXFSZ):
                signal.signal(number, signal.SIG_DFL)
            signal.pthread_sigmask(signal.SIG_UNBLOCK, term)  # the mask survives exec
            os.execv(argv[0], argv)
        except OSError as exc:
            os.write(err_w, ("%d %s" % (exc.errno or 0, exc.strerror or exc)).encode())
        os._exit(127)
    pid_holder.append(pid)
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
        # Reported so envelope can tell a background-construct cleanup (the
        # command text had its own `&`/`nohup`) from an ordinary exit with
        # nothing left running (design B11); macOS has no equivalent walk, so
        # it stays silent rather than guessing.
        if linux:
            count = len(_descendants_linux(os.getpid()))
            if count:
                _status(status_fd, "leftover %d" % count)
        _status(status_fd, "done")
        _kill_tree(linux, pid)
        os._exit(0)
    _status(status_fd, "done")
    os._exit(0)


main()
