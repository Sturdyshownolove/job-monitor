"""Normalizer tests (Normalizer.md unit-test checklist)."""

from __future__ import annotations

import pytest

from normalizer import Normalizer, Seniority, WorkMode


@pytest.fixture
def normalizer() -> Normalizer:
    return Normalizer()


class TestNormalizeText:
    def test_whitespace_collapsed(self, normalizer: Normalizer) -> None:
        assert normalizer.normalize_text(" Senior   Product\tDesigner ") == "Senior Product Designer"

    def test_html_entities_decoded(self, normalizer: Normalizer) -> None:
        assert normalizer.normalize_text("Research &amp; Design") == "Research & Design"

    def test_unicode_nfc(self, normalizer: Normalizer) -> None:
        decomposed = "Düsseldorf"  # u + combining diaeresis
        assert normalizer.normalize_text(decomposed) == "Düsseldorf"

    def test_zero_width_removed(self, normalizer: Normalizer) -> None:
        assert normalizer.normalize_text("Pro​duct") == "Product"

    def test_empty_becomes_none(self, normalizer: Normalizer) -> None:
        assert normalizer.normalize_text("   ") is None
        assert normalizer.normalize_text(None) is None

    def test_idempotent(self, normalizer: Normalizer) -> None:
        once = normalizer.normalize_text(" A &amp; B ")
        assert normalizer.normalize_text(once) == once


class TestNormalizeTitle:
    @pytest.mark.parametrize(
        "raw",
        [
            "Senior Product Designer (m/f/d)",
            "Senior Product Designer (all genders)",
            "Senior Product Designer (w/m/d)",
            "Senior Product Designer | Remote Germany",
            "Senior Product Designer - Berlin",
            "Senior Product Designer (Hybrid)",
            " Senior   Product Designer ",
        ],
    )
    def test_variants_collapse_to_same_title(self, normalizer: Normalizer, raw: str) -> None:
        assert normalizer.normalize_title(raw) == "Senior Product Designer"

    def test_slash_titles_preserved(self, normalizer: Normalizer) -> None:
        assert normalizer.normalize_title("UX/UI Designer") == "UX/UI Designer"

    def test_hyphenated_words_preserved(self, normalizer: Normalizer) -> None:
        assert normalizer.normalize_title("Co-Founder Design") == "Co-Founder Design"

    def test_idempotent(self, normalizer: Normalizer) -> None:
        once = normalizer.normalize_title("Product Designer (m/w/d) | Berlin")
        assert normalizer.normalize_title(once) == once


class TestNormalizeCompany:
    def test_whitespace(self, normalizer: Normalizer) -> None:
        assert normalizer.normalize_company("  Google   GmbH ") == "Google GmbH"

    def test_legal_suffix_preserved(self, normalizer: Normalizer) -> None:
        assert normalizer.normalize_company("Acme Ltd.") == "Acme Ltd."

    def test_trailing_separator_stripped(self, normalizer: Normalizer) -> None:
        assert normalizer.normalize_company("SAP SE,") == "SAP SE"

    def test_entities(self, normalizer: Normalizer) -> None:
        assert normalizer.normalize_company("Johnson &amp; Johnson") == "Johnson & Johnson"


class TestNormalizeLocation:
    def test_country_suffix_stripped(self, normalizer: Normalizer) -> None:
        assert normalizer.normalize_location("Berlin, Germany") == "Berlin"

    def test_german_country_suffix(self, normalizer: Normalizer) -> None:
        assert normalizer.normalize_location("Berlin, Deutschland") == "Berlin"

    def test_slash_segment(self, normalizer: Normalizer) -> None:
        assert normalizer.normalize_location("Berlin / Remote") == "Berlin"

    def test_plain_city(self, normalizer: Normalizer) -> None:
        assert normalizer.normalize_location("Munich") == "Munich"


class TestDetectCountry:
    @pytest.mark.parametrize(
        "location",
        ["Berlin", "Munich", "München", "Köln", "Remote Germany", "Hamburg, Deutschland", "Düsseldorf"],
    )
    def test_germany_detected(self, normalizer: Normalizer, location: str) -> None:
        assert normalizer.detect_country(location) == "Germany"

    @pytest.mark.parametrize("location", ["Paris", "London", "Remote Europe", "Amsterdam", None])
    def test_non_germany(self, normalizer: Normalizer, location: str | None) -> None:
        assert normalizer.detect_country(location) is None


class TestWorkMode:
    def test_remote(self, normalizer: Normalizer) -> None:
        assert normalizer.detect_work_mode("Remote Germany") == WorkMode.REMOTE

    def test_hybrid(self, normalizer: Normalizer) -> None:
        assert normalizer.detect_work_mode("Berlin (Hybrid)") == WorkMode.HYBRID

    def test_onsite(self, normalizer: Normalizer) -> None:
        assert normalizer.detect_work_mode("On-site Berlin") == WorkMode.ONSITE

    def test_unknown(self, normalizer: Normalizer) -> None:
        assert normalizer.detect_work_mode("Berlin") == WorkMode.UNKNOWN
        assert normalizer.detect_work_mode(None) == WorkMode.UNKNOWN


class TestSeniority:
    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Senior Product Designer", Seniority.SENIOR),
            ("Junior UX Designer", Seniority.JUNIOR),
            ("Lead Designer", Seniority.LEAD),
            ("Principal Designer", Seniority.PRINCIPAL),
            ("Head of Design", Seniority.HEAD),
            ("Staff Product Designer", Seniority.STAFF),
            ("Design Intern", Seniority.INTERN),
            ("Product Designer", None),
        ],
    )
    def test_detection(self, normalizer: Normalizer, title: str, expected: Seniority | None) -> None:
        assert normalizer.detect_seniority(title) == expected


class TestNormalizeUrl:
    def test_utm_stripped(self, normalizer: Normalizer) -> None:
        assert (
            normalizer.normalize_url("https://example.com/job?id=1&utm_source=abc")
            == "https://example.com/job?id=1"
        )

    def test_tracking_param_stripped(self, normalizer: Normalizer) -> None:
        assert (
            normalizer.normalize_url("https://www.linkedin.com/jobs/view/12345?trk=feed")
            == "https://www.linkedin.com/jobs/view/12345"
        )

    def test_fragment_dropped(self, normalizer: Normalizer) -> None:
        assert normalizer.normalize_url("https://example.com/job#apply") == "https://example.com/job"

    def test_trailing_slash(self, normalizer: Normalizer) -> None:
        assert normalizer.normalize_url("https://example.com/job/") == "https://example.com/job"

    def test_meaningful_params_kept(self, normalizer: Normalizer) -> None:
        assert (
            normalizer.normalize_url("https://de.indeed.com/jobs?q=Product+Designer&l=Germany")
            == "https://de.indeed.com/jobs?q=Product+Designer&l=Germany"
        )

    def test_idempotent(self, normalizer: Normalizer) -> None:
        once = normalizer.normalize_url("https://Example.com/Job/?utm_medium=x&id=2#frag")
        assert normalizer.normalize_url(once) == once
