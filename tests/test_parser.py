from crawler.parser import LinkParser, TextParser, tokenize


def test_tokenize_filters_short_and_stop_words():
    tokens = tokenize("The quick brown fox jumps over the lazy dog in AI systems")
    assert "the" not in tokens
    assert "in" not in tokens
    assert "quick" in tokens
    assert "brown" in tokens
    assert "systems" in tokens


def test_link_parser_resolves_and_deduplicates_links():
    html = """
    <html><body>
      <a href="/a">A1</a>
      <a href="https://example.com/a#frag">A2</a>
      <a href="mailto:test@example.com">mail</a>
    </body></html>
    """
    parser = LinkParser("https://example.com/base")
    parser.feed(html)
    assert parser.links == ["https://example.com/a"]


def test_text_parser_skips_script_and_head_content():
    html = """
    <html>
      <head><title>Alpha Title</title><script>ignore me</script></head>
      <body>Visible text only<script>bad</script></body>
    </html>
    """
    parser = TextParser()
    parser.feed(html)
    counts = parser.word_counts()
    assert "visible" in counts
    assert "ignore" not in counts
    assert "alpha" not in counts
