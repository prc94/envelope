#!/usr/bin/env python3
"""Dependency-free recording gateway for OpenAI-compatible Chat Completions.

Standard library only. It is the one process that holds the provider key; harnesses
receive per-trial tokens issued over a Unix admin socket (see README.md).
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import http.server
import ipaddress
import json
import os
import random
import socket
import socketserver
import stat
import sys
import tarfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, cast

# Transient upstream errors: rate limits and server errors that say nothing about the model.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
UPSTREAM_PATH = "/api/v1/chat/completions"
MODELS_PATH = "/api/v1/models/user"
CHAT_PATH = "/v1/chat/completions"
UNCONFIGURED_FAMILIES = {
    "/v1/messages": "anthropic_messages",
    "/v1/responses": "openai_responses",
}
SAMPLING_KEYS = (
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "max_tokens",
    "max_completion_tokens",
    "frequency_penalty",
    "presence_penalty",
    "repetition_penalty",
    "seed",
    "stop",
    "tool_choice",
    "reasoning",
    "reasoning_effort",
)


@dataclass
class Token:
    token: str
    lane: str
    job: str
    trial: str
    allowed_models: set[str]
    request_cap: int
    auxiliary_models: set[str] = field(default_factory=set)
    aliases: dict[str, str] = field(default_factory=dict)
    pins: dict[str, list[str]] = field(default_factory=dict)
    main_requests: int = 0
    requests: int = 0
    revoked: bool = False
    in_flight: int = 0
    issued: float = field(default_factory=time.monotonic)
    request_started: float | None = None
    last_content: float | None = None
    last_finished: float | None = None
    saw_tools: bool = False
    flags: list[str] = field(default_factory=list)

    def identity(self) -> dict[str, str]:
        return {"lane": self.lane, "job": self.job, "trial": self.trial}


class TokenRegistry:
    def __init__(self) -> None:
        self._tokens: dict[str, Token] = {}
        self.lock = threading.RLock()

    def issue(
        self,
        lane: str,
        job: str,
        trial: str,
        allowed_models: list[str],
        request_cap: int = 100,
        auxiliary_models: list[str] | None = None,
        aliases: dict[str, str] | None = None,
        pins: dict[str, list[str]] | None = None,
    ) -> Token:
        token = os.urandom(24).hex()
        auxiliary = set(auxiliary_models or [])
        value = Token(
            token,
            lane,
            job,
            trial,
            set(allowed_models) | auxiliary,
            request_cap,
            auxiliary,
            dict(aliases or {}),
            dict(pins or {}),
        )
        with self.lock:
            self._tokens[token] = value
        return value

    def get(self, token: str | None) -> Token | None:
        with self.lock:
            value = self._tokens.get(token or "")
            return value if value and not value.revoked else None

    def revoke(self, token: str) -> bool:
        with self.lock:
            value = self._tokens.get(token)
            if not value:
                return False
            value.revoked = True
            return True

    def secrets(self) -> list[str]:
        with self.lock:
            return list(self._tokens)

    def flag(self, value: Token, reason: str) -> None:
        with self.lock:
            if reason not in value.flags:
                value.flags.append(reason)

    def state(self, token: str) -> dict[str, Any] | None:
        with self.lock:
            value = self._tokens.get(token)
            if not value:
                return None
            now = time.monotonic()
            if value.in_flight:
                since = value.last_content or value.request_started or now
            else:
                since = value.last_finished or value.issued
            return {
                **value.identity(),
                "revoked": value.revoked,
                "in_flight": value.in_flight > 0,
                "idle_seconds": now - since,
                "requests": value.requests,
                "main_requests": value.main_requests,
                "flags": list(value.flags),
            }


class Ledger:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()

    def for_trial(self, trial: str) -> list[dict[str, Any]]:
        """Records of one trial; the ledger may live on another host than the runner."""
        if not self.path or not self.path.exists():
            return []
        rows = []
        with self._lock, self.path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (row.get("token") or {}).get("trial") == trial:
                    rows.append(row)
        return rows

    def append(self, record: dict[str, Any]) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())


def digest(value: Any) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def normalize_usage(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {"unknown": "upstream did not report usage"}
    usage = cast("dict[str, Any]", usage)
    prompt = usage.get("prompt_tokens_details") or {}
    completion = usage.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "cached_tokens": prompt.get("cached_tokens"),
        "reasoning_tokens": completion.get("reasoning_tokens"),
        "cost": usage.get("cost"),
    }


def is_auxiliary(info: Token, body: dict[str, Any]) -> bool:
    if body.get("model") in info.auxiliary_models:
        return True
    return info.saw_tools and not body.get("tools")


class ModelCatalog:
    """The models this provider key may use, guardrails included, cached for a while.

    The provider applies workspace guardrails (model and provider allowlists) to the key, so
    a request for a blocked model only fails after a round trip, as a 404. The catalog lets
    the gateway say so up front. When the list cannot be fetched the answer is "unknown" and
    nothing is blocked on that basis.
    """

    # Routing shortcuts that select an endpoint of the base model, not a different model.
    ROUTING_SUFFIXES = ("nitro", "floor", "online", "exacto")

    def __init__(self, fetch: Callable[[], set[str]], ttl: float = 600.0) -> None:
        self._fetch = fetch
        self.ttl = ttl
        self._ids: set[str] | None = None
        self._fetched = 0.0
        self.error: str | None = None
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        with self._lock:
            self._fetched = 0.0

    def ids(self, refresh: bool = False) -> set[str] | None:
        with self._lock:
            if refresh or self._ids is None or time.monotonic() - self._fetched > self.ttl:
                try:
                    self._ids = self._fetch()
                    self.error = None
                except Exception as exc:  # noqa: BLE001 - unknown is a valid answer
                    self._ids, self.error = None, str(exc)
                self._fetched = time.monotonic()
            return self._ids

    def allowed(self, model: str, ids: set[str]) -> bool:
        base, _, suffix = model.rpartition(":")
        return model in ids or (bool(base) and suffix in self.ROUTING_SUFFIXES and base in ids)

    def check(self, models: list[str], refresh: bool = False) -> tuple[list[str], list[str]] | None:
        """(allowed, blocked) for `models`, or None when the provider's list is unavailable."""
        ids = self.ids(refresh)
        if ids is None:
            return None
        allowed = [m for m in models if self.allowed(m, ids)]
        return allowed, [m for m in models if m not in allowed]


