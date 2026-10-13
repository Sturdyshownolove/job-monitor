"""HTML parser tests (HTMLParser.md unit-test checklist)."""

from __future__ import annotations

import pytest

from html_parser import HTMLParser

SAMPLE = """
<html><body>
  <div class="results">
    <div class="card" data-job-id="1">
      <h2 class="title">  Senior   Product Designer </h2>
      <span class="company">Miro</span>
      <a href="/jobs/view/1" aria-label="open">Open</a>
    </div>
    <div class="card" data-job-id="2">
      <h2 class="title">UX Designer</h2>
    </div>
  </div>
</body></html>
"""


@pytest.fixture
def parser() -> HTMLParser:
    return HTMLParser()


class TestParse:
    def test_valid_html(self, parser: HTMLParser) -> None:
        doc = parser.parse(SAMPLE)
        assert doc.exists(".results")

    def test_empty_html_returns_empty_dom(self, parser: HTMLParser) -> None:
        doc = parser.parse("")
        assert doc.select("div") == []
        assert doc.text() is None

    def test_none_returns_empty_dom(self, parser: HTMLParser) -> None:
        assert parser.parse(None).select("div") == []

    def test_broken_html_still_parses(self, parser: HTMLParser) -> None:
        doc = parser.parse("<div><span>unclosed")
        assert doc.text("span") == "unclosed"

    def test_bytes_input(self, parser: HTMLParser) -> None:
        doc = parser.parse("<p>Käse</p>".encode("utf-8"))
        assert doc.text("p") == "Käse"


class TestSelect:
    def test_select_all(self, parser: HTMLParser) -> None:
        assert len(parser.parse(SAMPLE).select(".card")) == 2

    def test_select_one(self, parser: HTMLParser) -> None:
        el = parser.parse(SAMPLE).select_one("[data-job-id='1']")
        assert el is not None

    def test_missing_returns_none(self, parser: HTMLParser) -> None:
        assert parser.parse(SAMPLE).select_one(".nope") is None

    def test_invalid_selector_safe(self, parser: HTMLParser) -> None:
        doc = parser.parse(SAMPLE)
        assert doc.select(":::bad:::") == []
        assert doc.select_one(":::bad:::") is None

    def test_exists(self, parser: HTMLParser) -> None:
        doc = parser.parse(SAMPLE)
        assert doc.exists(".company")
        assert not doc.exists(".salary")


class TestText:
    def test_text_cleaned(self, parser: HTMLParser) -> None:
        assert parser.parse(SAMPLE).text(".title") == "Senior Product Designer"

    def test_text_of_element(self, parser: HTMLParser) -> None:
        doc = parser.parse(SAMPLE)
        card = doc.select_one("[data-job-id='2']")
        assert doc.text(card) == "UX Designer"

    def test_text_missing_selector(self, parser: HTMLParser) -> None:
        assert parser.parse(SAMPLE).text(".nope") is None


class TestAttribute:
    def test_href(self, parser: HTMLParser) -> None:
        assert parser.parse(SAMPLE).attribute("a", "href") == "/jobs/view/1"

    def test_aria_label(self, parser: HTMLParser) -> None:
        assert parser.parse(SAMPLE).attribute("a", "aria-label") == "open"

    def test_data_attribute(self, parser: HTMLParser) -> None:
        assert parser.parse(SAMPLE).attribute(".card", "data-job-id") == "1"

    def test_missing_attribute(self, parser: HTMLParser) -> None:
        assert parser.parse(SAMPLE).attribute("a", "download") is None

    def test_missing_element(self, parser: HTMLParser) -> None:
        assert parser.parse(SAMPLE).attribute(".nope", "href") is None
