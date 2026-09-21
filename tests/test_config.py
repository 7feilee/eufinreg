"""Run configuration: presets, path resolution, and refusing silent typos."""

from __future__ import annotations

import json

import pytest

from eufinreg.config import Config, SourceSpec, resolve_preset
from eufinreg.http import DEFAULT_USER_AGENT


def _write(tmp_path, payload: dict) -> str:
    path = tmp_path / "eufinreg.config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


class TestLoading:
    def test_defaults_are_usable_without_a_file(self):
        config = Config()
        assert config.user_agent == DEFAULT_USER_AGENT
        assert config.keep_last == 2
        assert config.allow_personal_data is False

    def test_relative_paths_resolve_against_the_config_file(self, tmp_path):
        # A cron line runs from somewhere unpredictable; the config's own
        # directory is the only stable reference.
        path = _write(tmp_path, {"store": "./store", "watchlists": ["lists/cp.json"]})
        config = Config.load(path)
        assert config.store == tmp_path / "store"
        assert config.watchlists == [tmp_path / "lists" / "cp.json"]

    def test_an_absolute_path_is_left_alone(self, tmp_path):
        config = Config.load(_write(tmp_path, {"store": "/var/lib/eufinreg"}))
        assert str(config.store) == "/var/lib/eufinreg"

    def test_a_typo_is_an_error_not_a_setting_that_never_applies(self, tmp_path):
        with pytest.raises(ValueError, match="unknown config key"):
            Config.load(_write(tmp_path, {"user_agnet": "typo/1.0"}))

    def test_malformed_json_names_the_file(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="not valid JSON"):
            Config.load(path)

    def test_sources_accept_both_shapes(self, tmp_path):
        config = Config.load(
            _write(tmp_path, {"sources": ["finma", {"key": "gisa", "select": "konzessioniert"}]})
        )
        assert config.sources[0] == SourceSpec(key="finma")
        assert config.sources[1].select == "konzessioniert"

    def test_a_source_without_a_key_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match='each source is "key"'):
            Config.load(_write(tmp_path, {"sources": [{"select": "bank"}]}))

    def test_retention_zero_means_keep_everything(self, tmp_path):
        # An archive's value is its length; "0" must not read as "delete all".
        assert Config.load(_write(tmp_path, {"retention_days": 0})).retention_days is None


class TestPresets:
    def test_the_dach_preset_covers_all_three_countries(self):
        from eufinreg.sources import get_source

        jurisdictions = {get_source(spec.key).jurisdiction for spec in resolve_preset("dach")}
        assert jurisdictions == {"CH", "AT", "EU"}

    def test_a_preset_in_the_config_populates_the_sources(self, tmp_path):
        config = Config.load(_write(tmp_path, {"preset": "dach"}))
        assert {spec.key for spec in config.sources} >= {"finma", "gisa"}

    def test_explicit_sources_are_added_on_top_of_a_preset(self, tmp_path):
        config = Config.load(_write(tmp_path, {"preset": "dach", "sources": ["ctis"]}))
        keys = [spec.key for spec in config.sources]
        assert "ctis" in keys and "finma" in keys
        assert len(keys) == len(set(keys))


class TestShippedExamples:
    """The files in examples/ are documentation people copy. Keep them valid."""

    def test_the_example_config_parses_and_is_dach(self):
        config = Config.load("examples/eufinreg.config.json")
        assert {spec.key for spec in config.sources} >= {"finma", "gisa", "upreg"}
        assert config.retention_days and config.keep_last >= 1
        assert config.allow_personal_data is False

    def test_the_example_config_does_not_ship_the_default_user_agent(self):
        # Copying a config that leaves the default UA in place is exactly the
        # mistake the docs spend a paragraph warning about.
        assert Config.load("examples/eufinreg.config.json").user_agent != DEFAULT_USER_AGENT

    def test_the_example_watchlist_parses_and_has_no_collisions(self):
        from eufinreg.watchlist import Watchlist

        watchlist = Watchlist.load("examples/counterparties.json")
        assert len(watchlist.entities) >= 3
        assert not watchlist.collisions

    def test_the_example_watchlist_only_references_real_sources(self):
        from eufinreg.sources import source_keys
        from eufinreg.watchlist import Watchlist

        known = set(source_keys())
        for entity in Watchlist.load("examples/counterparties.json").entities:
            assert set(entity.sources) <= known, entity.id
