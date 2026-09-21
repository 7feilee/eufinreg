"""A small read-only HTTP front end for the same sources the CLI reads.

Standard library only — this project has one runtime dependency and a web UI is
not a reason to add a framework. ``http.server`` is a development server and
says so; :doc:`docs/ARCHITECTURE.md <../../docs/ARCHITECTURE.md>` describes what
to put in front of it for anything more than one analyst on a laptop.

The split is the point:

``eufinreg.sources``
    knows the registers. Unchanged by the existence of this file.
``eufinreg.service``
    knows how to be a long-running client of them: cache, one request at a
    time, bounded result sets.
``eufinreg.server``
    knows HTTP. Routing, content types, error codes. Nothing else.
``eufinreg/web/index.html``
    knows the browser. Talks only to the JSON API below, so any other front end
    can replace it.

Routes::

    GET /                       the single-page UI
    GET /api/health             liveness, version, source count
    GET /api/sources            the catalogue, as the sources describe themselves
    GET /api/rows?source=…      rows, with the same selectors the CLI has
    GET /api/values?source=…&field=…    distinct values with counts
    GET /api/cache              what is cached right now, and for how long
    GET /api/metrics            Prometheus text: requests, retries, bytes, staleness
"""

from __future__ import annotations

import contextlib
import json
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .http import DEFAULT_USER_AGENT, FetchError
from .observability import prometheus
from .output import rows_to_csv
from .service import PersonalDataRefused, RegisterService, parse_query_string
from .store import Store

WEB_ROOT = Path(__file__).parent / "web"


class Handler(BaseHTTPRequestHandler):
    """Routing and nothing else."""

    server_version = f"eufinreg/{__version__}"
    service: RegisterService
    log_message_hook: Any = None
    #: When set, every /api route requires `Authorization: Bearer <token>`.
    #: Absent by default, which is safe only because the server binds to
    #: loopback by default. Binding wider without a token is refused in serve().
    token: str | None = None

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        if callable(type(self).log_message_hook):
            type(self).log_message_hook(f"{self.address_string()} {fmt % args}")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        route = getattr(self, "_route", "?")
        outcome = "ok" if status < 400 else ("client" if status < 500 else "server")
        with contextlib.suppress(AttributeError):  # no service attached (tests)
            self.service.observe(route, outcome)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Local tool, no cookies, no credentials: allow a page served elsewhere
        # to read the API during development.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str, **extra: Any) -> None:
        self._json(status, {"error": message, **extra})

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query, keep_blank_values=False)
        self._route = route
        self._started = time.monotonic()
        try:
            if route == "/":
                return self._static("index.html")
            if route.startswith("/api") and not self._authorised():
                return self._error(401, "missing or wrong bearer token")
            if route == "/api/health":
                return self._health()
            if route == "/api/sources":
                return self._json(200, {"sources": self.service.catalogue()})
            if route == "/api/cache":
                return self._json(200, {"entries": self.service.cache_state()})
            if route == "/api/rows":
                return self._rows(params)
            if route == "/api/values":
                return self._values(params)
            if route == "/api/changes":
                return self._changes(params)
            if route == "/api/metrics":
                return self._metrics()
            return self._error(404, f"no route {route}")
        except PersonalDataRefused as exc:
            self._error(
                403,
                "this selection returns rows the register flags as personal data, and this "
                "server was not started with --allow-personal-data",
                detail=str(exc),
            )
        except KeyError as exc:  # unknown source
            self._error(404, exc.args[0] if exc.args else "not found")
        except ValueError as exc:  # a selector the source cannot express
            self._error(400, str(exc))
        except FetchError as exc:
            # The register said no. That is not this server being broken, and
            # the distinction matters when someone is debugging at 2am.
            self._error(502, str(exc), url=exc.url, upstream_status=exc.status)
        except Exception:  # pragma: no cover - last resort
            self._error(500, "internal error", detail=traceback.format_exc(limit=3))

    # A load balancer's health check is usually a HEAD. Without this,
    # BaseHTTPRequestHandler answers 501 and the pool marks the node down.
    # `_send` already omits the body for HEAD.
    do_HEAD = do_GET

    def _authorised(self) -> bool:
        expected = type(self).token
        if not expected:
            return True
        header = self.headers.get("Authorization", "")
        return header.strip() == f"Bearer {expected}"

    def _metrics(self) -> None:
        """Prometheus text. Hand-rolled, because a client library for twenty
        lines of output would be the largest dependency in the project."""
        snapshot = self.service.metrics_snapshot()
        body = prometheus(snapshot)
        extra = [
            "# HELP eufinreg_archive_sources Sources with at least one snapshot",
            "# TYPE eufinreg_archive_sources gauge",
            f"eufinreg_archive_sources {snapshot['archive_sources']}",
            "# HELP eufinreg_archive_stale Sources past twice their register's cadence",
            "# TYPE eufinreg_archive_stale gauge",
            f"eufinreg_archive_stale {snapshot['archive_stale']}",
            "# HELP eufinreg_archive_rows Rows in the newest snapshot of each source",
            "# TYPE eufinreg_archive_rows gauge",
            f"eufinreg_archive_rows {snapshot['archive_rows']}",
            "# HELP eufinreg_cache_entries Answers held in memory",
            "# TYPE eufinreg_cache_entries gauge",
            f"eufinreg_cache_entries {snapshot['cache_entries']}",
        ]
        for key, count in sorted(snapshot.get("served", {}).items()):
            route, _, outcome = key.rpartition(" ")
            extra.append(
                f'eufinreg_api_requests_total{{route="{route}",outcome="{outcome}"}} {count}'
            )
        self._send(
            200,
            (body + "\n".join(extra) + "\n").encode("utf-8"),
            "text/plain; version=0.0.4; charset=utf-8",
        )

    def _health(self) -> None:
        """Liveness *and* readiness: an archive nobody is refreshing is not healthy."""
        archive = self.service.store_status()
        stale = [entry["source"] for entry in archive if entry["stale"]]
        last_run = self.service.last_run()
        # The writer is the thing that usually breaks, so its last outcome is
        # part of this service's health rather than a separate dashboard.
        degraded = bool(stale) or bool(last_run.get("failed"))
        payload = {
            "status": "degraded" if degraded else "ok",
            "version": __version__,
            "sources": len(self.service.catalogue()),
            "user_agent": self.service.user_agent,
            "default_user_agent": self.service.user_agent == DEFAULT_USER_AGENT,
            "store": str(self.service.store.root) if self.service.store else "",
            "archive": archive,
            "stale": stale,
            "last_run": last_run,
            "metrics": {
                k: v
                for k, v in self.service.metrics_snapshot().items()
                if k in ("requests", "retries", "failures", "bytes_in", "cache_entries")
            },
            "serves_personal_data": self.service.allow_personal_data,
        }
        # 200 either way: the process is up. `status` and `stale` are what a
        # monitor should alert on, and hiding them behind a 503 would take the
        # whole API down because one register was late.
        self._json(200, payload)

    def _changes(self, params: dict[str, list[str]]) -> None:
        source = (params.get("source") or [""])[0]
        if not source:
            return self._error(400, "source is required")
        limit = (params.get("limit") or ["500"])[0]
        return self._json(
            200,
            self.service.changes(
                source,
                since=(params.get("since") or [""])[0],
                limit=int(limit) if limit.isdigit() else 500,
            ),
        )

    def _rows(self, params: dict[str, list[str]]) -> None:
        source = (params.get("source") or [""])[0]
        if not source:
            return self._error(400, "source is required; GET /api/sources for the list")
        payload = self.service.rows(source, **parse_query_string(params))
        if (params.get("format") or ["json"])[0] == "csv":
            body = rows_to_csv(payload["rows"], payload["columns"]).encode("utf-8")
            return self._send(200, body, "text/csv; charset=utf-8")
        return self._json(200, payload)

    def _values(self, params: dict[str, list[str]]) -> None:
        source = (params.get("source") or [""])[0]
        field = (params.get("field") or [""])[0]
        if not source or not field:
            return self._error(400, "source and field are both required")
        return self._json(
            200,
            self.service.values(
                source,
                field,
                select=(params.get("select") or [None])[0],
                query=(params.get("query") or [None])[0],
            ),
        )

    def _static(self, name: str) -> None:
        path = (WEB_ROOT / name).resolve()
        if not path.is_file() or WEB_ROOT.resolve() not in path.parents:
            return self._error(404, f"no such file {name}")
        types = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript"}
        self._send(200, path.read_bytes(), types.get(path.suffix, "application/octet-stream"))


