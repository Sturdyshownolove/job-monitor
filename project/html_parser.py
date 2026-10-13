"""HTML Parser (HTMLParser.md).

Consistent abstraction over BeautifulSoup + lxml. Scrapers never instantiate
BeautifulSoup directly. Every lookup is safe: missing nodes return None or an
empty list, never raise.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from logger import logger

_PRIMARY_PARSER = "lxml"
_FALLBACK_PARSER = "html.parser"
_WHITESPACE_RE = re.compile(r"\s+")


def chain_select_one(scope: Tag | BeautifulSoup, selectors: tuple[str, ...]) -> tuple[Tag, int] | None:
    """First element matching a selector fallback chain, with the chain index
    that matched (index > 0 means a fallback was used). Invalid selectors are
    skipped safely."""
    for index, selector in enumerate(selectors):
        try:
            element = scope.select_one(selector)
        except Exception:
            continue
        if element is not None:
            return element, index
    return None


def chain_select_all(scope: Tag | BeautifulSoup, selectors: tuple[str, ...]) -> tuple[list[Tag], int] | None:
    """All elements from the first selector in the chain that matches anything."""
    for index, selector in enumerate(selectors):
        try:
            elements = scope.select(selector)
        except Exception:
            continue
        if elements:
            return elements, index
    return None


class ParsedHTML:
    """Queryable DOM wrapper with safe lookups."""

    def __init__(self, soup: BeautifulSoup) -> None:
        self._soup = soup

    @property
    def root(self) -> BeautifulSoup:
        """Document root, for scoped chain queries."""
        return self._soup

    def select(self, selector: str) -> list[Tag]:
        try:
            return self._soup.select(selector)
        except Exception as exc:  # soupsieve raises on invalid selectors
            logger.warning("Invalid selector {!r}: {}", selector, exc)
            return []

    def select_one(self, selector: str) -> Tag | None:
        try:
            return self._soup.select_one(selector)
        except Exception as exc:
            logger.warning("Invalid selector {!r}: {}", selector, exc)
            return None

    def exists(self, selector: str) -> bool:
        return self.select_one(selector) is not None

    def text(self, target: str | Tag | None = None) -> str | None:
        """Clean visible text of a selector, element, or the whole document."""
        element: Tag | BeautifulSoup | None
        if target is None:
            element = self._soup
        elif isinstance(target, str):
            element = self.select_one(target)
        else:
            element = target
        if element is None:
            return None
        cleaned = _WHITESPACE_RE.sub(" ", element.get_text(" ", strip=True)).strip()
        return cleaned or None

    def attribute(self, target: str | Tag, name: str) -> str | None:
        """Safe attribute lookup. Returns None when element or attribute absent."""
        element = self.select_one(target) if isinstance(target, str) else target
        if element is None:
            return None
        value = element.get(name)
        if value is None:
            return None
        if isinstance(value, list):
            value = " ".join(value)
        return str(value) or None


class HTMLParser:
    """Creates ParsedHTML documents. lxml primary, html.parser fallback."""

    def parse(self, raw: str | bytes | None) -> ParsedHTML:
        if raw is None or (isinstance(raw, (str, bytes)) and not raw.strip()):
            logger.warning("Empty HTML received — returning empty DOM")
            return ParsedHTML(BeautifulSoup("", _FALLBACK_PARSER))
        try:
            soup = BeautifulSoup(raw, _PRIMARY_PARSER)
        except Exception as exc:
            logger.warning("lxml parse failed ({}), falling back to html.parser", exc)
            soup = BeautifulSoup(raw, _FALLBACK_PARSER)
        return ParsedHTML(soup)
