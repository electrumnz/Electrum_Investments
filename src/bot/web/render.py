"""HTML rendering for the operator command centre.

Plain Python string building rather than a template engine. For a single-user
tool, a template dependency and its packaging problems buy nothing, and keeping
the markup next to the data that shapes it makes the whole thing easier to
follow.

Styling is the Mudhorn Capital identity: cold, mineral, one accent used
sparingly, every figure tabular. A trading surface is scanned rather than read,
so state is encoded in shape as well as in number and anything needing
attention reads before the figures do.

## This is the real one

`brand/` is the public site and every number on it is invented. Everything here
is live: the journal, the broker, the audit log. That is the whole reason this
binds to `127.0.0.1` and is reached over Tailscale rather than published.

Read-only apart from `POST /chat`. Nothing rendered here can place, close or
resize a position, and the Settings page shows the risk limits without offering
a way to change them: limits move in a commit, by a person, with a reason.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..audit import AuditView, DecisionEntry
from ..broker import is_crypto_symbol
from ..config import DAY_NAMES, Env, InstrumentRules, Rules, WatchlistRules
from ..data.xfeed import DEFAULT_CACHE_TTL_SECONDS as XFEED_CACHE_TTL_SECONDS
from ..data.xfeed import FeedState
from ..dreamer import estimated_cost_usd, read_schedule
from ..dreaming import (
    FUSION,
    THIN_LEDGER_THRESHOLD,
    Adoption,
    ConditionState,
    ConferenceDecision,
    ConferenceVerdict,
    Dream,
    DreamCondition,
    DreamLedger,
    DreamMessage,
    DreamSummary,
    DreamVerdict,
    Hop,
    NoDecision,
    Vault,
    pending_observations,
    promotion_for,
)
from ..exit_review import ExitReport, ExitReview
from ..jobs import Coverage, JobAnswer, JobHistory, Outcome
from ..market_clock import (
    NY,
    ClockFace,
    MarketPhase,
    MarketState,
    VenueState,
    clock_faces,
    is_continuous,
    venue_state,
)
from ..metrics import JournalReport, render_excursions, render_summary
from ..models import (
    AccountSnapshot,
    Direction,
    ExitReason,
    Position,
    StandDownState,
    Trade,
    WorkingOrder,
)
from ..news_history import NewsItem, NewsRecall, Sightings, sightings
from ..options import ExpiryAlert
from ..position_actions import UnexplainedMoveReport
from ..session_calendar import SessionCalendar
from ..settings_agent import (
    ChangeRequest,
    LimitFact,
    Unit,
    effective_value,
    format_value,
)
from ..stop_width import measure_stop_widths
from ..tailnet import TailnetStatus

# At module scope for consistency with everything else here rather than for
# FastAPI's sake — the annotation rule that forces `Request` to the top of
# `app.py` does not apply to a plain string constant. Two constants, kept in
# their own file because the confirmation window is a self-contained thing with
# its own reasoning, and this module is long enough.
from .dream_fx import CSS as DREAM_FX_CSS
from .dream_fx import SCRIPT as DREAM_FX_SCRIPT
from .dream_fx import reveal_attrs
from .forge_window import CSS as FORGE_WINDOW_CSS
from .forge_window import SCRIPT as FORGE_WINDOW_SCRIPT
from .live import SessionDayView, TickerQuote
from .seen import Marker, SinceLastVisit

#: The two banners the live stream is allowed to take away, by id.
#:
#: Both are statements about a broker reading the SERVER had when it built the
#: page, and the stream exists to replace that reading — so both stop being true
#: the moment a fresh one arrives, and neither could clear itself. A server-
#: rendered warning that outlives its cause is the same failure as a timestamp
#: that outlives its reading, and it teaches an operator to read past the next
#: one.
#:
#: They are named here and repeated as literals in `SCRIPT`, because `SCRIPT` is
#: a plain string and interpolating into it is how the `{field: "close"}` trap
#: got into `SYSTEM_PROMPT_TEMPLATE`. `tests/test_web.py` fails the build if the
#: two copies drift apart.
#:
#: Nothing else may be removed this way. The client may UPDATE a figure the
#: server already rendered and may retract a claim the server made about its own
#: freshness; it must never be what reveals a figure.
STALE_BANNER_ID = "reading-stale"
COLD_START_BANNER_ID = "cold-start"

#: A hue per kind, applied to the SYMBOL LABEL ONLY.
#:
#: Direction already owns green and red on the price, the move and the rail, and
#: a second colour axis competing for the same glance would make both harder to
#: read. So the kind colours the name and nothing else — enough to see at a
#: glance that a strip holds metals and bonds and crypto rather than sixteen
#: large caps, without ever being mistaken for a gain or a loss.
#:
#: Deliberately none of them are the gain or loss colours.
KIND_HUES: dict[str, str] = {
    "index": "var(--holo)",
    "equity": "#9AA6B8",
    "crypto": "#C9922F",
    "defensive": "#7FA88C",
    "commodity": "#C4A24A",
    "rates": "#8E9BC4",
    "volatility": "#B8737F",
    "international": "#6FA9A2",
    "energy": "#B08050",
    "unclassified": "var(--pewter)",
}


def _kind_css() -> str:
    """One rule per kind. Generated so the palette above is the only source."""
    return "".join(
        f'.tape .cell[data-kind="{kind}"] .sym{{color:{hue}}}'
        for kind, hue in KIND_HUES.items()
    )


STYLES = """
:root {
  /* Tell the browser this is a dark document, before anything else in here.
     Nothing else can: a stylesheet colouring `body` says nothing about the
     chrome the platform draws ITSELF. Without it the chat `<textarea>`, the
     sign-in password field, every scrollbar (`.scroll` is a horizontal
     scroller on four pages), `::selection` and the paint that happens BEFORE
     this stylesheet applies all come from the light palette — so a phone on a
     slow tailnet flashes white, and a white input sits inside a graphite
     panel. One declaration, and it is the only one that reaches any of them. */
  color-scheme:dark;
  /* `--pewter` is the recessive text colour and it is used at 10px — `.stat
     span.k`, `th`, `.eyebrow`, `.tape .clk .city` are all .625rem uppercase
     mono. #7D8896 measured 4.81:1 on graphite, which clears WCAG 2's 4.5:1
     and clears it on a formula known to OVERSTATE contrast on a dark ground:
     WCAG 2 does not model reverse polarity, and APCA scores a 10px weight-400
     face here well under its body-text floor. Lifted to 5.76:1, which is one
     token edit rather than an audit of the ~40 rules that read it. Still
     recessive against `--bone` at 14.59:1, which is the job it does. */
  --ink:#0B0E12; --graphite:#161B22; --slate:#29313C; --pewter:#8B96A4;
  --bone:#E9ECEF; --patina:#4E8C7D;
  /* Severity, for banners. Two rusts, because one colour was doing two jobs
     with only one of them measured. `--rust` is a 3px left border — a non-text
     boundary, which needs 3:1, and it makes 3.48:1 on graphite. The same token
     also coloured `.banner.crit b`, which is 11px uppercase mono at .14em
     tracking, needs 4.5:1, and got 3.48:1: the most severe state on the deck
     had the least readable heading, which is a warning that did not happen.
     `--rust-text` is the same hue lifted to 5.50:1 on graphite (6.14:1 on
     ink). Borders keep `--rust`; label text takes `--rust-text`. */
  --amber:#C08A3E; --rust:#B3524A; --rust-text:#CF7A70;
  /* Figures only, lifted for contrast on graphite. Same pair as the public
     site: patina brightened for a gain, a cold rose for a loss, because a warm
     red on this ground reads as brick. */
  --gain:#5FA795; --loss:#C0707B;
  /* The projection layer, and nothing else. Every effect below — starfield,
     deck grid, bracket corners, the hyperspace streaks — is drawn in this and
     no data ever is. The identity has ONE accent on purpose, so patina keeps
     chrome, links and focus rings; holo is the medium the interface is
     projected in, which is why it may be everywhere at once without competing
     with an accent that has to mean something. If a figure ever picks this up,
     the rule has been broken. */
  --holo:#6FD3E8;
  --holo-faint:rgba(111,211,232,.14);
  --holo-line:rgba(111,211,232,.30);
  --serif: ui-serif,"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  --sans: ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
  --mono: ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --ease: cubic-bezier(.22,1,.36,1);
  --ease-jump: cubic-bezier(.7,0,.84,0);
}
*,*::before,*::after{box-sizing:border-box}
/* iOS Safari inflates text in a rotated viewport unless this is pinned, and the
   thing it inflates hardest is a wide table — which is every table on this deck,
   sitting in a `.scroll` with columns that are aligned on purpose. The setting
   is not zoom: page zoom still works and still should. */
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{margin:0;background:var(--ink);color:var(--bone);font-family:var(--sans);
  font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased}
:focus-visible{outline:2px solid var(--patina);outline-offset:3px}
/* An inline link used to be `--bone` — the body colour — underlined in
   `--slate`, which is 1.47:1 against ink. Body-coloured text with an invisible
   underline is not distinguishable as a link by ANY channel, which is WCAG
   1.4.1 failing in the direction where there is no colour difference either.
   The underline is the channel here, so it has to be visible: `--pewter` is
   6.44:1 on ink, and the offset keeps it clear of the descenders at 15px.
   Hover moves it to the one accent this identity has. */
a{color:var(--bone);text-decoration-color:var(--pewter);text-underline-offset:4px}
a:hover{text-decoration-color:var(--patina)}
.wrap{width:min(100% - 2rem,1240px);margin-inline:auto}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.pos,.gain{color:var(--gain)} .neg,.loss{color:var(--loss)} .muted{color:var(--pewter)}
/* A figure that could not be read, as opposed to one that is absent. `.muted`
   is for "there is nothing here and that is fine"; this is for "this should
   have a value and does not", which is a different claim and must not be
   whispered. Not the loss colour — unreadable is not losing. */
.alert{color:var(--amber)}
.note{font-size:.8125rem;color:var(--pewter)}
/* A path or a key with no spaces in it will not wrap, and one long enough to
   pass the viewport pushes the whole PAGE sideways -- 404px of content in a
   390px phone, which is the one thing the layout rules say must never happen.
   Measured on Settings, where the forge prints an absolute path. `anywhere`
   rather than `break-word` because only the former is allowed to break at a
   point that leaves the previous line short, which is exactly the case here. */
code{overflow-wrap:anywhere}
h1,h2,h3{font-family:var(--serif);font-weight:400;letter-spacing:-.01em;margin:0}
h1{font-size:1.75rem} h2{font-size:1.375rem;margin-bottom:.25rem} h3{font-size:1rem}
.eyebrow{font-family:var(--mono);font-size:.6875rem;letter-spacing:.18em;
  text-transform:uppercase;color:var(--pewter);margin:0}

/* The insets need `viewport-fit=cover` on the viewport meta to be anything but
   `0px`, and both `<head>` blocks carry it. On a desktop, on Android and on a
   phone with no notch every one of these resolves to zero, so this is inert
   until the hardware makes it matter — and when it does, the nav's last link
   is otherwise under the rounded corner in landscape and the footer under the
   home indicator. Every `env()` carries an explicit `0px` fallback so an engine
   that does not know the keyword drops the whole declaration to the same
   padding it had before rather than to nothing. */
header.bar{border-bottom:1px solid var(--slate);position:sticky;top:0;
  background:rgba(11,14,18,.94);backdrop-filter:blur(8px);z-index:20;
  padding-inline:env(safe-area-inset-left,0px) env(safe-area-inset-right,0px)}
header.bar .wrap{display:flex;align-items:center;gap:.875rem;
  padding:.75rem 0;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:.6rem;font-family:var(--serif);
  font-size:1rem;letter-spacing:.04em;text-decoration:none;white-space:nowrap}
.brand svg path{fill:var(--bone)}
.brand .thin{color:var(--pewter)}
nav{margin-left:auto;display:flex;gap:.2rem;flex-wrap:wrap}
nav a{font-family:var(--mono);font-size:.6875rem;letter-spacing:.14em;
  text-transform:uppercase;color:var(--pewter);text-decoration:none;
  padding:.4rem .7rem;border:1px solid transparent;border-radius:2px;
  transition:color .2s,border-color .2s,background .2s}
nav a:hover{color:var(--bone);border-color:var(--slate);background:var(--graphite)}
nav a[aria-current=page]{color:var(--bone);border-color:var(--patina)}
.live{font-family:var(--mono);font-size:.625rem;letter-spacing:.12em;
  text-transform:uppercase;color:var(--pewter);display:flex;align-items:center;
  gap:.4rem;padding-left:.875rem;border-left:1px solid var(--slate)}
.live i{width:6px;height:6px;border-radius:50%;background:var(--patina);
  display:inline-block}
.live.paper i{background:var(--patina)}

main{padding:2rem 0 calc(4rem + env(safe-area-inset-bottom,0px));
  padding-inline:env(safe-area-inset-left,0px) env(safe-area-inset-right,0px)}
.page-head{display:flex;align-items:baseline;gap:1rem;flex-wrap:wrap;
  margin-bottom:1.5rem}
.page-head .asof{margin-left:auto;font-family:var(--mono);font-size:.75rem;
  color:var(--pewter)}
section.block{margin-top:2.5rem}
section.block>h2{margin-bottom:.75rem}

/* ============================================================ ticker tape ==
   One strip under the header: the session phase pinned at the left, then a
   scrolling run of one clock per four instruments.

   ## Height, and why it is fixed

   The header measures 57px. This is 3.15rem, about 50px — a tenth shorter,
   which reads as subordinate to the header rather than competing with it.
   Measured in a browser rather than derived from the padding, because the
   mark, the nav and the mode badge all contribute and the tallest wins.
   The height is FIXED rather than
   content-derived because the track inside is absolutely positioned while it
   scrolls: a strip that grew with its contents would shove the page down a
   few pixels on every repaint.

   It sits in normal flow directly after the header, NOT sticky. Stacking a
   second sticky element under a sticky header means hard-coding the header's
   height as an offset, and that height changes the moment the nav wraps on a
   narrow screen — at which point the tape either overlaps the nav or floats
   below it with a gap. Normal flow cannot get that wrong.

   ## Not the command console

   Shares the deck's HUD vocabulary and shares no CSS with `.fx-console`, which
   is a modal overlay at z-index 70. This is a strip in the document at z-index
   0. They never appear in the same place and neither can style the other. */
/* z-index 19: under the sticky header (20) so it tucks beneath on scroll,
   and ABOVE the projection layer's grid, vignette and scanline planes,
   which are fixed full-screen at z 1-3. Without it the strip renders at
   `auto` — below all three — and the cells come out dimmed to near
   invisibility while the header beside them, at z 20, looks fine. Nothing
   warns: the elements are present, opaque and hit-testable, and
   `elementFromPoint` returns them. It is only visible in a screenshot. */
/* The tape is a BAND, and it has to read as one. It used to be `--ink` — the
   same colour as the page body and as good as the same as the header — so it
   dissolved into the dark field between them and the clocks dissolved into it.
   Three surfaces at the same value is one surface.
   Lifted to graphite with a rule top and bottom, so the order down the page is
   header / band / body rather than one continuous dark. */
.tape{height:3.15rem;border-top:1px solid var(--slate);
  border-bottom:1px solid var(--slate);background:var(--graphite);
  box-shadow:inset 0 1px 0 rgba(233,236,239,.04),0 1px 0 rgba(0,0,0,.5);
  display:flex;align-items:stretch;overflow:hidden;position:relative;z-index:19}
/* Two pinned facts, STACKED rather than strung along the row: when NYSE next
   changes state, and what the prices scrolling past it are plus when they were
   read. Stacking costs the strip the width of the wider line instead of the sum
   of the two, which is the only way a second pinned fact fits at 320px — and
   the band is a fixed 3.15rem, so two lines of micro-type have the room.
   `.ln` keeps the countdown and the "not ticking" marker on one row, because
   that marker qualifies the countdown and nothing else. */
.tape .fixed{display:flex;flex-direction:column;align-items:flex-start;
  justify-content:center;gap:.15rem;padding:0 .875rem;
  white-space:nowrap;border-right:1px solid var(--slate);background:var(--ink);
  font-family:var(--mono);font-size:.625rem;letter-spacing:.12em;
  text-transform:uppercase;color:var(--pewter);position:relative;z-index:1}
.tape .fixed .ln{display:flex;align-items:center;gap:.45rem}
.tape .fixed .until{color:var(--bone)}
/* Tabular figures, so the minute rolling over cannot change the width of the
   pinned block and nudge the whole strip sideways once a minute.
   It inherits pewter and does not take `--bone` back off the countdown: this is
   a qualifier on the prices scrolling past, not a thing to act on.
   Scoped under `.tape .fixed` so a later bare `.rd` rule cannot restyle it by
   winning a tie at equal specificity — the collision that has bitten
   `.pill.seed`, `.rung.gate` and `.note.alert`. */
.tape .fixed .rd{font-variant-numeric:tabular-nums}
.tape .view{flex:1;overflow:hidden;position:relative}
/* The view carries `tabindex="0"` because under `prefers-reduced-motion` it
   becomes a horizontal scroller with no focusable descendant, and Safari and
   Firefox give such a container no keyboard route of their own — the strip
   would be readable with a mouse and unreachable without one, in exactly the
   mode chosen by somebody who asked for less movement.
   The offset is NEGATIVE here alone. `:focus-visible` draws at +3px everywhere
   else, and `.tape` is `overflow:hidden`, so an outside ring on this element
   would be clipped away to nothing on three sides. Inset, it lands on the
   strip. */
.tape .view:focus-visible{outline:2px solid var(--patina);outline-offset:-3px}
.tape .track{display:flex;align-items:center;gap:0;height:100%;width:max-content;
  animation:tape-run 90s linear infinite;will-change:transform}
/* Each copy of the run is its own flex row inside the track, so the translation
   still lands the second copy exactly where the first began (-50% of two equal
   children) while the second copy remains a single addressable element. It has
   to be `flex:none` or the two would shrink to the viewport and the strip would
   compress rather than scroll.
   Named `marquee-run` rather than `run`, because `.fx-sweep.run` already uses
   that word as a STATE — and a bare `.run` layout rule would restyle the sweep
   element as a side effect. Same shape as the `.pill.seed` collision, and
   `test_no_stylesheet_rule_collides_with_a_state_badge` catches it. */
.tape .track > .marquee-run{display:flex;align-items:center;height:100%;
  flex:none}
@keyframes tape-run{from{transform:translate3d(0,0,0)}
  to{transform:translate3d(-50%,0,0)}}
/* Hovering is the gesture for "let me read that one". */
.tape:hover .track{animation-play-state:paused}

/* ONE vertical mark per cell, and it is the one that means something.
   There used to be two: this rail on the left, coloured by direction and scaled
   by `--mag`, and a flat grey `border-right` on the other edge meaning nothing
   at all. Adjacent cells therefore drew a grey line and a coloured one a few
   pixels apart, and a reader had no way to tell which of the two carried
   information. The border is gone and the padding does its job — a gap divides
   a list perfectly well, and it cannot be mistaken for a gauge. */
.tape .cell{display:flex;align-items:baseline;gap:.4rem;padding:0 1.1rem;
  white-space:nowrap;font-family:var(--mono);font-size:.75rem;height:100%;
  align-items:center;position:relative}
/* The power rail: a HUD gauge rather than an arrow. Colour carries direction,
   and `--mag` (0..1, set per cell by the server) carries SIZE — so a 0.1% drift
   and a 3% move do not look identical, which a bare triangle cannot express. */
.tape .cell::before{content:"";position:absolute;left:0;top:50%;
  transform:translateY(-50%);width:2px;height:calc(28% + var(--mag,0) * 52%);
  background:var(--pewter);opacity:.5;border-radius:1px}
/* No reading, no gauge. A cell with no quote — or a price with no prior close —
   has `--mag:0`, and the rail collapsed to a 28% pewter stub that read as a
   stray tick rather than as an absent measurement. A gauge showing its minimum
   is a claim that the move is small; there is no move to be small.
   Safe against the two-class trap that has bitten three times, because there is
   no tie to lose: nothing else on this pseudo-element declares `display`, so
   source order cannot decide this one. */
.tape .cell.norail::before{display:none}
.tape .cell.up::before{background:var(--gain);opacity:calc(.45 + var(--mag,0) * .55);
  box-shadow:0 0 calc(var(--mag,0) * 7px) var(--gain)}
.tape .cell.down::before{background:var(--loss);opacity:calc(.45 + var(--mag,0) * .55);
  box-shadow:0 0 calc(var(--mag,0) * 7px) var(--loss)}
.tape .cell .sym{color:var(--pewter);letter-spacing:.08em}
/* Tradeable names read brighter than watch-only ones. `watchlist.symbols` is a
   view and `allowed_symbols` is a permission, and the tape is the one place
   they sit side by side — so the difference is visible rather than implied. */
.tape .cell.can .sym{color:var(--bone)}
.tape .cell .px{color:var(--bone);font-variant-numeric:tabular-nums}
.tape .cell .mv{font-variant-numeric:tabular-nums;color:var(--pewter)}
.tape .cell.up .mv{color:var(--gain)}
.tape .cell.down .mv{color:var(--loss)}
.tape .cell .none{color:var(--pewter);font-style:italic;font-size:.6875rem}

/* Market shut for this instrument class: the figure is last session's close,
   not a live price. Dimmed rather than hidden — the move is still true of the
   day that traded, and withholding a correct figure teaches an operator the
   tape is unreliable, where showing it at full strength teaches them a Sunday
   price is current.

   AFTER the up/down rules so it wins on source order at equal specificity, and
   it must stay there: the live painter re-adds `up`/`down` on every frame and
   only ever removes those two, so `shut` survives a repaint and has to keep
   overriding what the repaint puts back.

   Not colour alone. `.shut` also strikes the rail back to neutral and the cell
   carries `title`, because a dimmed green and a dimmed red are exactly the
   pair that about one man in twelve cannot separate. */
/* Out of hours: a REAL price you cannot act on at that price.
   Deliberately not a dimmer version of `.shut`, because the two make opposite
   claims about the figure. `.shut` says the number is yesterday's; this says
   the number is now and the ORDER is the part that waits. So the price stays
   at full strength and the marker goes on the rail and the symbol: a hollow
   rail rather than a lit one, and a hairline under the name. */
.tape .cell.v-ooh::before{opacity:.28;box-shadow:none}
.tape .cell.v-ooh .sym{border-bottom:1px dashed currentColor;padding-bottom:1px}
.tape .cell.v-ooh .mv{opacity:.8}
.tape .cell.shut .sym,.tape .cell.shut .px,.tape .cell.shut .mv{
  color:var(--pewter);opacity:.65}
.tape .cell.shut::before{background:var(--slate);opacity:.5;box-shadow:none}
.tape .cell.shut::after{display:none}
.tape .cell.shut .sym{border-bottom:none}

/* A clock is a different KIND of object from a price, and it has to look like
   one. It used to be framed in `border-left`/`border-right: var(--slate)` —
   which was the exact line the cells used to divide themselves with, so the
   frame said "here is another cell" and only the fill said otherwise. Contrast
   alone could never fix that while the frame was shared.
   So it is not framed at all now. It is a module set INTO the band: inset
   from the strip on all four sides, recessed, rounded, and carrying the deck's
   own bracket corners. Nothing else on the tape has any of those, and the cells
   have no borders left to be confused with. */
.tape .clk{display:flex;align-items:center;gap:.5rem;padding:0 .8rem;
  white-space:nowrap;font-family:var(--mono);font-size:.75rem;
  height:calc(100% - .7rem);margin:.35rem .6rem;border-radius:2px;
  position:relative;
  background:linear-gradient(180deg,rgba(5,8,11,.95),rgba(11,14,18,.95));
  box-shadow:inset 0 1px 4px rgba(0,0,0,.6),0 0 0 1px rgba(42,52,65,.4)}
/* The diagonal pair, same as `.card` and every other framed box on the deck —
   half the pseudo-elements of four corners and it still reads as a frame. */
.tape .clk::before,.tape .clk::after{content:"";position:absolute;
  width:6px;height:6px;border:1px solid var(--holo);opacity:.5;
  pointer-events:none}
.tape .clk::before{top:0;left:0;border-right:0;border-bottom:0}
.tape .clk::after{bottom:0;right:0;border-left:0;border-top:0}
.tape .clk .city{font-size:.625rem;letter-spacing:.1em;text-transform:uppercase;
  color:var(--bone);opacity:.7;line-height:1}
/* Larger than the instrument prices, deliberately. The hour in four zones
   is what an operator wants at a glance; a price they will read off the Board.
   The city abbreviates to three letters to buy the time that width. */
/* Negative tracking on the digits. A monospace face at this size sets very
   loose, so the width the larger type costs is bought straight back from the
   gaps between numerals rather than from the strip. */
.tape .clk .t{color:var(--bone);font-variant-numeric:tabular-nums;
  font-size:1rem;letter-spacing:-.045em}
/* City over exchange, so a clock is two short lines rather than one long one.
   Inline, each clock ran as wide as three instrument cells. */
.tape .clk .who{display:flex;flex-direction:column;align-items:flex-start;
  gap:.1rem;line-height:1.15}
.tape .clk .mkt{font-size:.5rem;letter-spacing:.08em}
/* ONE visual channel per meaning. Every clock panel is identical now, and the
   only thing that varies is the exchange label's colour — which is the fact
   worth reading. New York used to get a lighter panel marking it as "the"
   market clock; that stopped meaning anything the moment each clock carried
   its own exchange, and a difference that looks deliberate while signifying
   nothing is worse than no difference at all.
   Not the gain/loss pair: this is a state, not a direction, and borrowing the
   P&L colours would make a shut market read as a losing one. */
/* Colour, glow, a SHAPE and a word — four channels, and the last two are the
   ones that survive not being able to read colour at all.
   The glow was described here as the second channel, and it is not one: it is
   luminance around a hue, so greyscale, a low-contrast screen and a screen
   reader all get exactly the same nothing out of it as they get out of the
   colour. Measured on the live deck with NYSE genuinely open — `mkt-live`
   against three `mkt-closed`, no text, no glyph and no tooltip anywhere on the
   badge — while the instrument cells one element across already carried a
   `title`.
   So: the pip is FILLED for a live session, half-filled out of hours and an
   empty ring when the market is shut, which is a difference in ink rather than
   in colour; and the state is spelt in micro-type beside it. A zone with no
   exchange gets neither, because it is making no claim to mark.
   Not the gain/loss pair: this is a state, not a direction, and borrowing the
   P&L colours would make a shut exchange read as a losing one. */
.tape .clk .mkt{display:inline-flex;align-items:center;gap:.28rem;
  font-size:.625rem;letter-spacing:.1em;font-weight:600}
.tape .clk .mkt .pip{width:6px;height:6px;border-radius:50%;flex:none;
  border:1px solid currentColor;background:transparent}
.tape .clk .mkt .st{font-size:.5rem;letter-spacing:.06em;font-weight:400;
  opacity:.85}
.tape .clk .mkt-live .pip{background:currentColor}
/* Half filled: current from the bottom up, so "partly" is legible as ink
   rather than as a tint. */
.tape .clk .mkt-ooh .pip{
  background:linear-gradient(to top,currentColor 50%,transparent 50%)}
.tape .clk .mkt-live{color:var(--patina);
  text-shadow:0 0 8px rgba(78,140,125,.75),0 0 16px rgba(78,140,125,.35)}
.tape .clk .mkt-ooh{color:var(--amber);
  text-shadow:0 0 7px rgba(192,138,62,.55)}
.tape .clk .mkt-closed{color:var(--pewter);opacity:.55;text-shadow:none}
/* No exchange in this zone. Dimmer than shut, because "there is nothing here"
   is a weaker statement than "this is closed right now". */
.tape .clk .mkt-bare{color:var(--slate);text-shadow:none}
/* Present only while SCRIPT has not run. A frozen clock is the one plausible
   wrong figure a clock can be, so it says so rather than looking correct. */
.tape .fixed .frozen{color:var(--amber);text-transform:none;letter-spacing:0}

/* The tick pulse: a charge running through the cell in the direction the price
   moved. Up rises, down falls — the motion carries the sign, so the meaning
   survives for anyone who cannot separate the two colours, which about one man
   in twelve cannot.

   Painted on `::after` so it cannot disturb layout: absolutely positioned,
   `pointer-events:none`, and gone when the animation ends. A strip that
   reflowed on every tick would judder the whole run. */
.tape .cell::after{content:"";position:absolute;inset:0;pointer-events:none;
  opacity:0}
@keyframes tape-charge-up{
  0%{opacity:0;background:linear-gradient(0deg,rgba(126,201,166,.55),transparent 42%)}
  22%{opacity:1}
  100%{opacity:0;background:linear-gradient(0deg,rgba(126,201,166,.55),transparent 100%)}}
@keyframes tape-charge-down{
  0%{opacity:0;background:linear-gradient(180deg,rgba(192,112,123,.55),transparent 42%)}
  22%{opacity:1}
  100%{opacity:0;background:linear-gradient(180deg,rgba(192,112,123,.55),transparent 100%)}}
.tape .cell.pulse-up::after{animation:tape-charge-up 900ms ease-out}
.tape .cell.pulse-down::after{animation:tape-charge-down 900ms ease-out}
/* The rail flares with it, so the gauge and the pulse read as one event. */
@keyframes tape-rail-up{0%{box-shadow:0 0 10px var(--gain)}100%{box-shadow:none}}
@keyframes tape-rail-down{0%{box-shadow:0 0 10px var(--loss)}100%{box-shadow:none}}
.tape .cell.pulse-up::before{animation:tape-rail-up 900ms ease-out}
.tape .cell.pulse-down::before{animation:tape-rail-down 900ms ease-out}

/* The session boundary: digits spin up to a blur and settle into the new
   state. The one moment this strip is worth watching. */
@keyframes tape-spin{0%{filter:blur(0);opacity:1}
  35%{filter:blur(3px);opacity:.75}
  70%{filter:blur(5px);opacity:.55}
  100%{filter:blur(0);opacity:1}}
.tape .clk.turning .t{animation:tape-spin 1500ms ease-in-out}
/* Directional, so the transition says WHICH way the session went rather than
   only that it went. Up into the regular session, down into a shut market,
   sideways into pre-market, after hours or overnight — the same three states
   the cells carry, so the whole strip tells one story. The vertical blur is
   what reads as digits spinning past on a mechanical counter. */
@keyframes tape-spin-up{
  0%{filter:blur(0);opacity:1;transform:translateY(0)}
  35%{filter:blur(0 3px);opacity:.7;transform:translateY(-5px)}
  70%{filter:blur(0 5px);opacity:.5;transform:translateY(-9px)}
  71%{transform:translateY(9px)}
  100%{filter:blur(0);opacity:1;transform:translateY(0)}}
@keyframes tape-spin-down{
  0%{filter:blur(0);opacity:1;transform:translateY(0)}
  35%{filter:blur(0 3px);opacity:.7;transform:translateY(5px)}
  70%{filter:blur(0 5px);opacity:.5;transform:translateY(9px)}
  71%{transform:translateY(-9px)}
  100%{filter:blur(0);opacity:1;transform:translateY(0)}}
@keyframes tape-spin-side{
  0%{filter:blur(0);opacity:1;transform:translateX(0)}
  35%{filter:blur(3px 0);opacity:.7;transform:translateX(-6px)}
  70%{filter:blur(5px 0);opacity:.5;transform:translateX(6px)}
  100%{filter:blur(0);opacity:1;transform:translateX(0)}}
.tape .clk.turn-up .t{animation:tape-spin-up 1500ms ease-in-out}
.tape .clk.turn-down .t{animation:tape-spin-down 1500ms ease-in-out}
.tape .clk.turn-side .t{animation:tape-spin-side 1500ms ease-in-out}
/* The directional rules come after the plain one and are more specific, so a
   clock carrying both `turning` and `turn-up` runs the directional animation.
   `turning` alone stays the fallback for a direction the script did not set. */
@keyframes tape-flash{0%{background:rgba(111,211,232,.20)}100%{background:transparent}}
.tape.turning{animation:tape-flash 1800ms ease-out}
/* There was a phase-coloured, pulsing dot here, and eleven rules driving it.
   It went with the verdict badge and for the same reason: an unlabelled
   coloured light is a claim nobody can read, and this one sat immediately
   beside a sentence about the NEXT phase while being coloured by the CURRENT
   one — so amber against "NYSE open in 3h 55m" invited exactly the wrong
   reading. The state is on the clock, where it has a filled/half/hollow pip
   and the word beside it, which survives greyscale and a screen reader.
   `.fixed .name` went with it: nothing has rendered that class since the phase
   block was removed, and a rule with no element is a rule nobody can check. */

@media (prefers-reduced-motion:reduce){
  /* Switched OFF, not slowed. A strip of text sliding sideways forever is a
     worked example of what the preference is asking to be spared, so the
     track stops and the strip becomes an ordinary horizontal scroller — the
     content stays reachable rather than being withdrawn. */
  .tape .track{animation:none;width:auto}
  .tape .view{overflow-x:auto}
  /* The second copy exists to make a translation loop seamless. Nothing is
     translating here, so it is sixteen instruments and four clocks printed
     again at the end of a strip somebody is scrolling by hand. */
  .tape .track > .marquee-run.dup{display:none}
  .tape .clk.turning .t,.tape.turning,
  .tape .clk.turn-up .t,.tape .clk.turn-down .t,
  .tape .clk.turn-side .t{animation:none}
  /* The pulse goes too, and the colour and the wedge carry the tick instead —
     both of which are already there and neither of which moves. */
  .tape .cell.pulse-up::after,.tape .cell.pulse-down::after,
  .tape .cell.pulse-up::before,.tape .cell.pulse-down::before{animation:none}
}
@media (max-width:640px){
  /* Tracking is what this panel actually costs at 320px. `.12em` on nineteen
     uppercase characters is most of a 157px block, against a strip 320px wide —
     so the letter-spacing goes and the padding tightens, and the words stay.
     Shortening the text instead would mean dropping "NYSE", and the exchange
     name is the whole reason this is allowed to be pinned at all. */
  .tape .fixed{font-size:.5625rem;padding:0 .55rem;letter-spacing:.04em;
    gap:.05rem}
  /* The horizontal gap belongs to the countdown's own row now. Left on
     `.fixed` it would only push the two stacked lines apart. */
  .tape .fixed .ln{gap:.3rem}
  /* The clock module keeps its inset but gives back the horizontal margin.
     At 390px the strip is a hand-scroller and a 19px gap either side of four
     clocks is a fifth of the visible width spent on air. */
  .tape .clk{margin-left:.3rem;margin-right:.3rem;padding:0 .55rem}
  .tape .cell{padding:0 .8rem}
}

.banner{border:1px solid var(--slate);border-left-width:3px;border-radius:2px;
  padding:.875rem 1.125rem;margin-bottom:.75rem;background:var(--graphite);
  font-size:.875rem}
.banner.crit{border-left-color:var(--rust)}
.banner.warn{border-left-color:var(--amber)}
.banner.ok{border-left-color:var(--patina)}
.banner b{display:block;font-family:var(--mono);font-size:.6875rem;
  letter-spacing:.14em;text-transform:uppercase;margin-bottom:.3rem}
/* The label takes the TEXT rust, not the border one. It is 11px uppercase mono
   at .14em tracking, which is small text needing 4.5:1; `--rust` gives it
   3.48:1. The border above keeps `--rust` because a 3px rail is a non-text
   boundary and 3.48:1 clears the 3:1 that applies to one. */
.banner.crit b{color:var(--rust-text)} .banner.warn b{color:var(--amber)}
.banner.ok b{color:var(--patina)}

.grid{display:grid;gap:1rem;align-items:start}
.g2{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.g3{grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}
.g4{grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}
.card{background:var(--graphite);border:1px solid var(--slate);border-radius:2px;
  padding:1.125rem}
.card h3{margin-bottom:.5rem}

.stat span.k{display:block;font-family:var(--mono);font-size:.625rem;
  letter-spacing:.14em;text-transform:uppercase;color:var(--pewter)}
.stat b{display:block;font-size:1.5rem;font-weight:500;margin-top:.35rem;
  font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.stat small{display:block;color:var(--pewter);font-size:.75rem;margin-top:.3rem}

.meter{margin-top:.75rem}
.meter .track{height:6px;background:var(--ink);border:1px solid var(--slate);
  border-radius:2px;overflow:hidden}
.meter .fill{height:100%;background:var(--patina)}
.meter .fill.over{background:var(--loss)}
.meter .legend{display:flex;justify-content:space-between;margin-top:.45rem;
  font-family:var(--mono);font-size:.6875rem;color:var(--pewter)}
.pips{display:flex;gap:.35rem;margin-top:.6rem}
.pips i{width:26px;height:6px;border:1px solid var(--slate);border-radius:2px;
  background:var(--ink)}
.pips i.lit{background:var(--loss);border-color:var(--loss)}

table{width:100%;border-collapse:collapse;font-size:.8125rem}
/* `overflow-x:auto` stays — a table wider than the deck genuinely needs it.
   What it also does, and what cost the operator four scrollbars a page, is
   make the computed `overflow-y` `auto` as well: a box cannot scroll on one
   axis and paint outside itself on the other. So this is a scroll container in
   BOTH directions, and anything overflowing its end edges by even a pixel is
   scrollable overflow rather than a decoration hanging over the border. The
   bracket corner at `bottom:-1px;right:-1px` was exactly that pixel — see
   `.scroll::after` below.

   `overscroll-behavior:contain` is the OTHER half of the operator's "weird
   scrolling stuff", and a different cause from the bracket. A touch drag that
   reaches the end of a horizontally scrolled table CHAINS into the nearest
   ancestor that can scroll, which is the page — so swiping a wide table
   sideways on a phone walks the whole deck instead, and pulling down inside the
   chat log bounces the document. `contain` stops the chain at the box and keeps
   the scroll where the gesture was aimed. It does not disable the scroll and it
   does not prevent overscroll INSIDE the box.

   Every `.scroll` in this file also carries `role="region"`, `tabindex="0"` and
   a name, and that tab stop is DELIBERATE — do not "fix" it away. It is not the
   junk one described above, which came from 1px of phantom overflow and had no
   name attached. A box that can scroll must be reachable from the keyboard, and
   whether this one actually scrolls depends on the viewport, which the server
   cannot see: the same table scrolls on a phone and does not on the deck. So
   the stop is unconditional and named, which is the trade WCAG 2.1.1 asks for.
   `tests/test_web.py` pins that every wrapper carries both. */
.scroll{overflow-x:auto;border:1px solid var(--slate);border-radius:2px}
.scroll,.chat .log{overscroll-behavior:contain}
caption{text-align:left;padding:0 0 .6rem;color:var(--pewter);font-size:.8125rem}
/* No `position:sticky` here, and its absence is deliberate rather than an
   oversight. Sticky resolves against the nearest SCROLLPORT, which is `.scroll`
   and not the viewport — and `.scroll` is sized by its content, so it has no
   vertical scroll range for a header to stick within. The rule was present for
   a long time and could never once have fired: measured in Chromium, the
   header's offset from the wrapper stayed at exactly 1px through a full page
   scroll.
   Making it work would mean giving `.scroll` a `max-height`, which puts an
   inner scroll region back on every table — the thing the fix above just took
   away. A property that cannot work reads like a feature to the next person,
   so it goes rather than staying as decoration. */
th{text-align:left;font-family:var(--mono);font-size:.625rem;letter-spacing:.12em;
  text-transform:uppercase;color:var(--pewter);font-weight:400;
  padding:.7rem .875rem;border-bottom:1px solid var(--slate);white-space:nowrap;
  background:var(--graphite)}
td{padding:.7rem .875rem;border-bottom:1px solid var(--slate);vertical-align:top}
td.r,th.r{text-align:right}
/* Tabular figures come with `.num` and every right-aligned cell in the repo
   carries it — which means the alignment a reader scans a column of money by is
   held by nobody but the author of the next cell. One forgotten `num` puts a
   column on proportional digits, where the decimal points stop lining up and
   nothing anywhere reports it. `.r` already means "this is a figure", so it can
   carry the consequence itself and the discipline stops being load-bearing. */
td.r{font-variant-numeric:tabular-nums}
tr.data td.r{white-space:nowrap}
tr:last-child td{border-bottom:0}
tr.data td{border-bottom:0}
tr.why td{padding-top:0;padding-bottom:.8rem;border-bottom:1px solid var(--slate);
  max-width:0}
tr.why .quote{border-left:2px solid var(--patina);padding-left:.875rem;
  font-family:var(--serif);font-size:.9375rem;color:var(--bone)}
.pill{display:inline-block;font-family:var(--mono);font-size:.5625rem;
  letter-spacing:.1em;text-transform:uppercase;padding:.15rem .45rem;
  border:1px solid currentColor;border-radius:2px;white-space:nowrap}
.pill.win{color:var(--gain)} .pill.loss{color:var(--loss)}
.pill.ok{color:var(--patina)} .pill.no{color:var(--rust)}
.pill.hold{color:var(--pewter)}
/* The position tags. Colour only, no layout, for the reason
   `test_no_stylesheet_rule_collides_with_a_state_badge` exists: a word that
   names a state is a bad name for a box, and a modifier that also sets a box
   property restyles whatever else happens to carry the word.
   `--rust-text` rather than `--rust`: these are 9px uppercase mono at .1em
   tracking, which is small text needing 4.5:1, and `--rust` gives 3.48:1. */
.pill.unexplained{color:var(--rust-text)}
.pill.unreadable{color:var(--amber)} .pill.stopless{color:var(--amber)}
.pill.moved{color:var(--pewter)}
/* A trailing leg. An ATTRIBUTE rather than another modifier word, the same
   move `data-verdict` and `data-vault` make: `trailing` names a state, and a
   `.pill.trailing` rule would put one more state word into the modifier
   vocabulary where the next bare `.trailing{...}` written for something else
   silently restyles it. Holo rather than amber on purpose -- a trail moving is
   the exit working, and the alert colour is reserved for the two unknowns
   beside it. */
.pill[data-stop=trailing]{color:var(--holo)}

.curve{border:1px solid var(--slate);border-radius:2px;background:var(--graphite);
  padding:1.125rem}
.curve svg{display:block;width:100%;height:auto}
.curve .line{fill:none;stroke:var(--patina);stroke-width:1.75;
  stroke-linejoin:round;stroke-linecap:round}
.curve .area{fill:color-mix(in srgb,var(--patina) 14%,transparent);stroke:none}
.curve .base{stroke:var(--slate);stroke-width:1;stroke-dasharray:3 4}
/* The axis labels are HTML, not SVG text, and that is the entire fix rather
   than a preference.
   The chart is drawn in a `viewBox="0 0 1000 240"` user space and stretched to
   whatever the card is wide, so EVERYTHING inside the SVG scales with it —
   including type. `font-size:10px` on an SVG `text` means ten USER units, so
   the rendered size is 10 x (container / 1000): legible 14px on a 1440 deck,
   and a measured 4.00px at 390 wide, where the container is 358. Unreadable,
   and unreadable in the direction nobody notices, because the deck it is built
   on is the wide one.
   Bumping the user-space size in a media query is the workaround, and it has
   to be re-tuned at every width the card can take. Taking the labels out of
   the scaled coordinate system fixes it once: `.plot` is a positioning context
   the same size as the SVG, the labels are absolutely placed in it as
   PERCENTAGES of that box — which is exactly what a user-space coordinate is,
   so they still track the data — and their type is CSS px, which is not
   scaled by anything. */
.curve .plot{position:relative}
.curve .lab{position:absolute;left:0;font-family:var(--mono);font-size:.625rem;
  line-height:1.1;color:var(--pewter);pointer-events:none;
  /* Over the area fill at the top of the range, so a background is what keeps
     it readable rather than luck about where the curve happens to sit. */
  background:var(--graphite);padding:0 .25rem;border-radius:2px}
/* The dates sit BELOW the plot in normal flow, not inside it. At 390 the plot
   is 86px tall and three rows of 10px type do not fit in it; stacking the two
   value labels inside and the two dates under is the only arrangement that
   never collides. */
.curve .axis{display:flex;justify-content:space-between;gap:1rem;
  margin:.4rem 0 0;font-family:var(--mono);font-size:.625rem;
  color:var(--pewter)}

.readout{font-family:var(--mono);font-size:.75rem;color:var(--pewter);
  background:var(--graphite);border:1px solid var(--slate);border-radius:2px;
  padding:1rem 1.125rem;white-space:pre-wrap;line-height:1.7}
.empty{color:var(--pewter);font-style:italic;padding:1.25rem}

/* ==================================================== the decision loop ==
   Whether the thing that trades is running. Coloured by a `data-loop`
   ATTRIBUTE for the reason `data-verdict` is: `ran`, `skipped` and `failed`
   are words that name states, and none of them belongs in the modifier
   vocabulary where a bare rule written for something else can reach it.

   Four values, drawn four ways on purpose. A pass that RAN is the quiet
   patina; a SKIPPED one is holo, because nothing looked and that is a fact
   rather than a fault; FAILED and NOT_RECORDED are both rust, because a cycle
   that got no decision and a moment nothing covers are the two states an
   operator has to act on. OUT_OF_RANGE is pewter: it is the read admitting it
   cannot speak for the moment, which is neither good news nor bad. */
.loopnow{margin:1rem 0 0;padding:.7rem .85rem;border-radius:2px;
  border:1px solid var(--slate);border-left-width:3px;
  background:rgba(22,27,34,.6);font-size:.8125rem;color:var(--bone)}
.loopnow[data-loop=ran]{border-left-color:var(--patina)}
.loopnow[data-loop=skipped]{border-left-color:var(--holo)}
.loopnow[data-loop=failed]{border-left-color:var(--rust)}
.loopnow[data-loop=not_recorded]{border-left-color:var(--rust)}
.loopnow[data-loop=out_of_range]{border-left-color:var(--pewter)}
.gaps{list-style:none;margin:.5rem 0 0;padding:0;font-size:.8125rem;
  color:var(--bone)}
.gaps li{padding:.4rem 0;border-bottom:1px dashed var(--slate)}
.gaps li:last-child{border-bottom:0}

/* ============================================== why each position ended ==
   The exit verdicts. Every identity here is a `data-exit` ATTRIBUTE, the same
   move `data-verdict` and `data-vault` make and for the same reason: these are
   words that name a state, and `.closed_early` in the modifier vocabulary is
   one bare rule away from restyling something it was never written for.

   NOTHING in this block carries a gain or a loss colour. The section grades
   the plan and holds no result figure at all -- painting a verdict green or
   red would smuggle the outcome back in through the one channel that survives
   the text being ignored. `closed_early` is amber because the plan was
   ABANDONED, which is a fact about discipline and true whether the trade made
   money or lost it. */
.tally{list-style:none;display:flex;flex-wrap:wrap;gap:.5rem;margin:1rem 0 0;
  padding:0}
.tally li{border:1px solid var(--slate);border-radius:2px;padding:.5rem .7rem;
  background:var(--graphite);min-width:7.5rem}
.tally li b{display:block;font-family:var(--mono);font-size:1.125rem;
  color:var(--bone);font-weight:400}
.tally li span{display:block;font-family:var(--mono);font-size:.5625rem;
  letter-spacing:.1em;text-transform:uppercase;color:var(--pewter);
  margin-top:.15rem}
.tally li[data-exit=closed_early] b{color:var(--amber)}
.tally li[data-exit=unknown] b{color:var(--pewter)}
.exits{list-style:none;margin:1rem 0 0;padding:0}
.exits li{padding:.8rem 0;border-bottom:1px dashed var(--slate)}
.exits li:last-child{border-bottom:0}
.exits .hdr{display:flex;gap:.5rem;align-items:baseline;flex-wrap:wrap;margin:0}
.exits .hdr b{font-family:var(--mono);font-size:.875rem;color:var(--bone);
  font-weight:400}
.exits .what{margin:.35rem 0 0;font-size:.8125rem;color:var(--bone)}
.exits .said{margin:.4rem 0 0;padding-left:.7rem;
  border-left:1px solid var(--slate);font-family:var(--serif);
  font-size:.875rem;color:var(--bone)}
.pill[data-exit=stop_hit]{color:var(--pewter)}
.pill[data-exit=target_hit]{color:var(--pewter)}
.pill[data-exit=closed_at_level]{color:var(--pewter)}
.pill[data-exit=expiry]{color:var(--holo)}
.pill[data-exit=unknown]{color:var(--pewter)}
.pill[data-exit=by_hand]{color:var(--bone)}
.pill[data-exit=unconfirmed_entry]{color:var(--pewter)}
/* The bucket that matters, and the only one lifted out of pewter. */
.pill[data-exit=closed_early]{color:var(--amber)}
.exits li[data-exit=closed_early]{border-left:2px solid var(--amber);
  padding-left:.7rem}

/* ------------------------------------------------------------- decisions */
.cycle{border:1px solid var(--slate);border-radius:2px;background:var(--graphite);
  margin-bottom:1rem}
.cycle>.head{display:flex;gap:.875rem;align-items:baseline;flex-wrap:wrap;
  padding:.875rem 1.125rem;border-bottom:1px solid var(--slate)}
/* The head line is a `<summary>`, so it is the control that opens the cycle.
   `display:flex` already suppresses the browser's own marker in Chromium —
   the triangle needs `display:list-item` — so `list-style` and the WebKit
   pseudo-element are belt and braces for the engines that keep it anyway, and
   the glyph below is ours. Same arrangement as `details.step` further down;
   two copies because the two are styled from opposite directions and merging
   them would tie the trail's layout to the feed block's. */
.cycle>summary{cursor:pointer;list-style:none}
.cycle>summary::-webkit-details-marker{display:none}
/* Literal characters, never CSS hex escapes — see the note on
   `details.step summary::before`. */
.cycle>summary::before{content:"▸";color:var(--pewter);font-size:.75rem}
.cycle[open]>summary::before{content:"▾"}
.cycle>summary:hover{background:rgba(78,140,125,.05)}
.cycle>summary:hover .when{color:var(--bone)}
.cycle>.head .when{font-family:var(--mono);font-size:.8125rem}
.cycle>.head .cost{margin-left:auto;font-family:var(--mono);font-size:.6875rem;
  color:var(--pewter)}
.cycle .assessment{padding:.875rem 1.125rem;color:var(--pewter);
  font-size:.875rem;border-bottom:1px solid var(--slate)}
.cycle .assessment q{font-family:var(--serif);font-size:.9375rem;color:var(--bone)}
.step{padding:.875rem 1.125rem;border-bottom:1px solid var(--slate)}
.step:last-child{border-bottom:0}
.step .what{display:flex;gap:.6rem;align-items:baseline;flex-wrap:wrap}
.step .what b{font-family:var(--mono);font-size:.875rem}
.chain{display:grid;gap:0;margin-top:.7rem;border-left:2px solid var(--slate);
  padding-left:.875rem}
.chain .rung{padding:.3rem 0;font-size:.8125rem}
.chain .rung .lbl{font-family:var(--mono);font-size:.625rem;letter-spacing:.12em;
  text-transform:uppercase;color:var(--pewter);margin-right:.5rem}
.chain .rung.gate.no{color:var(--loss)}
.chain .rung.gate.ok{color:var(--gain)}
/* Two-class modifiers under a descendant selector, matching `.rung.gate.no`
   above rather than being written as bare `.tight` — a modifier must not
   depend on winning a tie against whatever is declared later, which is the
   collision that has already cost `.pill.seed`, `.rung.gate` and `.note.alert`
   in this stylesheet. Amber rather than red on purpose: neither of these is a
   rejection. A tight stop is a fact about the denominator the size came from,
   and an unmeasured one is an indicator that did not arrive. */
.chain .rung.stopw.tight{color:var(--amber)}
.chain .rung.stopw.unmeasured{color:var(--amber)}
.reasons{margin:.35rem 0 0;padding-left:1.1rem;color:var(--loss);font-size:.8125rem}
.reasons li{margin:.15rem 0}
.pill.watch{color:var(--amber)}
.considered{padding:.5rem 0;border-bottom:1px dashed var(--slate)}
.considered:last-child{border-bottom:0}
.considered .chain{margin-top:.4rem;border-left-color:var(--slate)}
.feed{margin:.3rem 0 0;padding-left:1.1rem;font-size:.8125rem;color:var(--pewter)}
.feed li{margin:.15rem 0}
/* Where a feed item came from, on its own line UNDER the item rather than
   trailing it.
   Inline, it wraps into the headline and the two run together at 390 wide, so
   "already on file" reads as part of the story. On its own line it is a label
   about the line above it, which is what it is. Smaller and in the mono face so
   it cannot be mistaken for more of the item's text. */
.feed li .seen{display:block;font-family:var(--mono);font-size:.625rem;
  letter-spacing:.06em;margin-top:.1rem}
/* Newer / range / older. Ordinary links, so the trail is reachable with the
   script blocked and every page of it is a URL somebody can bookmark. */
.pager{display:flex;align-items:baseline;justify-content:space-between;
  gap:1rem;flex-wrap:wrap;margin-top:1.25rem;padding-top:1rem;
  border-top:1px solid var(--slate);font-family:var(--mono);font-size:.75rem}
.pager .range{color:var(--pewter)}
details.step summary{cursor:pointer;list-style:none}
details.step summary::-webkit-details-marker{display:none}
/* Literal characters, never CSS hex escapes. STYLES is an ordinary Python
   string, so a CSS escape like backslash-2-5-B-8 is read by PYTHON first,
   as an OCTAL escape, and the stylesheet receives a control character. The
   browser then draws a tofu box beside the leftover digits. Nothing warns.
   Keep backslashes out of this string altogether. */
details.step summary::before{content:"▸ ";color:var(--pewter)}
details.step[open] summary::before{content:"▾ "}
details.step summary:hover{color:var(--bone)}

/* ------------------------------------------------------------------ chat */
.chat{display:flex;flex-direction:column;gap:1rem}
.log{border:1px solid var(--slate);border-radius:2px;background:var(--graphite);
  padding:1.125rem;min-height:340px;max-height:60vh;overflow-y:auto;
  display:flex;flex-direction:column;gap:1rem}
.turn{display:flex;flex-direction:column;gap:.3rem}
.turn .who{font-family:var(--mono);font-size:.625rem;letter-spacing:.14em;
  text-transform:uppercase;color:var(--pewter)}
.turn .msg{white-space:pre-wrap;font-size:.9375rem}
.turn.agent .msg{border-left:2px solid var(--patina);padding-left:.875rem}
/* Three bouncing dots for the pending turn. Built from elements rather than
   an animated `content`, which is not reliable across browsers, and driven by
   CSS rather than a JS timer so nothing has to be torn down when the reply
   lands — replacing the message text removes the dots with it. */
.dots i{display:inline-block;width:.28em;height:.28em;margin-left:.2em;
  border-radius:50%;background:currentColor;opacity:.35;
  animation:bounce 1.05s infinite ease-in-out}
.dots i:nth-child(2){animation-delay:.15s}
.dots i:nth-child(3){animation-delay:.3s}
@keyframes bounce{
  0%,80%,100%{transform:translateY(0);opacity:.35}
  40%{transform:translateY(-.3em);opacity:1}}
@media (prefers-reduced-motion:reduce){
  .dots i{animation:none;opacity:.55}}
.turn.err .msg{border-left:2px solid var(--rust);padding-left:.875rem;
  color:var(--loss);font-family:var(--mono);font-size:.8125rem}
.composer{display:flex;gap:.6rem;align-items:flex-end}
textarea,input[type=text]{flex:1;background:var(--graphite);color:var(--bone);
  border:1px solid var(--slate);border-radius:2px;padding:.7rem .875rem;
  font-family:var(--sans);font-size:.9375rem;resize:vertical;min-height:44px}
textarea:hover,input:hover{border-color:var(--pewter)}
button.btn{background:transparent;color:var(--pewter);border:1px solid var(--slate);
  border-radius:2px;padding:.7rem 1.125rem;font-family:var(--mono);
  font-size:.6875rem;letter-spacing:.16em;text-transform:uppercase;cursor:pointer;
  transition:color .2s,border-color .2s,background .2s;min-height:44px}
button.btn:hover:not(:disabled){color:var(--bone);border-color:var(--patina);
  background:var(--graphite)}
button.btn:disabled{opacity:.5;cursor:not-allowed}
.prompts{display:flex;gap:.4rem;flex-wrap:wrap}
.prompts button{font-size:.6875rem;padding:.4rem .7rem;min-height:0;
  text-transform:none;letter-spacing:.02em;font-family:var(--sans);
  color:var(--pewter);background:transparent;border:1px solid var(--slate);
  border-radius:2px;cursor:pointer}
.prompts button:hover{color:var(--bone);border-color:var(--patina)}

/* --------------------------------------------------------------- settings */
.kv{display:grid;grid-template-columns:minmax(140px,auto) 1fr;gap:.4rem .875rem;
  font-size:.875rem}
.kv dt{font-family:var(--mono);font-size:.6875rem;letter-spacing:.1em;
  text-transform:uppercase;color:var(--pewter);padding-top:.15rem}
.kv dd{margin:0;font-family:var(--mono);font-variant-numeric:tabular-nums}
.kv dd .why{display:block;font-family:var(--sans);font-size:.8125rem;
  color:var(--pewter);margin-top:.1rem}
td.thin{color:var(--pewter);font-style:italic}
.source{margin-top:.875rem;font-size:.75rem;color:var(--pewter);
  border-top:1px solid var(--slate);padding-top:.6rem}
.source code{font-family:var(--mono);color:var(--bone)}

/* The forge: the only controls on this deck that submit anything but a
   password or a chat message. They record a change request and cannot write
   config/rules.yaml — see the note above `_forge` for why that is structural
   rather than a promise. */
.forge .field{display:flex;flex-direction:column;gap:.35rem;margin-bottom:.875rem}
.forge label{font-family:var(--mono);font-size:.625rem;letter-spacing:.14em;
  text-transform:uppercase;color:var(--pewter)}
.forge textarea,.forge input[type=text]{width:100%}
select{background:var(--graphite);color:var(--bone);width:100%;
  border:1px solid var(--slate);border-radius:2px;padding:.7rem .875rem;
  font-family:var(--sans);font-size:.9375rem;min-height:44px}
select:hover{border-color:var(--pewter)}
.forge .verdict{margin-top:1rem;border-top:1px solid var(--slate);
  padding-top:.875rem}
.forge .verdict h4{margin:0 0 .5rem;font-family:var(--mono);font-size:.6875rem;
  letter-spacing:.14em;text-transform:uppercase;color:var(--bone)}
.forge .verdict.loosening h4{color:var(--amber)}
.forge .verdict.tightening h4{color:var(--patina)}
.forge .verdict ul{margin:.2rem 0 .8rem 1.05rem;padding:0}
.forge .verdict li{font-size:.8125rem;color:var(--bone);margin-bottom:.4rem}
.forge pre{white-space:pre-wrap;word-break:break-word;margin:.6rem 0 0}

footer{padding:2rem 0 3rem;color:var(--pewter);font-size:.75rem;
  border-top:1px solid var(--slate)}

/* ======================================================= 2200 flight deck ==
   The projection layer: starfield, hyperspace jump, HUD chrome, and panels
   that materialise rather than simply being there. Decoration, and scoped so
   it can never become load-bearing.

   Three properties hold it in place and all three are the same idea.

   - **Nothing starts hidden unless the script said so.** Every entry
     animation keys off `html.fx-ready`, a class SCRIPT adds. JavaScript off,
     a script that threw, a CSP that blocked it: all render the plain
     dashboard with every figure visible. The obvious arrangement — hide in
     CSS, reveal in JS — fails to a BLANK page, on the one surface whose whole
     job is making problems visible.
   - **Reduced motion switches this off rather than down.** The block at the
     bottom of this stylesheet is the backstop; SCRIPT also declines to start
     the canvas at all, so the work is not merely invisible, it is not done.
   - **No effect touches a figure.** The starfield sits behind the content, the
     brackets are borders, the sweep is a clip-path removed on animationend.
     Nothing here reformats, rounds, hides or delays a number. A dashboard that
     made you wait to read your open risk would have got the trade backwards.

   Keep backslashes out of here, as everywhere else in STYLES. See the note
   above `details.step summary::before`. */

/* The fixed layers, back to front. Content sits at 5, the sticky header at 20
   (declared above), and the jump flash at 60 so it covers both. */
.fx{position:fixed;inset:0;pointer-events:none;contain:layout paint}
#fx-stars{z-index:0}
#fx-stars canvas{display:block;width:100%;height:100%}

/* The deck grid. Masked to the top so it reads as a floor receding under the
   content rather than as graph paper laid over it. */
.fx-grid{z-index:1;opacity:.5;
  background-image:linear-gradient(to right,var(--holo-faint) 1px,transparent 1px),
    linear-gradient(to bottom,var(--holo-faint) 1px,transparent 1px);
  background-size:72px 72px;
  -webkit-mask-image:radial-gradient(ellipse 110% 80% at 50% 0%,#000 5%,transparent 72%);
  mask-image:radial-gradient(ellipse 110% 80% at 50% 0%,#000 5%,transparent 72%)}

/* Vignette and scanlines, both deliberately near the edge of perception. Heavy
   scanlines read as a 1985 CRT, which is the opposite of the brief. */
.fx-vig{z-index:2;background:radial-gradient(ellipse 78% 62% at 50% 42%,
  transparent 45%,rgba(4,6,9,.62) 100%)}
.fx-scan{z-index:3;opacity:.5;background:repeating-linear-gradient(to bottom,
  transparent 0 3px,rgba(3,6,10,.16) 3px 4px)}

header.bar,main,footer{position:relative;z-index:5}
header.bar{background:rgba(11,14,18,.82)}

/* --------------------------------------------------------------- the jump */
/* Fired on sign-in and on every navigation. SCRIPT drives the starfield to
   lightspeed underneath; this is only the white-out at the far end of it. */
.fx-flash{z-index:60;opacity:0;background:radial-gradient(circle at 50% 50%,
  #F2FBFF 0%,#A9E2F2 30%,rgba(11,14,18,0) 70%)}
.fx-flash.out{animation:fx-out 520ms var(--ease-jump) forwards}
.fx-flash.in{animation:fx-in 720ms var(--ease) forwards}
@keyframes fx-out{from{opacity:0}to{opacity:1}}
@keyframes fx-in{from{opacity:.92}to{opacity:0}}

/* The projector beat: one bright line sweeps down the deck on arrival, the way
   a hologram resolves top to bottom. Once, then it removes itself. */
.fx-sweep{z-index:59;opacity:0;background:linear-gradient(to bottom,
  transparent 0%,var(--holo-line) 46%,rgba(210,245,255,.55) 50%,
  var(--holo-line) 54%,transparent 100%);background-size:100% 220px;
  background-repeat:no-repeat}
.fx-sweep.run{animation:fx-sweep 900ms var(--ease) forwards}
@keyframes fx-sweep{
  0%{opacity:0;background-position:0 -240px}
  12%{opacity:.85}
  85%{opacity:.25}
  100%{opacity:0;background-position:0 110vh}}

/* -------------------------------------------------- panel materialisation */
/* Every card, section and table arrives rather than appearing. The stagger is
   set per element by SCRIPT in --fx-d so a Board of twelve tiles reads as one
   deck powering up instead of twelve unrelated fades. */
.fx-ready .fx-panel{opacity:0}
.fx-ready .fx-panel.fx-in{animation:fx-rise 560ms var(--ease) var(--fx-d,0ms) both}
/* Removed on animationend, never left applied. `both` retains the final frame,
   and a retained clip-path makes the element a containing block, which would
   quietly break every sticky table header on the page. */
.fx-ready .fx-panel.fx-done{opacity:1;animation:none;clip-path:none;transform:none}
@keyframes fx-rise{
  0%{opacity:0;transform:translate3d(0,20px,0);clip-path:inset(0 0 100% 0)}
  55%{opacity:1}
  100%{opacity:1;transform:none;clip-path:inset(0 0 -2px 0)}}

/* ---------------------------------------------------------- the HUD chrome */
/* Bracket corners, targeting-computer style. Two pseudo-elements rather than
   four: the diagonal pair reads as a frame and costs half the boxes. */
.card,.curve,.cycle,.scroll,.readout,.banner,.chat .log{position:relative}
.card::before,.curve::before,.cycle::before,.scroll::before,.readout::before,
.banner::before,.chat .log::before,
.card::after,.curve::after,.cycle::after,.scroll::after,.readout::after,
.banner::after,.chat .log::after{
  content:"";position:absolute;width:9px;height:9px;pointer-events:none;
  border:1px solid var(--holo);opacity:.45;transition:opacity .25s var(--ease)}
.card::before,.curve::before,.cycle::before,.readout::before,
.banner::before{top:-1px;left:-1px;border-right:0;border-bottom:0}
.card::after,.curve::after,.cycle::after,.readout::after,
.banner::after{bottom:-1px;right:-1px;border-left:0;border-top:0}
/* The two scroll containers get the SAME brackets at zero rather than at -1px,
   and this is the operator's "weird scrolling stuff" in one declaration.
   `.scroll` is `overflow-x:auto` and `.chat .log` is `overflow-y:auto`, and in
   CSS either one makes the OTHER axis `auto` too — so both boxes are scroll
   containers on both axes. End-direction overflow from an absolutely positioned
   descendant is scrollable overflow, not clipped decoration, so a bracket
   hanging 1px past the bottom-right corner gave every table on the deck exactly
   1px of scroll range on each axis. Measured: `scrollWidth 1239 / clientWidth
   1238`, two full-length 15px scrollbars per table, six on Analytics, the first
   notch of any wheel gesture over a table eaten moving it one pixel, and a junk
   keyboard tab stop with no `tabindex` — Chrome makes a scroll container
   focusable when nothing inside it is.
   The start-direction pair is moved with it. `top:-1px` does not add scrollable
   overflow (start overflow is clipped) but it IS clipped, so leaving it would
   draw the top-left bracket a pixel short of the bottom-right one on the same
   box. Both corners now sit against the padding box, which is where the border
   is.
   Do not "restore" these to -1px, and do not answer the scrollbars by removing
   `overflow-x` — a table wider than the deck needs it. */
.scroll::before,.chat .log::before{top:0;left:0;border-right:0;border-bottom:0}
.scroll::after,.chat .log::after{bottom:0;right:0;border-left:0;border-top:0}
.card:hover::before,.card:hover::after,.cycle:hover::before,.cycle:hover::after{opacity:.9}

.card,.curve,.cycle,.readout,.chat .log{
  background:linear-gradient(180deg,rgba(22,27,34,.88),rgba(17,21,27,.92));
  backdrop-filter:blur(3px);
  box-shadow:inset 0 1px 0 rgba(111,211,232,.07),0 12px 30px -18px rgba(0,0,0,.9)}

/* ------------------------------------------------------------------- nav */
/* A nav link is a jump target, so it gets a charge line that fills before the
   ship moves. */
nav a{position:relative;overflow:hidden}
nav a::after{content:"";position:absolute;left:0;right:0;bottom:0;height:1px;
  background:var(--holo);transform:scaleX(0);transform-origin:left;
  transition:transform .28s var(--ease);opacity:.85}
nav a:hover::after,nav a[aria-current=page]::after{transform:scaleX(1)}

/* The reactor tell. A steady dot says "rendered once"; a breathing one says
   "this process is up", which is the actual question being asked of it. */
.live i{box-shadow:0 0 0 0 var(--holo);animation:fx-pulse 2.8s ease-in-out infinite}
@keyframes fx-pulse{
  0%,100%{box-shadow:0 0 0 0 rgba(111,211,232,.5);opacity:.75}
  50%{box-shadow:0 0 0 4px rgba(111,211,232,0);opacity:1}}

/* --------------------------------------------------------------- dreaming */
/* The dreamer is allowed to be playful and the rest of this stylesheet is not.
   That contrast is the point: a surface producing speculation should not look
   like the surface reporting positions, because they carry different weight and
   a reader must never mix them up. Everything here is warmer, rounder and
   softer than the Board, and it is the only place in the app that is. */
.dreamer{width:120px;height:120px;display:block;overflow:visible}
.dreamer .pad{fill:none;stroke:var(--holo);stroke-width:1;opacity:.35;
  animation:fx-pad 3.2s ease-in-out infinite}
.dreamer .floaty{animation:fx-float 3.2s ease-in-out infinite;
  transform-origin:60px 60px}
.dreamer .head,.dreamer .ear{fill:rgba(111,211,232,.10);stroke:var(--holo);
  stroke-width:1.5}
.dreamer .robe{fill:rgba(78,140,125,.16);stroke:var(--patina);stroke-width:1.5}
.dreamer .eye{fill:var(--bone);animation:fx-blink 6.4s ease-in-out infinite;
  transform-origin:center;transform-box:fill-box}
.dreamer .mouth{fill:none;stroke:var(--holo);stroke-width:1.5;
  stroke-linecap:round;opacity:.8}
.dreamer .ear.left{animation:fx-ear-l 5.1s ease-in-out infinite;
  transform-origin:38px 53px;transform-box:view-box}
.dreamer .ear.right{animation:fx-ear-r 5.1s ease-in-out infinite;
  transform-origin:82px 53px;transform-box:view-box}
@keyframes fx-float{0%,100%{transform:translateY(0)}50%{transform:translateY(-7px)}}
@keyframes fx-pad{0%,100%{opacity:.35;transform:scale(1)}
  50%{opacity:.15;transform:scale(.9);transform-origin:60px 106px}}
@keyframes fx-blink{0%,92%,100%{transform:scaleY(1)}96%{transform:scaleY(.1)}}
@keyframes fx-ear-l{0%,100%{transform:rotate(-14deg)}50%{transform:rotate(-19deg)}}
@keyframes fx-ear-r{0%,100%{transform:rotate(14deg)}50%{transform:rotate(19deg)}}

/* The chain, drawn as one. Scrolls rather than wraps: a wrapped chain stops
   reading as a sequence, which is the only thing the drawing is for. */
.chainviz{display:block;height:62px;width:100%;max-width:100%;overflow:visible;
  margin:0 0 .5rem}
.chainviz .node{fill:var(--ink);stroke-width:1.5}
.chainviz .node.checked{stroke:var(--patina)}
.chainviz .node.open{stroke:var(--amber)}
.chainviz .idx{fill:var(--pewter);font-family:var(--mono);font-size:11px;
  text-anchor:middle}
.chainviz .link{stroke-width:1.5}
.chainviz .link.solid{stroke:var(--patina)}
.chainviz .link.broken{stroke:var(--amber);opacity:.75}
.chainviz .gapmark{fill:var(--amber);font-family:var(--mono);font-size:12px;
  text-anchor:middle}
.chain-scroll{overflow-x:auto;overflow-y:hidden}

/* Thinking: the pad spins up and the eyes widen. Driven by a class the chat
   panel adds while a request is in flight, so it means "a call is open" and
   never "an idea is forming". */
.dreamer.thinking .pad{animation-duration:1.1s}
.dreamer.thinking .floaty{animation-duration:1.6s}

.dream-head{display:flex;gap:1.5rem;align-items:center;flex-wrap:wrap;
  margin-bottom:1.5rem}
.dream-head .who h1{font-size:1.75rem}
.dream-head .who p{margin:.35rem 0 0;max-width:52ch}

/* A dream is a card that reads top to bottom as an argument: the spark, then
   the chain, then what would break it, then what was decided. */
.dream{border:1px solid var(--slate);border-radius:2px;margin-bottom:1rem;
  background:linear-gradient(180deg,rgba(22,27,34,.9),rgba(17,21,27,.93));
  position:relative}
.dream>.top{display:flex;gap:.75rem;align-items:baseline;flex-wrap:wrap;
  padding:.875rem 1.125rem;border-bottom:1px solid var(--slate)}
.dream>.top h3{font-size:1.0625rem;margin:0}
.dream>.top .when{margin-left:auto;font-family:var(--mono);font-size:.6875rem;
  color:var(--pewter)}
/* `.spark`, not `.seed`. The stage pills are named after the stages, so a
   `.dream .seed` rule here also matched <span class="pill seed"> and dressed the
   stage badge as a full-width paragraph. Scope by element role, never by a word
   that is also a state name. */
.dream .spark{padding:.875rem 1.125rem;border-bottom:1px solid var(--slate);
  font-family:var(--serif);font-size:.9375rem;color:var(--bone)}
.dream .spark .from{display:block;margin-top:.4rem;font-family:var(--mono);
  font-size:.625rem;letter-spacing:.12em;text-transform:uppercase;
  color:var(--pewter)}
.dream .body{padding:.875rem 1.125rem}

/* The chain. Each hop is a separate claim and is marked for whether anybody
   checked it, because a chain whose links cannot be attacked one at a time is a
   story rather than a hypothesis. */
.hops{list-style:none;margin:0;padding:0;counter-reset:hop}
.hops li{position:relative;padding:.5rem 0 .5rem 2.25rem;font-size:.875rem;
  border-left:1px solid var(--slate);margin-left:.5rem}
.hops li::before{counter-increment:hop;content:counter(hop);
  position:absolute;left:-.7rem;top:.55rem;width:1.4rem;height:1.4rem;
  display:grid;place-items:center;font-family:var(--mono);font-size:.625rem;
  border:1px solid var(--slate);border-radius:50%;background:var(--ink);
  color:var(--pewter)}
.hops li.checked::before{border-color:var(--patina);color:var(--patina)}
.hops li.open::before{border-color:var(--amber);color:var(--amber)}
.hops .src{display:block;margin-top:.25rem;font-family:var(--mono);
  font-size:.6875rem;color:var(--pewter)}
.hops li.open .src{color:var(--amber)}

/* UNSCOPED, and that is the fix rather than an oversight. These were
   `.dream .weak`, which silently did not reach `WORKED_EXAMPLE` -- the
   illustration sits in `.worked`, not `.dream`. The label lost `display:block`
   and rendered as "Weakest hopWhether the brood overlap...", the amber box did
   not draw, and it was valid CSS producing a plausible page. Found by the
   operator on a phone, which is now the FOURTH time a stylesheet fault has
   been invisible to the suite and visible to a pair of eyes.
   A scoped rule with two use sites is a rule with one place to forget. */
.weak{margin:.875rem 0 0;padding:.6rem .75rem;font-size:.8125rem;
  border:1px solid rgba(192,138,62,.35);border-left-width:3px;border-radius:2px;
  background:rgba(192,138,62,.07);color:var(--bone)}
.weak b{font-family:var(--mono);font-size:.625rem;letter-spacing:.14em;
  text-transform:uppercase;color:var(--amber);display:block;margin-bottom:.2rem}
.trigger{margin:.75rem 0 0;font-size:.8125rem;color:var(--pewter)}
.trigger b{color:var(--bone);font-weight:400}

/* The thoughts stream: the working, in order, including the steps where it
   changed its mind. That is usually the interesting part, which is why this is
   append-only and why nothing overwrites it. */
.stream{margin-top:1rem;border-top:1px solid var(--slate);padding-top:.75rem}
.stream summary{cursor:pointer;list-style:none;font-family:var(--mono);
  font-size:.625rem;letter-spacing:.14em;text-transform:uppercase;
  color:var(--pewter)}
.stream summary::-webkit-details-marker{display:none}
.stream summary::before{content:"▸ ";color:var(--pewter)}
.stream[open] summary::before{content:"▾ "}
.stream summary:hover{color:var(--bone)}
.stream ol{list-style:none;margin:.75rem 0 0;padding:0}
.stream li{display:grid;grid-template-columns:5.5rem 1fr;gap:.875rem;
  padding:.5rem 0;border-bottom:1px dashed var(--slate);font-size:.8125rem}
.stream li:last-child{border-bottom:0}
.stream .st{font-family:var(--mono);font-size:.625rem;letter-spacing:.12em;
  text-transform:uppercase;color:var(--pewter)}
.stream .st.explore{color:var(--holo)}
.stream .st.iterate{color:var(--amber)}
.stream .st.verdict{color:var(--patina)}

.pill.seed{color:var(--pewter)} .pill.explore{color:var(--holo)}
.pill.iterate{color:var(--amber)} .pill.verdict{color:var(--bone)}
.pill.keep{color:var(--gain)} .pill.park{color:var(--amber)}
.pill.drop{color:var(--pewter)}
.pill.unverified{color:var(--amber)} .pill.partial{color:var(--pewter)}
.pill.sourced{color:var(--patina)}

/* The illustration on an empty deck. Dashed, so it cannot be mistaken at a
   glance for a dream the agent actually had. */
.worked{border:1px dashed var(--slate);border-radius:2px;padding:1.125rem;
  background:rgba(22,27,34,.5)}
.worked h3{margin-bottom:.5rem}

/* ==================================================== the shelves a dream sits on
   Five vaults, and the page is built around them rather than around one flat
   list, because WHERE a dream is held is the fact that decides what it can do:
   only the dream vault is visible to the trading agent, and only an adopted
   dream carries a live symbol permission.

   Every shelf identity is a `data-vault` ATTRIBUTE rather than a class, and
   that is the two-class lesson taken seriously rather than survived. `vault`,
   `archive` and `adopted` are all words that name a state, and a `.shelf.vault`
   rule would put three more of them into the modifier vocabulary — where the
   next bare `.vault{padding:...}` written for something else silently restyles
   them. An attribute selector cannot collide with a class at all. */
.shelfrail{display:grid;gap:.6rem;margin-top:1.5rem;
  grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}
.shelf{display:block;text-decoration:none;border:1px solid var(--slate);
  border-radius:2px;padding:.8rem .9rem;background:var(--graphite);
  border-top-width:2px;border-top-color:var(--slate)}
.shelf:hover{background:rgba(41,49,60,.55)}
.shelf .n{display:block;font-family:var(--mono);font-size:.625rem;
  letter-spacing:.14em;text-transform:uppercase;color:var(--pewter)}
.shelf .c{display:block;font-family:var(--mono);font-size:1.375rem;
  font-variant-numeric:tabular-nums;color:var(--bone);margin-top:.3rem}
.shelf .s{display:block;font-size:.75rem;color:var(--pewter);margin-top:.2rem}
.shelf[data-vault=prophecy]{border-top-color:var(--holo)}
.shelf[data-vault=vault]{border-top-color:var(--patina)}
.shelf[data-vault=adopted]{border-top-color:var(--amber)}
.shelf[data-vault=prophecy] .c{color:var(--holo)}
.shelf[data-vault=vault] .c{color:var(--patina)}
.shelf[data-vault=adopted] .c{color:var(--amber)}
/* An empty shelf is a stated zero, never a skipped row. `DreamSummary.by_vault`
   keys every vault for this reason: a missing row reads as "no data" and a nought
   reads as a fact, and on the adopted shelf those are very different claims. */
.shelf[data-empty] .c{color:var(--pewter)}

.shelf-head{display:flex;gap:1rem;align-items:center;flex-wrap:wrap;
  margin-bottom:.875rem}
.shelf-head .lede{margin:.2rem 0 0;max-width:60ch;font-size:.8125rem;
  color:var(--pewter)}
.shelf-head h2{margin:0}

/* Grogu with the crystal ball, for the prophecy shelf. Drawn in primitives for
   the reason the avatar is: it inherits the palette, it costs no request, and
   `aria-hidden` because it is a mood and everything it says is said in text. */
.oracle{width:104px;height:104px;display:block;overflow:visible;flex:none}
.oracle .orb{fill:rgba(111,211,232,.12);stroke:var(--holo);stroke-width:1.5;
  animation:fx-orb 4.4s ease-in-out infinite;transform-origin:60px 82px;
  transform-box:view-box}
.oracle .glint{fill:var(--holo);opacity:.85}
.oracle .head,.oracle .ear{fill:rgba(111,211,232,.10);stroke:var(--holo);
  stroke-width:1.5}
.oracle .robe{fill:rgba(78,140,125,.16);stroke:var(--patina);stroke-width:1.5}
.oracle .eye{fill:var(--bone)}
.oracle .stand,.oracle .arm{fill:none;stroke:var(--patina);stroke-width:1.5;
  stroke-linecap:round}
.oracle .hand{fill:rgba(78,140,125,.35);stroke:var(--patina);stroke-width:1.2}
@keyframes fx-orb{0%,100%{opacity:.55}50%{opacity:1}}

/* The dream vault itself: the one shelf the trading agent can see, so it is the
   one that gets a door drawn round it. */
.vaultdoor{border:1px solid var(--patina);border-radius:2px;padding:1.125rem;
  background:radial-gradient(120% 90% at 50% 0,rgba(78,140,125,.09),
    rgba(22,27,34,.55) 62%)}

/* What a prophecy pre-registered, and how much of it the world has settled.
   Counted, never summarised: "2 of 3" is arithmetic and "close" is an opinion. */
.conds{margin:.875rem 0 0;padding:0;list-style:none}
.conds li{position:relative;padding:.3rem 0 .3rem 1.4rem;font-size:.8125rem;
  color:var(--bone)}
.conds li::before{content:"·";position:absolute;left:.3rem;top:.28rem;
  font-family:var(--mono);color:var(--pewter)}
/* Five states, five markers, and the two ANSWERED ones must not look alike.
   `data-met` used to be the only attribute, so a claim the operator had ruled
   OUT drew the same dot as one nobody had looked at — the missing-versus-zero
   rule wearing a bullet point. The selectors are written `li[data-state=...]`,
   one class of specificity above the base rule, so none of them depends on
   winning a tie: see the `.note.alert` finding.
   `data-met` is kept beside it because it is the attribute the fused-card and
   dream tests already read, and because a marker that survives an unknown
   future state is better than one that vanishes. */
.conds li[data-met]::before{content:"✓";color:var(--patina);font-size:.75rem}
.conds li[data-state=ruled_out]::before{content:"✗";color:var(--rust-text);
  font-size:.75rem}
.conds li[data-state=overdue]::before{content:"!";color:var(--amber);
  font-weight:700}
.conds li[data-state=unsettleable]::before{content:"?";color:var(--pewter)}
.conds .thr{font-family:var(--mono);font-size:.6875rem;color:var(--pewter);
  display:block}
.conds .thr .alert{color:var(--amber)}
.condhead{margin:.875rem 0 0;font-family:var(--mono);font-size:.625rem;
  letter-spacing:.14em;text-transform:uppercase;color:var(--pewter)}
/* The command that answers a question this page can only show. Scrolls in its
   own box rather than wrapping: a broken command line is worse than one that
   needs a swipe, because the wrapped half looks like a second command. */
.cmd{margin:.875rem 0 0;padding:.75rem .875rem;background:var(--ink);
  border:1px solid var(--slate);border-radius:2px;font-family:var(--mono);
  font-size:.6875rem;line-height:1.7;color:var(--bone);overflow-x:auto;
  overscroll-behavior:contain}

/* Symbiosis. The card is drawn fused by `dream_fx.CSS`; these are the parts
   that carry the ARGUMENT rather than the treatment.
   `article.fused` rather than `.dream.fused` on purpose: element-plus-class is
   deliberately more specific than `.dream`, where a second class would be a tie
   decided by which file was concatenated last — the mistake this repository has
   made three times. */
article.fused{border-color:var(--patina)}
/* A parent crest is a LINK to a dream that is still there. It reads as one,
   which is the point: `fuse` consumes nothing, and a crest greyed out or struck
   through would describe a store that does not work that way. Specificity is
   deliberate again — `.fused .crest` is two classes, so an element-qualified
   three is what wins rather than an ordering. */
.fused a.crest{color:var(--bone);text-decoration:none}
.fused a.crest:hover{border-color:var(--patina);background:rgba(78,140,125,.1)}
.symbiosis{margin:0 0 .875rem;padding:.7rem .85rem;border-radius:2px;
  border:1px solid rgba(78,140,125,.4);border-left-width:3px;
  background:rgba(78,140,125,.07)}
.symbiosis b{display:block;font-family:var(--mono);font-size:.625rem;
  letter-spacing:.14em;text-transform:uppercase;color:var(--patina);
  margin-bottom:.25rem}
.symbiosis p{margin:0;font-size:.8125rem;color:var(--bone)}
.symbiosis p + p{margin-top:.35rem}
/* The same claim, marked where it sits in the chain. Attributes rather than
   classes, and a real element rather than `::after` content, because ONE HOP
   CAN BE BOTH — a shared claim can also be the weakest link — and a
   pseudo-element carries one label. Generated content is also announced
   inconsistently by screen readers, which is a limit this stylesheet already
   states about `td[data-l]::before`. */
.hops li[data-shared]{background:rgba(78,140,125,.07)}
.hops li[data-weakest]{background:rgba(192,138,62,.07)}
.hops li[data-weakest]::before{border-color:var(--amber);color:var(--amber);
  box-shadow:0 0 0 3px rgba(192,138,62,.16)}
.hops .hoptags{display:block;margin-top:.3rem}
.hops .hoptags i{font-family:var(--mono);font-size:.5625rem;letter-spacing:.12em;
  text-transform:uppercase;font-style:normal;margin-right:.6rem}
.hops .hoptags i[data-tag=shared]{color:var(--patina)}
.hops .hoptags i[data-tag=weakest]{color:var(--amber)}
/* The named weakest link, ringed in the diagram as well as in the list. Same
   attribute, same reason: `.node.weak` would put `weak` into the modifier
   vocabulary, where the padded amber `.weak` panel would style an SVG circle. */
.chainviz .node[data-weakest]{stroke-width:3}
.lineage{margin:.75rem 0 0;font-family:var(--mono);font-size:.6875rem;
  letter-spacing:.06em;color:var(--patina)}

/* Why a finished keep is still on the workbench. Deliberately NOT `.weak`,
   which is amber and means "this is the link that could kill the chain". This
   is the promotion rule explaining itself, which is an instruction rather than
   a danger, and spending the alert colour on it would leave the two reading as
   the same kind of fact on the same card. */
.notyet{margin:.875rem 0 0;padding:.6rem .75rem;font-size:.8125rem;
  border:1px solid var(--slate);border-left-width:3px;border-radius:2px;
  background:rgba(22,27,34,.6);color:var(--bone)}
.notyet b{font-family:var(--mono);font-size:.625rem;letter-spacing:.14em;
  text-transform:uppercase;color:var(--pewter);display:block;margin-bottom:.2rem}

/* The wisp an adopted dream leaves the dreamer, and what the adoption granted.
   Two different facts side by side: one is what Grogu kept, the other is what
   the account may trade because of it. */
.wisptrace{margin:.875rem 0 0;padding:.7rem .85rem;border-radius:2px;
  border:1px dashed var(--slate);background:rgba(22,27,34,.5);
  font-family:var(--serif);font-size:.875rem;color:var(--bone)}
.wisptrace b{display:block;font-family:var(--mono);font-size:.625rem;
  letter-spacing:.14em;text-transform:uppercase;color:var(--pewter);
  margin-bottom:.25rem}
.grantline{margin:.6rem 0 0;font-size:.8125rem;color:var(--bone)}
.grantline .sym{font-family:var(--mono);color:var(--amber)}
.grantline .lbl{font-family:var(--mono);font-size:.625rem;letter-spacing:.14em;
  text-transform:uppercase;color:var(--pewter);margin-right:.4rem}

/* The two agents talking, rendered so a person can read it. Stored either way,
   including the exchanges that ended in nothing, because a dream the trader
   kept declining is a fact about the dreamer worth having. */
.talk{margin-top:1rem;border-top:1px solid var(--slate);padding-top:.75rem}
.talk summary{cursor:pointer;list-style:none;font-family:var(--mono);
  font-size:.625rem;letter-spacing:.14em;text-transform:uppercase;
  color:var(--pewter)}
.talk summary::-webkit-details-marker{display:none}
.talk summary::before{content:"▸ ";color:var(--pewter)}
.talk[open] summary::before{content:"▾ "}
.talk summary:hover{color:var(--bone)}
.talk ol{list-style:none;margin:.75rem 0 0;padding:0}
.talk .said{padding:.5rem 0 .5rem .9rem;border-left:2px solid var(--slate);
  margin-bottom:.5rem}
.talk .said:last-child{margin-bottom:0}
.talk .said .hdr{display:flex;gap:.5rem;align-items:baseline;flex-wrap:wrap;
  font-family:var(--mono);font-size:.625rem;letter-spacing:.12em;
  text-transform:uppercase;color:var(--pewter);margin-bottom:.2rem}
.talk .said .hdr .name{color:var(--bone)}
.talk .said .hdr .at{margin-left:auto}
.talk .said p{margin:0;font-size:.875rem;color:var(--bone)}
.talk .said[data-who=dreamer]{border-left-color:var(--holo)}
.talk .said[data-who=dreamer] .hdr .name{color:var(--holo)}
.talk .said[data-who=trader]{border-left-color:var(--patina)}
.talk .said[data-who=trader] .hdr .name{color:var(--patina)}
.talk .said[data-who=operator]{border-left-color:var(--bone)}
.talk .said[data-who=fusion]{border-left-color:var(--amber);
  border-left-style:dashed}
.talk .said[data-who=fusion] .hdr .name{color:var(--amber)}

/* ============================================ what the two agents settled on ==
   The verdict one exchange reached, on the card of the dream it was about. The
   transcript above it is the evidence and is collapsed; this is the conclusion,
   and it is the only part a reader who opens nothing will see.

   Every verdict identity is a `data-verdict` ATTRIBUTE rather than a class, and
   that is the shelves' `data-vault` reasoning in a second place. `promote`,
   `defer`, `decline` and `archive` are all words that name a state, and a
   `.conclave.archive` rule would put four more of them into the modifier
   vocabulary -- where the next bare `.archive{padding:...}` written for
   something else silently restyles them. An attribute selector cannot collide
   with a class at all. */
.conclave{margin:1rem 0 0;padding:.7rem .85rem;border-radius:2px;
  border:1px solid var(--slate);border-left-width:3px;
  background:rgba(22,27,34,.6)}
.conclave > b{display:block;font-family:var(--mono);font-size:.625rem;
  letter-spacing:.14em;text-transform:uppercase;color:var(--pewter);
  margin-bottom:.25rem}
.conclave .what{margin:0;font-size:.8125rem;color:var(--bone)}
.conclave .words{margin:.5rem 0 0;padding-left:.7rem;
  border-left:1px solid var(--slate);font-family:var(--serif);
  font-size:.875rem;color:var(--bone)}
.conclave .meta{margin:.5rem 0 0;font-family:var(--mono);font-size:.625rem;
  letter-spacing:.12em;text-transform:uppercase;color:var(--pewter)}
.conclave .did{margin:.5rem 0 0;font-size:.8125rem;color:var(--patina)}
.conclave .undone{margin:.5rem 0 0;font-size:.8125rem;color:var(--bone)}
.conclave .undone b{color:var(--amber);font-weight:400}
/* The wake condition a deferral is parked against: the difference between a
   real deferral and running out of things to say. Same monospace threshold the
   prophecy conditions use, because it is the same kind of claim -- prose for a
   person and a number for code. */
.conclave .wake{margin:.5rem 0 0;font-size:.8125rem;color:var(--bone)}
.conclave .wake b{display:block;font-family:var(--mono);font-size:.625rem;
  letter-spacing:.14em;text-transform:uppercase;color:var(--holo);
  margin-bottom:.2rem}
.conclave .thr{font-family:var(--mono);font-size:.6875rem;color:var(--pewter);
  display:block;margin-top:.2rem}
.conclave[data-verdict=promote]{border-left-color:var(--patina)}
.conclave[data-verdict=defer]{border-left-color:var(--holo)}
.conclave[data-verdict=decline]{border-left-color:var(--pewter)}
.conclave[data-verdict=archive]{border-left-color:var(--slate)}
.conclave[data-verdict=no_decision]{border-left-color:var(--amber)}
/* A failed call is the one no-decision that is a FAULT; a turn cap and an
   unconditioned deferral are facts about the two agents. Written with both
   attributes deliberately, so it outranks the rule above on specificity rather
   than on which of the two happens to be declared last -- the tie this
   stylesheet has lost three times. */
.conclave[data-verdict=no_decision][data-cause=call_failed]{
  border-left-color:var(--rust)}
/* Never conferred: a third state, drawn as one. Dashed, so it cannot be read at
   a glance as a verdict that happens to be quiet. */
.conclave[data-unconferred]{border-left-style:dashed;
  border-left-color:var(--slate);background:rgba(22,27,34,.35)}

/* The same verdicts across every dream, newest first, so "what have those two
   been doing" is answerable without opening a card. */
.confeed{list-style:none;margin:1rem 0 0;padding:0}
.confeed li{padding:.7rem 0;border-bottom:1px dashed var(--slate)}
.confeed li:last-child{border-bottom:0}
.confeed .hdr{display:flex;gap:.6rem;align-items:baseline;flex-wrap:wrap}
.confeed .hdr a{color:var(--bone);text-decoration:none;
  border-bottom:1px solid var(--slate)}
.confeed .hdr a:hover{border-bottom-color:var(--patina)}
.confeed .subject{color:var(--bone)}
.confeed .at{margin-left:auto;font-family:var(--mono);font-size:.625rem;
  letter-spacing:.12em;color:var(--pewter)}
.confeed .why{margin:.35rem 0 0;font-size:.8125rem;color:var(--bone)}
.confeed .meta{margin:.35rem 0 0;font-family:var(--mono);font-size:.625rem;
  letter-spacing:.12em;text-transform:uppercase;color:var(--pewter)}
.pill[data-verdict=promote]{color:var(--patina)}
.pill[data-verdict=defer]{color:var(--holo)}
.pill[data-verdict=decline]{color:var(--bone)}
.pill[data-verdict=archive]{color:var(--pewter)}
.pill[data-verdict=no_decision]{color:var(--amber)}

/* A trade that came from a dream. `.wisped` and `.from-dream` are styled in
   `dream_fx.CSS`; this is the one thing that file cannot know — that the mote
   layer needs a block box to inset itself against, and a table cell's content
   is wrapped in a span for the 760px card layout. */
td .wisped{display:block}
td .wisped .from-dream{display:block;margin-top:.15rem;text-decoration:none}
td .wisped .from-dream:hover{text-decoration:underline}

/* ================================================== motion that MEANS things ==
   Everything above this line is decoration: a starfield, a jump, panels that
   materialise. It sets a mood and carries no information, which is why it is
   built to be removable without loss.

   Everything BELOW it is the opposite. These animations are the only way some
   state reaches the operator at all, and each one is tied to a specific fact:

     .tick        a figure changed, and which way
     .stale       what you are reading is older than it looks
     .fresh       this arrived since you last looked
     .attn        this needs a decision
     .link        whether the deck is actually connected

   The rule that follows from that: a motion carrying information must still
   have a non-motion channel. Reduced motion switches the ANIMATION off and
   leaves the colour, the border and the text, because someone who asked for
   less movement did not ask to be told less. Every rule here is paired.
   ========================================================================== */

/* ------------------------------------------------------------- a figure ticks */
/* The flash is brief and the direction is the message. A green flash on a
   falling number would be worse than no flash, so the class is set from the
   sign of the delta and never from the fact that something happened. */
.tick{transition:color .18s var(--ease)}
.tick.up{animation:fx-tick-up 1100ms var(--ease)}
.tick.down{animation:fx-tick-down 1100ms var(--ease)}
@keyframes fx-tick-up{
  0%{color:var(--gain);text-shadow:0 0 14px rgba(95,167,149,.55)}
  100%{color:inherit;text-shadow:none}}
@keyframes fx-tick-down{
  0%{color:var(--loss);text-shadow:0 0 14px rgba(192,112,123,.5)}
  100%{color:inherit;text-shadow:none}}

/* The arrow is the non-motion channel. It persists after the flash has gone, so
   a reader who arrived late, or who has motion switched off, still sees which
   way the last move went. */
.tick .dir{font-size:.7em;margin-left:.35em;opacity:.75}
.tick.up .dir{color:var(--gain)}
.tick.down .dir{color:var(--loss)}

/* ------------------------------------------------------- waiting for a figure */
/* Not a spinner. A spinner says "busy" and says nothing about what is coming,
   so it reads the same whether the value is a second away or never arriving.
   This holds the SHAPE of the number that is coming, at the width it will be,
   so nothing reflows when it lands. */
.pending{display:inline-block;min-width:5ch;border-radius:2px;
  background:linear-gradient(90deg,rgba(41,49,60,.35) 25%,rgba(111,211,232,.16) 50%,
    rgba(41,49,60,.35) 75%);
  background-size:220% 100%;animation:fx-wait 1.4s ease-in-out infinite;
  color:transparent;user-select:none}
@keyframes fx-wait{0%{background-position:120% 0}100%{background-position:-20% 0}}

/* ------------------------------------------------------------------- staleness */
/* A figure that stopped updating must not keep looking current. This is the
   `is_stale` rule from tailnet.py applied to pixels: a stale reading is
   reported as stale rather than quietly presented as fresh. */
.stale{opacity:.55}
.stale::after{content:" stale";font-family:var(--mono);font-size:.6rem;
  letter-spacing:.12em;text-transform:uppercase;color:var(--amber);
  margin-left:.4rem;opacity:1}

/* ------------------------------------------------ new since you were last here */
/* The operator opens this two or three times a day, so "what changed" is the
   real question. A left edge rather than a badge: it marks a run of rows at a
   glance without adding a thing to read in each one. */
.fresh{position:relative}
.fresh::before{content:"";position:absolute;left:-1px;top:-1px;bottom:-1px;
  width:2px;background:var(--holo);box-shadow:0 0 10px rgba(111,211,232,.5)}
.fresh-tag{font-family:var(--mono);font-size:.5625rem;letter-spacing:.14em;
  text-transform:uppercase;color:var(--holo);margin-left:.5rem}

/* ------------------------------------------------------------ needs a decision */
/* Reserved for things that need a person, and used sparingly on purpose. An
   interface where several things pulse has taught its reader to ignore pulsing. */
.attn{animation:fx-attn 2.4s ease-in-out infinite}
@keyframes fx-attn{
  0%,100%{box-shadow:0 0 0 0 rgba(192,138,62,.35)}
  50%{box-shadow:0 0 0 5px rgba(192,138,62,0)}}

/* ------------------------------------------------------------- the link itself */
/* The reactor dot used to pulse unconditionally, which implied "live" on a page
   that was frozen the moment it rendered. Now it reports the connection, and
   the three states are visually distinct rather than three shades of the same
   thing: lit and breathing, amber and slower, grey and still. */
.live i{transition:background .3s var(--ease),box-shadow .3s var(--ease)}
.live.link-live i{background:var(--holo)}
.live.link-retry i{background:var(--amber);animation-duration:1.1s}
.live.link-down i{background:var(--pewter);animation:none;box-shadow:none}
/* A fourth state, because "signed out" is not a degree of "reconnecting".
   RECONNECTING says wait; this says the wait is over and there is something
   for the operator to do, so it is the one link state that is coloured like a
   problem and carries an instruction rather than a status word. */
.live.link-out i{background:var(--rust);animation:none;box-shadow:none}
.live.link-out .link-label{color:var(--rust)}
.live .link-label{font-size:.5625rem;letter-spacing:.14em;margin-left:.4rem;
  color:var(--pewter)}

/* ------------------------------------------------- reduced motion, paired rules */
/* The information survives; only the movement goes. A stale figure is still
   marked stale, a fresh row still has its edge, a risen figure still shows its
   arrow and holds its colour instead of flashing and fading. */
@media (prefers-reduced-motion:reduce){
  .tick.up{color:var(--gain)}
  .tick.down{color:var(--loss)}
  .pending{animation:none;background:rgba(41,49,60,.5)}
  .attn{box-shadow:0 0 0 2px rgba(192,138,62,.45)}
  .live.link-retry i{background:var(--amber)}
}

/* ============================================== the curve is running, not drawn ==
   An equity chart is a picture of the past everywhere else. Here the newest
   reading is the only part that can still change, so the eye should go there:
   the trace draws itself once on arrival, and a lit head sits at the leading
   edge afterwards.

   The trace draw uses stroke-dasharray with the dash equal to the path length,
   so the line is "unwound" from nothing. `pathLength="1"` would be tidier and
   is not used: it needs the attribute on the element, and this path is built in
   Python where the length is not known. 4000 is comfortably longer than any
   curve this renders, and an over-long dash simply finishes early. */
.curve .trace{stroke-dasharray:4000;stroke-dashoffset:4000;
  animation:fx-trace 1600ms var(--ease) forwards}
@keyframes fx-trace{to{stroke-dashoffset:0}}

.curve .head{fill:var(--holo);stroke:var(--ink);stroke-width:1}
.curve .head-halo{fill:var(--holo);opacity:.18;
  animation:fx-head 2.4s ease-in-out infinite}
@keyframes fx-head{
  0%,100%{opacity:.10;transform:scale(.75)}
  50%{opacity:.30;transform:scale(1.25)}}
/* The halo scales about its own centre. Without this it grows from the SVG
   origin and slides across the chart, which looks like a bug rather than a
   pulse. */
.curve .head-halo{transform-origin:center;transform-box:fill-box}

/* ==================================================================== depth ==
   Three planes drifting by different amounts under the pointer. Two or three
   pixels each: enough that the deck stops reading as flat glass, nowhere near
   enough to notice as movement. The transform is set from JS on the layer
   itself, so a browser that never fires pointermove simply gets zero. */
.fx{will-change:transform}

/* ============================================================== the console ==
   Cmd+K. A ship's computer rather than a search box: it drops from the top,
   it is monospace, and it goes away the instant you stop needing it.

   Built by SCRIPT and absent from the markup, like the boot overlay, so a page
   without JavaScript cannot end up behind a panel it has no way to dismiss. */
.fx-console{position:fixed;inset:0;z-index:70;display:flex;
  justify-content:center;align-items:flex-start;padding-top:12vh;
  background:rgba(4,6,9,.55);backdrop-filter:blur(4px)}
.fx-console .box{width:min(100% - 3rem,34rem);background:rgba(15,19,25,.97);
  border:1px solid var(--slate);border-radius:2px;overflow:hidden;
  box-shadow:0 0 0 1px rgba(111,211,232,.08),0 40px 80px -40px rgba(0,0,0,1);
  animation:fx-console-in 220ms var(--ease)}
@keyframes fx-console-in{from{opacity:0;transform:translateY(-12px)}
  to{opacity:1;transform:none}}
.fx-console input{width:100%;background:transparent;border:0;
  border-bottom:1px solid var(--slate);color:var(--bone);
  font-family:var(--mono);font-size:1rem;padding:1rem 1.125rem;outline:none;
  letter-spacing:.04em}
.fx-console input::placeholder{color:var(--pewter)}
.fx-console ul{list-style:none;margin:0;padding:.375rem;max-height:46vh;
  overflow-y:auto}
.fx-console li{display:flex;gap:.75rem;align-items:baseline;
  padding:.5rem .75rem;border-radius:2px;cursor:pointer;font-size:.875rem}
.fx-console li .where{margin-left:auto;font-family:var(--mono);
  font-size:.625rem;letter-spacing:.12em;text-transform:uppercase;
  color:var(--pewter)}
.fx-console li[aria-selected=true]{background:rgba(111,211,232,.10)}
.fx-console li[aria-selected=true] .where{color:var(--holo)}
.fx-console .none{padding:1rem 1.125rem;color:var(--pewter);font-size:.875rem}
.fx-console .hint{padding:.5rem 1.125rem;border-top:1px solid var(--slate);
  font-family:var(--mono);font-size:.5625rem;letter-spacing:.12em;
  text-transform:uppercase;color:var(--pewter)}

/* ====================================================== tile becomes the page ==
   The View Transitions API animates between two DOM states the browser already
   knows how to render, so a tile can grow into the panel it opens rather than
   the page being replaced under you.

   Unsupported browsers get an ordinary navigation and lose nothing — the
   feature detect is in SCRIPT, and this block only styles the transition when
   one actually happens. */
@view-transition{navigation:auto}
::view-transition-old(root),::view-transition-new(root){
  animation-duration:280ms;animation-timing-function:cubic-bezier(.22,1,.36,1)}

@media (prefers-reduced-motion:reduce){
  .curve .trace{stroke-dasharray:none;stroke-dashoffset:0;animation:none}
  .curve .head-halo{animation:none;opacity:.2}
  .fx-console .box{animation:none}
  ::view-transition-old(root),::view-transition-new(root){animation:none}
}

/* ----------------------------------------------------------------- signin */
/* The sign-in screen, and the one the brief actually asked for: this is where
   the jump starts. It still reveals nothing about the account behind it — see
   the docstring on `login_page` — so everything dressed up here is chrome.

   Named `.signin` and NOT `.gate`, which it used to be. `.gate` is also the
   Decisions page's modifier for the risk-gate verdict row — `<div class="rung
   gate no">` — so a bare `.gate{min-height:calc(100svh - 8rem);place-items:
   center}` matched it too and stretched every verdict to most of a viewport
   with the rejection reason floating in the middle of the void. Valid CSS,
   silently styling the wrong element, invisible unless somebody looks at a
   page that HAS a decision on it — which no empty-journal render does. Exactly
   the `.pill`/`.seed` collision again, so the guard that caught that one now
   covers every modifier rather than only the badges. */
.signin{min-height:calc(100svh - 8rem);display:grid;place-items:center;padding:2rem 0}
.signin .panel{width:min(100% - 2rem,25rem);position:relative;
  border:1px solid var(--slate);border-radius:2px;padding:2rem 1.75rem;
  background:linear-gradient(180deg,rgba(22,27,34,.9),rgba(15,19,25,.94));
  backdrop-filter:blur(4px);
  box-shadow:0 0 0 1px rgba(111,211,232,.06),0 30px 70px -40px rgba(0,0,0,1)}
.signin .panel::before,.signin .panel::after{content:"";position:absolute;
  width:14px;height:14px;border:1px solid var(--holo);opacity:.6}
.signin .panel::before{top:-1px;left:-1px;border-right:0;border-bottom:0}
.signin .panel::after{bottom:-1px;right:-1px;border-left:0;border-top:0}
.signin .sig{text-align:center;margin-bottom:1.75rem}
.signin .sig svg{display:block;margin:0 auto .875rem}
.signin .sig svg path{fill:var(--bone)}
.signin .sig h1{font-size:1.25rem;letter-spacing:.16em;font-family:var(--serif)}
.signin .sig h1 span{color:var(--pewter)}
.signin .sig p{margin:.5rem 0 0;font-family:var(--mono);font-size:.625rem;
  letter-spacing:.2em;text-transform:uppercase;color:var(--holo);opacity:.8}
.signin label{display:block;margin-bottom:.5rem}
.signin input{width:100%;background:var(--ink);color:var(--bone);
  border:1px solid var(--slate);border-radius:2px;padding:.75rem .875rem;
  font-family:var(--mono);font-size:.9375rem;letter-spacing:.18em;
  transition:border-color .25s var(--ease),box-shadow .25s var(--ease)}
.signin input:focus{border-color:var(--holo);
  box-shadow:0 0 0 3px rgba(111,211,232,.12);outline:none}
.signin button{width:100%;margin-top:1rem;background:transparent;color:var(--bone);
  border:1px solid var(--patina);border-radius:2px;padding:.75rem;
  font-family:var(--mono);font-size:.6875rem;letter-spacing:.22em;
  text-transform:uppercase;cursor:pointer;position:relative;overflow:hidden;
  transition:background .25s var(--ease),border-color .25s var(--ease)}
.signin button:hover{background:rgba(78,140,125,.16);border-color:var(--holo)}
.signin .standby{margin:1.25rem 0 0;text-align:center;font-family:var(--mono);
  font-size:.625rem;letter-spacing:.16em;text-transform:uppercase;
  color:var(--pewter)}
.signin .standby i{display:inline-block;width:5px;height:5px;border-radius:50%;
  background:var(--holo);margin-right:.5rem;vertical-align:middle;
  animation:fx-pulse 2.8s ease-in-out infinite}
/* `p.err`, not `.err`: scoped by element role rather than by a word that is
   also a state modifier elsewhere. See the collision note above the section. */
.signin p.err{margin:0 0 1.25rem;padding:.625rem .75rem;font-size:.8125rem;
  color:var(--loss);border:1px solid rgba(192,112,123,.4);
  border-left-width:3px;border-radius:2px;background:rgba(192,112,123,.07)}

/* --------------------------------------------------------- the boot readout */
/* Built by SCRIPT, never present in the markup, so a page without JavaScript
   cannot end up behind an overlay it has no way to dismiss.

   Every line here names a piece of THIS INTERFACE, never a piece of the
   account. A boot screen reporting "RISK GATE ARMED" would be inventing a
   state it has not read, on a dashboard whose entire purpose is that figures
   are measured rather than plausible. The mode line is the one live fact on
   it, and it is passed in from the server. */
.fx-boot{position:fixed;inset:0;z-index:60;background:var(--ink);
  display:flex;align-items:center;justify-content:center;
  font-family:var(--mono);font-size:.8125rem;letter-spacing:.06em}
.fx-boot.done{animation:fx-boot-out 420ms var(--ease) forwards;pointer-events:none}
@keyframes fx-boot-out{to{opacity:0}}
.fx-boot .panel{width:min(100% - 3rem,26rem)}
.fx-boot .sig{font-family:var(--serif);font-size:1.125rem;letter-spacing:.16em;
  color:var(--bone);margin-bottom:1.25rem;text-align:center}
.fx-boot .sig span{color:var(--pewter)}
/* The operator's name is the one warm thing on an otherwise clinical readout,
   so it gets the accent rather than the muted treatment the wordmark's second
   half takes. */
.fx-boot .sig span.who{color:var(--holo);letter-spacing:.1em}
.fx-boot ul{list-style:none;margin:0;padding:0}
.fx-boot li{display:flex;gap:.75rem;align-items:baseline;color:var(--pewter);
  padding:.2rem 0;opacity:0}
.fx-boot li.on{animation:fx-line 260ms var(--ease) forwards}
@keyframes fx-line{from{opacity:0;transform:translate3d(-6px,0,0)}
  to{opacity:1;transform:none}}
.fx-boot li b{margin-left:auto;font-weight:400;color:var(--holo);font-size:.6875rem;
  letter-spacing:.14em}
.fx-boot .rule{height:1px;background:var(--slate);margin:1rem 0}
.fx-boot .rule i{display:block;height:1px;width:0;background:var(--holo);
  animation:fx-fill 1150ms var(--ease) forwards}
@keyframes fx-fill{to{width:100%}}

@media (max-width:760px){
  nav{margin-left:0;width:100%;order:3}
  /* MEASURED, not guessed: at a 390px viewport the eight nav links render 32px
     tall, against the 44px minimum a finger needs. They are the only way around
     the deck on a phone and they are the smallest targets on it.
     The fix is a minimum HEIGHT plus centring rather than more vertical padding,
     because padding on a wrapping flex row grows the header on every line the
     nav wraps to — and it is inside the 760px block, so the desktop bar keeps
     the compact 32px links it lays out in one row beside the wordmark. */
  nav a{min-height:44px;display:inline-flex;align-items:center}
  .live{margin-left:auto;padding-left:0;border-left:0}
  .scroll{border:0}
  /* A STATED LIMIT, because it is a real one and pretending otherwise is worse.
     `display:block` on a table removes the table, row and cell roles in every
     engine; the visually hidden `thead` takes the column headers out of the
     tree entirely; and `td[data-l]::before` puts the label back as generated
     content, which screen readers announce inconsistently and some do not
     announce at all. So under 760px these tables are, to a non-visual reader,
     an undifferentiated run of values — the card layout is bought with the
     semantics, on this breakpoint only.
     The known repair is to restate every role in the markup (`role="table"`,
     `rowgroup`, `row`, `columnheader`, `cell`) so `display` cannot strip them.
     It is deliberately NOT done here: redundant ARIA on a native table is an
     anti-pattern that is only correct because of what this block does, it is
     ~40 attributes carried on every desktop render to fix a phone, and the
     compatibility case for it is contested. Naming the trade is this
     codebase's answer to a gap it has decided not to close — the same answer
     `strategy.requires` and the weekday-shaped session guard already give.
     `scope=col` on every `<th>` and the named `role="region"` wrapper are the
     parts that are unambiguously right, and those ARE done. */
  table,thead,tbody,tr,td{display:block;width:100%}
  caption{display:block}
  thead{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
  tr.data{border:1px solid var(--slate);border-bottom:0;background:var(--graphite);
    margin-top:.875rem;padding:.4rem 0}
  tr.why{border:1px solid var(--slate);border-top:0;background:var(--graphite)}
  tr.data td,tr.why td{border-bottom:0;padding:.3rem .875rem;max-width:none;
    display:flex;gap:1rem;justify-content:space-between;align-items:baseline}
  tr.why td{display:block;padding-bottom:.8rem}
  td[data-l]::before{content:attr(data-l);font-family:var(--mono);font-size:.625rem;
    letter-spacing:.12em;text-transform:uppercase;color:var(--pewter);flex:none}
}
/* ----------------------------------------------------- reduced motion honoured */
/* Off, not slowed down. Someone who has asked their operating system for less
   motion has not asked for a gentler hyperspace jump, and vestibular triggers
   are exactly the full-screen, high-contrast, radially-moving things this
   stylesheet otherwise spends its time on.

   Belt and braces on purpose: SCRIPT reads the same query and never starts the
   canvas, never adds `fx-ready` and never builds the boot overlay, so the work
   is not done rather than done invisibly. This block is what catches the case
   where the preference is switched on after the page has loaded. */
@media (prefers-reduced-motion:reduce){
  *{animation:none!important;transition:none!important}
  #fx-stars,.fx-grid,.fx-scan,.fx-sweep,.fx-flash,.fx-boot{display:none!important}
  .fx-vig{opacity:.5}
  .fx-ready .fx-panel{opacity:1!important;clip-path:none!important;transform:none!important}
}
"""

# Generated rather than written out, so `KIND_HUES` is the only place a kind's
# colour is stated. A hand-maintained second list is how a kind gets added to
# the config and silently renders in the fallback grey.
STYLES += _kind_css()

# The confirmation window's styling, in its own file for the same reason its
# script is: it is one self-contained thing with its own argument about why it
# looks the way it does. Concatenated here so it lives under the same
# no-backslash rule as everything above — `tests/test_web.py` fails the build if
# a control character reaches `STYLES`, and that check has to see this too.
STYLES += FORGE_WINDOW_CSS

# The wisps and the fusion reveal, for the same reason and under the same rule.
# It is concatenated LAST of the three deliberately: `.fused` there sets the
# patina border on a fused card, and `article.fused` above wins that on
# specificity rather than on order — so this position is a convenience and not a
# thing any rule depends on.
STYLES += DREAM_FX_CSS

SCRIPT = """
/* The projection layer for the command centre: starfield, hyperspace jump,
   panel materialisation and the boot readout.

   ## The one rule this file follows

   It is decoration, and it is built so that it cannot stop being decoration.
   Every visible consequence of this script is additive: it creates its own
   layers, it adds its own classes, and if it throws on line one the dashboard
   behind it renders every figure exactly as before. Nothing here is asked to
   reveal content, because a reveal that fails leaves a blank page — and this
   is the surface an operator opens to find out whether anything is wrong.

   That is why the entry animations are gated on `html.fx-ready`, which is
   added HERE rather than served in the markup, and why there is a timer that
   force-finishes every panel whether or not the observer ever fired.

   ## Reduced motion

   Checked first and answered by doing nothing at all. No canvas, no observer,
   no overlay, no class. The stylesheet has a matching block, which catches the
   case where the preference changes after load, but the intent is that a
   machine asking for less motion never starts this work rather than doing it
   invisibly. A full-screen radial starfield accelerating to lightspeed is
   close to a worked example of a vestibular trigger.

   The Cmd+K console is the ONE exception and it lives in a second closure
   below, outside this bail-out. Everything in here is decoration and switching
   decoration off costs nothing; the palette is a keyboard route to every page,
   and withdrawing it would answer "I would rather things did not move" by
   removing a way of getting around. Its animation is in the stylesheet, which
   has a reduced-motion block of its own, so the preference is still honoured
   where it applies.
*/
(function () {
  'use strict';

  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)');
  if (reduced && reduced.matches) return;

  var doc = document.documentElement;
  var body = document.body;
  var store = {
    get: function (k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { sessionStorage.setItem(k, v); } catch (e) {} },
    drop: function (k) { try { sessionStorage.removeItem(k); } catch (e) {} }
  };

  /* --------------------------------------------------------- the layers */

  function layer(cls, id) {
    var el = document.createElement('div');
    el.className = cls ? 'fx ' + cls : 'fx';
    if (id) el.id = id;
    el.setAttribute('aria-hidden', 'true');
    body.appendChild(el);
    return el;
  }

  var starHost = layer('', 'fx-stars');
  layer('fx-grid');
  layer('fx-vig');
  layer('fx-scan');
  var sweep = layer('fx-sweep');
  var flash = layer('fx-flash');

  var canvas = document.createElement('canvas');
  starHost.appendChild(canvas);
  var ctx = canvas.getContext('2d');

  /* ------------------------------------------------------- the starfield */

  /* Classic perspective projection: a star is a point in a unit box with a
     depth, and the screen position is that point divided by the depth. Pulling
     the depth down moves it outward and accelerating that is the whole trick.
     Drawing from the PREVIOUS depth to the current one turns a dot into a
     streak for free, so idle drift and lightspeed are one number apart. */

  var IDLE = 0.0011;      /* ambient. Slow enough to read a table over */
  var WARP = 0.085;       /* lightspeed */
  var stars = [];
  var w = 0, h = 0, cx = 0, cy = 0, dpr = 1;
  var speed = IDLE, target = IDLE;
  var running = false, frame = 0;

  function size() {
    /* Capped at 2. A phone reporting 3 or 4 triples the fill cost of a
       full-screen canvas for a difference nobody can see on a starfield. */
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = window.innerWidth;
    h = window.innerHeight;
    cx = w / 2;
    cy = h / 2;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    /* Density by area rather than a fixed count, so a laptop and a phone get
       the same look and the phone does a fraction of the work. */
    var want = Math.max(90, Math.min(420, Math.round((w * h) / 5200)));
    while (stars.length > want) stars.pop();
    while (stars.length < want) stars.push(spawn(Math.random()));
  }

  function spawn(z) {
    return {
      x: (Math.random() - 0.5) * 2,
      y: (Math.random() - 0.5) * 2,
      z: z <= 0 ? 1 : z,
      pz: 0,
      /* A few bright ones. A uniform field reads as noise; a scattering of
         brighter stars gives the eye something to track through the jump. */
      m: Math.random() < 0.08 ? 1.9 : 1
    };
  }

  function tick() {
    frame = 0;
    if (!running) return;

    /* Ease toward the target rather than snapping, so the spool-up on sign-in
       and the drop-out on arrival are the same code as the idle drift. */
    speed += (target - speed) * 0.055;
    var warp = Math.max(0, Math.min(1, (speed - IDLE) / (WARP - IDLE)));

    /* Trails rather than a clear: the fade leaves the previous frame faintly
       behind, which is what makes streaks look like light and not like lines.
       Opaque enough at rest that stars do not smear across a still page. */
    ctx.fillStyle = 'rgba(11,14,18,' + (0.72 - warp * 0.42).toFixed(3) + ')';
    ctx.fillRect(0, 0, w, h);
    ctx.lineCap = 'round';

    for (var i = 0; i < stars.length; i++) {
      var s = stars[i];
      s.pz = s.z;
      s.z -= speed;
      if (s.z <= 0.02) { stars[i] = spawn(1); continue; }

      var k = Math.min(cx, cy) * 1.35;
      var x = cx + (s.x / s.z) * k;
      var y = cy + (s.y / s.z) * k;
      if (x < -60 || x > w + 60 || y < -60 || y > h + 60) { stars[i] = spawn(1); continue; }

      var px = cx + (s.x / s.pz) * k;
      var py = cy + (s.y / s.pz) * k;

      var depth = 1 - s.z;
      var alpha = Math.min(1, depth * depth * 1.5) * (0.35 + warp * 0.65) * s.m;
      var width = Math.max(0.5, depth * 1.7 * s.m);

      /* Toward white at speed, holo-tinted at rest. Blue-shift as a matter of
         taste rather than physics, but it is the taste the brief asked for. */
      ctx.strokeStyle = warp > 0.04
        ? 'rgba(' + Math.round(196 + warp * 59) + ',' +
                    Math.round(232 + warp * 23) + ',255,' + alpha.toFixed(3) + ')'
        : 'rgba(150,196,214,' + (alpha * 0.72).toFixed(3) + ')';
      ctx.lineWidth = width;
      ctx.beginPath();
      ctx.moveTo(px, py);
      /* At rest the streak is sub-pixel, so nudge it to a visible dot. */
      ctx.lineTo(x, y + (px === x && py === y ? 0.6 : 0));
      ctx.stroke();
    }

    frame = window.requestAnimationFrame(tick);
  }

  function start() {
    if (running || document.hidden) return;
    running = true;
    frame = window.requestAnimationFrame(tick);
  }

  function stop() {
    running = false;
    if (frame) window.cancelAnimationFrame(frame);
    frame = 0;
  }

  function warpTo(level) { target = IDLE + (WARP - IDLE) * level; }

  size();
  start();

  var resizeTimer = 0;
  window.addEventListener('resize', function () {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(size, 150);
  });
  /* A background tab burning a rAF loop on a starfield nobody is looking at is
     the cost this whole layer has to justify, so it does not pay it. */
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) stop(); else start();
  });

  /* ------------------------------------------------- panel materialisation */

  /* `.signin .panel` belongs here and was missing. It already carries the same
     bracket-corner rules as every other member of this set, so it was plainly
     meant to be one — and left out, it was the only content on any page that
     the boot overlay crossfaded ON TOP OF rather than into. The overlay is
     opaque, z-index 60, centred, and prints four status lines and a second
     wordmark straight across "OPERATOR SIGN-IN", the password label and the
     input for roughly 300ms. Self-resolving, and the first thing anyone sees
     on the one page that is publicly reachable once DASHBOARD_PASSWORD is set.

     Fail-to-visible is unaffected: hiding still needs BOTH `html.fx-ready` and
     the per-element `fx-panel` class, added together in one synchronous block,
     so a throw or a blocked script leaves a fully usable sign-in form. */
  var PANELS = '.page-head,.banner,.card,.curve,.scroll,.cycle,.readout,' +
               '.chat .log,.empty,section.block > h2,.signin .panel';

  function finish(el) {
    el.classList.remove('fx-in');
    el.classList.add('fx-done');
  }

  /* Queries the document rather than trusting a list built earlier. This is
     the function that guarantees no figure can be left hidden, so it must not
     depend on any of the code between here and the failure it is catching. */
  function settleAll() {
    var all = document.querySelectorAll('.fx-panel');
    for (var i = 0; i < all.length; i++) finish(all[i]);
  }

  /* Armed before anything else can throw. Hiding a panel takes BOTH
     `html.fx-ready` and a per-element `fx-panel` class, added together in one
     synchronous block below, and this clears them shortly afterwards whatever
     happened in between.

     It is re-armed rather than cancelled once setup succeeds, and the two
     durations do different jobs. The short one catches a throw between hiding
     and playing, which is the case that would leave a figure invisible with
     nothing coming to fix it. The long one is the last resort for panels the
     observer owns: settling those after two and a half seconds would force
     everything below the fold visible before anyone scrolled to it, so the
     reveal-on-scroll could never happen at all. */
  var guard = window.setTimeout(settleAll, 2600);

  function rearm(ms) {
    window.clearTimeout(guard);
    guard = window.setTimeout(settleAll, ms);
  }

  function play(list) {
    for (var i = 0; i < list.length; i++) {
      var el = list[i];
      if (el.classList.contains('fx-in') || el.classList.contains('fx-done')) continue;
      /* Capped stagger. A Decisions page of sixty cycles would otherwise take
         most of a minute to finish arriving. */
      el.style.setProperty('--fx-d', Math.min(i * 45, 420) + 'ms');
      el.classList.add('fx-in');
      el.addEventListener('animationend', function (e) {
        if (e.animationName === 'fx-rise') finish(e.currentTarget);
      }, { once: true });
    }
  }

  function materialise() {
    var panels = Array.prototype.slice.call(document.querySelectorAll(PANELS));
    if (!panels.length) return;

    /* The whole hiding step, in one synchronous block: mark the elements, set
       the root flag, and hand every one of them to something that will show it
       again. Nothing may be added between these lines that can throw. */
    var above = [], below = [];
    for (var i = 0; i < panels.length; i++) {
      panels[i].classList.add('fx-panel');
      (panels[i].getBoundingClientRect().top < window.innerHeight + 80 ? above : below)
        .push(panels[i]);
    }
    doc.classList.add('fx-ready');
    play(above);

    if (!below.length) { rearm(3000); return; }
    if ('IntersectionObserver' in window) {
      var io = new IntersectionObserver(function (entries) {
        var batch = [];
        for (var j = 0; j < entries.length; j++) {
          if (entries[j].isIntersecting) {
            batch.push(entries[j].target);
            io.unobserve(entries[j].target);
          }
        }
        if (batch.length) play(batch);
      }, { rootMargin: '0px 0px -8% 0px' });
      for (var k = 0; k < below.length; k++) io.observe(below[k]);
      /* Setup succeeded, so the observer owns these. Push the backstop out far
         enough that scrolling to a panel is what reveals it, and never remove
         it: a page left open still ends up wholly visible. */
      rearm(20000);
    } else {
      play(below);
      rearm(3000);
    }
  }

  /* ------------------------------------------------------------ the jump */

  var jumping = false;

  function jump(href) {
    if (jumping) return;
    jumping = true;
    warpTo(1);
    flash.classList.add('out');
    store.set('mudhorn.jump', '1');
    window.setTimeout(function () { window.location.href = href; }, 500);
    /* If the navigation never happens — a blocked scheme, a download, a
       back-forward cache restore — the deck comes back rather than sitting
       behind a white screen at lightspeed forever. */
    window.setTimeout(function () {
      jumping = false;
      warpTo(0);
      flash.classList.remove('out');
    }, 2600);
  }

  function arrive() {
    var jumped = store.get('mudhorn.jump') === '1';
    var landed = store.get('mudhorn.spool') === '1' &&
                 window.location.pathname.indexOf('/login') !== 0;
    store.drop('mudhorn.jump');
    store.drop('mudhorn.spool');

    if (jumped || landed) {
      /* Drop out of lightspeed rather than easing into it: the streaks are
         already long on the first frame and shorten, which is the arrival half
         of the jump the previous page started. */
      speed = WARP;
      flash.classList.add('in');
      sweep.classList.add('run');
    }
    warpTo(0);
  }

  /* Same-origin link clicks become jumps. Everything a browser can do with a
     link other than follow it here is left alone: a new tab, a download, a
     modified click and an external host all behave exactly as they would
     without this file loaded. */
  document.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey ||
        e.shiftKey || e.altKey) return;
    var a = e.target.closest ? e.target.closest('a') : null;
    if (!a || !a.href || a.target === '_blank' || a.hasAttribute('download')) return;
    if (a.origin !== window.location.origin) return;
    if (a.getAttribute('href').charAt(0) === '#') return;
    if (a.pathname === window.location.pathname && a.search === window.location.search) return;
    e.preventDefault();
    jump(a.href);
  });

  /* Sign-in is the one the brief actually asked for, so the stars spool up the
     moment the form is submitted and the Board completes the jump on arrival.

     The submit is NOT intercepted. Holding a login behind an animation means a
     script error locks the operator out of their own dashboard, and a wrong
     password would have played a triumphant arrival on the way back to the
     same form. The flag is read on the NEXT page instead: still on /login
     means it failed and the arrival is dropped. */
  var form = document.querySelector('form[action="/login"]');
  if (form) {
    form.addEventListener('submit', function () {
      store.set('mudhorn.spool', '1');
      warpTo(0.62);
      flash.classList.add('out');
    });
  }

  /* -------------------------------------------------------- boot readout */

  /* Once per browser session. Every line names a piece of THIS INTERFACE. Not
     one of them reports on the account, the gate or the journal, because a
     boot screen announcing "RISK GATE ARMED" would be stating a fact it has
     not read — on a dashboard whose entire argument is that figures are
     measured rather than plausible. The execution mode is the single live
     value here and it is rendered by the server into a data attribute. */
  /* The name reaches innerHTML, so it is escaped at the point of use rather
     than trusted because the server escaped it into the attribute. Two cheap
     escapes beat one assumption about who wrote the DOM. */
  function esc(t) {
    var d = document.createElement('div');
    d.textContent = t;
    return d.innerHTML;
  }

  function boot(then) {
    if (store.get('mudhorn.booted') === '1') { then(); return; }
    store.set('mudhorn.booted', '1');

    var mode = body.getAttribute('data-mode') || 'PAPER';
    var who = (body.getAttribute('data-operator') || '').trim();
    var lines = [
      ['Nav computer', 'READY'],
      ['Deck projection', 'ONLINE'],
      ['Render pipeline', 'LOCKED'],
      ['Execution mode', mode.toUpperCase()]
    ];

    var el = document.createElement('div');
    el.className = 'fx-boot';
    el.setAttribute('aria-hidden', 'true');
    var items = '';
    for (var i = 0; i < lines.length; i++) {
      items += '<li><span>' + lines[i][0] + '</span><b>' + lines[i][1] + '</b></li>';
    }
    /* The welcome is the one personal line here, and it can only ever be
       built behind the login: the sign-in page is rendered without a
       data-operator attribute at all. See the note on `Env.operator_name`. */
    var sig = who
      ? '<div class="sig">Welcome back, <span class="who">' + esc(who) + '</span></div>'
      : '<div class="sig">MUDHORN <span>CAPITAL</span></div>';
    el.innerHTML = '<div class="panel">' + sig +
                   '<div class="rule"><i></i></div><ul>' + items + '</ul></div>';
    body.appendChild(el);
    warpTo(0.25);

    var li = el.querySelectorAll('li');
    for (var j = 0; j < li.length; j++) {
      (function (node, n) {
        window.setTimeout(function () { node.classList.add('on'); }, 260 + n * 190);
      })(li[j], j);
    }

    var closed = false;
    function close() {
      if (closed) return;
      closed = true;
      el.classList.add('done');
      speed = WARP * 0.8;
      flash.classList.add('in');
      sweep.classList.add('run');
      window.setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 460);
      then();
    }
    /* Skippable. An operator checking open risk for the fourth time today does
       not want to watch a title sequence, and the flag above means they only
       see it once a session anyway. */
    el.addEventListener('click', close);
    document.addEventListener('keydown', close, { once: true });
    window.setTimeout(close, 1500);
  }

  function go() {
    boot(function () {
      arrive();
      materialise();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', go);
  } else {
    go();
  }

  /* ------------------------------------------------------- the live stream */

  /* Paints the account stream onto figures the server ALREADY rendered.

     That distinction is the whole safety argument, and it is the same rule the
     projection layer follows: this may only ever UPDATE a number, never be what
     reveals one. Every value is in the markup before this runs, so a browser
     with the script blocked shows the reading it was served — one page-load
     old, and honest about it — rather than an empty box.

     It is also entirely optional. No EventSource, a refused connection, a proxy
     that eats the stream: the page keeps the figures it was rendered with and
     the indicator says the link is down. Nothing here can empty the deck. */

  var link = document.getElementById('link');
  var lastValues = {};
  /* One-shot. The cold-start Board reloads once when the first reading lands,
     and several stream messages can arrive inside the delay before it does. */
  var coldReloading = false;

  function setLink(state, label) {
    if (!link) return;
    link.classList.remove('link-live', 'link-retry', 'link-down', 'link-out');
    link.classList.add('link-' + state);
    var tag = link.querySelector('.link-label');
    if (tag) tag.textContent = label || '';
  }

  function money(v) {
    /* Matches `render._money` deliberately. Two formatters for the same figure
       would eventually disagree, and the one that drifted would be the one
       nobody was looking at. */
    var sign = v < 0 ? '-' : '';
    return sign + '$' + Math.abs(v).toLocaleString('en-GB', {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    });
  }

  function paintFigure(el, key, value, signed) {
    if (value === null || value === undefined) return;
    var text = signed && value > 0 ? '+' + money(value) : money(value);
    if (el.textContent === text) return;

    var before = lastValues[key];
    el.textContent = text;
    lastValues[key] = value;
    if (before === undefined || before === value) return;

    /* The direction comes from the SIGN OF THE DELTA, never from the fact that
       something changed. A green flash on a falling number would be worse than
       no flash at all. */
    var dir = value > before ? 'up' : 'down';
    el.classList.remove('up', 'down');
    /* Reflow between removing and adding, or the animation does not restart
       when a figure moves the same way twice. */
    void el.offsetWidth;
    el.classList.add('tick', dir);
  }

  var SIGNED = { unrealised_pnl_usd: true, realised_pnl_today_usd: true };

  /* The stamp under the page title describes the READING, so the stream owns
     it the same way it owns the figures. Left alone it would keep saying what
     the server said at render time — which is right until the reading moves
     under it, and after that it is a timestamp attached to figures it no
     longer describes, standing for as long as the tab is open. */
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  function pad(n) { return n < 10 ? '0' + n : '' + n; }

  function whenUTC(d) {
    /* Matches `render._when` character for character, for the same reason
       `money` matches `_money`: the same instant formatted two ways on one
       page eventually disagrees, and it is the copy nobody watches that
       drifts. */
    return pad(d.getUTCDate()) + ' ' + MONTHS[d.getUTCMonth()] + ' ' +
      d.getUTCFullYear() + ', ' + pad(d.getUTCHours()) + ':' +
      pad(d.getUTCMinutes()) + ' UTC';
  }

  function paintStamp(data) {
    var el = document.querySelector('[data-live-read]');
    if (!el) return;
    if (!data.as_of) { el.textContent = 'read time unknown'; return; }
    var when = new Date(data.as_of);
    if (isNaN(when.getTime())) return;
    el.textContent = data.stale
      ? 'last read ' + whenUTC(when) + ' — not refreshed since'
      : 'read ' + whenUTC(when);
    el.classList.toggle('stale', !!data.stale);
  }

  /* The ticker tape, repainted from the same stream as the account.

     Every cell appears TWICE — the track emits the run twice so the marquee
     loops seamlessly — so this paints all matches for a symbol rather than the
     first. Painting one would leave the duplicate showing the opening price
     and the strip would contradict itself once a minute as it scrolled past.

     `lastTick` holds the previous price per symbol so the pulse fires on a
     CHANGE rather than on every frame. The stream re-sends the current state
     on a heartbeat, so keying the animation off arrival would strobe the whole
     strip every fifteen seconds with nothing having moved. */
  var lastTick = {};

  /* The tape's own read stamp, which is NOT the account's.

     `as_of` above dates the five-second account poll; this dates the sixty-
     second tape read, and the whole reason the strip can show a price a cent
     away from the positions table is that the two moments are different. A
     stamp left at what the server said would go on describing a reading the
     stream has already replaced — the same failure `paintStamp` exists to
     prevent, on the figure most likely to be behind. */
  function paintTapeStamp(data) {
    var el = document.querySelector('[data-tape-read]');
    if (!el) return;
    if (!data.ticker_as_of) { el.textContent = 'mid, read time unknown'; return; }
    var when = new Date(data.ticker_as_of);
    if (isNaN(when.getTime())) return;
    /* Matches `render._tape_read` exactly, for the reason `money`, `whenUTC`
       and `tapePrice` all match their server halves: one figure formatted two
       ways eventually disagrees, and here the two would alternate on screen
       every few seconds. */
    el.textContent = 'mid, read ' + pad(when.getUTCHours()) + ':' +
      pad(when.getUTCMinutes()) + ' UTC';
  }

  function tapePrice(v) {
    /* Matches `render._tape_price`. Third formatter, same rule as the other
       two: one figure formatted two ways eventually disagrees. */
    return v < 10000
      ? v.toLocaleString('en-GB', {minimumFractionDigits: 2, maximumFractionDigits: 2})
      : v.toLocaleString('en-GB', {maximumFractionDigits: 0});
  }

  function paintTape(data) {
    if (!data || !data.ticker || !data.ticker.length) return;
    for (var i = 0; i < data.ticker.length; i++) {
      var row = data.ticker[i];
      var cells = document.querySelectorAll('[data-tick="' + row.symbol + '"]');
      if (!cells.length) continue;

      var before = lastTick[row.symbol];
      var moved = row.last !== null && before !== undefined && row.last !== before;
      if (row.last !== null) lastTick[row.symbol] = row.last;

      for (var c = 0; c < cells.length; c++) {
        var cell = cells[c];
        var px = cell.querySelector('[data-tick-px]');
        var mv = cell.querySelector('[data-tick-mv]');
        /* A cell the server rendered as "no quote" has no price element to
           paint into. Left alone rather than rebuilt: this layer may UPDATE a
           figure the server rendered and must never be what reveals one. */
        if (px && row.last !== null) px.textContent = tapePrice(row.last);
        if (mv && row.change_pct !== null && row.change_pct !== undefined) {
          mv.textContent = (row.change_pct >= 0 ? '▲' : '▼') +
            Math.abs(row.change_pct).toFixed(2) + '%';
          cell.classList.remove('up', 'down');
          cell.classList.add(row.change_pct >= 0 ? 'up' : 'down');
          var mag = Math.min(Math.abs(row.change_pct) / 3, 1);
          cell.style.setProperty('--mag', mag.toFixed(3));
          /* The server withholds the rail where there was no move to measure.
             A percentage has just arrived, so there is one — and leaving the
             class on would show a figure with its gauge suppressed, which is
             the mirror of the stub the class exists to prevent. */
          cell.classList.remove('norail');
        }
        if (!moved) continue;
        /* Direction from the SIGN OF THE DELTA, never from the day's change:
           a stock down 2% on the day that just ticked up has moved up, and
           flashing that red would say the opposite of what happened. */
        var dir = row.last > before ? 'pulse-up' : 'pulse-down';
        (function (el, cls) {
          el.classList.remove('pulse-up', 'pulse-down');
          void el.offsetWidth;          /* restart the animation */
          el.classList.add(cls);
          window.setTimeout(function () { el.classList.remove(cls); }, 950);
        })(cell, dir);
      }
    }
  }

  function paint(data) {
    if (!data) return;

    /* Four states, and they must not collapse into two. `slow` used to fall
       through to the `else` and paint green, which said "live" while a read
       was outstanding and the figures on screen were the previous ones —
       precisely the reading the state exists to distinguish. */
    if (data.status === 'failing') {
      setLink('retry', 'broker');
    } else if (data.status === 'slow') {
      setLink('retry', 'broker slow');
    } else if (data.stale) {
      setLink('retry', 'stale');
    } else if (data.status === 'starting') {
      setLink('retry', 'reading');
    } else {
      setLink('live', '');
    }

    paintStamp(data);
    paintTapeStamp(data);
    paintTape(data);

    var account = data.account;
    if (!account) return;

    /* Two server-rendered claims about the reading, retracted the moment the
       reading they describe is superseded. Both ids are `render.py` constants
       repeated here as literals — see the note beside them; a test pins the
       copies together.

       This is not the client revealing a figure. Both banners are statements
       the SERVER made about its own freshness, and each is provably false once
       a fresh non-stale reading has landed. Leaving them up is how a page ends
       up asserting two contradictory things at once: "these figures are not
       current" printed above four figures repainting every five seconds. */
    if (!data.stale) {
      var staleBanner = document.getElementById('reading-stale');
      if (staleBanner) staleBanner.remove();
    }

    /* The cold-start Board is the harder half, because the missing part is not
       a banner — it is every SECTION. `_board_waiting` renders four tiles and
       nothing else, and the stream can only repaint what is already in the
       markup, so the positions, the resting orders and the risk meters can
       never arrive. One reload, once, on the first reading that carries an
       account.

       It cannot loop: the server builds the Board from the same poller
       snapshot this payload came from, so a reading good enough to trigger the
       reload is a reading good enough for the full page, and the reloaded page
       carries no cold-start banner to trigger it again. `coldReloading` guards
       the several messages that may arrive inside the delay. */
    var cold = document.getElementById('cold-start');
    if (cold && !coldReloading) {
      coldReloading = true;
      cold.remove();
      window.setTimeout(function () { window.location.reload(); }, 400);
    }

    var targets = document.querySelectorAll('[data-live]');
    for (var i = 0; i < targets.length; i++) {
      var el = targets[i];
      var key = el.getAttribute('data-live');
      if (!(key in account)) continue;
      /* The first frame replaces the waiting shimmer. After that it is a
         straight text swap on a figure that was already there. */
      var shimmer = el.querySelector('.pending');
      if (shimmer) el.textContent = '';
      paintFigure(el, key, account[key], !!SIGNED[key]);
    }

    /* A reading older than the stream thinks is reasonable is marked on the
       figures themselves, not only on the indicator: a reader looking at a
       number should be told about THAT number. */
    for (var j = 0; j < targets.length; j++) {
      targets[j].classList.toggle('stale', !!data.stale);
    }
  }

  function connect() {
    /* Only open the stream on a page that consumes it.
     *
     * Without this the SIGN-IN page opened it too — inheriting this script —
     * and got a 401, because `/live` is behind the same password as the pages
     * that render an account. Confirmed in a browser: one request, one 401,
     * one console error on every view of the login form.
     *
     * It also stops Decisions, Trades, Settings and Chat opening a stream they
     * have no figures for. Today only the Board carries `data-live` targets, so
     * today only the Board subscribes — and since the poller starts on the
     * first subscription and idle-stops after the last, a session that never
     * opens the Board never talks to the broker at all.
     */
    if (!document.querySelector('[data-live]')) return;
    if (!window.EventSource) { setLink('down', 'no stream'); return; }
    var es;
    try {
      es = new EventSource('/live');
    } catch (e) {
      setLink('down', 'no stream');
      return;
    }
    es.onmessage = function (e) {
      try { paint(JSON.parse(e.data)); } catch (err) { /* a torn frame is not worth a page */ }
    };
    /* EventSource reconnects by itself, so this reports rather than retries.
       Writing our own backoff on top would fight the browser's.

       But it does not always reconnect, and the difference matters more than
       it looks. Per the spec a transport failure is "reestablish", leaving
       readyState at CONNECTING; a response that is not 200 text/event-stream
       is "fail the connection" — one error event, readyState CLOSED, and no
       further attempt ever. A lapsed session takes the second path, because
       `/live` answers 401 to a non-HTML Accept, which is correct for an API
       caller and invisible from here: EventSource exposes no status.

       So a signed-out Board sat on amber RECONNECTING indefinitely with the
       last-good figures still on screen and nothing saying the operator was
       signed out. Waiting was the one thing that could not help.

       CLOSED tells us it is permanent. `/session` tells us whether it is
       permanent because of authentication — it is behind the same gate, so it
       answers 401 in exactly the case that matters and 200 otherwise. Any
       other outcome, including the fetch itself failing, stays on the weaker
       claim: reporting "signed out" at somebody whose Wi-Fi dropped would send
       them to re-enter a password they never lost. */
    es.onerror = function () {
      if (es.readyState !== 2) { setLink('retry', 'reconnecting'); return; }
      setLink('retry', 'checking');
      if (!window.fetch) { setLink('down', 'stream closed'); return; }
      window.fetch('/session', {
        credentials: 'same-origin',
        headers: { 'Accept': 'application/json' }
      }).then(function (r) {
        if (r.status === 401) setLink('out', 'signed out - reload to sign in');
        else setLink('down', 'stream closed - reload');
      }).catch(function () { setLink('down', 'stream closed'); });
    };
    es.onopen = function () { setLink('live', ''); };
  }

  connect();

  /* ------------------------------------------------------------------ depth */

  /* Three planes drifting by different amounts under the pointer. The whole
     effect is six pixels of travel at the extremes; the point is not that it
     is seen, but that the deck stops reading as flat glass.

     Pointer only. A touch screen has no hover, so a finger dragging the page
     would shove the background around while the content scrolls, which is
     worse than nothing. `pointermove` with `pointerType` checked keeps it to
     a mouse or a trackpad.

     Written on an animation frame rather than on every event: a pointer fires
     far faster than the screen refreshes, and a transform per event is work
     thrown away before it is ever painted. */
  var planes = [
    { el: starHost, depth: 1.0 },
    { el: document.querySelector('.fx-grid'), depth: 2.6 },
    { el: document.querySelector('.fx-vig'), depth: 0.6 }
  ];
  var px = 0, py = 0, parallaxQueued = false;

  function applyParallax() {
    parallaxQueued = false;
    for (var i = 0; i < planes.length; i++) {
      var plane = planes[i];
      if (!plane.el) continue;
      plane.el.style.transform =
        'translate3d(' + (px * plane.depth).toFixed(2) + 'px,' +
        (py * plane.depth).toFixed(2) + 'px,0)';
    }
  }

  window.addEventListener('pointermove', function (e) {
    if (e.pointerType === 'touch') return;
    px = (e.clientX / window.innerWidth - 0.5) * -6;
    py = (e.clientY / window.innerHeight - 0.5) * -6;
    if (parallaxQueued) return;
    parallaxQueued = true;
    window.requestAnimationFrame(applyParallax);
  }, { passive: true });

  var api = window.MUDHORN_FX || (window.MUDHORN_FX = {});
  api.jump = jump;
  api.warpTo = warpTo;
  api.settle = settleAll;
})();


/* -------------------------------------------------------------------- console */

/* Cmd+K. Everything it can reach is a link that already exists in the nav, so
   it is a faster route to the same places rather than a second navigation model
   that could drift from the first.

   This is its OWN closure, and it sits deliberately OUTSIDE the reduced-motion
   bail-out above. The palette is navigation, not decoration: a machine asking
   for less motion is asking for fewer moving pixels, not for a keyboard route
   around the deck to be taken away. What it animates it animates through the
   stylesheet, which carries its own reduced-motion block, so the preference is
   honoured at the layer where it means something.

   It is independent of the projection layer in the other direction too. It
   reaches hyperspace through `window.MUDHORN_FX` if that layer built one and
   falls back to an ordinary navigation if it did not, so a throw up there costs
   the starfield rather than the way around the site. Same principle as the
   settle timer: the recovery path must not depend on the code it is recovering
   from. */
(function () {
  'use strict';

  var consoleEl = null;
  var consoleIndex = 0;
  var consoleMatches = [];
  var lastFocus = null;

  function go(href) {
    var fx = window.MUDHORN_FX;
    if (fx && fx.jump) { fx.jump(href); return; }
    window.location.href = href;
  }

  function destinations() {
    var out = [];
    var links = document.querySelectorAll('nav a');
    for (var i = 0; i < links.length; i++) {
      out.push({ label: links[i].textContent.trim(), href: links[i].href, where: 'page' });
    }
    return out;
  }

  function closeConsole() {
    if (!consoleEl) return;
    consoleEl.remove();
    consoleEl = null;
    restoreFocus();
  }

  /* Focus goes back where it came from. A palette that dismisses and leaves
     focus on the body strands a keyboard user at the top of the document with
     the whole page to tab back through.

     "Where it came from" is frequently NOWHERE, though, and that case is the
     one worth writing down: the shortcut is global, so it is usually pressed
     with nothing focused at all and `activeElement` is the body. Calling
     `.focus()` on the body silently does nothing — it is not focusable, no
     error is raised, and the strand happens anyway. So the return is CHECKED
     rather than assumed, and the fallback puts focus on the main region the
     way a skip link would.

     `preventScroll` on both calls, and it is not a nicety. Focusing an element
     scrolls it into view by default, and the element focus came FROM is
     frequently not the element the reader is looking at — the shortcut is
     global, so the fallback is `main`, and dismissing the palette from
     anywhere on the Board jumped the page 108px and pushed the header and the
     tape off screen. With a table focused first it jumped 872px, to the bottom
     of the document. Measured in Chromium both times.

     Closing a palette is not a request to go anywhere. */
  function restoreFocus() {
    if (lastFocus && lastFocus.focus && lastFocus !== document.body
        && document.contains(lastFocus)) {
      lastFocus.focus({ preventScroll: true });
      if (document.activeElement === lastFocus) return;
    }
    var main = document.querySelector('main');
    if (!main) return;
    main.setAttribute('tabindex', '-1');
    main.focus({ preventScroll: true });
  }

  function renderMatches(list, box) {
    var ul = box.querySelector('ul');
    ul.innerHTML = '';
    if (!list.length) {
      var none = document.createElement('li');
      none.className = 'none';
      none.textContent = 'Nothing matches.';
      ul.appendChild(none);
      return;
    }
    for (var i = 0; i < list.length; i++) {
      var li = document.createElement('li');
      li.setAttribute('role', 'option');
      li.setAttribute('aria-selected', String(i === consoleIndex));
      var name = document.createElement('span');
      name.textContent = list[i].label;
      var where = document.createElement('span');
      where.className = 'where';
      where.textContent = list[i].where;
      li.appendChild(name);
      li.appendChild(where);
      (function (item) {
        li.addEventListener('click', function () { closeConsole(); go(item.href); });
      })(list[i]);
      ul.appendChild(li);
    }
  }

  function openConsole() {
    if (consoleEl) return;
    /* Nowhere to go means no palette. SCRIPT is inlined into the sign-in page
       as well, which has no nav, and an overlay reading "Nothing matches." on
       the one page that is meant to say nothing at all is worse than the
       shortcut appearing not to work. */
    var all = destinations();
    if (!all.length) return;
    lastFocus = document.activeElement;
    consoleEl = document.createElement('div');
    consoleEl.className = 'fx-console';
    /* The field is built with createElement rather than written as markup, and
       that is not a style preference. SCRIPT is inlined into every page
       including Settings, and `tests/test_web.py` greps that page for an
       input tag to prove the limits cannot be edited from a browser. (This
       comment cannot spell the tag either, for the same reason — the grep
       reads the whole rendered page, comments included.)
       A search box in a command palette is not a settings control, but the
       guard cannot tell the difference by reading source — so the literal
       simply does not appear, and the guard keeps its teeth. */
    var box = document.createElement('div');
    box.className = 'box';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    box.setAttribute('aria-label', 'Command console');

    var input = document.createElement('input');
    input.type = 'text';
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-expanded', 'true');
    input.setAttribute('aria-controls', 'fx-console-list');
    input.placeholder = 'Where to?';

    var list = document.createElement('ul');
    list.id = 'fx-console-list';
    list.setAttribute('role', 'listbox');

    var hint = document.createElement('div');
    hint.className = 'hint';
    hint.textContent = 'Enter to go / Esc to close';

    box.appendChild(input);
    box.appendChild(list);
    box.appendChild(hint);
    consoleEl.appendChild(box);
    document.body.appendChild(consoleEl);
    consoleIndex = 0;
    consoleMatches = all;
    renderMatches(all, box);
    input.focus();

    input.addEventListener('input', function () {
      var q = input.value.trim().toLowerCase();
      consoleMatches = q
        ? all.filter(function (d) { return d.label.toLowerCase().indexOf(q) !== -1; })
        : all;
      consoleIndex = 0;
      renderMatches(consoleMatches, box);
    });

    consoleEl.addEventListener('click', function (e) {
      if (e.target === consoleEl) closeConsole();
    });

    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { e.preventDefault(); closeConsole(); return; }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (!consoleMatches.length) return;
        consoleIndex += e.key === 'ArrowDown' ? 1 : -1;
        if (consoleIndex < 0) consoleIndex = consoleMatches.length - 1;
        if (consoleIndex >= consoleMatches.length) consoleIndex = 0;
        renderMatches(consoleMatches, box);
        return;
      }
      if (e.key === 'Enter') {
        e.preventDefault();
        var pick = consoleMatches[consoleIndex];
        if (pick) { closeConsole(); go(pick.href); }
      }
    });
  }

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'k' && e.key !== 'K') return;
    if (!(e.metaKey || e.ctrlKey)) return;
    e.preventDefault();
    if (consoleEl) closeConsole(); else openConsole();
  });

  var fx = window.MUDHORN_FX || (window.MUDHORN_FX = {});
  fx.console = openConsole;
})();


/* --------------------------------------------------------------------- clocks */

/* Four cities and a session phase, ticking.

   Its OWN closure, outside the reduced-motion bail-out, for the same reason as
   the console: this is information, not decoration. Somebody asking for less
   motion still needs to know what time it is in New York. The one genuinely
   decorative part — the spin-to-blur on a phase change — is a CSS animation,
   and the stylesheet's reduced-motion block switches that off while the digits
   keep updating.

   ## The honesty problem, which a clock has and a figure does not

   Every other value on this page is a READING: taken at a moment, true of that
   moment, and honestly labelled with when. A clock is not like that. A clock
   showing 08:51 at 09:30 is not an old reading, it is a wrong one, and it is
   wrong in the most plausible possible way — it looks exactly like a right one.

   So the server renders the time it computed AND each face says "at page load"
   underneath. This script's first act is to remove that label, before it starts
   ticking. Script blocked, script threw, EventSource-less browser: the label
   stays and the reader is told the clock has stopped. That is the same rule as
   the Board's `taken_at` stamp arriving somewhere the word "reading" does not
   obviously apply. */
(function () {
  'use strict';

  var bar = document.querySelector('.tape');
  if (!bar) return;

  var faces = Array.prototype.slice.call(bar.querySelectorAll('.clk'));

  /* The label the server rendered saying the clock is not ticking. Taking it
     down is this script's proof of life, and it happens BEFORE the first tick
     so a throw below cannot leave a live-looking clock that never moves. */
  var stale = bar.querySelector('.frozen');
  if (stale && stale.parentNode) stale.parentNode.removeChild(stale);

  function two(n) { return n < 10 ? '0' + n : '' + n; }

  function paintFace(face, now) {
    var tz = face.getAttribute('data-tz');
    var out = face.querySelector('.t');
    if (!tz || !out) return;
    /* Formatted through Intl with the zone name rather than by adding a fixed
       offset. A page left open across a daylight-saving change is right
       afterwards instead of an hour out until somebody reloads — and this deck
       is meant to be left open. */
    var parts;
    try {
      parts = new Intl.DateTimeFormat('en-GB', {
        timeZone: tz, hour12: false,
        hour: '2-digit', minute: '2-digit', second: '2-digit'
      }).formatToParts(now);
    } catch (e) { return; }

    var got = {};
    for (var i = 0; i < parts.length; i++) got[parts[i].type] = parts[i].value;
    /* Intl renders midnight as 24 in some engines. 00 is what a clock says. */
    var hh = got.hour === '24' ? '00' : got.hour;
    out.textContent = hh + ':' + got.minute + ':' + got.second;
  }

  function countdown(ms) {
    if (!(ms > 0)) return 'any moment';
    var mins = Math.floor(ms / 60000);
    var hours = Math.floor(mins / 60);
    if (hours >= 24) return Math.floor(hours / 24) + 'd ' + (hours % 24) + 'h';
    if (hours) return hours + 'h ' + two(mins % 60) + 'm';
    if (mins) return mins + 'm';
    return 'under a minute';
  }

  /* The countdown beside the clocks, and the reason it needs a label map.
     `data-next-phase` carries the ENUM VALUE — 'pre', 'post' — and the server
     renders the human label. Formatting it here as the raw value would have the
     line change from "NYSE after hours in 3h 12m" to "post in 3h 11m" one
     second after load, which is the `_gmt_offset` lesson: two halves of one
     figure formatted two ways eventually disagree, and here they disagree
     visibly and immediately. */
  var PHASE_WORDS = {
    weekend: 'weekend', overnight: 'overnight', pre: 'pre-market',
    open: 'open', post: 'after hours'
  };
  function phaseWord(v) { return PHASE_WORDS[v] || v; }

  var until = bar.querySelector('.until');
  var turned = 0;

  function tick() {
    var now = new Date();
    for (var i = 0; i < faces.length; i++) paintFace(faces[i], now);
    if (!until) return;

    /* Re-read rather than cached at startup. The attribute is what the SERVER
       said, and the server is the authority on where the boundary is — this
       loop only counts down to it. A value parsed once at page load means a
       boundary that changes underneath is silently ignored, which is the
       client quietly disagreeing with the server about the session. */
    var changeAt = Date.parse(bar.getAttribute('data-change-at') || '');
    var nextPhase = bar.getAttribute('data-next-phase') || '';
    if (!changeAt) return;

    var left = changeAt - now.getTime();
    /* The preposition belongs to the caller, not to `countdown`: it answers
       "any moment" at zero, and "in any moment" is not English. Same split as
       the server's, so the two never disagree mid-sentence. */
    var gap = countdown(left);
    until.textContent = 'NYSE ' + phaseWord(nextPhase) +
      (gap === 'any moment' ? ' ' : ' in ') + gap;

    /* The moment worth watching. The digits spin up to a blur and settle into
       the new state, once — `turned` latches, because an animation that
       retriggered every second past the boundary would be a strobe.

       The phase attribute is updated so the accent colours follow, but the
       page is NOT re-fetched: this script may repaint a clock and must not be
       what decides the session. `market_clock.py` on the server owns that, and
       the next render is what confirms it. Same rule as the live stream — the
       client updates a value the server already rendered, never reveals one. */
    /* Latched on the BOUNDARY, not on a boolean. A bare flag would fire once
       and never again, so a page left open across two sessions would animate
       the first and sit through the second; latching on the timestamp lets a
       new boundary arm it again while still refusing to strobe on this one. */
    if (left <= 0 && turned !== changeAt) {
      turned = changeAt;
      bar.setAttribute('data-phase', nextPhase);
      until.textContent = 'NYSE ' + phaseWord(nextPhase) +
        ' now — reload for the next boundary';

      /* The direction carries the meaning. A blur alone says "something
         changed" and makes the reader look for what; spinning UP into the
         regular session, DOWN into a shut market and SIDEWAYS into an
         out-of-hours session says which of the three before they read a word.
         Matches the three states the cells use, so the strip tells one story.

         Unknown phases fall back to sideways rather than guessing a direction:
         a wrong direction is worse than a neutral one, because it would be
         read as information. */
      var dir = nextPhase === 'open' ? 'up'
              : (nextPhase === 'weekend' ? 'down' : 'side');
      var marks = ['turning', 'turn-' + dir];
      var clear = function (el) {
        window.setTimeout(function () {
          el.classList.remove('turning', 'turn-up', 'turn-down', 'turn-side');
        }, 1900);
      };
      bar.classList.add(marks[0], marks[1]);
      clear(bar);
      for (var j = 0; j < faces.length; j++) {
        (function (el) {
          el.classList.add(marks[0], marks[1]);
          clear(el);
        })(faces[j]);
      }
    }
  }

  tick();
  window.setInterval(tick, 1000);
})();
"""

MARK = (
    '<svg viewBox="0 0 64 64" width="22" height="22" aria-hidden="true">'
    '<path fill-rule="evenodd" d="M32 2a30 30 0 1 0 0 60 30 30 0 0 0 0-60Z'
    'm0 8a22 22 0 1 1 0 44 22 22 0 0 1 0-44Z"/>'
    '<path d="M32 17 47 45h-9.4L32 34.2 26.4 45H17L32 17Z"/></svg>'
)

# Inlined as a data URI rather than served from a route. A browser asks for
# /favicon.ico on every fresh page load, and without this the dashboard answers
# 404 into the console on a surface whose whole job is making problems visible.
FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
    "%3Crect width='64' height='64' fill='%230B0E12'/%3E"
    "%3Cg fill='%23E9ECEF'%3E%3Cpath fill-rule='evenodd' d='M32 6a26 26 0 1 0 0 52"
    " 26 26 0 0 0 0-52Zm0 7a19 19 0 1 1 0 38 19 19 0 0 1 0-38Z'/%3E"
    "%3Cpath d='M32 19 45 44h-8.1L32 35.3 27.1 44H19L32 19Z'/%3E%3C/g%3E%3C/svg%3E"
)

PAGES: tuple[tuple[str, str], ...] = (
    ("/", "Board"),
    ("/decisions", "Decisions"),
    ("/trades", "Trades"),
    ("/analytics", "Analytics"),
    # Chat ahead of Dreaming, because Chat is the dominant one. It is the page
    # an operator opens to ask a question about the account, several times a
    # day; Dreaming is read when there is time to read it. The pair used to sit
    # the other way round, which put the speculative surface in front of the
    # one that answers.
    ("/chat", "Chat"),
    ("/dreaming", "Dreaming"),
    # Settings last, beside Sign out. The order is a scan path: the pages you
    # open several times a day sit left, and the one you touch when something
    # needs configuring sits at the far end with the other administrative
    # control rather than between two you use constantly.
    #
    # That leaves Dreaming between Chat and Settings, which is where it belongs
    # for the same reason: it is the least frequent of the reading pages, so it
    # sits at the boundary between what you use and what you administer.
    ("/settings", "Settings"),
)

# The dreamer's avatar. Drawn here in primitives rather than shipped as an
# image: it is six ellipses and two paths, it inherits the palette, it animates
# in CSS, and it costs no request. A small hooded creature with oversized ears,
# projected on a hover pad, in the same holo line the rest of the deck is drawn
# in.
#
# `aria-hidden`, because it is a mood and not information. Everything the page
# actually says is said in text beside it.
DREAMER = """
<svg class="dreamer" viewBox="0 0 120 120" aria-hidden="true">
  <ellipse class="pad" cx="60" cy="106" rx="30" ry="5"/>
  <g class="floaty">
    <ellipse class="ear left" cx="23" cy="53" rx="18" ry="8.5"/>
    <ellipse class="ear right" cx="97" cy="53" rx="18" ry="8.5"/>
    <path class="robe" d="M35 74c9 7 41 7 50 0l8 26H27Z"/>
    <path class="head" d="M60 25c17 0 28 12 28 27s-12 27-28 27-28-12-28-27 11-27 28-27Z"/>
    <ellipse class="eye" cx="49" cy="51" rx="6" ry="7"/>
    <ellipse class="eye" cx="71" cy="51" rx="6" ry="7"/>
    <path class="mouth" d="M54 66c4 3 8 3 12 0"/>
  </g>
</svg>
"""


# ------------------------------------------------------------------ helpers


def _e(value: object) -> str:
    return html.escape(str(value))


#: The `user:password@` a URL is allowed to carry between its scheme and its
#: host. Matched loosely on purpose — anything at all sitting in that position
#: is userinfo, whether or not it looks like a secret.
_URL_USERINFO = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/\s@]+)@")


def _redact_urls(text: str) -> str:
    """Any credential a URL is carrying, removed before the page renders it.

    **This page reports a credential as configured or not configured and never
    renders one, and a base URL is the hole in that rule.** `ANTHROPIC_API_KEY`
    and `DO_INFERENCE_KEY` are only ever asked about; `DO_INFERENCE_BASE_URL` is
    operator-supplied free text that is PRINTED — as the Inference row's value,
    inside `InferenceProvider.detail`, and again on the dream card — and
    `https://user:secret@host` is a perfectly ordinary way to write one, which
    every http client in the world accepts. So an operator who put the
    credential where the URL wanted it would have it rendered three times on a
    page that may be exposed behind one shared password, and a screenshot
    travels.

    Nothing here validates the URL and nothing rejects one: this is a display
    rule, and the value that reaches the endpoint is untouched. The failure
    direction is deliberately towards over-redaction — an `@` in that position
    is userinfo whether or not it looks like a secret, and a host printed as
    `https://***@host` is a legible surprise, where a printed password is not.

    Applied to `detail` as well as to `base_url`, because that sentence embeds
    the URL — including in the half-configured case, where `base_url` is empty
    and the declared one appears in the prose and nowhere else.
    """
    return _URL_USERINFO.sub(r"\1***@", text)


def _money(value: float | None, *, sign: bool = False) -> str:
    if value is None:
        return "n/a"
    return f"{'+' if sign and value > 0 else ''}${value:,.2f}"


def _pct(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}%"


def _cls(value: float | None) -> str:
    if value is None or value == 0:
        return ""
    return "pos" if value > 0 else "neg"


def _word(count: float, singular: str, plural: str | None = None) -> str:
    """The right form of a noun for a count, without the count.

    For the cases where the figure is rendered separately from the word it
    governs — a stat tile's value and its caption, say.
    """
    return singular if count == 1 else (plural or singular + "s")


def _count(count: int, singular: str, plural: str | None = None) -> str:
    """A count and its noun, agreeing: `1 trade`, `3 trades`.

    Written once because it was written wrong sixteen times. "1 qualifying
    loss(es) in a row", "only 1 trades so treat as noise", "no losing trades yet
    across 1", "Median chain 1 — hops" — every one of them a template that never
    considered the singular, and every one of them on a deck whose entire
    argument is that its figures are careful. A reader who catches the deck
    being sloppy about a word has no way to know it is not being sloppy about a
    number.

    `(s)` is not the fix. It is the same shrug written down, and it reads as a
    form nobody finished.
    """
    return f"{count} {_word(count, singular, plural)}"


def _when(stamp: datetime) -> str:
    return stamp.astimezone(UTC).strftime("%d %b %Y, %H:%M UTC")


def stat(
    label: str, value: str, meta: str = "", cls: str = "", live: str = ""
) -> str:
    """One figure in a card.

    `live` names a field in the stream's payload, and the ONLY thing it does is
    let the client repaint a figure the server already rendered. It never
    reveals one: the value is in the markup before any script runs, exactly as
    the projection layer's rule requires, so a browser with no JavaScript shows
    the reading it was served rather than an empty box.
    """
    attr = f' data-live="{_e(live)}"' if live else ""
    return (
        f'<div class="card stat"><span class="k">{_e(label)}</span>'
        f'<b class="{cls}"{attr}>{value}</b>'
        + (f"<small>{meta}</small>" if meta else "")
        + "</div>"
    )


def meter(used: float, cap: float, unit: str, digits: int = 2) -> str:
    ratio = min(used / cap, 1.0) if cap else 0.0
    over = "over" if cap and used > cap else ""
    return (
        f'<div class="meter"><div class="track">'
        f'<div class="fill {over}" style="width:{ratio * 100:.1f}%"></div></div>'
        f'<div class="legend"><span>{used:.{digits}f}{unit} used</span>'
        f"<span>{cap:.{digits}f}{unit} cap</span></div></div>"
    )


def pips(lit: int, total: int) -> str:
    cells = "".join(
        f'<i class="{"lit" if i < lit else ""}"></i>' for i in range(total)
    )
    return (
        f'<div class="pips" role="img" aria-label="{lit} of {total} qualifying '
        f'losses toward the stand-down trigger">{cells}</div>'
    )


def shell(
    title: str, active: str, body: str, *, env: Env, exposed: bool = False,
    tape: str = "",
) -> str:
    """`tape` is the ticker strip, rendered by the caller because it needs
    the poller and the rules and this function has neither.

    Empty is a supported state and renders no strip at all — a deployment
    with the watchlist switched off, or any page built without one, gets the
    header sitting straight on the content exactly as before.

    Two things in the `<head>` are duplicated in `login_page` and have to stay
    that way, because that page does not come through here and it is the first
    thing a phone loads. `theme-color` paints the browser's own chrome to match
    the deck, so the address bar stops sitting on the page as a light band.
    `viewport-fit=cover` is not a layout choice: it is the precondition for
    `env(safe-area-inset-*)` resolving to anything but zero, and without it the
    safe-area padding in `STYLES` is dead code on the only hardware that needs
    it.
    """
    nav = "".join(
        f'<a href="{path}"{" aria-current=page" if path == active else ""}>{label}</a>'
        for path, label in PAGES
    )
    if exposed:
        nav += '<a href="/logout">Sign out</a>'
    mode = "paper" if env.alpaca_paper_trade else "LIVE"
    return f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0B0E12">
<meta name="robots" content="noindex">
<link rel="icon" href="{FAVICON}">
<title>{_e(title)} &middot; Mudhorn Capital</title><style>{STYLES}</style></head>
<body data-mode="{_e(mode)}" data-operator="{_e(env.greeting_name)}">
<header class="bar"><div class="wrap">
  <a class="brand" href="/">{MARK} MUDHORN <span class="thin">CAPITAL</span></a>
  <nav>{nav}</nav>
  <span class="live paper" id="link"><i></i>{_e(mode)}<span class="link-label"></span></span>
</div></header>
{tape}
<main><div class="wrap">{body}</div></main>
<footer class="wrap">Live operator view{
    " behind a shared password" if exposed else ", bound to the loopback interface"
}. Paper trading. Private vehicle, not managing anyone else's money. Rendered
{datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC.</footer>
<script>{SCRIPT}</script>
</body></html>"""


# The operator's own timezone, for greetings ONLY.
#
# Everything else in this repository reasons in UTC on purpose — sessions, every
# journal timestamp, every figure rendered on these pages — and none of that
# changes. But "Good morning" is a statement about the reader's day, not about
# the market's, and computing it in UTC would wish a New Zealander good evening
# over breakfast. The conversion is confined to this one function and touches no
# figure.
OPERATOR_TZ = "Pacific/Auckland"


def greeting(env: Env, *, now: datetime | None = None) -> str:
    """A time-aware greeting, or an empty string when nobody said who to greet.

    Empty is a real answer here. A deployment with no `OPERATOR_NAME` is a
    supported configuration, and a greeting reading "Good morning, " would be
    worse than none.
    """
    name = _e(env.greeting_name)
    if not name:
        return ""

    stamp = now or datetime.now(UTC)
    try:
        local = stamp.astimezone(ZoneInfo(OPERATOR_TZ))
    except Exception:
        # A box without a timezone database still gets a greeting, just a
        # less-well-judged one. Losing the salutation entirely because tzdata is
        # missing would be a poor trade.
        local = stamp

    hour = local.hour
    if hour < 5:
        word = "You are up late"
    elif hour < 12:
        word = "Good morning"
    elif hour < 18:
        word = "Good afternoon"
    else:
        word = "Good evening"
    return f"{word}, {name}"


def head(
    eyebrow: str, title: str, asof: str = "", lede: str = "", asof_live: bool = False
) -> str:
    """`asof_live` marks the stamp for the stream to rewrite.

    Only the Board sets it, and only because the Board is the one page whose
    stamp describes a reading rather than the render. Left off, the stamp is
    whatever the server said and stays that way, which is right for a page
    built from files on disk."""
    mark = ' data-live-read=""' if asof_live else ""
    return (
        f'<div class="page-head"><div><p class="eyebrow">{_e(eyebrow)}</p>'
        f"<h1>{_e(title)}</h1></div>"
        + (f'<p class="asof"{mark}>{_e(asof)}</p>' if asof else "")
        + "</div>"
        + (f'<p class="note" style="max-width:68ch">{_e(lede)}</p>' if lede else "")
    )


# ------------------------------------------------------------------ banners


def banners(
    stand_down: StandDownState,
    alerts: list[ExpiryAlert],
    untracked: list[str],
    *,
    stale: list[str] | None = None,
    audit: AuditView | None = None,
    tailnet: TailnetStatus | None = None,
    reading_stale: bool = False,
) -> str:
    """Anything needing attention, ahead of the numbers.

    Ordered by consequence rather than by section: an option expiry resolves
    itself automatically and irreversibly if nobody is watching, so it leads.

    **`reading_stale` goes first of all, because it qualifies the rest.** Every
    banner below it is a claim about a broker reading — this position expires
    on Friday, this symbol is held but not journalled — and an old reading makes
    each of them a claim about how the account stood some hours ago. The
    expiries still lead among the banners that assert something; this one is
    ahead of them because it says how much to trust the assertion.
    """
    out: list[str] = []
    now = datetime.now(UTC)

    if reading_stale:
        # The id is the stream's handle on it. This banner is a claim about the
        # reading the SERVER built the page from, and the stream's whole job is
        # to replace that reading — so once a fresh one lands the claim is
        # false, and it used to stand for as long as the tab was open. Measured:
        # forty-five seconds and eleven stream messages later the page still
        # said its figures were not current while the tiles repainted every five
        # seconds above the sentence. A warning that outlives its cause teaches
        # an operator to ignore the next one, which is the same reasoning that
        # put `RECHECK_COMMAND` on the tailnet banner.
        out.append(
            f'<div class="banner warn" id="{STALE_BANNER_ID}">'
            "<b>These figures are not current</b>"
            "The last successful broker read is older than this page expects, so "
            "everything below — the positions, the expiries, the risk against the "
            "caps — describes that reading rather than the account as it stands "
            "now. Nothing has failed: the poller stops reading when nobody is "
            "watching, and the live stream refreshes this within a few seconds of "
            "the page opening.</div>"
        )

    # Deliberately on the surface that is about to disappear. It reads backwards
    # — a warning about losing the dashboard, shown on the dashboard — and it is
    # the only channel that works: the failure is 80 days of notice followed by
    # an outage, and during the notice period this page is up and being looked
    # at. After the key lapses nothing on this box can reach anyone.
    if tailnet is not None and tailnet.needs_attention(now=now):
        out.append(
            '<div class="banner warn"><b>Tailscale link</b>'
            f"{_e(tailnet.headline(now=now))}</div>"
        )

    for alert in (a for a in alerts if a.needs_action):
        out.append(
            '<div class="banner crit"><b>Option expiry, action required</b>'
            f"{_e(alert.message)}</div>"
        )

    if stand_down.is_active(now):
        out.append(
            f'<div class="banner warn"><b>Stage {stand_down.stage} stand-down</b>'
            f"{stand_down.consecutive_losses} consecutive losses. Live trading is "
            f"suspended for {stand_down.days_remaining(now):.1f} more days. "
            "Paper trading continues as normal, and closing a position or moving "
            "a stop is never blocked.</div>"
        )

    if untracked:
        out.append(
            '<div class="banner warn"><b>Open risk is understated</b>'
            f"{_count(len(untracked), 'held position')} have no journal entry "
            f"({_e(', '.join(untracked))}), so their planned stop is unknown and "
            "their risk is not counted below. Actual risk is higher than shown.</div>"
        )

    # The mirror image of `untracked`, and the one that is easy to miss: the
    # journal says a position is open, the broker does not hold it. Open risk
    # counts from the journal, so this makes the headline figure OVERSTATED
    # rather than understated, and the Board would otherwise show risk against
    # an empty position list with nothing to explain the contradiction.
    if stale:
        out.append(
            '<div class="banner warn"><b>Open risk may be overstated</b>'
            f"The journal holds {_count(len(stale), 'open trade')} the broker is not "
            f"reporting ({_e(', '.join(stale))}). Either they closed outside the "
            "bot, or reconciliation has not run since they did. Open risk below "
            "still counts them, so the real figure is lower.</div>"
        )

    if audit is not None and audit.is_degraded:
        detail = _count(audit.malformed, "unreadable line")
        if audit.unreadable_files:
            detail += ", " + _count(len(audit.unreadable_files), "unreadable file")
        out.append(
            '<div class="banner warn"><b>Decision log is incomplete</b>'
            f"{detail}. The trail below is missing entries rather than showing "
            "everything that happened.</div>"
        )

    if not out:
        out.append(
            '<div class="banner ok"><b>Clear</b>No stand-down, no expiries '
            "needing action, and every held position is journalled.</div>"
        )
    return "".join(out)


# -------------------------------------------------------------------- board


def since_last_visit(summary: SinceLastVisit) -> str:
    """The strip answering "what changed while I was away".

    The module supplies its own words — `headline()` and `caveats()` — the same
    way `tailnet.headline()` and `news_history` do, so a renderer cannot get the
    three states wrong by paraphrasing them. All this decides is emphasis.

    It renders nothing at all on a first visit. A strip saying "we have no
    record of you" is noise on the one visit where there is genuinely nothing to
    report, and the empty case is the common one.
    """
    if not summary.is_reportable:
        return ""

    tone = "warn" if summary.needs_attention else ""
    counts = ""
    if summary.anything_new:
        bits = []
        if summary.new_decisions:
            bits.append(_count(summary.new_decisions, "cycle"))
        if summary.new_trades_closed:
            bits.append(_count(summary.new_trades_closed, "trade") + " closed")
        if summary.new_rejections:
            bits.append(f"{summary.new_rejections} rejected")
        if bits:
            counts = (
                '<span class="fresh-tag">' + _e(" &middot; ".join(bits)) + "</span>"
            )

    caveats = "".join(f'<p class="note">{_e(c)}</p>' for c in summary.caveats())
    return (
        f'<div class="banner {tone} fresh" style="margin-bottom:1rem">'
        f"<b>Since you were last here</b>{_e(summary.headline())}{counts}"
        f"{caveats}</div>"
    )


def _limit_row(value: float | int | None, default: float | int, fmt: str) -> str:
    """One limit, and where it came from.

    Three states rather than two, and the third is the one that earns its keep.
    A class may set a limit LOOSER than the portfolio default — `account:` is a
    default, not a ceiling, and nothing refuses one — so a looser value is said
    out loud rather than rendered identically to a tighter one. That is
    information, not a warning: the operator chose it, and the settings agent
    is what argues the case when one is being changed.

    An absent override shows the portfolio figure rather than a blank, because
    an empty cell reads as "no limit", which is the opposite of what it means.
    """
    if value is None:
        return fmt.format(default) + " (portfolio default)"
    rendered = fmt.format(value) + " (this class)"
    if value > default:
        return rendered + f" — looser than the {fmt.format(default)} default"
    return rendered


def _is_continuous(inst: InstrumentRules) -> bool:
    """A market with no closed hours at all: every day, midnight to midnight.

    Delegates so the dashboard and the model's context cannot form different
    opinions about which markets have a session at all.
    """
    return is_continuous(inst.windows_by_day)


def _countdown(seconds: float) -> str:
    """A duration a person can act on. Never negative, never bare seconds.

    Clamped at zero because the server renders one moment and the reader sees a
    later one; a countdown that has run out says "any moment" rather than
    showing a minus sign, which reads as a fault.
    """
    if seconds <= 0:
        return "any moment"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return "under a minute"


def _gmt_offset(local: datetime) -> str:
    """`GMT+12`, matching what `Intl.DateTimeFormat` renders in the browser.

    The offset rather than the abbreviation, and the two halves agreeing
    matters more than which is chosen. The server used to print `NZST` and the
    script replaced it with `GMT+12` on the first tick, so the label visibly
    changed a second after load for no reason a reader could see.

    The offset also happens to be the more useful of the two here: the point of
    four clocks side by side is the arithmetic between them, and `GMT-4` next
    to `GMT+12` states the sixteen-hour gap that `EDT` next to `NZST` leaves as
    an exercise.
    """
    offset = local.utcoffset()
    if offset is None:
        return ""
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    return f"GMT{sign}{hours}" + (f":{mins:02d}" if mins else "")


def _tape_price(value: float) -> str:
    """No currency sign and no thousands separator, deliberately.

    The tape carries mixed instruments — equities in dollars, `BTC/USD` in the
    tens of thousands — and a strip this narrow cannot afford four extra
    characters per cell. Two decimals throughout so the columns stay even, and
    the browser's formatter matches this exactly for the same reason `money`
    matches `_money`: one figure formatted two ways eventually disagrees.
    """
    return f"{value:,.2f}" if value < 10_000 else f"{value:,.0f}"


#: What the tape's tooltip says the strip is, once, for the whole run.
#:
#: The cells carry no column header, so without this the tape is a row of prices
#: with nothing saying which price. It is the SAME measurement the resting
#: orders quote — `LivePoller._read_ticker` calls `broker.get_tick(symbol).mid`,
#: exactly as the order rows do — read on the tape's own slower clock, so what
#: separates the two figures is the moment rather than the method. If that call
#: ever stops being the midpoint, this label is the first thing that becomes a
#: lie.
TAPE_SOURCE_TITLE = (
    "Bid-ask midpoints, read on the tape's own clock — slower than the account "
    "figures on the Board, which is why it can differ from them."
)


def _tape_read(read_at: datetime | None) -> str:
    """What the strip shows and when it was read, pinned beside the countdown.

    The Board's tiles carry a `taken_at` stamp because a figure with no reading
    time is a present-tense claim about an account nobody may have read since
    last night. The tape had no stamp at all AND its own deliberately slower
    cadence, so it was the one price on the page that could disagree with the
    others for a reason nobody could see.

    Hours and minutes rather than `_when`'s full date: the strip refreshes every
    sixty seconds, so the date would be four fixed characters of noise on the
    tightest row in the interface. The word "mid" travels with it because a time
    on its own would say when the tape was read without saying what it read.

    `None` is "read time unknown" and never a default of now — the same rule as
    the Board's stamp, in the place where the reading is oldest by design.
    """
    if read_at is None:
        return "mid, read time unknown"
    return f"mid, read {read_at.astimezone(UTC):%H:%M UTC}"


#: The move, in percent, at which the power rail is full. Beyond it the rail
#: simply stays full rather than growing — the gauge is for telling a drift from
#: a move, and a scale that ran to the day's worst case would render every
#: ordinary session as a flat line.
FULL_SCALE_PCT = 3.0

#: Instruments between one clock and the next. Four clocks against a watchlist
#: that is a multiple of four, so the pattern repeats cleanly; a remainder would
#: leave one clock trailing a short group and the loop would visibly limp.
PER_GROUP = 4



#: What each venue state MEANS, in a sentence, on the cell's tooltip.
#:
#: Three, because the middle one is a different claim from either neighbour: the
#: figure is current and what you can do with it is not. Collapsing it into
#: "market shut" would be false about the price; collapsing it into the live
#: state would be false about the order.
VENUE_TITLES: dict[VenueState, str] = {
    VenueState.LIVE: "regular session, trading now",
    VenueState.OUT_OF_HOURS: (
        "out of hours - this price is live, but an order placed now rests "
        "until the next regular open"
    ),
    VenueState.CLOSED: "market shut - this is the last session's close",
}

#: The same three states in micro-type on the clock badge itself.
#:
#: Short because it sits beside a four-letter exchange code on a strip with
#: sixteen instruments on it, and a word rather than a glyph because this is
#: the channel that has to survive greyscale, a screen reader and somebody who
#: cannot separate green from amber. The tooltip carries the full sentence from
#: VENUE_TITLES; this carries enough to act on without one.
VENUE_WORDS: dict[VenueState, str] = {
    VenueState.LIVE: "open",
    VenueState.OUT_OF_HOURS: "ext",
    VenueState.CLOSED: "shut",
}

def _tape_cell(quote: TickerQuote, *, venue: VenueState, kind: str) -> str:
    """One instrument on the tape.

    **A quote that could not be read renders as unavailable, never as flat.**
    On a strip of sixteen this is the least conspicuous place in the whole
    interface to put a plausible wrong figure, which is precisely why it must
    not happen here.
    """
    pct = quote.change_pct
    classes = ["cell", f"v-{venue.value}"]
    if quote.tradeable:
        classes.append("can")
    if venue is VenueState.CLOSED:
        # Greyed, because the figure is last session's close and not a live
        # price. The move beside it is still TRUE — it is what happened on the
        # last day that traded — so it is dimmed rather than withheld: hiding a
        # correct figure teaches an operator the tape is unreliable, while
        # showing it at full strength teaches them a Sunday price is current.
        classes.append("shut")

    if quote.last is None:
        body = '<span class="none">no quote</span>'
        mag = 0.0
        # No reading, so no gauge. The rail measures the SIZE of the move, and
        # at `--mag:0` it collapsed to a short pewter stub that reads as a tiny
        # move rather than as an absent one — a plausible wrong figure drawn in
        # 2 pixels. Missing stays missing here as everywhere else.
        classes.append("norail")
    else:
        body = f'<span class="px" data-tick-px>{_tape_price(quote.last)}</span>'
        if pct is None:
            # A price with no previous close: real, and its move is unknown.
            # Saying so beats implying the day is flat.
            body += '<span class="mv" data-tick-mv>no prior close</span>'
            mag = 0.0
            classes.append("norail")
        else:
            classes.append("up" if pct >= 0 else "down")
            wedge = "▲" if pct >= 0 else "▼"
            body += f'<span class="mv" data-tick-mv>{wedge}{abs(pct):.2f}%</span>'
            mag = min(abs(pct) / FULL_SCALE_PCT, 1.0)

    # A title, not only a dimmer colour. A dimmed green and a dimmed red are
    # the pair about one man in twelve cannot separate, and "why is this one
    # grey" is a question the tape should answer without being asked twice.
    #
    # Out of hours gets its own sentence rather than sharing the shut one. The
    # figure is CURRENT in that state and stale in the other, so one label for
    # both would be wrong about one of them.
    title = f"{kind} - {VENUE_TITLES[venue]}"
    return (
        f'<span class="{" ".join(classes)}" style="--mag:{mag:.3f}" '
        f'data-tick="{_e(quote.symbol)}" data-kind="{_e(kind)}" '
        f'title="{_e(title)}">'
        f'<span class="sym">{_e(quote.symbol)}</span>{body}</span>'
    )


def _tape_clock(
    face: ClockFace,
    local: datetime,
    now: datetime,
    phase: MarketPhase | None,
    trades_today: bool | None = None,
) -> str:
    """One clock: the EXCHANGE symbol, its state, and the time.

    The exchange symbol is the label — not the city and the exchange stacked,
    which made each clock as wide as three instrument cells. NYSE already tells
    you it is New York.

    State is carried by colour and glow on that symbol, so there is exactly one
    visual channel for it. Every panel is identical now; New York used to get a
    lighter background marking it as "the" market clock, and that stopped
    meaning anything once each clock carried its own exchange. A difference
    that looks deliberate while signifying nothing is worse than none.

    A zone with no exchange shows the city and no state. Los Angeles is that
    case — the operator asked for US east and west, and there is no exchange on
    the west coast, so it makes no claim rather than borrowing New York's.
    """
    # The calendar is Alpaca's US equity one, so it speaks for New York and
    # for nothing else. Handing it to Sydney would assert that ASX keeps the
    # NYSE holiday calendar, which is a confident wrong answer rather than an
    # absent one.
    state = face.state(
        now,
        phase if face.is_market else None,
        trades_today=trades_today if face.is_market else None,
    )
    label = face.exchange or face.code or face.label
    # `mkt-live`, never `mkt live`. A bare state word as a modifier is what
    # the stylesheet collision guard exists to catch, and it caught this twice
    # in one sitting — `none` (already "no quote" on a cell) and `live`. A
    # compound class cannot be restyled by an unrelated rule that happens to
    # use the same word.
    cls = f"mkt mkt-{state.value}" if state is not None else "mkt mkt-bare"
    # The limit travels with the badge. An exchange whose holidays nobody
    # tracks must say so on the thing making the claim, or a reader takes
    # "closed" and "open" as equally well founded when only one of them is.
    if state is None:
        title = f"{face.label} — no exchange in this zone"
    else:
        caveat = (
            ""
            if face.tracks_holidays
            else " (regular weekday hours; public holidays not tracked "
            "for this exchange)"
        )
        title = f"{face.label} — {VENUE_TITLES[state]}{caveat}"
    # A pip and a word, not only a colour and a glow.
    #
    # Both are omitted where there is no exchange: a badge that said "shut"
    # for Los Angeles would be asserting a session that does not exist there,
    # which is the confident wrong answer this file refuses everywhere else.
    # The pip is `aria-hidden` because the word beside it says the same thing
    # in text, and a screen reader announcing an empty span twice is noise.
    mark = (
        ""
        if state is None
        else '<i class="pip" aria-hidden="true"></i>'
    )
    word = "" if state is None else f'<span class="st">{VENUE_WORDS[state]}</span>'
    return (
        f'<span class="clk" data-tz="{_e(face.zone)}" title="{_e(title)}">'
        f'<span class="{cls}">{mark}{_e(label)}{word}</span>'
        f'<span class="t">{local:%H:%M:%S}</span></span>'
    )

def ticker_tape(
    state: MarketState,
    quotes: list[TickerQuote],
    *,
    watchlist: WatchlistRules | None = None,
    calendar: SessionCalendar | None = None,
    read_at: datetime | None = None,
) -> str:
    """The strip under the header: session phase, four clocks, the watchlist.

    One clock per four instruments, so the four zones are spread through the
    run rather than bunched at the front — a reader glancing at any part of the
    strip sees a clock.

    The run is emitted TWICE and the track scrolls to -50%. That is what makes
    the loop seamless: at -50% the second copy sits exactly where the first
    began, so the animation restarting is invisible. One copy would snap back.

    Two things are pinned outside the scroller. When New York's session next
    changes, which is the fact a clock cannot give and a marquee would carry
    out of sight every ninety seconds — and what these prices ARE.

    **`read_at` is when the TAPE was read, which is not when the account was.**
    The tape runs on `watchlist.refresh_seconds` (60) against a five-second
    account poll, deliberately: a tape is orientation and a minute-old price is
    fine there, where a minute-old equity figure would not be. That gap is
    exactly why the strip showed SPY at 774.12 above a positions table saying
    774.0900 in one render, and with no stamp the older figure looked like a
    contradiction rather than an older figure. `None` renders "read time
    unknown" and never the clock: a caller that cannot say when the quotes were
    read must not be handed a timestamp by default. `LiveSnapshot` carries the
    moment as `ticker_taken_at`, and the stream repaints this stamp from
    `ticker_as_of` on the next message.
    """
    faces = clock_faces(state.now)

    # The calendar's two answers, resolved once rather than per cell. Both are
    # about the US equity session, so a continuous market never consults them.
    local_date = state.now.astimezone(NY).date()
    trades_today = calendar.is_trading_day(local_date) if calendar else None
    today = calendar.day(local_date) if calendar else None
    past_close = today is not None and state.now >= today.close_utc

    def venue_for(symbol: str) -> VenueState:
        """Which of the three states THIS symbol's market is in.

        Crypto is never anything but live. It runs continuously, so a Sunday
        BTC price is a current one — and greying it alongside the equities
        would be the single global session all over again, which is the bug
        `config/rules.yaml` grew an `instruments:` block to fix.

        For equities the distinction that matters is the middle state. A
        pre-market quote is REAL, so it must not be greyed like a Sunday
        close; but an order against it rests until the open, so it must not
        look like a regular-session price either.
        """
        return venue_state(
            continuous=is_crypto_symbol(symbol),
            phase=state.phase,
            trades_today=trades_today,
            past_close=past_close,
        )

    def kind_for(symbol: str) -> str:
        return watchlist.kind_of(symbol) if watchlist else "unclassified"

    def cell(quote: TickerQuote) -> str:
        return _tape_cell(
            quote, venue=venue_for(quote.symbol), kind=kind_for(quote.symbol)
        )

    groups: list[str] = []
    for index, (face, local) in enumerate(faces):
        groups.append(_tape_clock(face, local, state.now, state.phase, trades_today))
        chunk = quotes[index * PER_GROUP : (index + 1) * PER_GROUP]
        groups.extend(
            cell(q) for q in chunk
        )
    # Anything past the last group still gets shown rather than silently
    # dropped: a watchlist that is not a multiple of four is a config choice,
    # not a reason to hide symbols the operator asked for.
    groups.extend(
        cell(q) for q in quotes[len(faces) * PER_GROUP :]
    )

    run = "".join(groups)
    # What is pinned here is a fact about ONE exchange, and it says which one.
    #
    # Two things have now been thrown off this slot for the same reason. The
    # session phase went first — "PRE-MARKET" pinned over the strip was a claim
    # about every cell beneath it and false for the crypto trading right beside
    # it. The gate verdict went second: `trading` / `armed` / `idle` looked like
    # a status light and told an operator nothing they could act on, and the
    # clocks already answer whether New York is open. It now lives on the Board,
    # under "What it will do right now", where there is room to say what each
    # state actually costs.
    #
    # The countdown is what the strip is missing and no clock can give: WHEN the
    # state changes. Naming NYSE is what keeps it out of the trap the phase
    # label fell into — it is a claim about one exchange rather than about
    # everything scrolling past it.
    #
    # "in any moment" is not English, and `_countdown` answers exactly that at
    # zero — deliberately, because it describes something about to happen and a
    # negative figure reads as a fault. So the preposition is the caller's, not
    # the formatter's.
    left = _countdown(state.seconds_to_change)
    until = f"NYSE {state.next_label.lower()}" + (
        f" {left}" if left == "any moment" else f" in {left}"
    )

    return (
        f'<div class="tape" data-phase="{_e(state.phase.value)}" '
        f'data-change-at="{state.next_change.isoformat()}" '
        f'data-next-phase="{_e(state.next_phase.value)}">'
        '<div class="fixed">'
        # Two lines stacked, and the countdown keeps its own row so the
        # "not ticking" marker stays beside the thing it qualifies. The band is
        # a fixed 3.15rem and both lines are micro-type, so stacking costs the
        # strip the width of the WIDER line rather than the sum of the two —
        # which is what makes a second pinned fact affordable at 320px.
        #
        # SCRIPT removes `.frozen` through its own `parentNode`, so wrapping it
        # here needs no change there.
        f'<span class="ln"><span class="until">{_e(until)}</span>'
        # Removed by SCRIPT before its first tick, so its presence means the
        # clocks below — and the countdown beside it — are frozen at page-load
        # time.
        '<span class="frozen">not ticking</span></span>'
        # What the prices on this strip are, and when they were read. Both
        # halves are needed: the time alone would say when without saying what,
        # and the word alone would put an unlabelled reading beside two
        # differently-measured prices on the page below.
        f'<span class="rd" data-tape-read title="{_e(TAPE_SOURCE_TITLE)}">'
        f"{_e(_tape_read(read_at))}</span>"
        "</div>"
        # The run twice, but the second copy in its own element and marked
        # `aria-hidden`. Both halves of that matter and they fix different bugs.
        #
        # The wrapper is what makes the duplicate REMOVABLE in one CSS rule.
        # Under `prefers-reduced-motion` the track stops and the strip becomes an
        # ordinary horizontal scroller, and a reader who scrolls it reached the
        # end and found the whole watchlist again — 32 cells for 16 instruments,
        # 8 clocks for 4. Nothing had ever hidden the copy because nothing could
        # address it.
        #
        # `aria-hidden` fixes the older and quieter one: a screen reader has been
        # reading every instrument and every clock twice in EVERY mode since the
        # marquee was written. The duplicate exists to make a translation loop
        # seamless, which is a statement about pixels and about nothing a
        # non-visual reader is being told.
        #
        # `role="marquee"` names the region for what it is, and its implicit
        # `aria-live="off"` is exactly right — the cells repaint every few
        # seconds and announcing that would never stop. The role REQUIRES an
        # accessible name, so the label is not optional decoration.
        #
        # `tabindex="0"` is the other half of the reduced-motion fix. With the
        # animation off the strip becomes an ordinary horizontal scroller, and
        # Safari and Firefox do not make a scroll container focusable on their
        # own — so without this the whole watchlist is mouse-only in the mode
        # somebody chose by asking for less motion. Unconditional because the
        # server cannot read a media query, and a tabindex added by script
        # would make a keyboard route depend on the projection layer.
        f'<div class="view" role="marquee" tabindex="0" '
        f'aria-label="Watchlist prices and exchange clocks"><div class="track">'
        f'<div class="marquee-run">{run}</div>'
        f'<div class="marquee-run dup" aria-hidden="true">{run}</div>'
        "</div></div>"
        "</div>"
    )


def gate_stance(state: MarketState | None) -> str:
    """Will it trade right now, and what does each answer cost.

    This used to be three words pinned to the left of the ticker tape —
    `trading` / `armed · orders rest until open` / `idle` — and the operator
    flagged it twice. On the strip it read as a status light: it looked like
    something to act on and there was nothing in it to act on, and the clocks
    beside it already said whether New York was open.

    Here there is room for the half that was missing. The middle state is the
    one worth having at all, and three words could never carry it: the bot will
    propose, the gate will approve, and the order will REST rather than fill.

    `None` renders nothing. A caller that cannot say what time the market
    thinks it is must not be handed a verdict — same rule as the reading stamp.
    """
    if state is None:
        return ""

    if state.is_tradeable_by_bot:
        pill, answer = "ok", "It will trade."
        because = (
            "The window in config/rules.yaml is open and the regular session is "
            "running, so an approved proposal reaches the broker and fills at "
            "the price it was sized against."
        )
    elif state.bot_window_open:
        pill, answer = "watch", "It will place. The order will wait."
        because = (
            "The window is open and the regular session is not. Every entry "
            "carries its stop, and Alpaca refuses extended hours on a bracket, "
            "so an approved order rests at the broker and becomes eligible at "
            "the next open — at a price that appears nowhere in what the model "
            "read when it proposed."
        )
    else:
        pill, answer = "hold", "It will not trade."
        because = (
            "Now is outside the window in config/rules.yaml, so the gate "
            "refuses on session grounds before it weighs anything else. "
            "Nothing is wrong; this is the window doing its job."
        )

    return (
        '<section class="block"><h2>What it will do right now</h2>'
        f'<div class="card"><h3>{_e(answer)}'
        f'<span class="pill {pill}">gate</span></h3>'
        # `68ch`, the measure a lede gets. This card is full width where the
        # risk cards beside it are half of a `g2`, so left alone the sentence
        # set to about 150 characters a line — long enough that a reader loses
        # the start of the next one.
        f'<p class="note" style="max-width:68ch">{_e(because)}</p>'
        # The tape carries the same boundary as a gap. This gives the CLOCK
        # time, which the strip has no room for and which is the half an
        # operator needs to plan an evening around.
        '<p class="note" style="max-width:68ch;margin-top:.7rem">New York moves to '
        f"{_e(state.next_label.lower())} at {_e(_when(state.next_change))}, "
        f"{_e(_countdown(state.seconds_to_change))} from now.</p></div></section>"
    )


#: What each loop outcome is called on the deck, and the attribute that colours
#: it. The words are the operator's, the keys are `jobs.Outcome`.
LOOP_WORDS: dict[Outcome, str] = {
    Outcome.RAN: "ran",
    Outcome.SKIPPED: "skipped",
    Outcome.FAILED: "failed",
}


def loop_activity(history: JobHistory | None, *, now: datetime | None = None) -> str:
    """Was the loop actually running, and what was it doing a moment ago?

    `jobs.py` has recorded every pass since it shipped and only the CLI could
    read it, so the one surface an operator actually opens could not answer the
    question the module was written for. It is answered from the audit log, so
    it costs no broker call and is just as true on a cold start as it is once
    the account has been read — which is why it renders on both.

    **Four states, and they must not collapse into two.** A pass that ran and
    proposed nothing is the bot working; a pass SKIPPED with the market shut
    never looked; a pass that FAILED got no decision and must never be read as
    one that decided to hold; and a moment nothing covers at all is not an
    outcome — the loop was stopped, restarting, or the record was lost.
    `Coverage.OUT_OF_RANGE` is kept apart from `NOT_RECORDED` again, because
    "the read could not speak for that moment" and "nothing covers it" are
    opposite claims about the same silence.

    The headline is `JobAnswer.describe()` rather than a sentence rebuilt here.
    Two renderings of one verdict is two things that can disagree, and the one
    that would be wrong is the one on screen.
    """
    if history is None:
        return (
            '<section class="block"><h2>The decision loop</h2>'
            '<p class="note">The loop\'s own record was not read for this '
            "render, so nothing here says whether it is running.</p></section>"
        )

    # The moment the LOG was read, never the moment the page was rendered.
    #
    # Those are microseconds apart and the difference is total: `history.at`
    # refuses anything past `window_to`, so a fresh `datetime.now()` here is
    # always a hair outside the span and every load answered "outside the span
    # that was read" — the card's headline permanently reporting that it could
    # not say, on a history holding fifteen passes. Found by loading the page;
    # a test passing a fixed `now` cannot see it, because the bug is precisely
    # that the two clocks are read twice.
    #
    # Asking about `window_to` is also the honest question: it is the latest
    # instant the record can speak for at all. Same rule as the Board's stamp
    # naming `taken_at` rather than the render.
    moment = now or history.window_to or datetime.now(UTC)
    answer = history.at(moment)
    head_line = (
        f'<p class="loopnow" data-loop="{_e(_loop_key(answer))}">'
        f"{_e(answer.describe())}</p>"
    )

    if not history.has_jobs:
        caveat = (
            " The read also hit its limit, so this window was not fully "
            "examined and older passes are unexamined rather than absent."
            if history.truncated
            else ""
        )
        return (
            '<section class="block"><h2>The decision loop</h2>'
            + head_line
            + '<p class="note"><span class="alert">No pass of the loop is on '
            f"file for the last {history.window_hours:g}h. It was not running, "
            "was restarting, or its records were lost — this is NOT a report "
            f"that it ran and found nothing to do.{caveat}</span></p></section>"
        )

    tiles = (
        stat(
            "Passes",
            f"{len(history.jobs)}",
            f"in the last {history.window_hours:g}h",
        )
        + stat(
            "Ran",
            f"{history.ran}",
            f"{history.quiet} proposed nothing",
        )
        + stat("Skipped", f"{history.skipped}", "market shut, never looked")
        + stat("Failed", f"{history.failed}", "no decision obtained")
    )

    gaps = "".join(
        "<li>"
        + _e(
            f"Gap of {gap.seconds / 60:.0f} minutes after "
            f"{gap.after.isoformat(timespec='minutes')} — "
            + (
                "an unknown number of"
                if gap.missed is None
                else str(gap.missed)
            )
            + " pass(es) missing, and "
            + (
                "the loop was shut down inside it."
                if gap.explained_by_shutdown
                else "no shutdown was recorded inside it."
            )
        )
        + "</li>"
        for gap in history.gaps
    )
    gap_block = (
        f'<p class="note">Stretches the recorded cadence cannot account for. A '
        "shutdown inside one is an ordinary event; one without is the loop "
        f'having gone away while it was supposed to be running.</p><ul class="gaps">{gaps}</ul>'
        if gaps
        else ""
    )

    degraded = ""
    if history.is_degraded:
        parts: list[str] = []
        if history.truncated:
            parts.append(
                "the read hit its limit, so anything older than the oldest "
                "pass counted here is unexamined rather than absent"
            )
        if history.unreadable_records:
            parts.append(
                f"{history.unreadable_records} job record(s) could not be read "
                "— no start time, or an outcome this build does not recognise"
            )
        if history.malformed_lines or history.unreadable_files:
            parts.append(
                f"{history.malformed_lines} unparseable audit line(s) and "
                f"{len(history.unreadable_files)} unreadable file(s) were "
                "skipped"
            )
        degraded = (
            '<p class="note"><span class="alert">These counts are known to be '
            f"incomplete: {_e('; '.join(parts))}.</span></p>"
        )

    return (
        '<section class="block"><h2>The decision loop</h2>'
        + head_line
        + f'<div class="grid g4">{tiles}</div>'
        + '<p class="note">A pass that ran and proposed nothing is the bot '
        "standing pat, which is frequently the right answer. A skipped pass "
        "never looked, and a failed one got no decision at all — none of the "
        "three is a version of either other.</p>"
        + gap_block
        + degraded
        + "</section>"
    )


def _loop_key(answer: JobAnswer) -> str:
    """The one word this answer is coloured by.

    Coverage first, because `NOT_RECORDED` and `OUT_OF_RANGE` are not outcomes
    and must never borrow an outcome's colour. Only a `FOUND` answer has a job
    on it at all.
    """
    if answer.coverage is not Coverage.FOUND or answer.job is None:
        return answer.coverage.value
    return LOOP_WORDS[answer.job.outcome]


def _board_waiting(
    env: Env | None,
    market: MarketState | None = None,
    loop: JobHistory | None = None,
) -> str:
    """The Board before the first reading has arrived.

    Same shape as the real thing, so nothing jumps when the figures land: four
    tiles at the same size, in the same order, holding the width the numbers
    will occupy. `.pending` carries the shimmer; the text under each tile says
    what is being waited for rather than leaving a reader to guess whether the
    deck is broken.

    `market` renders here too, and it is the one thing on this page that is
    genuinely KNOWN on a cold start: it is clock arithmetic against
    `config/rules.yaml` and needs no broker at all. Withholding it alongside
    the figures would be treating "not read yet" as if it meant "nothing can be
    said".

    `loop` is the second of those, and on this page it is the MORE useful of
    the two: a cold start is exactly the moment somebody wants to know whether
    the thing that trades is running, and that answer comes off the audit log
    rather than the broker.
    """
    tiles = "".join(
        stat(label, '<span class="pending">000,000.00</span>', meta, live=key)
        for label, meta, key in (
            ("Equity", "waiting for the first read", "equity_usd"),
            ("Unrealised", "across an unknown number of positions",
             "unrealised_pnl_usd"),
            ("Realised today", "closed trades only", "realised_pnl_today_usd"),
            ("Open risk", "loss if every stop filled at once", "open_risk_usd"),
        )
    )
    return (
        # `asof_live=True` is the whole reason this stamp is not a lie a few
        # seconds later. Without it the stamp carries no `data-live-read`,
        # `paintStamp` returns on its first line, and the page keeps saying
        # "not read yet" while the four tiles beneath it repaint every five
        # seconds with real equity and real open risk. One screen said three
        # separate times that it had no figures, directly above four of them.
        head(
            greeting(env) if env else "Account",
            "Board",
            "not read yet",
            asof_live=True,
        )
        # The id lets the stream retract this the moment the first reading
        # lands — and the reload beside it is what fills in everything this
        # page does not have. A cold-start Board renders four tiles and NO
        # sections: no positions, no resting orders, no risk meters. The stream
        # can only repaint figures the server already rendered, so those
        # sections can never arrive by themselves and the operator had no route
        # to them short of noticing and reloading by hand.
        + f'<div class="banner" id="{COLD_START_BANNER_ID}" '
        'style="border-left-color:var(--holo)">'
        "<b>Reading the account</b>No broker reading has come back yet, so "
        "there are no figures to show. This is a cold start rather than an "
        "empty account: nothing here is zero, it is unknown. The live stream "
        "fills these in as soon as the first read lands, and the page reloads "
        "itself once to bring in the positions, the resting orders and the risk "
        "meters, which cannot be streamed into a page that never rendered them."
        "</div>"
        + f'<div class="grid g4">{tiles}</div>'
        + gate_stance(market)
        + loop_activity(loop)
    )


def board(
    account: AccountSnapshot | None,
    rules: Rules,
    curve: list[tuple[str, float]],
    open_trades: list[Trade],
    stand_down: StandDownState,
    consecutive_losses: int,
    orders: list[WorkingOrder] | None = None,
    prices: dict[str, float] | None = None,
    env: Env | None = None,
    read_at: datetime | None = None,
    stale: bool = False,
    unexplained: UnexplainedMoveReport | None = None,
    market: MarketState | None = None,
    loop: JobHistory | None = None,
) -> str:
    """The account at a glance.

    `env` is optional and only supplies the operator's name for the greeting, so
    every existing caller keeps working and a deployment that never set
    OPERATOR_NAME renders exactly as before.

    **`account` is None until the first broker read comes back**, which is the
    honest state on a cold start rather than a gap to paper over. The page
    renders its own shape with every figure marked as not-yet-read, and the
    live stream fills them in a moment later. Rendering zeros would put a
    number on screen that the page has no way to walk back — and 0.00 equity is
    exactly the sort of plausible wrong figure this repository is built to
    refuse.

    **`read_at` is when the BROKER was read, and the stamp must never be the
    clock.** This page used to print `as at <now>`, which is true of the render
    and says nothing about the figures beneath it. The poller idle-stops when
    nobody is watching and keeps its last reading, so the first load of a
    morning served an overnight snapshot under the current time — every figure
    stale, nothing on screen saying so, and the stamp the one element a reader
    checks to find out. That is the confident-partial-answer failure this
    repository exists to prevent, arriving through the furniture.

    So the stamp names the reading, `stale` says when the reading is older than
    the stream expects, and the whole thing carries `data-live-read` so the
    stream corrects it rather than leaving a false timestamp standing for as
    long as the tab is open. Omitted, it reads as unknown: a caller that cannot
    say when the figures were read must not be given a timestamp by default.

    **`unexplained` is what `reconcile` found when it compared the broker's
    stops and quantities against the journal's.** Optional, and `None` is a
    third state rather than a clean one: it means this render was given nothing
    to compare, so the absence of a tag below says nothing at all. See
    `_unexplained_note`.

    **`market` is the ticker tape's old verdict badge, rehoused.** Optional and
    `None` renders nothing: it is the only figure here that owes nothing to the
    broker, so a caller with no clock reading is a caller that did not ask,
    which is different from a market that could not be read.

    **`loop` is whether the thing that trades is running at all**, read off the
    audit log. Every other section on this page is a statement about the
    ACCOUNT; this one is a statement about the process, and the two fail
    independently — a healthy-looking account with a stopped loop is exactly
    the state the deck could not previously show. `None` means it was not read
    for this render, which is its own answer and not a clean one.
    """
    if account is None:
        return _board_waiting(env, market, loop)

    equity = account.equity_usd
    open_risk_pct = (account.open_risk_usd / equity * 100) if equity else 0.0
    # `current_risk_usd`, so this agrees with the Open risk tile beside it —
    # that figure comes from `apply_journal_state`, which counts the stops in
    # force. The card claims "the most any one open position can lose", which is
    # a statement about the stop protecting it now; `planned_risk_usd` is what
    # it was SIZED to lose and would report a position whose stop has been
    # pulled in as still risking the original amount.
    largest = max(
        (t.current_risk_usd / equity * 100 for t in open_trades if equity), default=0.0
    )
    unrealised = sum(p.unrealised_pnl_usd for p in account.open_positions)

    tiles = (
        stat(
            "Equity", _money(equity), f"{_money(account.cash_usd)} cash",
            live="equity_usd",
        )
        + stat(
            "Unrealised",
            _money(unrealised, sign=True),
            f"across {_count(len(account.open_positions), 'position')}",
            _cls(unrealised),
            live="unrealised_pnl_usd",
        )
        + stat(
            "Realised today",
            _money(account.realised_pnl_today_usd, sign=True),
            "closed trades only",
            _cls(account.realised_pnl_today_usd),
            live="realised_pnl_today_usd",
        )
        + stat(
            "Open risk",
            _money(account.open_risk_usd),
            "loss if every stop filled at once",
            live="open_risk_usd",
        )
    )

    risk_cards = (
        '<div class="card"><h3>Total open risk</h3>'
        '<p class="note">Every open stop filling at once, as a share of equity.</p>'
        + meter(open_risk_pct, rules.account.max_total_risk_pct, "%")
        + '<p class="note" style="margin-top:.7rem">A proposal that would push '
        "this past the cap is rejected outright, never trimmed to fit.</p></div>"
        '<div class="card"><h3>Largest single trade</h3>'
        '<p class="note">The most any one open position can lose.</p>'
        + meter(largest, rules.account.max_risk_per_trade_pct, "%")
        + '<p class="note" style="margin-top:.7rem">A wider stop buys a smaller '
        "position, never more risk.</p></div>"
        '<div class="card"><h3>Stand-down</h3>'
        + (
            f'<p class="note">Active at stage {stand_down.stage}. Live execution '
            f"is withheld for {stand_down.days_remaining(datetime.now(UTC)):.1f} "
            "more days.</p>"
            if stand_down.is_active(datetime.now(UTC))
            else '<p class="note">Clear. '
            + _count(consecutive_losses, "qualifying loss", "qualifying losses")
            + f" in a row against a trigger of "
            f"{rules.stand_down.consecutive_losses_trigger}.</p>"
        )
        + pips(consecutive_losses, rules.stand_down.consecutive_losses_trigger)
        + '<p class="note" style="margin-top:.7rem">A stand-down suspends money, '
        "not trading. Paper carries on.</p></div>"
        '<div class="card"><h3>Concurrent positions</h3>'
        '<p class="note">Held at once, across every symbol.</p>'
        + meter(
            len(account.open_positions),
            float(rules.account.max_concurrent_positions),
            "",
            0,
        )
        + '<p class="note" style="margin-top:.7rem">Trade frequency is a risk '
        "parameter here, not a performance one.</p></div>"
    )

    if read_at is None:
        stamp = "read time unknown"
    elif stale:
        stamp = f"last read {_when(read_at)} — not refreshed since"
    else:
        stamp = f"read {_when(read_at)}"

    fixed = _fixed_reading_note(read_at)

    return (
        head(
            greeting(env) if env else "Account",
            "Board",
            stamp,
            asof_live=True,
        )
        + f'<div class="grid g4">{tiles}</div>'
        + gate_stance(market)
        # Directly under the account figures and above everything derived from
        # them, because it is what says whether those figures are being acted
        # on. A stopped loop leaves every tile above looking exactly as healthy
        # as a running one does.
        + loop_activity(loop)
        + '<section class="block"><h2>Equity</h2>'
        + _curve(curve)
        + "</section>"
        + '<section class="block"><h2>Risk against the caps</h2>'
        + f'<div class="grid g2">{risk_cards}</div></section>'
        + '<section class="block"><h2>Open positions</h2>'
        + fixed
        + _positions(account, open_trades, equity, unexplained)
        + "</section>"
        + _working_orders(
            orders or [],
            prices or {},
            fixed_reading=fixed,
            # What is HELD, so a resting order can be told apart from an entry
            # that has not filled. Without it every protective leg reads as a
            # pending order, which is how the operator's own stop came to be
            # reported as clutter twice.
            positions=account.open_positions,
            unexplained=unexplained,
        )
    )


def _fixed_reading_note(read_at: datetime | None) -> str:
    """Which reading a section that does NOT repaint was built from.

    The stamp under the page title is owned by the stream, so after the first
    message it describes the reading the four TILES are showing. Four elements
    on the Board carry `data-live`; the positions table, the resting orders, the
    risk meters and the equity curve are server-rendered and cannot be
    repainted, because the client may only update a figure the server already
    put on the page.

    So one timestamp was making a claim about two different readings at once —
    true of the tiles under it and false of the tables below, with nothing on
    screen to say which. That is the same failure as `as at <now>` over an
    overnight snapshot, one layer in: a timestamp that describes the render
    rather than the reading, arriving through the furniture.

    The fix is not to make the stamp vaguer. Each section that cannot refresh
    itself carries the reading it was built from, fixed, and says plainly that
    it will not move until the page is reloaded. It deliberately carries no
    `data-live-read`: the whole point is that this one does not change.
    """
    when = _when(read_at) if read_at else None
    return (
        '<p class="note" style="margin-bottom:.6rem">'
        + (
            f"Built from the broker reading taken {_e(when)}. "
            if when
            else "Built from a broker reading whose time is unknown. "
        )
        + "This section does not repaint — the four figures above it do. Reload "
        "for a newer one.</p>"
    )


def _trail(o: WorkingOrder) -> str:
    """What a trailing leg follows the price by, or that nobody can say.

    Two facts, and the second is the one that has to be loud. The trail SIZE is
    what turns a moving number into an exit somebody chose: 1.50% behind the
    high-water mark is a decision, and a level that was 820 yesterday and is 815
    today with no size beside it is a mystery. `trail_is_unreadable` is the
    broker having reported the leg as trailing and named neither a percentage
    nor an amount, which leaves the current trigger readable and where it goes
    next unknowable — so it takes the alert colour, exactly as
    `trigger_price_unknown` does one level up.

    The high-water mark is shown where the broker gave one, because it is what
    the trail is measured FROM. Without it the size is a distance from a point
    that appears nowhere on the page.
    """
    if o.trail_is_unreadable:
        return (
            '<span class="alert">trailing stop &mdash; trail size unknown</span>'
        )
    if o.trail_percent is not None:
        behind = f"{o.trail_percent:g}% behind"
    elif o.trail_price is not None:
        behind = f"{o.trail_price:,.4f} behind"
    else:  # pragma: no cover - `trail_is_unreadable` covers this above
        behind = "trail unstated"
    mark = (
        f", high water {o.high_water_mark:,.4f}"
        if o.high_water_mark is not None
        else ""
    )
    return f'<span class="muted">trailing stop, {behind}{mark}</span>'


def _order_level(o: WorkingOrder) -> str:
    """The price this order is waiting on, named for what kind of price it is.

    This cell used to be `limit_price or "market"`, which made two separate
    false statements about the one order that matters most. Every entry is a
    GTC bracket or an OTO now, so a stop leg is resting at the broker for as
    long as the position is open — and a stop leg has no `limit_price`, so it
    rendered as **market**. It is not a market order: it becomes one only if it
    triggers, and the level it triggers at appeared nowhere on the deck.

    Worse, a stop at a known 820 and a stop whose trigger the broker did not
    report rendered identically. `models.WorkingOrder` carries `order_type` next
    to `stop_price` precisely so those two can be told apart — "this is a limit
    order and correctly has no stop" against "this is the leg the operator's
    third rule depends on and nobody can read its level". The second is the one
    worth saying loudly, so it says `unknown` in the alert colour rather than
    disappearing into a muted blank.

    **A trailing leg had the same defect one level in.** A real trailing stop
    rendered as `815.0000 stop` — every figure correct, and the cell said the
    operator had chosen 815 when the broker had trailed to it and would be
    somewhere else by the next reading. So the word is "trailing stop" and the
    trail travels with it: without the size, a level that moves on its own is
    indistinguishable from a fixed level moving for reasons nobody recorded,
    which is precisely the reading `detect_unexplained_moves` used to take.

    And `trail_is_unreadable` gets `trigger_price_unknown`'s alert treatment,
    for the reason that one has it: a resting stop whose level nobody can read
    is most of the way to no stop, and a trailing leg with no trail on it is a
    level nobody can read one reading ahead.
    """
    if o.trigger_price_unknown:
        return '<span class="alert">unknown</span> <span class="muted">stop</span>'
    if o.is_stop and o.stop_price is not None:
        if o.is_trailing:
            return f"{o.stop_price:,.4f} {_trail(o)}"
        return f'{o.stop_price:,.4f} <span class="muted">stop</span>'
    if o.limit_price is not None:
        return f"{o.limit_price:,.4f}"
    if "market" in o.order_type.lower():
        return "market"
    # No level, and the broker did not say what kind of order this is. That is
    # not a market order either; it is a gap in what was read back.
    return '<span class="muted">unknown</span>'


def _order_gap(o: WorkingOrder, price: float) -> float | None:
    """How far the market still has to travel before this order does something.

    Positive means it has not got there yet, for either kind of order — but the
    arithmetic is the MIRROR of a limit's, because a stop sits on the other side
    of the market. A buy limit rests below the price and a buy stop triggers
    above it, so reusing `distance_to_fill` for a stop leg would report the
    right magnitude with the wrong sign, and a stop 6% away from firing would
    read as one that should already have gone.

    `distance_to_fill` is left alone rather than taught about stops: it is named
    for filling, and a stop does not fill at its trigger.
    """
    if price <= 0:
        return None
    if o.is_stop:
        if o.stop_price is None:
            return None
        if o.direction == Direction.BUY:
            return (o.stop_price - price) / price * 100
        return (price - o.stop_price) / price * 100
    return o.distance_to_fill(price)


def _protects(order: WorkingOrder, held: Mapping[str, Direction]) -> bool:
    """Whether this resting order is the other half of an open position.

    A protective leg is one that would REDUCE a position that exists: a stop or
    a take-profit on the opposite side to what is held. An order on the same
    side as the position is adding to it, and an order on a symbol nothing is
    held in is an entry — both are pending in the way the word usually means.

    Read off the direction rather than off `is_stop`, because a bracket's
    take-profit is a plain limit order and is every bit as much a consequence of
    the position as the stop leg is. `is_stop` is what decides how the LEVEL is
    read; this decides which list the row belongs in.

    A stop resting on a symbol with nothing held is False here, and that is the
    honest answer rather than an oversight: this reading shows no position for
    it to protect. `_working_orders` says so on the row instead of quietly
    filing it under protection it cannot demonstrate.
    """
    side = held.get(order.symbol)
    if side is None:
        return False
    return order.direction is not side


def _working_orders(
    orders: list[WorkingOrder],
    prices: dict[str, float],
    *,
    fixed_reading: str = "",
    positions: Sequence[Position] = (),
    unexplained: UnexplainedMoveReport | None = None,
) -> str:
    """Orders resting at the broker, split by what they are actually doing.

    **Two entirely different things rest here and one flat list conflated
    them.** An entry is a limit order waiting for its price, which never fills
    if the price does not come — genuinely pending. A **protective leg** is the
    other half of the bracket every entry goes out as: it exists *because* a
    position exists, it is doing its job by sitting there, and it is what the
    operator's third rule amounts to at the broker.

    **The word "Pending" was the defect, not the row under it.** *"I understand
    what the pending order is but for the ui it's not a pending order it's a
    stop loss isn't it? Pending infers the trading agent is going to put another
    trade down."* Exactly so: a heading is a claim, and "Pending orders" claims
    something is about to happen. A resting stop is the guarantee that something
    will NOT happen — it is the opposite claim — so that heading told the
    operator the agent was mid-way into a new position every time they looked.
    The word is reserved for entries now, where it is true.

    That is the second time this same leg has been read as debris. The first was
    `str()` on an alpaca-py enum rendering it as status `other`; this one was a
    heading. *A badly rendered safety mechanism gets mistaken for junk and asked
    to be removed*, and the general form is worth carrying: **a wrong heading is
    not fixed by the row underneath being correct.**

    So the two are separated and named rather than filtered, and **nothing here
    offers a way to cancel one** — there is no `cancel_order` on the `Broker`
    protocol, and a button that took a stop off a dashboard would be the
    shortest route in this repository to an unprotected live position.

    **The absence of a protective leg becomes legible for the first time here.**
    In one list it was invisible; grouped, an open position with nothing resting
    behind it is a gap in the row of protection, and `unexplained` is what can
    state it — including saying it could not be established, because
    `can_check` is False when the order book itself could not be read and an
    outage must never render as a clean all-clear.
    """
    held = {p.symbol: p.direction for p in positions}
    protective = [o for o in orders if _protects(o, held)]
    pending = [o for o in orders if not _protects(o, held)]
    unreadable = unexplained is not None and not unexplained.can_check

    return (
        '<section class="block"><h2>Stop losses and targets in force</h2>'
        + fixed_reading
        # No separate lede: the caption below is visible AND is what names the
        # scroll region through `aria-labelledby`, so a sentence above it would
        # be the same claim twice with two places to let it drift.
        + _protection_gap(positions, protective, unexplained)
        + _order_table(
            protective,
            prices,
            kind="protective",
            empty=(
                # A failed read must not render as an empty book. The banner
                # above says so and this sentence must not contradict it, for
                # the reason `_board_waiting` exists: an unknown is not a nought.
                "The order book could not be read, so this is not a list of "
                "nothing resting — it is a list of nothing returned."
                if unreadable
                else "Nothing is resting behind the open positions."
                if positions
                else "No positions open, so there is nothing to protect."
            ),
        )
        + "</section>"
        + '<section class="block"><h2>Pending entries</h2>'
        # Both tables are server-rendered from the same reading and NEITHER
        # repaints, so both say so. One note across two sections would leave the
        # second looking live.
        + fixed_reading
        + _order_table(
            pending,
            prices,
            kind="pending",
            empty=(
                "The order book could not be read, so nothing here says an "
                "entry is or is not waiting."
                if unreadable
                else "No entries pending. The agent is not part-way into a new "
                "position."
            ),
        )
        + "</section>"
    )


def _protection_gap(
    positions: Sequence[Position],
    protective: Sequence[WorkingOrder],
    unexplained: UnexplainedMoveReport | None,
) -> str:
    """An open position with nothing resting behind it, said loudly.

    Three states, never two. `unexplained` is the authority when it is present,
    because it already excludes crypto — Alpaca accepts no bracket there, so a
    crypto position with no resting stop is expected rather than alarming — and
    because `can_check` is False when the order book could not be read at all.
    An outage must not render as an all-clear, which is the
    `FinnhubCalendar.is_degraded` rule arriving at the order book.

    With no report at all, this falls back to naming the positions with no
    protective row on this reading and says plainly that nothing compared it
    against the journal. That is weaker than the report and is still a fact
    about what is on screen.
    """
    if not positions:
        return ""

    if unexplained is not None and not unexplained.can_check:
        return (
            '<p class="note"><span class="alert">The resting orders could not '
            "be read, so this list is not a list of what is protecting the "
            "account — it is what one failed reading returned. Nothing here "
            "says a position is unprotected, and nothing here says one is "
            "protected.</span></p>"
        )

    if unexplained is not None:
        naked = unexplained.positions_without_a_resting_stop
        if not naked:
            return ""
        return (
            '<p class="note"><span class="alert">'
            f"{_e(', '.join(naked))} {_word(len(naked), 'is', 'are')} open with "
            "no stop order resting at the broker. The journal's stop is a figure "
            "rather than an order, so rule 3 is being met by stop_watch "
            "reporting a breach on the loop's pulse and by nothing at the "
            "broker. Crypto is excluded from this — Alpaca accepts no bracket on "
            "it — so these are equities whose bracket is gone.</span></p>"
        )

    covered = {o.symbol for o in protective}
    naked = sorted(p.symbol for p in positions if p.symbol not in covered)
    if not naked:
        return ""
    return (
        '<p class="note"><span class="alert">'
        f"{_e(', '.join(naked))} {_word(len(naked), 'has', 'have')} no order "
        "resting on this reading. Nothing compared that against the journal, so "
        "this is what the broker returned and not a verdict.</span></p>"
    )


def _order_table(
    orders: Sequence[WorkingOrder],
    prices: dict[str, float],
    *,
    kind: str,
    empty: str,
) -> str:
    """One of the two tables. `kind` chooses the caption's opening and nothing
    else; the sentence naming what the price column measures is shared, because
    it is true of both tables and one copy cannot drift from the other."""
    label = (
        "Stop losses and targets in force" if kind == "protective"
        else "Pending entries"
    )
    if not orders:
        return (
            '<div class="scroll" role="region" tabindex="0" '
            f'aria-label="{_e(label)}"><p class="empty">{_e(empty)}</p></div>'
        )

    rows = ""
    for o in orders:
        price = prices.get(o.symbol)
        gap = _order_gap(o, price) if price else None
        level_word = "trigger" if o.is_stop else "limit"
        if gap is None:
            gap_text = "n/a"
        elif abs(gap) <= 0.005:
            gap_text = f"at the {level_word}"
        elif gap < 0 and o.is_stop:
            # The market is already through a stop that has not fired. Out of
            # hours that is expected — a stop becomes a market order and
            # extended-hours venues take limits only — and it is exactly what
            # `stop_watch` reports on the loop's pulse. Never muted.
            gap_text = f"{gap:+.2f}% — through the trigger"
        else:
            gap_text = f"{gap:+.2f}% away"
        # A positive gap means the price still has to travel; that is the
        # difference between waiting and never filling.
        if gap is None:
            gap_cls = "muted"
        elif gap < 0 and o.is_stop:
            gap_cls = "alert"
        elif gap <= 0:
            gap_cls = "muted"
        else:
            gap_cls = ""

        # Each cell is built before the row, never with a trailing conditional
        # on a multi-part f-string: the ternary binds to the whole expression,
        # not the last fragment, and silently eats the rest of the row.
        level_cell = _order_level(o)
        # The bid-ask midpoint the poller computed, which is what `_order_gap`
        # measures from — see the caption. Not the broker's mark on a position:
        # the two are a cent or so apart and only one of them answers "how far
        # is this order from doing something".
        mid_cell = f"{price:,.4f}" if price else "unknown"
        filled_note = (
            f' <span class="muted">({o.filled_qty:g} filled)</span>'
            if o.filled_qty
            else ""
        )
        submitted = _when(o.submitted_at) if o.submitted_at else "unknown"
        # The broker's own word when it gave one, never the bucket it fell
        # into. `OrderStatus.OTHER` is a truthful answer to "which of our seven
        # is this" and an unusable one on a Board: the live stop leg holding
        # the operator's third rule up rendered as OTHER, indistinguishable
        # from a status this build has never seen. Alpaca calls it `held`, and
        # `held` is a thing an operator can act on.
        status = (o.broker_status or o.status.value).replace("_", " ")

        # A value and its qualifier travel inside ONE element. Under 760px each
        # `td` becomes a `space-between` flex row with the label injected as
        # `::before`, so a bare "6" plus a bare "(2 filled)" are two flex items
        # and land at opposite ends of the card with the label between them —
        # one figure rendered as two fields. Wrapped, the row has exactly two
        # children and reads "QTY   6 (2 filled)".
        # A stop with nothing held against it. Not filed under protection it
        # cannot demonstrate, and not passed off as an ordinary entry either —
        # the bot submits no stop entries, so this is a leg whose position has
        # gone: closed by hand, or filled and not yet read back.
        orphan = (
            '<br><span class="alert">a stop with no position on this reading'
            "</span>"
            if kind == "pending" and o.is_stop
            else ""
        )
        rows += (
            f'<tr class="data"><td data-l="Symbol"><span><b>{_e(o.symbol)}</b>'
            f"{orphan}</span></td>"
            f'<td data-l="Side">{_e(o.direction.value)}</td>'
            f'<td data-l="Status"><span class="pill hold">{_e(status)}</span></td>'
            f'<td data-l="Qty" class="r num"><span>{o.qty:g}{filled_note}</span></td>'
            f'<td data-l="Trigger / limit" class="r num">'
            f"<span>{level_cell}</span></td>"
            f'<td data-l="Bid-ask mid" class="r num">{mid_cell}</td>'
            f'<td data-l="Needs" class="r num {gap_cls}">{gap_text}</td>'
            f'<td data-l="Submitted">{_e(submitted)}</td></tr>'
        )

    caption = (
        "Each of these exists BECAUSE a position does — a stop or a target on "
        "the other side of something held — so nothing here is waiting to "
        "become a trade. One resting here is the hard stop doing its job: the "
        "guarantee that a loss will not run. "
        '&ldquo;Needs&rdquo; is how far the market still has to move to the '
        "trigger; a trigger reading &ldquo;unknown&rdquo; means the broker did "
        "not report the level, so nobody can say where the stop is and it "
        "cannot be checked against the journal. A leg marked "
        "&ldquo;trailing stop&rdquo; sets its own trigger as the price runs in "
        "its favour, so the level beside it is where the broker has trailed to "
        "on THIS reading rather than a level anyone chose &mdash; and it is not "
        "compared against the journal for that reason. There is no way to "
        "cancel one from here, deliberately."
        if kind == "protective"
        else "Entries that have not filled and would become positions. "
        "&ldquo;Needs&rdquo; is how far the market still has to move to the "
        "limit; an order placed out of hours rests until the next regular open "
        "rather than trading now."
    )
    # Named on both tables rather than once, because each is read on its own.
    # The column was "Market", which is the word every other price on this page
    # could equally have claimed — and three of them did, in one render.
    caption += (
        " &ldquo;Bid-ask mid&rdquo; is the midpoint of this reading's quote and "
        "is what &ldquo;Needs&rdquo; is measured from; the positions table shows "
        "the broker's own mark on the position, which is a different measurement "
        "of the same instrument."
    )
    cap_id = f"ord-cap-{kind}"
    return (
        # The caption is already the sentence that describes this table, so it
        # is what names the scroll region — `aria-labelledby` rather than an
        # `aria-label` repeating it, which would be two names to keep in step.
        '<div class="scroll" role="region" tabindex="0" '
        f'aria-labelledby="{cap_id}"><table>'
        f'<caption id="{cap_id}">{caption}</caption>'
        "<thead><tr><th scope=col>Symbol</th><th scope=col>Side</th>"
        "<th scope=col>Status</th><th scope=col class=r>Qty</th>"
        "<th scope=col class=r>Trigger / limit</th>"
        "<th scope=col class=r>Bid-ask mid</th>"
        "<th scope=col class=r>Needs</th>"
        "<th scope=col>Submitted</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )


def _curve(points: list[tuple[str, float]]) -> str:
    if len(points) < 2:
        return (
            '<div class="curve"><p class="empty">Not enough equity marks to draw a '
            "curve yet. The loop records one per cycle.</p></div>"
        )

    values = [v for _, v in points]
    lo_v, hi_v = min(values), max(values)
    span = (hi_v - lo_v) or 1.0
    lo, hi = lo_v - span * 0.12, hi_v + span * 0.12
    width, height, pad = 1000.0, 240.0, 26.0

    def x(i: int) -> float:
        return 8 + (i / (len(points) - 1)) * (width - 16)

    def y(v: float) -> float:
        return (height - pad) - ((v - lo) / (hi - lo)) * (height - pad - 14)

    line = "".join(
        f"{'M' if i == 0 else 'L'}{x(i):.1f} {y(v):.1f}"
        for i, (_, v) in enumerate(points)
    )
    area = f"{line}L{x(len(points) - 1):.1f} {height - pad:.1f}L{x(0):.1f} {height - pad:.1f}Z"
    base = y(values[0])
    label = (
        f"Equity from {_money(values[0])} to {_money(values[-1])} across "
        f"{len(points)} marks."
    )
    # The two value labels as PERCENTAGES of the plot box rather than as SVG
    # text at a user-space font size. The percentage is the user-space
    # coordinate divided by the viewBox, so each label still sits exactly where
    # it did; what changes is that its TYPE is no longer scaled with the
    # drawing. See the `.curve .lab` comment in STYLES for the measurement.
    #
    # The high label hangs by its bottom edge and the low one by its top, so
    # each is pushed away from the reading it names rather than over it, and
    # neither can be clipped by the plot's own edge at a narrow width.
    hi_from_bottom = (height - (y(hi_v) - 3.0)) / height * 100
    lo_from_top = (y(lo_v) + 3.0) / height * 100
    return (
        '<div class="curve"><div class="plot">'
        f'<svg viewBox="0 0 {width:.0f} {height:.0f}" role="img" '
        f'preserveAspectRatio="none" aria-label="{_e(label)}">'
        f'<path class="area" d="{area}"/>'
        f'<line class="base" x1="8" x2="{width - 8:.0f}" y1="{base:.1f}" y2="{base:.1f}"/>'
        f'<path class="line trace" d="{line}"/>'
        # The head: a dot at the newest reading, with a halo behind it. This is
        # what turns a printed graph into something that is running — the eye
        # goes to the leading edge, which is the only part that can still
        # change. Drawn LAST so it sits above the line it terminates.
        f'<circle class="head-halo" cx="{x(len(points) - 1):.1f}" '
        f'cy="{y(values[-1]):.1f}" r="9"/>'
        f'<circle class="head" cx="{x(len(points) - 1):.1f}" '
        f'cy="{y(values[-1]):.1f}" r="3.5"/>'
        "</svg>"
        f'<span class="lab" style="bottom:{hi_from_bottom:.2f}%">{_money(hi_v)}</span>'
        f'<span class="lab" style="top:{lo_from_top:.2f}%">{_money(lo_v)}</span>'
        "</div>"
        f'<p class="axis"><span>{_e(points[0][0][:10])}</span>'
        f"<span>{_e(points[-1][0][:10])}</span></p>"
        f'<p class="note" style="margin:.75rem 0 0">The dashed line is where this '
        f"curve started. {len(points)} marks recorded.</p></div>"
    )


def _position_tags(
    symbol: str, trade: Trade | None, unexplained: UnexplainedMoveReport | None
) -> tuple[str, list[tuple[str, bool]]]:
    """The badges on one position row, and the sentence behind each.

    Two returns rather than one because they go in different places: the pill
    sits beside the symbol where it is scanned, the sentence sits under the row
    where it is read. A tag whose meaning lives only in a `title` attribute is a
    tag a phone cannot show and a screen reader announces as decoration.

    Each sentence carries whether it is a WARNING. A recorded tighten and an
    unexplained move are not the same kind of fact, and colouring both amber
    would spend the alert colour on the feature working correctly — after which
    it stops meaning anything on the one row where it does.

    **Three states, never two.** `moves` is the finding; `unreadable_stops` and
    `positions_without_a_resting_stop` are the two ways the finding could not be
    established, and folding either into silence would let "we could not look"
    read as "nothing has moved". Same rule as `stops_unchecked` beside
    `stops_breached`, and it is the whole reason the inverse tag exists at all:
    a record that only showed the moves it captured would hide its own failures.

    A fourth and a fifth arrived with trailing legs, and they are the same rule
    again. `trailing_stops` is a level that MOVES BY DESIGN and is deliberately
    not compared — without the tag, "nothing differed" and "this one is not
    checked" are the same silence. `unreadable_trails` is a trailing leg with
    no trail size, which is an unknown and takes the warning colour;
    `trailing_stops` is the exit working and does not.
    """
    pills: list[str] = []
    notes: list[tuple[str, bool]] = []

    if trade is not None and trade.stop_has_moved:
        # Not a warning, and NOT conditional on the broker comparison having
        # run: a recorded tighten is a fact about the journal row, and the two
        # figures on the row need explaining whether or not anything could be
        # read back from the broker. An explained move is the feature working.
        pills.append('<span class="pill moved">stop moved</span>')
        notes.append(
            (
                f"{symbol} was sized against {trade.planned_stop:,.4f} and its "
                f"stop is now {trade.effective_stop:,.4f}. The Stop and At-risk "
                "columns show the level in force; R on the Trades page stays "
                "measured against the level it was sized with.",
                False,
            )
        )

    if unexplained is None:
        return " ".join(pills), notes

    for move in unexplained.moves:
        if move.symbol != symbol:
            continue
        pills.append('<span class="pill unexplained">unexplained-move</span>')
        notes.append((move.describe(), True))

    if symbol in unexplained.unreadable_stops:
        pills.append('<span class="pill unreadable">stop unreadable</span>')
        notes.append(
            (
                f"A stop leg is resting on {symbol} and the broker reported no "
                "trigger price, so whether it has moved is UNKNOWN rather than "
                "fine. Nothing here can state the level it would fire at.",
                True,
            )
        )

    if symbol in unexplained.trailing_stops:
        # Not a warning. A trail moving is the exit working, and colouring it
        # amber beside the two real unknowns above would spend the alert colour
        # on the feature doing its job — after which it stops meaning anything
        # on the row where it does not. The pill exists because the ABSENCE of
        # an unexplained-move tag on a level that visibly moves needs an
        # account of itself: without it, "nothing was found to differ" and
        # "this one is not compared" look identical.
        pills.append('<span class="pill" data-stop="trailing">trailing stop</span>')
        notes.append(
            (
                f"The stop resting on {symbol} is a TRAILING leg, so its trigger "
                "is where the broker has trailed to on this reading rather than "
                "a level anybody chose. It is deliberately not compared against "
                "the journal's stop — a trail moving is the exit working, and "
                "reporting it as an unexplained move would tag it on every "
                "cycle it trailed.",
                False,
            )
        )

    if symbol in unexplained.unreadable_trails:
        pills.append('<span class="pill unreadable">trail unreadable</span>')
        notes.append(
            (
                f"The trailing leg on {symbol} was reported with no trail size — "
                "neither a percentage nor an amount — so its current trigger can "
                "be read and where that trigger moves to next cannot. The level "
                "on screen is true for this reading only.",
                True,
            )
        )

    if symbol in unexplained.positions_without_a_resting_stop:
        pills.append('<span class="pill stopless">no resting stop</span>')
        notes.append(
            (
                f"No stop order is resting on {symbol}. The journal's stop is a "
                "figure rather than an order, so the third rule is being met by "
                "stop_watch reporting a breach on the loop's pulse and by "
                "nothing at the broker. Crypto is excluded from this — Alpaca "
                "accepts no bracket on it — so this is an equity whose bracket "
                "is gone.",
                True,
            )
        )

    return " ".join(pills), notes


def _from_dream(trade: Trade | None) -> tuple[str, str]:
    """The motes on a position that came from an adopted dream, and its trace.

    *"Trades that are 'dreams' show differently — a wisp animation or something
    like orbs floating around it."* A tag would say the same thing in the same
    voice as every other tag on the deck; the point of this one is that the
    provenance is felt before it is read.

    **The line naming the dream is not optional decoration.** The motes say THAT
    a trade came from one; only the link says which, and a treatment that cannot
    be traced back to a record is decoration pretending to be provenance. It
    survives `prefers-reduced-motion` for exactly that reason, where the motes
    do not.

    Returns the attribute for the cell wrapper and the markup to put inside it,
    because they go in different places and the caller owns the wrapper.
    """
    if trade is None or trade.dream_id is None:
        return "", ""
    return (
        ' class="wisped"',
        '<span class="wisps" aria-hidden="true"><b></b><b></b><b></b><b></b>'
        "</span>"
        f'<a class="from-dream" href="/dreaming#dream-{trade.dream_id}">'
        f"from dream #{trade.dream_id}</a>",
    )


def _positions(
    account: AccountSnapshot,
    open_trades: list[Trade],
    equity: float,
    unexplained: UnexplainedMoveReport | None = None,
) -> str:
    """The open positions, at the stop that is PROTECTING each one.

    `effective_stop` and `current_risk_usd` rather than `planned_stop` and
    `planned_risk_usd`. The Board answers "what is at risk right now", so a stop
    the agent or the operator has pulled in has to be the one on screen; showing
    the sizing figure would report a loss the position can no longer take. The
    Trades page keeps `planned_stop`, because a closed row's R is measured
    against what it was sized with and changing that would redefine every
    historical R.

    `unexplained` is optional so a caller with no broker order book — a test, a
    cold start — renders exactly as before rather than claiming a clean check it
    never ran.

    **The price column is named for the MEASUREMENT, not for the moment.** It
    said "Now", and so did the resting orders' "Market" column, and so did the
    tape — three columns in one render reading as "the current price of SPY"
    while showing 774.0900, 774.0800 and 774.12. Every one was correctly
    obtained and they are different facts: this is `Position.current_price`,
    Alpaca's own mark on the position, which is what the broker settles against
    and what unrealised P&L is computed from; the orders table quotes the
    bid-ask midpoint, because that is what an order's distance to its level is
    measured from. Labelled the same, a disagreement between them reads as
    something broken, which is the `market_clock` rule arriving in a new place:
    the venue's phase and the gate's window are stated separately and never
    merged into one green light. They are named, never unified — collapsing
    them onto one source would take the right number away from one of the two
    jobs.

    **A position with no mark reports that, rather than borrowing the entry
    price.** The cell was `current_price or entry_price`, which renders a
    position that opened at 773.32 and cannot be valued as though it were
    sitting exactly at 773.32 — flat, unmoved, and indistinguishable from a real
    reading. That is a fourth price in this column wearing the third one's
    label. `notional_usd` keeps that fallback deliberately, because a rough
    valuation beats none for a total; a per-position column has room to say
    unknown.
    """
    if not account.open_positions:
        return (
            '<div class="scroll" role="region" tabindex="0" '
            'aria-label="Open positions"><p class="empty">Nothing open. Most '
            "days the correct action is none.</p></div>"
        )

    by_symbol = {t.symbol: t for t in open_trades}
    rows: list[str] = []
    for p in account.open_positions:
        trade = by_symbol.get(p.symbol)
        # The stop IN FORCE, which is the tightened one where there is one. A
        # position whose stop has been pulled in stands to lose less, and the
        # Board is the surface that has to say so.
        stop = f"{trade.effective_stop:,.4f}" if trade else "unknown"
        risk = _money(trade.current_risk_usd) if trade else "unknown"
        risk_pct = (
            _pct(trade.current_risk_usd / equity * 100)
            if trade and equity
            else "unknown"
        )
        # Missing stays missing. See the docstring: the entry price standing in
        # for a mark is a figure that looks like a measurement and is not.
        mark = f"{p.current_price:,.4f}" if p.current_price else "unknown"
        tags, notes = _position_tags(p.symbol, trade, unexplained)
        wisp_attr, wisps = _from_dream(trade)
        # `.alert` on an INNER span, never beside `.note` on the `<p>`. Both are
        # single-class rules and `.note` is declared after `.alert`, so
        # `class="note alert"` resolves to pewter and the warning quietly loses
        # its colour. See `_feed_rung`.
        detail = "".join(
            f'<p class="note"><span class="alert">{_e(text)}</span></p>'
            if warn
            else f'<p class="note">{_e(text)}</p>'
            for text, warn in notes
        )
        rows.append(
            # Symbol and its badges inside ONE wrapper. Under 760px a `td` is a
            # `space-between` flex row with the label injected as generated
            # content, so a bare `<b>SPY</b>` beside two bare pills is four flex
            # items flung across the card as though they were four fields. Same
            # trap as the "at risk" cell below and as `_working_orders`.
            f'<tr class="data"><td data-l="Symbol"><span{wisp_attr}>'
            f"<b>{_e(p.symbol)}</b>"
            + (f" {tags}" if tags else "")
            + wisps
            + "</span></td>"
            f'<td data-l="Side">{_e(p.direction.value)}</td>'
            f'<td data-l="Qty" class="r num">{p.qty:g}</td>'
            f'<td data-l="Entry" class="r num">{p.entry_price:,.4f}</td>'
            f'<td data-l="Broker mark" class="r num">{mark}</td>'
            f'<td data-l="Stop" class="r num">{stop}</td>'
            # Value and qualifier inside one element — see `_working_orders`.
            # Under 760px a bare "$980.19" and a bare "(0.98%)" are two flex
            # items in a `space-between` row and end up at opposite ends of the
            # card, reading as two separate fields rather than one figure.
            f'<td data-l="At risk" class="r num"><span>{risk} '
            f'<span class="muted">({risk_pct})</span></span></td>'
            f'<td data-l="Unrealised" class="r num {_cls(p.unrealised_pnl_usd)}">'
            f"{_money(p.unrealised_pnl_usd, sign=True)}</td></tr>"
            + (
                f'<tr class="why"><td colspan="8">{detail}'
                + (
                    f'<div class="quote">{_e(trade.rationale)}</div>'
                    if trade and trade.rationale
                    else ""
                )
                + "</td></tr>"
                if detail or (trade and trade.rationale)
                else ""
            )
        )

    return (
        _unexplained_note(unexplained)
        + '<div class="scroll" role="region" tabindex="0" '
        'aria-labelledby="pos-cap"><table>'
        '<caption id="pos-cap">The stop shown is the one IN FORCE — the '
        "tightened level where a move has been recorded, tagged "
        "&ldquo;stop moved&rdquo; — and &ldquo;at risk&rdquo; follows it. A stop "
        'reading "unknown" means the position is held at the broker with no '
        "journal entry, so its risk cannot be counted. That is reported rather "
        "than guessed at. &ldquo;unexplained-move&rdquo; means the broker's own "
        "figure differs from the journal's with no recorded reason for the "
        "change. &ldquo;Broker mark&rdquo; is Alpaca's own valuation of the "
        "position, which is what unrealised follows; the resting orders below "
        "quote the bid-ask midpoint instead, so the two differ slightly and "
        "both are right.</caption>"
        "<thead><tr><th scope=col>Symbol</th><th scope=col>Side</th>"
        "<th scope=col class=r>Qty</th>"
        "<th scope=col class=r>Entry</th>"
        "<th scope=col class=r>Broker mark</th>"
        "<th scope=col class=r>Stop</th>"
        "<th scope=col class=r>At risk</th>"
        "<th scope=col class=r>Unrealised</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _unexplained_note(unexplained: UnexplainedMoveReport | None) -> str:
    """Whether the broker/journal comparison ran at all, said above the table.

    Three states and they are not interchangeable:

    - **No report** — this render was given nothing to compare, so no row can
      carry a tag and the absence of tags means nothing. A cold start and a test
      are both here.
    - **`can_check` false** — the resting orders could not be read. An empty
      order book from a failed fetch looks exactly like an account with nothing
      resting, so an empty `moves` list means nothing either. This is the
      `FinnhubCalendar.is_degraded` rule on the position-management side.
    - **Checked** — the tags on the rows are the whole finding, and no tags
      means nothing has moved unexplained. That claim is only worth making
      because the two states above are stated apart from it.
    """
    if unexplained is None:
        return (
            '<p class="note">The broker\'s stops and quantities were not '
            "compared against the journal for this render, so no row below can "
            "carry an unexplained-move tag and their absence says nothing.</p>"
        )
    if not unexplained.can_check:
        return (
            # `.alert` on an inner span — see `_position_tags`' sibling comment
            # and `_feed_rung`: `.note` is declared after `.alert`, so the two
            # on one element resolve to pewter.
            '<p class="note"><span class="alert">The resting orders could not be '
            "read, so no stop could be compared against the journal. An empty "
            "order book from a failed fetch looks exactly like an account with "
            "nothing resting — no unexplained-move tag below means UNKNOWN "
            "here, not clean.</span></p>"
        )
    return ""


# ---------------------------------------------------------------- decisions


#: Cycles rendered on one page of the trail.
#:
#: The loop wakes 96 times a day, so this is well under a full session and the
#: page has to say so out loud — see `decisions`.
DECISIONS_PER_PAGE = 40


def decisions(view: AuditView, *, shown: int = DECISIONS_PER_PAGE, page: int = 1) -> str:
    """The agent's decision trail: proposal, gate ruling, outcome.

    This is the only surface on which a REJECTED proposal is visible at all. It
    never becomes a trade, so it reaches neither the journal nor the broker, and
    the reasoning behind a refusal exists nowhere else.

    **The header states what is WITHHELD, and there is a route to it.** It used
    to print `len(view.decisions)` — the total in the view — above
    `view.decisions[:shown]`. Those coincided on the day it was measured, at 40
    of 40, so the page said "40 cycles" and showed forty. The loop wakes 96
    times a day: on the next full session the header would have said 96 with 56
    missing, no "showing 40 of 96" anywhere, and no way to reach the rest. On
    the page whose entire reason to exist is that a rejected proposal is
    visible nowhere else, a silent truncation is the worst available bug — the
    reasoning does not merely look wrong, it looks like it was never recorded.

    Paging is a plain `?page=` link rather than an infinite scroll: it is a URL
    an operator can bookmark and hand to somebody, it needs no JavaScript, and
    it cannot lose its place.
    """
    total = len(view.decisions)
    page = max(1, page)
    start = (page - 1) * shown
    # A page past the end walks back to the last real one rather than rendering
    # an empty section under a header claiming cycles exist. `?page=99` is a
    # typo, not a request to be told nothing.
    if start >= total and total:
        page = (total + shown - 1) // shown
        start = (page - 1) * shown
    window = view.decisions[start : start + shown]
    end = start + len(window)

    lede = (
        "The loop wakes every fifteen minutes, and most of the time the right "
        "answer is nothing. Both are here, newest first. Where it did propose, "
        "so is the gate's ruling and every reason behind it — a refused "
        "proposal never becomes a trade, so this is the only place one exists."
    )
    count = (
        f"{total} cycles"
        if total <= shown
        else f"showing {start + 1}-{end} of the newest {total}"
    )
    body = head("Trail", "Decisions", count, lede)

    if view.is_degraded:
        body += banners(StandDownState(), [], [], audit=view)

    if not view.decisions:
        body += (
            '<div class="card" style="margin-top:1.5rem"><p class="empty">'
            "Nothing recorded yet. The loop writes an entry per cycle into "
            "<code>audit/</code>; run <code>electrum-bot loop</code> and they "
            "appear here.</p></div>"
        )
        return body

    body += '<section class="block">'
    # What was cut from here: a sentence explaining that a cycle is folded
    # behind its head line and that clicking one opens it. A disclosure
    # triangle already says that, and telling a reader how a `<details>` works
    # is the page spending its opening line on itself.
    #
    # What was kept: the half naming what is NOT on screen. That qualifies a
    # count, and a page whose whole reason to exist is that a rejection is
    # visible nowhere else must never leave a reader thinking they have seen
    # everything.
    if total > shown:
        body += (
            '<p class="note" style="max-width:68ch">Older cycles than this '
            "window are still on disk in <code>audit/</code>, and the history "
            "tools reach them.</p>"
        )
    # The legend for the feed markers, ONCE on the page rather than once per
    # cycle. It is a property of how the page reads, not of any one cycle, and
    # at ~380 bytes it would have been 15 KB across forty cycles saying the
    # same sentence forty times — on the page already measured at 269 KB.
    #
    # `68ch` is the measure `head` gives a lede, and this sits directly under
    # one. Left at full width it set to about 150 characters a line beside a
    # paragraph wrapping at 68, which reads as two different kinds of text
    # rather than as one page talking.
    body += (
        '<p class="note" style="max-width:68ch">Under &ldquo;what it read&rdquo;, '
        "an item marked as already on file was in front of the model on an "
        "earlier pass too — the headline cache runs 30 minutes and the post "
        "cache 10, against a loop that wakes every 15. Ages are measured to "
        "that cycle, not to now, and the count is how many of the cycles on "
        "this page carried it.</p>"
    )
    # Built ONCE over the whole view rather than per cycle, and over the whole
    # view rather than the page: paging back must not change whether a headline
    # counts as new. It is a dict walk over decisions already parsed off disk,
    # so it costs no read of its own.
    index = sightings(view)

    for i, entry in enumerate(window):
        # Only the first on the page. Every cycle open is what made this 57,574
        # pixels tall at 390 wide; every cycle shut would make the newest one —
        # the reason anybody opened the page — cost a click as well.
        body += _cycle(entry, expanded=i == 0, seen=index)
    body += "</section>"
    body += _pager(page=page, start=start, end=end, total=total, shown=shown)
    return body


def _pager(*, page: int, start: int, end: int, total: int, shown: int) -> str:
    """Newer / older, and the range in words between them.

    Rendered even on a single page, where both links are absent and the line
    still states the range. A control that appears only once there is something
    to page through teaches an operator that the page has no more — which is
    the claim this whole change exists to stop the header making.
    """
    if total <= shown:
        return ""
    newer = (
        f'<a href="/decisions?page={page - 1}">&larr; Newer</a>'
        if page > 1
        else '<span class="muted">&larr; Newer</span>'
    )
    older = (
        f'<a href="/decisions?page={page + 1}">Older &rarr;</a>'
        if end < total
        else '<span class="muted">Older &rarr;</span>'
    )
    return (
        f'<nav class="pager" aria-label="Decision pages">{newer}'
        f'<span class="range">{start + 1}-{end} of {total}</span>{older}</nav>'
    )


STANCE_PILL = {
    "take": "ok",
    "watch": "watch",
    "pass": "hold",
    "blocked": "no",
}


def _considered(entry: DecisionEntry) -> str:
    """Every symbol the model looked at, not only the ones it proposed.

    Without this a quiet cycle records "no proposals" and nothing else, and
    "nothing met the conditions" reads identically to "the loop never looked at
    QQQ". Only one of those is a working bot.
    """
    assessments = entry.decision.assessments
    if not assessments:
        return ""

    rows = ""
    for a in sorted(assessments, key=lambda x: (x.stance != "watch", x.symbol)):
        waiting = ""
        if a.stance == "watch":
            waiting = (
                f'<div class="rung"><span class="lbl">Trigger</span>{_e(a.waiting_for)}</div>'
                if a.waiting_for.strip()
                else '<div class="rung"><span class="lbl">Trigger</span>'
                '<span class="muted">no condition named, so this is a feeling '
                "rather than a plan</span></div>"
            )
        rows += (
            '<div class="considered">'
            f'<div class="what"><b>{_e(a.symbol)}</b>'
            f'<span class="pill {STANCE_PILL.get(a.stance, "hold")}">{_e(a.stance)}</span>'
            "</div>"
            f'<div class="chain"><div class="rung">{_e(a.reasoning)}</div>{waiting}</div>'
            "</div>"
        )

    watching = sum(1 for a in assessments if a.stance == "watch")
    return (
        f'<div class="step"><p class="eyebrow">Considered '
        f"({_count(len(assessments), 'symbol')}"
        + (f", {watching} on watch" if watching else "")
        + f")</p>{rows}</div>"
    )


def _held(entry: DecisionEntry) -> str:
    """Why each open position is still open, and what would end it."""
    plans = entry.decision.position_plans
    if not plans:
        return ""

    rows = ""
    for p in plans:
        pill = {"hold": "ok", "close": "no", "tighten_stop": "watch"}.get(p.action, "hold")
        rows += (
            '<div class="considered">'
            f'<div class="what"><b>{_e(p.symbol)}</b>'
            f'<span class="pill {pill}">{_e(p.action.replace("_", " "))}</span>'
            + (
                ""
                if p.thesis_intact
                else '<span class="pill no">thesis broken</span>'
            )
            + "</div><div class=\"chain\">"
            f'<div class="rung"><span class="lbl">Still in</span>{_e(p.reasoning)}</div>'
            + (
                f'<div class="rung"><span class="lbl">Closes on</span>'
                f"{_e(p.waiting_for)}</div>"
                if p.waiting_for.strip()
                else ""
            )
            + (
                f'<div class="rung"><span class="lbl">Wrong if</span>'
                f"{_e(p.invalidation)}</div>"
                if p.invalidation.strip()
                else ""
            )
            + "</div></div>"
        )

    return (
        '<div class="step"><p class="eyebrow">Open positions reviewed</p>'
        f"{rows}"
        '<p class="note" style="margin-top:.6rem">Advisory only. Closing a '
        "position and moving a stop sit outside the proposal path, so nothing "
        "here reached the broker.</p></div>"
    )


#: How many items of each feed a cycle renders before it starts counting.
#:
#: The Decisions page already measured 269 KB and 57,574px tall at 390 wide for
#: forty cycles, and the loop wakes 96 times a day. Every feed line is inside
#: the cycle's `<details>` and inside this step's own `<details>`, so it costs
#: no layout until somebody opens it — but it still costs BYTES on every render,
#: which is the budget these caps are actually protecting.
HEADLINES_PER_CYCLE = 8
POSTS_PER_CYCLE = 10
# No cap in practice: the calendar returns the announcements inside one blackout
# window, which is a handful. The number is here so that a feed that started
# returning hundreds could not silently make this page enormous.
BLACKOUTS_PER_CYCLE = 12


def _span(seconds: float) -> str:
    """An elapsed gap between two recorded moments.

    Not `_countdown`, which answers "any moment" at zero because it describes
    something about to happen. This describes something that already did.
    """
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return "under a minute"


def _aware(stamp: datetime) -> datetime:
    """Treat a naive timestamp as UTC rather than raising on a comparison.

    Everything the loop writes is timezone-aware. The audit log is append-only
    and read tolerantly, so a hand-edited or older line must not take a page
    down. Same reasoning as `audit._timestamp` and `news_history._aware`.
    """
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _provenance(
    text: str, index: dict[str, NewsItem], stamp: datetime, *, edge: bool
) -> str:
    """Whether this cycle is where an item FIRST appeared, or a later sighting.

    The Marketaux cache is 30 minutes against a loop that wakes every 15, so
    the same headline is in front of the model for two or three consecutive
    cycles and the X cache repeats a post for up to ten minutes. Rendering each
    of those as a fresh sighting would show one story forty times down the page
    and present every one of them as news — which is the `first_seen`/`last_seen`
    distinction being decorative instead of load-bearing.

    **The oldest cycle on the page cannot claim anything is new.** The index is
    built from the cycles actually read off disk, so an item appearing first in
    the oldest of them may be far older than that; saying "new this cycle"
    there would be an artefact of where the read window happened to stop. It is
    named as the edge instead, which is the same rule `has_cycles` and
    `can_grade_anything` follow: an absence of evidence gets its own answer.
    """
    item = index.get(text)
    if item is None:
        # Only reachable when a caller renders a cycle the index never saw.
        # Silence is honest; a guessed provenance would not be.
        return ""

    moment = _aware(stamp)
    if item.first_seen >= moment:
        if edge:
            return '<span class="seen alert">oldest cycle read &mdash; may be older</span>'
        return '<span class="seen">new this cycle</span>'

    first = item.first_seen.astimezone(UTC)
    # The date only when it differs from the cycle's own, which is most of the
    # time it does not. A repeated date on every line of a page of one day's
    # cycles is bytes spent restating the cycle header above it — and this page
    # renders up to forty cycles of nine items each, so a dozen redundant
    # characters is a measurable fraction of it.
    when = first.strftime(
        "%H:%M UTC" if first.date() == moment.astimezone(UTC).date() else "%d %b %H:%M UTC"
    )
    gap = _span((moment - first).total_seconds())
    # `sightings` counts across the whole view, so this is "in N of the cycles
    # read" and not "in N cycles up to here". The legend above the block says
    # so once; saying it on every line would be the same words 360 times.
    return (
        f'<span class="seen">on file since {_e(when)} &mdash; {_e(gap)} before '
        f"this cycle, in {_count(item.cycles, 'cycle')}</span>"
    )


def _feed_rung(
    label: str,
    texts: list[str],
    index: dict[str, NewsItem],
    stamp: datetime,
    *,
    edge: bool,
    limit: int,
    empty: str,
    degraded: bool = False,
    degraded_text: str = "",
) -> str:
    """One feed's contribution to a cycle, with its emptiness explained.

    An absent block and an empty one read identically, so this never renders
    nothing: a feed with no items says so in a sentence naming which of the
    several reasons applies. That is the `calendar_degraded` lesson — zero is
    not the same claim as unknown — applied per feed, per cycle.
    """
    if not texts:
        if degraded:
            return (
                f'<div class="rung gate no"><span class="lbl">{_e(label)}</span>'
                f"{degraded_text}</div>"
            )
        return (
            f'<div class="rung"><span class="lbl">{_e(label)}</span>'
            f'<span class="muted">{empty}</span></div>'
        )

    items = "".join(
        f"<li>{_e(t)}{_provenance(t, index, stamp, edge=edge)}</li>"
        for t in texts[:limit]
    )
    more = (
        f'<li class="muted">and {len(texts) - limit} more</li>'
        if len(texts) > limit
        else ""
    )
    # A list that arrived alongside a failed fetch is incomplete even though it
    # is not empty. Saying so under the items rather than instead of them, so
    # the partial answer is still readable and still labelled partial.
    #
    # `.alert` sits on an INNER span rather than beside `.note` on the `<p>`.
    # Both are single-class rules and `.note` is declared after `.alert` in the
    # stylesheet, so `class="note alert"` resolves to pewter and the warning
    # colour is silently lost — measured in Chromium at rgb(139,150,164) where
    # amber was intended. Nothing errors and the text still reads; it just
    # stops looking like a warning, which is the whole job it has.
    warn = (
        '<p class="note" style="margin-top:.35rem"><span class="alert">This '
        "list is INCOMPLETE</span>: a fetch failed on this cycle, so what is "
        "above is part of what there was.</p>"
        if degraded
        else ""
    )
    return (
        f'<div class="rung"><span class="lbl">{_e(label)}</span>'
        f'<ul class="feed">{items}{more}</ul>{warn}</div>'
    )


def _read(entry: DecisionEntry, *, seen: Sightings | None = None) -> str:
    """What the model was actually shown when it decided.

    Recorded with the decision rather than reconstructed later. A snapshot taken
    now answers a different question from the one an old cycle raises.

    **The feeds live here rather than on a page of their own.** A headline is
    evidence for one cycle's reasoning, and this is the only surface on which a
    rejected proposal exists at all — so the story and the decision it informed
    belong in the same box. A separate news feed would show the same headlines
    with no decision beside them, which is a different and much weaker artefact.

    `seen` is the cross-cycle index. Optional, because a caller rendering one
    cycle in isolation genuinely has no way to know what came before it, and an
    unmarked item is better than one wrongly marked new.
    """
    inputs = entry.decision.inputs
    index = seen or Sightings()
    stamp = _aware(entry.timestamp)
    edge = index.is_edge(stamp)

    if inputs is None:
        # NOT an empty string, which is what this used to be. A cycle written
        # before `MarketInputs` existed has no feeds ON FILE, and rendering
        # nothing makes it indistinguishable from a cycle that saw nothing. The
        # audit log is append-only and never migrated, so these lines keep the
        # shape they were written in and no later cleverness recovers them.
        return (
            '<details class="step"><summary class="eyebrow">What it read'
            "</summary>"
            '<div class="chain" style="margin-top:.7rem">'
            '<div class="rung"><span class="lbl">Not on file</span>'
            "This cycle predates input recording, so the headlines, posts and "
            "blackout windows it was shown were never written down. That is a "
            "gap in the record rather than a cycle that saw nothing, and it "
            "cannot be backfilled.</div></div></details>"
        )

    # Posts BEFORE headlines, exactly as they reach the model. By the time a
    # headline carries the story the gap has already opened, so reading them in
    # the other order inverts what makes the feed worth having.
    parts = _feed_rung(
        "Posts",
        list(inputs.social_posts),
        index.social_posts,
        stamp,
        edge=edge,
        limit=POSTS_PER_CYCLE,
        empty=(
            # The distinction is what has to survive here: from this record
            # alone a feed nobody switched on and a genuinely quiet morning are
            # the same line. What was cut is how to switch it on — two
            # environment variable names, which is a setup instruction sitting
            # inside a record of one cycle, and Settings is already named as
            # the page that answers it.
            "no posts this cycle. An unconfigured feed and a quiet one look "
            "identical from here; Settings says which this deployment is."
        ),
        degraded=inputs.social_degraded,
        degraded_text=(
            "the social feed was DEGRADED. An empty list here means the fetch "
            "failed, not that nothing was posted."
        ),
    )

    parts += _feed_rung(
        "Headlines",
        list(inputs.headlines),
        index.headlines,
        stamp,
        edge=edge,
        limit=HEADLINES_PER_CYCLE,
        empty=(
            "no headlines this cycle. Marketaux gates nothing, so this is "
            "context the model did without."
        ),
    )

    # Through the same helper as the two feeds above, so a degraded calendar and
    # a degraded X feed cannot drift into being reported differently. This one
    # is the feed that actually gates something — `RiskGate._news_blackout`
    # reads it — which is why its degraded wording says the RULE could not fire.
    parts += _feed_rung(
        "Blackouts",
        list(inputs.news_windows),
        index.news_windows,
        stamp,
        edge=edge,
        limit=BLACKOUTS_PER_CYCLE,
        empty="no announcements inside the window",
        degraded=inputs.calendar_degraded,
        degraded_text=(
            "the earnings calendar was DEGRADED. Zero windows here means the feed "
            "failed, not that there were no announcements, and the blackout rule "
            "could not fire."
        ),
    )

    if inputs.symbols_without_history:
        parts += (
            '<div class="rung gate no"><span class="lbl">No history</span>'
            f"{_e(', '.join(inputs.symbols_without_history))} had no bars, so no "
            "indicators were computed for them.</div>"
        )

    if inputs.indicators:
        rows = "".join(
            f'<tr class="data"><td data-l="Symbol"><b>{_e(s)}</b></td>'
            f'<td data-l="Reading" class="num">{_e(v)}</td></tr>'
            for s, v in sorted(inputs.indicators.items())
        )
        parts += (
            '<div class="rung"><span class="lbl">Indicators</span>'
            # Named with an `aria-label` rather than a caption id: this renders
            # once per cycle and the Decisions page shows forty, so an id would
            # be forty duplicates of the same one. It sits inside a collapsed
            # `<details>`, so the tab stop only exists once a reader has opened
            # the step it belongs to.
            '<div class="scroll" role="region" tabindex="0" '
            'aria-label="Indicator readings the model was shown" '
            'style="margin-top:.4rem"><table><tbody>'
            f"{rows}</tbody></table></div></div>"
        )

    return (
        '<details class="step"><summary class="eyebrow">What it read</summary>'
        f'<div class="chain" style="margin-top:.7rem">{parts}</div></details>'
    )


def _cycle(
    entry: DecisionEntry, *, expanded: bool = False, seen: Sightings | None = None
) -> str:
    d = entry.decision
    pill = {
        "executed": "ok",
        "approved": "ok",
        "refused": "no",
        "rejected": "no",
        "held": "hold",
    }[entry.outcome]

    cost = ""
    if d.claude_input_tokens or d.claude_output_tokens:
        # **A zero here is a missing price, not a free call**, and this is where
        # it has to be said out loud. `Decision.estimated_cost_usd` is a plain
        # float on an append-only record that is never migrated, so it cannot
        # hold "unknown" — but the branch above has already established that
        # tokens were spent, and a call that spent tokens cannot have cost
        # exactly nothing. The pair is therefore readable even though the field
        # is not, and `$0.0000` would be a plausible wrong figure on a page
        # whose whole argument is that its numbers were measured.
        #
        # The cause is a model with no entry in `config.MODEL_SPECS`. The loop
        # writes a `model_cost_unknown` event naming it, because an inference
        # drawn here is not a record.
        priced = f"${d.estimated_cost_usd:.4f}" if d.estimated_cost_usd else "cost unknown"
        # **A price is computed from the price sheet of the model that was
        # ASKED for, so a substitution makes the figure beside it wrong.** The
        # cost, the tokens and every proposal below came back from whatever
        # actually answered — a catalogue aliasing a retired id, a proxy named
        # in `ANTHROPIC_BASE_URL` routing elsewhere — and none of that raises,
        # so the cycle reads as entirely ordinary. Named here rather than left
        # to a log line that has long since scrolled.
        #
        # Only when the two are BOTH on file and disagree. `served_as_requested`
        # is `None` for every record written before the fields existed, and
        # rendering that as a mismatch would manufacture a finding about a
        # cycle nobody recorded anything about.
        substituted = (
            f' &middot; <span class="alert">served by '
            f"{_e(d.served_model_id)}, not the "
            f"{_e(d.requested_model_id)} this cost is priced from</span>"
            if d.served_as_requested is False
            else ""
        )
        cost = (
            f"{d.claude_input_tokens:,} in / {d.claude_output_tokens:,} out"
            + (f" / {d.claude_cached_tokens:,} cached" if d.claude_cached_tokens else "")
            + f" &middot; {priced}"
            + substituted
        )

    # `<details>`, and the head line is its `<summary>`.
    #
    # 40 cycles rendered open measured 57,574px tall at 390 wide — roughly 68
    # phone screens for ONE day of a loop that wakes 96 times. Collapsed, the
    # same page is a scannable list of head lines and every cycle is still one
    # click away with its rejection reasons intact.
    #
    # `<details>` rather than a script, deliberately: the projection layer's
    # rule is that nothing may be hidden unless the script said so, and this is
    # hidden by the browser with no script involved at all. JavaScript off, a
    # throw, a blocked file — the summaries still expand. Printing expands them
    # too in browsers that honour it, which a JS accordion does not.
    out = (
        f'<details class="cycle"{" open" if expanded else ""}>'
        '<summary class="head">'
        f'<span class="when">{_e(_when(entry.timestamp))}</span>'
        f'<span class="pill {pill}">{entry.outcome}</span>'
        f'<span class="note">{_count(len(d.proposals), "proposal")}, '
        f"{entry.approved} approved, {entry.rejected} rejected</span>"
        + (f'<span class="cost">{cost}</span>' if cost else "")
        + "</summary>"
    )

    if d.notes:
        out += f'<div class="assessment"><q>{_e(d.notes)}</q></div>'

    out += _considered(entry)
    out += _held(entry)
    out += _read(entry, seen=seen)

    if not d.proposals:
        out += (
            '<div class="step"><p class="note">Nothing proposed. Doing nothing is '
            "usually the right answer, and it is recorded rather than "
            "assumed.</p></div>"
        )

    # Derived here rather than read off a stored field, and derived from the
    # readings THIS cycle recorded. `MarketInputs.readings` is what the model
    # was shown, so the page states the stop's width against the same ATR the
    # proposal was written over — not against a figure fetched today, which
    # would answer a question nobody asked about a cycle three months old.
    #
    # A record predating `readings` has no figures at all, so every proposal on
    # it renders as NOT MEASURED. That is the honest answer: nothing recovers a
    # number that was never written down, and rendering an ordinary-looking
    # width would be inventing one.
    widths = measure_stop_widths(
        d.proposals, d.inputs.readings if d.inputs is not None else {}
    )

    for i, proposal in enumerate(d.proposals):
        verdict = entry.verdict_for(i)
        # Built as its own value first, never as a ternary trailing a
        # multi-part f-string. Adjacent string literals concatenate BEFORE the
        # conditional binds, so `a b if cond else c d e` is `(a b)` against
        # `(c d e)` — and this one had eaten half the row in each direction: a
        # proposal WITH a target rendered no "risks" figure and no closing
        # `</div>`, and one without lost its `<b>` heading and opened a
        # `</span>` that was never opened. The same trap is called out by name
        # in `_working_orders`, which is where the shape was recognised.
        levels = (
            f"limit {proposal.limit_price:,.4f}, stop "
            f"{proposal.stop_loss_price:,.4f}, target "
            f"{proposal.take_profit_price:,.4f}"
            if proposal.take_profit_price is not None
            # Said out loud rather than left blank. An empty cell reads as a
            # missing figure; "no target" is a decision.
            else f"limit {proposal.limit_price:,.4f}, stop "
            f"{proposal.stop_loss_price:,.4f}, no target"
        )
        out += '<div class="step"><div class="what">'
        out += (
            f"<b>{_e(proposal.direction.value.upper())} {proposal.qty:g} "
            f"{_e(proposal.symbol)}</b>"
            f'<span class="note">{levels}</span>'
            f'<span class="note">risks {_money(proposal.risk_usd)}</span>'
            "</div>"
        )

        out += '<div class="chain">'
        out += (
            '<div class="rung"><span class="lbl">Why</span>'
            f"{_e(proposal.rationale)}</div>"
        )

        if verdict is None:
            out += (
                '<div class="rung"><span class="lbl">Gate</span>'
                '<span class="muted">no verdict on file. The loop drops a '
                "proposal whose symbol has no tick, so the two lists no longer "
                "line up — pairing them would be a guess.</span></div>"
            )
        elif verdict.approved:
            out += '<div class="rung gate ok"><span class="lbl">Gate</span>approved</div>'
        else:
            reasons = "".join(f"<li>{_e(r)}</li>" for r in verdict.reasons)
            out += (
                '<div class="rung gate no"><span class="lbl">Gate</span>rejected'
                f'<ul class="reasons">{reasons}</ul></div>'
            )

        # AFTER the gate's verdict, deliberately. Size is the risk ceiling
        # divided by the stop distance, so a stop inside the spread buys an
        # arbitrarily large position at the same stated risk — TODO.md item 25,
        # measured on a live KO proposal at 0.04 ATR. It belongs on this page
        # because this is the only surface a rejected proposal exists on.
        #
        # It reads last so that nothing can mistake it for an input to the
        # verdict above it. `RiskGate` holds no opinion on where the stop goes,
        # deliberately and permanently, and a line rendered ahead of the gate's
        # own reasons would look like the opinion arriving through the UI.
        if i < len(widths):
            width = widths[i]
            # Both the tight case and the unmeasured one are called out, and
            # they are different findings: one is a measurement that came back
            # alarming, the other is a measurement that never happened. Folding
            # them into one class would let an indicator outage render in the
            # colour of a clean read.
            mark = (
                " tight"
                if width.is_tight
                else " unmeasured"
                if width.is_unmeasured
                else ""
            )
            out += (
                f'<div class="rung stopw{mark}"><span class="lbl">Stop</span>'
                f"{_e(width.render())}</div>"
            )

        result = d.executed[i] if i < len(d.executed) else None
        if result is None:
            out += (
                '<div class="rung"><span class="lbl">Then</span>'
                '<span class="muted">not sent. Either the gate refused it, or the '
                "loop is running without --execute.</span></div>"
            )
        elif result.accepted:
            filled = (
                f"filled {result.filled_qty:g} at {result.filled_price:,.4f}"
                if result.filled_price and result.filled_qty
                else "accepted"
            )
            out += (
                f'<div class="rung"><span class="lbl">Then</span>{filled} '
                f'<span class="muted">({_e(result.order_id or "no id")})</span></div>'
            )
        else:
            out += (
                '<div class="rung gate no"><span class="lbl">Then</span>'
                f"broker refused: {_e(result.error or 'no reason given')}</div>"
            )

        out += "</div></div>"

    return out + "</details>"


# ------------------------------------------------------------------- trades


#: The word for each exit verdict, in the reader's language rather than the
#: enum's. Every member is here: `ExitReport.counts` includes the zeros on
#: purpose, so a bucket that renders as absent rather than as `0` would undo
#: that at the last step.
EXIT_WORDS: dict[ExitReason, str] = {
    ExitReason.STOP_HIT: "stop hit",
    ExitReason.TARGET_HIT: "target hit",
    ExitReason.CLOSED_EARLY: "plan abandoned",
    ExitReason.CLOSED_AT_LEVEL: "closed at a level",
    ExitReason.EXPIRY: "resolved by the broker",
    ExitReason.UNKNOWN: "could not be established",
}


def exit_reviews(report: ExitReport | None, *, never_classified: int = 0) -> str:
    """Why each position ended — the PLAN graded, and never the profit.

    `exit_review.py` has classified every close since it shipped and nothing
    rendered a word of it, so stop-hit, target-hit, closed-by-hand and expiry
    were as indistinguishable to an operator as they were in the journal before
    the module existed. This is the surface.

    **No realised figure appears in this section, and none may be added.** The
    module's own boundary is structural — a test parses its AST and fails if it
    reads any P&L or risk field — and putting a result beside a reason here
    would undo that at the presentation layer, which is the only place left
    that can. The table below carries the money; this carries the plan; the two
    are separate sections and the note says why. A reader who wants to
    correlate them can, and nothing on the page invites it.

    Three distinctions have to survive the render, and each is a rule this
    repository already holds elsewhere:

    - **`None` is not an empty report.** No classification was run for this
      render, so the absence of verdicts says nothing at all. Same shape as
      `unexplained=None` on the Board.
    - **`can_grade_anything` is not "the list is empty".** Nothing closed and
      everything closed unestablishable are opposite findings.
    - **A row the journal never classified is not `UNKNOWN`.** One predates the
      review and was never asked; the other was asked and could not be
      answered. `never_classified` counts the first.

    Rows render in the order they arrive, which the caller is expected to hand
    over newest first — the order every other reader in this repository uses,
    and the order the table under this section renders in.
    """
    if report is None:
        return (
            '<section class="block"><h2>Why each position ended</h2>'
            '<p class="note">Exits were not classified for this render, so '
            "nothing below grades a close and the absence of verdicts says "
            "nothing.</p></section>"
        )

    lede = (
        '<p class="note" style="max-width:72ch">Whether each trade ended the way '
        "it was designed to. This grades the PLAN and carries no result figure "
        "at all &mdash; deliberately, and the money is in the table below "
        "instead. A track record is what a model overfits to on a sample this "
        "small, which is why nothing here reaches the prompt either.</p>"
    )

    if not report.reviews:
        return (
            '<section class="block"><h2>Why each position ended</h2>'
            + lede
            + '<p class="empty">No closed trades, so nothing is graded. That is '
            "not the same as every trade having ended as designed.</p></section>"
        )

    tally = "".join(
        f'<li data-exit="{_e(reason.value)}"><b>{count}</b>'
        f"<span>{_e(EXIT_WORDS[reason])}</span></li>"
        for reason, count in report.counts.items()
    )

    notes = ""
    abandoned = report.plan_abandoned
    if abandoned:
        names = ", ".join(sorted({r.symbol for r in abandoned}))
        notes += (
            '<p class="note"><span class="alert">'
            f"{_e(names)} closed by hand with the price at neither level. That "
            "is the plan being abandoned rather than completed, and it is the "
            "one bucket here that says something about the operator or the "
            "agent rather than about the market.</span></p>"
        )
    if not report.can_grade_anything:
        notes += (
            '<p class="note"><span class="alert">'
            f"{_count(report.reviewed, 'trade')} closed and not one exit could "
            "be established. That is a finding about the record rather than "
            "about the trading &mdash; it is not a clean sheet.</span></p>"
        )
    if report.exits_without_price_provenance:
        notes += (
            f'<p class="note">{report.exits_without_price_provenance} of these '
            "predate the recording of whether the exit price was a broker "
            "reading or the fallback to the entry price, so their price is not "
            "treated as evidence about a level.</p>"
        )
    if never_classified:
        notes += (
            f'<p class="note">{never_classified} closed row(s) carry no reason '
            "recorded at the time they closed and were graded here from what "
            "the row holds. Never classified is NOT the same as "
            "&ldquo;could not be established&rdquo;: one was never asked, the "
            "other was asked and had no answer.</p>"
        )
    # Once, above the list, rather than as a paragraph on every row. Measured on
    # the rendered page: a caveat true of every trade in the set repeated itself
    # down the whole section and stopped being read by the third one. The pill
    # on each row still says WHICH; the sentence explaining it lives here.
    unconfirmed = sorted({r.symbol for r in report.reviews if r.entry_never_confirmed})
    if unconfirmed:
        notes += (
            f'<p class="note">{_e(", ".join(unconfirmed))} '
            f"{_word(len(unconfirmed), 'was', 'were')} graded against what was "
            "SUBMITTED rather than what filled: the entry was never confirmed "
            "against the broker, so the levels each verdict is measured from "
            "are the proposal's.</p>"
        )

    rows = "".join(_exit_row(r) for r in report.reviews)
    return (
        '<section class="block"><h2>Why each position ended</h2>'
        + lede
        + f'<ul class="tally">{tally}</ul>'
        + notes
        + f'<ul class="exits">{rows}</ul></section>'
    )


def _exit_row(review: ExitReview) -> str:
    """One graded exit. The words recorded at the close are quoted, never paraphrased."""
    said = (
        f'<p class="said">Recorded at the close: <q>{_e(review.close_reason)}</q></p>'
        if review.close_reason
        else ""
    )
    hand = (
        '<span class="pill" data-exit="by_hand">closed by hand</span>'
        if review.closed_by_hand
        else ""
    )
    # A pill and not a sentence: the sentence is true of every row in most
    # sets, so it is said once above the list. See `exit_reviews`.
    unconfirmed = (
        '<span class="pill" data-exit="unconfirmed_entry">entry unconfirmed</span>'
        if review.entry_never_confirmed
        else ""
    )
    return (
        f'<li data-exit="{_e(review.reason.value)}">'
        f'<p class="hdr"><b>{_e(review.symbol)}</b> '
        f'<span class="pill" data-exit="{_e(review.reason.value)}">'
        f"{_e(EXIT_WORDS[review.reason])}</span> {hand}{unconfirmed}</p>"
        f'<p class="what">{_e(review.detail)}</p>{said}</li>'
    )


def trades_page(
    recent: list[Trade],
    report: JournalReport,
    exits: ExitReport | None = None,
    *,
    never_classified: int = 0,
) -> str:
    body = head(
        "Journal",
        "Trades",
        f"{report.overall.trade_count} closed",
        "What actually happened, carrying the reasoning it was written down "
        "with rather than a reconstruction. The figure under each result is "
        "its R: the outcome as a multiple of what the trade was designed to "
        "lose, so -1R is the stop doing its job and +2R is twice that back.",
    )

    # Above the table, because it is the verdict and the table is the detail —
    # and because the two are separate sections rather than one row carrying a
    # reason next to a result. See `exit_reviews`.
    body += exit_reviews(exits, never_classified=never_classified)

    if not recent:
        body += (
            '<div class="card" style="margin-top:1.5rem"><p class="empty">'
            "Nothing has closed yet. Open positions are on the Board; this is "
            "where they end up.</p></div>"
        )
        return body

    rows: list[str] = []
    for t in reversed(recent):
        r = t.r_multiple
        exit_price = f"{t.exit_price:,.4f}" if t.exit_price is not None else "n/a"
        exit_date = t.exit_time.date().isoformat() if t.exit_time else "open"
        r_text = f"{r:+.2f}R" if r is not None else "n/a"
        rationale = (
            f'<tr class="why"><td colspan="9"><div class="quote">'
            f"{_e(t.rationale)}</div></td></tr>"
            if t.rationale
            else ""
        )
        wisp_attr, wisps = _from_dream(t)
        rows.append(
            # One wrapper per cell, for the reason given in `_working_orders`:
            # under 760px a `td` is a `space-between` flex row, a `<br>` does
            # not break a line inside one, and the two stacked lines would be
            # flung to opposite ends of the card as though they were separate
            # fields. Wrapped, each cell is one flex item and the `<br>` goes
            # back to doing what it does on the desktop table.
            f'<tr class="data"><td data-l="Symbol"><span{wisp_attr}>'
            f"<b>{_e(t.symbol)}</b><br>"
            f'<span class="note">{_e(t.strategy)}</span>{wisps}</span></td>'
            f'<td data-l="Held"><span>{_e(t.entry_time.date().isoformat())}<br>'
            f'<span class="note">to {_e(exit_date)}</span></span></td>'
            f'<td data-l="Qty" class="r num">{t.qty:g}</td>'
            f'<td data-l="Entry" class="r num">{t.entry_price:,.4f}</td>'
            # `planned_stop` here, deliberately, where the Board shows
            # `effective_stop`. This row is a CLOSED trade and the column sits
            # beside R: R is the result as a multiple of what the trade was
            # designed to lose, so the stop printed next to it has to be the one
            # it was sized against. Swapping in the tightened level would
            # silently redefine every historical R on the page.
            f'<td data-l="Stop" class="r num">{t.planned_stop:,.4f}</td>'
            f'<td data-l="Exit" class="r num">{exit_price}</td>'
            f'<td data-l="At risk" class="r num">{_money(t.planned_risk_usd)}</td>'
            f'<td data-l="Fees" class="r num muted">{_money(t.fees_usd)}</td>'
            f'<td data-l="Result" class="r num {_cls(t.net_pnl_usd)}">'
            f"<span>{_money(t.net_pnl_usd, sign=True)}<br>"
            f'<span class="note">{r_text}</span></span></td></tr>' + rationale
        )

    body += (
        '<div class="scroll" role="region" tabindex="0" '
        'aria-label="Closed trades" style="margin-top:1.5rem"><table>'
        "<thead><tr><th scope=col>Symbol</th><th scope=col>Held</th>"
        "<th scope=col class=r>Qty</th>"
        "<th scope=col class=r>Entry</th><th scope=col class=r>Stop</th>"
        "<th scope=col class=r>Exit</th>"
        "<th scope=col class=r>At risk</th><th scope=col class=r>Fees</th>"
        "<th scope=col class=r>Result</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )
    return body


# ---------------------------------------------------------------- analytics


def analytics_page(report: JournalReport) -> str:
    s = report.overall
    pf = f"{s.profit_factor:.2f}" if s.profit_factor is not None else "n/a"
    er = f"{s.expectancy_r:+.2f}R" if s.expectancy_r is not None else "n/a"

    # An empty sample answers "n/a", never a number.
    #
    # Profit factor already did this and the other three did not, so a journal
    # with nothing closed rendered `Win rate 0%  0W / 0L`, `Expectancy $0.00`
    # and `Net $0.00` beside it — three figures that look measured, on the one
    # page whose strapline says a wrong metric gets believed and then acted on.
    # 0% is not "unmeasured", it is "everything lost"; $0.00 is not
    # "unmeasured", it is "broke even". Measured on the live deck.
    #
    # The gate is `PerformanceSummary.is_empty` rather than `trade_count == 0`
    # spelled out four times, so the question is asked in one place and the
    # answer cannot drift between cards.
    none_yet = "no closed trades yet"
    body = head(
        "Measurement",
        "Analytics",
        f"{s.trade_count} closed trades",
        # The lede used to be "a wrong metric is worse than no metric, because
        # it gets believed and then acted on" — a true principle, aimed at
        # whoever writes this page rather than at whoever reads it, and a
        # disclaimer where an answer belongs. The consequence for a reader is
        # the sample size, so that is what it says now; the principle survives
        # in what the figures DO, which is answer "n/a" rather than 0%.
        "How it has actually gone. Under about twenty closed trades this is "
        "noise rather than a track record — three losses in a row prove very "
        "little, and the Reading column says so wherever the sample is thin. "
        "That is also why none of it is shown to the model.",
    )
    body += (
        '<div class="grid g4" style="margin-top:1.5rem">'
        + stat(
            "Win rate",
            "n/a" if s.is_empty else f"{s.win_rate:.0%}",
            none_yet if s.is_empty else f"{s.wins}W / {s.losses}L",
        )
        + stat("Profit factor", pf, _e(s.health))
        + stat(
            "Expectancy",
            "n/a" if s.is_empty else _money(s.expectancy_usd, sign=True),
            none_yet if s.is_empty else f"per trade, {er}",
            # No gain/loss colour either. A green or red "n/a" would put the
            # figure back in the one channel that survives not being read.
            "" if s.is_empty else _cls(s.expectancy_usd),
        )
        + stat(
            "Net",
            "n/a" if s.is_empty else _money(s.total_pnl_usd, sign=True),
            none_yet
            if s.is_empty
            else f"max drawdown {_money(s.max_drawdown_usd)}",
            "" if s.is_empty else _cls(s.total_pnl_usd),
        )
        + "</div>"
    )
    body += (
        '<section class="block"><h2>The short version</h2>'
        f'<div class="readout">{_e(chr(10).join(render_summary(s)))}</div></section>'
        '<section class="block"><h2>Stops and targets, judged after the fact</h2>'
        f'<div class="readout">{_e(chr(10).join(render_excursions(report.excursions)))}'
        "</div></section>"
    )

    for title, table in (
        ("By strategy", report.by_strategy),
        ("By asset class", report.by_asset_class),
        ("By weekday", report.by_weekday),
    ):
        if not table:
            continue
        # The reading column carries `PerformanceSummary.health`, which hedges
        # itself below the thin-sample threshold. Without it a three-trade row
        # showing 67% and +$400 reads as a result, while the headline above —
        # computed from the same code — says the sample is noise. Two figures
        # from one module disagreeing about how much they can be trusted is the
        # sma_200-over-40-bars error in a table.
        rows = "".join(
            f'<tr class="data"><td data-l="Group"><b>{_e(name)}</b></td>'
            f'<td data-l="Trades" class="r num">{g.trade_count}</td>'
            f'<td data-l="Win rate" class="r num">{g.win_rate:.0%}</td>'
            f'<td data-l="Net" class="r num {_cls(g.total_pnl_usd)}">'
            f"{_money(g.total_pnl_usd, sign=True)}</td>"
            f'<td data-l="Reading"{" class=thin" if g.sample_is_thin else ""}>'
            f"{_e(g.health)}</td></tr>"
            for name, g in table.items()
        )
        body += (
            f'<section class="block"><h2>{_e(title)}</h2>'
            # Three of these render on one page, so the name carries the
            # heading — "By strategy", "By asset class", "By weekday" — rather
            # than three regions all called "Breakdown".
            f'<div class="scroll" role="region" tabindex="0" '
            f'aria-label="{_e(title)}"><table><thead><tr><th scope=col>Group</th>'
            "<th scope=col class=r>Trades</th><th scope=col class=r>Win rate</th>"
            "<th scope=col class=r>Net</th>"
            "<th scope=col>Reading</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div></section>"
        )
    return body


# ----------------------------------------------------------------- settings


def _row(key: str, value: str, why: str = "") -> str:
    return (
        f"<dt>{_e(key)}</dt><dd>{_e(value)}"
        + (f'<span class="why">{_e(why)}</span>' if why else "")
        + "</dd>"
    )


def _calendar_card(
    sessions_ahead: Sequence[SessionDayView],
    *,
    loaded: bool,
    degraded: bool,
    poller_has_read: bool,
) -> str:
    """Which days actually trade, keyed to the equity classes rather than to a
    symbol.

    Every US equity on Alpaca shares one session, so a table keyed by symbol
    would be N identical rows with N chances to drift apart. The class is the
    real key and `config/rules.yaml` already holds the configured window; this
    card holds the part the config CANNOT state, because it changes by date:
    which days are skipped and which end early.

    An empty list with `loaded` false is NOT "no sessions ahead". A calendar
    nobody fetched and a quarter with no trading days are opposite findings, and
    a card that rendered both as a blank space would be the more dangerous one
    silently. Same rule as the cold-start Board saying unknown rather than zero.
    """
    if not loaded:
        # Two different facts, and guessing between them is the failure this
        # whole card is about. "The Board has not been opened" is a reasonable
        # cause and is WRONG whenever the poller has in fact read and the
        # broker had no calendar to give — which is every mock deployment. So
        # the state that is known is reported, and no cause is invented for the
        # other. Found by running it, not by the suite.
        why = (
            "The live poller has not read yet, so this fills in once the Board "
            "has been opened."
            if not poller_has_read
            else "The poller has read and the broker returned no calendar — "
            "either it is a MockBroker, which has none, or the fetch failed."
        )
        return (
            '<div class="card"><h3>Trading calendar</h3>'
            '<p class="muted">Not loaded. Holidays and early closes are '
            "unknown, and the hours above assume an ordinary session. "
            f"{why}</p>"
            '<p class="source">src/bot/session_calendar.py</p></div>'
        )

    stale = (
        '<p class="warn">The last refresh failed, so these dates are not '
        "confirmed current. They are published well in advance and rarely "
        "change.</p>"
        if degraded
        else ""
    )
    if not sessions_ahead:
        rows = (
            '<p class="muted">No sessions in the fetched range.</p>'
        )
    else:
        rows = '<dl class="kv">' + "".join(
            _row(
                day.date,
                _e(day.label),
                "Shorter session than the usual 09:30-16:00."
                if day.early_close
                else "",
            )
            for day in sessions_ahead
        ) + "</dl>"

    early = [d for d in sessions_ahead if d.early_close]
    note = (
        "An early close is the one that bites quietly: the session is genuinely "
        "open, every figure stays plausible, and the market shuts three hours "
        "before anything here would otherwise assume."
        if early
        else "All standard 09:30-16:00 New York sessions."
    )
    return (
        '<div class="card"><h3>Trading calendar</h3>'
        + stale
        + rows
        + f'<p class="muted">{note}</p>'
        '<p class="source">Alpaca trading calendar, cached. Gates nothing.</p>'
        "</div>"
    )


SOCIAL_PILL = {"on": "ok", "off": "hold", "no token": "watch", "degraded": "no"}

#: The span Settings describes the X feed's observed state over.
#:
#: Named here rather than taken from whatever the caller happened to read,
#: because the figure is rendered into the row label — "Degraded in 24h" — and a
#: label that disagreed with the window it describes would be worse than no
#: label at all.
NEWS_WINDOW_HOURS = 24.0


def social_state(rules: Rules, env: Env, recall: NewsRecall | None) -> FeedState:
    """What Settings is allowed to say about the X feed.

    Configuration comes from `config/rules.yaml` and the environment; the two
    observed facts come out of the audit log, because the dashboard never holds
    an `XFeed` — the loop owns it, in a different process.

    **`recall=None` means nobody has read the record**, which is why `degraded`
    stays `None` there rather than defaulting to `False`. A page that had not
    looked must not report a healthy feed, and neither must one whose window
    holds no cycles: `FeedState` keeps those two apart from "nothing failed".
    """
    return FeedState(
        enabled=rules.social.enabled,
        token_present=bool(env.x_bearer_token),
        accounts=tuple(rules.social.accounts),
        lookback_minutes=rules.social.lookback_minutes,
        max_posts=rules.social.max_posts,
        # The loop builds its `XFeed` with the module default, so this is the
        # TTL actually in force rather than a number this page chose. If
        # `main.build_social_feed` ever starts passing one, it belongs in
        # `config/rules.yaml` and this reads it from there instead.
        cache_ttl_seconds=XFEED_CACHE_TTL_SECONDS,
        degraded=(
            None if recall is None or not recall.has_cycles else recall.social_degraded
        ),
        last_post_at=None if recall is None else recall.social_last_seen_at,
    )


def _social_card(state: FeedState, *, window_hours: float) -> str:
    """The X feed's state, reported and never guessed.

    Every one of these is either read from a file or read from the record. The
    one that looks like a measurement and is not is `Last post on file`, which
    is evidence a read WORKED and is not evidence that one failed — a watched
    account with nothing to say produces exactly the same absence. It says so
    on the row rather than leaving a reader to assume.
    """
    degraded = (
        "not known"
        if state.degraded is None
        else ("YES — a fetch failed" if state.degraded else "no failed fetch")
    )
    last = (
        _when(state.last_post_at) if state.last_post_at is not None else "none on file"
    )
    # `.alert` on an inner span, not beside `.note`. See `_feed_rung`: `.note`
    # is declared after `.alert`, so the two together resolve to pewter and the
    # caveat stops looking like one.
    caveats = "".join(
        f'<p class="note"><span class="alert">{_e(c)}</span></p>'
        for c in state.caveats()
    )
    return (
        '<div class="card"><h3>X posts '
        f'<span class="pill {SOCIAL_PILL.get(state.status, "hold")}">'
        f"{_e(state.status)}</span></h3>"
        f'<p class="note">{_e(state.headline())}</p>'
        '<dl class="kv">'
        + _row(
            "Enabled",
            "yes" if state.enabled else "no",
            "social.enabled in config/rules.yaml.",
        )
        + _row(
            "Bearer token",
            "configured" if state.token_present else "not configured",
            "X_BEARER_TOKEN. Presence only — no key is rendered here.",
        )
        + _row("Accounts", ", ".join(state.accounts) or "none")
        + _row("Lookback", f"{state.lookback_minutes} minutes")
        + _row("Posts per cycle", str(state.max_posts))
        + _row(
            "Cache TTL",
            f"{state.cache_ttl_seconds:g}s",
            "Shorter than Marketaux's 1800s on purpose: caching a "
            "market-moving post for half an hour defeats the point of it. A "
            "DEGRADED result is never cached, so one bad minute cannot silence "
            "the feed for a whole TTL.",
        )
        + _row(
            f"Degraded in {window_hours:g}h",
            degraded,
            "Any failed fetch anywhere in the window, not merely the newest "
            "cycle: the claim being made is that the post list was complete "
            "over the whole span.",
        )
        + _row(
            "Last post on file",
            last,
            "The newest cycle that recorded a post. Evidence a read worked; "
            "its absence is NOT evidence that one failed, because a quiet "
            "account looks identical from the record.",
        )
        + "</dl>"
        + caveats
        + '<p class="source">Context only. Nothing from this feed reaches '
        "<code>src/bot/risk.py</code> — a blackout after a high-impact post "
        "would change what the gate refuses and is its own commit.</p></div>"
    )


# --------------------------------------------------------------- the forge
#
# The one place on this dashboard that submits anything other than a password
# or a chat message, and the reasoning for it belongs where somebody tightening
# the page would look.
#
# `tests/test_web.py` asserted that Settings carried no `<form`, `<input`,
# `<select`, `<button` or `contenteditable` at all. That rule has been widened a
# third time, deliberately and by editing the assertion rather than loosening
# it: `<select>` picks WHICH limit, `<input>` and `<textarea>` carry the
# proposed value and the operator's reason, and two `<button>`s ask and confirm.
#
# **These controls CAN change a limit now, and that paragraph used to say the
# opposite.** It was true when it was written and the operator reversed it —
# *"Settings agent can't edit settings?? That's broken"* — so it is rewritten
# rather than left, because prose asserting a guarantee that no longer holds is
# the exact thing this file has already been caught doing once.
#
# What is true, and what a person tightening this page needs to know:
#
# - The controls POST to `/settings/request` and nowhere else. There is no
#   `<form>`: nothing here works with the script blocked, which is the
#   affordance this surface must not have.
# - **This process still cannot write `config/rules.yaml`.** `config/` is
#   root-owned on the droplet, and the route asks root to make the edit through
#   `deploy/apply-settings.sh` — no arguments, id on stdin — which re-validates
#   the whole file through `Rules.load` on a staged copy first. Without that
#   wrapper and its sudoers rule, the change is recorded and the page says
#   plainly that the file did not move.
# - **A loosening still cannot be agreed to in the click that asked about it.**
#   The confirmation is a second, separate act, enforced in
#   `settings_agent.decide` rather than here.


def _forge_option(fact: LimitFact, rules: Rules) -> str:
    """One limit in the picker, quoted in the notation the FILE uses.

    `1.0` rather than `1`, because that is what the line says and what the diff
    will show. A picker that rendered a prettier number would leave the operator
    comparing two spellings of the same value and wondering which is real.
    """
    value, source = effective_value(rules, fact.key)
    shown = "unset" if value is None else format_value(fact, value)
    # "unset, unset" is what naming the source unconditionally produced. An
    # inherited value is worth flagging; an absent one already says so.
    tail = f", {source}" if source == "portfolio default" else ""
    return (
        f'<option value="{_e(fact.key)}" data-current="{_e(shown)}">'
        f"{_e(fact.label)} — now {_e(shown)}{_e(tail)}</option>"
    )


def _forge_reference(limits: dict[str, LimitFact], rules: Rules) -> str:
    """Every limit, with what it is, why it sits there, and what loosening costs.

    Collapsed by default because the whole table is long — thirty entries — and
    a wall of prose is a wall nobody reads. The four questions are kept apart
    rather than merged into one paragraph: what a number DOES, why it sits at
    the value it sits at, the goal it serves, and what loosening actually costs
    are four different answers, and collapsing them is how "it is for safety"
    ends up being the whole justification for a limit.
    """
    rows = []
    for fact in limits.values():
        value, source = effective_value(rules, fact.key)
        shown = (
            "(a list)"
            if fact.unit is Unit.LIST
            else ("unset" if value is None else format_value(fact, value))
        )
        note = (
            f' <span class="muted">({_e(source)})</span>'
            if source == "portfolio default"
            else ""
        )
        rows.append(
            '<details class="step"><summary>'
            f"{_e(fact.label)} — <code>{_e(fact.key)}</code> = {_e(shown)}{note}"
            "</summary>"
            f'<p class="note"><b>What it is.</b> {_e(fact.what)}</p>'
            f'<p class="note"><b>Why it sits there.</b> {_e(fact.why)}</p>'
            f'<p class="note"><b>The goal it serves.</b> {_e(fact.goal)}</p>'
            f'<p class="note"><b>Loosening it costs.</b> {_e(fact.cost)}</p>'
            f'<p class="note">Looser means <b>{_e(fact.looser.value)}</b>.'
            + (
                ""
                if fact.requestable
                else " Not editable through the forge: it is a list, not a "
                "single value, so no safe one-line diff exists for it."
            )
            + "</p></details>"
        )
    return (
        '<div class="card"><h3>What each limit is for</h3>'
        '<p class="note">Lifted from <code>config/rules.yaml</code> and '
        "<code>CLAUDE.md</code> rather than rewritten, and handed to the Armorer "
        "as its briefing — so the sentence you read here is the sentence it "
        "argues from.</p>" + "".join(rows) + "</div>"
    )


def _forge_requests(requests: Sequence[ChangeRequest]) -> str:
    """The arguments already had, won or lost.

    An argument the operator won is a fact worth keeping and so is one they
    lost, so the objection is rendered beside the reason rather than dropped
    once the request was recorded. Six months on, the useful question is not
    what the limit is — the file says that — but what was said before it moved.
    """
    if not requests:
        return (
            '<div class="card"><h3>Change requests</h3>'
            '<p class="empty">None recorded. Nothing has been argued on this '
            "deck yet.</p></div>"
        )
    rows = []
    for req in requests:
        rows.append(
            '<details class="step"><summary>'
            f"#{req.id} <code>{_e(req.key)}</code> {_e(req.old_value)} &rarr; "
            f"{_e(req.new_value)} &mdash; {_e(req.stance)}, {_e(req.status)}"
            "</summary>"
            f'<p class="note"><b>Asked for because.</b> '
            f"{_e(req.reason or 'no reason was given')}</p>"
            + (
                f'<p class="note"><b>The Armorer objected.</b> {_e(req.objection)}</p>'
                if req.objection.strip()
                else '<p class="note">No objection: this was a tightening.</p>'
            )
            + (
                '<p class="note"><b>Stated at the time.</b></p><ul class="note">'
                + "".join(f"<li>{_e(line)}</li>" for line in req.consequence)
                + "</ul>"
                if req.consequence
                else ""
            )
            + (f'<pre class="readout">{_e(req.diff)}</pre>' if req.diff else "")
            + _forge_request_status(req)
            + "</details>"
        )
    return (
        '<div class="card"><h3>Change requests</h3>'
        '<p class="note">Recorded in <code>data/settings_requests.db</code>, '
        "which is not the journal and is not backed up. A <b>pending</b> one did "
        "not move the file and waits on the apply command; an <b>applied</b> one "
        "did, and says when and by which route.</p>" + "".join(rows) + "</div>"
    )


def _forge_request_status(req: ChangeRequest) -> str:
    """What actually happened to the file, per request.

    Three states rather than two, and the third is the reason this is its own
    function: **reverted is not pending.** A row that was applied and then put
    back has a history, and collapsing it to "not currently in force" would
    lose the fact that the limit moved at all — which is the fact somebody
    reading this months later is looking for.

    The route is named and the person is not, because nobody on this box can
    honestly say which person it was: the dashboard has one shared password and
    keeps no record of who signed in.
    """
    if req.status == "pending":
        return (
            f'<p class="note">Not applied. The file still holds '
            f"<code>{_e(req.old_value)}</code>. Apply with "
            f"<code>{_e(req.apply_command)}</code>, which re-validates the "
            "edited file through <code>Rules.load</code> and refuses if the "
            "file has moved.</p>"
        )
    if req.status == "applied":
        when = f"{req.applied_at:%Y-%m-%d %H:%M} UTC" if req.applied_at else "at an unrecorded time"
        route = f", via {_e(req.applied_by)}" if req.applied_by else ""
        return (
            f'<p class="note"><b>Applied</b> {_e(when)}{route}. Undo it with '
            f"<code>{_e(req.revert_command)}</code>, which puts back the exact "
            "previous value.</p>"
        )
    if req.status == "reverted":
        when = (
            f"{req.reverted_at:%Y-%m-%d %H:%M} UTC"
            if req.reverted_at
            else "at an unrecorded time"
        )
        route = f", via {_e(req.reverted_by)}" if req.reverted_by else ""
        return (
            f'<p class="note"><b>Applied and then reverted</b> {_e(when)}'
            f"{route}. The limit moved and was put back; the file holds "
            f"<code>{_e(req.old_value)}</code>.</p>"
        )
    return f'<p class="note">Status: {_e(req.status)}.</p>'


#: The forge's client half.
#:
#: A plain string with a placeholder rather than an f-string, and that is the
#: `SYSTEM_PROMPT_TEMPLATE` lesson applied to JavaScript: this text is full of
#: `{` and `}`, so interpolating into it means doubling every brace, and a
#: missed one is a `KeyError` at render time or, worse, a silently mangled
#: script. One `.replace` of one token avoids the whole class of problem.
#:
#: Every value it puts on screen goes in through `textContent`, never
#: `innerHTML`. The server escapes what it renders; building nodes means the
#: client cannot be the place that stops doing so.
FORGE_SCRIPT = """
<script>
(function () {
  var TOKEN = __TOKEN__;
  var sel = document.getElementById('fg-key');
  var val = document.getElementById('fg-val');
  var why = document.getElementById('fg-why');
  var ask = document.getElementById('fg-ask');
  var out = document.getElementById('fg-out');
  if (!sel || !ask || !out) return;

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function el(tag, text, cls) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  /* What the operator was actually SHOWN, read off the Armorer's log rather
     than held in a parallel array. An argument the operator won is worth
     keeping and so is one they lost; recording the panel as it stands means
     the transcript cannot drift from what was on screen. */
  function transcript() {
    var turns = [];
    document.querySelectorAll('.chat .log .turn').forEach(function (t) {
      var msg = t.querySelector('.msg');
      if (!msg) return;
      turns.push({
        role: t.classList.contains('user') ? 'operator' : 'armorer',
        text: msg.textContent
      });
    });
    return turns;
  }

  function post(confirmed) {
    ask.disabled = true;
    clear(out);
    out.appendChild(el('p', 'Working it out.', 'note'));
    fetch('/settings/request', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        token: TOKEN,
        key: sel.value,
        value: val.value,
        reason: why.value,
        confirm: !!confirmed,
        transcript: transcript()
      })
    })
      .then(function (r) { return r.json(); })
      .then(paint)
      .catch(function (e) {
        clear(out);
        out.appendChild(el('p', String(e), 'note'));
      })
      .finally(function () { ask.disabled = false; });
  }

  function paint(d) {
    clear(out);
    if (!d.ok) {
      out.appendChild(el('p', d.error || 'The request was refused.', 'note'));
      return;
    }
    var box = el('div', null, 'verdict ' + (d.stance || ''));
    box.appendChild(el('h4', d.stance === 'loosening'
      ? 'This loosens a limit'
      : (d.stance === 'tightening' ? 'This tightens a limit' : 'No change')));
    var list = el('ul');
    (d.consequence || []).forEach(function (line) {
      list.appendChild(el('li', line));
    });
    box.appendChild(list);

    if (!d.can_record) {
      box.appendChild(el('p', d.blocked_reason || 'This cannot be recorded.', 'note'));
    } else if (d.recorded) {
      /* What actually happened to the file, never inferred from the fact that
         a request exists. `applied` carries its own ok, and a recorded request
         whose write failed says so in the operator's own words rather than
         reading as a change that landed. */
      if (d.applied && d.applied.ok) {
        box.appendChild(el('p',
          d.label + ' is now ' + d.new_value + ', was ' + d.old_value +
          '. Recorded as request #' + d.recorded.id + '.', 'note'));
      } else if (d.applied) {
        box.appendChild(el('p',
          'Recorded as request #' + d.recorded.id + ' and NOT applied. ' +
          d.applied.detail, 'note'));
      } else {
        box.appendChild(el('p',
          'Recorded as request #' + d.recorded.id + '. Nothing has changed yet.',
          'note'));
      }
      box.appendChild(el('pre', d.recorded.diff, 'readout'));
      box.appendChild(el('pre', d.recorded.commit_message, 'readout'));
      box.appendChild(el('p',
        (d.applied && d.applied.ok)
          ? ('Undo it with ' + d.recorded.revert_command +
             '. Reload this page to see it in the request list.')
          : ('Apply it as root: ' + d.recorded.apply_command +
             '. Reload this page to see it in the request list.'), 'note'));
    } else if (d.requires_confirmation) {
      /* The asymmetry, on screen. A loosening change cannot be accepted in the
         same click that asked about it, so the cost has been read before it is
         agreed to. A tightening one never reaches here. */
      box.appendChild(el('p',
        'Nothing has been recorded. Confirm only if you still want this after '
        + 'reading the above.', 'note'));
      if (window.MUDHORN_FORGE) {
        var open = el('button', 'Put it on the anvil', 'btn');
        open.type = 'button';
        open.addEventListener('click', function () {
          window.MUDHORN_FORGE.confirm({
            key: d.key, label: d.label, unit: d.unit, stance: d.stance,
            oldText: d.old_value, newText: d.new_value,
            consequence: d.consequence || [],
            objection: d.objection, diff: d.diff
          }).then(function (agreed) { if (agreed) post(true); });
        });
        box.appendChild(open);
      } else {
        /* The fallback, and it is deliberate. A throw or a blocked file up
           there must cost a nicer confirmation, never the ability to confirm —
           the same principle as the Cmd+K console falling back to an ordinary
           navigation when the projection layer is absent. Do not delete this on
           the grounds that the module is always there. */
        var yes = el('button', 'I have read that. Make the change.', 'btn');
        yes.type = 'button';
        yes.addEventListener('click', function () { post(true); });
        box.appendChild(yes);
      }
    }
    out.appendChild(box);
  }

  ask.addEventListener('click', function () { post(false); });
})();
</script>
"""


def _forge(
    rules: Rules,
    limits: dict[str, LimitFact],
    *,
    enabled: bool,
    token: str,
    hermes_available: bool,
    soul_found: bool,
    rules_path: Path | None,
    requests: Sequence[ChangeRequest],
) -> str:
    path_name = str(rules_path) if rules_path else "config/rules.yaml"
    intro = (
        '<section class="block"><h2>The forge</h2>'
        '<p class="note" style="max-width:68ch">The Armorer keeps the limits. '
        "It will equip you &mdash; it does not refuse &mdash; but it makes you "
        "say what a change is for, and it states what the new number costs "
        "before anything is written down. A limit getting <b>tighter</b> is "
        "recorded as asked. A limit getting <b>looser</b> states the arithmetic "
        "and waits for you to confirm after reading it.</p>"
        '<p class="note" style="max-width:68ch"><b>This dashboard still cannot '
        f"write <code>{_e(path_name)}</code> with its own hands.</b> That file "
        "is owned by root on the box precisely so the service account cannot "
        "edit its own limits, and this dashboard runs as the service account. "
        "It asks root to make the edit, through one root-owned wrapper that "
        "takes no arguments and re-validates the whole file through "
        "<code>Rules.load</code> on a staged copy before replacing it &mdash; a "
        "value that would stop the bot starting is refused and nothing is "
        "written. Every change is recorded either way: the exact key, the old "
        "value, the new value, your reason, the objection it made, the YAML "
        "diff and a commit message. Undo one with "
        "<code>electrum-bot settings-revert &lt;id&gt;</code>.</p>"
        '<p class="note" style="max-width:68ch">If that wrapper is not installed '
        "&mdash; on a laptop, or on a box where the grant was never made &mdash; "
        "the change is <b>recorded and not applied</b>, this page says so, and "
        "<code>electrum-bot settings-apply &lt;id&gt;</code> at a root shell "
        "finishes the job.</p>"
    )

    if not enabled:
        return intro + (
            '<div class="banner warn"><b>The forge is off</b>'
            "Set <code>DASHBOARD_CHAT_TOKEN</code> in the environment to enable "
            "it. Off by default on purpose: the rest of this page "
            "<em>displays</em> configuration, while this drives an agent and "
            "writes a request somebody may act on. Switching it on should be a "
            "decision, never a side effect of deploying.</div></section>"
        )

    requestable = [f for f in limits.values() if f.requestable]
    options = "".join(_forge_option(fact, rules) for fact in requestable)

    panel = (
        '<div class="card forge"><h3>Argue a limit</h3>'
        '<div class="field"><label for="fg-key">Which limit</label>'
        f'<select id="fg-key" aria-describedby="fg-note">{options}</select></div>'
        '<div class="field"><label for="fg-val">Proposed value</label>'
        '<input id="fg-val" type="text" inputmode="decimal" '
        'placeholder="a number, in the same units as the current value"></div>'
        '<div class="field"><label for="fg-why">What is this change for</label>'
        '<textarea id="fg-why" rows="3" placeholder="The reason is recorded '
        'with the request and goes into the commit message"></textarea></div>'
        '<div class="composer"><button class="btn" id="fg-ask" type="button">'
        "Ask the Armorer</button></div>"
        '<p class="note" id="fg-note">Asking computes the consequence and '
        "records nothing. A loosening change needs a second, explicit "
        "confirmation after you have read it.</p>"
        '<div id="fg-out" aria-live="polite"></div></div>'
    )

    body = intro + (
        f'<div class="grid g2">{panel}{_forge_reference(limits, rules)}</div>'
        f'<div style="margin-top:1rem">{_forge_requests(requests)}</div>'
        # The window first, so `window.MUDHORN_FORGE` exists by the time the
        # forge's own script wires a button to it. It guards against a double
        # load and interpolates nothing, so it needs no `.replace` and no brace
        # doubling — unlike the script below, which carries the token.
        + FORGE_WINDOW_SCRIPT
        + FORGE_SCRIPT.replace("__TOKEN__", json.dumps(token))
    )

    if not hermes_available:
        body += (
            '<p class="note">The Armorer\'s conversation panel needs Hermes '
            "installed where this process expects it; see "
            "<code>docs/HERMES_SETUP.md</code>. The forge above does not need "
            "it &mdash; the arithmetic is computed in Python, not by a model.</p>"
        )
    else:
        if not soul_found:
            body += (
                '<div class="banner warn"><b>Speaking without a character</b>'
                "<code>souls/armorer.md</code> did not load, so the agent below "
                "answers plainly. It reaches the same figures and is bound by "
                "the same arrangement; only the voice is missing.</div>"
            )
        body += chat_panel(
            token=token,
            soul="armorer",
            who="The Armorer",
            intro="Tell me which limit, and what it is for.",
            placeholder="Ask what a limit is for, or what moving it would cost",
            suggestions=[
                "What would raising max_risk_per_trade_pct to 2% actually cost?",
                "Why is the equity floor where it is?",
                "I have had three losses. Should I loosen the stand-down?",
                "What does raising a per-class limit above the total do?",
                "Which limit is most likely to be the one that refuses a trade?",
            ],
            footnote="It argues, it records, and it applies what you agree to "
            "— through root, never with its own hands. Every figure it quotes "
            "is computed in Python and handed to it; ask it for the arithmetic "
            "and it will name where the number came from.",
            extra_notes=ARMORER_NOTES,
        )
    return body + "</section>"


# Notes specific to the ARMORER, in the shape `ACCOUNT_AGENT_NOTES` established.
# Both describe reach. This one has to say exactly how far the agent's reaches,
# because the obvious reading of a settings agent is that it changes settings —
# which is now true, and was not when this constant was first written.
#
# Raw HTML rather than escaped text, and a module constant for that reason:
# nothing user-supplied reaches it.
ARMORER_NOTES = """
  <p class="note"><b>It records, and it applies.</b> Every change is a row in
  <code>data/settings_requests.db</code> first &mdash; the key, both values,
  your reason and its objection &mdash; and the edit to
  <code>config/rules.yaml</code> is made by root through one wrapper that takes
  no arguments, after the whole file has been re-validated on a staged copy. A
  tightening goes in on the first ask. A loosening waits for you to agree to the
  cost. Check the request list above: it says what was applied, when, and by
  which route.</p>
  <p class="note"><b>It cannot write the file itself, and a failure is not
  silent.</b> If the privileged route is missing, the change is recorded and NOT
  made, and it will tell you so and name the command that finishes it. Take
  "recorded" and "applied" as different words, because they are.</p>
  <p class="note"><b>The arithmetic is not its own.</b> Every consequence is
  computed in <code>src/bot/settings_agent.py</code> against the rules actually
  loaded and handed to it as figures, the same way indicators are computed in
  Python rather than derived by the model. A model asked to work out what
  doubling a cap costs will produce a number, state it confidently, and be
  believed.</p>
"""


def settings_page(
    rules: Rules,
    env: Env,
    *,
    chat_enabled: bool,
    sessions_ahead: Sequence[SessionDayView] = (),
    calendar_loaded: bool = False,
    calendar_degraded: bool = False,
    poller_has_read: bool = False,
    social_recall: NewsRecall | None = None,
    forge_enabled: bool = False,
    token: str = "",
    hermes_available: bool = False,
    soul_found: bool = False,
    limits: dict[str, LimitFact] | None = None,
    rules_path: Path | None = None,
    requests: Sequence[ChangeRequest] = (),
) -> str:
    """Structured, and read-only for every figure that governs risk.

    **The one control on this page cannot change a limit**, and that is a
    deliberate widening of a rule this page used to keep absolutely. A settings
    screen that could widen a cap would be used to widen one during a losing
    run, which is exactly when the cap is doing its job — so the forge below
    argues, computes the arithmetic consequence, and records a CHANGE REQUEST.
    `config/` is root-owned on the box so this process cannot write
    `config/rules.yaml` at all, and applying a request is
    `electrum-bot settings-apply <id>` run as root by a person.

    Everything else here stays display only: the limits are shown with their
    reasoning and with the file that owns them.
    """
    body = head(
        "Configuration",
        "Settings",
        "",
        "What the bot is currently configured to do. The risk limits are shown "
        "read-only on purpose: they change in a commit, by a person, with a "
        "reason. The forge below argues about one and records the request; it "
        "cannot write the file and this process could not if it tried.",
    )

    # Read once. It is a pure property over the environment, but it is asked
    # three times on this page and one call means the three rows cannot disagree
    # about which provider is in force.
    provider = env.inference_provider
    # And redacted once, for the same reason. `DO_INFERENCE_BASE_URL` is the one
    # credential-bearing value on this page that is PRINTED rather than asked
    # about — see `_redact_urls`. Both are taken here so no later row can render
    # the raw pair by reaching for `provider` directly.
    provider_url = _redact_urls(provider.base_url)
    provider_detail = _redact_urls(provider.detail)

    a = rules.account
    # The reasoning beside each figure comes from `settings_agent.limits_for`
    # rather than being written out again here. One story per limit: the same
    # sentence the Armorer argues from is the one the card shows, so a change to
    # what a limit is FOR cannot land in one place and not the other.
    facts = limits or {}

    def _why(key: str, fallback: str = "") -> str:
        fact = facts.get(key)
        return fact.what if fact else fallback

    body += (
        '<section class="block"><h2>Risk limits</h2><div class="grid g2">'
        '<div class="card"><h3>Per trade and across the book</h3><dl class="kv">'
        + _row(
            "Per trade",
            f"{a.max_risk_per_trade_pct:.2f}%",
            _why(
                "account.max_risk_per_trade_pct",
                "Most any single position may lose if its stop fills.",
            ),
        )
        + _row(
            "Total open",
            f"{a.max_total_risk_pct:.2f}%",
            _why(
                "account.max_total_risk_pct",
                "Most everything open may lose at once. Measured in risk, not "
                "notional, which is what makes it leverage-neutral.",
            ),
        )
        + _row(
            "Concentration",
            f"{a.max_position_pct:.1f}%",
            _why(
                "account.max_position_pct",
                "A backstop on one position's market value. Deliberately "
                "generous; it is not meant to be the binding constraint.",
            ),
        )
        + _row(
            "Concurrent positions",
            str(a.max_concurrent_positions),
            _why("account.max_concurrent_positions"),
        )
        + _row(
            "Daily loss kill",
            f"{a.daily_loss_kill_pct:.2f}%",
            _why(
                "account.daily_loss_kill_pct",
                "Sticky for the session. A recovery within the same day does "
                "not re-enable trading.",
            ),
        )
        + _row(
            "Equity floor",
            _money(a.min_equity_floor_usd),
            _why("account.min_equity_floor_usd", "Below this the bot halts."),
        )
        + '</dl><p class="source">Owned by <code>config/rules.yaml</code>, '
        "enforced in <code>src/bot/risk.py</code>.</p></div>"
        '<div class="card"><h3>Anti-churn</h3><dl class="kv">'
        + _row("Trades per day", str(rules.frequency.max_trades_per_day))
        + _row("Trades per week", str(rules.frequency.max_trades_per_week))
        + _row(
            "Per-symbol cooldown",
            f"{rules.frequency.min_seconds_between_trades_per_symbol}s",
            "Prevents flip-flopping on one name.",
        )
        + _row(
            "News blackout",
            f"{rules.news_blackout_minutes_before}m before / "
            f"{rules.news_blackout_minutes_after}m after",
        )
        + '</dl><p class="source">Fees, not stock picking, were the dominant loss '
        "driver in the Alpha Arena competition. These are risk controls.</p></div>"
        '<div class="card"><h3>Stand-down</h3><dl class="kv">'
        + _row("Trigger", f"{rules.stand_down.consecutive_losses_trigger} losses in a row")
        + _row(
            "Scratch threshold",
            f"{rules.stand_down.loss_threshold_r:.2f}R",
            "A smaller loss neither counts toward the streak nor resets it.",
        )
        + _row("First trip", f"{rules.stand_down.stage_one_days} days")
        + _row(
            "Repeat trip",
            f"{rules.stand_down.stage_two_days} days",
            f"if inside {rules.stand_down.repeat_window_days} days of the last one",
        )
        + '</dl><p class="source">Live execution is withheld. Paper trading '
        "continues, and closing a position is never gated.</p></div>"
        '<div class="card"><h3>Margin</h3><dl class="kv">'
        + _row(
            "Buying-power use",
            f"{rules.margin.max_buying_power_utilisation_pct:.1f}%",
            "Share of available buying power one order may consume.",
        )
        + _row(
            "Gross exposure",
            f"{rules.margin.max_gross_notional_pct:.1f}%",
            "Reg T permits 200% overnight; this leaves headroom.",
        )
        + '</dl><p class="source">These replace the Pattern Day Trader rule, which '
        "FINRA retired on 2026-06-04. What applies now is Intraday Margin "
        "Deficit calls.</p></div></div></section>"
    )

    # Directly under the limits it argues about, rather than at the foot of the
    # page. An operator reading a cap and wondering what moving it would cost
    # should not have to scroll past the runtime and the dream schedule to find
    # the thing that answers.
    body += _forge(
        rules,
        facts,
        enabled=forge_enabled,
        token=token,
        hermes_available=hermes_available,
        soul_found=soul_found,
        rules_path=rules_path,
        requests=requests,
    )

    instrument_cards = _calendar_card(
        sessions_ahead,
        loaded=calendar_loaded,
        degraded=calendar_degraded,
        poller_has_read=poller_has_read,
    )
    for name, inst in rules.instruments.items():
        sessions = inst.render_sessions()
        # Shown beside the hours rather than folded into them. The hours alone
        # read as "this is when it trades", and for three quarters of a year
        # that is wrong by two days a week.
        days = ", ".join(DAY_NAMES[d] for d in sorted(inst.session_days_utc) if 0 <= d <= 6)
        instrument_cards += (
            f'<div class="card"><h3>{_e(name)}</h3><dl class="kv">'
            + _row("Enabled", "yes" if inst.enabled else "no")
            + _row("Strategy", inst.strategy)
            + _row("Symbols", ", ".join(inst.allowed_symbols) or "none")
            + _row("Sessions (UTC)", sessions or "none")
            + _row("Trading days", days or "none")
            # Named on its own row rather than folded into the hours, because
            # it is the one session rule the hours cannot express: those are
            # fixed UTC and the US session moves an hour twice a year, so the
            # window permits half an hour of pre-market every winter day.
            + _row(
                "Pre-market",
                "refused (04:00-09:30 New York)"
                if inst.refuse_premarket
                # A 24/7 market has no pre-market, so "permitted" would be
                # answering a question that does not apply — and reads as a
                # gap in the rules rather than as an absent concept.
                else "not applicable (24/7 market)"
                if _is_continuous(inst)
                else "permitted by these hours",
            )
            + (
                _row("Capital cap", f"{inst.capital_cap_pct:.1f}%")
                if inst.capital_cap_pct is not None
                else ""
            )
            # This class's own limits, beside the portfolio ones rather than
            # instead of them. A per-instrument limit OVERRIDES the portfolio
            # one in either direction — the validator that once refused a looser
            # class limit at config load is gone, because refusing to start
            # denies at the least useful moment and offers the operator no way
            # to say "yes, I mean it". So showing both is the only way to read
            # which figure is actually in force, and `_limit_row` says out loud
            # when a class is looser than the default.
            #
            # "portfolio limit" rather than a blank where a class has no
            # opinion: an empty cell reads as "no limit", which is the exact
            # opposite of what an absent override means.
            + _row(
                "Risk per trade",
                _limit_row(
                    inst.max_risk_per_trade_pct,
                    rules.account.max_risk_per_trade_pct,
                    "{:.2f}%",
                ),
            )
            + _row(
                "Max position",
                _limit_row(
                    inst.max_position_pct, rules.account.max_position_pct, "{:.1f}%"
                ),
            )
            + _row(
                "Concurrent positions",
                _limit_row(
                    inst.max_concurrent_positions,
                    rules.account.max_concurrent_positions,
                    "{}",
                ),
            )
            + "</dl>"
            + (
                '<p class="source">Disabled. Its symbols are refused by the gate '
                "and marked watch-only on the tape. These limits are configured "
                "and ready, so enabling it is a one-word edit rather than a "
                "design decision taken at whatever hour it becomes urgent.</p>"
                if not inst.enabled
                else ""
            )
            + "</div>"
        )
    open_now = rules.classes_in_session(datetime.now(UTC))
    skip = rules.loop.skip_model_call_when_all_markets_closed
    body += (
        '<section class="block"><h2>Loop</h2>'
        '<div class="card"><dl class="kv">'
        + _row("Open right now", ", ".join(open_now) or "nothing")
        + _row(
            "Skip the model call when everything is shut",
            "yes" if skip else "no",
        )
        + "</dl>"
        '<p class="source">Not a risk limit. The worst a wrong value here does is '
        "stop the model being asked; it cannot widen what the gate allows. The "
        "open window is derived from the instrument classes below, so enabling a "
        "class that trades at the weekend widens it with nothing else to change. "
        "Owned by config/rules.yaml.</p></div></section>"
    )

    body += (
        '<section class="block"><h2>Instruments</h2>'
        '<p class="note">Each class carries its own session window, symbol list '
        "and strategy. The portfolio limits above stay global.</p>"
        f'<div class="grid g2" style="margin-top:1rem">{instrument_cards}</div></section>'
    )

    # Credentials are reported as present or absent, never shown. This page is
    # loopback-bound, but a screenshot is not.
    body += (
        '<section class="block"><h2>Runtime</h2><div class="grid g2">'
        '<div class="card"><h3>Execution</h3><dl class="kv">'
        + _row(
            "Mode",
            "paper" if env.alpaca_paper_trade else "LIVE",
            "The code refuses to start twice over if this is ever false.",
        )
        + _row("Decision interval", f"{env.decision_interval_seconds}s")
        # The model, not the tier. A tier could only ever name one of three
        # Claude strings, so a page reporting one was answering a narrower
        # question than the one being asked the moment any other model became
        # nameable — and would keep saying "haiku" while DECISION_MODEL_ID sent
        # something else entirely.
        + _row(
            "Decision model",
            env.decision_spec.model_id,
            "What goes on the wire. CLAUDE_TIER picks one of three Claude "
            "models; DECISION_MODEL_ID names any model outright and overrides "
            "it. A model with no prices on file still runs — its calls report "
            "an unknown cost rather than a zero.",
        )
        # Which endpoint answers, and the half of it nobody could infer. A
        # provider swap leaves no trace in a figure, so a page reporting only
        # the model id would say `deepseek-v4-pro` and leave "where does that
        # run, and is the schema still enforced" unanswerable from any surface.
        # `provider.detail` is written to be printed and never contains the KEY
        # — but it embeds the base URL, which is operator-supplied and may carry
        # a credential of its own. `_redact_urls` is what keeps the promise in
        # the sentence above this card: reported, never rendered.
        + _row(
            "Inference",
            provider_url if provider.is_digitalocean else "Anthropic direct",
            provider_detail,
        )
        # **This row reports the CONFIGURATION and must not state what any
        # command does about it.** It used to end "Model calls refuse rather
        # than falling back to the other provider", and that is false in one of
        # the two unusable states: with a base URL set and no key, `usable` is
        # False, this row renders — and `provider_is_unusable` answers None, so
        # the decision loop runs and its calls go to Anthropic. Measured on both
        # states rather than reasoned about.
        #
        # The guards are per entry point in `main.py` and differ on purpose — a
        # cycle with no model call still reconciles the journal and runs
        # `stop_watch`, which is worth more than the proposal — so a single
        # sentence here cannot be true of all of them, and a page restating
        # another module's control flow drifts the first time that flow changes.
        # Same rule as the shelf card refusing to state `cycles_available`: name
        # what is establishable from here and say where the rest lives.
        + (
            ""
            if provider.usable
            else _row(
                "Provider state",
                "UNUSABLE",
                "Configured and wrong: these settings do not describe an "
                "endpoint this process can talk to. What each command does "
                "about that is decided in main.py and differs by entry point, "
                "so the log line a command writes is where that shows and this "
                "row does not guess at it. Read the sentence above for which "
                "half of the configuration is wrong.",
            )
        )
        + '</dl><p class="source">Whether orders are actually placed is decided by '
        "the <code>--execute</code> flag on the service unit, not from here.</p></div>"
        '<div class="card"><h3>Credentials and feeds</h3><dl class="kv">'
        + _row("Alpaca", "configured" if env.alpaca_api_key else "not configured")
        # The row names the variable the configured provider actually reads. It
        # said "Anthropic" against `ANTHROPIC_API_KEY` unconditionally, which
        # became a plausible wrong answer the moment a second endpoint existed:
        # on a DigitalOcean box it reported the key nothing uses and stayed
        # silent about the one everything depends on.
        + _row(
            provider.credential_name,
            "configured" if env.model_api_key else "not configured",
            "The credential the Python model path authenticates with. It "
            "follows the provider above; the other one is not read and not "
            "reported here.",
        )
        + _row(
            "Finnhub",
            "configured" if env.finnhub_api_key else "not configured",
            "Feeds the earnings blackout. Without it that rule cannot fire.",
        )
        + _row(
            "Marketaux",
            "configured" if env.marketaux_api_key else "not configured",
            "Headlines only. Gates nothing.",
        )
        # The X feed used to be one row here and is a card of its own beside
        # this one now. A single "configured / not configured" cell could not
        # carry the distinctions that matter for this feed — off, enabled
        # without a token, and reading-but-degraded are three different states
        # and only the third is a fault.
        + _row("Dashboard chat", "on" if chat_enabled else "off")
        + '</dl><p class="source">Presence only. No key is rendered on this page, '
        "on any surface, at any time.</p></div>"
        + _social_card(
            social_state(rules, env, social_recall),
            window_hours=(
                social_recall.window_hours if social_recall else NEWS_WINDOW_HOURS
            ),
        )
        + "</div></section>"
    )

    # The dreamer is scheduled outside this process, so what it says here is
    # read from the unit rather than quoted from a constant. A Settings screen
    # holding its own copy of a cadence keeps announcing the old one forever
    # after somebody edits the timer on the box.
    schedule = read_schedule()

    # **Absent rather than zero.** `estimated_cost_usd` answers `None` for a
    # model nobody has prices on file for, and `~$0.000 a run, ~$0 a year` would
    # be the cheapest thing on this page while being a figure nobody measured.
    # The row states which model it could not price, because "unknown" with no
    # subject sends an operator looking in the wrong place.
    dream_spec = env.dream_spec
    dream_estimate = estimated_cost_usd(dream_spec)
    if dream_estimate is None:
        dream_cost_row = _row(
            "Estimated cost",
            "unknown",
            f"No prices are on file for {dream_spec.model_id}, so a run's "
            "cost cannot be computed. It is added to config.MODEL_SPECS in a "
            "commit, exactly as a limit is added to config/rules.yaml — an "
            "estimate typed into the environment would be a figure with no "
            "review behind it.",
        )
    else:
        per_run, per_year = dream_estimate
        dream_cost_row = _row(
            "Estimated cost",
            f"~${per_run:.3f} a run, ~${per_year:.0f} a year",
            "An estimate from the model's prices and a typical prompt. The real "
            "figure for a run that happened is logged with it.",
        )
    body += (
        '<section class="block"><h2>Dreaming</h2><div class="grid g2">'
        '<div class="card"><h3>Schedule</h3><dl class="kv">'
        + _row(
            "Timer",
            _e(schedule.calendar) if schedule.calendar else "no OnCalendar line",
            "New Zealand time, named rather than converted, so the hour does "
            "not drift when daylight saving starts and ends.",
        )
        + _row(
            "Unit",
            schedule.state,
            # The distinction that matters and the one a file check cannot make.
            "Whether the timer is ENABLED is not visible from this process. "
            "A unit on disk is not a running schedule; check "
            "systemctl list-timers.",
        )
        + _row(
            "Next thought",
            "not scheduled from here",
            "The loop and the dreamer are separate units on purpose: one wakes "
            "every few minutes, the other once a day.",
        )
        + '</dl><p class="source">Owned by '
        "<code>deploy/systemd/mudhorn-dream.timer</code>.</p></div>"
        '<div class="card"><h3>The call</h3><dl class="kv">'
        + _row(
            "Model",
            dream_spec.model_id,
            "Set by DREAM_MODEL_ID, or by DREAM_CLAUDE_TIER when no id is "
            "named. It deliberately does not follow CLAUDE_TIER, because that "
            "defaults to a tier with no extended thinking and thinking is how "
            "a dream gets past its first hop.",
        )
        + _row(
            "Bought",
            "deep, not fast" if dream_spec.sends_anthropic_thinking else "plain",
            (
                "High effort, a large thinking budget and a 240s timeout. "
                "Nothing waits on this call and depth is the whole product."
                if dream_spec.sends_anthropic_thinking
                else "This model is not sent a thinking budget or an effort "
                "setting, so the depth a dream needs has to come from the "
                "model itself. The budget and the 240s timeout still apply."
            ),
        )
        # The dream half of this is a decision made HERE and is true at either
        # endpoint: nothing sends `cache_control` on a dream call, so a daily
        # run cannot pay for a write it would always miss.
        #
        # The sentence about the LOOP is a claim about the far end, and it is
        # only established for one of the two. Measured against DigitalOcean
        # serverless with the real 4,791-token system block, sent twice eight
        # seconds apart on two models: HTTP 200 both times, and
        # `cache_creation_input_tokens`, `cache_read_input_tokens` and
        # `ephemeral_1h_input_tokens` all nought, with `input_tokens` identical
        # on the repeat. That is the `output_config` shape again — accepted and
        # ignored — and it is the expensive direction, because the loop wakes 96
        # times a day.
        #
        # **What is NOT claimed is that the endpoint does not cache.** A nought
        # is equally what a proxy that caches and does not report it looks like,
        # and only the billing dashboard separates those. Same rule as
        # `calendar_degraded` and the tailnet `unknown` status: say the weaker
        # true thing rather than the confident one.
        + _row(
            "Prompt cache",
            "off",
            "Deliberate. A 1h cache write bills at twice base input and this "
            "runs daily, so it would miss every time and pay double. "
            + (
                "The decision loop ASKS for one because it wakes every fifteen "
                "minutes, and whether this endpoint honours it is unestablished: "
                "cache_control is not in DigitalOcean's published request "
                "schema, it is accepted with a 200, and every reply measured so "
                "far reported nought cached — read and written both. A nought "
                "is not proof the endpoint did not cache, only that it reported "
                "none, and the billing dashboard is what settles that. Until it "
                "does, budget for the loop's system prompt being billed in full "
                "on all 96 cycles a day; cached_tokens on the cycle_complete "
                "line is the reading from here."
                if provider.is_digitalocean
                else "The decision loop caches because it wakes every fifteen "
                "minutes, and Anthropic reports the hits: cached_tokens on the "
                "cycle_complete line is what confirms it is working."
            ),
        )
        + dream_cost_row
        + _row(
            f"{provider.credential_name}",
            "configured" if env.model_api_key else "not configured",
            (
                ""
                if env.model_api_key
                else "Without it the command exits non-zero and writes nothing."
            ),
        )
        # Named here as well as in Runtime, because this card is read on its own
        # by somebody asking why the dream timer produced nothing this morning,
        # and "which key" is the first thing they need.
        + _row(
            "Endpoint",
            provider_url if provider.is_digitalocean else "Anthropic direct",
            "Where the dream call goes. The schema is enforced by this process "
            "rather than by the endpoint when that is DigitalOcean, so a reply "
            "carrying no tool call fails the run instead of half-filling it."
            if provider.is_digitalocean
            else "The schema is enforced server-side here.",
        )
        + '</dl><p class="source">Nothing here reaches the broker. A dream '
        "carries no quantity, entry, stop or side, so it cannot describe an "
        "order at all.</p></div></div></section>"
    )
    return body


# --------------------------------------------------------------------- chat


def chat_page(*, enabled: bool, token: str, hermes_available: bool) -> str:
    body = head(
        "Command centre",
        "Chat",
        "",
        "Hermes reaches the bot through the MCP tools, so every order-placing "
        "tool behind this re-runs the risk gate first. It answers questions and "
        "cannot route around anything.",
    )

    if not enabled:
        return body + (
            '<div class="banner warn" style="margin-top:1.5rem"><b>Chat is off</b>'
            "Set <code>DASHBOARD_CHAT_TOKEN</code> in the environment to enable it. "
            "This is off by default on purpose: the rest of this dashboard only "
            "<em>displays</em> an account, while chat can <em>drive an agent</em> "
            "that reaches the broker. Exposure here risks action rather than "
            "disclosure, so switching it on should be a decision and never a side "
            "effect of deploying.</div>"
        )

    if not hermes_available:
        return body + (
            '<div class="banner crit" style="margin-top:1.5rem"><b>Hermes not found</b>'
            "The token is set, but the Hermes binary is not installed where this "
            "process expects it. See <code>docs/HERMES_SETUP.md</code>.</div>"
        )

    return body + chat_panel(
        token=token,
        soul="yoda",
        who="Yoda",
        # Deliberately one line. What this used to say is already said twice
        # on the page: the note above states what Hermes reaches and that it
        # cannot route around the gate, and the buttons below are a better list
        # of what to ask than a sentence describing one.
        intro="Active. Ask away.",
        placeholder="Ask about the account, a trade, a rejection, or the news",
        suggestions=[
            "What is my open risk right now, and how close is it to the cap?",
            "Why was the last proposal rejected?",
            "What news has the bot seen today, and how old is it?",
            "Which rejection reason has fired most often, and on what?",
            "How many times have we watched a symbol without naming a trigger?",
            "Summarise this week's trades and what closed them.",
            "Is anything expiring soon that needs action?",
        ],
        footnote="Turns are replayed for continuity but this is not a long-lived "
        "session. Hermes keeps its own memory; the dashboard does not.",
        extra_notes=ACCOUNT_AGENT_NOTES,
    )


# Notes specific to the ACCOUNT agent, not the dreamer. Both describe reach:
# what Yoda can and cannot see. Grogu has neither the news recording nor the
# insight index, so putting these on the shared panel would describe tools the
# dreamer does not have.
#
# Raw HTML rather than escaped text, and it is a module constant for that
# reason: nothing user-supplied reaches it.
ACCOUNT_AGENT_NOTES = """
  <p class="note"><b>News here is a recording, not a search.</b> Hermes has no
  web access, deliberately. What it can read is what the trading loop was shown
  and wrote down each cycle &mdash; headlines, posts from watched accounts and
  earnings windows &mdash; with the age of each attached. Asking does not fetch
  anything: the Marketaux free tier is 100 requests a day against a loop that
  wakes 96 times, so that quota belongs to the loop.</p>
  <p class="note"><b>The whole history is searchable, not just recent days.</b>
  Every cycle, assessment, rejection reason and headline is indexed into
  <code>data/insight.db</code>, and Hermes queries it with read-only SQL. So
  &ldquo;what did we decide about AAPL in March&rdquo; and &ldquo;which rule
  refuses proposals most often&rdquo; are answerable. The index is derived from
  <code>audit/</code> and rebuilt from it, so the log stays the record and the
  index is only ever a faster way to read it.</p>
"""


def chat_panel(
    *,
    token: str,
    soul: str,
    who: str,
    intro: str,
    placeholder: str,
    suggestions: list[str],
    footnote: str,
    avatar: bool = False,
    extra_notes: str = "",
) -> str:
    """One conversation panel, used by both agents.

    Shared rather than copied because the two differ only in which character
    answers. The transport, the token, the history replay and the error handling
    are one implementation, so a fix to any of them cannot reach one agent and
    miss the other.

    `soul` travels in the request body and is the only thing that picks the
    character. It is validated server-side against a fixed set, so a value typed
    into the console cannot name an arbitrary file.

    `extra_notes` is raw HTML for things true of ONE agent's reach — the account
    agent's news recording and searchable history, which the dreamer does not
    have. Describing them on the shared panel would credit the dreamer with
    tools it cannot reach.
    """
    chips = "".join(
        f'<button type="button" data-q="{_e(q)}">{_e(q)}</button>' for q in suggestions
    )
    return f"""
<div class="chat" style="margin-top:1.5rem">
  <div class="log" id="log" aria-live="polite" aria-label="Conversation">
    <div class="turn agent"><span class="who">{_e(who)}</span>
      <div class="msg">{_e(intro)}</div></div>
  </div>
  <div class="prompts">{chips}</div>
  <div class="composer">
    <textarea id="msg" rows="2" placeholder="{_e(placeholder)}"
      aria-label="Message" aria-describedby="key-hint"></textarea>
    <button class="btn" id="send" type="submit">Send</button>
  </div>
  <p class="note" id="key-hint"><b>Enter</b> sends. <b>Ctrl+Enter</b> or
  <b>Shift+Enter</b> for a new line.</p>
  <p class="note">{_e(footnote)}</p>{extra_notes}
</div>
<script>
(function () {{
  var TOKEN = {json.dumps(token)};
  var SOUL = {json.dumps(soul)};
  var WHO = {json.dumps(who)};
  var AVATAR = {json.dumps(bool(avatar))};
  var log = document.getElementById('log');
  var box = document.getElementById('msg');
  var send = document.getElementById('send');
  var face = AVATAR ? document.querySelector('.dreamer') : null;
  var history = [];

  function turn(who, text, cls) {{
    var el = document.createElement('div');
    el.className = 'turn ' + (cls || '');
    var w = document.createElement('span'); w.className = 'who'; w.textContent = who;
    var m = document.createElement('div'); m.className = 'msg'; m.textContent = text;
    el.appendChild(w); el.appendChild(m); log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return m;
  }}

  function ask() {{
    var text = box.value.trim();
    if (!text) return;
    box.value = '';
    turn('You', text, 'user');
    var pending = turn(WHO, 'Thinking', 'agent');
    pending.appendChild(dots());
    send.disabled = true;
    /* Means "a request is open", never "an idea is forming". The avatar must
       not imply the agent is doing something it is not. */
    if (face) face.classList.add('thinking');

    fetch('/chat', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ token: TOKEN, message: text, history: history, soul: SOUL }})
    }})
      .then(function (r) {{ return r.json(); }})
      .then(function (d) {{
        if (d.ok) {{
          pending.textContent = d.text;
          history.push({{ user: text, agent: d.text }});
        }} else {{
          pending.parentElement.className = 'turn err';
          pending.textContent = d.error || (WHO + ' returned nothing.');
        }}
      }})
      .catch(function (e) {{
        pending.parentElement.className = 'turn err';
        pending.textContent = String(e);
      }})
      .finally(function () {{
        send.disabled = false;
        if (face) face.classList.remove('thinking');
        box.focus();
      }});
  }}

  function dots() {{
    // Elements rather than text, so the animation is CSS. `turn()` sets
    // textContent, so this is appended afterwards; when the reply arrives it
    // overwrites textContent and takes the dots with it.
    var s = document.createElement('span');
    s.className = 'dots';
    s.setAttribute('aria-hidden', 'true');
    for (var i = 0; i < 3; i++) s.appendChild(document.createElement('i'));
    return s;
  }}

  function newlineAtCaret() {{
    // Ctrl/Cmd+Enter does NOT insert a newline in a textarea by default, so
    // taking it over for that means putting one in by hand and leaving the
    // caret after it. Shift+Enter needs none of this: the browser already
    // inserts one, so that path just returns and lets the default run.
    var start = box.selectionStart, end = box.selectionEnd;
    box.value = box.value.slice(0, start) + '\\n' + box.value.slice(end);
    box.selectionStart = box.selectionEnd = start + 1;
  }}

  send.addEventListener('click', ask);
  box.addEventListener('keydown', function (e) {{
    if (e.key !== 'Enter') return;
    // Enter also confirms a candidate in an IME. Sending on it would swallow
    // the word being composed rather than the message.
    if (e.isComposing || e.keyCode === 229) return;
    if (e.ctrlKey || e.metaKey) {{ e.preventDefault(); newlineAtCaret(); return; }}
    if (e.shiftKey) return;
    e.preventDefault();
    ask();
  }});
  document.querySelectorAll('.prompts button').forEach(function (b) {{
    b.addEventListener('click', function () {{ box.value = b.dataset.q; ask(); }});
  }});
}})();
</script>"""


# ----------------------------------------------------------------- dreaming


def _hop(hop: Hop, *, shared: bool = False, weakest: bool = False) -> str:
    """One link, whether anybody checked it, and what it is to this chain.

    `shared` marks a claim two fused parents had in common; `weakest` marks the
    hop the dreamer named as the one that could kill it. **Both can be true of
    one hop**, which is why they are ATTRIBUTES rather than second classes:
    `checked` and `open` already occupy that slot, and stacking more words there
    is the collision this stylesheet has lost three times.

    The labels are real elements rather than `::after` content, for two reasons
    that point the same way. Generated content is announced inconsistently by
    screen readers and by some not at all — the limit this stylesheet already
    states about `td[data-l]::before` — and one pseudo-element cannot carry two
    labels, so a hop that was both would silently show one.
    """
    state = "checked" if hop.checked else "open"
    source = (
        f'<span class="src">{_e(hop.source)}</span>'
        if hop.checked and hop.source
        else '<span class="src">Not checked. Nobody has verified this hop.</span>'
    )
    marks = (' data-shared=""' if shared else "") + (
        ' data-weakest=""' if weakest else ""
    )
    tags = ""
    if shared:
        tags += '<i data-tag="shared">shared with both parents</i>'
    if weakest:
        tags += '<i data-tag="weakest">the weakest link</i>'
    if tags:
        tags = f'<span class="hoptags">{tags}</span>'
    return f'<li class="{state}"{marks}>{_e(hop.claim)}{source}{tags}</li>'


def _chain_diagram(dream: Dream) -> str:
    """The chain drawn as a chain, so a reader can SEE where it breaks.

    The ordered list below this says the same thing in words and is what anyone
    actually reads. This is the shape of the argument at a glance, and it earns
    its place by making one property visible that prose buries: a causal chain
    is only as strong as its weakest link, and an unverified hop is a break in
    it rather than a slightly weaker section.

    So a connector into an unchecked hop is drawn BROKEN. Not thinner, not a
    different colour with the same continuity — actually discontinuous, because
    that is what an unverified link does to an argument. A reader who takes
    nothing else from this page should take away that the line stops.

    `role="img"` with a spoken summary, because everything here is also stated
    in the list underneath and a screen reader should get the summary rather
    than thirty positioned circles.
    """
    hops = dream.chain
    if not hops:
        return ""

    # READ, never recomputed. Same rule as the verification badge: a page that
    # worked out its own answer would disagree with `promotion_for`, and the
    # rule is the one that decides which shelf this sits on.
    weakest = dream.resolved_weakest_hop

    # Geometry in a fixed viewBox, scaled by CSS. Nodes are evenly spaced and
    # the whole thing is one row: a chain that wrapped would stop reading as a
    # sequence, which is the only thing this drawing is for.
    node_r = 13.0
    gap = 104.0
    left = 26.0
    width = left * 2 + gap * max(len(hops) - 1, 1)
    height = 62.0

    parts: list[str] = []
    for i, hop in enumerate(hops):
        x = left + gap * i
        if i:
            # The connector belongs to the hop it arrives AT: it is that hop's
            # claim that is or is not established.
            prev_x = left + gap * (i - 1)
            x1 = prev_x + node_r + 5
            x2 = x - node_r - 5
            if hop.checked:
                parts.append(
                    f'<line class="link solid" x1="{x1:.0f}" y1="26" '
                    f'x2="{x2:.0f}" y2="26"/>'
                )
            else:
                # Drawn as two stubs with a visible gap, rather than a dashed
                # line. A dash still reads as continuous; a gap does not.
                mid = (x1 + x2) / 2
                parts.append(
                    f'<line class="link broken" x1="{x1:.0f}" y1="26" '
                    f'x2="{mid - 9:.0f}" y2="26"/>'
                    f'<line class="link broken" x1="{mid + 9:.0f}" y1="26" '
                    f'x2="{x2:.0f}" y2="26"/>'
                    f'<text class="gapmark" x="{mid:.0f}" y="30">?</text>'
                )
        state = "checked" if hop.checked else "open"
        # The named weakest link, ringed. An ATTRIBUTE rather than a `weak`
        # class, because `.weak` is already a bare layout rule for the box
        # underneath — `.node.weak` would put that word into the modifier
        # vocabulary and the padded amber panel would style an SVG circle.
        weak = ' data-weakest=""' if weakest == i + 1 else ""
        parts.append(
            f'<circle class="node {state}"{weak} cx="{x:.0f}" cy="26" '
            f'r="{node_r:.0f}"/>'
            f'<text class="idx" x="{x:.0f}" y="31">{i + 1}</text>'
        )

    checked = sum(1 for h in hops if h.checked)
    named = (
        f" Hop {weakest} is named as the weakest link."
        if weakest is not None
        else ""
    )
    label = (
        f"A chain of {len(hops)} hops, of which {checked} are checked.{named} "
        "A broken connector marks a hop nobody has verified."
    )
    return (
        f'<svg class="chainviz" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'preserveAspectRatio="xMinYMid meet" role="img" '
        f'aria-label="{_e(label)}">' + "".join(parts) + "</svg>"
    )


def _crest(dream_id: int, titles: Mapping[int, str]) -> str:
    """One parent of a fusion, LINKED and rendered alive.

    Nothing was consumed to make the child — `DreamStore.fuse` says so in its
    first line and the parents keep everything they had. So a crest is a link to
    a dream still sitting on its own shelf, never a strikethrough and never
    greyed: a visual that implied the parents were eaten would be describing a
    store that does not work that way.

    A title this page has not loaded degrades to the id alone. The id is the
    fact; the title is the convenience.
    """
    title = titles.get(dream_id, "")
    label = f"#{dream_id} {title}" if title else f"#{dream_id}"
    return f'<a class="crest" href="#dream-{dream_id}">{_e(label)}</a>'


def _symbiosis(dream: Dream, titles: Mapping[int, str]) -> str:
    """The crests, and the claim the parents had in common.

    The shared hop leads, because it is the reason the fusion exists at all. A
    fused card that buried it under the chain would be asking a reader to spot
    the overlap by holding two arguments side by side, which is the work the
    fusion was supposed to have already done.
    """
    if not dream.is_fusion:
        return ""

    crests = '<span class="join">+</span>'.join(
        _crest(pid, titles) for pid in dream.parents
    )
    out = (
        '<div class="crests"><span class="sigil"><i></i>fused</span>'
        + crests
        + "</div>"
    )

    shared = "".join(f"<p>{_e(claim)}</p>" for claim in dream.shared_hops)
    if shared:
        out += (
            '<div class="symbiosis"><b>The hop they both stand on</b>'
            + shared
            + "<p><small>Both parents are unchanged and still on their own "
            "shelves. This is a third chain that carries the pair.</small></p>"
            "</div>"
        )
    else:
        # `plan_fusion` can produce a child with no overlap recorded. Saying so
        # is better than a heading with nothing under it: the shared hop is the
        # argument, and its absence is worth reading.
        out += (
            '<div class="symbiosis"><b>The hop they both stand on</b>'
            "<p>None recorded. These two were tied together without a claim in "
            "common on file, so the reason lives in the working below rather "
            "than in a shared link.</p></div>"
        )
    return out


def _verification_note(dream: Dream) -> str:
    """Why the badge reads the way it does, when a fusion is what capped it.

    The badge itself is `dream.verification` and is never recomputed here. A
    card that counted `checked` flags for itself would show a better badge than
    the dream claims — the union of two chains can hold checked hops from one
    parent and unchecked ones from the other, which counts PARTIAL even when a
    parent was UNVERIFIED. Two unverified chains must not make a sourced one.
    """
    ceiling = dream.verification_ceiling
    if ceiling is None:
        return ""
    return (
        '<p class="note" style="margin-top:.75rem">Capped at the weakest parent: '
        f"no better than <b>{_e(str(ceiling))}</b>. Combining two arguments is "
        "not evidence for either of them.</p>"
    )


def _conditions(dream: Dream, now: datetime) -> str:
    """What was pre-registered, how much the world has settled, and what it pins.

    `conditions_met` of `len(conditions)` — a count, never a summary. A fusion
    inherits every condition both parents carried, so it is HARDER to promote
    than either of them, and the sentence says that rather than letting a long
    unmet list read as a failing.

    **Which hop a condition settles is rendered, and "none yet" is rendered
    too.** That is the actionable state: the prophecy shelf is for a dream
    parked awaiting the link that could kill it, so a threshold on a link
    nobody doubted grades cleanly and settles nothing. An operator looking at a
    finished `keep` stuck on the workbench has to be able to see which of its
    conditions bears on the weakest hop and that none of them does.

    Nothing here is graded on this page: `grade_conditions` settles them against
    the figures the decision loop recorded, and this renders what it wrote.

    **There are TWO shapes of pre-registration and this line used to know about
    one.** Anything without a number rendered as *"No number in this one, so
    nothing can settle it"*, which stopped being true the moment an observation
    — a subject, a claim and a review date, settled by the operator — became a
    way onto the prophecy shelf. That sentence would now tell an operator a
    claim addressed to THEM was unsettleable, which is the confident wrong
    answer this repository exists to refuse, arriving as page copy.

    So the two shapes are rendered apart, and the sentence about nothing being
    able to settle it is kept for the case where it is still true.
    """
    if not dream.has_conditions:
        return ""

    weakest = dream.resolved_weakest_hop

    def row(cond: DreamCondition) -> str:
        state = cond.state(now)
        # Both attributes. `data-met` is what the existing markers and tests
        # read; `data-state` is what tells RULED_OUT apart from nobody having
        # looked, which a boolean structurally cannot.
        met = ' data-met=""' if cond.fulfilled else ""
        marks = f'{met} data-state="{state.value}"'
        if cond.is_checkable:
            threshold = (
                f'<span class="thr">{_e(cond.symbol)} {_e(cond.field)} '
                f"{_e(cond.op)} {cond.value:g}</span>"
            )
        elif cond.is_observable:
            # The date is the actionable half — it is what puts the claim on
            # somebody's list — so it is stated rather than implied, and an
            # elapsed one is coloured rather than described, for the same
            # reason `_wake` colours a missing symbol: "overdue" written into
            # the sentence reads as part of the claim.
            due = _e(cond.observe_by.strftime("%d %b %Y")) if cond.observe_by else ""
            when = (
                f'<span class="alert">review was due {due}</span>'
                if state is ConditionState.OVERDUE
                else f"review by {due}"
            )
            threshold = (
                f'<span class="thr">Look at {_e(cond.subject)} — '
                f"{_e(cond.observable)}; {when}. Only the operator settles "
                f"this.</span>"
            )
        else:
            threshold = (
                '<span class="thr">Nothing was pre-registered here — no '
                "threshold and no observation — so nobody can settle it.</span>"
            )
        if not cond.settles_hops:
            pin = '<span class="thr">Settles no hop yet.</span>'
        else:
            named = ", ".join(str(h) for h in cond.settles_hops)
            kills = (
                " — the weakest link"
                if weakest is not None and cond.settles(weakest)
                else ""
            )
            pin = (
                f'<span class="thr">Pinned to '
                f"{_word(len(cond.settles_hops), 'hop')} {_e(named)}"
                f"{kills}.</span>"
            )
        return f"<li{marks}>{_e(cond.text)}{threshold}{pin}</li>"

    met = dream.conditions_met
    total = len(dream.conditions)
    head = (
        f'<p class="condhead">{met} of {total} '
        f"{_word(total, 'condition')} met</p>"
    )
    if dream.is_fusion:
        head += (
            '<p class="note">A fusion carries every condition both parents '
            "wrote, so it has more to clear than either of them did.</p>"
        )
    return head + '<ul class="conds">' + "".join(row(c) for c in dream.conditions) + "</ul>"


def _stuck(dream: Dream) -> str:
    """Why a finished `keep` is still on the workbench, in the rule's own words.

    A dream that has been attacked, kept, and will not promote reads as broken
    rather than incomplete unless the page says which clause is holding it. The
    sentence is `promotion_for(dream).reason` verbatim — it names the hop,
    quotes the claim and says what to change — and paraphrasing it here would
    put a second, shorter account of the rule beside the rule, which is how the
    two drift apart.

    Rendered only for a KEEP on the workbench that is not moving. Every other
    "stays put" is an ordinary state the stage pill already reports: a dream
    still being worked has no verdict, and a fusion is refused on the verdict
    clause before the weakest-hop one is ever reached — showing it here would
    read as a defect in something whose whole state is "nobody has attacked
    this yet".
    """
    if dream.vault is not Vault.WORKBENCH or dream.verdict is not DreamVerdict.KEEP:
        return ""
    promotion = promotion_for(dream)
    if promotion.moves:
        return ""
    return (
        '<p class="notyet"><b>Not a prophecy yet</b>'
        f"{_e(promotion.reason)}</p>"
    )


def _wisp_and_grant(dream: Dream, adoptions: Sequence[Adoption], now: datetime) -> str:
    """What the dreamer kept, and what the account may trade because of it.

    Two different facts, and only one of them is a permission. The wisp is a
    trace — the chain, the conditions and the thoughts are all still on the row,
    nothing was destroyed to produce it — and it is what stops an adopted dream
    occupying a slot in Grogu's head. The grant is the live thing.

    The motes are `dream_fx`'s, on the wisp and nowhere else on this card: this
    is the one paragraph on the page describing something that has been let go
    of, and it should look like it.
    """
    out = ""
    if dream.wisp:
        out += (
            '<div class="wisptrace wisped"><span class="wisps"><b></b><b></b>'
            "<b></b><b></b></span><b>What Grogu kept</b>"
            f"{_e(dream.wisp)}</div>"
        )

    for adoption in adoptions:
        live = adoption.is_live(now)
        symbols = ", ".join(adoption.symbols_granted) or "nothing"
        when = (
            f"expires {_when(adoption.expires_at)}"
            if adoption.expires_at
            else "no expiry recorded, which grants nothing"
        )
        if adoption.returned_at is not None:
            state = f"handed back {_when(adoption.returned_at)}"
        elif live:
            state = when
        else:
            state = f"lapsed — {when}"
        out += (
            '<p class="grantline"><span class="lbl">Granted</span>'
            f'<span class="sym">{_e(symbols)}</span> under '
            f"{_e(adoption.asset_class or 'no class, so nothing is permitted')}"
            f' <span class="muted">({_e(state)})</span></p>'
        )
        if adoption.return_reason:
            out += (
                '<p class="note">Handed back because: '
                f"{_e(adoption.return_reason)}</p>"
            )
    return out


SPEAKER_NAMES = {
    "dreamer": "Grogu",
    "trader": "The trading agent",
    "operator": "You",
    FUSION: "The store",
}


def _transcript(messages: Sequence[DreamMessage]) -> str:
    """The two agents talking about one dream, in the order they said it.

    Oldest first, because a negotiation read newest-first is a negotiation read
    backwards. Exchanges that ended in nothing are here too: a dream the trader
    kept declining is a fact about the dreamer worth having, and a transcript
    that kept only the successes would be a sales record.

    `speaker` and `kind` are open strings by design, so an unfamiliar one costs
    a badge rather than a turn — the name falls back to the raw word.
    """
    if not messages:
        return ""

    rows = ""
    for m in messages:
        who = m.speaker.lower()
        name = SPEAKER_NAMES.get(who, m.speaker or "unattributed")
        rows += (
            f'<li class="said" data-who="{_e(who)}"><div class="hdr">'
            f'<span class="name">{_e(name)}</span>'
            f'<span class="kind">{_e(m.kind)}</span>'
            f'<span class="at">{_e(_when(m.at))}</span></div>'
            f"<p>{_e(m.text)}</p></li>"
        )
    return (
        '<details class="talk"><summary>What was said about it, '
        f"{_count(len(messages), 'turn')}</summary><ol>{rows}</ol></details>"
    )


# ----------------------------------------------------- what they settled on


#: What each verdict is CALLED on the page, and what being it MEANS.
#:
#: **The word in the badge is the act, never the machine value**, because a badge
#: is a heading and a heading is a claim. A pill reading `archive` says the
#: conference archived the dream; it did not, and it could not — no verdict here
#: is the trading agent's route to one, and the party with a route to the broker
#: recommending that a chain be destroyed would be pressure rather than an
#: opinion. The raw value travels in `data-verdict`, where the stylesheet and the
#: tests read it and no reader mistakes it for a sentence.
#:
#: **The word names the DECISION and never the outcome**, which is the same
#: separation `ConferenceDecision` is built around: a promote whose adoption the
#: store refused was badged `adopted` in the first draft, over a row that said
#: two lines lower that the shelf was full and nothing had moved. So it reads
#: `agreed to adopt`, which is true whether or not the shelf had room, and
#: `effected` is what says which — see `_conference_effect`. Found by looking at
#: the page, which is the fourth time that has been the only way.
#:
#: `NO_DECISION` is deliberately absent. It is not a decision, so it does not get
#: a decision's sentence — `_conference_undecided` renders it, and the cause is
#: what carries the meaning there.
CONFERENCE_WORDS: dict[ConferenceVerdict, tuple[str, str]] = {
    ConferenceVerdict.PROMOTE: (
        "agreed to adopt",
        "The trading agent took it out of the vault. What that buys is small: "
        "permission to consider its symbols for a while, under the resolved "
        "class's own limits, with every gate in <code>risk.py</code> still "
        "running on anything traded under it.",
    ),
    ConferenceVerdict.DEFER: (
        "deferred",
        # Deliberately does NOT promise the condition below. It used to end
        # "and what would restart the conversation is written below with a
        # number in it", which on an incomplete row was a sentence contradicted
        # four lines under it by the card's own alert. `_conference_wake` heads
        # the condition itself, so the promise is made by the thing that keeps
        # it.
        "The trading agent parked it against a named condition rather than "
        "putting it down. It stays in the vault.",
    ),
    ConferenceVerdict.DECLINE: (
        "declined",
        "The trading agent will not take it, and said why. A judgement on the "
        "merits rather than a wait, so nothing is pending: the dream stays in "
        "the vault and the dreamer may rework the chain and offer it again.",
    ),
    ConferenceVerdict.ARCHIVE: (
        "the dreamer withdrew it",
        "Grogu took its own dream off the table. This was not done TO the dream "
        "by the conference: the trading agent has no archive verb, may only "
        "adopt out of the vault and hand back with a reason, and cannot so much "
        "as recommend one — a recommendation to destroy a chain, from the party "
        "with a route to the broker, would be pressure rather than an opinion.",
    ),
}


#: Which of the three ways nobody decided, and whether it is anybody's fault.
#:
#: A bare "no decision" hides the distinction, and the distinction is the whole
#: value: a turn cap and an unconditioned deferral are facts about the two
#: agents, and a failed call is a fact about the machinery that says nothing
#: about either of them.
NO_DECISION_CAUSES: dict[NoDecision, str] = {
    NoDecision.TURNS_EXHAUSTED: (
        "They talked and did not converge. The exchange reached its turn cap "
        "with nothing settled, which is a fact about the two of them rather "
        "than a fault in anything."
    ),
    NoDecision.CALL_FAILED: (
        "The model call failed part-way through, so the machinery broke rather "
        "than the two of them weighing it up. Nothing about either agent can be "
        "read off this — not that the dream is weak, and not that the trading "
        "agent was cautious. This is the one of the three that is a fault."
    ),
    NoDecision.DEFER_WITHOUT_WAKE: (
        "The trading agent reached for a deferral and could not name what would "
        "end the wait. Recorded as nobody deciding rather than as a deferral, "
        "because a park with nothing to wake it is &ldquo;we ran out of things "
        "to say&rdquo; wearing a decision's clothes."
    ),
}


#: Who decided, in words. Falls back through the transcript's own names and then
#: to the raw string, for the reason `DreamMessage.speaker` is an open string:
#: the intended direction is several dreamers arguing a topic out, and a record
#: that cannot say who said what is not a record.
DECIDER_NAMES = {**SPEAKER_NAMES, "conference": "The conference"}


def _unworded(value: object) -> str:
    """A stored value this version of the page has no sentence for.

    Reached when a verdict or a cause is added to `dreaming.py` and nobody adds
    the copy here. Naming it is the honest answer: it is a real recorded state,
    and neither dropping it nor taking the page down with a `KeyError` says so.
    Same failure direction as the store's own tolerant readers, and the same
    reason the badge text is separate from the machine value in the first place.
    """
    return (
        f'<span class="alert">Recorded as <b>{_e(str(value))}</b>, which this '
        "page has no wording for. The record is real; the sentence describing "
        "it has not been written.</span>"
    )


def _conference_meta(decision: ConferenceDecision) -> str:
    """Who, over how many turns, and when — on one monospace line.

    **Zero turns is a fact rather than a blank.** A call that failed before the
    opening offer produced no turn at all, and `0 turns` reads like a counter
    that was never wired up, so it is said in words.
    """
    who = DECIDER_NAMES.get(decision.decided_by.lower(), decision.decided_by)
    turns = (
        "no turn taken"
        if decision.turns == 0
        else _count(decision.turns, "turn")
    )
    lead = who or "unattributed"
    if not decision.decided:
        # Nobody decided, so nobody may be named as having. `decided_by` on one
        # of these is the conference narrating itself.
        lead = f"recorded by {lead}"
    return (
        f'<p class="meta">{_e(lead)} &middot; {_e(turns)} &middot; '
        f"{_e(_when(decision.at))}</p>"
    )


def _conference_effect(decision: ConferenceDecision) -> str:
    """Whether the store did the thing the verdict implies. **Three states.**

    `None` is not a failure and must never be drawn as one: a decline, a
    deferral and a no-decision all leave every shelf exactly where it was, so
    there was nothing to attempt. `False` is the real failure — the two of them
    agreed and the shelf had no room — and the agreement is the half that says
    something about the agents, so it is kept rather than collapsed into
    "nothing happened". `True` is the effect landing.
    """
    if decision.effected is None:
        # **`None` means two different things and only one of them is "nothing".**
        # On a deferral, a decline or a no-decision it is a stated fact: those
        # leave every shelf where it was, so there was nothing to attempt. On a
        # promote or an archive it cannot be — both ask something of a shelf —
        # so it is an UNKNOWN, and saying "asks nothing of any shelf" over one
        # would be a wrong claim about the verdict itself. Not reachable from
        # `Conference`, which always records the store's answer; reachable by
        # anything else that writes a row, which is why it is answered here.
        asks = {
            ConferenceVerdict.DEFER: "A deferral moves nothing: the dream stays "
            "exactly where it was.",
            ConferenceVerdict.DECLINE: "A decline moves nothing: the dream stays "
            "in the vault.",
            ConferenceVerdict.NO_DECISION: "Nothing was decided, so nothing was "
            "asked of any shelf.",
        }.get(decision.verdict)
        if asks is None:
            return (
                '<p class="note"><span class="alert">Whether the store carried '
                "this out was not recorded. This verdict does ask something of a "
                "shelf, so that is an unknown rather than a nothing, and the "
                "shelf itself is the only place to find out.</span></p>"
            )
        return (
            f'<p class="note">{asks} Nothing was attempted, which is a '
            "different answer from something being attempted and refused.</p>"
        )
    if decision.effected:
        detail = decision.effect_detail or "The store carried it out."
        return f'<p class="did">Carried out &mdash; {_e(detail)}</p>'
    return (
        '<p class="undone"><b>Decided, and the store refused to carry it '
        "out.</b>"
        + (f" {_e(decision.effect_detail)}" if decision.effect_detail else "")
        + " The decision stands: what they agreed and what the shelf allowed are "
        "two facts, and this row keeps both.</p>"
    )


def _conference_wake(decision: ConferenceDecision) -> str:
    """The condition that would restart the conversation, with its number.

    **A stored `DEFER` always carries a gradeable one.** `is_coherent` refuses
    the row on write and `_to_conference_decision` drops it on read, so a
    deferral rendering without a wake here is a record that was skipped rather
    than shown — and that is said loudly instead of drawing an empty deferral,
    which is the exact shape the wake condition exists to make impossible.
    """
    wake = decision.wake
    if wake is None:
        return (
            '<p class="note"><span class="alert">This deferral is rendering '
            "with no wake condition, and a stored one cannot be in that state: "
            "the store refuses an incoherent row on write and skips one it "
            "cannot parse on read. Treat this as an incomplete record rather "
            "than as a deferral that named nothing.</span></p>"
        )

    if wake.is_checkable:
        # The missing subject is coloured rather than written into the sentence.
        # `no symbol close below 641.2` read as one phrase on screen, which is a
        # comparison that looks like it has a subject and does not.
        subject = (
            _e(wake.symbol)
            if wake.symbol.strip()
            else '<span class="alert">no symbol</span>'
        )
        threshold = (
            f'<span class="thr">{subject} '
            f"{_e(str(wake.field))} {_e(str(wake.op))} {wake.value:g}</span>"
        )
    else:
        threshold = '<span class="thr">No number in this one.</span>'

    ungradeable = (
        ""
        if wake.is_gradeable
        else '<p class="note"><span class="alert">Nothing can settle this: a '
        "wake condition needs a symbol, a field, an operator and a number, and "
        "this one is short of at least one of them. A stored deferral is "
        "refused without all four, so this record is incomplete.</span></p>"
    )
    return (
        f'<p class="wake"><b>What would wake it</b>{_e(wake.text)}'
        f"{threshold}</p>{ungradeable}"
    )


def _conference_undecided(decision: ConferenceDecision) -> str:
    """Nobody decided, and WHICH of the three ways.

    Rendered apart from the four real verdicts rather than as the mildest of
    them, and reached because `decided` is read before `verdict`. Reporting the
    absence of a decision as a decision is how a silent failure starts looking
    healthy — the rule `news_history.has_cycles` and `seen.py`'s first visit
    already carry, arriving at the conference.
    """
    cause = decision.undecided
    if cause is None:
        why = (
            '<p class="what"><span class="alert">No cause is recorded. A stored '
            "no-decision always carries one — the store refuses a row without it "
            "— so this is an incomplete record rather than a conversation that "
            "ended for no reason.</span></p>"
        )
        attr = ""
    else:
        # `.get` rather than `[]`. `_to_conference_decision` skips a cause it
        # cannot parse, so nothing unknown reaches here from the store today —
        # but a value added to `NoDecision` later would parse, and a `KeyError`
        # on the page an operator opened to browse is the wrong failure. It
        # names the raw word instead, which is at least the fact.
        why = (
            f'<p class="what">{NO_DECISION_CAUSES.get(cause) or _unworded(cause)}'
            "</p>"
        )
        attr = f' data-cause="{_e(str(cause))}"'

    return (
        f'<div class="conclave" data-verdict="no_decision"{attr}>'
        '<b><span class="pill" data-verdict="no_decision">no decision</span> '
        "Nobody decided</b>"
        + why
        + (
            f'<p class="words">{_e(decision.reason)}</p>'
            if decision.reason
            else ""
        )
        + _conference_meta(decision)
        + _conference_effect(decision)
        + "</div>"
    )


#: Why a dream has no verdict on it, said per shelf rather than as one sentence.
#:
#: A workbench dream has never been offered because the trading agent cannot see
#: that shelf, which is the machine working; a vault dream not yet conferred is a
#: queue; and a dream that reached ADOPTED or ARCHIVE with no exchange got there
#: some other way, which is worth knowing. One sentence for all four would make
#: the first read like the third.
UNCONFERRED: dict[Vault, str] = {
    Vault.WORKBENCH: "It has never been offered. The trading agent only sees "
    "the dream vault, so a chain on the workbench is out of its sight by "
    "design — that is the shelf, not a decision about the idea.",
    Vault.PROPHECY: "It has never been offered. The prophecy shelf is the "
    "dreamer's, and the trading agent only sees the dream vault.",
    Vault.VAULT: "It is on the one shelf the trading agent can see, and no "
    "exchange about it has been recorded yet. The two confer once a day, and a "
    "run that skips because nothing has changed writes no row at all.",
    Vault.ADOPTED: "It reached the adopted shelf without a recorded exchange, "
    "so something other than a conference adopted it — an operator, or the "
    "tool.",
    Vault.ARCHIVE: "It reached the archive without a recorded exchange.",
}


def _conference(
    dream: Dream,
    decision: ConferenceDecision | None,
    *,
    readable: bool = True,
) -> str:
    """What the two agents decided about this one, at a glance.

    **`decision is None` is a third state and never the mildest verdict.** It
    means they have never conferred about this dream at all, and rendering it as
    a `NO_DECISION` would report a conversation that never happened as one that
    happened and settled nothing. Same rule as `seen.py`'s first visit and
    `news_history.has_cycles`.

    `readable=False` is a fourth: the store could not be asked. A page that
    answered "never conferred" on the strength of a failed read would be a
    confident wrong claim about every card at once, so it says which it is.

    `decided` is read BEFORE `verdict`, so a sixth verdict added later is
    classified once, in `DECIDED_VERDICTS`, rather than falling through this
    branch into a decision's wording.
    """
    if not readable:
        return (
            '<div class="conclave" data-unconferred=""><b>Verdict not '
            'read</b><p class="what"><span class="alert">The conference record '
            "could not be read from <code>data/dreams.db</code>, so this card "
            "cannot say whether the two agents have decided anything about "
            "this dream. That is not the same as their never having "
            "met.</span></p></div>"
        )

    if decision is None:
        return (
            '<div class="conclave" data-unconferred=""><b>Never '
            f'conferred</b><p class="what">{UNCONFERRED[dream.vault]} Nobody '
            "has met about it, which is a different thing from meeting and "
            "settling nothing.</p></div>"
        )

    if not decision.decided:
        return _conference_undecided(decision)

    badge, lede = CONFERENCE_WORDS.get(
        decision.verdict, (str(decision.verdict), _unworded(decision.verdict))
    )
    # The archive is the one verdict that is a letting-go, and the motes are
    # already this page's mark for exactly that — they sit on the wisp and
    # nowhere else. Decoration, and `dream_fx.CSS` takes them away under
    # `prefers-reduced-motion`: everything they say is said in the words beside
    # them.
    letting_go = decision.verdict is ConferenceVerdict.ARCHIVE
    motes = (
        '<span class="wisps"><b></b><b></b><b></b><b></b></span>'
        if letting_go
        else ""
    )
    out = (
        f'<div class="conclave{" wisped" if letting_go else ""}" '
        f'data-verdict="{_e(str(decision.verdict))}">{motes}'
        # "The verdict" rather than "what THEY decided": an archive is the
        # dreamer's alone and a promote is the trading agent's, so a heading
        # asserting a joint decision would be wrong on three of the four. The
        # meta line names who, from the row.
        f'<b><span class="pill" data-verdict="{_e(str(decision.verdict))}">'
        f"{_e(badge)}</span> The verdict</b>"
        f'<p class="what">{lede}</p>'
    )
    if decision.reason:
        # The deciding agent's own words, never a sentence this repository wrote
        # about them.
        out += f'<p class="words">{_e(decision.reason)}</p>'
    if decision.verdict is ConferenceVerdict.DEFER:
        out += _conference_wake(decision)
    out += _conference_meta(decision)
    out += _conference_effect(decision)
    return out + "</div>"


def _conference_feed(
    decisions: Sequence[ConferenceDecision],
    titles: Mapping[int, str],
    *,
    readable: bool = True,
) -> str:
    """Every recent verdict across every dream, newest first.

    The cards answer "what did they decide about this one"; this answers "what
    have those two been doing", which an operator should not have to open eleven
    cards to find out. Newest first because that is the question asked of a feed
    — the transcript inside a card is oldest-first for the opposite reason, since
    a negotiation read backwards is a negotiation read backwards.

    **An empty feed is not "they agreed on nothing".** An exchange skipped
    because nothing changed, because the epoch's budget is spent or because the
    dream is at its lifetime ceiling writes no row at all, so an empty list is
    most often the change gate doing its job. The sentence says so rather than
    letting the silence be read.
    """
    head = (
        '<section class="block" id="conference"><h2>What the two agents have '
        "been deciding</h2>"
    )
    if not readable:
        return head + (
            '<div class="banner warn"><b>The conference record could not be '
            "read</b>Something went wrong opening <code>data/dreams.db</code>, "
            "so this list is not empty — it is unknown. The shelves below are "
            "rendered from whatever the same store did return.</div></section>"
        )
    lede = (
        '<p class="note" style="max-width:74ch">One row per exchange that '
        "reached the model, including the ones that ended in nothing. They "
        "confer once a day, on the dream timer, never on the trading loop's "
        "fifteen-minute pulse.</p>"
    )
    if not decisions:
        return head + lede + (
            '<p class="note">No exchange has been recorded. That is not the '
            "same as their having met and settled nothing: a run skipped "
            "because nothing about a dream has changed since the last exchange "
            "writes no row at all, and neither does an empty vault.</p></section>"
        )

    rows = ""
    for d in decisions:
        badge = (
            CONFERENCE_WORDS.get(d.verdict, (str(d.verdict), ""))[0]
            if d.decided
            else "no decision"
        )
        title = titles.get(d.dream_id)
        subject = (
            f'<a href="#dream-{d.dream_id}">{_e(title)}</a>'
            if title is not None
            else f'<span class="subject">Dream #{d.dream_id}</span> '
            '<span class="muted">(not in the window below)</span>'
        )
        cause = (
            f' &middot; {_e(str(d.undecided).replace("_", " "))}'
            if d.undecided is not None
            else ""
        )
        effect = (
            " &middot; nothing to carry out"
            if d.effected is None
            else (
                " &middot; carried out"
                if d.effected
                else " &middot; the store refused it"
            )
        )
        who = DECIDER_NAMES.get(d.decided_by.lower(), d.decided_by) or "unattributed"
        turns = "no turn taken" if d.turns == 0 else _count(d.turns, "turn")
        rows += (
            f'<li data-verdict="{_e(str(d.verdict))}"><div class="hdr">'
            f'<span class="pill" data-verdict="{_e(str(d.verdict))}">'
            f"{_e(badge)}</span>{subject}"
            f'<span class="at">{_e(_when(d.at))}</span></div>'
            + (f'<p class="why">{_e(d.reason)}</p>' if d.reason else "")
            + (
                f'<p class="why">Would wake on {_e(d.wake.text)}</p>'
                if d.verdict is ConferenceVerdict.DEFER and d.wake is not None
                else ""
            )
            + f'<p class="meta">{_e(who)} &middot; {_e(turns)}{cause}{effect}</p>'
            "</li>"
        )
    return head + lede + f'<ol class="confeed">{rows}</ol></section>'


def _dream(
    dream: Dream,
    *,
    titles: Mapping[int, str] | None = None,
    fused_into: Sequence[int] = (),
    transcript: Sequence[DreamMessage] = (),
    adoptions: Sequence[Adoption] = (),
    decision: ConferenceDecision | None = None,
    decision_readable: bool = True,
    is_new_fusion: bool = False,
    now: datetime | None = None,
) -> str:
    """One mini-project, read top to bottom as an argument.

    The spark, the chain, the hop most likely to break it, then what was
    decided. Ordering it that way is deliberate: a reader who stops after two
    sections has read the idea and the reason to doubt it, which is the right
    pair to have if you only read two.

    **The conference verdict sits immediately above the transcript**, and that
    pairing is the point: the verdict is the conclusion and is always visible,
    the conversation that produced it is the evidence and is one click away. A
    verdict with the six turns behind it collapsed underneath is readable; six
    turns with the outcome buried in the last one is not, which is the whole
    reason the verdict is stored rather than reconstructed.

    **A fusion is drawn already fused.** The `fused` class is in the markup the
    server sends; `is_new_fusion` only decides whether the client plays the
    joining once. Script off, script threw, reduced motion — all render both
    parent titles and the shared hop as ordinary text, because the alternative
    (two cards merged by JavaScript) fails to two cards and a lie.

    **It is not an endorsement either.** No "stronger idea" styling, no verdict
    it has not earned, and no weakest hop inherited from a parent: nobody has
    attacked the combined chain yet, and that is where it has got to rather than
    a gap in it.
    """
    names = titles or {}
    moment = now or datetime.now(UTC)
    verdict = (
        f'<span class="pill {dream.verdict}">{_e(str(dream.verdict))}</span>'
        if dream.verdict
        else ""
    )
    verification = dream.verification
    fused = " fused" if dream.is_fusion else ""
    anchor = f' id="dream-{dream.id}"' if dream.id is not None else ""
    reveal = (
        reveal_attrs(is_new=is_new_fusion, fusion_id=dream.id)
        if dream.is_fusion
        else ""
    )
    out = (
        f'<article class="dream{fused}"{anchor}{reveal}><div class="top">'
        f"<h3>{_e(dream.title)}</h3>"
        f'<span class="pill {dream.stage}">{_e(str(dream.stage))}</span>'
        f"{verdict}"
        f'<span class="pill {verification}">{_e(str(verification))}</span>'
        f'<span class="when">{_e(_when(dream.updated_at))}</span>'
        "</div>"
        f'<div class="spark">{_e(dream.seed)}'
        + (
            # A fusion's origin is its parents, and `fuse` writes it as prose
            # naming both titles — which the crests below say better, linked and
            # by id. Rendering both puts the same sentence on the card three
            # times, since `_fusion_title` is the pair as well.
            f'<span class="from">Sparked by {_e(dream.origin)}</span>'
            if dream.origin and not dream.is_fusion
            else ""
        )
        + '</div><div class="body">'
        + _symbiosis(dream, names)
    )

    if dream.chain:
        shared = set(dream.shared_hops)
        # Read off the dream, never worked out here. `resolved_weakest_hop`
        # honours the number the dreamer wrote when it lands inside the chain
        # and otherwise looks for an exact claim match, and it deliberately does
        # not clamp: a model answering 9 on a three-hop chain has named no hop.
        weakest = dream.resolved_weakest_hop
        out += _chain_diagram(dream)
        out += (
            '<ol class="hops">'
            + "".join(
                _hop(h, shared=h.claim in shared, weakest=(i == weakest))
                for i, h in enumerate(dream.chain, 1)
            )
            + "</ol>"
        )
    else:
        out += '<p class="note">No chain recorded yet. Still a spark.</p>'

    # The weakest hop leads the commentary because confidence in a chain is the
    # minimum across its links rather than the average, and a reader given one
    # sentence should be given the one that could kill it.
    if dream.weakest_hop:
        # Which hop the sentence NAMES, when it names one. `None` on a chain
        # that has hops is "could not establish which", not "there is no weak
        # link" — the missing-versus-absent rule, and it is exactly the state
        # that blocks promotion, so it is said in words rather than left as a
        # hop with no ring round it.
        unplaced = (
            ""
            if dream.resolved_weakest_hop is not None or not dream.chain
            else " <span class=\"muted\">Which hop this is has not been "
            "established: the sentence matches no claim in the chain and no "
            "usable hop number was given, so nothing can be pinned to it.</span>"
        )
        out += (
            f'<p class="weak"><b>Weakest hop</b>{_e(dream.weakest_hop)}{unplaced}</p>'
        )
    elif dream.is_fusion:
        # Deliberately a different sentence from the one below. A fusion is
        # BORN without a weakest hop and without a verdict — `fuse` refuses to
        # inherit either, because claiming the combined chain had been examined
        # would be back-dating an attack nobody has made. That is a state, not a
        # shortfall, and the copy has to read as one.
        out += (
            '<p class="weak"><b>Weakest hop</b>Nobody has attacked this yet. A '
            "fusion starts with no weakest hop and no verdict on purpose — "
            "neither parent's is inherited, because the combined chain has not "
            "been examined and a chain without a stated weakest link has not "
            "been attacked yet.</p>"
        )
    elif dream.chain:
        out += (
            '<p class="weak"><b>Weakest hop</b>Not named. A chain without a '
            "stated weakest link has not been attacked yet.</p>"
        )

    out += _verification_note(dream)

    if dream.trigger:
        out += f'<p class="trigger"><b>Watching for:</b> {_e(dream.trigger)}</p>'
    elif dream.verdict in (DreamVerdict.KEEP, DreamVerdict.PARK):
        # Same rule the Decisions page applies to the loop's own watches: a
        # watch with no named trigger is not a plan, and rendering it as one
        # would let the dreamer look like it has a view when it does not.
        out += (
            '<p class="trigger"><b>Watching for:</b> <span class="muted">nothing '
            "named. A kept idea with no trigger is a note, not a watch.</span></p>"
        )

    out += _conditions(dream, moment)
    out += _stuck(dream)

    if dream.instruments:
        out += (
            '<p class="trigger"><b>About:</b> '
            + _e(", ".join(dream.instruments))
            + ' <span class="muted">(subject matter, not an instruction)</span></p>'
        )

    if dream.symbols and dream.vault is not Vault.ADOPTED:
        # What it CLAIMS, which is not yet what anything permits. Only an
        # adoption row grants, and only while it is live.
        out += (
            '<p class="trigger"><b>Claims:</b> '
            + _e(", ".join(dream.symbols))
            + ' <span class="muted">(what an adoption would permit, not a '
            "permission)</span></p>"
        )

    out += _wisp_and_grant(dream, adoptions, moment)

    if fused_into:
        named = ", ".join(
            f'<a href="#dream-{i}">#{i}</a>' for i in sorted(set(fused_into))
        )
        out += (
            f'<p class="lineage">Fused into {named} — unchanged and still '
            "here.</p>"
        )

    if dream.thoughts:
        rows = "".join(
            f'<li><span class="st {t.stage}">{_e(str(t.stage))}</span>'
            f"<span>{_e(t.text)}</span></li>"
            for t in dream.thoughts
        )
        out += (
            f'<details class="stream"><summary>The working, '
            f"{_count(len(dream.thoughts), 'step')}</summary><ol>{rows}</ol></details>"
        )

    out += _conference(dream, decision, readable=decision_readable)
    out += _transcript(transcript)

    return out + "</div></article>"


def _ledger(ledger: DreamLedger) -> str:
    """The consolidation pass, rendered for the operator and nobody else.

    This is the one form of learning the repository allows: facts about the
    dreamer's REASONING, which are true regardless of how any trade went, rather
    than facts about returns, which are a forty-sample coin flip a model would
    confidently overfit to. Same loop as `metrics.py` and the Analytics page,
    and it terminates in the same place, which is a person.
    """
    if not ledger.dreams:
        return ""

    def rate(value: float | None) -> str:
        # `n/a`, never `0%`. An empty store has not scored nought for sourcing;
        # it has no evidence either way, and the two must not read alike.
        return "n/a" if value is None else f"{value:.0f}%"

    thin = (
        '<p class="note" style="margin-top:.875rem">Computed over '
        f"{_count(ledger.resolved, 'resolved chain')}. Below "
        f"{THIN_LEDGER_THRESHOLD} this is a thin sample and every rate above is "
        "noise, which is stated here for the same reason the Analytics page "
        "states it: a rate without its sample count gets believed anyway.</p>"
        if ledger.sample_is_thin
        else ""
    )

    flags = ""
    if ledger.untriggered_keeps:
        flags += (
            f'<p class="note">{_count(ledger.untriggered_keeps, "kept idea")} name no '
            "trigger. Those are notes rather than watches.</p>"
        )
    if ledger.unattacked:
        flags += (
            f'<p class="note">{_count(ledger.unattacked, "chain")} {_word(ledger.unattacked, "has", "have")} no stated weakest '
            "hop, so nobody has tried to break them yet.</p>"
        )

    return (
        '<section class="block"><h2>What it has learned about its own thinking</h2>'
        '<p class="note" style="max-width:68ch;margin-bottom:1rem">Counted over the '
        "chains below, and deliberately nothing about what any of them would "
        "have earned. The dreamer never sees profit and loss: forty trades is "
        "noise, and a model shown three losses will confidently change "
        "approach. These are facts about the reasoning — true however a trade "
        "went — and they reach you rather than it.</p>"
        '<div class="grid g4">'
        + stat("Hops sourced", rate(ledger.sourcing_rate), "of every hop recorded")
        + stat(
            "Chains dropped",
            rate(ledger.drop_rate),
            "of those resolved. High is healthy",
        )
        + stat(
            "Median chain",
            "n/a" if ledger.median_hops is None else f"{ledger.median_hops:g}",
            # The word agrees with the figure above it, which is the one place
            # a caption cannot just say "hops": a median of exactly 1 read as
            # "Median chain 1 — hops".
            (
                "hops. Two is the minimum it aims for"
                if ledger.median_hops is None
                else f"{_word(ledger.median_hops, 'hop')}. "
                "Two is the minimum it aims for"
            ),
        )
        + stat("Resolved", str(ledger.resolved), f"of {ledger.dreams} recorded")
        + "</div>"
        + flags
        + thin
        + "</section>"
    )


WORKED_EXAMPLE = """
<div class="worked">
  <h3>What a dream looks like</h3>
  <p class="note" style="margin-top:0">An illustration, not a recorded dream.
  Nothing below was produced by the agent and nothing below is a suggestion.</p>
  <ol class="hops" style="margin-top:.875rem">
    <li class="checked">Periodical cicadas emerge on fixed multi-year broods, and
      the brood map is published.<span class="src">Checkable against
      entomological records</span></li>
    <li class="checked">Sesame production is concentrated in a small number of
      countries.<span class="src">Checkable against agricultural
      statistics</span></li>
    <li class="open">In a given year, two of the three largest producers fall
      inside overlapping brood ranges and lose harvest in the same
      season.<span class="src">Not checked. Nobody has verified this
      hop.</span></li>
    <li class="checked">Indonesia has no periodical cicadas, so its crop is
      unaffected.<span class="src">Checkable against the same brood
      map</span></li>
  </ol>
  <p class="weak"><b>Weakest hop</b>Whether the brood overlap and the production
  concentration actually coincide in any particular year. Everything else is
  reference data; this is the part that has to be true and is not yet
  established.</p>
  <p class="note" style="margin-top:.875rem">That is the whole shape. Each link
  is a separate physical claim that can be checked on its own, any one of them
  breaking kills the chain, and the conclusion is the only speculative part. A
  chain whose links cannot be attacked one at a time is a story rather than a
  hypothesis, and gets dropped.</p>
</div>
"""


# Grogu at the crystal ball, for the shelf where a chain stops being rearranged
# and becomes a claim the world can settle. Primitives rather than an image, for
# the reason `DREAMER` is: it inherits the palette, animates in CSS and costs no
# request. `aria-hidden`, because it is a mood — everything it says is said in
# words beside it.
ORACLE = """
<svg class="oracle" viewBox="0 0 120 120" aria-hidden="true">
  <ellipse class="ear" cx="24" cy="30" rx="14" ry="6.5"/>
  <ellipse class="ear" cx="96" cy="30" rx="14" ry="6.5"/>
  <path class="robe" d="M42 44c10 6 26 6 36 0l5 24H37Z"/>
  <path class="head" d="M60 6c13 0 21 9 21 21s-9 21-21 21-21-9-21-21 8-21 21-21Z"/>
  <ellipse class="eye" cx="52" cy="29" rx="4" ry="5"/>
  <ellipse class="eye" cx="68" cy="29" rx="4" ry="5"/>
  <path class="stand" d="M46 110h28M52 110l8-5M68 110l-8-5"/>
  <circle class="orb" cx="60" cy="88" r="18"/>
  <path class="arm" d="M43 56c-8 8-10 18-1 26M77 56c8 8 10 18 1 26"/>
  <circle class="hand" cx="43" cy="83" r="3.4"/>
  <circle class="hand" cx="77" cy="83" r="3.4"/>
  <circle class="glint" cx="52" cy="81" r="2.8"/>
</svg>
"""


#: The five shelves, in the order a dream travels them, with the sentence that
#: says what being on one MEANS.
#:
#: Every shelf appears whether or not anything is on it. `DreamSummary.by_vault`
#: keys all five for the same reason: a missing row reads as "no data" and a
#: nought reads as a stated fact, and on the adopted shelf — where a row is a
#: live symbol permission — those are very different claims.
SHELVES: tuple[tuple[Vault, str, str, str, str], ...] = (
    (
        Vault.WORKBENCH,
        "Workbench",
        "still being turned over",
        "Grogu's table. Unfinished by definition, and the one place to follow "
        "something silly and find out that it was silly.",
        "Nothing on the table.",
    ),
    (
        Vault.PROPHECY,
        "Prophecy vault",
        "waiting on the world",
        "A chain he has stopped rearranging. It has become a claim the world "
        "can settle without him, so it carries a condition with a number in "
        "it — a threshold that names another figure tests a level nobody ever "
        "saw.",
        "Empty. Nothing has become a claim the world can settle yet.",
    ),
    (
        Vault.VAULT,
        "Dream vault",
        "the trading agent can see these",
        "The only shelf the trading agent can see, so putting something here "
        "is an act rather than a filing decision. What it buys is small: "
        "permission to consider some symbols for a while. Not a position, not "
        "a size, not a direction.",
        "Empty, and that is a complete answer rather than a missing one — the "
        "trading agent has nothing on offer.",
    ),
    (
        Vault.ADOPTED,
        "Adopted",
        "carrying a live permission",
        "Where he lets go. Nothing is destroyed — the chain, the conditions "
        "and the working all stay on the record — it simply stops taking up a "
        "slot in his head. His again only if the trading agent hands it back "
        "and says why.",
        "Nothing adopted. No dream is permitting a symbol right now.",
    ),
    (
        Vault.ARCHIVE,
        "Archive",
        "finished with",
        "Done with. A chain that fell apart on the second hop is a good "
        "outcome; what is kept is which hop broke, so the same idea does not "
        "arrive next month wearing a different headline.",
        "Nothing archived yet.",
    ),
)


def _shelf_rail(summary: DreamSummary) -> str:
    """Where everything is, before any of it is read.

    The spine of the page, because WHERE a dream is held is what decides what it
    can do: only the dream vault is visible to the trading agent, and only an
    adopted dream carries a permission. A flat list of cards cannot say that.
    """
    tiles = ""
    for vault, name, short, _lede, _empty in SHELVES:
        count = summary.by_vault.get(vault, 0)
        empty = ' data-empty=""' if not count else ""
        tiles += (
            f'<a class="shelf" href="#shelf-{vault}" data-vault="{vault}"{empty}>'
            f'<span class="n">{_e(name)}</span>'
            f'<span class="c">{count}</span>'
            f'<span class="s">{_e(short)}</span></a>'
        )
    return f'<div class="shelfrail">{tiles}</div>'


def _awaiting_you(dreams: Sequence[Dream], now: datetime) -> str:
    """The observations waiting on a PERSON, on the page a person actually opens.

    An observation is the half of a prophecy that fails silently. A threshold is
    graded by code on every dream run whether or not anybody is watching; an
    observation is graded by somebody looking at the world, and somebody who is
    never asked never looks. Without this the prophecy shelf becomes a place
    dreams wait for ever while every surface on the deck reports patience.

    **It shows and it does not settle**, exactly as the Settings page shows the
    limits and offers no way to change them. A fulfilled condition can carry a
    dream to the vault, a vaulted dream is what an adoption is taken from, and
    an adoption is a live permission to trade a symbol that is not in
    `config/rules.yaml` — so the write belongs at a terminal on the box, behind
    the shell, rather than behind one shared password on a surface that may be
    exposed. The card names the command instead, in the same way each limit
    names the file that owns it.

    **Nothing waiting is not nothing stuck**, and the card says so. A dream can
    equally be held by a threshold the market has not reached, which no amount
    of looking settles. Letting the first stand for the second is the confident
    partial answer this repository exists to refuse.
    """
    due = pending_observations(dreams, now=now)
    if not due:
        return ""

    late = sum(1 for item in due if item.is_overdue)
    rows = ""
    for item in due:
        condition = item.condition
        when = (
            condition.observe_by.strftime("%d %b %Y") if condition.observe_by else ""
        )
        # Overdue is a fact about the LOOKING, never about the world. An
        # unopened dashboard must not read as a refuted prophecy, so the date
        # is coloured and the claim is left exactly as it was written.
        stamp = (
            f'<span class="alert">review was due {_e(when)}</span>'
            if item.is_overdue
            else f"review by {_e(when)}"
        )
        rows += (
            f'<li data-state="{item.state.value}"><code>{_e(item.handle)}</code> '
            f"{_e(condition.text)}"
            f'<span class="thr">#{item.dream_id} {_e(item.title)}</span>'
            f'<span class="thr">Look at {_e(condition.subject)} — '
            f"{_e(condition.observable)}; {stamp}.</span></li>"
        )

    head = f"{_count(len(due), 'question', 'questions')} waiting on you"
    if late:
        head += f", {late} past the review date"

    return (
        '<section class="block" id="awaiting-you"><h2>Waiting on you</h2>'
        f'<p class="lede">{head}. These are the claims no figure the loop '
        "records can settle — somebody has to go and look.</p>"
        f'<ul class="conds">{rows}</ul>'
        '<p class="note">Answer one at a terminal on the box. This page shows '
        "them and cannot settle them: an answer can carry a dream to the vault, "
        "and a vaulted dream is what a live symbol permission is taken from.</p>"
        '<pre class="cmd">electrum-bot observations\n'
        'electrum-bot settle &lt;handle&gt; --met --note "what you saw"\n'
        'electrum-bot settle &lt;handle&gt; --ruled-out --note "what you saw"</pre>'
        '<p class="note">Nothing waiting is not the same as nothing stuck. A '
        "dream can equally be held by a threshold the market has not reached, "
        "which no amount of looking settles.</p></section>"
    )


#: How stale the newest write on this page may be before the grading card says
#: the daily run is not landing.
#:
#: Two days rather than one, because `mudhorn-dream.timer` fires once a day and
#: a twenty-five-hour gap is an ordinary late run. Shouting at one of those
#: teaches an operator to stop reading the warning that matters — the same
#: reasoning that puts the Tailscale banner at ten days rather than at the first
#: hour of the notice period.
GRADING_STALE_AFTER_SECONDS = 2 * 24 * 3600.0


def _shelf_grading(dreams: Sequence[Dream], now: datetime) -> str:
    """Whether the prophecy shelf can move at all, and what is holding it.

    `PromotionRun` counts every one of these — `cycles_available`,
    `conditions_fulfilled`, `awaiting_operator` — onto a log line, and not one
    of them reached a page. So a shelf that CANNOT move looked exactly like a
    shelf being patient, which is the `can_grade_anything` question with nowhere
    to be asked: no condition fired because nothing was recorded, and no
    condition fired because nothing here is gradeable, are opposite findings
    that produce the same empty result.

    **Only one of those two is establishable from this page, and the card
    renders them apart rather than picking the comfortable one.**

    - **Every unanswered condition being ungradeable** is a fact about what is
      stored, so it is stated outright: no amount of market movement moves this
      shelf, and the reason is named per cause.
    - **Whether the loop recorded any figures to grade against** is not on this
      page at all. That is `PromotionRun.cycles_available`, produced at grading
      time and written to `dream_promotion`, and letting a stated zero above
      stand in for it would be inventing the missing half of the answer. The
      card says which figure is missing and where it lives instead.

    **Absent rather than empty**, following `_awaiting_you`. With nothing on the
    prophecy shelf there is no grading state to report, and the shelf rail and
    the shelf's own section already state that nought in their own words. A
    panel announcing zero teaches an operator to skip the panel.

    Every count comes off the conditions' stored fields through the model's own
    properties, in the SAME precedence `grade_conditions` applies — answered,
    then a threshold code can look up, then an observation, then neither. A
    second classification written here would be a second opinion about what is
    gradeable, and the page and the log line would drift apart at exactly the
    moment somebody was comparing them.

    Ages are measured from `vault_entered_at`, which is when a dream entered
    THIS shelf, never `created_at`. A dream pulled back out for another pass and
    returned has been waiting since it came back, and the expiry clock it is
    actually running against is the same one.
    """
    shelf = [d for d in dreams if d.vault is Vault.PROPHECY]
    if not shelf:
        return ""

    settled = on_figure = on_person = no_symbol = unregistered = overdue = 0
    # Conditions counted under `on_figure` that a person is ALSO being asked
    # about, and the reason the four tiles are not a partition.
    #
    # `grade_conditions` settles a dual condition on its threshold, so the
    # buckets below put it there — and `pending_observations` does NOT apply
    # that precedence, so the same condition is on the operator's worklist, in
    # `_awaiting_you` immediately above this card and in `electrum-bot
    # observations`. Counting it once and then captioning the other tile
    # "nothing here waits on you" made this card contradict the one above it
    # about the same claim, which is worse than either answer on its own.
    #
    # So the bucket arithmetic is untouched — `total`, `stuck` and every note
    # below still describe a partition — and the person tile reports the
    # worklist, with the overlap named rather than hidden.
    both = 0
    for dream in shelf:
        for cond in dream.conditions:
            # The order is `grade_conditions`', deliberately. A condition
            # carrying BOTH a threshold and an observation is graded on the
            # threshold there, because code settles it without needing anybody,
            # so it has to be counted on the threshold here too.
            if cond.is_answered:
                settled += 1
            elif cond.is_gradeable:
                on_figure += 1
                if cond.is_observable:
                    both += 1
            elif cond.is_observable:
                on_person += 1
                if cond.state(now) is ConditionState.OVERDUE:
                    overdue += 1
            elif cond.is_checkable:
                # A number with nothing to look it up on. See below: this is the
                # one bucket that reads as a pre-registered claim and can never
                # be settled by anything.
                no_symbol += 1
            else:
                unregistered += 1

    unanswered = on_figure + on_person + no_symbol + unregistered
    stuck = no_symbol + unregistered
    # Nothing pre-registered ANYWHERE on the shelf is its own state, and it must
    # not be reported as everything being settleable. `promotion_for` requires
    # one pre-registered claim to leave the workbench, so this can only arrive
    # by hand or by a restatement wiping the list — and an absence of conditions
    # is never conditions satisfied, which is what `all_conditions_met` being
    # False on an empty list already encodes.
    total = settled + unanswered

    # Pairs rather than a mapping keyed by the dream: `Dream` is a mutable
    # dataclass and so unhashable, and the alternative — keying by `id`, which
    # is `int | None` — would collapse every unsaved dream onto one entry.
    ages = [
        (d, max(0.0, (now - _aware(d.vault_entered_at)).total_seconds()))
        for d in shelf
    ]
    oldest, oldest_age = max(ages, key=lambda pair: pair[1])

    # Dreams that have cleared the promotion rule and are still sitting here.
    #
    # `promotion_for` rather than a re-reading of the rule: it is the same pure
    # function `_stuck` renders and `dreamer.promote_dreams` acts on, so this
    # cannot develop its own opinion about what should have moved. It answers
    # over stored state alone and knows nothing about the vault's caps, which is
    # why the sentence below names both explanations instead of choosing.
    cleared = [d for d in shelf if promotion_for(d).moves]

    # A prophecy carrying NO condition at all can never be promoted by anything,
    # and until now that was only visible when it was true of the whole shelf.
    # One conditionless dream sitting beside settleable ones was counted nowhere
    # — the buckets count conditions, and it has none — so the card reported
    # patience over exactly the dream that is stuck hardest. That is the failure
    # this card exists to catch, surviving in the mixed case.
    conditionless = [d for d in shelf if not d.conditions]

    oldest_meta = f"oldest {_e(_span(oldest_age))} on this shelf"
    if conditionless:
        oldest_meta += (
            f", {len(conditionless)} carrying no condition at all"
        )

    tiles = (
        stat(
            "Prophecies waiting",
            str(len(shelf)),
            oldest_meta,
            "alert" if conditionless else "",
        )
        + stat(
            "On a figure",
            str(on_figure),
            # The caption changes with the figure rather than reading as a
            # claim about claims that are not there. "Code settles these" over
            # a nought describes an empty set.
            "code settles these, next time the dreamer runs"
            if on_figure
            else "nothing here waits on the market",
        )
        # The count is what the OPERATOR is being asked, which is
        # `pending_observations`' answer and not the bucket's — see `both`. A
        # dual condition is on the worklist above whatever this card decides to
        # file it under, so a tile reading nought beside a card listing it would
        # be the page disagreeing with itself.
        + stat(
            "On a person",
            str(on_person + both),
            f"{overdue} past the review date"
            if overdue
            else (
                f"{both} of them code can settle too"
                if both
                else (
                    "only you settle these"
                    if on_person
                    else "nothing here waits on you"
                )
            ),
            "alert" if overdue else "",
        )
        + stat(
            "Nothing can settle",
            str(stuck),
            # Three captions, because a nought here has two very different
            # meanings: every claim is settleable, or there is no claim.
            "these will never fire"
            if stuck
            else ("every claim here is settleable" if total else "no claim to settle"),
            "alert" if stuck or not total else "",
        )
    )

    notes = ""
    if not total:
        notes += (
            '<p class="note"><span class="alert">No prophecy on this shelf '
            "carries a condition at all.</span> Leaving the workbench takes one "
            "pre-registered claim, so a shelf in this state was either moved by "
            "hand or had its conditions restated away. An absence of conditions "
            "is not conditions satisfied, and nothing can carry these to the "
            "vault.</p>"
        )
    elif unanswered and not on_figure:
        # The finding that used to be invisible. Stated flatly, because it is a
        # fact about what is stored rather than a guess about the world: not one
        # unsettled claim on this shelf names a figure the loop can look up, so
        # the market cannot move any of it however it moves.
        if on_person:
            notes += (
                '<p class="note"><span class="alert">Nothing on this shelf is '
                "waiting on the market.</span> Every unsettled condition here "
                "needs somebody to go and look, so no reading the loop records "
                "will move any of it. The card above names them and the command "
                "that answers one.</p>"
            )
        else:
            notes += (
                '<p class="note"><span class="alert">Nothing on this shelf can '
                "be settled by anybody.</span> No condition here names a figure "
                "code can look up or a claim a person could answer, so the shelf "
                "cannot move at all — not by the market, not by you. That is a "
                "fact about what was written down, not about whether any of it "
                "is true.</p>"
            )
    elif on_figure:
        # And its opposite, which is the half this page CANNOT establish. Saying
        # "waiting on the market" and stopping there would report patience over
        # a loop that may have recorded nothing to check against.
        notes += (
            f'<p class="note">{_count(on_figure, "unsettled condition")} here '
            f"{_word(on_figure, 'names', 'name')} a threshold code can look up. "
            "<b>Whether the loop recorded any figures to check them against is "
            "not visible from this page</b> — that count is "
            "<code>cycles_available</code> on the <code>dream_promotion</code> "
            "log line, and a nought there means nothing could have fired however "
            "the market moved. A shelf waiting on the market and a shelf with "
            "nothing to grade against look identical from here, so this says "
            "which figure is missing rather than assuming the first.</p>"
        )

    # Said whenever there is a conditionless dream and the note above did not
    # already cover the whole shelf. Kept out of the `if/elif` chain on purpose:
    # this is a fact about DREAMS and every branch up there is about conditions,
    # so a shelf can be in exactly one of those states and in this one as well.
    if conditionless and total:
        notes += (
            f'<p class="note"><span class="alert">'
            f'{_count(len(conditionless), "prophecy", "prophecies")} here '
            f"{_word(len(conditionless), 'carries', 'carry')} no condition at "
            "all.</span> The counts above are of CONDITIONS, so a dream "
            "carrying none appears in none of them and reads as patience. "
            "Nothing can carry "
            f"{_word(len(conditionless), 'it', 'them')} to the vault: leaving "
            "the workbench takes one pre-registered claim, and an absence of "
            "conditions is not conditions satisfied.</p>"
        )

    if both:
        notes += (
            f'<p class="note">{_count(both, "condition")} above '
            f"{_word(both, 'is', 'are')} counted under BOTH "
            "<em>on a figure</em> and <em>on a person</em>: "
            f"{_word(both, 'it names', 'they name')} a threshold code can "
            "settle and an observation somebody has to go and look at, so "
            f"{_word(both, 'it is', 'they are')} on the "
            '<a href="#awaiting-you">Waiting on you</a> card as well. Either '
            f"answer settles {_word(both, 'it', 'them')}. The tiles count who "
            "COULD answer, which is not a partition when both could.</p>"
        )

    if no_symbol:
        notes += (
            f'<p class="note"><span class="alert">'
            f'{_count(no_symbol, "condition")} '
            f"{_word(no_symbol, 'carries', 'carry')} a number and no symbol."
            "</span> A field, an operator and a value with nothing to look them "
            "up on is a comparison with no subject, so <code>grade_conditions"
            "</code> skips it: it will never fire, however the market moves, and "
            "it counts against its dream's unmet conditions for as long as it "
            "stands. This is the one bucket that reads as a pre-registered claim "
            "and is not one.</p>"
        )

    if unregistered:
        notes += (
            f'<p class="note">{_count(unregistered, "condition")} pre-registered '
            "nothing at all — no threshold and no observation — so nothing can "
            "settle "
            f"{_word(unregistered, 'it', 'them')}. Counted rather than dropped, "
            "the same as a watch with prose and no trigger: a claim nobody can "
            "grade is the interesting failure, and an invisible one reads as "
            "patience.</p>"
        )

    if cleared:
        notes += (
            f'<p class="note"><span class="alert">'
            f'{_count(len(cleared), "prophecy", "prophecies")} '
            f"{_word(len(cleared), 'has', 'have')} met every condition and "
            f"{_word(len(cleared), 'is', 'are')} still on this shelf.</span> "
            "Promotion moves a dream only when <code>electrum-bot dream</code> "
            "runs, so either it has not run since the last condition fired or "
            "the dream vault is full. <code>DreamStore.promote</code> is the "
            "only thing that knows which, and it says so on the run that "
            "refuses.</p>"
        )

    # Linked only when there is an id to link to. `_dream` puts the anchor on a
    # card that has one, so `#dream-None` would be a link to nowhere on exactly
    # the dream a reader was being pointed at.
    waiting_longest = (
        f'<a href="#dream-{oldest.id}">{_e(oldest.title)}</a>'
        if oldest.id is not None
        else f"<b>{_e(oldest.title)}</b>"
    )

    # Every dream on the page, not only the prophecies: a run writes whichever
    # dream it worked on, and that is usually one on the workbench.
    last = max(_aware(d.updated_at) for d in dreams)
    age = max(0.0, (now - last).total_seconds())
    if age >= GRADING_STALE_AFTER_SECONDS:
        cadence = (
            '<p class="note"><span class="alert">Nothing shown on this page has '
            f"been written to for {_e(_span(age))}</span> — the newest is "
            f"{_e(_when(last))}. A dream run writes a step every time it "
            "completes, so a gap longer than the daily timer means no run has "
            "landed here, and nothing above has been graded in that time.</p>"
        )
    else:
        cadence = (
            f'<p class="note">The newest write on this page is {_e(_when(last))}, '
            f"{_e(_span(age))} ago. A dream run writes a step every time it "
            "completes, so something has landed recently — which is not the same "
            "as the last run having succeeded.</p>"
        )

    return (
        '<section class="block" id="grading-state">'
        "<h2>Whether the shelf can move</h2>"
        '<p class="note" style="max-width:68ch;margin-bottom:1rem">The prophecy '
        "shelf holds claims somebody can settle, and a claim nobody can settle "
        "looks exactly the same sitting there. These are the conditions on it, "
        "counted by <em>who or what could ever answer them</em> — which is the "
        "question that decides whether this shelf is being patient or is "
        "stuck.</p>"
        f'<div class="grid g4">{tiles}</div>'
        + notes
        + f'<p class="note">{_count(settled, "condition")} '
        f"{_word(settled, 'has', 'have')} been settled here, either way — met "
        "or ruled out. The oldest prophecy is "
        + waiting_longest
        + f", on this shelf {_e(_span(oldest_age))}, measured from when it "
        "entered this shelf and never from when it was first dreamt: a dream "
        "taken apart again starts its wait over.</p>"
        '<p class="note">Grading and promotion both run inside '
        "<code>electrum-bot dream</code>, once a day, and nowhere else. The "
        "decision loop's fifteen-minute pulse deliberately does not touch this "
        "shelf — so a deployment that has stopped dreaming stops grading, and "
        "every figure above freezes looking like patience.</p>"
        + cadence
        + '<p class="note">Whether the timer is ENABLED is not visible from this '
        "process; Settings names <code>systemctl list-timers</code> and says so "
        "for the same reason. A run whose model call failed writes nothing and "
        "grades nothing, so neither figure can tell a stopped timer from a "
        "failing one — the log is where that shows.</p>"
        '<pre class="cmd">electrum-bot dream\n'
        "systemctl list-timers mudhorn-dream.timer</pre></section>"
    )


def dreaming_page(
    dreams: list[Dream],
    summary: DreamSummary,
    *,
    enabled: bool,
    token: str,
    hermes_available: bool,
    soul_found: bool,
    isolated: bool = False,
    titles: Mapping[int, str] | None = None,
    fused_into: Mapping[int, Sequence[int]] | None = None,
    transcripts: Mapping[int, Sequence[DreamMessage]] | None = None,
    adoptions: Mapping[int, Sequence[Adoption]] | None = None,
    verdicts: Mapping[int, ConferenceDecision] | None = None,
    conference: Sequence[ConferenceDecision] = (),
    conference_readable: bool = True,
    marker: Marker | None = None,
    now: datetime | None = None,
) -> str:
    """The dreamer's deck: where every idea is being held, and a way to talk to it.

    The warning at the top is not decoration and is not dismissible. Everything
    on this page is speculation, produced by a model that is good at sounding
    certain, on a surface that otherwise reports measured facts about real
    money. A reader arriving mid-page has to be told which of the two they are
    looking at, in the same way the public site labels its invented figures.

    **The page is organised by SHELF rather than by recency**, because the shelf
    is the consequential fact: the dream vault is the only one the trading agent
    can see and the adopted shelf is the only one carrying a live symbol
    permission. A flat list sorted by date cannot say either of those.

    `marker` is `seen.py`'s answer to "when was the operator last provably shown
    the state of the world", and the ONLY thing it decides here is whether a
    fusion plays its joining animation. **A caller with no marker gets no
    animation**: `Marker.is_new` answers `None` on a first visit rather than
    True, and a vault of dreams all animating at once on a first ever load is a
    fireworks display rather than a notification.

    `verdicts` is the LATEST conference decision per dream and `conference` is
    the cross-dream feed, and they are two reads rather than one filtered twice.
    Taking each dream's latest out of the feed would silently answer "never
    conferred" for any dream whose last exchange fell outside the feed's window
    — a confident wrong claim, produced by an optimisation, about the one thing
    on this card that nothing else can tell you.

    `conference_readable=False` is the fourth state: the store could not be
    asked at all. It is passed through to every card rather than collapsed into
    an empty mapping, because an empty mapping and an unreadable store would
    otherwise render identically as "never conferred".
    """
    names = dict(titles or {})
    for dream in dreams:
        if dream.id is not None:
            names.setdefault(dream.id, dream.title)
    children = fused_into or {}
    talk = transcripts or {}
    grants = adoptions or {}
    settled = verdicts or {}
    moment = now or datetime.now(UTC)

    body = (
        '<div class="dream-head">'
        + DREAMER
        + '<div class="who"><p class="eyebrow">Grogu</p><h1>Dreaming</h1>'
        "<p class=note>Two hops away from whatever anyone is watching. Every "
        "link is a physical claim that can be checked on its own, and any one "
        "of them can break the whole thing.</p></div></div>"
    )

    # Ahead of everything, including the counts. The single most important fact
    # about this page is what it is not.
    #
    # Two claims, deliberately separated, because only the first is structural
    # and an earlier version of this banner ran them together and overstated the
    # second. The dream RECORDS below cannot describe an order. The CHAT PANEL
    # at the bottom is an agent, and what it can reach depends on which Hermes
    # instance is answering — which is a deployment fact this process can check
    # and therefore must state rather than assume.
    body += (
        '<div class="banner warn"><b>Nothing here is a proposal</b>'
        "Everything below is speculation. A dream carries no quantity, no entry, "
        "no stop and no side, so nothing recorded here can describe an order at "
        "all: the decision loop proposes and <code>src/bot/risk.py</code> vets "
        "what it proposes, and none of this is in either path. An idea worth "
        "acting on is read by a person and goes through the ordinary machinery, "
        "in their own time.</div>"
    )

    if enabled and hermes_available:
        body += (
            '<div class="banner ok"><b>The dreamer runs on its own agent</b>'
            "Its Hermes instance is separate from the one behind Chat, with its "
            "own memory and its own tool registry, so it has no broker tool to "
            "reach for. That is a structure rather than an instruction.</div>"
            if isolated
            else '<div class="banner warn"><b>Sharing the account agent</b>'
            "No separate dreamer instance is installed, so the panel below talks "
            "to the same Hermes as Chat and can reach the same tools, including "
            "the order tools. Every one of those still runs the risk gate first, "
            "so the operator's limits hold either way, but keeping a speculative "
            "agent away from the broker is currently a sentence in "
            "<code>souls/grogu.md</code> rather than a missing tool. "
            "<code>deploy/run-dream.sh</code> is the second instance; see the "
            "setup notes in its header.</div>"
        )

    if not soul_found:
        body += (
            '<div class="banner warn"><b>Running without a soul</b>'
            "<code>souls/grogu.md</code> could not be read, so the agent is "
            "answering without its character file. It still reaches the same "
            "tools and is still bound by the same limits; it will simply sound "
            "like nothing in particular.</div>"
        )

    body += _shelf_rail(summary)

    # The verdicts, kept as a line rather than four tiles. They describe how the
    # THINKING ended; the shelves above describe where the dream is being held,
    # and a chain can be at any stage in any vault. The rail is the spine, so
    # this sits under it as a sentence.
    body += (
        f'<p class="note" style="margin-top:.75rem">{summary.open_dreams} still '
        f"open, {summary.kept} kept, {summary.parked} parked, {summary.dropped} "
        "dropped. "
        + (
            f"{_count(summary.fusions, 'is a fusion', 'are fusions')} — chains "
            "that turned out to rest on the same claim."
            if summary.fusions
            else "Nothing has been fused yet."
        )
        + "</p>"
    )

    body += _ledger(DreamLedger.of(dreams))

    if not dreams:
        body += (
            '<section class="block"><h2>Nothing on any shelf yet</h2>'
            '<p class="note" style="margin-bottom:1rem">The dreamer writes here '
            "as it works. Here is the shape one takes.</p>" + WORKED_EXAMPLE
            + "</section>"
        )
        return body + _dreaming_talk(
            enabled=enabled,
            token=token,
            hermes_available=hermes_available,
            isolated=isolated,
        )

    if summary.unverified:
        body += (
            f'<p class="note" style="margin-top:1rem">{summary.unverified} of '
            f"{summary.total} rest entirely on hops nobody has checked. They are "
            "marked <span class=\"pill unverified\">unverified</span>, which is a "
            "statement about evidence rather than about how likely they are.</p>"
        )

    # Ahead of both, because it is the only thing on this page addressed to the
    # READER rather than describing what the agents did.
    body += _awaiting_you(dreams, moment)

    # And immediately under it, because it answers the question the card above
    # provokes: nothing waiting on you is not nothing stuck, and this is where
    # the other kind of stuck is counted. Second rather than first — that one is
    # addressed to the reader and this one describes the shelf.
    body += _shelf_grading(dreams, moment)

    # Above the shelves, because it answers a question about the two AGENTS and
    # the shelves answer one about the dreams. Below the early return on an empty
    # store, because a deck with nothing on it is a teaching page and a feed of
    # exchanges that cannot exist yet is furniture on it.
    body += _conference_feed(conference, names, readable=conference_readable)

    by_vault: dict[Vault, list[Dream]] = {v: [] for v, *_ in SHELVES}
    for dream in dreams:
        by_vault.setdefault(dream.vault, []).append(dream)

    for vault, name, _short, lede, empty in SHELVES:
        held = by_vault.get(vault, [])
        heading = (
            f'<section class="block" id="shelf-{vault}"><div class="shelf-head">'
            + (ORACLE if vault is Vault.PROPHECY else "")
            + f"<div><h2>{_e(name)}</h2>"
            f'<p class="lede">{_e(lede)}</p></div></div>'
        )
        if not held:
            # A stated nought, never a skipped shelf.
            body += heading + f'<p class="note">{_e(empty)}</p></section>'
            continue

        cards = "".join(
            _dream(
                dream,
                titles=names,
                fused_into=children.get(dream.id or 0, ()),
                transcript=talk.get(dream.id or 0, ()),
                adoptions=grants.get(dream.id or 0, ()),
                decision=settled.get(dream.id or 0),
                decision_readable=conference_readable,
                is_new_fusion=bool(
                    marker.is_new(dream.created_at) if marker else False
                ),
                now=moment,
            )
            for dream in held
        )
        # The dream vault gets a door drawn round it, because it is the one
        # shelf whose contents are visible to something that can trade.
        if vault is Vault.VAULT:
            cards = f'<div class="vaultdoor">{cards}</div>'
        body += heading + cards + "</section>"

    return body + _dreaming_talk(
        enabled=enabled,
        token=token,
        hermes_available=hermes_available,
        isolated=isolated,
    )


def _dreaming_talk(
    *, enabled: bool, token: str, hermes_available: bool, isolated: bool
) -> str:
    """The panel at the foot of the page, and the two ways it can be off.

    Its own function because the page returns early on an empty store, and the
    conversation is worth having whether or not anything has been recorded —
    the first dream has to start somewhere.

    **`DREAM_FX_SCRIPT` is emitted here, at the end of the document.** It plays
    the joining on a fusion the server marked new and does nothing else; every
    fused card is already fused in the markup above it, so a browser that never
    runs this shows the same page without the animation.
    """
    out = '<section class="block"><h2>Talk to it</h2>'
    if not enabled:
        return out + (
            '<div class="banner warn"><b>Chat is off</b>'
            "Set <code>DASHBOARD_CHAT_TOKEN</code> in the environment to enable "
            "it. The shelves above are rendered from "
            "<code>data/dreams.db</code> and do not need it.</div></section>"
            + DREAM_FX_SCRIPT
        )
    if not hermes_available:
        return out + (
            '<div class="banner crit"><b>Hermes not found</b>'
            "The token is set, but the Hermes binary is not installed where this "
            "process expects it. See <code>docs/HERMES_SETUP.md</code>.</div></section>"
            + DREAM_FX_SCRIPT
        )

    return (
        out
        + chat_panel(
            token=token,
            soul="grogu",
            who="Grogu",
            intro="I look two hops away from whatever anyone is watching. Give me "
            "something to pull on: a headline, an odd trade, a supply chain, a "
            "season. I will not tell you what to buy, and I will tell you which "
            "part of a chain I could not check.",
            placeholder="Give it something to pull on",
            suggestions=[
                "What is downstream of a shipping lane closing for a month?",
                "Something in the journal closed oddly. What might explain it?",
                "Pick a commodity nobody is talking about and find its second order.",
                "What would break the last idea you kept?",
            ],
            footnote=(
                "Ideas here are speculation and are recorded as such. Nothing "
                "said in this panel is written to the journal or becomes a "
                "proposal."
                if isolated
                else "Ideas here are speculation and are recorded as such. This "
                "panel shares the account agent's tools, so treat it as the same "
                "privilege as Chat until a separate dreamer instance is installed."
            ),
            avatar=True,
        )
        + "</section>"
        + DREAM_FX_SCRIPT
    )


# -------------------------------------------------------------------- login


def not_found_page(path: str) -> str:
    """A mistyped URL, inside the deck rather than as a JSON object.

    `GET /definitely-not-a-page` while signed in returned
    `{"detail":"Not Found"}` — no nav, no styling, no way back but the browser
    button, on a surface where every other page is themed and every other page
    carries the nav. Measured on the live site.

    It says which path was asked for and nothing about which paths exist. The
    nav in the shell already names every page an operator can reach, so listing
    them again here would be the same information twice, and a 404 that
    enumerates routes is a route list served to whoever asked for a wrong one.
    """
    # `head` escapes what it is given, so the raw path goes in. Escaping it
    # here as well would render `&lt;` at an operator who typed `<`.
    return head(
        "404",
        "No such page",
        path,
        "Nothing is served at that path. The nav above is every page this deck "
        "has; if you followed a link from somewhere, it is out of date rather "
        "than broken.",
    )


def login_page(*, env: Env, error: str = "", next_path: str = "") -> str:
    """The gate. Deliberately says nothing about the account behind it.

    No equity, no positions, no symbol list, not even whether a trade has ever
    been placed. Everything this page renders is already public in the GitHub
    repository, so an unauthenticated visitor learns nothing they could not
    read there. That is the whole job of a sign-in screen and it is easy to
    lose by putting a friendly summary above the form.

    `next_path` rides through as a hidden field so a deep link survives the
    sign-in. **It is validated by the caller against the application's own
    routes before it ever gets here** — an unchecked `next` on a login form is
    the textbook open redirect, and this page has no way to check one. It is
    rendered escaped into a value attribute regardless, because a page that
    depends on its caller having been careful should still not be the thing
    that breaks when the caller was not.
    """
    mode = "paper" if env.alpaca_paper_trade else "LIVE"
    message = f'<p class="err" role="alert">{_e(error)}</p>' if error else ""
    carry = (
        f'<input type="hidden" name="next" value="{_e(next_path)}">'
        if next_path
        else ""
    )
    return f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0B0E12">
<meta name="robots" content="noindex">
<link rel="icon" href="{FAVICON}">
<title>Sign in &middot; Mudhorn Capital</title><style>{STYLES}</style></head>
<body data-mode="{_e(mode)}">
<header class="bar"><div class="wrap">
  <span class="brand">{MARK} MUDHORN <span class="thin">CAPITAL</span></span>
  <span class="live paper"><i></i>{_e(mode)}</span>
</div></header>
<main><div class="wrap"><div class="signin"><div class="panel">
  <div class="sig">
    <svg viewBox="0 0 64 64" width="44" height="44" aria-hidden="true">
      <path fill-rule="evenodd" d="M32 2a30 30 0 1 0 0 60 30 30 0 0 0 0-60Zm0 8a22 22 0 1 1 0 44 22 22 0 0 1 0-44Z"/>
      <path d="M32 17 47 45h-9.4L32 34.2 26.4 45H17L32 17Z"/>
    </svg>
    <h1>MUDHORN <span>CAPITAL</span></h1>
    <p>Operator sign-in</p>
  </div>
  {message}
  <form method="post" action="/login">
    {carry}
    <label for="password" class="eyebrow">Password</label>
    <input id="password" name="password" type="password"
      autocomplete="current-password" required autofocus>
    <button type="submit">Sign in</button>
  </form>
  <p class="standby"><i></i>Nav computer standing by</p>
</div></div></div></main>
<footer class="wrap">Paper trading only. Rendered
{datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC.</footer>
<script>{SCRIPT}</script>
</body></html>"""
