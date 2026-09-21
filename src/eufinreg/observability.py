"""What the process saw, in a form you can act on at 04:12 in the morning.

A scheduled job that only prints prose is a job you cannot debug: "it was slow
last night" and "ESMA rate-limited us 40 times" look identical in a log full of
sentences. This module is the counting and the correlation, and it is deliberately
small — three ideas, no dependencies:

**Metrics.** Every HTTP attempt is recorded where it happens, in
:class:`eufinreg.http.Fetcher`, because that is the only place that knows about
retries, waits and byte counts. Everything downstream reads the totals rather
than re-deriving them. Cheap enough to leave on always: a few counters and a
bounded list of the slowest requests.

**Phases.** A source that took 90 seconds is not a diagnosis. *Fetching* took 88
and parsing took 2 is a diagnosis, and it points at a different fix than the
reverse. :class:`Phases` times the four steps every ingest goes through.

**Correlation.** One ``run_id`` per invocation, stamped into the logs, the run
record and the snapshot metadata. When somebody asks why Tuesday's numbers moved,
the answer is one grep rather than an archaeology project.

Log output is either the human line this project has always printed, or one JSON
object per line for anything that ships logs somewhere. Same events, same
fields — the difference is the renderer, so a human and a log pipeline never
disagree about what happened.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

#: How many "slowest request" samples to keep. Enough to spot a pattern, small
#: enough that a million-row ingest does not grow a million-entry list.
SLOW_SAMPLES = 5


def new_run_id(now: float | None = None) -> str:
    """A time-ordered, unique-enough identifier: ``20260816T131500Z-3f9a1c``.

    Sortable by eye and by ``ls``, which is the property that matters when you
    are looking at a directory of run records rather than querying something.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    return f"{stamp}-{os.urandom(3).hex()}"


def host_of(url: str) -> str:
    """``https://api.gleif.org/api/v1/…`` → ``api.gleif.org``.

    Metrics are keyed by host rather than by URL: a paging loop produces
    thousands of distinct URLs and exactly one interesting number.
    """
    try:
        return urlsplit(url).netloc or "?"
    except ValueError:
        return "?"


@dataclass
class Metrics:
    """Counters for one process. Not thread-safe by design — see the note.

    The service layer serialises outbound requests behind a lock, so the only
    writer is the thread holding that lock. Adding synchronisation here would
    imply a concurrency this project does not have and does not want.
    """

    requests: int = 0
    retries: int = 0
    failures: int = 0
    bytes_in: int = 0
    seconds_requesting: float = 0.0
    seconds_waiting: float = 0.0
    by_status: Counter[str] = field(default_factory=Counter)
    by_host: Counter[str] = field(default_factory=Counter)
    by_outcome: Counter[str] = field(default_factory=Counter)
    #: ``(seconds, method, url)`` for the slowest requests seen.
    slowest: list[tuple[float, str, str]] = field(default_factory=list)

    def record_request(
        self,
        *,
        method: str,
        url: str,
        status: int | None,
        seconds: float,
        size: int = 0,
        outcome: str = "ok",
    ) -> None:
        self.requests += 1
        self.seconds_requesting += max(0.0, seconds)
        self.bytes_in += max(0, size)
        self.by_host[host_of(url)] += 1
        self.by_status[str(status) if status is not None else "none"] += 1
        self.by_outcome[outcome] += 1
        if outcome == "retry":
            self.retries += 1
        elif outcome == "error":
            self.failures += 1

        self.slowest.append((round(max(0.0, seconds), 3), method, url))
        self.slowest.sort(key=lambda item: -item[0])
        del self.slowest[SLOW_SAMPLES:]

    def record_wait(self, seconds: float) -> None:
        """Time spent deliberately doing nothing: throttle delay and backoff.

        Separated from request time because they mean opposite things. High wait
        with low request time is politeness working as intended; the reverse is a
        register in trouble.
        """
        self.seconds_waiting += max(0.0, seconds)

    def merge(self, other: Metrics) -> None:
        self.requests += other.requests
        self.retries += other.retries
        self.failures += other.failures
        self.bytes_in += other.bytes_in
        self.seconds_requesting += other.seconds_requesting
        self.seconds_waiting += other.seconds_waiting
        self.by_status.update(other.by_status)
        self.by_host.update(other.by_host)
        self.by_outcome.update(other.by_outcome)
        self.slowest = sorted([*self.slowest, *other.slowest], key=lambda i: -i[0])[:SLOW_SAMPLES]

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "failures": self.failures,
            "bytes_in": self.bytes_in,
            "seconds_requesting": round(self.seconds_requesting, 3),
            "seconds_waiting": round(self.seconds_waiting, 3),
            "by_status": dict(self.by_status),
            "by_host": dict(self.by_host),
            "by_outcome": dict(self.by_outcome),
            "slowest": [{"seconds": s, "method": m, "url": u} for s, m, u in self.slowest],
        }

    def summary(self) -> str:
        """One line, for the end of a run."""
        return (
            f"{self.requests} request(s), {self.retries} retried, {self.failures} failed, "
            f"{self.bytes_in / 1e6:.1f} MB in, "
            f"{self.seconds_requesting:.1f}s on the wire, {self.seconds_waiting:.1f}s waiting"
        )


