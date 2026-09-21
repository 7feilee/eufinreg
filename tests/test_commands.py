"""The operational verbs, end to end, with every HTTP call mocked.

These assert the things cron depends on: exit codes, refusal to run twice, and
the legacy flag-only CLI still working after subcommands were added.
"""

from __future__ import annotations

import json

import pytest
import responses

from eufinreg.cli import main as cli_main
from eufinreg.commands import EXIT_FAILED, EXIT_LOCKED, EXIT_OK, EXIT_USAGE, is_verb
from eufinreg.sources.finma import UID_CSV_URL
from eufinreg.store import Store

from .conftest import load_text

SOLR_URL = "https://registers.esma.europa.eu/solr/esma_registers_upreg/select"


def _mock_finma(times: int = 1) -> None:
    for _ in range(times):
        responses.add(
            responses.GET, UID_CSV_URL, body=load_text("finma_uid.csv"), content_type="text/csv"
        )


def _config(tmp_path, **extra) -> str:
    payload = {
        "store": str(tmp_path / "store"),
        "user_agent": "eufinreg-tests/0",
        "delay": 0,
        "sources": ["finma"],
        **extra,
    }
    path = tmp_path / "eufinreg.config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _watchlist(tmp_path) -> str:
    path = tmp_path / "watch.json"
    path.write_text(
        json.dumps(
            {
                "name": "cp",
                "entities": [
                    {"id": "ubs", "label": "UBS AG", "identifiers": ["CHE-101.329.561"]},
                    {"id": "ghost", "label": "Nonexistent Treuhand AG"},
                ],
            }
        ),
        encoding="utf-8",
    )
    return str(path)


class TestDispatch:
    def test_verbs_are_recognised(self):
        assert is_verb(["ingest"]) and is_verb(["watch"])
        assert not is_verb(["--source", "upreg"])
        assert not is_verb([])

    @responses.activate
    def test_the_legacy_flag_interface_still_works(self, tmp_path):
        # People have `eufinreg --source finma -o x.csv` in scripts. Adding
        # subcommands must not have moved it.
        _mock_finma()
        out = tmp_path / "x.csv"
        assert cli_main(["--source", "finma", "--delay", "0", "--quiet", "-o", str(out)]) == 0
        assert out.read_text(encoding="utf-8").startswith("Name,City,UID")


