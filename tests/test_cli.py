"""End-to-end CLI runs, with every HTTP call mocked."""

from __future__ import annotations

import csv
import json

import pytest
import responses

from eufinreg.cli import main

from .conftest import load_json, load_text

SOLR_URL = "https://registers.esma.europa.eu/solr/esma_registers_upreg/select"
CSV_URL = "https://www.esma.europa.eu/sites/default/files/2024-12/CASPS.csv"

FAST = ["--delay", "0", "--quiet"]


def _mock_upreg() -> None:
    for n in (1, 2, 3):
        responses.add(responses.GET, SOLR_URL, json=load_json(f"upreg_page{n}.json"), status=200)


def _mock_casps() -> None:
    responses.add(
        responses.GET,
        CSV_URL,
        body=load_text("casps_sample.csv"),
        status=200,
        content_type="text/csv",
    )


def _read_csv(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class TestArgumentHandling:
    def test_list_sources(self, capsys):
        assert main(["--list-sources"]) == 0
        out = capsys.readouterr().out
        assert "upreg" in out and "mica-casp" in out and "solr:<core>" in out

    def test_unknown_source_is_rejected(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            main(["--source", "nope"])
        assert excinfo.value.code == 2
        assert "unknown source" in capsys.readouterr().err

    def test_malformed_field_filter_is_rejected(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            main(["--field", "broken"])
        assert excinfo.value.code == 2
        assert "NAME=VALUE" in capsys.readouterr().err


class TestFetch:
    @responses.activate
    def test_writes_one_csv_row_per_entity(self, tmp_path):
        _mock_upreg()
        out = tmp_path / "e.csv"
        assert main([*FAST, "--source", "upreg", "-o", str(out)]) == 0
        rows = _read_csv(out)
        assert len(rows) == 3  # 8 documents -> 3 entities
        assert rows[0]["ae_entityTypeCode"] == "CSP"

    @responses.activate
    def test_ragged_rows_share_one_header(self, tmp_path):
        _mock_upreg()
        out = tmp_path / "e.csv"
        main([*FAST, "--source", "upreg", "-o", str(out)])
        with open(out, encoding="utf-8", newline="") as handle:
            lines = list(csv.reader(handle))
        assert all(len(line) == len(lines[0]) for line in lines)

    @responses.activate
    def test_select_is_pushed_into_the_solr_query(self, tmp_path):
        _mock_upreg()
        main([*FAST, "--source", "upreg", "--select", "CSP", "-o", str(tmp_path / "e.csv")])
        assert "join+from%3Did+to%3D_root_" in responses.calls[0].request.url

    @responses.activate
    def test_no_flatten_emits_raw_documents(self, tmp_path):
        _mock_upreg()
        out = tmp_path / "e.csv"
        main([*FAST, "--source", "upreg", "--no-flatten", "-o", str(out)])
        assert len(_read_csv(out)) == 8

    @responses.activate
    def test_json_output(self, tmp_path):
        _mock_upreg()
        out = tmp_path / "e.json"
        main([*FAST, "--source", "upreg", "--format", "json", "-o", str(out)])
        assert len(json.loads(out.read_text(encoding="utf-8"))) == 3

    @responses.activate
    def test_jsonl_output(self, tmp_path):
        _mock_upreg()
        out = tmp_path / "e.jsonl"
        main([*FAST, "--source", "upreg", "--format", "jsonl", "-o", str(out)])
        lines = out.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3 and json.loads(lines[0])["ae_entityTypeCode"] == "CSP"

    @responses.activate
    def test_output_goes_to_stdout_by_default(self, capsys):
        _mock_upreg()
        main([*FAST, "--source", "upreg"])
        assert "ae_entityName" in capsys.readouterr().out

    @responses.activate
    def test_limit_caps_the_rows(self, tmp_path):
        _mock_upreg()
        out = tmp_path / "e.csv"
        main([*FAST, "--source", "upreg", "--limit", "1", "-o", str(out)])
        assert len(_read_csv(out)) == 1

    @responses.activate
    def test_csv_source_end_to_end(self, tmp_path):
        _mock_casps()
        out = tmp_path / "c.csv"
        assert main([*FAST, "--source", "mica-casp", "-o", str(out)]) == 0
        rows = _read_csv(out)
        assert len(rows) == 7
        assert "ac_serviceCode_normalised" in rows[0]


class TestFilters:
    @responses.activate
    def test_contains_narrows_the_result(self, tmp_path):
        _mock_casps()
        out = tmp_path / "c.csv"
        main([*FAST, "--source", "mica-casp", "--contains", "bybit", "-o", str(out)])
        rows = _read_csv(out)
        assert len(rows) == 1 and rows[0]["ae_lei_name"] == "Bybit EU GmbH"

    @responses.activate
    def test_field_match_narrows_the_result(self, tmp_path):
        _mock_casps()
        out = tmp_path / "c.csv"
        main([*FAST, "--source", "mica-casp", "--field", "ae_homeMemberState=AT", "-o", str(out)])
        assert all(r["ae_homeMemberState"] == "AT" for r in _read_csv(out))

    @responses.activate
    def test_a_bad_field_name_warns_instead_of_silently_returning_nothing(self, tmp_path, capsys):
        _mock_casps()
        main(
            [
                "--delay",
                "0",
                "--source",
                "mica-casp",
                "--field",
                "ae_homeMemberStat=AT",
                "-o",
                str(tmp_path / "c.csv"),
            ]
        )
        assert "does not exist" in capsys.readouterr().err


class TestInspect:
    @responses.activate
    def test_reports_real_field_names_and_fill_rates(self, capsys):
        _mock_upreg()
        assert main([*FAST, "--source", "upreg", "--inspect", "10"]) == 0
        out = capsys.readouterr().out
        assert "ae_entityName" in out
        assert "sampled" in out

    @responses.activate
    def test_block_structured_sources_are_profiled_before_and_after_flattening(self, capsys):
        _mock_upreg()
        main([*FAST, "--source", "upreg", "--inspect", "10"])
        out = capsys.readouterr().out
        assert "as received from the register" in out
        assert "after flattening" in out
        assert "aeActivity_ac_serviceName" in out

    @responses.activate
    def test_csv_source_is_profiled_once(self, capsys):
        _mock_casps()
        main([*FAST, "--source", "mica-casp", "--inspect", "5"])
        out = capsys.readouterr().out
        assert "ae_lei_name" in out
        assert "after flattening" not in out


class TestListValues:
    @responses.activate
    def test_solr_facets_are_printed(self, capsys):
        responses.add(responses.GET, SOLR_URL, json=load_json("upreg_facet.json"), status=200)
        assert main([*FAST, "--source", "upreg", "--list-values", "ae_entityTypeCode"]) == 0
        out = capsys.readouterr().out
        assert "mif" in out and "7194" in out

    @responses.activate
    def test_csv_values_are_counted_locally(self, capsys):
        _mock_casps()
        main([*FAST, "--source", "mica-casp", "--list-values", "ae_homeMemberState"])
        assert "AT" in capsys.readouterr().out

    @responses.activate
    def test_list_enums_covers_every_declared_field(self, capsys):
        _mock_casps()
        main([*FAST, "--source", "mica-casp", "--list-enums"])
        out = capsys.readouterr().out
        for name in ("ae_competentAuthority", "ae_homeMemberState", "ac_serviceCode_cou"):
            assert name in out


class TestRawCapture:
    @responses.activate
    def test_raw_bodies_and_manifest_are_written(self, tmp_path):
        _mock_upreg()
        raw = tmp_path / "raw"
        main([*FAST, "--source", "upreg", "--raw", str(raw), "-o", str(tmp_path / "e.csv")])
        bodies = sorted(p.name for p in raw.glob("*.json"))
        assert len(bodies) == 3
        manifest = (raw / "manifest.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(manifest) == 3
        assert json.loads(manifest[0])["url"].startswith(SOLR_URL)


class TestErrors:
    @responses.activate
    def test_a_dead_endpoint_exits_nonzero_with_a_message(self, capsys):
        for _ in range(6):
            responses.add(responses.GET, SOLR_URL, status=503)
        assert main([*FAST, "--source", "upreg", "--retries", "1"]) == 1
        assert "eufinreg:" in capsys.readouterr().err

    @responses.activate
    def test_default_user_agent_prints_a_nudge(self, capsys):
        _mock_upreg()
        main(["--delay", "0", "--source", "upreg", "--limit", "1"])
        assert "default User-Agent" in capsys.readouterr().err

    @responses.activate
    def test_a_custom_user_agent_is_sent_and_silences_the_nudge(self, capsys):
        _mock_upreg()
        main(["--delay", "0", "--source", "upreg", "--user-agent", "mine/1.0"])
        assert responses.calls[0].request.headers["User-Agent"] == "mine/1.0"
        assert "default User-Agent" not in capsys.readouterr().err
