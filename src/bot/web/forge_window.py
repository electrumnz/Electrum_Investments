"""The forge window: what the beskar looks like before the strike, and after.

The Armorer already states what a change costs. What it could not do was show
the operator the two numbers side by side and make them agree to the second one
as a separate act. The confirmation was a sentence and a button, which is the
right *arrangement* rendered as the least memorable thing on the page.

So this is a dialog, and three properties are the whole reason it exists rather
than being decoration:

- **Both values are on screen at once.** "Raise it to 2.0" is a number with
  nothing to compare it against; `1.0 -> 2.0` is a change. The old value is the
  exact text that is on the line in `config/rules.yaml`, never a re-rendering of
  the parsed number, for the same reason `ChangeRequest.old_value` keeps it that
  way: `90000` and `90000.0` are the same limit and two different diffs.
- **Confirming is a distinct act from asking.** The asymmetry the forge is built
  around — a tightening change is recorded as asked, a loosening one has to be
  agreed to after its cost has been read — is enforced server-side and always
  was. This makes it *visible*, which is the half that was missing.
- **It cannot invent a figure.** Everything it renders arrives in the response
  that opened it. There is no arithmetic in this file and no fetch; it is a
  window onto a payload, and `strike` merely resolves a promise the caller
  already knows what to do with.

**The fail-to-visible rule applies, in the form it takes for a control.**
`render.STYLES` may never hide a figure that JavaScript reveals — a blocked
script has to leave every number on screen. Nothing here is hidden CSS-side:
the dialog does not exist in the markup at all until the client builds it, and
the caller keeps its inline fallback for the case where this module did not
load. So a throw up here costs the operator a nicer confirmation, never the
ability to confirm, and never a figure. Same principle as the Cmd+K console
falling back to an ordinary navigation when the projection layer is absent.

**Under `prefers-reduced-motion` the window still opens.** The preference
switches off the heat sweep and the entry transform and nothing else. Somebody
asking for fewer moving pixels is not asking to be denied the only route to
agreeing to a change — that is the same reading the console palette got right
and the starfield gets to ignore.
"""

from __future__ import annotations

__all__ = ["CSS", "SCRIPT"]