def guardrail_blocked(error: Any) -> bool:
    """Whether a provider error says the key's guardrail excluded every endpoint."""
    metadata = error.get("metadata") if isinstance(error, dict) else None
    if not isinstance(metadata, dict):
        return False
    reasons = metadata.get("ineligibility_reasons") or []
    return metadata.get("failed_routing_step") == "Filter by Guardrails" or any(
        isinstance(r, dict) and r.get("reason") == "model-ignored-by-guardrail" for r in reasons
    )


class GatewayHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def gateway(self) -> GatewayServer:
        return cast("GatewayServer", self.server)

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _json(self, status: int, value: Any) -> None:
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reject(self, status: int, message: str, record: dict[str, Any] | None = None) -> None:
        if record is not None:
            self.gateway.ledger.append({"timestamp": time.time(), **record, "rejected": message})
        self._json(status, {"error": {"message": message}})

    def do_POST(self) -> None:
        gw = self.gateway
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(length) if length else b""
        family = UNCONFIGURED_FAMILIES.get(path)
        if family is not None:
            self._reject(
                400,
                f"unconfigured API family: {family}",
                {"api_family": family, "error": "unconfigured_family"},
            )
            return
        if path != CHAT_PATH:
            self._reject(404, "unsupported API family or path", {"error": "unsupported_path"})
            return
        presented = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        info = gw.registry.get(presented)
        if not info:
            self._reject(
                401,
                "missing, unknown or revoked token",
                {"api_family": "chat_completions", "error": "unauthorized"},
            )
            return
        try:
            body = json.loads(raw_body)
            if not isinstance(body, dict):
                raise TypeError("body is not an object")
        except (ValueError, TypeError):
            self._reject(400, "invalid JSON")
            return
        body = cast("dict[str, Any]", body)
        requested = body.get("model")
        aliases = gw.aliases | info.aliases
        resolved = aliases.get(str(requested), requested)
        base = {"token": info.identity(), "api_family": "chat_completions", "model": requested}
        if requested not in info.allowed_models and resolved not in info.allowed_models:
            gw.registry.flag(info, "disallowed_model")
            self._reject(403, "model is not allowed", base | {"error": "disallowed_model"})
            return
        auxiliary = is_auxiliary(info, body)
        role = "auxiliary" if auxiliary else "main"
        with gw.registry.lock:
            over_cap = not auxiliary and info.main_requests >= info.request_cap
            if not over_cap:
                info.requests += 1
                if not auxiliary:
                    info.main_requests += 1
                if body.get("tools"):
                    info.saw_tools = True
                info.in_flight += 1
                info.request_started = time.monotonic()
        if over_cap:
            gw.registry.flag(info, "runaway")
            self._reject(429, "request cap exceeded", base | {"role": role, "error": "runaway"})
            return
        try:
            self._proxy(info, body, requested, aliases, role)
        finally:
            with gw.registry.lock:
                info.in_flight -= 1
                info.last_finished = time.monotonic()

    def _proxy(
        self, info: Token, body: dict[str, Any], requested: Any, aliases: dict[str, str], role: str
    ) -> None:
        gw = self.gateway
        started = time.monotonic()
        streamed = bool(body.get("stream"))
        upstream_body = dict(body)
        if streamed:
            options = dict(upstream_body.get("stream_options") or {})
            options["include_usage"] = True
            upstream_body["stream_options"] = options
        pin = info.pins.get(requested) or gw.pins.get(requested)
        if pin:
            upstream_body["provider"] = {"order": pin, "allow_fallbacks": False}
        result = Outcome()
        conn: http.client.HTTPConnection | None = None

        def record() -> None:
            if not result.recorded:
                result.recorded = True
                self._record(info, body, requested, aliases, role, streamed, started, result)

        try:
            conn, response = gw.forward_retrying(upstream_body, result.retries)
            content_type = response.getheader("Content-Type", "")
            if streamed and response.status < 400 and "event-stream" in content_type:
                self._relay_stream(info, response, result)
            else:
                self._relay_body(info, response, result, record)
        except OSError as exc:
            # Includes client disconnects (BrokenPipe/ConnectionReset) and upstream failures.
            result.end_reason = result.end_reason or type(exc).__name__
            result.error = result.error or str(exc)
            if not result.headers_sent:
                self._json(502, {"error": {"message": f"upstream failure: {exc}"}})
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001
            result.end_reason = type(exc).__name__
            result.error = str(exc)
            if not result.headers_sent:
                self._json(502, {"error": {"message": f"gateway failure: {exc}"}})
            self.close_connection = True
        finally:
            if conn is not None:
                conn.close()
            record()

    def _relay_stream(self, info: Token, response: http.client.HTTPResponse, out: Outcome) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # No Content-Length and no chunking: the client reads until we close.
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        out.headers_sent = True
        out.status = response.status
        tool_calls: dict[int, dict[str, str]] = {}
        for raw in response:
            self.wfile.write(raw)
            self.wfile.flush()
            line = raw.decode("utf-8", "replace").strip()
            if not line or line.startswith(":"):
                continue  # blank separators and keep-alive comments carry no content
            now = time.monotonic()
            out.first = out.first or now
            out.last = now
            info.last_content = now
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                out.done = True
                continue
            if self.gateway.record_bodies:
                out.events.append(payload)
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event = cast("dict[str, Any]", event)
            if event.get("error"):
                out.error = event["error"]
                out.end_reason = "stream_error"
            out.served_model = out.served_model or event.get("model")
            out.provider = out.provider or event.get("provider")
            if event.get("usage"):
                out.usage = event["usage"]
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                out.completion.append(delta.get("content") or "")
                for call in delta.get("tool_calls") or []:
                    slot = tool_calls.setdefault(
                        call.get("index", 0), {"name": "", "arguments": ""}
                    )
                    function = call.get("function") or {}
                    slot["name"] += function.get("name") or ""
                    slot["arguments"] += function.get("arguments") or ""
                if choice.get("finish_reason"):
                    out.finish_reason = choice["finish_reason"]
        out.tool_calls = [tool_calls[i] for i in sorted(tool_calls)]
        if out.end_reason is None:
            out.end_reason = "complete" if out.done else "truncated_stream"

    def _relay_body(
        self,
        info: Token,
        response: http.client.HTTPResponse,
        out: Outcome,
        record: Callable[[], None],
    ) -> None:
        data = response.read()
        out.status = response.status
        out.first = out.last = time.monotonic()
        info.last_content = out.last
        self._summarize_body(response.status, data, out)
        record()  # the ledger line exists before the client can see the response
        self.send_response(response.status)
        self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        out.headers_sent = True
        self.wfile.write(data)

    def _summarize_body(self, status: int, data: bytes, out: Outcome) -> None:
        try:
            parsed = json.loads(data) if data else {}
        except json.JSONDecodeError:
            parsed = {}
        parsed = cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else {}
        if self.gateway.record_bodies:
            out.events.append(data.decode("utf-8", "replace"))
        if status >= 400:
            out.end_reason = f"http_{status}"
            out.error = parsed.get("error") or data[:500].decode("utf-8", "replace")
            if guardrail_blocked(parsed.get("error")):
                out.end_reason = "guardrail_blocked"
            return
        if parsed.get("error"):
            out.end_reason = "error"
            out.error = parsed["error"]
            return
        out.usage = parsed.get("usage")
        out.served_model = parsed.get("model")
        out.provider = parsed.get("provider")
        choices = parsed.get("choices") or [{}]
        message = choices[0].get("message") or {}
        out.completion.append(message.get("content") or "")
        out.tool_calls = [
            {
                "name": (call.get("function") or {}).get("name", ""),
                "arguments": (call.get("function") or {}).get("arguments", ""),
            }
            for call in message.get("tool_calls") or []
        ]
        out.finish_reason = choices[0].get("finish_reason")
        out.end_reason = "complete"

    def _record(
        self,
        info: Token,
        body: dict[str, Any],
        requested: Any,
        aliases: dict[str, str],
        role: str,
        streamed: bool,
        started: float,
        out: Outcome,
    ) -> None:
        gw = self.gateway
        expected = aliases.get(requested, requested)
        served = out.served_model
        mismatch = None if served is None else served not in {expected, requested}
        if mismatch:
            gw.registry.flag(info, "model_mismatch")
        if out.end_reason == "guardrail_blocked":
            gw.registry.flag(info, "guardrail_blocked")
            gw.catalog.invalidate()  # the cached list was wrong or stale
        messages = body.get("messages") or []
        record: dict[str, Any] = {
            "timestamp": time.time(),
            "token": info.identity(),
            "api_family": "chat_completions",
            "role": role,
            "model": requested,
            "served_model": served,
            "provider": out.provider,
            "model_mismatch": mismatch,
            "stream": streamed,
            "sampling": {k: body[k] for k in SAMPLING_KEYS if k in body},
            "tools_hash": digest(body.get("tools")),
            "tool_names": sorted(
                str((t.get("function") or {}).get("name", t.get("name")))
                for t in body.get("tools") or []
                if isinstance(t, dict)
            ),
            "system_hash": digest(
                [m for m in messages if m.get("role") in ("system", "developer")]
            ),
            "prompt_hash": digest(messages),
            "completion": "".join(out.completion),
            "tool_calls": out.tool_calls,
            "finish_reason": out.finish_reason,
            "usage": normalize_usage(out.usage),
            "status": out.status,
            "attempts": len(out.retries) + 1,
            "retried": [r["status"] for r in out.retries],
            "retry_wait": sum(r["wait"] for r in out.retries),
            "time_to_first_content": out.first - started if out.first else None,
            "time_of_last_content": out.last - started if out.last else None,
            "duration": time.monotonic() - started,
            "end_reason": out.end_reason,
            "error": out.error,
        }
        if gw.record_bodies:
            record["request_body"] = body
            record["response_body"] = out.events
            record = gw.redact(record)
        gw.ledger.append(record)


