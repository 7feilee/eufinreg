"""The scheduled path: isolation between sources, and a complete record each run."""

from __future__ import annotations

import pytest
import responses

from eufinreg.config import Config, SourceSpec, resolve_preset
from eufinreg.pipeline import ingest, ingest_source, report
from eufinreg.sources.finma import UID_CSV_URL
from eufinreg.store import Store, utc_stamp

from .conftest import load_text


def _config(tmp_path, *keys: str, **kwargs) -> Config:
    return Config(
        store=tmp_path / "store",
        user_agent="eufinreg-tests/0",
        delay=0.0,
        sources=[SourceSpec(key=key) for key in keys],
        **kwargs,
    )


def _mock_finma(times: int = 1, status: int = 200) -> None:
    for _ in range(times):
        responses.add(
            responses.GET,
            UID_CSV_URL,
            body=load_text("finma_uid.csv") if status == 200 else "boom",
            status=status,
            content_type="text/csv",
        )


class TestOneSource:
    @responses.activate
    def test_a_run_writes_a_snapshot_raw_bytes_and_a_change_file(self, tmp_path):
        _mock_finma()
        store = Store(tmp_path / "store")
        store.init()
        result = ingest_source(store, SourceSpec(key="finma"), _config(tmp_path), stamp="S1")
        assert result.ok and result.rows
        assert (store.snapshots_dir("finma") / "S1.jsonl.gz").exists()  # compressed by default
        assert (store.changes_dir("finma") / "S1.jsonl").exists()
        assert (store.raw_dir("finma", "S1") / "manifest.jsonl").exists()

    @responses.activate
    def test_the_snapshot_records_how_it_was_taken(self, tmp_path):
        _mock_finma()
        store = Store(tmp_path / "store")
        store.init()
        ingest_source(store, SourceSpec(key="finma", select="bank"), _config(tmp_path), stamp="S1")
        meta = store.latest("finma").meta
        assert meta["select"] == "bank"
        assert meta["user_agent"] == "eufinreg-tests/0"
        assert meta["jurisdiction"] == "CH"
        assert meta["cadence_hours"] == 24.0

    @responses.activate
    def test_a_second_run_reports_what_moved(self, tmp_path):
        _mock_finma(times=2)
        store = Store(tmp_path / "store")
        store.init()
        config = _config(tmp_path)
        ingest_source(store, SourceSpec(key="finma"), config, stamp="S1")
        result = ingest_source(store, SourceSpec(key="finma"), config, stamp="S2")
        assert result.ok and result.unchanged
        assert (result.added, result.changed, result.removed) == (0, 0, 0)

    @responses.activate
    def test_no_raw_capture_when_asked(self, tmp_path):
        _mock_finma()
        store = Store(tmp_path / "store")
        store.init()
        ingest_source(
            store, SourceSpec(key="finma"), _config(tmp_path, capture_raw=False), stamp="S1"
        )
        assert not store.raw_dir("finma", "S1").exists()

    def test_a_lookup_only_source_is_refused_with_a_reason(self, tmp_path):
        # ch-uid answers searches; archiving one query's rows would pass off a
        # sample as a register.
        store = Store(tmp_path / "store")
        store.init()
        result = ingest_source(store, SourceSpec(key="ch-uid"), _config(tmp_path))
        assert result.skipped and "does not publish a population" in result.skipped
        assert not result.ok

    def test_an_unknown_source_fails_that_source_only(self, tmp_path):
        store = Store(tmp_path / "store")
        store.init()
        result = ingest_source(store, SourceSpec(key="nope"), _config(tmp_path))
        assert not result.ok and "unknown source" in result.error


class TestIsolation:
    @responses.activate
    def test_one_failing_register_does_not_stop_the_run(self, tmp_path):
        # ESMA being down at 04:00 is not a reason to skip FINMA.
        responses.add(
            responses.GET,
            "https://registers.esma.europa.eu/solr/esma_registers_upreg/select",
            status=500,
        )
        _mock_finma()
        store = Store(tmp_path / "store")
        store.init()
        config = _config(tmp_path, "upreg", "finma")
        config.retries = 0
        results = ingest(store, config)
        summary = report(results)
        assert summary["failed"] == 1
        assert summary["ok"] == 1
        assert store.latest("finma") is not None

    @responses.activate
    def test_every_source_shares_one_stamp(self, tmp_path):
        # A run is a point in time; snapshots taken minutes apart should still
        # line up when they are diffed later.
        _mock_finma(times=2)
        store = Store(tmp_path / "store")
        store.init()
        config = _config(tmp_path, "finma")
        results = ingest(store, config)
        assert results[0].stamp

    @responses.activate
    def test_the_report_is_machine_readable(self, tmp_path):
        _mock_finma()
        store = Store(tmp_path / "store")
        store.init()
        summary = report(ingest(store, _config(tmp_path, "finma")))
        assert set(summary) >= {"sources", "ok", "failed", "skipped", "rows", "results"}
        assert summary["results"][0]["source"] == "finma"


class TestPresets:
    def test_the_dach_preset_is_the_dach_licence_picture(self):
        keys = {spec.key for spec in resolve_preset("dach")}
        assert {"finma", "gisa", "upreg", "eba-psd"} <= keys

    def test_the_all_preset_excludes_lookup_only_sources(self):
        keys = {spec.key for spec in resolve_preset("all")}
        assert "ch-uid" not in keys
        assert "finma" in keys

    def test_an_unknown_preset_names_the_known_ones(self):
        import pytest

        with pytest.raises(ValueError, match="known presets"):
            resolve_preset("benelux")


