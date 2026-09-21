"""Counting, correlating and rendering — the parts you need at 04:12."""

from __future__ import annotations

import io
import json

import pytest

from eufinreg.observability import (
    SLOW_SAMPLES,
    Logger,
    Metrics,
    Phases,
    host_of,
    new_run_id,
    prometheus,
)


class TestRunIds:
    def test_they_sort_chronologically(self):
        # The property that matters when you are looking at `ls runs/` rather
        # than querying something.
        assert new_run_id(1_000_000) < new_run_id(2_000_000)

    def test_two_in_the_same_second_are_still_distinct(self):
        assert new_run_id(1_000_000) != new_run_id(1_000_000)

    def test_the_prefix_is_a_readable_timestamp(self):
        assert new_run_id(1_700_000_000).startswith("20231114T")


class TestMetrics:
    def test_a_successful_request_updates_every_axis(self):
        metrics = Metrics()
        metrics.record_request(
            method="GET", url="https://api.gleif.org/x", status=200, seconds=0.5, size=1234
        )
        assert metrics.requests == 1
        assert metrics.bytes_in == 1234
        assert metrics.by_host["api.gleif.org"] == 1
        assert metrics.by_status["200"] == 1
        assert metrics.retries == 0

    def test_retries_and_failures_are_counted_separately(self):
        metrics = Metrics()
        metrics.record_request(
            method="GET", url="https://x/y", status=503, seconds=0.1, outcome="retry"
        )
        metrics.record_request(
            method="GET", url="https://x/y", status=503, seconds=0.1, outcome="error"
        )
        assert metrics.requests == 2
        assert metrics.retries == 1
        assert metrics.failures == 1

    def test_waiting_is_not_wire_time(self):
        # High wait with low wire time is politeness working; the reverse is a
        # register in trouble. One number cannot say both.
        metrics = Metrics()
        metrics.record_request(method="GET", url="https://x/y", status=200, seconds=0.2)
        metrics.record_wait(5.0)
        assert metrics.seconds_requesting == pytest.approx(0.2)
        assert metrics.seconds_waiting == pytest.approx(5.0)

    def test_the_slow_list_is_bounded(self):
        # A million-row ingest must not grow a million-entry list.
        metrics = Metrics()
        for n in range(50):
            metrics.record_request(method="GET", url=f"https://x/{n}", status=200, seconds=n / 10)
        assert len(metrics.slowest) == SLOW_SAMPLES
        assert metrics.slowest[0][0] == pytest.approx(4.9)

    def test_merging_sums_counters_and_keeps_the_worst_samples(self):
        a, b = Metrics(), Metrics()
        a.record_request(method="GET", url="https://a/1", status=200, seconds=1.0, size=10)
        b.record_request(
            method="GET", url="https://b/1", status=404, seconds=9.0, size=20, outcome="error"
        )
        a.merge(b)
        assert a.requests == 2 and a.bytes_in == 30 and a.failures == 1
        assert a.by_host == {"a": 1, "b": 1}
        assert a.slowest[0][2] == "https://b/1"

    def test_negative_durations_cannot_poison_the_totals(self):
        # A clock that goes backwards is rare and its effect should be zero,
        # not a negative total that reads as a bug elsewhere.
        metrics = Metrics()
        metrics.record_request(method="GET", url="https://x/y", status=200, seconds=-3.0)
        metrics.record_wait(-1.0)
        assert metrics.seconds_requesting == 0.0
        assert metrics.seconds_waiting == 0.0

    def test_a_summary_line_reads_like_a_sentence(self):
        metrics = Metrics()
        metrics.record_request(
            method="GET", url="https://x/y", status=200, seconds=1.0, size=2_000_000
        )
        assert "1 request(s)" in metrics.summary()
        assert "2.0 MB in" in metrics.summary()

    def test_host_extraction_survives_rubbish(self):
        assert host_of("https://api.ted.europa.eu/v3/x") == "api.ted.europa.eu"
        assert host_of("not a url") == "?"
        assert host_of("") == "?"