@dataclass
class Outcome:
    status: int | None = None
    headers_sent: bool = False
    first: float | None = None
    last: float | None = None
    usage: Any = None
    served_model: str | None = None
    provider: str | None = None
    end_reason: str | None = None
    error: Any = None
    finish_reason: str | None = None
    done: bool = False
    recorded: bool = False
    retries: list[dict[str, Any]] = field(default_factory=list)  # absorbed transient errors
    completion: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, str]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)


class GatewayServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        upstream: tuple[str, int],
        registry: TokenRegistry,
        ledger: Ledger,
        pins: dict[str, list[str]] | None = None,
        aliases: dict[str, str] | None = None,
        api_key: str | None = None,
        tls: bool = False,
        record_bodies: bool = False,
        app: dict[str, str] | None = None,
        retries: int = 8,
        retry_base: float = 2.0,
        retry_cap: float = 45.0,
        retry_budget: float = 150.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        validate_bind_address(address[0])
        super().__init__(address, GatewayHandler)
        self.upstream = upstream
        self.registry = registry
        self.ledger = ledger
        self.pins = pins or {}
        self.aliases = aliases or {}
        self._api_key = api_key
        self.tls = tls
        self.record_bodies = record_bodies
        self.app = app or {}
        self.retries, self.retry_base = retries, retry_base
        self.retry_cap, self.retry_budget, self._sleep = retry_cap, retry_budget, sleep
        self.catalog = ModelCatalog(self._list_models)

    def _connect(self, timeout: float) -> http.client.HTTPConnection:
        factory = http.client.HTTPSConnection if self.tls else http.client.HTTPConnection
        return factory(*self.upstream, timeout=timeout)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.app}
        if self._api_key:
            headers["Authorization"] = "Bearer " + self._api_key
        return headers

    def forward(
        self, body: dict[str, Any]
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        conn = self._connect(600)
        conn.request("POST", UPSTREAM_PATH, json.dumps(body), self._headers())
        return conn, conn.getresponse()

    def forward_retrying(
        self, body: dict[str, Any], log: list[dict[str, Any]]
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        """`forward`, absorbing transient errors before any byte reaches the harness.

        A rate limit or a 5xx (or a connection that fails before a response) is retried with
        exponential backoff (honouring Retry-After) for at most `retries` more attempts and
        `retry_budget` seconds of waiting; each absorbed attempt is appended to `log`. When the
        limits run out the last response is relayed unchanged, so the harness sees the error
        that persisted, not the ones that passed."""
        waited, attempt = 0.0, 0
        while True:
            attempt += 1
            try:
                conn, response = self.forward(body)
            except OSError as exc:
                delay = self._backoff(attempt, None, waited)
                if attempt > self.retries or delay is None:
                    raise
                log.append({"status": None, "reason": str(exc), "wait": delay})
                waited += delay
                self._sleep(delay)
                continue
            if response.status not in RETRY_STATUSES or attempt > self.retries:
                return conn, response
            delay = self._backoff(attempt, response.getheader("Retry-After"), waited)
            if delay is None:
                return conn, response  # out of budget: relay this response as it is
            response.read()  # drain so the connection can be closed cleanly
            conn.close()
            log.append({"status": response.status, "reason": None, "wait": delay})
            waited += delay
            self._sleep(delay)

    def _backoff(self, attempt: int, retry_after: str | None, waited: float) -> float | None:
        """Seconds to wait before the next attempt, or None when the budget would be exceeded."""
        delay = min(self.retry_cap, self.retry_base * 2 ** (attempt - 1))
        try:
            delay = max(delay, min(self.retry_cap, float(retry_after or 0)))
        except ValueError:
            pass
        delay *= random.uniform(0.75, 1.0)
        return delay if waited + delay <= self.retry_budget else None

    def _list_models(self) -> set[str]:
        """Models the key may use: the provider filters this list by the key's guardrails."""
        conn = self._connect(30)
        try:
            conn.request("GET", MODELS_PATH, headers=self._headers())
            response = conn.getresponse()
            data = response.read()
        finally:
            conn.close()
        if response.status != 200:
            raise RuntimeError(f"model list returned HTTP {response.status}")
        ids: set[str] = set()
        for entry in json.loads(data).get("data", []):
            ids.update(v for v in (entry.get("id"), entry.get("canonical_slug")) if v)
        return ids

    def redact(self, record: dict[str, Any]) -> dict[str, Any]:
        text = json.dumps(record, ensure_ascii=False)
        secrets = [self._api_key or "", *self.registry.secrets()]
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[redacted]")
        return json.loads(text)

    def scan(self, root: str | Path) -> list[dict[str, Any]]:
        return scan_credentials(root, self._api_key or "")

    def scan_stream(self, fileobj: IO[bytes]) -> list[dict[str, Any]]:
        return scan_tar_stream(fileobj, self._api_key or "")


def validate_bind_address(address: str) -> None:
    """Reject wildcard/public binds; the gateway is local or Docker-bridge only."""
    ip = ipaddress.ip_address(address)
    if ip.is_unspecified or not (ip.is_loopback or ip.is_private):
        raise ValueError("gateway must bind to loopback or a private Docker bridge address")


def scan_offsets(fh: IO[bytes], needle: bytes, chunk: int = 1 << 20) -> list[int]:
    """Byte offsets of `needle` in a stream, including matches that span chunk boundaries."""
    found: list[int] = []
    if not needle:
        return found
    carry = b""
    consumed = 0  # bytes before `carry` in the stream
    while data := fh.read(chunk):
        window = carry + data
        start = 0
        while (at := window.find(needle, start)) >= 0:
            found.append(consumed + at)
            start = at + 1
        keep = len(needle) - 1
        carry = window[-keep:] if keep else b""
        consumed += len(window) - len(carry)
    return found


def scan_credentials(root: str | Path, credential: str) -> list[dict[str, Any]]:
    """Report files and byte offsets of a credential without revealing it."""
    findings: list[dict[str, Any]] = []
    needle = credential.encode()
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file():
            continue
        try:
            with path.open("rb") as fh:
                offsets = scan_offsets(fh, needle)
        except OSError:
            continue
        findings += [{"file": str(path), "offset": offset} for offset in offsets]
    return findings


def scan_tar_stream(fileobj: IO[bytes], credential: str) -> list[dict[str, Any]]:
    """Scan a tar stream sent by the runner (its results live on another host)."""
    findings: list[dict[str, Any]] = []
    needle = credential.encode()
    with tarfile.open(fileobj=fileobj, mode="r|*") as archive:
        for member in archive:
            handle = archive.extractfile(member) if member.isfile() else None
            if handle is None:
                continue
            findings += [
                {"file": member.name, "offset": offset} for offset in scan_offsets(handle, needle)
            ]
    return findings


class AdminHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        admin = cast("AdminServer", self.server)
        try:
            request = json.loads(self.rfile.readline())
            if request.get("op") == "scan_tar":
                # The tar stream follows the request line and ends when the client half-closes.
                if admin.gateway is None:
                    raise ValueError("no gateway attached")
                result = {"findings": admin.gateway.scan_stream(cast("IO[bytes]", self.rfile))}
            else:
                result = admin.dispatch(request)
        except (tarfile.TarError, OSError) as exc:
            result = {"error": f"scan failed: {exc}"}
        except (ValueError, KeyError, TypeError) as exc:
            result = {"error": f"invalid request: {exc}"}
        self.wfile.write((json.dumps(result) + "\n").encode())


class AdminServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, registry: TokenRegistry, gateway: GatewayServer | None = None):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        old = os.umask(0o117)  # socket is 0660: gateway user and the runner's group only
        try:
            super().__init__(path, AdminHandler)
        finally:
            os.umask(old)
        self.registry = registry
        self.gateway = gateway

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        reg = self.registry
        op = request.get("op")
        if op == "issue":
            models = [*request["allowed_models"], *(request.get("auxiliary_models") or [])]
            checked = self.gateway.catalog.check(models) if self.gateway else None
            if checked and checked[1]:
                return {"error": f"model blocked by the key's guardrail: {', '.join(checked[1])}"}
            value = reg.issue(
                request["lane"],
                request["job"],
                request["trial"],
                request["allowed_models"],
                request.get("request_cap", 100),
                request.get("auxiliary_models"),
                request.get("aliases"),
                request.get("pins"),
            )
            return {
                "token": value.token,
                **value.identity(),
                "model_check": "ok" if checked else "unavailable",
            }
        if op == "models":
            if self.gateway is None:
                return {"error": "no gateway attached"}
            catalog = self.gateway.catalog
            ids = catalog.ids(bool(request.get("refresh")))
            if ids is None:
                return {"error": f"model list unavailable: {catalog.error}"}
            reply: dict[str, Any] = {"models": sorted(ids)}
            if request.get("check"):
                allowed, blocked = catalog.check(list(request["check"])) or ([], [])
                reply |= {"allowed": allowed, "blocked": blocked}
            return reply
        if op == "revoke":
            return {"revoked": reg.revoke(request["token"])}
        if op == "state":
            return reg.state(request["token"]) or {"error": "unknown token"}
        if op == "ledger":
            if self.gateway is None:
                return {"error": "no gateway attached"}
            return {"records": self.gateway.ledger.for_trial(request["trial"])}
        if op == "scan":
            if self.gateway is None:
                return {"error": "no gateway attached"}
            return {"findings": self.gateway.scan(request["root"])}
        return {"error": "unknown operation"}


def app_headers(args: argparse.Namespace) -> dict[str, str]:
    """App attribution for the provider. The harness's own headers never reach it (the gateway
    builds the upstream request), so the gateway states which app the traffic belongs to."""
    pairs = (
        ("HTTP-Referer", args.app_url),
        ("X-OpenRouter-Title", args.app_title),
        ("X-OpenRouter-Categories", args.app_categories),
    )
    headers = {name: value for name, value in pairs if value}
    if headers and "HTTP-Referer" not in headers:
        raise SystemExit(
            "--app-title/--app-categories need --app-url: without HTTP-Referer no app is created"
        )
    return headers


def load_key(path: str) -> str:
    mode = os.stat(path).st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SystemExit(f"refusing key file {path}: must not be accessible to group or others")
    return Path(path).read_text(encoding="utf-8").strip()


def relay(socket_path: str) -> int:
    """Pipe stdin/stdout to the admin socket: the forced command of the runner's SSH key.

    The runner's account can reach the socket only through this relay, and the relay does
    nothing but move bytes, so the account cannot run anything else on the gateway host.
    """
    with socket.socket(socket.AF_UNIX) as sock:
        sock.connect(socket_path)

        def pump() -> None:
            try:
                while chunk := sys.stdin.buffer.read1(1 << 16):  # type: ignore[attr-defined]
                    sock.sendall(chunk)
            finally:
                sock.shutdown(socket.SHUT_WR)

        threading.Thread(target=pump, daemon=True).start()
        while chunk := sock.recv(1 << 16):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["relay"]:
        parser = argparse.ArgumentParser(prog="gateway.py relay")
        parser.add_argument("--socket", required=True)
        return relay(parser.parse_args(argv[1:]).socket)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", action="append", required=True, metavar="HOST:PORT")
    parser.add_argument("--admin-socket", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--upstream", default="openrouter.ai:443", metavar="HOST:PORT")
    parser.add_argument("--plain-upstream", action="store_true", help="HTTP instead of HTTPS")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--record-bodies", action="store_true")
    parser.add_argument(
        "--app-url", help="OpenRouter app attribution: HTTP-Referer (identifies the app)"
    )
    parser.add_argument("--app-title", help="OpenRouter app attribution: X-OpenRouter-Title")
    parser.add_argument(
        "--app-categories", help="OpenRouter app attribution: X-OpenRouter-Categories"
    )
    parser.add_argument("--upstream-retries", type=int, default=8)
    parser.add_argument("--retry-base", type=float, default=2.0, help="first backoff, seconds")
    parser.add_argument("--retry-cap", type=float, default=45.0, help="longest single backoff")
    parser.add_argument("--retry-budget", type=float, default=150.0, help="total wait per request")
    parser.add_argument("--pins", help="JSON file: {model: [provider, ...]}")
    parser.add_argument("--aliases", help="JSON file: {requested: served}")
    args = parser.parse_args(argv)

    key = load_key(args.key_file)
    host, _, port = args.upstream.rpartition(":")
    registry = TokenRegistry()
    ledger = Ledger(args.ledger)
    pins = json.loads(Path(args.pins).read_text()) if args.pins else None
    aliases = json.loads(Path(args.aliases).read_text()) if args.aliases else None
    servers: list[GatewayServer] = []
    for spec in args.listen:
        bind_host, _, bind_port = spec.rpartition(":")
        servers.append(
            GatewayServer(
                (bind_host, int(bind_port)),
                (host, int(port)),
                registry,
                ledger,
                pins,
                aliases,
                key,
                not args.plain_upstream,
                args.record_bodies,
                app_headers(args),
                args.upstream_retries,
                args.retry_base,
                args.retry_cap,
                args.retry_budget,
            )
        )
    admin = AdminServer(args.admin_socket, registry, servers[0])
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"gateway listening on {', '.join(args.listen)}", file=sys.stderr)
    try:
        admin.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
