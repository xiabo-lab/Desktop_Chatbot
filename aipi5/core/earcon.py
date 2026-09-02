"""A short sound that means "I heard you".

**The problem this solves is not cosmetic.** Until now the only sign that the
wake word had fired was a status line on the screen — which is fine for
somebody standing at the panel and useless for everybody else: from across a
room, from outside the camera's view, or while playing a motion game a metre
and a half back with their hands in the air. Without feedback the only way to
find out whether the device is listening is to say the whole command and see
what happens, and when it did not fire, the natural response is to say the wake
word again *over* the turn that did start.

So: two rising notes, 150 ms in total, the moment the wake word fires.

**Synthesised, not shipped.** Sixty lines of numpy against a WAV file that
would need a licence, a path, and a place in the deploy — and it is genuinely
better this way, because the shape is a constant here rather than a binary
nobody can diff.

**Played without blocking the voice loop.** `sd.play` returns as soon as the
samples are queued, which is what the loop needs: the next thing it does is
open the endpointer and start collecting speech, and 150 ms of sleep there is
150 ms of a command it did not hear.

**Not drained from the microphone afterwards, deliberately.** The obvious
worry is that the chime is captured and confuses recognition, and the obvious
fix — drain the buffer after playing it — is wrong here, because AIA supports
saying the wake word and the command in one breath. Draining would throw away
the beginning of "小艾同学，现在几点" every time. A short quiet tone at the head
of the captured audio is something SenseVoice ignores; a missing first syllable
is not.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

#: Sample rate. 48 kHz because that is what PipeWire runs at on this device, so
#: nothing has to resample a sound whose whole job is to be immediate.
RATE = 48_000

#: The two notes, in Hz, and how long each lasts. Rising, because rising reads
#: as a question — "yes?" — and falling reads as a dismissal. A6 then E7: high
#: enough to cut through a room without being loud, and far enough apart to be
#: heard as two notes rather than one wobble.
NOTES = ((880.0, 0.065), (1318.5, 0.085))

#: Peak amplitude. Deliberately quiet: this plays through the same HDMI sink as
#: the assistant's voice and the music, right next to a microphone that is
#: about to record a command, and it only has to be *noticed*, not announced.
LEVEL = 0.18

#: Fade in and out of each note, in seconds. Without it the discontinuity at
#: each edge is a click, and on a small speaker the click is louder and more
#: noticeable than the note it is attached to.
FADE_S = 0.006

_samples = None
_lock = threading.Lock()
_failed = False


def _build():
    """The waveform, built once and kept. None if numpy is unavailable."""
    global _samples
    if _samples is not None:
        return _samples

    try:
        import numpy as np
    except ImportError:
        return None

    pieces = []
    for freq, seconds in NOTES:
        count = int(RATE * seconds)
        time_axis = np.arange(count, dtype=np.float32) / RATE
        # A sine plus a quiet octave. The octave is what stops it sounding like
        # a test tone — a pure sine reads as an alarm, two partials read as a
        # chime.
        wave = (np.sin(2 * np.pi * freq * time_axis)
                + 0.22 * np.sin(4 * np.pi * freq * time_axis))

        envelope = np.ones(count, dtype=np.float32)
        edge = max(1, int(RATE * FADE_S))
        if count > 2 * edge:
            envelope[:edge] = np.linspace(0.0, 1.0, edge)
            envelope[-edge:] = np.linspace(1.0, 0.0, edge)
        pieces.append((wave * envelope).astype(np.float32))

    joined = np.concatenate(pieces)
    peak = float(np.max(np.abs(joined))) or 1.0
    _samples = (joined / peak * LEVEL).astype(np.float32)
    return _samples


def play() -> bool:
    """Sound the "listening" chime. Never raises; never blocks.

    Returns whether the samples reached the device, which is for the tests and
    the boot check rather than for the caller on the voice path — a chime that
    did not play is not a reason to stop listening.
    """
    global _failed

    samples = _build()
    if samples is None:
        return False

    try:
        import sounddevice as sd
    except ImportError:
        return False

    try:
        # Not blocking. The caller's next move is to start collecting speech,
        # and waiting here would be a chunk of the command lost.
        #
        # This does stop whatever else was playing on the default stream, and
        # that is correct: the only thing it can interrupt is the assistant's
        # own previous reply, and somebody saying the wake word over it wants
        # it to stop.
        sd.play(samples, RATE)
        _failed = False
        return True
    except Exception as exc:  # noqa: BLE001
        # Once, not every wake. On a device whose only sink is HDMI this is
        # usually momentary contention, and a warning per utterance would be
        # the loudest thing in the journal.
        if not _failed:
            _failed = True
            log.warning("could not play the wake chime: %s", exc)
        return False


def warm() -> None:
    """Build the waveform ahead of the first wake word.

    Called from startup. Numpy's first `concatenate` and the import itself cost
    a few milliseconds, and paying them on the first wake would put them
    exactly where the whole point is to be immediate.
    """
    with _lock:
        _build()
