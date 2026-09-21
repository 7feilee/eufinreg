"""Watchlists: identifier vs name matching, and never pretending to be sure."""

from __future__ import annotations

import pytest

from eufinreg.watchlist import (
    WatchedEntity,
    Watchlist,
    normalise_identifier,
    normalise_name,
)

FINMA_IDS = ("UID", "uid_digits")
FINMA_NAMES = ("Name",)

ROWS = [
    {"UID": "CHE-101.329.561", "uid_digits": "101329561", "Name": "UBS AG", "City": "Zürich"},
    {"UID": "CHE-108.954.607", "uid_digits": "108954607", "Name": "Zürcher Kantonalbank"},
    {"UID": "", "uid_digits": "", "Name": "SIX Swiss Exchange AG"},
    {"UID": "CHE-999.999.999", "uid_digits": "999999999", "Name": "Unrelated Sagl"},
]


def _list(*entities: WatchedEntity) -> Watchlist:
    return Watchlist(name="test", entities=list(entities))


class TestNormalisation:
    def test_an_identifier_matches_across_punctuation(self):
        assert normalise_identifier("CHE-101.329.561") & normalise_identifier("che101329561")

    def test_the_digits_only_form_bridges_two_spellings(self):
        # FINMA prints CHE-101.329.561; the UID web service wants 101329561.
        assert normalise_identifier("CHE-101.329.561") & normalise_identifier("101329561")

    def test_a_short_code_is_not_a_match_key(self):
        # "CH" or "12" appearing in some column must never join two entities.
        assert normalise_identifier("CH") == set()
        assert normalise_identifier("12345") == set()

    def test_names_lose_case_accents_punctuation_and_legal_form(self):
        assert normalise_name("Zürcher Kantonalbank") == normalise_name("ZURCHER KANTONALBANK")
        assert normalise_name("Bank Vontobel AG") == "bank vontobel"
        assert normalise_name("Alpha S.A.") == "alpha"

    def test_holding_is_not_a_legal_form(self):
        # "X Holding AG" and "X AG" are different legal entities. Merging them
        # would be a bug with legal consequences.
        assert normalise_name("Vontobel Holding AG") != normalise_name("Vontobel AG")


class TestMatching:
    def test_an_identifier_match_is_reported_as_confident(self):
        watchlist = _list(WatchedEntity(id="ubs", label="UBS AG", identifiers=("CHE-101.329.561",)))
        match = watchlist.match_row(
            ROWS[0], source="finma", identifier_columns=FINMA_IDS, name_columns=FINMA_NAMES
        )
        assert match and match.kind == "identifier" and match.confident

    def test_a_name_match_is_reported_as_weaker(self):
        watchlist = _list(WatchedEntity(id="six", label="SIX Swiss Exchange AG"))
        match = watchlist.match_row(
            ROWS[2], source="finma", identifier_columns=FINMA_IDS, name_columns=FINMA_NAMES
        )
        assert match and match.kind == "name" and not match.confident

    def test_the_identifier_wins_when_both_could_match(self):
        watchlist = _list(WatchedEntity(id="ubs", label="UBS AG", identifiers=("CHE-101.329.561",)))
        match = watchlist.match_row(
            ROWS[0], source="finma", identifier_columns=FINMA_IDS, name_columns=FINMA_NAMES
        )
        assert match.kind == "identifier"

    def test_similar_names_do_not_match(self):
        watchlist = _list(WatchedEntity(id="v", label="Vontobel Holding AG"))
        assert (
            watchlist.match_row(
                {"Name": "Bank Vontobel AG"},
                source="finma",
                identifier_columns=(),
                name_columns=FINMA_NAMES,
            )
            is None
        )

    def test_a_pipe_joined_cell_is_matched_part_by_part(self):
        watchlist = _list(WatchedEntity(id="a", label="Alpha AG"))
        match = watchlist.match_row(
            {"names": "Beta SA | Alpha AG"},
            source="x",
            name_columns=("names",),
        )
        assert match and match.value == "Alpha AG"

    def test_an_entity_can_be_scoped_to_certain_sources(self):
        watchlist = _list(
            WatchedEntity(
                id="ubs", label="UBS AG", identifiers=("CHE-101.329.561",), sources=("ch-uid",)
            )
        )
        assert watchlist.match_row(ROWS[0], source="finma", identifier_columns=FINMA_IDS) is None
        assert watchlist.match_row(ROWS[0], source="ch-uid", identifier_columns=FINMA_IDS)

    def test_a_key_claimed_twice_matches_nothing(self):
        # Two watched entities sharing an identifier means the list is wrong.
        # Attributing the row to whichever was parsed first would be worse.
        watchlist = _list(
            WatchedEntity(id="a", label="A", identifiers=("CHE-101.329.561",)),
            WatchedEntity(id="b", label="B", identifiers=("CHE-101.329.561",)),
        )
        assert watchlist.collisions
        assert watchlist.match_row(ROWS[0], source="finma", identifier_columns=FINMA_IDS) is None


