"""Wisps, orbs and the moment two dreams become one.

Two visual treatments the operator asked for by name, kept in their own module
for the reason `forge_window.py` is: `render.py` is six and a half thousand
lines and every addition to it is a merge conflict waiting for the next session.

**A trade that came from a dream is marked, and the marking is not a badge.**
*"Trades that are 'dreams' show differently — a wisp animation or something like
orbs floating around it."* A tag would say the same thing and say it the way
every other tag on the deck says everything; the point of this one is that the
provenance is felt rather than read.

**A fusion is a state, and the animation is a notification about it.** Two or
three complementary dreams tying together — *"like a cartoon where two creatures
would combine into a special creature"* — with the joining happening in the
backend whenever it happens, and the reveal waiting until somebody is actually
looking. Those are different clocks on purpose:

- The **join** must not wait on being watched. A dream vault that only fuses
  while a browser is open would make the feature a function of the operator's
  attention, which is the opposite of what a background dreamer is for.
- The **reveal** must not be spent on nobody. An animation that played at 3am
  played to an empty room, and the operator arrives to a fused card with no way
  to know it is new.

So the server decides whether this fusion is newer than the operator's seen
marker (`web/seen.py`, which advances to the PREVIOUS request rather than to
now, precisely so nothing is marked seen that was never on screen) and says so
in an attribute. The client plays it once and stops.

**The fused state is rendered in the markup, never by the animation.** This is
the fail-to-visible rule in the place it is easiest to get wrong: it is very
natural to draw two separate cards and have JavaScript merge them, and that
fails to *two cards and a lie*. The card is drawn fused; the animation is the
arrival of a thing that is already true. Script off, script threw, reduced
motion — all show a fused card, correctly, with no motion.
"""

from __future__ import annotations

from html import escape

__all__ = ["CSS", "SCRIPT", "reveal_attrs"]


def reveal_attrs(*, is_new: bool, fusion_id: object) -> str:
    """The attributes that decide whether the joining plays on this view.

    `is_new` is the caller's answer to "did this fusion happen after the moment
    the operator was last provably shown the state of the world" — which is
    `seen.py`'s question, not a timestamp comparison to invent here.

    **A first visit must not read as "everything is new".** `seen.py` reports
    that as its own third state rather than as a marker of zero, and a caller
    that has no marker should pass `is_new=False`: a vault of dreams all
    animating at once on somebody's first ever load is a fireworks display, not
    a notification. Same rule as every count on that module being `int | None`
    rather than `0` when there is nothing to compare against.
    """
    if not is_new:
        return ""
    # Escaped here rather than trusted to be an integer. It is one today, and a
    # helper that is safe only while its caller passes the right type is a
    # helper that stops being safe silently. `render._e` is the usual route and
    # is deliberately not imported: `render` imports this module, so reaching
    # back would be a cycle.
    return f' data-fuse-new="1" data-fuse-id="{escape(str(fusion_id), quote=True)}"'


