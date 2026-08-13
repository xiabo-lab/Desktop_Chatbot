"""A QR code for the picker URL, because this device has no keyboard.

The Google Photos picker has to be opened in a browser signed in to the
account, and the only browser on this Pi is a full-screen kiosk window with no
address bar and nothing to type on — see `scripts/aipi5-ui.sh`, which turns off
the address bar, the context menu and the on-screen prompts on purpose. So the
URL goes on the screen as a square, the person points their phone at it, and
they pick there.

`segno` is a pure-Python QR encoder with no dependencies of its own. **The
import is optional**, and that is not caution about the library — it is the
deployment story: `~/AIPI5` on the device is updated by copying files, not by
`pip install`, so a new requirement arrives one release before it is installed.
Without it the page shows the URL as text, which is unusable on a kiosk but
perfectly usable over ssh, and the settings page says which is happening.

SVG rather than PNG: it is a few kilobytes of text, it scales to whatever the
page gives it with no resampling, and it needs no image library on a device
that does not have one.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

#: segno writes `<svg width="456" height="456">` and **no viewBox**. That
#: matters more than it sounds: an SVG without a viewBox has no intrinsic
#: coordinate system to map onto its viewport, so giving it a smaller width in
#: CSS *crops* it rather than scaling it. The first version of this page did
#: exactly that — a 456-unit code in a 340 px box — and what reached the panel
#: was the top-left three quarters of a QR code, missing a finder pattern and
#: unreadable by any phone. It looked completely fine to a person; a QR code is
#: not something you can eyeball for correctness.
#:
#: So the width and height attributes are replaced with a viewBox, and the page
#: sizes it. `tests/test_photos.py` asserts the viewBox is there.
_SVG_OPEN = re.compile(r'<svg\s+width="(\d+)"\s+height="(\d+)"')

try:  # pragma: no cover — presence depends on the deployment
    import segno
except ImportError:  # pragma: no cover
    segno = None


def available() -> bool:
    return segno is not None


def svg(data: str, scale: int = 8) -> str | None:
    """`data` as a scalable SVG QR code, or None when segno is not installed.

    Error correction M rather than the default: a phone camera pointed at a
    glossy 1280x800 panel across a room is reading through reflections, and
    the extra redundancy costs a slightly denser square.

    `border=4` is the quiet zone the QR specification requires. segno's default
    of 4 is right and the first version of this overrode it to 2 to save
    space — which is exactly the sort of saving that turns a code that scans
    into one that sometimes does.
    """
    if segno is None or not data:
        return None
    try:
        code = segno.make(data, error="m")
        # `dark`/`light` rather than a stylesheet: the page renders this inside
        # a light card whatever the surrounding theme is, because a QR code
        # inverted by a dark theme is one many phone cameras will not read.
        markup = code.svg_inline(scale=scale, dark="#000000", light="#ffffff",
                                 border=4)
    except Exception:
        log.exception("could not render the picker QR code")
        return None

    # Turn the fixed pixel size into a coordinate system the page can scale.
    # Without this the code is cropped rather than resized — see the note on
    # `_SVG_OPEN` above, which is a bug this actually shipped with.
    match = _SVG_OPEN.match(markup)
    if match is None:
        # A future segno that emits a different opening tag. Better to serve
        # it at its natural size than to serve a silently cropped one.
        log.warning("the QR SVG did not start as expected; leaving it "
                    "unscaled rather than risking a cropped code")
        return markup
    width, height = match.group(1), match.group(2)
    return _SVG_OPEN.sub(
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="100%" '
        f'preserveAspectRatio="xMidYMid meet"', markup, count=1)
