"""
HTML parsing utilities for link extraction and text extraction.
Uses only Python stdlib (html.parser, urllib.parse, re).
"""

import re
from collections import Counter
from html.parser import HTMLParser
from typing import Dict, List
from urllib.parse import urljoin, urlparse


STOP_WORDS: frozenset = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "up", "about", "into", "through", "during",
    "is", "are", "was", "were", "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "will", "would", "could", "should", "may", "might",
    "shall", "can", "this", "that", "these", "those", "it", "its", "they",
    "them", "their", "we", "our", "you", "your", "he", "she", "his", "her",
    "not", "no", "nor", "so", "yet", "both", "either", "neither", "each",
    "few", "more", "most", "other", "some", "such", "than", "too", "very",
    "just", "then", "there", "when", "where", "who", "which", "what", "how",
    "all", "any", "only", "same", "also", "back", "after", "use", "two",
    "out", "if", "as", "its", "via", "per", "one", "get", "let", "new",
    "now", "see", "set", "way", "got", "put", "run", "try", "was", "own",
})


def tokenize(text: str) -> List[str]:
    """Lowercase alphabetic tokens ≥ 3 chars with stop words removed."""
    return [t for t in re.findall(r"[a-z]{3,}", text.lower()) if t not in STOP_WORDS]


class LinkParser(HTMLParser):
    """Extracts and resolves all href links from <a> tags in an HTML document."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._seen: set = set()
        self.links: List[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href") or ""
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "data:")):
            return

        absolute = urljoin(self._base_url, href)

        # Strip fragment identifier
        fragment_pos = absolute.find("#")
        if fragment_pos != -1:
            absolute = absolute[:fragment_pos]

        if not absolute:
            return

        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return

        if absolute not in self._seen:
            self._seen.add(absolute)
            self.links.append(absolute)

    def error(self, message: str) -> None:  # pragma: no cover
        pass


class TextParser(HTMLParser):
    """
    Extracts visible text from HTML, suppressing content inside
    <script>, <style>, <noscript>, <head> tags.

    Title text is collected separately so callers can weight it differently.
    """

    _OPAQUE_TAGS: frozenset = frozenset({"script", "style", "noscript", "head", "meta",
                                         "link", "iframe", "object", "embed", "canvas",
                                         "svg", "math"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._opaque_depth: int = 0
        self._in_title: bool = False
        self._text_parts: List[str] = []
        self._title_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        lower = tag.lower()
        if lower in self._OPAQUE_TAGS:
            self._opaque_depth += 1
        elif lower == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower in self._OPAQUE_TAGS:
            self._opaque_depth = max(0, self._opaque_depth - 1)
        elif lower == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._opaque_depth > 0:
            return
        stripped = data.strip()
        if not stripped:
            return
        if self._in_title:
            self._title_parts.append(stripped)
        else:
            self._text_parts.append(stripped)

    def error(self, message: str) -> None:  # pragma: no cover
        pass

    @property
    def title(self) -> str:
        return " ".join(self._title_parts)

    def word_counts(self) -> Dict[str, int]:
        """
        Returns token frequencies for all visible text.
        Title tokens are included once (equal weight); callers who want
        title boosting should multiply title counts before merging.
        """
        combined = " ".join(self._title_parts + self._text_parts)
        return dict(Counter(tokenize(combined)))
