"""Retry, backoff and throttling behaviour — asserted without sleeping."""

from __future__ import annotations

import email.utils
import time

import pytest
import requests
import responses

from eufinreg.http import Fetcher, FetchError, RawRecorder, parse_retry_after

URL = "https://example.test/select"


class TestParseRetryAfter:
    def test_seconds(self):
        assert parse_retry_after("120") == 120.0

    def test_http_date(self):
        future = email.utils.formatdate(time.time() + 30, usegmt=True)
        value = parse_retry_after(future)
        assert value is not None and 25 <= value <= 35

    def test_past_date_is_clamped_to_zero(self):
        past = email.utils.formatdate(time.time() - 500, usegmt=True)
        assert parse_retry_after(past) == 0.0

    def test_missing_and_junk(self):
        assert parse_retry_after(None) is None
        assert parse_retry_after("") is None
        assert parse_retry_after("soon-ish") is None


@responses.activate
def test_successful_get_sends_the_user_agent(fetcher):
    responses.add(responses.GET, URL, json={"ok": True}, status=200)
    assert fetcher.get_json(URL) == {"ok": True}
    assert responses.calls[0].request.headers["User-Agent"] == "eufinreg-tests/0"


@responses.activate
def test_retries_then_succeeds(fetcher):
    responses.add(responses.GET, URL, status=503)
    responses.add(responses.GET, URL, status=502)
    responses.add(responses.GET, URL, json={"ok": True}, status=200)
    assert fetcher.get_json(URL) == {"ok": True}
    assert len(responses.calls) == 3


@responses.activate
def test_backoff_is_exponential(fetcher):
    responses.add(responses.GET, URL, status=500)
    responses.add(responses.GET, URL, status=500)
    responses.add(responses.GET, URL, json={}, status=200)
    fetcher.get(URL)
    assert fetcher.slept == [1.0, 2.0]


@responses.activate
def test_retry_after_beats_the_computed_backoff(fetcher):
    responses.add(responses.GET, URL, status=429, headers={"Retry-After": "7"})
    responses.add(responses.GET, URL, json={}, status=200)
    fetcher.get(URL)
    assert fetcher.slept == [7.0]


@responses.activate
def test_gives_up_after_the_configured_retries(fetcher):
    for _ in range(3):
        responses.add(responses.GET, URL, status=503)
    with pytest.raises(FetchError) as excinfo:
        fetcher.get(URL)
    assert excinfo.value.status == 503
    assert len(responses.calls) == 3  # 1 attempt + 2 retries


@responses.activate
def test_non_retryable_error_fails_immediately(fetcher):
    responses.add(responses.GET, URL, status=404)
    with pytest.raises(FetchError) as excinfo:
        fetcher.get(URL)
    assert excinfo.value.status == 404
    assert len(responses.calls) == 1


@responses.activate
def test_delay_is_enforced_between_requests_but_not_before_the_first():
    clock = {"t": 0.0}

    def fake_sleep(seconds: float) -> None:
        clock["t"] += seconds

    instance = Fetcher(delay=1.0, sleep=fake_sleep, monotonic=lambda: clock["t"], jitter=0.0)
    responses.add(responses.GET, URL, json={}, status=200)
    responses.add(responses.GET, URL, json={}, status=200)
    instance.get(URL)
    instance.get(URL)
    assert instance.slept == [1.0]


@responses.activate
def test_connection_errors_hint_at_the_ipv4_flag(fetcher):
    for _ in range(3):
        responses.add(responses.GET, URL, body=requests.ConnectionError("no route"))
    with pytest.raises(FetchError, match=r"--ipv4"):
        fetcher.get(URL)


@responses.activate
def test_bom_encoded_csv_is_decoded_as_utf8(fetcher):
    responses.add(
        responses.GET,
        URL,
        body="﻿name\nZürich AG\n".encode(),
        status=200,
        content_type="text/csv",
    )
    assert "Zürich AG" in fetcher.get_text(URL)