class TestRefetchPolicy:
    """Do not re-download a monthly file every night."""

    @responses.activate
    def test_a_fresh_archive_entry_is_left_alone(self, tmp_path):
        _mock_finma()
        store = Store(tmp_path / "store")
        store.init()
        config = _config(tmp_path)
        first = ingest_source(
            store, SourceSpec(key="finma"), config, stamp=utc_stamp(1_700_000_000)
        )
        assert first.ok
        second = ingest_source(store, SourceSpec(key="finma"), config, now=1_700_000_000 + 3600)
        assert second.skipped and "nothing can have changed" in second.skipped
        assert len(responses.calls) == 1

    @responses.activate
    def test_it_refetches_once_the_cadence_has_passed(self, tmp_path):
        _mock_finma(times=2)
        store = Store(tmp_path / "store")
        store.init()
        config = _config(tmp_path)
        ingest_source(store, SourceSpec(key="finma"), config, stamp=utc_stamp(1_700_000_000))
        later = 1_700_000_000 + 20 * 3600
        result = ingest_source(
            store, SourceSpec(key="finma"), config, stamp=utc_stamp(later), now=later
        )
        assert result.ok and not result.skipped

    @responses.activate
    def test_jitter_in_the_schedule_does_not_cause_a_skip(self, tmp_path):
        # A nightly timer with a randomised delay can fire 23.9 h after the last
        # run. Skipping then would silently halve the archive's resolution.
        _mock_finma(times=2)
        store = Store(tmp_path / "store")
        store.init()
        config = _config(tmp_path)
        ingest_source(store, SourceSpec(key="finma"), config, stamp=utc_stamp(1_700_000_000))
        nearly_a_day = 1_700_000_000 + int(23.9 * 3600)
        result = ingest_source(
            store,
            SourceSpec(key="finma"),
            config,
            stamp=utc_stamp(nearly_a_day),
            now=nearly_a_day,
        )
        assert result.ok

    @responses.activate
    def test_force_overrides_it(self, tmp_path):
        _mock_finma(times=2)
        store = Store(tmp_path / "store")
        store.init()
        config = _config(tmp_path)
        ingest_source(store, SourceSpec(key="finma"), config, stamp=utc_stamp(1_700_000_000))
        result = ingest_source(
            store,
            SourceSpec(key="finma"),
            config,
            stamp=utc_stamp(1_700_000_000 + 60),
            now=1_700_000_000 + 60,
            force=True,
        )
        assert result.ok and len(responses.calls) == 2

    @responses.activate
    def test_a_source_with_no_declared_cadence_is_always_fetched(self, tmp_path):
        # ESMA publishes no cadence for the A2A endpoint, so there is nothing to
        # infer from and guessing would mean missing changes.
        from eufinreg.sources import get_source

        assert get_source("upreg").cadence_hours is None


class TestFailureIsolation:
    """The promise is that one register cannot stop the run. Test it that way."""

    def _broken(self, monkeypatch, exception):
        from eufinreg.sources import get_source

        source = get_source("finma")
        monkeypatch.setattr(
            source,
            "iter_records",
            lambda fetcher, query: (_ for _ in ()).throw(exception),
        )

    @pytest.mark.parametrize(
        "exception",
        [KeyError("missing column"), TypeError("None is not iterable"), RecursionError("deep")],
    )
    def test_an_unplanned_exception_fails_only_its_own_source(
        self, tmp_path, monkeypatch, exception
    ):
        # Schema drift arrives as a KeyError inside a parser. Catching only the
        # expected exception types meant one register's drift aborted the whole
        # nightly run.
        self._broken(monkeypatch, exception)
        store = Store(tmp_path / "store")
        store.init()
        result = ingest_source(store, SourceSpec(key="finma"), _config(tmp_path), stamp="S1")
        assert not result.ok
        assert type(exception).__name__ in result.error

    def test_the_traceback_is_kept_because_it_failed_last_night(self, tmp_path, monkeypatch):
        self._broken(monkeypatch, KeyError("ae_entityName"))
        store = Store(tmp_path / "store")
        store.init()
        result = ingest_source(store, SourceSpec(key="finma"), _config(tmp_path), stamp="S1")
        assert "KeyError" in result.traceback
        assert len(result.traceback) <= 2000

    def test_an_interrupt_is_the_operator_talking_and_still_propagates(self, tmp_path, monkeypatch):
        self._broken(monkeypatch, KeyboardInterrupt())
        store = Store(tmp_path / "store")
        store.init()
        with pytest.raises(KeyboardInterrupt):
            ingest_source(store, SourceSpec(key="finma"), _config(tmp_path), stamp="S1")

    @responses.activate
    def test_a_broken_source_does_not_stop_the_ones_after_it(self, tmp_path, monkeypatch):
        self._broken(monkeypatch, KeyError("boom"))
        responses.add(
            responses.GET,
            "https://www.gisa.gv.at/gisa-svc-public/GisaPublicV2.svc/ogd/stat02/csv",
            body="ist_historisch,gewerbewortlaut,gewerbeschluessel,gewerbeart\n0,Test,500001,frei\n",
            content_type="text/csv",
        )
        store = Store(tmp_path / "store")
        store.init()
        results = ingest(store, _config(tmp_path, "finma", "gisa-codes"))
        summary = report(results)
        assert summary["failed"] == 1 and summary["ok"] == 1
        assert store.latest("gisa-codes") is not None
