"""Spike S3: POSIX containment via the launch shim. Run: python scripts/spikes/s3_posix.py"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SHIM = (Path(__file__).resolve().parents[2] / "envelope/execution/_shim.py").read_text()
MARK = f"s3spike{os.getpid()}"


def shim_cmd(status_w: int, argv: list[str], parent: int | None = None) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-S",
        "-c",
        SHIM,
        str(status_w),
        str(parent or os.getpid()),
        "--",
        *argv,
    ]


def alive() -> list[str]:
    out = subprocess.run(["pgrep", "-af", MARK], capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if "pgrep" not in line and "s3_posix" not in line]


TREE = (
    f"sleep 300 & sleep 300 & (setsid sleep 301 &) ; sh -c 'sleep 302' & exec -a {MARK} sleep 303"
)


def start(parent: int | None = None) -> tuple[subprocess.Popen[bytes], int]:
    r, w = os.pipe()
    env = dict(os.environ, MARKER=MARK)
    p = subprocess.Popen(
        shim_cmd(
            w, ["/bin/bash", "-c", f"# {MARK}\n" + TREE.replace("sleep 30", "sleep 30")], parent
        ),
        pass_fds=[w],
        start_new_session=True,
        env=env,
    )
    os.close(w)
    return p, r


def count_tree(shim_pid: int) -> int:
    out = subprocess.run(["ps", "-e", "-o", "comm=,args="], capture_output=True, text=True).stdout
    return sum(1 for line in out.splitlines() if line.split()[0] == "sleep" and " 30" in line)


def test_timeout_kill() -> None:
    p, r = start()
    time.sleep(0.5)
    before = count_tree(p.pid)
    p.send_signal(signal.SIGTERM)
    p.wait(5)
    time.sleep(0.2)
    status = os.read(r, 4096).decode()
    after = count_tree(p.pid)
    print(f"[timeout kill] sleeps before={before} after={after} status={status.split()}")
    assert before >= 5 and after == 0


def test_parent_sigkill() -> None:
    # A fake "envelope" that starts the shim then hangs; we SIGKILL it.
    fake = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,subprocess,sys,time\n"
            f"shim=open({str(Path(__file__).resolve().parents[2] / 'envelope/execution/_shim.py')!r}).read()\n"
            "r,w=os.pipe()\n"
            "subprocess.Popen([sys.executable,'-I','-S','-c',shim,str(w),str(os.getpid()),'--',"
            f"'/bin/bash','-c',{TREE!r}],pass_fds=[w],start_new_session=True)\n"
            "time.sleep(1000)\n",
        ],
    )
    time.sleep(0.8)
    before = count_tree(fake.pid)
    fake.kill()
    fake.wait()
    time.sleep(0.5)
    after = count_tree(fake.pid)
    print(f"[parent SIGKILL] sleeps before={before} after={after}")
    assert before >= 5 and after == 0


def test_overhead(n: int = 20) -> None:
    def timed(argv_fn) -> float:
        t = time.perf_counter()
        for _ in range(n):
            r, w = os.pipe()
            subprocess.run(argv_fn(w), pass_fds=[w], check=True)
            os.close(w)
            os.close(r)
        return (time.perf_counter() - t) / n * 1000

    direct = timed(lambda w: ["/bin/bash", "-c", "true"])
    shim = timed(lambda w: shim_cmd(w, ["/bin/bash", "-c", "true"]))
    print(f"[overhead] direct={direct:.1f} ms shim={shim:.1f} ms overhead={shim - direct:.1f} ms")


if __name__ == "__main__":
    print(sys.version.split()[0], sys.platform)
    test_timeout_kill()
    test_parent_sigkill()
    test_overhead()
    print("S3 OK")
