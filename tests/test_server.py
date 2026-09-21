"""The HTTP surface, against a real socket, with a stubbed service.

Offline like everything else: the service is replaced, so no register is
contacted. What is being tested is the part `test_service.py` cannot reach —
status codes, headers, auth and the shape of the bytes on the wire.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from eufinreg.server import Handler
from eufinreg.service import PersonalDataRefused


class StubService:
    """Everything the handler asks for, none of the network."""

    user_agent = "eufinreg-tests/0"
    store = None
    allow_personal_data = False

    def __init__(self) -> None:
        self.observed: list[tuple[str, str]] = []

    def catalogue(self):
        return [{"key": "finma", "title": "FINMA", "docs_url": "https://example"}]

    def rows(self, source, *, fields=(), **kwargs):
        # Parse the filters the way the real service does, so the handler's
        # 400-on-bad-selector path is exercised rather than stubbed away.
        from eufinreg.filters import FieldFilter

        [FieldFilter.parse(text) for text in fields]
        if source == "gated":
            raise PersonalDataRefused("agents are people")
        if source == "unknown":
            raise KeyError("unknown source 'unknown'; known sources: finma")
        return {
            "source": source,
            "rows": [{"Name": "Alpha AG", "City": "Zürich"}],
            "columns": ["Name", "City"],
            "total_rows": 1,
            "returned_rows": 1,
            "truncated": False,
            "warnings": [],
            "origin": "store",
            "snapshot": "20260816T040000Z",
            "fetched_at": "2026-08-16T04:00:00Z",
            "cached_until": "2026-08-16T10:00:00Z",
        }

    def values(self, source, field, **kwargs):
        return {"source": source, "field": field, "values": [], "note": ""}

    def changes(self, source, **kwargs):
        return {"source": source, "since": "", "count": 0, "events": []}

    def cache_state(self):
        return []

    def store_status(self):
        return [{"source": "finma", "stale": False, "rows": 2827}]

    def last_run(self):
        return {"run_id": "20260816T040000Z-abcdef", "failed": 0}

    def metrics_snapshot(self):
        return {
            "requests": 3,
            "retries": 1,
            "failures": 0,
            "bytes_in": 100,
            "seconds_requesting": 1.0,
            "seconds_waiting": 2.0,
            "by_status": {"200": 3},
            "by_host": {"www.finma.ch": 3},
            "served": {"/api/rows ok": 1},
            "cache_entries": 1,
            "cached_rows": 1,
            "archive_sources": 1,
            "archive_stale": 0,
            "archive_rows": 2827,
        }

    def observe(self, route, outcome="ok"):
        self.observed.append((route, outcome))


@pytest.fixture
def server():
    service = StubService()
    Handler.service = service
    Handler.log_message_hook = None
    Handler.token = None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, service
    finally:
        httpd.shutdown()
        httpd.server_close()
        Handler.token = None


def fetch(url, *, method="GET", headers=None):
    request = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


class TestRoutes:
    def test_the_ui_is_served_at_the_root(self, server):
        base, _ = server
        status, body, headers = fetch(f"{base}/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert b"eufinreg" in body

    def test_head_is_answered_because_health_checks_use_it(self, server):
        # BaseHTTPRequestHandler answers 501 without do_HEAD, and a pool marks
        # the node down.
        base, _ = server
        status, body, _ = fetch(f"{base}/", method="HEAD")
        assert status == 200
        assert body == b""

    def test_an_unknown_route_is_404_not_500(self, server):
        base, _ = server
        status, body, _ = fetch(f"{base}/api/nope")
        assert status == 404
        assert "no route" in json.loads(body)["error"]

    def test_a_traversal_path_cannot_read_outside_the_package(self, server):
        base, _ = server
        status, _, _ = fetch(f"{base}/../../etc/passwd")
        assert status in (400, 404)

    def test_rows_carry_their_provenance(self, server):
        base, _ = server
        status, body, _ = fetch(f"{base}/api/rows?source=finma")
        payload = json.loads(body)
        assert status == 200
        assert payload["origin"] == "store"
        assert payload["snapshot"] == "20260816T040000Z"

    def test_csv_comes_back_as_csv(self, server):
        base, _ = server
        status, body, headers = fetch(f"{base}/api/rows?source=finma&format=csv")
        assert status == 200
        assert headers["Content-Type"].startswith("text/csv")
        assert body.startswith(b"Name,City")

    def test_metrics_are_prometheus_text(self, server):
        base, _ = server
        status, body, headers = fetch(f"{base}/api/metrics")
        assert status == 200
        assert "version=0.0.4" in headers["Content-Type"]
        text = body.decode()
        assert "eufinreg_http_requests_total 3" in text
        assert 'eufinreg_http_responses_total{status="200"} 3' in text
        assert "eufinreg_archive_stale 0" in text


class TestErrors:
    def test_a_missing_source_is_a_client_error(self, server):
        base, _ = server
        status, body, _ = fetch(f"{base}/api/rows")
        assert status == 400
        assert "source is required" in json.loads(body)["error"]

    def test_an_unknown_source_is_404_with_the_list(self, server):
        base, _ = server
        status, body, _ = fetch(f"{base}/api/rows?source=unknown")
        assert status == 404
        assert "known sources" in json.loads(body)["error"]

    def test_a_gated_selection_is_403_with_the_reason(self, server):
        base, _ = server
        status, body, _ = fetch(f"{base}/api/rows?source=gated")
        payload = json.loads(body)
        assert status == 403
        assert "personal data" in payload["error"]
        assert "agents are people" in payload["detail"]

    def test_a_malformed_field_filter_is_400(self, server):
        base, _ = server
        status, _, _ = fetch(f"{base}/api/rows?source=finma&field=City")
        assert status == 400


class TestAuthAndCounting:
    def test_a_token_is_required_when_one_is_set(self, server):
        base, _ = server
        Handler.token = "s3cret"
        assert fetch(f"{base}/api/sources")[0] == 401
        ok, _, _ = fetch(f"{base}/api/sources", headers={"Authorization": "Bearer s3cret"})
        assert ok == 200

    def test_the_ui_stays_reachable_without_a_token(self, server):
        # The page itself carries no data; gating it only breaks the browser.
        base, _ = server
        Handler.token = "s3cret"
        assert fetch(f"{base}/")[0] == 200

    def test_every_answer_is_counted_by_route_and_outcome(self, server):
        base, service = server
        fetch(f"{base}/api/rows?source=finma")
        fetch(f"{base}/api/nope")
        assert ("/api/rows", "ok") in service.observed
        assert ("/api/nope", "client") in service.observed