def serve(
    *,
    port: int = 8000,
    log=print,
    defaults=None,
    host: str = "127.0.0.1",
    config=None,
    token: str | None = None,
) -> int:
    """Run the server until interrupted.

    Binds to loopback by default. The registers' etiquette rules assume one
    identifiable client; exposing this to a network turns your User-Agent into a
    shared one, which is exactly the thing that gets an IP range blocked — so
    binding wider without a token is refused rather than warned about.
    """
    store = Store(config.store) if config is not None and Path(config.store).is_dir() else None
    service = RegisterService(
        user_agent=getattr(config, "user_agent", None)
        or getattr(defaults, "user_agent", DEFAULT_USER_AGENT),
        delay=getattr(defaults, "delay", 1.0),
        timeout=getattr(defaults, "timeout", 60.0),
        retries=getattr(defaults, "retries", 5),
        store=store,
        allow_personal_data=bool(getattr(config, "allow_personal_data", False)),
    )
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        log(
            f"refusing to bind {host} without --token: an open register proxy is a way to "
            f"get everyone's access withdrawn"
        )
        return 2

    Handler.service = service
    Handler.log_message_hook = staticmethod(log)
    Handler.token = token

    httpd = ThreadingHTTPServer((host, port), Handler)
    log(f"serving http://{host}:{port}/  (Ctrl-C to stop)")
    log(f"api:     http://{host}:{port}/api/sources")
    if store is not None:
        log(f"store:   {store.root} — answers come from the archive, not from the registers")
    else:
        log("store:   none — every uncached request reaches a register. See 'eufinreg ingest'.")
    if service.allow_personal_data:
        log("note: serving rows flagged as personal data, because you asked for it")
    if service.user_agent == DEFAULT_USER_AGENT:
        log("note: still using the default User-Agent — set --user-agent before sharing this")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        log("stopping")
    finally:
        httpd.server_close()
    return 0
