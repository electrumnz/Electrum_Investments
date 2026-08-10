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
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ..audit import AuditView, DecisionEntry
from ..config import DAY_NAMES, Env, Rules
from ..dreamer import estimated_cost_usd, read_schedule
from ..dreaming import (
    THIN_LEDGER_THRESHOLD,
    Dream,
    DreamLedger,
    DreamSummary,
    DreamVerdict,
    Hop,
)
from ..metrics import JournalReport, render_excursions, render_summary
from ..models import AccountSnapshot, StandDownState, Trade, WorkingOrder
from ..options import ExpiryAlert
from ..tailnet import TailnetStatus
from .seen import SinceLastVisit

STYLES = """
:root {
  --ink:#0B0E12; --graphite:#161B22; --slate:#29313C; --pewter:#7D8896;
  --bone:#E9ECEF; --patina:#4E8C7D;
  /* Severity, for banners. */
  --amber:#C08A3E; --rust:#B3524A;
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
body{margin:0;background:var(--ink);color:var(--bone);font-family:var(--sans);
  font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased}
:focus-visible{outline:2px solid var(--patina);outline-offset:3px}
a{color:var(--bone);text-decoration-color:var(--slate);text-underline-offset:4px}
a:hover{text-decoration-color:var(--patina)}
.wrap{width:min(100% - 2rem,1240px);margin-inline:auto}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.pos,.gain{color:var(--gain)} .neg,.loss{color:var(--loss)} .muted{color:var(--pewter)}
.note{font-size:.8125rem;color:var(--pewter)}
h1,h2,h3{font-family:var(--serif);font-weight:400;letter-spacing:-.01em;margin:0}
h1{font-size:1.75rem} h2{font-size:1.375rem;margin-bottom:.25rem} h3{font-size:1rem}
.eyebrow{font-family:var(--mono);font-size:.6875rem;letter-spacing:.18em;
  text-transform:uppercase;color:var(--pewter);margin:0}

header.bar{border-bottom:1px solid var(--slate);position:sticky;top:0;
  background:rgba(11,14,18,.94);backdrop-filter:blur(8px);z-index:20}
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

main{padding:2rem 0 4rem}
.page-head{display:flex;align-items:baseline;gap:1rem;flex-wrap:wrap;
  margin-bottom:1.5rem}
.page-head .asof{margin-left:auto;font-family:var(--mono);font-size:.75rem;
  color:var(--pewter)}
section.block{margin-top:2.5rem}
section.block>h2{margin-bottom:.75rem}

.banner{border:1px solid var(--slate);border-left-width:3px;border-radius:2px;
  padding:.875rem 1.125rem;margin-bottom:.75rem;background:var(--graphite);
  font-size:.875rem}
.banner.crit{border-left-color:var(--rust)}
.banner.warn{border-left-color:var(--amber)}
.banner.ok{border-left-color:var(--patina)}
.banner b{display:block;font-family:var(--mono);font-size:.6875rem;
  letter-spacing:.14em;text-transform:uppercase;margin-bottom:.3rem}
.banner.crit b{color:var(--rust)} .banner.warn b{color:var(--amber)}
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
.scroll{overflow-x:auto;border:1px solid var(--slate);border-radius:2px}
caption{text-align:left;padding:0 0 .6rem;color:var(--pewter);font-size:.8125rem}
th{text-align:left;font-family:var(--mono);font-size:.625rem;letter-spacing:.12em;
  text-transform:uppercase;color:var(--pewter);font-weight:400;
  padding:.7rem .875rem;border-bottom:1px solid var(--slate);white-space:nowrap;
  background:var(--graphite);position:sticky;top:0}
td{padding:.7rem .875rem;border-bottom:1px solid var(--slate);vertical-align:top}
td.r,th.r{text-align:right}
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

.curve{border:1px solid var(--slate);border-radius:2px;background:var(--graphite);
  padding:1.125rem}
.curve svg{display:block;width:100%;height:auto}
.curve .line{fill:none;stroke:var(--patina);stroke-width:1.75;
  stroke-linejoin:round;stroke-linecap:round}
.curve .area{fill:color-mix(in srgb,var(--patina) 14%,transparent);stroke:none}
.curve .base{stroke:var(--slate);stroke-width:1;stroke-dasharray:3 4}
.curve .tick{fill:var(--pewter);font-family:var(--mono);font-size:10px}

.readout{font-family:var(--mono);font-size:.75rem;color:var(--pewter);
  background:var(--graphite);border:1px solid var(--slate);border-radius:2px;
  padding:1rem 1.125rem;white-space:pre-wrap;line-height:1.7}
.empty{color:var(--pewter);font-style:italic;padding:1.25rem}

/* ------------------------------------------------------------- decisions */
.cycle{border:1px solid var(--slate);border-radius:2px;background:var(--graphite);
  margin-bottom:1rem}
.cycle>.head{display:flex;gap:.875rem;align-items:baseline;flex-wrap:wrap;
  padding:.875rem 1.125rem;border-bottom:1px solid var(--slate)}
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
.reasons{margin:.35rem 0 0;padding-left:1.1rem;color:var(--loss);font-size:.8125rem}
.reasons li{margin:.15rem 0}
.pill.watch{color:var(--amber)}
.considered{padding:.5rem 0;border-bottom:1px dashed var(--slate)}
.considered:last-child{border-bottom:0}
.considered .chain{margin-top:.4rem;border-left-color:var(--slate)}
.feed{margin:.3rem 0 0;padding-left:1.1rem;font-size:.8125rem;color:var(--pewter)}
.feed li{margin:.15rem 0}
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
.card::before,.curve::before,.cycle::before,.scroll::before,.readout::before,
.banner::before,.chat .log::before{top:-1px;left:-1px;border-right:0;border-bottom:0}
.card::after,.curve::after,.cycle::after,.scroll::after,.readout::after,
.banner::after,.chat .log::after{bottom:-1px;right:-1px;border-left:0;border-top:0}
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

.dream .weak{margin:.875rem 0 0;padding:.6rem .75rem;font-size:.8125rem;
  border:1px solid rgba(192,138,62,.35);border-left-width:3px;border-radius:2px;
  background:rgba(192,138,62,.07);color:var(--bone)}
.dream .weak b{font-family:var(--mono);font-size:.625rem;letter-spacing:.14em;
  text-transform:uppercase;color:var(--amber);display:block;margin-bottom:.2rem}
.dream .trigger{margin:.75rem 0 0;font-size:.8125rem;color:var(--pewter)}
.dream .trigger b{color:var(--bone);font-weight:400}

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

/* ------------------------------------------------------------------- gate */
/* The sign-in screen, and the one the brief actually asked for: this is where
   the jump starts. It still reveals nothing about the account behind it — see
   the docstring on `login_page` — so everything dressed up here is chrome. */
.gate{min-height:calc(100svh - 8rem);display:grid;place-items:center;padding:2rem 0}
.gate .panel{width:min(100% - 2rem,25rem);position:relative;
  border:1px solid var(--slate);border-radius:2px;padding:2rem 1.75rem;
  background:linear-gradient(180deg,rgba(22,27,34,.9),rgba(15,19,25,.94));
  backdrop-filter:blur(4px);
  box-shadow:0 0 0 1px rgba(111,211,232,.06),0 30px 70px -40px rgba(0,0,0,1)}
.gate .panel::before,.gate .panel::after{content:"";position:absolute;
  width:14px;height:14px;border:1px solid var(--holo);opacity:.6}
.gate .panel::before{top:-1px;left:-1px;border-right:0;border-bottom:0}
.gate .panel::after{bottom:-1px;right:-1px;border-left:0;border-top:0}
.gate .sig{text-align:center;margin-bottom:1.75rem}
.gate .sig svg{display:block;margin:0 auto .875rem}
.gate .sig svg path{fill:var(--bone)}
.gate .sig h1{font-size:1.25rem;letter-spacing:.16em;font-family:var(--serif)}
.gate .sig h1 span{color:var(--pewter)}
.gate .sig p{margin:.5rem 0 0;font-family:var(--mono);font-size:.625rem;
  letter-spacing:.2em;text-transform:uppercase;color:var(--holo);opacity:.8}
.gate label{display:block;margin-bottom:.5rem}
.gate input{width:100%;background:var(--ink);color:var(--bone);
  border:1px solid var(--slate);border-radius:2px;padding:.75rem .875rem;
  font-family:var(--mono);font-size:.9375rem;letter-spacing:.18em;
  transition:border-color .25s var(--ease),box-shadow .25s var(--ease)}
.gate input:focus{border-color:var(--holo);
  box-shadow:0 0 0 3px rgba(111,211,232,.12);outline:none}
.gate button{width:100%;margin-top:1rem;background:transparent;color:var(--bone);
  border:1px solid var(--patina);border-radius:2px;padding:.75rem;
  font-family:var(--mono);font-size:.6875rem;letter-spacing:.22em;
  text-transform:uppercase;cursor:pointer;position:relative;overflow:hidden;
  transition:background .25s var(--ease),border-color .25s var(--ease)}
.gate button:hover{background:rgba(78,140,125,.16);border-color:var(--holo)}
.gate .standby{margin:1.25rem 0 0;text-align:center;font-family:var(--mono);
  font-size:.625rem;letter-spacing:.16em;text-transform:uppercase;
  color:var(--pewter)}
.gate .standby i{display:inline-block;width:5px;height:5px;border-radius:50%;
  background:var(--holo);margin-right:.5rem;vertical-align:middle;
  animation:fx-pulse 2.8s ease-in-out infinite}
.gate .err{margin:0 0 1.25rem;padding:.625rem .75rem;font-size:.8125rem;
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
  .live{margin-left:auto;padding-left:0;border-left:0}
  .scroll{border:0}
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

  var PANELS = '.page-head,.banner,.card,.curve,.scroll,.cycle,.readout,' +
               '.chat .log,.empty,section.block > h2';

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

  function setLink(state, label) {
    if (!link) return;
    link.classList.remove('link-live', 'link-retry', 'link-down');
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

    var account = data.account;
    if (!account) return;

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
       Writing our own backoff on top would fight the browser's. */
    es.onerror = function () { setLink('retry', 'reconnecting'); };
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
     way a skip link would. */
  function restoreFocus() {
    if (lastFocus && lastFocus.focus && lastFocus !== document.body
        && document.contains(lastFocus)) {
      lastFocus.focus();
      if (document.activeElement === lastFocus) return;
    }
    var main = document.querySelector('main');
    if (!main) return;
    main.setAttribute('tabindex', '-1');
    main.focus();
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
    ("/dreaming", "Dreaming"),
    ("/chat", "Chat"),
    # Settings last, beside Sign out. The order is a scan path: the pages you
    # open several times a day sit left, and the one you touch when something
    # needs configuring sits at the far end with the other administrative
    # control rather than between two you use constantly.
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
    title: str, active: str, body: str, *, env: Env, exposed: bool = False
) -> str:
    nav = "".join(
        f'<a href="{path}"{" aria-current=page" if path == active else ""}>{label}</a>'
        for path, label in PAGES
    )
    if exposed:
        nav += '<a href="/logout">Sign out</a>'
    mode = "paper" if env.alpaca_paper_trade else "LIVE"
    return f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<link rel="icon" href="{FAVICON}">
<title>{_e(title)} &middot; Mudhorn Capital</title><style>{STYLES}</style></head>
<body data-mode="{_e(mode)}" data-operator="{_e(env.greeting_name)}">
<header class="bar"><div class="wrap">
  <a class="brand" href="/">{MARK} MUDHORN <span class="thin">CAPITAL</span></a>
  <nav>{nav}</nav>
  <span class="live paper" id="link"><i></i>{_e(mode)}<span class="link-label"></span></span>
</div></header>
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
        out.append(
            '<div class="banner warn"><b>These figures are not current</b>'
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
            f"{len(untracked)} held position(s) have no journal entry "
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
            f"The journal holds {len(stale)} open trade(s) the broker is not "
            f"reporting ({_e(', '.join(stale))}). Either they closed outside the "
            "bot, or reconciliation has not run since they did. Open risk below "
            "still counts them, so the real figure is lower.</div>"
        )

    if audit is not None and audit.is_degraded:
        detail = f"{audit.malformed} unreadable line(s)"
        if audit.unreadable_files:
            detail += f", {len(audit.unreadable_files)} unreadable file(s)"
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
            bits.append(f"{summary.new_decisions} cycle(s)")
        if summary.new_trades_closed:
            bits.append(f"{summary.new_trades_closed} trade(s) closed")
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


def _board_waiting(env: Env | None) -> str:
    """The Board before the first reading has arrived.

    Same shape as the real thing, so nothing jumps when the figures land: four
    tiles at the same size, in the same order, holding the width the numbers
    will occupy. `.pending` carries the shimmer; the text under each tile says
    what is being waited for rather than leaving a reader to guess whether the
    deck is broken.
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
        head(greeting(env) if env else "Account", "Board", "not read yet")
        + '<div class="banner" style="border-left-color:var(--holo)">'
        "<b>Reading the account</b>No broker reading has come back yet, so "
        "there are no figures to show. This is a cold start rather than an "
        "empty account: nothing here is zero, it is unknown. The live stream "
        "fills these in as soon as the first read lands, and the connection "
        "indicator beside the mode badge says whether it is getting through."
        "</div>"
        + f'<div class="grid g4">{tiles}</div>'
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
    """
    if account is None:
        return _board_waiting(env)

    equity = account.equity_usd
    open_risk_pct = (account.open_risk_usd / equity * 100) if equity else 0.0
    largest = max(
        (t.planned_risk_usd / equity * 100 for t in open_trades if equity), default=0.0
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
            f"across {len(account.open_positions)} position(s)",
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
            else f'<p class="note">Clear. {consecutive_losses} qualifying loss(es) '
            f"in a row against a trigger of "
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

    return (
        head(
            greeting(env) if env else "Account",
            "Board",
            stamp,
            asof_live=True,
        )
        + f'<div class="grid g4">{tiles}</div>'
        + '<section class="block"><h2>Equity</h2>'
        + _curve(curve)
        + "</section>"
        + '<section class="block"><h2>Risk against the caps</h2>'
        + f'<div class="grid g2">{risk_cards}</div></section>'
        + '<section class="block"><h2>Open positions</h2>'
        + _positions(account, open_trades, equity)
        + "</section>"
        + _working_orders(orders or [], prices or {})
    )


def _working_orders(orders: list[WorkingOrder], prices: dict[str, float]) -> str:
    """Orders resting at the broker, and how far the market is from filling them.

    The bot submits limit orders only, deliberately, so one that does not reach
    its price simply waits. Without this the Board shows no position and no
    explanation, when the truth is that an order is sitting there needing a move
    that may never come.
    """
    if not orders:
        return (
            '<section class="block"><h2>Pending orders</h2>'
            '<div class="scroll"><p class="empty">Nothing resting at the broker. '
            "Every order either filled or was never sent.</p></div></section>"
        )

    rows = ""
    for o in orders:
        price = prices.get(o.symbol)
        gap = o.distance_to_fill(price) if price else None
        gap_text = (
            "n/a"
            if gap is None
            else (f"{gap:+.2f}% away" if abs(gap) > 0.005 else "at the limit")
        )
        # A positive gap means the price still has to travel; that is the
        # difference between waiting and never filling.
        gap_cls = "muted" if gap is None or gap <= 0 else ""

        # Each cell is built before the row, never with a trailing conditional
        # on a multi-part f-string: the ternary binds to the whole expression,
        # not the last fragment, and silently eats the rest of the row.
        limit_cell = (
            f"{o.limit_price:,.4f}" if o.limit_price is not None else "market"
        )
        market_cell = f"{price:,.4f}" if price else "unknown"
        filled_note = (
            f' <span class="muted">({o.filled_qty:g} filled)</span>'
            if o.filled_qty
            else ""
        )
        submitted = _when(o.submitted_at) if o.submitted_at else "unknown"
        status = o.status.value.replace("_", " ")

        rows += (
            f'<tr class="data"><td data-l="Symbol"><b>{_e(o.symbol)}</b></td>'
            f'<td data-l="Side">{_e(o.direction.value)}</td>'
            f'<td data-l="Status"><span class="pill hold">{_e(status)}</span></td>'
            f'<td data-l="Qty" class="r num">{o.qty:g}{filled_note}</td>'
            f'<td data-l="Limit" class="r num">{limit_cell}</td>'
            f'<td data-l="Market" class="r num">{market_cell}</td>'
            f'<td data-l="Needs" class="r num {gap_cls}">{gap_text}</td>'
            f'<td data-l="Submitted">{_e(submitted)}</td></tr>'
        )

    return (
        '<section class="block"><h2>Pending orders</h2>'
        '<div class="scroll"><table><caption>"Needs" is how far the market still '
        "has to move for the limit to fill. A large positive number is an order "
        "that is not going to fill today.</caption>"
        "<thead><tr><th>Symbol</th><th>Side</th><th>Status</th><th class=r>Qty</th>"
        "<th class=r>Limit</th><th class=r>Market</th><th class=r>Needs</th>"
        f"<th>Submitted</th></tr></thead><tbody>{rows}</tbody></table></div></section>"
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
    return (
        f'<div class="curve"><svg viewBox="0 0 {width:.0f} {height:.0f}" role="img" '
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
        f'<text class="tick" x="8" y="{y(hi_v) - 5:.1f}">{_money(hi_v)}</text>'
        f'<text class="tick" x="8" y="{y(lo_v) + 13:.1f}">{_money(lo_v)}</text>'
        f'<text class="tick" x="8" y="{height - 8:.0f}">{_e(points[0][0][:10])}</text>'
        f'<text class="tick" x="{width - 8:.0f}" y="{height - 8:.0f}" '
        f'text-anchor="end">{_e(points[-1][0][:10])}</text>'
        "</svg>"
        f'<p class="note" style="margin:.75rem 0 0">The dashed line is where this '
        f"curve started. {len(points)} marks recorded.</p></div>"
    )


def _positions(
    account: AccountSnapshot, open_trades: list[Trade], equity: float
) -> str:
    if not account.open_positions:
        return (
            '<div class="scroll"><p class="empty">Nothing open. Most days the '
            "correct action is none.</p></div>"
        )

    by_symbol = {t.symbol: t for t in open_trades}
    rows: list[str] = []
    for p in account.open_positions:
        trade = by_symbol.get(p.symbol)
        stop = f"{trade.planned_stop:,.4f}" if trade else "unknown"
        risk = _money(trade.planned_risk_usd) if trade else "unknown"
        risk_pct = (
            _pct(trade.planned_risk_usd / equity * 100)
            if trade and equity
            else "unknown"
        )
        current = p.current_price or p.entry_price
        rows.append(
            f'<tr class="data"><td data-l="Symbol"><b>{_e(p.symbol)}</b></td>'
            f'<td data-l="Side">{_e(p.direction.value)}</td>'
            f'<td data-l="Qty" class="r num">{p.qty:g}</td>'
            f'<td data-l="Entry" class="r num">{p.entry_price:,.4f}</td>'
            f'<td data-l="Now" class="r num">{current:,.4f}</td>'
            f'<td data-l="Stop" class="r num">{stop}</td>'
            f'<td data-l="At risk" class="r num">{risk} '
            f'<span class="muted">({risk_pct})</span></td>'
            f'<td data-l="Unrealised" class="r num {_cls(p.unrealised_pnl_usd)}">'
            f"{_money(p.unrealised_pnl_usd, sign=True)}</td></tr>"
            + (
                f'<tr class="why"><td colspan="8"><div class="quote">'
                f"{_e(trade.rationale)}</div></td></tr>"
                if trade and trade.rationale
                else ""
            )
        )

    return (
        '<div class="scroll"><table><caption>A stop reading "unknown" means the '
        "position is held at the broker with no journal entry, so its risk cannot "
        "be counted. That is reported rather than guessed at.</caption>"
        "<thead><tr><th>Symbol</th><th>Side</th><th class=r>Qty</th>"
        "<th class=r>Entry</th><th class=r>Now</th><th class=r>Stop</th>"
        "<th class=r>At risk</th><th class=r>Unrealised</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


# ---------------------------------------------------------------- decisions


def decisions(view: AuditView, *, shown: int = 40) -> str:
    """The agent's decision trail: proposal, gate ruling, outcome.

    This is the only surface on which a REJECTED proposal is visible at all. It
    never becomes a trade, so it reaches neither the journal nor the broker, and
    the reasoning behind a refusal exists nowhere else.
    """
    lede = (
        "Every pass of the loop, newest first: what the model assessed, what it "
        "proposed, what the risk gate ruled and on which grounds, and what "
        "actually reached the broker. A rejected proposal appears here and "
        "nowhere else."
    )
    body = head("Trail", "Decisions", f"{len(view.decisions)} cycles", lede)

    if view.is_degraded:
        body += banners(StandDownState(), [], [], audit=view)

    if not view.decisions:
        body += (
            '<div class="card" style="margin-top:1.5rem"><p class="empty">'
            "No decisions recorded yet. The loop writes one entry per cycle to "
            "<code>audit/</code>, so this fills in once "
            "<code>electrum-bot loop</code> has run.</p></div>"
        )
        return body

    body += '<section class="block">'
    for entry in view.decisions[:shown]:
        body += _cycle(entry)
    body += "</section>"
    return body


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
        f"({len(assessments)} symbol(s)"
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
        '<p class="note" style="margin-top:.6rem">Advisory only. The loop does '
        "not act on these: closing a position and moving a stop sit outside the "
        "proposal path, so nothing here reached the broker.</p></div>"
    )


def _read(entry: DecisionEntry) -> str:
    """What the model was actually shown when it decided.

    Recorded with the decision rather than reconstructed later. A snapshot taken
    now answers a different question from the one an old cycle raises.
    """
    inputs = entry.decision.inputs
    if inputs is None:
        return ""

    parts = ""
    if inputs.headlines:
        items = "".join(f"<li>{_e(h)}</li>" for h in inputs.headlines[:8])
        more = (
            f'<li class="muted">and {len(inputs.headlines) - 8} more</li>'
            if len(inputs.headlines) > 8
            else ""
        )
        parts += f'<div class="rung"><span class="lbl">Headlines</span><ul class="feed">{items}{more}</ul></div>'
    else:
        parts += (
            '<div class="rung"><span class="lbl">Headlines</span>'
            '<span class="muted">none supplied. Marketaux gates nothing, so this '
            "is context the model did without.</span></div>"
        )

    if inputs.social_posts:
        items = "".join(f"<li>{_e(s)}</li>" for s in inputs.social_posts[:10])
        parts += (
            '<div class="rung"><span class="lbl">Posts</span>'
            f'<ul class="feed">{items}</ul></div>'
        )
    elif inputs.social_degraded:
        parts += (
            '<div class="rung gate no"><span class="lbl">Posts</span>'
            "the social feed was DEGRADED. An empty list here means the fetch "
            "failed, not that nothing was posted.</div>"
        )

    if inputs.news_windows:
        items = "".join(f"<li>{_e(w)}</li>" for w in inputs.news_windows)
        parts += f'<div class="rung"><span class="lbl">Blackouts</span><ul class="feed">{items}</ul></div>'
    elif inputs.calendar_degraded:
        parts += (
            '<div class="rung gate no"><span class="lbl">Blackouts</span>'
            "the earnings calendar was DEGRADED. Zero windows here means the feed "
            "failed, not that there were no announcements, and the blackout rule "
            "could not fire.</div>"
        )
    else:
        parts += (
            '<div class="rung"><span class="lbl">Blackouts</span>'
            '<span class="muted">no announcements inside the window</span></div>'
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
            '<div class="scroll" style="margin-top:.4rem"><table><tbody>'
            f"{rows}</tbody></table></div></div>"
        )

    return (
        '<details class="step"><summary class="eyebrow">What it read</summary>'
        f'<div class="chain" style="margin-top:.7rem">{parts}</div></details>'
    )


def _cycle(entry: DecisionEntry) -> str:
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
        cost = (
            f"{d.claude_input_tokens:,} in / {d.claude_output_tokens:,} out"
            + (f" / {d.claude_cached_tokens:,} cached" if d.claude_cached_tokens else "")
            + f" &middot; ${d.estimated_cost_usd:.4f}"
        )

    out = (
        '<article class="cycle"><div class="head">'
        f'<span class="when">{_e(_when(entry.timestamp))}</span>'
        f'<span class="pill {pill}">{entry.outcome}</span>'
        f'<span class="note">{len(d.proposals)} proposal(s), '
        f"{entry.approved} approved, {entry.rejected} rejected</span>"
        + (f'<span class="cost">{cost}</span>' if cost else "")
        + "</div>"
    )

    if d.notes:
        out += f'<div class="assessment"><q>{_e(d.notes)}</q></div>'

    out += _considered(entry)
    out += _held(entry)
    out += _read(entry)

    if not d.proposals:
        out += (
            '<div class="step"><p class="note">Nothing proposed. Doing nothing is '
            "a valid output and is recorded as one.</p></div>"
        )

    for i, proposal in enumerate(d.proposals):
        verdict = entry.verdict_for(i)
        out += '<div class="step"><div class="what">'
        out += (
            f"<b>{_e(proposal.direction.value.upper())} {proposal.qty:g} "
            f"{_e(proposal.symbol)}</b>"
            f'<span class="note">limit {proposal.limit_price:,.4f}, stop '
            f"{proposal.stop_loss_price:,.4f}, target "
            f"{proposal.take_profit_price:,.4f}</span>"
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
                '<span class="muted">no verdict recorded against this proposal. '
                "The loop skips a proposal whose symbol has no tick, so the lists "
                "no longer line up and pairing them would be a guess.</span></div>"
            )
        elif verdict.approved:
            out += '<div class="rung gate ok"><span class="lbl">Gate</span>approved</div>'
        else:
            reasons = "".join(f"<li>{_e(r)}</li>" for r in verdict.reasons)
            out += (
                '<div class="rung gate no"><span class="lbl">Gate</span>rejected'
                f'<ul class="reasons">{reasons}</ul></div>'
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

    return out + "</article>"


# ------------------------------------------------------------------- trades


def trades_page(recent: list[Trade], report: JournalReport) -> str:
    body = head(
        "Journal",
        "Trades",
        f"{report.overall.trade_count} closed",
        "Every closed trade with the reasoning recorded against it at the time. "
        "R is the result as a multiple of what the trade was designed to lose.",
    )

    if not recent:
        body += (
            '<div class="card" style="margin-top:1.5rem"><p class="empty">'
            "No closed trades yet.</p></div>"
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
        rows.append(
            f'<tr class="data"><td data-l="Symbol"><b>{_e(t.symbol)}</b><br>'
            f'<span class="note">{_e(t.strategy)}</span></td>'
            f'<td data-l="Held">{_e(t.entry_time.date().isoformat())}<br>'
            f'<span class="note">to {_e(exit_date)}</span></td>'
            f'<td data-l="Qty" class="r num">{t.qty:g}</td>'
            f'<td data-l="Entry" class="r num">{t.entry_price:,.4f}</td>'
            f'<td data-l="Stop" class="r num">{t.planned_stop:,.4f}</td>'
            f'<td data-l="Exit" class="r num">{exit_price}</td>'
            f'<td data-l="At risk" class="r num">{_money(t.planned_risk_usd)}</td>'
            f'<td data-l="Fees" class="r num muted">{_money(t.fees_usd)}</td>'
            f'<td data-l="Result" class="r num {_cls(t.net_pnl_usd)}">'
            f"{_money(t.net_pnl_usd, sign=True)}<br>"
            f'<span class="note">{r_text}</span></td></tr>' + rationale
        )

    body += (
        '<div class="scroll" style="margin-top:1.5rem"><table>'
        "<thead><tr><th>Symbol</th><th>Held</th><th class=r>Qty</th>"
        "<th class=r>Entry</th><th class=r>Stop</th><th class=r>Exit</th>"
        "<th class=r>At risk</th><th class=r>Fees</th><th class=r>Result</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )
    return body


# ---------------------------------------------------------------- analytics


def analytics_page(report: JournalReport) -> str:
    s = report.overall
    pf = f"{s.profit_factor:.2f}" if s.profit_factor is not None else "n/a"
    er = f"{s.expectancy_r:+.2f}R" if s.expectancy_r is not None else "n/a"

    body = head(
        "Measurement",
        "Analytics",
        f"{s.trade_count} closed trades",
        "A wrong metric is worse than no metric, because it gets believed and "
        "then acted on. Every figure here is computed by src/bot/metrics.py.",
    )
    body += (
        '<div class="grid g4" style="margin-top:1.5rem">'
        + stat("Win rate", f"{s.win_rate:.0%}", f"{s.wins}W / {s.losses}L")
        + stat("Profit factor", pf, _e(s.health))
        + stat(
            "Expectancy",
            _money(s.expectancy_usd, sign=True),
            f"per trade, {er}",
            _cls(s.expectancy_usd),
        )
        + stat(
            "Net",
            _money(s.total_pnl_usd, sign=True),
            f"max drawdown {_money(s.max_drawdown_usd)}",
            _cls(s.total_pnl_usd),
        )
        + "</div>"
    )
    body += (
        '<section class="block"><h2>Headline</h2>'
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
            '<div class="scroll"><table><thead><tr><th>Group</th>'
            "<th class=r>Trades</th><th class=r>Win rate</th><th class=r>Net</th>"
            "<th>Reading</th>"
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


def settings_page(rules: Rules, env: Env, *, chat_enabled: bool) -> str:
    """Structured, and read-only for anything that governs risk.

    A settings screen that could widen a limit would be a settings screen that
    gets used to widen one during a losing run, which is exactly when the limit
    is doing its job. So the limits are shown with their reasoning and with the
    file that owns them, and changing one stays a commit.
    """
    body = head(
        "Configuration",
        "Settings",
        "",
        "What the bot is currently configured to do. The risk limits are shown "
        "read-only on purpose: they change in a commit, by a person, with a "
        "reason, and never from a browser.",
    )

    a = rules.account
    body += (
        '<section class="block"><h2>Risk limits</h2><div class="grid g2">'
        '<div class="card"><h3>Per trade and across the book</h3><dl class="kv">'
        + _row(
            "Per trade",
            f"{a.max_risk_per_trade_pct:.2f}%",
            "Most any single position may lose if its stop fills.",
        )
        + _row(
            "Total open",
            f"{a.max_total_risk_pct:.2f}%",
            "Most everything open may lose at once. Measured in risk, not "
            "notional, which is what makes it leverage-neutral.",
        )
        + _row(
            "Concentration",
            f"{a.max_position_pct:.1f}%",
            "A backstop on one position's market value. Deliberately generous; "
            "it is not meant to be the binding constraint.",
        )
        + _row("Concurrent positions", str(a.max_concurrent_positions))
        + _row(
            "Daily loss kill",
            f"{a.daily_loss_kill_pct:.2f}%",
            "Sticky for the session. A recovery within the same day does not "
            "re-enable trading.",
        )
        + _row("Equity floor", _money(a.min_equity_floor_usd), "Below this the bot halts.")
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

    instrument_cards = ""
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
            + (
                _row("Capital cap", f"{inst.capital_cap_pct:.1f}%")
                if inst.capital_cap_pct is not None
                else ""
            )
            + "</dl></div>"
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
        + _row("Model tier", env.claude_tier.value)
        + '</dl><p class="source">Whether orders are actually placed is decided by '
        "the <code>--execute</code> flag on the service unit, not from here.</p></div>"
        '<div class="card"><h3>Credentials and feeds</h3><dl class="kv">'
        + _row("Alpaca", "configured" if env.alpaca_api_key else "not configured")
        + _row("Anthropic", "configured" if env.anthropic_api_key else "not configured")
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
        + _row(
            "X posts",
            (
                "off in rules.yaml"
                if not rules.social.enabled
                else ("configured" if env.x_bearer_token else "enabled but no token")
            ),
            (
                f"Watching {', '.join(rules.social.accounts)}. Context only."
                if rules.social.enabled and rules.social.accounts
                else "Accounts to watch live in the social block of rules.yaml."
            ),
        )
        + _row("Dashboard chat", "on" if chat_enabled else "off")
        + '</dl><p class="source">Presence only. No key is rendered on this page, '
        "on any surface, at any time.</p></div></div></section>"
    )

    # The dreamer is scheduled outside this process, so what it says here is
    # read from the unit rather than quoted from a constant. A Settings screen
    # holding its own copy of a cadence keeps announcing the old one forever
    # after somebody edits the timer on the box.
    schedule = read_schedule()
    per_run, per_year = estimated_cost_usd(env.dream_tier)
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
            "Model tier",
            env.dream_tier.value,
            "Set by DREAM_CLAUDE_TIER. It deliberately does not follow "
            "CLAUDE_TIER, because that defaults to a tier with no extended "
            "thinking and thinking is how a dream gets past its first hop.",
        )
        + _row(
            "Bought",
            "deep, not fast",
            "High effort, a large thinking budget and a 900s timeout. Nothing "
            "waits on this call and depth is the whole product.",
        )
        + _row(
            "Prompt cache",
            "off",
            "Deliberate. A 1h cache write bills at twice base input and this "
            "runs daily, so it would miss every time and pay double. The "
            "decision loop caches because it wakes every fifteen minutes.",
        )
        + _row(
            "Estimated cost",
            f"~${per_run:.3f} a run, ~${per_year:.0f} a year",
            "An estimate from the tier and a typical prompt. The real figure "
            "for a run that happened is logged with it.",
        )
        + _row(
            "Anthropic key",
            "configured" if env.anthropic_api_key else "not configured",
            (
                ""
                if env.anthropic_api_key
                else "Without it the command exits non-zero and writes nothing."
            ),
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


def _hop(hop: Hop) -> str:
    state = "checked" if hop.checked else "open"
    source = (
        f'<span class="src">{_e(hop.source)}</span>'
        if hop.checked and hop.source
        else '<span class="src">Not checked. Nobody has verified this hop.</span>'
    )
    return f'<li class="{state}">{_e(hop.claim)}{source}</li>'


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
        parts.append(
            f'<circle class="node {state}" cx="{x:.0f}" cy="26" r="{node_r:.0f}"/>'
            f'<text class="idx" x="{x:.0f}" y="31">{i + 1}</text>'
        )

    checked = sum(1 for h in hops if h.checked)
    label = (
        f"A chain of {len(hops)} hops, of which {checked} are checked. "
        "A broken connector marks a hop nobody has verified."
    )
    return (
        f'<svg class="chainviz" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'preserveAspectRatio="xMinYMid meet" role="img" '
        f'aria-label="{_e(label)}">' + "".join(parts) + "</svg>"
    )


def _dream(dream: Dream) -> str:
    """One mini-project, read top to bottom as an argument.

    The spark, the chain, the hop most likely to break it, then what was
    decided. Ordering it that way is deliberate: a reader who stops after two
    sections has read the idea and the reason to doubt it, which is the right
    pair to have if you only read two.
    """
    verdict = (
        f'<span class="pill {dream.verdict}">{_e(str(dream.verdict))}</span>'
        if dream.verdict
        else ""
    )
    verification = dream.verification
    out = (
        '<article class="dream"><div class="top">'
        f"<h3>{_e(dream.title)}</h3>"
        f'<span class="pill {dream.stage}">{_e(str(dream.stage))}</span>'
        f"{verdict}"
        f'<span class="pill {verification}">{_e(str(verification))}</span>'
        f'<span class="when">{_e(_when(dream.updated_at))}</span>'
        "</div>"
        f'<div class="spark">{_e(dream.seed)}'
        + (
            f'<span class="from">Sparked by {_e(dream.origin)}</span>'
            if dream.origin
            else ""
        )
        + '</div><div class="body">'
    )

    if dream.chain:
        out += _chain_diagram(dream)
        out += '<ol class="hops">' + "".join(_hop(h) for h in dream.chain) + "</ol>"
    else:
        out += '<p class="note">No chain recorded yet. Still a spark.</p>'

    # The weakest hop leads the commentary because confidence in a chain is the
    # minimum across its links rather than the average, and a reader given one
    # sentence should be given the one that could kill it.
    if dream.weakest_hop:
        out += (
            f'<p class="weak"><b>Weakest hop</b>{_e(dream.weakest_hop)}</p>'
        )
    elif dream.chain:
        out += (
            '<p class="weak"><b>Weakest hop</b>Not named. A chain without a '
            "stated weakest link has not been attacked yet.</p>"
        )

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

    if dream.instruments:
        out += (
            '<p class="trigger"><b>About:</b> '
            + _e(", ".join(dream.instruments))
            + ' <span class="muted">(subject matter, not an instruction)</span></p>'
        )

    if dream.thoughts:
        rows = "".join(
            f'<li><span class="st {t.stage}">{_e(str(t.stage))}</span>'
            f"<span>{_e(t.text)}</span></li>"
            for t in dream.thoughts
        )
        out += (
            f'<details class="stream"><summary>The working, '
            f"{len(dream.thoughts)} step(s)</summary><ol>{rows}</ol></details>"
        )

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
        f"{ledger.resolved} resolved chain(s). Below "
        f"{THIN_LEDGER_THRESHOLD} this is a thin sample and every rate above is "
        "noise, which is stated here for the same reason the Analytics page "
        "states it: a rate without its sample count gets believed anyway.</p>"
        if ledger.sample_is_thin
        else ""
    )

    flags = ""
    if ledger.untriggered_keeps:
        flags += (
            f'<p class="note">{ledger.untriggered_keeps} kept idea(s) name no '
            "trigger. Those are notes rather than watches.</p>"
        )
    if ledger.unattacked:
        flags += (
            f'<p class="note">{ledger.unattacked} chain(s) have no stated weakest '
            "hop, so nobody has tried to break them yet.</p>"
        )

    return (
        '<section class="block"><h2>What it has learned about its own thinking</h2>'
        '<p class="note" style="max-width:68ch;margin-bottom:1rem">Counted over the '
        "chains below. Deliberately nothing about what any idea would have "
        "earned: the dreamer does not learn from profit and loss, because forty "
        "trades is noise and a model shown three losses will confidently change "
        "approach. These are facts about the reasoning, true regardless of how a "
        "trade went, and they reach you rather than it.</p>"
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
            "hops. Two is the minimum it aims for",
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


def dreaming_page(
    dreams: list[Dream],
    summary: DreamSummary,
    *,
    enabled: bool,
    token: str,
    hermes_available: bool,
    soul_found: bool,
    isolated: bool = False,
) -> str:
    """The dreamer's deck: what it is thinking about, and a way to talk to it.

    The warning at the top is not decoration and is not dismissible. Everything
    on this page is speculation, produced by a model that is good at sounding
    certain, on a surface that otherwise reports measured facts about real
    money. A reader arriving mid-page has to be told which of the two they are
    looking at, in the same way the public site labels its invented figures.
    """
    body = (
        '<div class="dream-head">'
        + DREAMER
        + '<div class="who"><p class="eyebrow">Grogu</p><h1>Dreaming</h1>'
        "<p class=note>Second-order ideas, thought about in public. It reaches "
        "for connections nobody asked for, records the chain link by link, "
        "attacks it, and reaches a verdict.</p></div></div>"
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
        "acting on is read by a person and acted on through the ordinary "
        "machinery, in their own time.</div>"
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

    body += (
        '<div class="grid g4" style="margin-top:1.5rem">'
        + stat("Open", str(summary.open_dreams), "still being thought about")
        + stat("Kept", str(summary.kept), "chain held, worth watching")
        + stat("Parked", str(summary.parked), "interesting, not now")
        + stat("Dropped", str(summary.dropped), "broke on inspection")
        + "</div>"
    )

    body += _ledger(DreamLedger.of(dreams))

    body += '<section class="block"><h2>Thoughts</h2>'
    if dreams:
        if summary.unverified:
            body += (
                f'<p class="note" style="margin-bottom:1rem">{summary.unverified} of '
                f"{summary.total} rest entirely on hops nobody has checked. They are "
                "marked <span class=\"pill unverified\">unverified</span> and that "
                "is a statement about evidence, not about how likely they are.</p>"
            )
        body += "".join(_dream(d) for d in dreams)
    else:
        body += (
            '<p class="note" style="margin-bottom:1rem">Nothing recorded yet. The '
            "dreamer writes here as it works; until then, here is the shape one "
            "takes.</p>" + WORKED_EXAMPLE
        )
    body += "</section>"

    body += '<section class="block"><h2>Talk to it</h2>'
    if not enabled:
        return body + (
            '<div class="banner warn"><b>Chat is off</b>'
            "Set <code>DASHBOARD_CHAT_TOKEN</code> in the environment to enable "
            "it. The thoughts above are rendered from "
            "<code>data/dreams.db</code> and do not need it.</div></section>"
        )
    if not hermes_available:
        return body + (
            '<div class="banner crit"><b>Hermes not found</b>'
            "The token is set, but the Hermes binary is not installed where this "
            "process expects it. See <code>docs/HERMES_SETUP.md</code>.</div></section>"
        )

    return (
        body
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
    )


# -------------------------------------------------------------------- login


def login_page(*, env: Env, error: str = "") -> str:
    """The gate. Deliberately says nothing about the account behind it.

    No equity, no positions, no symbol list, not even whether a trade has ever
    been placed. Everything this page renders is already public in the GitHub
    repository, so an unauthenticated visitor learns nothing they could not
    read there. That is the whole job of a sign-in screen and it is easy to
    lose by putting a friendly summary above the form.
    """
    mode = "paper" if env.alpaca_paper_trade else "LIVE"
    message = f'<p class="err" role="alert">{_e(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<link rel="icon" href="{FAVICON}">
<title>Sign in &middot; Mudhorn Capital</title><style>{STYLES}</style></head>
<body data-mode="{_e(mode)}">
<header class="bar"><div class="wrap">
  <span class="brand">{MARK} MUDHORN <span class="thin">CAPITAL</span></span>
  <span class="live paper"><i></i>{_e(mode)}</span>
</div></header>
<main><div class="wrap"><div class="gate"><div class="panel">
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
