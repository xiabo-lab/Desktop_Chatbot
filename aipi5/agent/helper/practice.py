"""A page for practising hand control, and nothing else.

Built because "it does not work" and "I am not holding my hand right" are
indistinguishable on a real website. YouTube gives no feedback about a gesture
that was *nearly* a sweep, a click that landed two centimetres low, or a Back
that fired when a scroll was meant. This page answers all three, out loud, in
letters readable from across the room.

**It is a `data:` URL, and that is a security decision rather than a
convenience.** The obvious thing would be to serve it from the assistant's own
web server and open `http://127.0.0.1:8092/practice`. `_check_url` refuses
exactly that, on purpose: the UI server has unauthenticated POST routes because
it is on loopback and only the kiosk can reach it, so a page in the *agent's*
browser reaching loopback would turn "open a web page" into "restart the
assistant". A `data:` URL has an opaque origin. It cannot fetch, cannot read a
file, cannot reach localhost, and cannot be aimed anywhere by the caller --
this op takes no arguments at all.

**The cursor here is a drawn hand**, which the pointer on a real page cannot
be. That one is an `Overlay` rectangle because marking somebody else's page
without running script in it is all `Overlay` can do. This page is ours, so its
own script may draw whatever it likes -- and `Input.dispatchMouseEvent` sends
real `mousemove` and `mousedown` events, which any page can listen for.
"""

