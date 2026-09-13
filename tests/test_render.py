from interact.render import CSP, render_page, render_tombstone


def _render(html, **config):
    return render_page(html, config={"pageId": "abc", **config}, title="Preços")


def test_fragment_is_wrapped_in_a_full_document():
    out = _render("<h1>Oi</h1>")
    assert out.startswith("<!doctype html>")
    assert '<main class="ix-page">\n<h1>Oi</h1>' in out
    assert "<title>Preços</title>" in out
    assert "window.Interact" in out and "telegram-web-app.js" in out and "sendData" in out
    # The CSP must come before any script so it governs them.
    assert out.index("Content-Security-Policy") < out.index("telegram-web-app.js")


def test_csp_allows_telegram_and_no_network():
    assert "connect-src 'self'" in CSP and "form-action 'none'" in CSP
    assert "script-src 'self' 'unsafe-inline' https://telegram.org" in CSP


def test_full_document_gets_injected_after_head_and_keeps_its_title():
    out = _render("<!DOCTYPE html><html><head><title>Mine</title></head><body>x</body></html>")
    assert out.count("<title>") == 1 and "<title>Mine</title>" in out
    assert out.index("<head>") < out.index("Content-Security-Policy") < out.index("<title>Mine</title>")


def test_document_without_head_gets_one():
    out = _render("<html><body>x</body></html>")
    assert "<html>\n<head>\n" in out and "</head>\n<body>x</body>" in out


def test_config_cannot_break_out_of_the_script_tag():
    out = _render("<p>x</p>", title="</script><script>alert(1)</script>")
    assert "</script><script>alert(1)" not in out
    assert "\\u003c/script>" in out


def test_tombstone_escapes_title():
    out = render_tombstone("<b>x</b>", "expired")
    assert "Página expirada" in out and "&lt;b&gt;x&lt;/b&gt;" in out