class TestScopeAndCoverage:
    def test_scope_keeps_only_watched_rows_and_says_why(self):
        watchlist = _list(WatchedEntity(id="ubs", label="UBS AG", identifiers=("CHE-101.329.561",)))
        scoped = watchlist.scope(
            ROWS, source="finma", identifier_columns=FINMA_IDS, name_columns=FINMA_NAMES
        )
        assert len(scoped) == 1
        assert scoped[0]["_entity_id"] == "ubs"
        assert scoped[0]["_match"] == "identifier"
        assert scoped[0]["Name"] == "UBS AG"

    def test_coverage_names_the_entities_nobody_can_see(self):
        # The failure this exists to prevent: quietly monitoring 1 of 2.
        watchlist = _list(
            WatchedEntity(id="ubs", label="UBS AG", identifiers=("CHE-101.329.561",)),
            WatchedEntity(id="ghost", label="Nonexistent AG"),
        )
        report = watchlist.coverage(
            ROWS, source="finma", identifier_columns=FINMA_IDS, name_columns=FINMA_NAMES
        )
        assert report["watched"] == 2
        assert report["matched"] == 1
        assert report["unmatched"] == ["ghost"]

    def test_coverage_separates_strong_from_weak_matches(self):
        watchlist = _list(
            WatchedEntity(id="ubs", label="UBS AG", identifiers=("CHE-101.329.561",)),
            WatchedEntity(id="six", label="SIX Swiss Exchange AG"),
        )
        report = watchlist.coverage(
            ROWS, source="finma", identifier_columns=FINMA_IDS, name_columns=FINMA_NAMES
        )
        assert report["by_identifier"] == 1
        assert report["by_name"] == 1


class TestLoading:
    def test_json_round_trips(self, tmp_path):
        path = tmp_path / "w.json"
        path.write_text(
            '{"name": "cp", "entities": [{"id": "ubs", "label": "UBS AG", '
            '"identifiers": ["CHE-101.329.561"]}]}',
            encoding="utf-8",
        )
        watchlist = Watchlist.load(path)
        assert watchlist.name == "cp"
        assert watchlist.entities[0].identifiers == ("CHE-101.329.561",)

    def test_csv_is_accepted_because_lists_arrive_as_spreadsheets(self, tmp_path):
        path = tmp_path / "w.csv"
        path.write_text(
            "id,label,identifiers,names,sources\n"
            "ubs,UBS AG,CHE-101.329.561,UBS AG|UBS Switzerland AG,finma|ch-uid\n",
            encoding="utf-8",
        )
        entity = Watchlist.load(path).entities[0]
        assert entity.identifiers == ("CHE-101.329.561",)
        assert "UBS Switzerland AG" in entity.names
        assert entity.sources == ("finma", "ch-uid")

    def test_an_entity_with_nothing_to_match_on_is_rejected(self, tmp_path):
        path = tmp_path / "w.json"
        path.write_text('{"entities": [{"id": "x", "note": "tbd"}]}', encoding="utf-8")
        with pytest.raises(ValueError, match="neither an identifier nor a name"):
            Watchlist.load(path)

    def test_a_bare_list_of_names_is_accepted(self):
        watchlist = Watchlist.from_json('["UBS AG", "Julius Bär"]')
        assert len(watchlist.entities) == 2
        assert watchlist.entities[0].label == "UBS AG"

    def test_an_id_is_derived_when_none_is_given(self):
        assert (
            Watchlist.from_json('[{"label": "Bank Vontobel AG"}]').entities[0].id == "bank-vontobel"
        )


class TestCrossRegisterIdentity:
    """One LEI on a watchlist should find the entity in every register that carries one.

    This is the entity-resolution problem in its solvable form: not fuzzy name
    matching, but *a shared identifier the registers themselves publish*.
    """

    def _sources(self):
        from eufinreg.sources import get_source

        return [get_source(key) for key in ("upreg", "mica-casp", "gleif")]

    def test_an_lei_matches_across_three_registers(self):
        watchlist = _list(
            WatchedEntity(id="oney", label="ONEY BANK", identifiers=("969500E07BR6468F5910",))
        )
        rows = {
            # As each register actually spells it, in its own column.
            "upreg": {"ae_lei": "969500E07BR6468F5910", "ae_entityName": "ONEY BANK"},
            "mica-casp": {"ae_lei": "969500E07BR6468F5910", "ae_lei_name": "ONEY BANK"},
            "gleif": {"lei": "969500E07BR6468F5910", "entity.legalName.name": "ONEY BANK"},
        }
        for source in self._sources():
            match = watchlist.match_row(
                rows[source.key],
                source=source.key,
                identifier_columns=source.identifier_columns,
                name_columns=source.name_columns,
            )
            assert match is not None, source.key
            assert match.kind == "identifier", source.key

    def test_every_register_that_carries_an_lei_declares_it(self):
        # If a source stops declaring its identifier column, watchlists quietly
        # fall back to name matching and nobody notices until it mis-attributes.
        for source in self._sources():
            assert source.identifier_columns, source.key
