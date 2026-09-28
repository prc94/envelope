"""Helpers shared by task generators. They run on the runner host, standard library only."""

from __future__ import annotations

import random
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

WORDS = [
    "cache",
    "session",
    "token",
    "queue",
    "index",
    "shard",
    "replica",
    "worker",
    "schema",
    "record",
    "buffer",
    "socket",
    "handler",
    "router",
    "policy",
    "limit",
    "quota",
    "timer",
    "batch",
    "stream",
    "ledger",
    "invoice",
    "payment",
    "order",
    "catalog",
    "search",
    "notify",
    "gateway",
    "upstream",
    "backend",
    "client",
    "server",
    "request",
    "response",
]
VERBS = [
    "fix",
    "add",
    "update",
    "remove",
    "refactor",
    "tune",
    "rename",
    "document",
    "simplify",
    "harden",
    "speed",
    "up",
]
FIRST = [
    "dana",
    "marek",
    "priya",
    "oleg",
    "sofia",
    "kenji",
    "lena",
    "tomas",
    "noor",
    "ivan",
    "chloe",
    "amir",
]
LAST = [
    "whitfield",
    "novak",
    "sharma",
    "petrov",
    "rossi",
    "tanaka",
    "berg",
    "horvat",
    "haddad",
    "orlov",
    "martin",
    "rahimi",
]


def rng_for(seed: int, salt: str = "") -> random.Random:
    return random.Random(f"{seed}:{salt}")


def token(rng: random.Random, n: int, alphabet: str = "0123456789abcdef") -> str:
    return "".join(rng.choice(alphabet) for _ in range(n))


def stamp(epoch: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch))


def write_lines(
    path: Path, lines: Iterable[str], eol: str = "\n", encoding: str = "utf-8", bom: bytes = b""
) -> None:
    """Exact bytes: no newline translation, whatever the host. `bom` is written once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(bom)
        for line in lines:
            fh.write((line + eol).encode(encoding))


def make_repo(root: Path, commits: list[dict[str, str | int]]) -> None:
    """A packed git repository with the given commits (author, email, when, message, path,
    content), created with fast-import so thousands of commits take a second."""
    root.mkdir(parents=True, exist_ok=True)
    stream = bytearray()
    for i, c in enumerate(commits, 1):
        msg = str(c["message"]).encode()
        body = str(c["content"]).encode()
        who = f"{c['author']} <{c['email']}> {c['when']} +0000".encode()
        stream += b"commit refs/heads/main\nmark :%d\n" % i
        stream += b"author " + who + b"\ncommitter " + who + b"\n"
        stream += b"data %d\n" % len(msg) + msg + b"\n"
        if i > 1:
            stream += b"from :%d\n" % (i - 1)
        stream += b"M 100644 inline " + str(c["path"]).encode() + b"\n"
        stream += b"data %d\n" % len(body) + body + b"\n"
    run = lambda *a, **k: subprocess.run(
        ["git", "-C", str(root), *a], check=True, capture_output=True, **k
    )
    subprocess.run(["git", "init", "-q", "--template=", "-b", "main", str(root)], check=True)
    run("fast-import", "--quiet", input=bytes(stream))
    run("checkout", "-q", "-f", "main")
    run("config", "user.name", "eval")
    run("config", "user.email", "eval@example.com")
    run("config", "core.autocrlf", "false")
    run("gc", "-q", "--prune=now")
