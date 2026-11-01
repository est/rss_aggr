from src.sanitize import sanitize_html

BASE = "https://example.com/posts/1"


def test_removes_scripts_handlers_and_comments():
    html = (
        "<p>Hello <strong>world</strong></p>"
        "<!-- tracking comment -->"
        "<script>alert('xss')</script>"
        "<style>body{display:none}</style>"
        '<iframe src="https://evil.test">nested</iframe>'
        '<img src="https://example.com/a.png" onerror="alert(1)">'
        '<a href="javascript:alert(1)" onclick="alert(1)">click</a>'
    )

    result = sanitize_html(html, BASE)
    lowered = result.lower()

    assert "<script" not in lowered
    assert "<style" not in lowered
    assert "<iframe" not in lowered
    assert "onerror" not in lowered
    assert "onclick" not in lowered
    assert "javascript:" not in lowered
    assert "<!--" not in result
    assert "world" in result


def test_relative_links_and_images_use_base_url():
    html = '<p><a href="/about">About</a> <a href="img.png">Image</a> <img src="pic.png" alt="p"></p>'

    result = sanitize_html(html, BASE)

    assert "[About](https://example.com/about)" in result
    assert "[Image](https://example.com/posts/img.png)" in result
    assert "![p](https://example.com/posts/pic.png)" in result


def test_keeps_normal_content_tags_and_tables():
    html = (
        "<h1>Title</h1><p><strong>bold</strong> <em>italic</em></p>"
        "<ul><li>one</li><li>two</li></ul>"
        "<blockquote>quote</blockquote><pre><code>x = 1</code></pre>"
        "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
        "<hr>"
    )

    result = sanitize_html(html, BASE)

    assert "Title" in result
    assert "**bold**" in result
    assert "*italic*" in result or "_italic_" in result
    assert "one" in result and "two" in result
    assert "quote" in result
    assert "x = 1" in result
    assert "A" in result and "B" in result


def test_escaped_code_never_emits_raw_script_markup():
    html = "<pre><code>&lt;script&gt;alert(1)&lt;/script&gt;</code></pre>"

    result = sanitize_html(html, BASE)

    assert "<script" not in result.lower()
    assert "alert(1)" in result
    # The literal text is still visible, but neutralized.
    assert "&lt;script" in result


def test_data_urls_and_tracking_pixels_are_dropped():
    html = (
        '<img src="data:image/gif;base64,R0lGOD">'
        '<img src="https://example.com/pixel.gif" width="1" height="1">'
        '<a href="data:text/html,hi">x</a><p>visible</p>'
    )

    result = sanitize_html(html, BASE)

    assert "data:" not in result
    assert "pixel.gif" not in result
    assert "visible" in result


def test_crlf_and_blank_lines_are_normalized():
    html = "<p>first</p>\r\n\r\n\r\n\r\n<p>second</p>"

    result = sanitize_html(html, BASE)

    assert "\r" not in result
    assert "\n\n\n" not in result
    assert result.endswith("\n")
    assert not result.startswith("\n")
    assert result == "first\n\nsecond\n"


def test_output_is_deterministic_and_malformed_input_never_raises():
    html = "<div><p>a <b>bold<script>bad</script><table><tr><td>cell"

    first = sanitize_html(html, BASE)
    second = sanitize_html(html, BASE)

    assert first == second
    assert "bad" not in first
    assert first.endswith("\n")


def test_empty_input_returns_empty_string():
    assert sanitize_html("", BASE) == ""
    assert sanitize_html("   \r\n  ", BASE) == ""
