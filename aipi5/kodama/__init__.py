"""Starting Kodama-Lite, and nothing else about it.

Everything else — play, pause, next, search, volume, shuffle, repeat, like,
lyrics, karaoke, close — is AIA's `aia/plugins/kodama.py`, imported and
registered unchanged. This package adds exactly one command AIA does not have,
because AIA assumes the player is already running and section 27 requires the
assistant to be able to open it.
"""
