"""AIPI5 — a voice assistant for the Raspberry Pi 5, built on AIA.

This package is deliberately not a fork. AIA's wake word, microphone capture,
endpointing, speech recognition, Piper synthesis, intent router and Kodama-Lite
command set are imported from an AIA checkout and used as they are; what lives
here is only what AIA does not have — a conversational model reached over the
OpenAI API, weather, local news, bedtime stories, camera vision, local person
detection, and a 1280x800 touchscreen UI that gives way to a screensaver when
the room is empty.

See `aipi5/core/aia_bridge.py` for how AIA is found, and README.md for why it
is reused rather than copied.
"""

__all__ = ["__version__"]

# Deployment stamps a version in the way AIA does — from the tag it was
# deployed from. Until there is one, this is what the settings page shows.
__version__ = "0.1.0"
