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
