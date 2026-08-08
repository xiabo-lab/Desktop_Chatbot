"""The 1280x800 touchscreen, and the screensaver that replaces it.

Designed for this panel and no other. AIA's overlay is a Wayland layer-shell
strip sized for a 1920x440 display and is not used here — section 22 is
explicit that the old geometry must not be inherited, and a strip is the wrong
shape for a screen that is mostly height.

A local web page rather than a native toolkit, for the same three reasons AIA
put its scrollback in a browser: it needs touch input and a layer-shell surface
takes no input, it keeps PyGObject out of the virtualenv, and it is the shape
Kodama-Lite already runs in on this device so the two behave the same way under
the compositor.
"""