# ---------------------------------------------------------------- the styling
#
# **Never put a backslash in here.** This is an ordinary Python string, so a CSS
# hex escape (`content:"\2192"`) is read by *Python* first, as an octal escape:
# the stylesheet receives a control character and the browser draws a tofu box
# beside the leftover digits. Nothing warns and `ruff` does not care. Every
# glyph below is the literal character. `tests/test_web.py` fails the build if a
# control character reaches `render.STYLES`, which this is concatenated into.
#
# The two-class lesson applies as well, and it has bitten this repository three
# times: `.fw-ingot.after` is styled by DECLARATION ORDER, so the stance
# modifiers sit after the base rule rather than trusting specificity to sort it
# out.
CSS = """
/* The forge window. Beskar on the anvil: what it is, what it would become. */
.fw-scrim{position:fixed;inset:0;z-index:60;background:rgba(11,14,18,.82);
  display:flex;align-items:flex-start;justify-content:center;
  padding:clamp(1rem,6vh,4rem) 1rem;overflow-y:auto;
  -webkit-overflow-scrolling:touch;overscroll-behavior:contain}
.fw{width:min(46rem,100%);background:var(--graphite);
  border:1px solid var(--slate);border-radius:3px;
  box-shadow:0 24px 60px rgba(0,0,0,.55);position:relative}
.fw:focus{outline:none}

/* The heat bar. It is the stance, read before any word is. */
.fw-heat{height:2px;background:var(--slate);border-radius:3px 3px 0 0;
  overflow:hidden}
.fw-heat i{display:block;height:100%;width:100%;transform-origin:left center;
  background:linear-gradient(90deg,var(--slate),var(--patina))}
.fw.loosening .fw-heat i{background:linear-gradient(90deg,var(--rust),
  var(--amber),#E8B26A)}

.fw-head{padding:1.1rem 1.35rem .9rem;border-bottom:1px solid var(--slate)}
.fw-head .eyebrow{margin:0 0 .3rem}
.fw-head h3{margin:0;font-size:1.0625rem;letter-spacing:.01em}
.fw-head code{font-size:.75rem;color:var(--pewter)}
.fw-body{padding:1.15rem 1.35rem}

/* The anvil: two ingots and the strike between them. On a narrow screen it
   stacks, and the strike mark rotates a quarter turn so the arrow still points
   from the old value at the new one rather than off the side of the phone. */
.fw-anvil{display:grid;grid-template-columns:1fr auto 1fr;align-items:stretch;
  gap:.75rem}
.fw-ingot{border:1px solid var(--slate);border-radius:2px;padding:.85rem .9rem;
  background:var(--ink);min-width:0}
.fw-ingot .k{display:block;font-family:var(--mono);font-size:.5625rem;
  letter-spacing:.16em;text-transform:uppercase;color:var(--pewter);
  margin-bottom:.45rem}
.fw-ingot .v{display:block;font-family:var(--mono);
  font-variant-numeric:tabular-nums;font-size:1.5rem;line-height:1.15;
  color:var(--bone);overflow-wrap:anywhere}
.fw-ingot .u{display:block;font-size:.75rem;color:var(--pewter);
  margin-top:.3rem}
/* Declaration order, not specificity: `after` is the base coat and the stance
   modifiers are painted over it. Both are one class deep, so whichever is
   written last wins, and writing them in the other order would silently leave
   every window cold. */
.fw-ingot.after{border-color:var(--pewter)}
.fw-ingot.after.tightening{border-color:var(--patina);
  box-shadow:inset 0 0 0 1px rgba(78,140,125,.35),0 0 18px rgba(78,140,125,.16)}
.fw-ingot.after.loosening{border-color:var(--amber);
  box-shadow:inset 0 0 0 1px rgba(192,138,62,.40),0 0 22px rgba(192,138,62,.20)}
.fw-ingot.after.tightening .v{color:var(--patina)}
.fw-ingot.after.loosening .v{color:#E8B26A}

.fw-strike{display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:.4rem;padding:0 .15rem;color:var(--pewter)}
.fw-strike .mark{font-size:1.1rem;line-height:1;color:var(--pewter)}
.fw-strike .lbl{font-family:var(--mono);font-size:.5rem;letter-spacing:.16em;
  text-transform:uppercase}
.fw.loosening .fw-strike .mark{color:var(--amber)}
.fw.tightening .fw-strike .mark{color:var(--patina)}

.fw-sect{margin-top:1.15rem}
.fw-sect h4{margin:0 0 .5rem;font-family:var(--mono);font-size:.625rem;
  letter-spacing:.16em;text-transform:uppercase;color:var(--pewter)}
.fw-sect ul{margin:0 0 0 1.05rem;padding:0}
.fw-sect li{font-size:.8125rem;color:var(--bone);margin-bottom:.4rem}
.fw-sect p{margin:0;font-size:.8125rem;color:var(--bone)}
.fw-sect pre{white-space:pre-wrap;word-break:break-word;margin:0}
.fw-said{border-left:3px solid var(--rust);padding-left:.8rem}
.fw-said p{color:var(--bone)}

/* The creed. It is the one line on this deck allowed to be a slogan, and it
   changes with the stance because agreeing to less armour is not the same act
   as agreeing to more. */
.fw-creed{margin-top:1.15rem;padding-top:.85rem;border-top:1px solid var(--slate);
  font-family:var(--mono);font-size:.6875rem;letter-spacing:.06em;
  color:var(--pewter)}
.fw.loosening .fw-creed{color:var(--amber)}

.fw-foot{display:flex;flex-wrap:wrap;gap:.6rem;align-items:center;
  padding:1rem 1.35rem 1.2rem;border-top:1px solid var(--slate)}
.fw-foot .spacer{flex:1 1 auto}
.fw-foot .btn.strike{border-color:var(--patina);color:var(--bone)}
.fw-foot .btn.strike:hover{background:rgba(78,140,125,.14)}
.fw.loosening .fw-foot .btn.strike{border-color:var(--amber)}
.fw.loosening .fw-foot .btn.strike:hover{background:rgba(192,138,62,.14)}

@media (max-width:640px){
  .fw-anvil{grid-template-columns:1fr;gap:.5rem}
  .fw-strike{flex-direction:row;gap:.5rem;padding:.1rem 0}
  .fw-strike .mark{transform:rotate(90deg)}
  .fw-ingot .v{font-size:1.3125rem}
}

/* Motion. The window ARRIVES; it is not revealed, so none of this is load
   bearing and all of it is switched off below rather than slowed down. */
.fw{animation:fw-rise .22s ease-out both}
.fw-scrim{animation:fw-fade .16s ease-out both}
.fw-heat i{animation:fw-forge .55s ease-out both}
@keyframes fw-fade{from{opacity:0}to{opacity:1}}
@keyframes fw-rise{from{opacity:0;transform:translate3d(0,10px,0)}
  to{opacity:1;transform:translate3d(0,0,0)}}
@keyframes fw-forge{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@media (prefers-reduced-motion:reduce){
  .fw,.fw-scrim,.fw-heat i{animation:none}
}
"""


