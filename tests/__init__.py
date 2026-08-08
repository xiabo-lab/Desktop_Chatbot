"""Tests that run on a development machine.

No microphone, no camera, no accelerator, no network and no API key. That is
the constraint, and it is what decided which parts of this project are pure
functions: the presence debounce, the screensaver timing, the feed parsing, the
weather cache, the conversation trimming and the tool-call gate are all
testable here, and they are the parts where a mistake is silent on the device.

    python -m unittest discover -s tests -t .

Anything needing real hardware is verified on the Pi instead, with
`scripts/check_hardware.sh` and by reading the startup checks in the journal.
"""