@responses.activate
def test_non_json_body_raises_a_useful_error(fetcher):
    responses.add(responses.GET, URL, body="<html>maintenance</html>", status=200)
    with pytest.raises(FetchError, match="not JSON"):
        fetcher.get_json(URL)


@responses.activate
def test_raw_recorder_writes_bodies_and_a_manifest(tmp_path, fetcher):
    recorder = RawRecorder(tmp_path / "raw")
    fetcher.recorder = recorder
    responses.add(
        responses.GET,
        URL,
        json={"hello": "world"},
        status=200,
        headers={"Last-Modified": "Wed, 12 Aug 2026 08:47:01 GMT"},
    )
    fetcher.get(URL, label="page1")
    recorder.close()

    body = tmp_path / "raw" / "00001_page1.json"
    assert body.read_text(encoding="utf-8") == '{"hello": "world"}'
    manifest = (tmp_path / "raw" / "manifest.jsonl").read_text(encoding="utf-8")
    assert '"status": 200' in manifest
    assert "Wed, 12 Aug 2026 08:47:01 GMT" in manifest


class TestRetryAfterCap:
    """A server-supplied wait is honoured, but not without limit."""

    @responses.activate
    def test_a_sane_retry_after_is_honoured_exactly(self, fetcher):
        responses.add(responses.GET, URL, status=503, headers={"Retry-After": "7"})
        responses.add(responses.GET, URL, json={"ok": True})
        fetcher.get(URL)
        assert 7 in fetcher.slept

    @responses.activate
    def test_an_absurd_retry_after_is_capped(self, fetcher):
        # Retry-After: 86400 would park a nightly ingest for a day, and the
        # operator would find a job that had been "running" since Tuesday.
        responses.add(responses.GET, URL, status=503, headers={"Retry-After": "86400"})
        responses.add(responses.GET, URL, json={"ok": True})
        fetcher.max_retry_after = 300.0
        fetcher.get(URL)
        assert max(fetcher.slept) == 300.0

    @responses.activate
    def test_no_header_still_uses_exponential_backoff(self, fetcher):
        responses.add(responses.GET, URL, status=503)
        responses.add(responses.GET, URL, json={"ok": True})
        fetcher.get(URL)
        assert fetcher.slept  # the computed backoff, not a server instruction


class TestRawLabelSafety:
    def test_a_label_with_a_path_separator_cannot_escape_the_directory(self, tmp_path):
        from eufinreg.http import RawRecorder, safe_label

        assert "/" not in safe_label("solr:esma/../../etc/passwd")
        recorder = RawRecorder(tmp_path / "raw")
        assert safe_label("../../evil") == "evil"
        recorder.close()

    def test_a_label_keeps_its_readable_shape(self):
        from eufinreg.http import safe_label

        assert safe_label("upreg-p0001") == "upreg-p0001"
        assert safe_label("") == "response"


class TestMetrics:
    @responses.activate
    def test_a_successful_request_is_counted(self, fetcher):
        responses.add(responses.GET, URL, json={"ok": True})
        fetcher.get(URL)
        assert fetcher.metrics.requests == 1
        assert fetcher.metrics.by_status["200"] == 1
        assert fetcher.metrics.by_outcome["ok"] == 1

    @responses.activate
    def test_retries_and_the_final_failure_are_distinguished(self, fetcher):
        for _ in range(fetcher.retries + 1):
            responses.add(responses.GET, URL, status=503)
        with pytest.raises(FetchError):
            fetcher.get(URL)
        # Every attempt counted; only the last one is a failure.
        assert fetcher.metrics.requests == fetcher.retries + 1
        assert fetcher.metrics.retries == fetcher.retries
        assert fetcher.metrics.failures == 1

    @responses.activate
    def test_waiting_is_counted_separately_from_the_wire(self, fetcher):
        # High wait with low request time is politeness working; the reverse is
        # a register in trouble. They must not be one number.
        responses.add(responses.GET, URL, status=503, headers={"Retry-After": "5"})
        responses.add(responses.GET, URL, json={"ok": True})
        fetcher.get(URL)
        assert fetcher.metrics.seconds_waiting >= 5
