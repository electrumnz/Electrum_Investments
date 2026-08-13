"""Wisps and symbiosis: the properties that make them decoration.

A visual layer earns the right to exist by being unable to take anything away
when it fails. These are the assertions that keep that true.
"""

from __future__ import annotations

import re

from bot.web import dream_fx


def _strip_css_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _code(script: str) -> str:
    """The script with its block comments removed.

    These assertions are about what the client DOES. The comments explain why
    it does it, and several of them name the thing being ruled out — the note
    on `sessionStorage` says the word `localStorage` in the course of
    explaining why it is not used. Matching against the prose would fail the
    build for documenting a decision, which is the opposite of what these are
    for.
    """
    return re.sub(r"/\*.*?\*/", "", script, flags=re.S)


def _reduced_motion_block(css: str) -> str:
    stripped = _strip_css_comments(css)
    return stripped[stripped.index("prefers-reduced-motion") :]


def test_no_control_characters_in_the_stylesheet() -> None:
    """A CSS hex escape in a Python string is read by Python as an octal one.

    The stylesheet receives a control character and the browser draws a tofu
    box beside the leftover digits. Nothing warns and `ruff` does not care.
    """
    offenders = [
        (i, line)
        for i, line in enumerate(dream_fx.CSS.splitlines(), 1)
        if any(ord(ch) < 32 and ch != "\t" for ch in line)
    ]
    assert not offenders, f"control characters in dream_fx.CSS: {offenders}"


def test_a_fused_card_is_fused_without_the_animation() -> None:
    """The fail-to-visible rule, in the place it is easiest to get wrong here.

    It is very natural to draw two cards and have JavaScript merge them, and
    that fails to *two cards and a lie*. The card is drawn fused in the markup;
    the animation is the arrival of something already true. So the client may
    only ever add the transient `joining` class, never `fused` itself.
    """
    code = _code(dream_fx.SCRIPT)
    assert "'fused'" not in code
    assert '"fused"' not in code
    assert "classList.add('joining')" in code


def test_reduced_motion_takes_the_motion_and_never_the_meaning() -> None:
    """A fused card stays fused, and the trade stays traceable.

    The vestibular trigger is the drifting and the flare, so those stop. What
    must not stop is anything carrying information: the patina border, the
    seam, the parent crests, and above all `.from-dream`, which is the only
    thing on the row that says WHICH dream a trade came from. A treatment that
    cannot be traced back to a record is decoration pretending to be
    provenance.
    """
    block = _reduced_motion_block(dream_fx.CSS)
    assert ".wisped .wisps{display:none}" in block
    assert "animation:none" in block
    for informative in (".from-dream", ".fused .crest", ".fused::before"):
        assert informative not in block, (
            f"{informative} carries information and must survive "
            "prefers-reduced-motion"
        )
    # The `.fused` base rule itself must not be neutralised, only its motion.
    assert ".fused{" not in block


def test_reduced_motion_is_answered_by_not_running() -> None:
    """Switched off, not done invisibly.

    Same choice the projection layer makes about the starfield: the script
    checks the preference and returns rather than relying on a stylesheet to
    suppress work it went ahead and did.
    """
    assert "prefers-reduced-motion: reduce" in dream_fx.SCRIPT
    assert "if (still" in dream_fx.SCRIPT or "still ||" in dream_fx.SCRIPT


def test_a_first_visit_does_not_animate_the_whole_vault() -> None:
    """No marker is a third state, never a marker of zero.

    `seen.py` reports first visit apart from "nothing happened" precisely so a
    renderer cannot treat them the same. A caller with no marker passes
    `is_new=False`; a vault of dreams all animating at once on somebody's first
    ever load is a fireworks display, not a notification.
    """
    assert dream_fx.reveal_attrs(is_new=False, fusion_id=7) == ""
    assert "data-fuse-new" in dream_fx.reveal_attrs(is_new=True, fusion_id=7)


def test_the_fusion_id_is_escaped_into_the_attribute() -> None:
    """Safe by construction, not by the caller passing an integer today.

    `render._e` is the usual route and cannot be used: `render` imports this
    module, so reaching back would be a cycle.
    """
    attrs = dream_fx.reveal_attrs(is_new=True, fusion_id='4" onload="x')
    assert 'onload="x' not in attrs
    assert "&quot;" in attrs


def test_a_played_fusion_is_remembered_for_the_sitting_only() -> None:
    """`sessionStorage`, never `localStorage`.

    The server decides what is new from the seen marker and is the authority.
    This only stops a replay when the same tab re-renders the same card before
    the marker has moved — a live poll, a back-navigation. Remembering it for
    good would mean a fusion the operator has since forgotten never animates
    again, so the one view that mattered is the one that got lost.
    """
    code = _code(dream_fx.SCRIPT)
    assert "sessionStorage" in code
    assert "localStorage" not in code


def test_the_store_failing_replays_rather_than_swallows() -> None:
    """Private mode, a quota, a disabled store: play it again.

    The failure direction matters. Treating an unreadable store as "already
    played" would silently disable the feature for anyone browsing privately,
    and it would look like the animation was never built.
    """
    assert "catch (e) { return []; }" in dream_fx.SCRIPT


def test_nothing_is_written_as_markup() -> None:
    code = _code(dream_fx.SCRIPT)
    assert "innerHTML" not in code
    assert "insertAdjacentHTML" not in code


def test_loading_twice_is_a_no_op() -> None:
    assert "if (window.MUDHORN_DREAMFX) return;" in dream_fx.SCRIPT