class TestIngest:
    @responses.activate
    def test_a_clean_run_exits_zero_and_fills_the_store(self, tmp_path):
        _mock_finma()
        assert cli_main(["ingest", "--config", _config(tmp_path), "--quiet"]) == EXIT_OK
        assert Store(tmp_path / "store").latest("finma") is not None

    @responses.activate
    def test_a_failing_register_exits_one(self, tmp_path):
        responses.add(responses.GET, UID_CSV_URL, status=500)
        code = cli_main(["ingest", "--config", _config(tmp_path), "--quiet"])
        assert code == EXIT_FAILED

    @responses.activate
    def test_the_json_report_is_emitted_for_a_scheduler(self, tmp_path, capsys):
        _mock_finma()
        cli_main(["ingest", "--config", _config(tmp_path), "--quiet", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] == 1 and payload["results"][0]["source"] == "finma"

    @responses.activate
    def test_a_held_lock_exits_three(self, tmp_path):
        _mock_finma()
        store = Store(tmp_path / "store")
        store.init()
        store.acquire_lock()
        try:
            assert cli_main(["ingest", "--config", _config(tmp_path), "--quiet"]) == EXIT_LOCKED
        finally:
            store.release_lock()

    def test_a_bad_config_key_is_a_usage_error(self, tmp_path, capsys):
        path = tmp_path / "bad.json"
        path.write_text('{"stores": "./x"}', encoding="utf-8")
        assert cli_main(["ingest", "--config", str(path), "--quiet"]) == EXIT_USAGE
        assert "unknown config key" in capsys.readouterr().err

    @responses.activate
    def test_source_on_the_command_line_overrides_the_config(self, tmp_path):
        _mock_finma()
        cli_main(
            [
                "ingest",
                "--config",
                _config(tmp_path, sources=["upreg"]),
                "--source",
                "finma",
                "--quiet",
            ]
        )
        assert Store(tmp_path / "store").sources() == ["finma"]


class TestWatch:
    @responses.activate
    def test_events_are_scoped_to_the_watchlist(self, tmp_path, capsys):
        _mock_finma(times=2)
        config = _config(tmp_path)
        cli_main(["ingest", "--config", config, "--quiet"])

        # Second run: UBS gains an authorisation, an unwatched firm changes too.
        store = Store(tmp_path / "store")
        rows = [dict(r) for r in store.latest("finma").load().rows]
        for row in rows:
            if row.get("UID") == "CHE-101.329.561":
                row["authorisation_AuthorisationTypeEN"] += " | Custodian bank"
            elif row.get("Name") == "SIX Swiss Exchange AG":
                row["City"] = "Zug"
        from eufinreg.snapshot import diff_snapshots

        previous = store.latest("finma")
        new = store.add_snapshot(
            "finma", rows, key_columns=("UID", "Name"), stamp="99991231T000000Z"
        )
        store.write_changes("finma", new.stamp, diff_snapshots(previous.load(), new.load()))

        out = tmp_path / "events.csv"
        assert (
            cli_main(
                [
                    "watch",
                    "--config",
                    config,
                    "--watchlist",
                    _watchlist(tmp_path),
                    "-o",
                    str(out),
                    "--quiet",
                ]
            )
            == EXIT_OK
        )
        body = out.read_text(encoding="utf-8")
        assert "UBS AG" in body
        assert "SIX Swiss Exchange" not in body  # changed, but nobody is watching it

    @responses.activate
    def test_coverage_names_entities_no_register_can_see(self, tmp_path, capsys):
        _mock_finma()
        config = _config(tmp_path)
        cli_main(["ingest", "--config", config, "--quiet"])
        cli_main(
            [
                "watch",
                "--config",
                config,
                "--watchlist",
                _watchlist(tmp_path),
                "--coverage",
                "-o",
                str(tmp_path / "e.csv"),
            ]
        )
        assert "not found: ghost" in capsys.readouterr().err

    def test_no_watchlist_is_a_usage_error(self, tmp_path, capsys):
        assert cli_main(["watch", "--config", _config(tmp_path), "--quiet"]) == EXIT_USAGE
        assert "no watchlist" in capsys.readouterr().err

    @responses.activate
    def test_an_empty_store_says_to_ingest_first(self, tmp_path, capsys):
        code = cli_main(
            ["watch", "--config", _config(tmp_path), "--watchlist", _watchlist(tmp_path), "--quiet"]
        )
        assert code == EXIT_USAGE
        assert "run 'eufinreg ingest' first" in capsys.readouterr().err


class TestReceipt:
    @responses.activate
    def test_a_bundle_is_built_and_verifies(self, tmp_path, capsys):
        _mock_finma()
        config = _config(tmp_path)
        cli_main(["ingest", "--config", config, "--quiet"])
        out = tmp_path / "receipt.zip"
        assert (
            cli_main(
                [
                    "receipt",
                    "--config",
                    config,
                    "--source",
                    "finma",
                    "--match",
                    "CHE-101.329.561",
                    "-o",
                    str(out),
                    "--quiet",
                ]
            )
            == EXIT_OK
        )
        assert out.exists()
        assert cli_main(["receipt", "--verify", str(out), "--quiet"]) == EXIT_OK

    @responses.activate
    def test_a_name_match_is_flagged_as_weaker(self, tmp_path, capsys):
        _mock_finma()
        config = _config(tmp_path)
        cli_main(["ingest", "--config", config, "--quiet"])
        cli_main(
            [
                "receipt",
                "--config",
                config,
                "--source",
                "finma",
                "--match",
                "SIX Swiss Exchange AG",
                "-o",
                str(tmp_path / "r.zip"),
            ]
        )
        assert "matched by name" in capsys.readouterr().err

    @responses.activate
    def test_no_match_exits_one_and_says_what_was_searched(self, tmp_path, capsys):
        _mock_finma()
        config = _config(tmp_path)
        cli_main(["ingest", "--config", config, "--quiet"])
        code = cli_main(
            [
                "receipt",
                "--config",
                config,
                "--source",
                "finma",
                "--match",
                "Nobody AG",
                "-o",
                str(tmp_path / "r.zip"),
                "--quiet",
            ]
        )
        assert code == EXIT_FAILED
        assert "Identifiers are matched on" in capsys.readouterr().err

    def test_a_receipt_without_an_archive_says_to_ingest(self, tmp_path, capsys):
        code = cli_main(
            [
                "receipt",
                "--config",
                _config(tmp_path),
                "--source",
                "finma",
                "--match",
                "x",
                "--quiet",
            ]
        )
        assert code == EXIT_USAGE
        assert "issued from the archive" in capsys.readouterr().err


class TestStoreCommands:
    @responses.activate
    def test_status_reports_freshness(self, tmp_path, capsys):
        _mock_finma()
        config = _config(tmp_path)
        cli_main(["ingest", "--config", config, "--quiet"])
        cli_main(["store", "status", "--config", config, "--quiet"])
        out = capsys.readouterr().out
        assert "finma" in out and "cadence_h" in out

    @responses.activate
    def test_verify_catches_an_edited_archive(self, tmp_path, capsys):
        _mock_finma()
        config = _config(tmp_path)
        cli_main(["ingest", "--config", config, "--quiet"])
        import gzip

        ref = Store(tmp_path / "store").latest("finma")
        lines = gzip.decompress(ref.path.read_bytes()).decode("utf-8").splitlines()
        row = json.loads(lines[1])
        row["City"] = "Somewhere Else"
        lines[1] = json.dumps(row, ensure_ascii=False, sort_keys=True)
        ref.path.write_bytes(gzip.compress(("\n".join(lines) + "\n").encode("utf-8")))
        assert cli_main(["store", "verify", "--config", config, "--quiet"]) == EXIT_FAILED

    @responses.activate
    def test_prune_needs_a_retention_window(self, tmp_path, capsys):
        _mock_finma()
        config = _config(tmp_path)
        cli_main(["ingest", "--config", config, "--quiet"])
        assert cli_main(["store", "prune", "--config", config, "--quiet"]) == EXIT_USAGE
        assert "--keep-days" in capsys.readouterr().err


class TestConfigPrecedence:
    """Flags beat the config; the config beats the defaults. Never the reverse."""

    def test_a_flag_default_does_not_shadow_the_config(self, tmp_path):
        from eufinreg.commands import build_parser
        from eufinreg.config import Config, merge_cli
        from eufinreg.http import DEFAULT_USER_AGENT

        path = _config(tmp_path, user_agent="acme-compliance/1.0 (+…; ops@acme.example)")
        args = build_parser().parse_args(["serve", "--config", path])
        config = merge_cli(Config.load(path), args)
        assert config.user_agent != DEFAULT_USER_AGENT
        assert "acme" in config.user_agent

    def test_an_explicit_flag_still_wins(self, tmp_path):
        from eufinreg.commands import build_parser
        from eufinreg.config import Config, merge_cli

        path = _config(tmp_path, user_agent="from-config/1.0")
        args = build_parser().parse_args(
            ["serve", "--config", path, "--user-agent", "from-flag/1.0"]
        )
        assert merge_cli(Config.load(path), args).user_agent == "from-flag/1.0"


class TestNetworkFlags:
    """`-4` must exist wherever a register is contacted, not only on doctor."""

    @pytest.mark.parametrize("verb", ["ingest", "doctor", "serve"])
    def test_ipv4_is_accepted(self, verb):
        from eufinreg.commands import build_parser

        args = build_parser().parse_args([verb, "-4"])
        assert args.ipv4 is True

    def test_the_reading_cli_still_has_it_too(self):
        from eufinreg.cli import build_parser as reading_parser

        assert reading_parser().parse_args(["-4"]).ipv4 is True