# ---------------------------------------------------------------- the client
#
# A plain string with no placeholder, because nothing is interpolated into it.
# That is deliberate rather than incidental: this file renders operator-supplied
# text and server-supplied figures, and the way it stays safe is that every one
# of them goes in through `textContent`. Building nodes means the client cannot
# be the place that stops escaping — the same reasoning `FORGE_SCRIPT` records,
# arriving in a second file.
#
# The contract, and the caller keeps a fallback for when this module is absent:
#
#     window.MUDHORN_FORGE.confirm({
#       key, label, unit, stance,          // strings
#       oldText, newText,                  // the exact file text, both sides
#       consequence: [ ... ],              // what the Armorer computed
#       objection, diff                    // both optional
#     }).then(function (agreed) { ... })
#
# It resolves `true` for a strike and `false` for every other way out — the
# cancel button, Escape, and a click on the scrim. **False is the resolution,
# never a rejection.** A dismissed dialog is an answer, and modelling it as an
# error would put "the operator said no" down the same path as "the browser
# threw", which is how a refusal quietly turns into a stack trace nobody reads.
SCRIPT = """
<script>
(function () {
  if (window.MUDHORN_FORGE) return;

  function el(tag, text, cls) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function ingot(kind, label, value, unit, stance) {
    var box = el('div', null, 'fw-ingot ' + kind + (stance ? ' ' + stance : ''));
    box.appendChild(el('span', label, 'k'));
    box.appendChild(el('span', value, 'v'));
    if (unit) box.appendChild(el('span', unit, 'u'));
    return box;
  }

  function section(title, build) {
    var s = el('div', null, 'fw-sect');
    s.appendChild(el('h4', title));
    build(s);
    return s;
  }

  /* The creed changes with the stance because the two acts are different.
     Agreeing to more armour is confirmation; agreeing to less is a decision,
     and the line should not congratulate somebody for making it. */
  function creed(stance) {
    if (stance === 'loosening') {
      return 'Loosening beskar is still forging it. What comes off the anvil '
        + 'is what protects the account.';
    }
    if (stance === 'tightening') return 'Thicker plate. This is the Way.';
    return 'The value does not move. Nothing will be written.';
  }

  function confirm(d) {
    return new Promise(function (resolve) {
      var stance = d.stance || '';
      /* Checked, never assumed. The strike button is usually pressed with
         nothing focused, and `body.focus()` is a silent no-op that raises no
         error while stranding a keyboard user at the top of the document. */
      var opener = document.activeElement;
      var settled = false;

      var scrim = el('div', null, 'fw-scrim');
      var win = el('div', null, 'fw ' + stance);
      win.setAttribute('role', 'dialog');
      win.setAttribute('aria-modal', 'true');
      win.setAttribute('aria-labelledby', 'fw-title');
      win.tabIndex = -1;

      var heat = el('div', null, 'fw-heat');
      heat.appendChild(el('i'));
      win.appendChild(heat);

      var head = el('div', null, 'fw-head');
      head.appendChild(el('p', 'On the anvil', 'eyebrow'));
      var h = el('h3', d.label || d.key);
      h.id = 'fw-title';
      head.appendChild(h);
      head.appendChild(el('code', d.key));
      win.appendChild(head);

      var body = el('div', null, 'fw-body');

      var anvil = el('div', null, 'fw-anvil');
      anvil.appendChild(ingot('before', 'As it stands', d.oldText, d.unit, ''));
      var strike = el('div', null, 'fw-strike');
      strike.appendChild(el('span', '→', 'mark'));
      strike.appendChild(el('span', 'strike', 'lbl'));
      anvil.appendChild(strike);
      anvil.appendChild(ingot('after', 'As it would be', d.newText, d.unit, stance));
      body.appendChild(anvil);

      var lines = d.consequence || [];
      if (lines.length) {
        body.appendChild(section('What this costs', function (s) {
          var ul = el('ul');
          lines.forEach(function (line) { ul.appendChild(el('li', line)); });
          s.appendChild(ul);
        }));
      }

      if (d.objection && String(d.objection).trim()) {
        body.appendChild(section('The Armorer says', function (s) {
          var q = el('div', null, 'fw-said');
          q.appendChild(el('p', d.objection));
          s.appendChild(q);
        }));
      }

      if (d.diff && String(d.diff).trim()) {
        body.appendChild(section('The cut in the file', function (s) {
          s.appendChild(el('pre', d.diff, 'readout'));
        }));
      }

      body.appendChild(el('p', creed(stance), 'fw-creed'));
      win.appendChild(body);

      var foot = el('div', null, 'fw-foot');
      var yes = el('button', 'Strike it', 'btn strike');
      yes.type = 'button';
      var no = el('button', 'Let it cool', 'btn');
      no.type = 'button';
      foot.appendChild(yes);
      foot.appendChild(no);
      foot.appendChild(el('span', null, 'spacer'));
      win.appendChild(foot);

      scrim.appendChild(win);

      function close(agreed) {
        if (settled) return;
        settled = true;
        document.removeEventListener('keydown', onKey, true);
        if (scrim.parentNode) scrim.parentNode.removeChild(scrim);
        if (opener && typeof opener.focus === 'function'
            && opener !== document.body) {
          opener.focus({ preventScroll: true });
        }
        resolve(!!agreed);
      }

      /* Focus stays inside while the window is up. Tab off the last control
         and it comes back to the first, so the operator cannot end up typing
         into the page behind a dialog that is asking them a question. */
      function onKey(e) {
        if (e.key === 'Escape') { e.preventDefault(); close(false); return; }
        if (e.key !== 'Tab') return;
        var stops = [yes, no];
        var i = stops.indexOf(document.activeElement);
        e.preventDefault();
        var next = e.shiftKey ? i - 1 : i + 1;
        if (i < 0) next = e.shiftKey ? stops.length - 1 : 0;
        if (next < 0) next = stops.length - 1;
        if (next >= stops.length) next = 0;
        stops[next].focus();
      }

      yes.addEventListener('click', function () { close(true); });
      no.addEventListener('click', function () { close(false); });
      scrim.addEventListener('click', function (e) {
        if (e.target === scrim) close(false);
      });
      document.addEventListener('keydown', onKey, true);

      document.body.appendChild(scrim);
      /* `no` rather than `yes`. The default target of a stray Enter must be
         the way out, not the way through -- the whole point of this window is
         that agreeing is a separate, deliberate act. */
      no.focus({ preventScroll: true });
    });
  }

  window.MUDHORN_FORGE = { confirm: confirm };
})();
</script>
"""
