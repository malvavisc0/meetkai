"""Automated guardrails for the Cockpit UI Standard (see STYLEGUIDE.md).

STYLEGUIDE.md's "Review guardrails" section describes two rules that were,
until now, only a manual PR checklist item:

1. No inline ``style="..."`` attributes or ``<style>`` blocks in cockpit
   templates — one-off page styles must become primitives in
   ``cockpit.css`` instead.
2. No hard-coded ``px`` font sizes / padding / margin literals added to
   ``cockpit.css`` outside the ``tokens`` cascade layer (the ``tokens``
   layer is where raw px/rem values legitimately live).

A written guardrail nobody has to run is not a guardrail — this module
makes both rules an actual, always-run check instead of a reminder.
"""

import re
from pathlib import Path

from tests.cockpit.helpers import csrf_post

COCKPIT_STATIC_DIR = Path(__file__).parents[2] / "src" / "kai" / "cockpit" / "static"
COCKPIT_TEMPLATES_DIR = Path(__file__).parents[2] / "src" / "kai" / "cockpit" / "templates"
COCKPIT_CSS = COCKPIT_STATIC_DIR / "cockpit.css"

# Properties where a hard-coded `px` literal signals a one-off value that
# should have used an existing spacing/type token instead.
_TOKEN_BACKED_PROPERTIES = ("font-size", "padding", "margin")
_PX_LITERAL_RE = re.compile(
    r"(?P<prop>" + "|".join(_TOKEN_BACKED_PROPERTIES) + r")\s*:\s*(?P<value>[^;]+);"
)
_INLINE_STYLE_ATTR_RE = re.compile(r"""\bstyle\s*=\s*["']""")
_INLINE_STYLE_TAG_RE = re.compile(r"<style\b", re.IGNORECASE)


def _iter_template_files() -> list[Path]:
    return sorted(COCKPIT_TEMPLATES_DIR.rglob("*.html"))


def _tokens_layer_span(css: str) -> tuple[int, int]:
    """Return the (start, end) character offsets of the `@layer tokens {...}` block."""
    match = re.search(r"@layer\s+tokens\s*\{", css)
    assert match, "cockpit.css must declare an `@layer tokens { ... }` block"
    depth = 1
    i = match.end()
    while depth > 0:
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
        i += 1
    return match.start(), i


def _css_outside_tokens_layer(css: str) -> str:
    start, end = _tokens_layer_span(css)
    # Replace (rather than delete) the tokens layer with blank lines so
    # reported line numbers for any violation stay accurate.
    removed = css[start:end]
    blanked = "\n" * removed.count("\n")
    return css[:start] + blanked + css[end:]


def test_no_inline_style_attributes_in_templates():
    """Cockpit templates must style elements via cockpit.css classes only."""
    offenders = []
    for path in _iter_template_files():
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _INLINE_STYLE_ATTR_RE.search(line):
                rel = path.relative_to(COCKPIT_TEMPLATES_DIR.parent.parent.parent)
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        'Found inline style="..." attribute(s) in cockpit templates. Add a '
        "class to cockpit.css instead of styling inline (see STYLEGUIDE.md):\n"
        + "\n".join(offenders)
    )


def test_no_inline_style_blocks_in_templates():
    """Cockpit templates must never define a page-local <style> block."""
    offenders = []
    for path in _iter_template_files():
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _INLINE_STYLE_TAG_RE.search(line):
                rel = path.relative_to(COCKPIT_TEMPLATES_DIR.parent.parent.parent)
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Found <style> block(s) in cockpit templates. cockpit.css is the "
        "single source of styling truth (see STYLEGUIDE.md):\n" + "\n".join(offenders)
    )


def test_no_hardcoded_px_outside_tokens_layer():
    """No new hard-coded `px` font-size/padding/margin literals outside `tokens`.

    The `tokens` cascade layer is the only place raw px/rem values
    legitimately live (STYLEGUIDE.md). Everywhere else, use the existing
    spacing/type tokens (`var(--space-*)`, `var(--text-*)`) or an existing
    primitive class.
    """
    css = COCKPIT_CSS.read_text()
    scoped = _css_outside_tokens_layer(css)

    offenders = []
    for lineno, line in enumerate(scoped.splitlines(), start=1):
        for match in _PX_LITERAL_RE.finditer(line):
            value = match.group("value")
            # ``var(...)`` and ``clamp(...)`` are responsive/token-backed
            # expressions, not one-off literals — the styleguide documents
            # ``clamp(14px, 4.8vw, 16px)`` as an intentional responsive
            # override (STYLEGUIDE.md, ``.landing-proof__email``).
            if "var(" in value or "clamp(" in value:
                continue
            if re.search(r"\d+(\.\d+)?px", value):
                offenders.append(f"cockpit.css:{lineno}: {line.strip()}")

    assert not offenders, (
        "Found hard-coded px font-size/padding/margin literal(s) outside the "
        "`tokens` layer. Use var(--text-*) / var(--space-*) instead "
        "(see STYLEGUIDE.md):\n" + "\n".join(offenders)
    )


def test_login_preserves_email_and_csrf_form_contract(client):
    response = client.get("/login")

    assert response.status_code == 200
    assert 'class="auth-hero__wordmark"' in response.text
    assert '<form action="/login" method="post"' in response.text
    assert 'name="_csrf"' in response.text
    email = re.search(r'<input\s+id="email"[^>]+>', response.text)
    assert email
    assert 'type="email"' in email.group()
    assert 'name="email"' in email.group()
    assert 'autocomplete="email"' in email.group()
    assert 'aria-describedby="login-help"' in email.group()
    assert "required" in email.group()
    assert 'id="login-help"' in response.text


def test_login_rejects_post_without_csrf(client):
    response = client.post("/login", data={"email": "unknown@example.test"})

    assert response.status_code == 403


def test_invalid_magic_link_offers_working_retry_form(client):
    response = client.get("/login/auth?token=invalid-ui-test-token")

    assert response.status_code == 200
    assert 'id="login-error" role="alert"' in response.text
    assert "Invalid or expired token." in response.text
    assert '<form action="/login" method="post"' in response.text
    assert 'aria-describedby="login-help login-error"' in response.text
    retry = csrf_post(client, "/login", data={"email": "unknown@example.test"})
    assert retry.status_code == 200
    assert "Check your email" in retry.text
    assert 'id="email"' not in retry.text


def test_login_confirmation_does_not_disclose_account_membership(client, user, monkeypatch):
    from kai.cockpit.routes import auth

    monkeypatch.setattr(auth, "auto_approve_enabled", lambda: False)
    approved = csrf_post(client, "/login", data={"email": user.email})
    unknown = csrf_post(client, "/login", data={"email": "unknown@example.test"})

    assert approved.status_code == unknown.status_code == 200
    assert approved.text == unknown.text
    assert "If your address is approved" in approved.text
    assert user.email not in approved.text
