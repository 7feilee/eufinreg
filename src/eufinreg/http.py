"""Polite HTTP layer: identifiable User-Agent, inter-request delay, exponential
backoff, and ``Retry-After`` compliance.

Nothing in here is register-specific — the sources in :mod:`eufinreg.sources`
all go through :class:`Fetcher` so that throttling and retry behaviour is
identical no matter which register is being read.
"""

from __future__ import annotations

import email.utils
import json
import random
import re
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from .observability import Metrics

#: Default User-Agent. **Change this to your own repository / contact address**
#: before running the tool at any volume — see the "Polite use" section of the
#: README. Public registers are small public-sector deployments; an anonymous
#: hammering client is the thing that gets IP ranges blocked for everyone.
DEFAULT_USER_AGENT = "eufinreg/0.5.0 (+https://github.com/7feilee/eufinreg; public-register client)"

#: Status codes worth retrying. 408/425/429 are client-ish but transient;
#: 5xx are server-side.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Longest this client will honour a ``Retry-After`` for. The header is a
#: server-controlled number and this process sleeps for it: a misconfigured
#: ``Retry-After: 86400`` would otherwise park a nightly ingest for a day, and
#: the operator would find a job that had been "running" since Tuesday. Beyond
#: the cap the request is abandoned rather than slept on, which is both faster
#: to notice and kinder to the register.
MAX_RETRY_AFTER = 300.0


_ORIGINAL_GETADDRINFO = socket.getaddrinfo


def force_ipv4(enabled: bool = True) -> None:
    """Resolve hostnames to IPv4 only, process-wide — the equivalent of ``curl -4``.

    Python has no Happy Eyeballs: :mod:`urllib3` walks the addresses returned by
    ``getaddrinfo`` in order and waits out the full connect timeout on each one.
    On a network with AAAA records but no working IPv6 route, that turns a 300 ms
    download into a multi-minute stall. ``www.esma.europa.eu`` sits behind a CDN
    that publishes AAAA records, so this is not a hypothetical.

    Off by default: silently disabling IPv6 for everyone would be worse.
    """
    if enabled:

        def ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
            return _ORIGINAL_GETADDRINFO(host, port, socket.AF_INET, type, proto, flags)

        socket.getaddrinfo = ipv4_only  # type: ignore[assignment]
    else:
        socket.getaddrinfo = _ORIGINAL_GETADDRINFO  # type: ignore[assignment]