PAGE = r"""<!doctype html>
<meta charset="utf-8">
<title>Hand control practice</title>
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; }
  /* The track down the right *is* the scrollbar here, drawn big enough
     to read from across the room. The real one only sat behind it. */
  html { scrollbar-width: none; }
  ::-webkit-scrollbar { display: none; }
  body {
    background: #12161d; color: #e8edf5;
    font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
    cursor: none;                     /* the drawn hand is the cursor */
    padding-right: 92px;              /* room for the scroll track */
  }

  /* ── the bar across the top ───────────────────────────────────── */
  header {
    position: fixed; inset: 0 92px auto 0; height: 74px; z-index: 30;
    display: flex; align-items: center; gap: 18px; padding: 0 18px;
    background: #1b2230; border-bottom: 2px solid #2c3648;
  }
  .arrow {
    width: 62px; height: 50px; flex: none; border: 0; cursor: none;
    display: flex; align-items: center; justify-content: center;
    font-size: 30px; font-weight: 700; text-decoration: none;
    color: #12161d; background: #7dd3fc; border-radius: 10px;
  }
  .arrow.dim { background: #33405a; color: #7c8aa5; }
  .where { font-size: 22px; font-weight: 700; }
  .hint { color: #93a4bf; font-size: 15px; }
  .score {
    margin-left: auto; font-size: 22px; font-weight: 700; color: #4ade80;
    font-variant-numeric: tabular-nums;
  }
  #said {
    position: fixed; top: 74px; left: 0; right: 92px; z-index: 29;
    padding: 10px 18px; background: #0b3a2e; color: #7bf1a8;
    font-size: 19px; font-weight: 600; min-height: 44px;
  }

  /* ── the scroll track down the right ──────────────────────────── */
  #track {
    position: fixed; top: 0; right: 0; bottom: 0; width: 92px; z-index: 30;
    background: #1b2230; border-left: 2px solid #2c3648;
  }
  #thumb {
    position: absolute; right: 10px; width: 72px; border-radius: 12px;
    background: #fbbf24; border: 2px solid #12161d;
  }
  #depth {
    position: absolute; left: 0; right: 0; bottom: 12px; text-align: center;
    font-size: 15px; color: #93a4bf; font-variant-numeric: tabular-nums;
  }

  /* ── the page itself ──────────────────────────────────────────── */
  /* Clear of both fixed bars: the header is 74 and the readout under it
     is 44, and the first heading was landing behind them. */
  main { padding: 140px 26px 140px; max-width: 900px; }
  h1 { font-size: 27px; margin: 0 0 6px; }
  p.lead { color: #93a4bf; margin: 0 0 26px; font-size: 17px; }
  .band { margin: 0 0 34px; }
  .band h2 { font-size: 15px; letter-spacing: .12em; text-transform: uppercase;
             color: #7c8aa5; margin: 0 0 10px; font-weight: 600; }
  .target {
    display: flex; align-items: center; justify-content: center;
    height: 116px; border-radius: 14px; font-size: 25px; font-weight: 700;
    background: #263449; border: 3px solid #3a4c69; color: #cfe0f5;
    user-select: none;
  }
  .target.hit { background: #14532d; border-color: #4ade80; color: #86efac; }
  .filler { color: #5d6b83; font-size: 15px; line-height: 2.1; }

  /* ── the hand ─────────────────────────────────────────────────── */
  #hand {
    position: fixed; z-index: 40; pointer-events: none;
    width: 54px; height: 66px; margin: -33px 0 0 -27px;
    transition: transform .06s linear; display: none;
  }
  #hand.on { display: block; }
  #hand.shut { transform: scale(.82); }
</style>

<header>
  <button class="arrow" id="back" type="button">&#8592;</button>
  <button class="arrow" id="next" type="button">&#8594;</button>
  <div>
    <div class="where" id="where">Sheet 1 of 4</div>
    <div class="hint">open palm sweeps &#183; fist clicks</div>
  </div>
  <div class="score" id="score">0 / 5 hit</div>
</header>
<div id="said">Raise an open palm where the camera can see it.</div>

<div id="track"><div id="thumb"></div><div id="depth">top</div></div>

<svg id="hand" viewBox="0 0 44 54" aria-hidden="true">
  <g id="palm" fill="#fbbf24" stroke="#12161d" stroke-width="2.4"
     stroke-linejoin="round">
    <path d="M8 26V12a3.4 3.4 0 016.8 0v11"/>
    <path d="M14.8 23V8a3.4 3.4 0 016.8 0v15"/>
    <path d="M21.6 23V9.5a3.4 3.4 0 016.8 0V24"/>
    <path d="M28.4 25V14a3.4 3.4 0 016.8 0v16"/>
    <path d="M8 26c0-4-6-8-6-3 0 3 2 8 4 12 3 6 6 13 6 17h21c0-6 2-12 2-18V22"/>
  </g>
</svg>

<main id="sheet"></main>

<script>
(() => {
  "use strict";
  const SHEETS = 4;
  const TARGETS = 5;

  const say = (text) => { document.getElementById("said").textContent = text; };

  // **History without a URL.** The obvious way to give back and forward
  // something to walk is a link per sheet -- `href="#2"` and so on. It does
  // not work here: this page is a `data:` URL, its origin is opaque, and
  // fragment navigation is refused. Measured, not assumed: after two clicks
  // of the arrow the address still had no fragment at all, and Back went
  // straight past the page to `about:blank`.
  //
  // `pushState` with the URL left out is the way through. It adds a real
  // history entry without touching an address it is not allowed to touch, and
  // `Page.navigateToHistoryEntry` -- which is what a sideways sweep runs --
  // moves between those entries and fires `popstate` here.
  let sheet = 1;
  const sheetNow = () => sheet;

  // ── the hand, drawn from real mouse events ──────────────────────
  // `Input.dispatchMouseEvent` produces ordinary `mousemove` and `mousedown`,
  // so nothing here knows or cares that a camera is driving it.
  const hand = document.getElementById("hand");
  let shutUntil = 0;
  addEventListener("mousemove", (event) => {
    hand.classList.add("on");
    hand.style.left = event.clientX + "px";
    hand.style.top = event.clientY + "px";
  }, { passive: true });
  addEventListener("mousedown", () => {
    hand.classList.add("shut");
    shutUntil = Date.now() + 260;
    setTimeout(() => {
      if (Date.now() >= shutUntil) hand.classList.remove("shut");
    }, 280);
  }, { passive: true });

  // ── the scroll track ────────────────────────────────────────────
  const thumb = document.getElementById("thumb");
  const depth = document.getElementById("depth");
  let lastY = 0;
  function drawTrack() {
    const max = Math.max(1, document.body.scrollHeight - innerHeight);
    const seen = Math.min(1, innerHeight / document.body.scrollHeight);
    const high = Math.max(60, seen * innerHeight);
    thumb.style.height = high + "px";
    thumb.style.top = ((innerHeight - high) * (scrollY / max)) + "px";
    const pct = Math.round(100 * scrollY / max);
    depth.textContent = pct <= 0 ? "top" : pct >= 99 ? "end" : pct + "%";
  }
  addEventListener("scroll", () => {
    const moved = scrollY - lastY;
    if (Math.abs(moved) > 4) {
      say((moved > 0 ? "scrolled DOWN " : "scrolled UP ") +
          Math.abs(Math.round(moved)) + "px");
      lastY = scrollY;
    }
    drawTrack();
  }, { passive: true });
  addEventListener("resize", drawTrack);

  // ── one sheet ───────────────────────────────────────────────────
  let hits = 0;
  function build() {
    const n = sheetNow();
    document.getElementById("where").textContent =
      "Sheet " + n + " of " + SHEETS;
    document.getElementById("back").className =
      "arrow" + (n > 1 ? "" : " dim");
    document.getElementById("next").className =
      "arrow" + (n < SHEETS ? "" : " dim");

    hits = 0;
    const main = document.getElementById("sheet");
    main.innerHTML = "";
    const head = document.createElement("div");
    head.innerHTML =
      "<h1>Sheet " + n + "</h1><p class='lead'>Close your hand on each panel." +
      " Sweep down to reach the ones below. Sweep sideways to change sheet." +
      "</p>";
    main.appendChild(head);

    for (let i = 1; i <= TARGETS; i++) {
      const band = document.createElement("div");
      band.className = "band";
      const label = document.createElement("h2");
      label.textContent = "target " + i + " of " + TARGETS;
      const box = document.createElement("div");
      box.className = "target";
      box.textContent = "click me";
      box.addEventListener("click", () => {
        if (box.classList.contains("hit")) {
          say("already hit — target " + i);
          return;
        }
        box.classList.add("hit");
        box.textContent = "✓  hit";
        hits += 1;
        document.getElementById("score").textContent =
          hits + " / " + TARGETS + " hit";
        say(hits >= TARGETS
            ? "all " + TARGETS + " hit — sweep sideways for the next sheet"
            : "HIT target " + i);
      });
      band.appendChild(label);
      band.appendChild(box);
      main.appendChild(band);

      if (i < TARGETS) {
        const gap = document.createElement("div");
        gap.className = "filler";
        // Long enough that each target needs its own sweep to reach. The
        // first version fitted the whole sheet in two, which practises
        // nothing: the point is to do the movement five times and find out
        // which of them the camera reads.
        gap.textContent =
          ("keep sweeping down to the next target · ").repeat(46);
        main.appendChild(gap);
      }
    }
    document.getElementById("score").textContent = "0 / " + TARGETS + " hit";
    scrollTo(0, 0);
    lastY = 0;
    drawTrack();
  }

  function goSheet(n, push) {
    n = Math.min(SHEETS, Math.max(1, n));
    if (n === sheet && push) return;
    sheet = n;
    if (push) history.pushState({ sheet: n }, "");
    build();
    say("sheet " + n + " of " + SHEETS +
        " — sweep sideways to move between them");
  }

  document.getElementById("back").addEventListener("click", () => {
    // The arrow moves a sheet the way a sweep does: through history, so the
    // two agree and either can undo the other.
    if (sheet > 1) history.back(); else say("this is the first sheet");
  });
  document.getElementById("next").addEventListener("click", () => {
    if (sheet < SHEETS) goSheet(sheet + 1, true);
    else say("this is the last sheet");
  });

  addEventListener("popstate", (event) => {
    goSheet((event.state && event.state.sheet) || 1, false);
  });

  history.replaceState({ sheet: 1 }, "");
  build();
})();
</script>
"""


def data_url() -> str:
    """The page as a `data:` URL, which is the only form it is ever opened in."""
    import base64

    encoded = base64.b64encode(PAGE.encode("utf-8")).decode("ascii")
    return "data:text/html;base64," + encoded
