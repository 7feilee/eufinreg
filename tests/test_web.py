"""The single-page UI, checked the way a page can be checked without a browser.

These are contract tests, not appearance tests. They guard the things that break
silently in a hand-written page: a token defined in only one of the three theme
states, a script referencing an element that was renamed, an unescaped value, or
a promise the docs make that the markup quietly dropped.
"""

from __future__ import annotations

import re

import pytest

from eufinreg.server import WEB_ROOT

PAGE = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
SCRIPT = PAGE.split("<script>")[1].split("</script>")[0]
STYLE = PAGE.split("<style>")[1].split("</style>")[0]


class TestThemes:
    """Three states: explicit light, explicit dark, and un-stamped system."""

    @pytest.mark.parametrize(
        "token", ["--ink", "--paper", "--text", "--stamp", "--seal", "--focus"]
    )
    def test_every_token_is_defined_in_all_three_states(self, token):
        # A colour defined only inside a media query never applies to the
        # un-stamped document, which renders one theme's text on the other's
        # ground — the classic unreadable-page bug.
        assert len(re.findall(rf"{token}:", STYLE)) >= 3, token

    def test_the_body_paints_its_own_background(self):
        body = STYLE.split("body {")[1].split("}")[0]
        assert "background: var(--paper)" in body

    def test_the_dark_media_query_yields_to_an_explicit_light_choice(self):
        assert ':root:not([data-theme="light"])' in STYLE
        assert ':root[data-theme="dark"]' in STYLE


class TestWiring:
    def test_every_element_the_script_reaches_for_exists(self):
        ids = set(re.findall(r'id="([^"]+)"', PAGE))
        used = set(re.findall(r'\$\("([^"]+)"\)', SCRIPT))
        assert used <= ids, used - ids

    def test_it_only_calls_routes_the_server_answers(self):
        from eufinreg.server import Handler

        served = {
            c
            for c in Handler.do_GET.__code__.co_consts
            if isinstance(c, str) and c.startswith("/api/")
        }
        called = {m.split("?")[0] for m in re.findall(r"`(/api/[^`$?]*)", SCRIPT)}
        assert called <= served, called - served

    def test_no_external_requests_are_made(self):
        # The page is served by a local process and must work offline; a CDN
        # font or script would fail silently behind the CSP of any host.
        remote = re.findall(r'(?:src|href)="(https?://[^"]+)"', PAGE)
        assert all("github.com" in u or "finma.ch" in u for u in remote), remote


class TestItKeepsThePromisesTheDocsMake:
    def test_register_data_is_escaped_before_it_reaches_the_dom(self):
        """Values from a register are other people's text, not ours.

        Scoped precisely, because a looser rule cannot be kept: inside a
        template literal that builds markup, any expression that *reads a field
        off an API object* must pass through esc(). Locally computed fragments
        (already escaped) and numbers are not the risk; a company name typed by
        a national authority and dropped into innerHTML is.
        """
        assert "const esc =" in SCRIPT
        markup = [lit for lit in re.findall(r"`([^`]*)`", SCRIPT) if "<" in lit]
        assert markup, "expected the page to build markup in template literals"
        reads_api_data = re.compile(
            r"row\[|\br\[|e\._|payload\.|source\.|\bs\.[a-z]|\ba\.[a-z]|v\.value"
        )
        offenders = [
            expr.strip()
            for literal in markup
            for expr in re.findall(r"\$\{([^}]*)\}", literal)
            if reads_api_data.search(expr)
            and "esc(" not in expr
            # A URL parameter is percent-encoded, and a count is a number.
            and "encodeURIComponent(" not in expr
            and not re.search(r"\.(size|length|rows|count)\b|toLocaleString|Math\.", expr)
            # A ternary choosing between two string literals cannot carry data.
            and not re.fullmatch(r'[^"?]*\?\s*"[^"]*"\s*:\s*"[^"]*"', expr.strip())
        ]
        assert not offenders, offenders

    def test_provenance_is_shown_on_every_answer(self):
        # An archived answer and a live one must never look the same.
        assert "archive" in SCRIPT and "captured" in SCRIPT
        assert 'class="tag live"' in SCRIPT

    def test_warnings_have_somewhere_to_go(self):
        assert 'id="warnings"' in PAGE
        assert "notes(p.warnings)" in SCRIPT

    def test_the_cost_of_a_heavy_register_is_declared_before_the_click(self):
        assert "expensive" in SCRIPT and "Heavy" in SCRIPT

    def test_the_footer_repeats_the_caveat_that_matters(self):
        assert "Licensed ≠ operating" in PAGE


class TestApproachability:
    def test_the_first_control_asks_for_a_name_not_a_flag(self):
        # The old page led with --select and --query, which only helps somebody
        # who already knows the CLI.
        placeholder = re.search(r'id="q"[^>]*placeholder="([^"]+)"', PAGE)
        assert placeholder and "name" in placeholder.group(1).lower()

    def test_examples_are_offered_so_an_empty_page_is_not_a_dead_end(self):
        assert len(re.findall(r'data-q="[^"]+"', PAGE)) >= 3

    def test_an_empty_archive_explains_the_command_that_fills_it(self):
        assert "eufinreg ingest" in SCRIPT

    def test_reduced_motion_is_respected(self):
        assert "prefers-reduced-motion" in STYLE

    def test_the_search_field_is_labelled_for_a_screen_reader(self):
        assert re.search(r'id="q"[^>]*aria-label="[^"]+"', PAGE)