class TestPhases:
    def test_a_phase_entered_twice_accumulates(self):
        # Paging enters `fetch` once per page; reporting only the last visit
        # would understate it by the page count.
        clock = iter([0.0, 1.0, 10.0, 12.0])
        phases = Phases()
        tick = lambda: next(clock)  # noqa: E731
        with phases.measure("fetch", clock=tick):
            pass
        with phases.measure("fetch", clock=tick):
            pass
        assert phases.timings["fetch"] == pytest.approx(3.0)

    def test_a_phase_that_raises_is_still_timed(self):
        phases = Phases()
        with pytest.raises(RuntimeError), phases.measure("fetch"):
            raise RuntimeError("register said no")
        assert "fetch" in phases.timings

    def test_the_slowest_phase_is_the_diagnosis(self):
        phases = Phases(timings={"fetch": 88.0, "flatten": 2.0})
        assert phases.slowest_phase() == "fetch"
        assert Phases().slowest_phase() == ""


class TestLogger:
    def test_text_output_keeps_the_prefix_scripts_grep_for(self):
        stream = io.StringIO()
        Logger(stream=stream).event("fetching finma")
        assert stream.getvalue() == "eufinreg: fetching finma\n"

    def test_json_output_carries_the_run_id_and_the_fields(self):
        stream = io.StringIO()
        Logger(fmt="json", run_id="r-1", stream=stream, clock=lambda: 0).event(
            "done", rows=2827, source="finma"
        )
        payload = json.loads(stream.getvalue())
        assert payload["run_id"] == "r-1"
        assert payload["rows"] == 2827
        assert payload["message"] == "done"
        assert payload["level"] == "info"

    def test_the_message_is_identical_in_both_renderings(self):
        # A human and a log pipeline must never disagree about what happened.
        text, structured = io.StringIO(), io.StringIO()
        Logger(stream=text).event("finma: 2827 rows")
        Logger(fmt="json", stream=structured, clock=lambda: 0).event("finma: 2827 rows")
        assert "finma: 2827 rows" in text.getvalue()
        assert json.loads(structured.getvalue())["message"] == "finma: 2827 rows"

    def test_quiet_silences_info_but_never_errors(self):
        stream = io.StringIO()
        logger = Logger(quiet=True, stream=stream)
        logger.event("progress")
        logger.error("the register said no")
        assert "progress" not in stream.getvalue()
        assert "the register said no" in stream.getvalue()

    def test_none_valued_fields_are_dropped_from_json(self):
        stream = io.StringIO()
        Logger(fmt="json", stream=stream, clock=lambda: 0).event("x", a=1, b=None)
        payload = json.loads(stream.getvalue())
        assert "b" not in payload and payload["a"] == 1

    def test_an_unknown_format_is_refused_at_construction(self):
        with pytest.raises(ValueError, match="unknown log format"):
            Logger(fmt="xml")

    def test_it_is_callable_so_it_drops_into_log_str_call_sites(self):
        stream = io.StringIO()
        logger = Logger(stream=stream)
        logger("plain call")
        assert "plain call" in stream.getvalue()


class TestPrometheus:
    def test_counters_are_rendered_with_help_and_type(self):
        text = prometheus(Metrics().as_dict())
        assert "# HELP eufinreg_http_requests_total" in text
        assert "# TYPE eufinreg_http_requests_total counter" in text
        assert "eufinreg_http_requests_total 0" in text

    def test_status_and_host_become_labelled_series(self):
        metrics = Metrics()
        metrics.record_request(
            method="GET", url="https://api.gleif.org/x", status=429, seconds=0.1, outcome="retry"
        )
        text = prometheus(metrics.as_dict())
        assert 'eufinreg_http_responses_total{status="429"} 1' in text
        assert 'eufinreg_http_requests_by_host_total{host="api.gleif.org"} 1' in text

    def test_label_values_are_escaped(self):
        # A host with a quote in it would otherwise produce a file Prometheus
        # cannot parse — and a broken scrape is a silent monitoring outage.
        text = prometheus({"by_host": {'we"ird\nhost': 1}})
        assert 'host="we\\"ird host"' in text

    def test_the_output_ends_with_a_newline(self):
        assert prometheus(Metrics().as_dict()).endswith("\n")
