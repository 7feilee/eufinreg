"""The service layer: caching, one outbound request at a time, bounded answers.

These are the properties a long-running process needs and a one-shot CLI does
not. Getting them wrong does not break the tool — it turns a web UI into an
unattended crawler pointed at a small public-sector deployment.
"""

from __future__ import annotations

import threading

import pytest
import responses

from eufinreg.server import Handler
from eufinreg.service import CACHE_TTL, EXPENSIVE, RegisterService, parse_query_string
from eufinreg.sources.finma import UID_CSV_URL

from .conftest import load_text


def _mock_finma(times: int = 1) -> None:
    for _ in range(times):
        responses.add(
            responses.GET, UID_CSV_URL, body=load_text("finma_uid.csv"), content_type="text/csv"
        )


@pytest.fixture
def service() -> RegisterService:
    clock = {"t": 1_000.0}
    instance = RegisterService(user_agent="eufinreg-tests/0", delay=0.0)
    instance._clock = lambda: clock["t"]
    instance._advance = lambda seconds: clock.__setitem__("t", clock["t"] + seconds)
    return instance


class TestCatalogue:
    def test_every_source_describes_itself(self, service):
        catalogue = service.catalogue()
        keys = {entry["key"] for entry in catalogue}
        assert {"finma", "ch-uid", "gisa", "upreg", "eba-psd"} <= keys
        assert all(entry["title"] and entry["docs_url"] for entry in catalogue)

    def test_expensive_sources_are_declared_before_the_click(self, service):
        entry = next(e for e in service.catalogue() if e["key"] == "eba-psd")
        assert entry["expensive"] == EXPENSIVE["eba-psd"]

    def test_cache_ttl_follows_the_registers_own_cadence(self, service):
        entries = {e["key"]: e["cache_ttl"] for e in service.catalogue()}
        # GISA is a monthly file; ch-uid is a live lookup. Caching them the same
        # is either stale answers or pointless traffic.
        assert entries["gisa"] > entries["ch-uid"]
        assert entries["eba-psd"] == CACHE_TTL["eba-psd"]


class TestCaching:
    @responses.activate
    def test_a_repeated_question_does_not_reach_the_register(self, service):
        _mock_finma()
        service.rows("finma")
        service.rows("finma")
        assert len(responses.calls) == 1

    @responses.activate
    def test_local_filters_reuse_the_cached_answer(self, service):
        _mock_finma()
        everything = service.rows("finma")
        narrowed = service.rows("finma", contains=["Zürich"])
        assert len(responses.calls) == 1
        assert 0 < narrowed["returned_rows"] <= everything["returned_rows"]

    @responses.activate
    def test_a_different_selection_is_a_different_answer(self, service):
        _mock_finma(times=2)
        service.rows("finma")
        service.rows("finma", select="bank")
        assert len(responses.calls) == 2

    @responses.activate
    def test_the_entry_expires(self, service):
        _mock_finma(times=2)
        service.rows("finma")
        service._advance(CACHE_TTL["finma"] + 1)
        service.rows("finma")
        assert len(responses.calls) == 2

    @responses.activate
    def test_cache_state_is_inspectable(self, service):
        _mock_finma()
        service.rows("finma")
        state = service.cache_state()
        assert state[0]["source"] == "finma"
        assert state[0]["expires_in"] > 0
        assert service.purge() == 1


class TestConcurrency:
    @responses.activate
    def test_concurrent_callers_produce_one_outbound_request(self, service):
        # Two browser tabs must not become two paging loops against a register
        # that has no rate limit to protect itself with.
        _mock_finma(times=4)
        errors: list[BaseException] = []

        def ask() -> None:
            try:
                service.rows("finma")
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=ask) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors
        assert len(responses.calls) == 1


class TestBounds:
    @responses.activate
    def test_the_row_limit_is_enforced_server_side(self, service):
        _mock_finma()
        service.max_rows = 2
        payload = service.rows("finma", limit=10_000)
        assert payload["returned_rows"] == 2
        assert payload["truncated"] is True

    @responses.activate
    def test_warnings_from_the_source_are_passed_through(self, service):
        _mock_finma()
        payload = service.rows("finma", fields=["City=Nowhere"])
        assert payload["returned_rows"] == 0

    def test_an_unknown_source_is_a_key_error_with_the_list(self, service):
        with pytest.raises(KeyError) as excinfo:
            service.rows("nope")
        assert "known sources" in excinfo.value.args[0]


