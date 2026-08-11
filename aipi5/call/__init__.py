"""The remote video call: signalling, trusted devices, and the phone page.

Phase 2 of the video-call procedure — a two-way WebRTC call between one phone
and this Pi, on the same network. What is *not* here is phase 3's TURN relay,
so a call across the Internet is not yet expected to connect.

The shape of it, and why:

    phone (Safari/Chrome)                     Pi (the kiosk Chromium)
      |                                              |
      |  https://aipi5.local:8443   (TLS, token)     |  http://127.0.0.1:8092
      |         aipi5/call/server.py                 |     aipi5/ui/server.py
      |                    \\                        /
      |                     `--- SignalingHub ---'
      |                        (aipi5/call/signaling.py)
      |                                              |
      `============ WebRTC media, peer to peer ======'

**Both browsers do the WebRTC; no Python touches the media.** This is the
decision everything else follows from. Chromium already has an encoder, a
congestion controller that drops bitrate instead of freezing, and — the part
that cannot be bolted on afterwards — an acoustic echo canceller that works
because the same process owns both the capture and the playback stream. A
Python peer built on aiortc would have none of those three, and the third is
what the requirement's audio-quality section actually asks for: the Brio
capsule is inches from the speaker the caller's voice comes out of.

So Python's job here is signalling, authentication, and arbitrating the
hardware — telling the voice loop to let go of the Brio so Chromium can have
it, and taking it back afterwards.

**Two doors into one hub, for one reason: secure contexts.** `getUserMedia`
refuses to run on a page that is not a secure context, and `http://` on a LAN
address is not one — but `http://127.0.0.1` *is*, by definition. So the Pi's
own page keeps talking to the existing loopback server with no TLS anywhere,
and only the phone needs the certificate. That is also the security boundary
the project already had: `aipi5/ui/server.py` stays loopback-only and
unauthenticated, and the one thing listening on the network is this module,
which authenticates every request before it does anything.
"""

from aipi5.call.signaling import CallState, SignalingHub
from aipi5.call.tokens import TrustedDevices

__all__ = ["CallState", "SignalingHub", "TrustedDevices"]