# ---------------------------------------------------------------- the styling
#
# **Never put a backslash in here.** Ordinary Python string, so a CSS hex escape
# is read by Python as an octal one and the browser draws a tofu box beside the
# leftover digits. Literal characters only. `tests/test_web.py` fails the build
# on a control character reaching `render.STYLES`, which this concatenates into.
CSS = """
/* A trade that came from a dream. Motes, not a badge: the provenance should be
   felt rather than read, and there is already a tag vocabulary for reading. */
.wisped{position:relative}
.wisped .wisps{position:absolute;inset:-2px -6px;pointer-events:none;
  overflow:hidden;border-radius:3px}
.wisped .wisps b{position:absolute;width:4px;height:4px;border-radius:50%;
  background:var(--patina);opacity:0;
  box-shadow:0 0 7px rgba(78,140,125,.75)}
.wisped .wisps b:nth-child(1){left:4%;animation:wisp-drift 7.5s linear infinite}
.wisped .wisps b:nth-child(2){left:27%;width:3px;height:3px;
  animation:wisp-drift 9.5s linear infinite 1.4s}
.wisped .wisps b:nth-child(3){left:58%;animation:wisp-drift 8.2s linear infinite 3.1s}
.wisped .wisps b:nth-child(4){left:81%;width:3px;height:3px;
  animation:wisp-drift 11s linear infinite 4.7s}
@keyframes wisp-drift{
  0%{transform:translate3d(0,110%,0);opacity:0}
  14%{opacity:.85}
  76%{opacity:.5}
  100%{transform:translate3d(6px,-30%,0);opacity:0}}

/* The dream a trade came from, named in the row itself. The orbs say THAT it
   came from one; only this says which, and a treatment that cannot be traced
   back to a record is decoration pretending to be provenance. */
.wisped .from-dream{font-family:var(--mono);font-size:.5625rem;
  letter-spacing:.14em;text-transform:uppercase;color:var(--patina)}

/* Symbiosis. The card is drawn fused; this styles what fused looks like. */
.fused{position:relative;border-color:var(--patina)}
.fused .crests{display:flex;align-items:center;gap:.5rem;margin-bottom:.6rem;
  flex-wrap:wrap}
.fused .crest{font-family:var(--mono);font-size:.625rem;letter-spacing:.08em;
  color:var(--pewter);border:1px solid var(--slate);border-radius:2px;
  padding:.2rem .45rem;background:var(--ink);min-width:0;
  overflow-wrap:anywhere}
.fused .join{color:var(--patina);font-size:.875rem;line-height:1}
.fused .sigil{display:inline-flex;align-items:center;gap:.4rem;
  font-family:var(--mono);font-size:.5625rem;letter-spacing:.16em;
  text-transform:uppercase;color:var(--patina)}
.fused .sigil i{width:7px;height:7px;border-radius:50%;background:var(--patina);
  box-shadow:0 0 9px rgba(78,140,125,.6)}

/* The seam: a hairline that runs the width of a fused card, so the join is
   legible on a static page with no motion and no colour perception. */
.fused::before{content:"";position:absolute;left:0;right:0;top:-1px;height:1px;
  background:linear-gradient(90deg,transparent,var(--patina),transparent)}

/* The joining. It plays when the server said this fusion is newer than the
   operator's seen marker, once, and then the class is removed. Everything here
   animates something that is ALREADY on screen and correct -- nothing below
   reveals a value, which is what keeps it decoration. */
.fused.joining .crests{animation:fuse-draw .9s ease-out both}
.fused.joining .join{animation:fuse-spark 1.15s ease-out both}
.fused.joining::after{content:"";position:absolute;inset:0;border-radius:2px;
  pointer-events:none;animation:fuse-flare 1.15s ease-out both}
@keyframes fuse-draw{
  0%{transform:translate3d(0,0,0)}
  45%{transform:translate3d(0,-3px,0)}
  100%{transform:translate3d(0,0,0)}}
@keyframes fuse-spark{
  0%{transform:scale(.6) rotate(0deg);opacity:.4}
  55%{transform:scale(1.45) rotate(180deg);opacity:1}
  100%{transform:scale(1) rotate(360deg);opacity:1}}
@keyframes fuse-flare{
  0%{box-shadow:inset 0 0 0 1px rgba(78,140,125,0);background:transparent}
  40%{box-shadow:inset 0 0 0 1px rgba(78,140,125,.55);
    background:radial-gradient(circle at 50% 50%,rgba(78,140,125,.16),transparent 62%)}
  100%{box-shadow:inset 0 0 0 1px rgba(78,140,125,0);background:transparent}}

/* Reduced motion takes the MOTION and never the meaning. A fused card is still
   fused, still bordered in patina, still carries its seam and its parents --
   the vestibular trigger is the drifting and the flare, so those are what stop.
   Switched off rather than slowed, so the work is not done invisibly. */
@media (prefers-reduced-motion:reduce){
  .wisped .wisps{display:none}
  .fused.joining .crests,.fused.joining .join{animation:none}
  .fused.joining::after{animation:none;content:none}
}
"""


# ---------------------------------------------------------------- the client
#
# Small on purpose. The server has already decided everything; this plays an
# animation and forgets about it.
SCRIPT = """
<script>
(function () {
  if (window.MUDHORN_DREAMFX) return;
  window.MUDHORN_DREAMFX = true;

  /* Reduced motion is answered by not running, rather than by running into a
     stylesheet that happens to suppress it. The work is not done, which is the
     same choice the projection layer makes about the starfield. */
  var still = window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* Played fusions are remembered for the SITTING, not forever. The server
     decides what is new from the seen marker and is the authority; this only
     stops a replay when the same tab re-renders the same card -- a live poll,
     a back-navigation -- before the marker has moved. sessionStorage rather
     than localStorage because a fusion the operator saw last week and has
     forgotten is worth showing again, and remembering it for good would mean
     the one view that mattered was the one that got lost. */
  var KEY = 'mudhorn.fused';

  function played() {
    try { return JSON.parse(window.sessionStorage.getItem(KEY) || '[]'); }
    catch (e) { return []; }
  }

  function remember(id) {
    try {
      var seen = played();
      if (seen.indexOf(id) < 0) seen.push(id);
      window.sessionStorage.setItem(KEY, JSON.stringify(seen.slice(-200)));
    } catch (e) { /* private mode, quota, a disabled store: play it again. */ }
  }

  function run() {
    var seen = played();
    document.querySelectorAll('[data-fuse-new]').forEach(function (card) {
      var id = card.getAttribute('data-fuse-id') || '';
      card.removeAttribute('data-fuse-new');
      if (still || seen.indexOf(id) >= 0) return;
      remember(id);
      card.classList.add('joining');
      window.setTimeout(function () { card.classList.remove('joining'); }, 1400);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
    run();
  }
})();
</script>
"""