class FetchError(RuntimeError):
    """Raised when a request keeps failing after all retries are exhausted."""

    def __init__(self, message: str, *, url: str, status: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status = status


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Parse a ``Retry-After`` header into seconds.

    The header comes in two flavours (RFC 9110): delta-seconds, or an HTTP-date.
    Returns ``None`` when the header is absent or unparseable, and never returns
    a negative delay.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return max(0.0, float(int(value)))
    except ValueError:
        pass
    try:
        # Python >= 3.10 raises here; older versions returned None.
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    reference = time.time() if now is None else now
    return max(0.0, parsed.timestamp() - reference)


@dataclass
class RawRecorder:
    """Writes every raw response body to disk, plus a manifest.

    This exists so that when the output looks wrong you can tell whether the bug
    is yours or theirs. The bodies are stored byte-for-byte as received; nothing
    is parsed, normalised or re-encoded.
    """

    directory: Path
    _n: int = 0
    _manifest: Any = None

    def __post_init__(self) -> None:
        self.directory = Path(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._manifest = (self.directory / "manifest.jsonl").open("a", encoding="utf-8")

    def record(self, response: requests.Response, *, label: str = "response") -> Path:
        self._n += 1
        suffix = _suffix_for(response.headers.get("Content-Type", ""))
        # The label comes from a source and lands in a filename. A key with a
        # slash in it (`solr:core/name`) would otherwise write outside the
        # directory the caller asked for.
        path = self.directory / f"{self._n:05d}_{safe_label(label)}{suffix}"
        path.write_bytes(response.content)
        entry = {
            "seq": self._n,
            "file": path.name,
            "url": response.url,
            "status": response.status_code,
            "content_type": response.headers.get("Content-Type"),
            "content_length": len(response.content),
            "last_modified": response.headers.get("Last-Modified"),
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._manifest.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._manifest.flush()
        return path

    def close(self) -> None:
        if self._manifest is not None:
            self._manifest.close()
            self._manifest = None


#: Characters allowed in the label part of a raw-capture filename.
_LABEL_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def strip_nul(text: str) -> str:
    """Remove NUL characters from decoded text.

    Python 3.10's :mod:`csv` raises ``_csv.Error: line contains NUL`` while
    3.11 and later pass it through, so a single stray byte in one cell of a
    register's export aborts a source on some installs and not others. Registers
    do produce them — they are compiled from national submissions. Stripping is
    the only behaviour that is the same everywhere, and losing a NUL loses
    nothing a reader wanted.
    """
    return text.replace("\x00", "") if "\x00" in text else text


def safe_label(label: str) -> str:
    """Reduce a source-supplied label to something safe to put in a filename."""
    cleaned = _LABEL_SAFE.sub("-", str(label or "response")).strip("-.") or "response"
    return cleaned[:60]


def _suffix_for(content_type: str) -> str:
    """A sensible file extension for an archived response body.

    The Content-Type is taken as a hint, not as truth — GISA serves
    ``application/application/x-7z-compressed`` with the prefix doubled, so the
    lookup checks the tail of the type rather than demanding an exact match. The
    name only matters because a human opens these out of an evidence bundle.
    """
    ct = content_type.split(";")[0].strip().lower()
    known = {
        "application/json": ".json",
        "text/json": ".json",
        "text/csv": ".csv",
        "application/xml": ".xml",
        "text/xml": ".xml",
        "text/html": ".html",
        "application/zip": ".zip",
        "application/x-7z-compressed": ".7z",
        "application/gzip": ".gz",
        "application/octet-stream": ".bin",
    }
    if ct in known:
        return known[ct]
    for name, suffix in known.items():
        if ct.endswith(name):
            return suffix
    return ".txt"


@dataclass
class Fetcher:
    """A thin, deliberately boring HTTP client.

    * one request at a time, never concurrent;
    * ``delay`` seconds enforced *between* requests (not before the first one);
    * retries on connection errors and on :data:`RETRY_STATUS`;
    * exponential backoff, capped, with a little jitter;
    * ``Retry-After`` always wins over the computed backoff.

    ``sleep`` and ``monotonic`` are injectable so the tests can run instantly and
    still assert on the exact delays that would have been slept.
    """

    user_agent: str = DEFAULT_USER_AGENT
    delay: float = 1.0
    timeout: float = 60.0
    #: Kept separate from the read timeout so a dead route fails fast instead of
    #: burning the whole budget on a TCP handshake. See :func:`force_ipv4`.
    connect_timeout: float = 10.0
    retries: int = 5
    backoff_base: float = 1.0
    backoff_cap: float = 60.0
    jitter: float = 0.1
    session: requests.Session | None = None
    recorder: RawRecorder | None = None
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    #: Longest a server-supplied ``Retry-After`` will be honoured.
    max_retry_after: float = MAX_RETRY_AFTER
    #: Counters for everything this client did. Always on; see
    #: :mod:`eufinreg.observability`.
    metrics: Metrics = field(default_factory=Metrics)
    _rng: random.Random = field(default_factory=random.Random)
    _last_finished: float | None = None
    #: Every delay actually slept, in order. Handy in tests and in --verbose.
    slept: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent, "Accept-Encoding": "gzip"})

    # -- internals ---------------------------------------------------------

    def _wait(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self.slept.append(seconds)
        self.metrics.record_wait(seconds)
        self.sleep(seconds)

    def _throttle(self) -> None:
        if self._last_finished is None or self.delay <= 0:
            return
        elapsed = self.monotonic() - self._last_finished
        self._wait(self.delay - elapsed)

    def _retry_delay(self, requested: float | None, attempt: int) -> float:
        """How long to wait before the next attempt.

        ``Retry-After`` wins over the computed backoff — it is the server saying
        what it wants — but only up to :attr:`max_retry_after`. A header asking
        for a day is almost always a misconfiguration, and honouring it would
        turn a nightly job into a process that appears hung.
        """
        if requested is None:
            return self._backoff(attempt)
        return min(requested, self.max_retry_after)

    def _backoff(self, attempt: int) -> float:
        raw = min(self.backoff_base * (2**attempt), self.backoff_cap)
        if self.jitter:
            raw += self._rng.uniform(0, self.jitter * raw)
        return raw

    # -- public API --------------------------------------------------------

    def get(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        label: str = "response",
        stream: bool = False,
    ) -> requests.Response:
        """GET ``url``, retrying transient failures. Raises :class:`FetchError`."""
        return self.request("GET", url, params, label=label, stream=stream)

    def request(
        self,
        method: str,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        json_body: Any = None,
        data: bytes | str | None = None,
        headers: Mapping[str, str] | None = None,
        label: str = "response",
        stream: bool = False,
    ) -> requests.Response:
        """One request, with the throttle, retry and recording behaviour.

        ``method`` is separate from :meth:`get` only because a couple of
        registers put their search behind ``POST`` — the etiquette is identical
        either way, and a retried POST here is always a read.

        ``data`` and ``headers`` exist for the registers whose only interface is
        SOAP: a hand-built XML envelope plus a ``SOAPAction`` header. Same
        throttle, same retries, same recorder.
        """
        assert self.session is not None  # set in __post_init__
        last_error: str = "no attempt was made"
        last_status: int | None = None

        for attempt in range(self.retries + 1):
            self._throttle()
            started = self.monotonic()
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    data=data,
                    headers=dict(headers) if headers else None,
                    timeout=(self.connect_timeout, self.timeout),
                    stream=stream,
                )
            except requests.RequestException as exc:  # connection reset, DNS, timeout…
                self._last_finished = self.monotonic()
                last_error = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, (requests.ConnectTimeout, requests.ConnectionError)):
                    last_error += " (if the host publishes AAAA records and your network has "
                    last_error += "no IPv6 route, retry with --ipv4)"
                final = attempt >= self.retries
                self.metrics.record_request(
                    method=method,
                    url=url,
                    status=None,
                    seconds=self._last_finished - started,
                    outcome="error" if final else "retry",
                )
                if final:
                    break
                self._wait(self._backoff(attempt))
                continue

            self._last_finished = self.monotonic()
            elapsed = self._last_finished - started

            if response.status_code in RETRY_STATUS:
                last_status = response.status_code
                last_error = f"HTTP {response.status_code}"
                final = attempt >= self.retries
                self.metrics.record_request(
                    method=method,
                    url=url,
                    status=response.status_code,
                    seconds=elapsed,
                    outcome="error" if final else "retry",
                )
                if final:
                    break
                requested = parse_retry_after(response.headers.get("Retry-After"))
                self._wait(self._retry_delay(requested, attempt))
                continue

            self.metrics.record_request(
                method=method,
                url=url,
                status=response.status_code,
                seconds=elapsed,
                size=int(response.headers.get("Content-Length") or 0)
                or (0 if stream else len(response.content)),
                outcome="ok" if response.ok else "error",
            )

            if self.recorder is not None:
                self.recorder.record(response, label=label)

            if not response.ok:
                raise FetchError(
                    f"HTTP {response.status_code} for {response.url}",
                    url=response.url,
                    status=response.status_code,
                )
            return response

        raise FetchError(
            f"giving up on {url} after {self.retries + 1} attempts ({last_error})",
            url=url,
            status=last_status,
        )

    def get_json(
        self, url: str, params: Mapping[str, Any] | None = None, *, label: str = "response"
    ) -> Any:
        return self._json(self.get(url, params, label=label))

    def post_json(self, url: str, body: Any, *, label: str = "response") -> Any:
        """POST a JSON body and decode a JSON reply.

        Used by registers whose only search interface is a POST — the request is
        still a read, so it inherits the same delay and retry policy.
        """
        return self._json(self.request("POST", url, json_body=body, label=label))

    def post_soap(self, url: str, envelope: str, *, action: str, label: str = "response") -> str:
        """POST a SOAP 1.1 envelope and return the response body as text.

        The Swiss UID register is a WCF ``BasicHttpBinding`` service, which means
        SOAP 1.1: ``text/xml`` plus a quoted ``SOAPAction``. Sending the wrong
        content type there produces an HTTP 415 with no explanation.
        """
        response = self.request(
            "POST",
            url,
            data=envelope.encode("utf-8"),
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f'"{action}"',
            },
            label=label,
        )
        response.encoding = response.encoding or "utf-8"
        return response.text

    @staticmethod
    def _json(response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(
                f"response from {response.url} is not JSON: {exc}", url=response.url
            ) from exc

    def get_text(
        self, url: str, params: Mapping[str, Any] | None = None, *, label: str = "response"
    ) -> str:
        response = self.get(url, params, label=label)
        # The MiCA CSVs are UTF-8 with a BOM; requests guesses ISO-8859-1 for
        # text/* when the server sends no charset, which mangles accented names.
        if response.encoding is None or "charset" not in (
            response.headers.get("Content-Type") or ""
        ):
            response.encoding = "utf-8-sig"
        return strip_nul(response.text)

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
        if self.recorder is not None:
            self.recorder.close()