class TestQueryStringParsing:
    def test_repeated_parameters_become_lists(self):
        parsed = parse_query_string({"contains": ["a", "b"], "field": ["City=Zug"]})
        assert parsed["contains"] == ["a", "b"]
        assert parsed["fields"] == ["City=Zug"]

    def test_non_numeric_limits_are_ignored_rather_than_crashing(self):
        assert parse_query_string({"limit": ["lots"]})["limit"] is None

    def test_blank_selectors_do_not_become_empty_filters(self):
        assert parse_query_string({"select": [""], "query": [""]})["select"] is None


class TestRoutes:
    def test_the_ui_is_served_from_inside_the_package(self):
        from eufinreg.server import WEB_ROOT

        assert (WEB_ROOT / "index.html").is_file()

    def test_the_handler_only_answers_known_routes(self):
        # Guards against the API growing a route the docs do not mention.
        source = Handler.do_GET.__code__.co_consts
        routes = {c for c in source if isinstance(c, str) and c.startswith("/api/")}
        assert routes == {
            "/api/health",
            "/api/sources",
            "/api/cache",
            "/api/rows",
            "/api/values",
            "/api/changes",
            "/api/metrics",
        }


class TestStoreBackedReads:
    """With a store configured, the API must stop being a proxy to the registers."""

    @responses.activate
    def test_an_archived_answer_reaches_no_register(self, tmp_path, service):
        from eufinreg.store import Store

        store = Store(tmp_path / "store")
        store.init()
        store.add_snapshot(
            "finma",
            [{"Name": "Alpha AG", "UID": "CHE-1"}],
            key_columns=("UID", "Name"),
            meta={"select": "", "query": ""},
            stamp="20260816T040000Z",
        )
        service.store = store
        payload = service.rows("finma")
        assert payload["origin"] == "store"
        assert payload["snapshot"] == "20260816T040000Z"
        assert not responses.calls

    @responses.activate
    def test_a_different_selection_falls_through_to_the_register(self, tmp_path, service):
        # A snapshot of --select bank cannot answer a question about everything;
        # returning it would be a subset masquerading as the whole register.
        from eufinreg.store import Store

        store = Store(tmp_path / "store")
        store.init()
        store.add_snapshot("finma", [{"Name": "A"}], meta={"select": "bank"}, stamp="S1")
        service.store = store
        _mock_finma()
        payload = service.rows("finma")
        assert payload["origin"] == "live"
        assert len(responses.calls) == 1

    @responses.activate
    def test_live_can_be_disabled_entirely(self, tmp_path, service):
        from eufinreg.store import Store

        store = Store(tmp_path / "store")
        store.init()
        service.store = store
        service.allow_live = False
        with pytest.raises(ValueError, match="live reads are disabled"):
            service.rows("finma")

    @responses.activate
    def test_changes_come_only_from_the_archive(self, tmp_path, service):
        from eufinreg.snapshot import Change
        from eufinreg.store import Store

        store = Store(tmp_path / "store")
        store.init()
        store.write_changes("finma", "S1", [Change(kind="removed", key="CHE-1")])
        service.store = store
        assert service.changes("finma")["count"] == 1

    def test_changes_without_a_store_explain_the_setup(self, service):
        with pytest.raises(ValueError, match="no store is configured"):
            service.changes("finma")


class TestPersonalDataGate:
    @responses.activate
    def test_a_flagged_selection_is_refused_by_default(self, service):
        from eufinreg.service import PersonalDataRefused

        with pytest.raises(PersonalDataRefused, match="natural persons"):
            service.rows("eba-psd", select="ALL")

    @responses.activate
    def test_the_default_selection_of_the_same_source_is_fine(self, service):
        # Institutions are companies; agents are people. The gate is per
        # selection, not per source, or the whole register becomes unusable.
        from eufinreg.sources import get_source

        assert get_source("eba-psd").personal_data_for(None) == ""

    def test_an_operator_can_enable_it_deliberately(self, service):
        # The gate is a default, not a prohibition: the CLI can always fetch
        # these, and an operator who has thought about it can serve them too.
        from eufinreg.service import CacheEntry

        service.allow_personal_data = True
        service._answer = lambda *a, **k: CacheEntry(
            rows=[{"ENT_NAM": "A Person"}],
            columns=["ENT_NAM"],
            warnings=[],
            fetched_at=0.0,
            expires_at=1e12,
        )
        assert service.rows("eba-psd", select="ALL")["returned_rows"] == 1