@dataclass
class Phases:
    """Where the time actually went, per source.

    ``fetch`` includes the politeness waits, because from an operator's point of
    view a run that spends 90 seconds being polite really did take 90 seconds.
    The waits are also counted separately in :class:`Metrics` for when the
    distinction matters.
    """

    timings: dict[str, float] = field(default_factory=dict)

    @contextmanager
    def measure(self, name: str, clock: Callable[[], float] = time.monotonic) -> Iterator[None]:
        started = clock()
        try:
            yield
        finally:
            # += rather than =: a phase entered twice (paging, retries) should
            # accumulate rather than report only its last visit.
            self.timings[name] = round(self.timings.get(name, 0.0) + (clock() - started), 3)

    def as_dict(self) -> dict[str, float]:
        return dict(self.timings)

    def slowest_phase(self) -> str:
        if not self.timings:
            return ""
        return max(self.timings.items(), key=lambda kv: kv[1])[0]


class Logger:
    """Human lines or JSON lines. Same events either way.

    The ``eufinreg: …`` prefix and the wording are unchanged from every earlier
    version, so existing scripts that grep stderr keep working; ``json`` adds
    machine-readable fields without changing what is said.
    """

    def __init__(
        self,
        *,
        fmt: str = "text",
        quiet: bool = False,
        run_id: str = "",
        stream: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if fmt not in ("text", "json"):
            raise ValueError(f"unknown log format {fmt!r}; use 'text' or 'json'")
        self.fmt = fmt
        self.quiet = quiet
        self.run_id = run_id
        self.stream = stream if stream is not None else sys.stderr
        self.clock = clock

    def event(self, message: str, *, level: str = "info", **fields: Any) -> None:
        if self.quiet and level == "info":
            return
        if self.fmt == "text":
            prefix = "eufinreg: " if level == "info" else f"eufinreg: {level}: "
            print(f"{prefix}{message}", file=self.stream)
        else:
            payload = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())),
                "level": level,
                "run_id": self.run_id,
                "message": message,
                **{k: v for k, v in fields.items() if v is not None},
            }
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=self.stream)
        with contextlib.suppress(AttributeError, ValueError):  # a closed stream
            self.stream.flush()

    def warn(self, message: str, **fields: Any) -> None:
        self.event(message, level="warning", **fields)

    def error(self, message: str, **fields: Any) -> None:
        self.event(message, level="error", **fields)

    def __call__(self, message: str) -> None:
        """So a :class:`Logger` can be passed anywhere a ``log(str)`` is expected."""
        self.event(message)


def prometheus(metrics: Mapping[str, Any], *, prefix: str = "eufinreg") -> str:
    """Render a metrics dict as Prometheus text exposition.

    Deliberately hand-rolled: pulling in a client library to print twenty lines
    of text would be the largest dependency in the project.
    """
    lines: list[str] = []

    def emit(name: str, value: Any, help_text: str, kind: str, labels: str = "") -> None:
        full = f"{prefix}_{name}"
        if not labels:
            lines.append(f"# HELP {full} {help_text}")
            lines.append(f"# TYPE {full} {kind}")
        lines.append(f"{full}{labels} {value}")

    scalars = (
        ("http_requests_total", "requests", "HTTP requests made to registers", "counter"),
        ("http_retries_total", "retries", "Requests retried after a transient failure", "counter"),
        ("http_failures_total", "failures", "Requests that failed after all retries", "counter"),
        ("http_bytes_in_total", "bytes_in", "Bytes received from registers", "counter"),
        ("http_seconds_total", "seconds_requesting", "Seconds spent on the wire", "counter"),
        (
            "wait_seconds_total",
            "seconds_waiting",
            "Seconds spent throttling and backing off",
            "counter",
        ),
    )
    for name, key, help_text, kind in scalars:
        emit(name, metrics.get(key, 0), help_text, kind)

    by_status = metrics.get("by_status") or {}
    if by_status:
        emit("http_responses_total", "", "Responses by status code", "counter", labels="")
        lines.pop()  # the bare metric line; only labelled series follow
        for status, count in sorted(by_status.items()):
            lines.append(f'{prefix}_http_responses_total{{status="{status}"}} {count}')

    by_host = metrics.get("by_host") or {}
    if by_host:
        lines.append(f"# HELP {prefix}_http_requests_by_host_total Requests per register host")
        lines.append(f"# TYPE {prefix}_http_requests_by_host_total counter")
        for host, count in sorted(by_host.items()):
            lines.append(f'{prefix}_http_requests_by_host_total{{host="{_escape(host)}"}} {count}')

    return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
