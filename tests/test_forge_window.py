"""The forge window's traps, pinned.

Every assertion here is a mistake this repository has already made somewhere
else, arriving in a new file. None of them is a style preference and none of
them is visible from reading the page in a browser, which is exactly why they
are tests.
"""

from __future__ import annotations

import re

from bot.web import forge_window


def _strip_css_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def test_no_control_characters_in_the_stylesheet() -> None:
    """A CSS hex escape in a Python string is an OCTAL escape.

    `content:"\\2192"` is read by Python first: the stylesheet receives a
    control character and the browser draws a tofu box beside the leftover
    digits. Nothing warns, `ruff` does not care, and it is invisible unless
    somebody looks at the rendered page. The fix is always the literal
    character, never the escape.

    `tests/test_web.py` pins this for `render.STYLES`. This file is
    concatenated into it, so the same rule has to hold here or the guard is
    passed by a string that arrived after the check.
    """
    offenders = [
        (i, line)
        for i, line in enumerate(forge_window.CSS.splitlines(), 1)
        if any(ord(ch) < 32 and ch != "\t" for ch in line)
    ]
    assert not offenders, f"control characters in forge_window.CSS: {offenders}"


def test_the_stance_modifiers_are_declared_after_the_base_rule() -> None:
    """A two-class element is decided by DECLARATION ORDER, and it has bitten
    three times.

    `.fw-ingot.after` and `.fw-ingot.after.loosening` are not equally specific,
    but `.fw-ingot.after{border-color:...}` and the stance rules that override
    it are close enough that the winner is whichever is written last. Writing
    the base coat after the stance would leave every window cold, in valid CSS,
    silently.
    """
    css = _strip_css_comments(forge_window.CSS)
    base = css.index(".fw-ingot.after{")
    for stance in ("tightening", "loosening"):
        assert base < css.index(f".fw-ingot.after.{stance}{{"), (
            f".fw-ingot.after.{stance} must be declared after the base rule, "
            "or the stance colour never lands"
        )


def test_reduced_motion_switches_the_window_off_and_never_away() -> None:
    """Fewer moving pixels is not a request to be denied the confirmation.

    The starfield answers `prefers-reduced-motion` by not running at all, which
    is right for decoration. This dialog is the only route to agreeing to a
    change, so the preference may switch off the animation and must not touch
    `display`, `visibility` or `opacity` on the window itself — the same
    reading the Cmd+K console got right by sitting outside the projection
    layer's bail-out.
    """
    css = _strip_css_comments(forge_window.CSS)
    block = css[css.index("prefers-reduced-motion") :]
    assert "animation:none" in block
    for hidden in ("display:none", "visibility:hidden", "opacity:0"):
        assert hidden not in block, (
            f"the reduced-motion block must not {hidden} the forge window: it "
            "is a control, not decoration"
        )


def test_the_window_is_built_and_never_revealed() -> None:
    """The fail-to-visible rule, in the form it takes for a control.

    Nothing may be hidden in CSS and revealed by JavaScript. A dialog that does
    not exist until the client builds it satisfies that trivially; a dialog
    rendered into the markup and hidden by a class does not, and would take the
    whole page's figures down with it if the script threw.
    """
    assert "fw-scrim" not in forge_window.SCRIPT.split("createElement")[0]
    assert "document.body.appendChild(scrim)" in forge_window.SCRIPT


def test_every_value_goes_in_through_text_content() -> None:
    """The server escapes what it renders; the client must not be the place
    that stops doing so.

    This window renders an operator-written reason, an agent-written objection
    and a YAML diff. `innerHTML` anywhere in here would be a cross-site
    scripting hole reachable by typing into a textarea on the same page.
    """
    assert "innerHTML" not in forge_window.SCRIPT
    assert "insertAdjacentHTML" not in forge_window.SCRIPT
    assert "textContent" in forge_window.SCRIPT


def test_declining_resolves_rather_than_rejects() -> None:
    """"The operator said no" is an answer, not an error.

    Rejecting the promise would put a dismissed dialog down the same path as a
    browser throw, and the caller's `.catch` would report a refusal as a
    failure. Every exit resolves.
    """
    assert "reject(" not in forge_window.SCRIPT
    assert "resolve(!!agreed)" in forge_window.SCRIPT


def test_focus_returns_to_something_that_can_hold_it() -> None:
    """`body.focus()` is a silent no-op.

    The shortcut that opens this is usually pressed with nothing focused, so
    `document.activeElement` is the body — and focusing it raises no error
    while leaving a keyboard user stranded at the top of the document. Checked,
    never assumed. Found once already, in the console palette.
    """
    assert "opener !== document.body" in forge_window.SCRIPT
    assert "typeof opener.focus === 'function'" in forge_window.SCRIPT


def test_the_default_target_is_the_way_out() -> None:
    """A stray Enter must not agree to a change.

    The whole argument for this window is that confirming is a separate,
    deliberate act. Focusing the strike button on open would make the fastest
    path through it identical to the one it replaced.
    """
    tail = forge_window.SCRIPT[forge_window.SCRIPT.index("document.body.appendChild") :]
    assert "no.focus(" in tail
    assert "yes.focus(" not in tail


def test_loading_twice_does_not_replace_a_live_handle() -> None:
    """The script is emitted per page and the module is idempotent.

    A second copy reassigning `window.MUDHORN_FORGE` would be harmless today
    and would stop being harmless the moment the handle carries state.
    """
    assert "if (window.MUDHORN_FORGE) return;" in forge_window.SCRIPT
