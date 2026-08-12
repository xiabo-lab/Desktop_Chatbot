"""The video call, in the parts that can be wrong without anybody noticing.

Three of those, and they are why this file exists rather than being covered by
making a call and watching it work:

**Authentication.** A call that connects proves the happy path. It says nothing
about whether a *wrong* token is refused, whether a revoked phone stays
revoked, or whether the store is where it is supposed to be — and every one of
those failures presents as a working feature.

**The state machine.** Ringing, answering, hanging up and timing out are a
sequence of events across three threads, which is the shape that cannot be
checked by pressing a button. The specific failure being guarded is a call that
ends without the hub returning to idle: the Brio is then lent forever, and what
somebody sees is a person detector that stopped working the next day.

**Where secrets live.** A test, not a review comment, because "is this path
inside the repository" is exactly the question a reviewer answers by assuming.

No network, no camera, no phone.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import unittest
from pathlib import Path

from aipi5.call import turn
from aipi5.call.signaling import PHONE, PI, CallState, SignalingHub
from aipi5.call.server import MAX_BODY
from aipi5.call.tokens import MAX_FAILURES, TrustedDevices, new_token
from aipi5.core import config as config_mod

ROOT = Path(__file__).resolve().parent.parent


class TestTrustedDevices(unittest.TestCase):
    """The gate in front of a camera that answers by itself."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "devices.json"
        self.devices = TrustedDevices(self.store)

    def test_a_paired_phone_is_recognised(self):
        token = self.devices.pair("an iPhone")
        device = self.devices.authenticate(token, "10.0.0.5")
        self.assertIsNotNone(device)
        self.assertEqual(device["name"], "an iPhone")

    def test_nothing_is_trusted_before_pairing(self):
        # The state of a fresh install. Every token fails, including one that
        # is well-formed — there is nothing to match it against.
        self.assertIsNone(self.devices.authenticate(new_token(), "10.0.0.5"))

    def test_a_wrong_token_is_refused(self):
        self.devices.pair("an iPhone")
        self.assertIsNone(self.devices.authenticate(new_token(), "10.0.0.5"))
        self.assertIsNone(self.devices.authenticate("", "10.0.0.5"))

    def test_the_token_itself_is_never_stored(self):
        # The whole reason only a digest is kept: a copy of this file must not
        # be a working phone. If this ever fails, a backup of the Pi's home
        # directory has become a credential.
        token = self.devices.pair("an iPhone")
        raw = self.store.read_text("utf-8")
        self.assertNotIn(token, raw)
        self.assertIn("digest", raw)

    def test_revoking_a_phone_stops_it_calling(self):
        token = self.devices.pair("an old phone")
        self.assertIsNotNone(self.devices.authenticate(token))
        self.assertEqual(self.devices.revoke("an old phone"), 1)
        self.assertIsNone(self.devices.authenticate(token))

    def test_revocation_survives_a_restart(self):
        # Revoked in one process, still revoked in the next. The failure this
        # prevents is a revoke that only ever lived in memory.
        token = self.devices.pair("an old phone")
        self.devices.revoke("an old phone")
        self.assertIsNone(TrustedDevices(self.store).authenticate(token))

    def test_pairing_survives_a_restart(self):
        token = self.devices.pair("an iPhone")
        self.assertIsNotNone(TrustedDevices(self.store).authenticate(token))

    def test_a_second_phone_can_be_added(self):
        # The requirement asks for more family phones later, so two tokens must
        # coexist rather than the second replacing the first.
        first = self.devices.pair("phone one")
        second = self.devices.pair("phone two")
        self.assertIsNotNone(self.devices.authenticate(first))
        self.assertIsNotNone(self.devices.authenticate(second))
        self.assertEqual(len(self.devices), 2)

    def test_repeated_failures_lock_an_address_out(self):
        self.devices.pair("an iPhone")
        for _ in range(MAX_FAILURES):
            self.devices.authenticate("wrong", "10.0.0.9")
        self.assertGreater(self.devices.blocked("10.0.0.9"), 0)
        # And only that address. A stranger guessing must not lock the house
        # out of its own device.
        self.assertEqual(self.devices.blocked("10.0.0.10"), 0)

    def test_revoking_from_another_process_takes_effect_at_once(self):
        # The bug this is here for, found by rotating a token on the device:
        # `scripts/pair-phone.sh` is its own process, so the *running*
        # assistant kept honouring a token that had just been revoked. A
        # revocation that needs a restart is not a revocation.
        #
        # `serving` is the long-lived instance inside the assistant; `tool` is
        # the script. They share only the file.
        serving = TrustedDevices(self.store)
        token = serving.pair("an iPhone")
        self.assertIsNotNone(serving.authenticate(token))

        tool = TrustedDevices(self.store)
        tool.revoke("an iPhone")

        self.assertIsNone(serving.authenticate(token),
                          "a revoked phone must stop working without a restart")

    def test_pairing_from_another_process_takes_effect_at_once(self):
        # The other half, and the symptom that exposed it: the newly issued
        # token was refused while the old one still worked.
        serving = TrustedDevices(self.store)
        token = TrustedDevices(self.store).pair("a new iPhone")
        self.assertIsNotNone(serving.authenticate(token),
                             "a phone paired just now must be able to call")

    def test_a_rotation_swaps_which_token_works(self):
        # Exactly the sequence run on the device: revoke, re-pair, and the old
        # token must die in the same instant the new one starts working.
        serving = TrustedDevices(self.store)
        old = serving.pair("an iPhone")
        self.assertIsNotNone(serving.authenticate(old))

        tool = TrustedDevices(self.store)
        tool.revoke("an iPhone")
        new = tool.pair("an iPhone")

        self.assertIsNone(serving.authenticate(old), "the old token must be dead")
        self.assertIsNotNone(serving.authenticate(new), "the new one must work")

    def test_a_lockout_survives_a_reload(self):
        # Re-reading the device file must not hand an attacker a fresh set of
        # attempts — otherwise re-pairing a phone resets everybody's lockout.
        self.devices.pair("an iPhone")
        for _ in range(MAX_FAILURES):
            self.devices.authenticate("wrong", "10.0.0.9")
        self.assertGreater(self.devices.blocked("10.0.0.9"), 0)
        TrustedDevices(self.store).pair("another phone")   # touches the file
        self.devices.authenticate("wrong", "10.0.0.9")     # forces the reload
        self.assertGreater(self.devices.blocked("10.0.0.9"), 0)

    def test_a_corrupt_store_trusts_nobody(self):
        # Fails closed. A store that cannot be parsed must not become "trust
        # anybody" — nor be silently ignored.
        self.store.write_text("{ this is not json", "utf-8")
        self.assertEqual(len(TrustedDevices(self.store)), 0)

    def test_tokens_are_generated_and_not_guessable(self):
        tokens = {new_token() for _ in range(50)}
        self.assertEqual(len(tokens), 50)
        self.assertTrue(all(len(t) >= 32 for t in tokens))


class TestSecretsLiveOutsideTheRepository(unittest.TestCase):
    """The requirement forbids credentials in the repository. Asserted, not read."""

    def test_the_default_paths_are_outside_the_tree(self):
        settings = config_mod.load()
        for name, path in (("devices", settings.call.devices),
                           ("certificate", settings.call.certificate),
                           ("private key", settings.call.private_key)):
            with self.subTest(what=name):
                self.assertFalse(
                    str(path.resolve()).startswith(str(ROOT.resolve())),
                    f"the call {name} defaults inside the repository: {path}")

    def test_a_relative_setting_does_not_land_in_the_repository(self):
        # The trap this guards: `_path` resolves a relative setting against the
        # project root, which is right for a model file and catastrophic for a
        # TLS private key. `_secret_path` resolves under ~/.config instead.
        resolved = config_mod._secret_path("call-key.pem", "call-key.pem")
        self.assertFalse(str(resolved).startswith(str(ROOT)))

    def test_no_token_or_key_is_committed(self):
        # A guard on the tree itself, not on the code. Cheap, and it is the
        # check somebody would otherwise do by eye after a hurried commit.
        #
        # The virtualenv is excluded because it is not the tree — `certifi`
        # ships `cacert.pem`, which is a CA bundle and not a secret, and which
        # made this fail on the Pi where the venv lives inside the project
        # directory. Anything beginning with a dot goes for the same reason.
        for pattern in ("*.pem", "*key.pem", "call-devices.json"):
            found = [p for p in ROOT.rglob(pattern)
                     if not any(part.startswith(".") for part in p.parts)]
            self.assertEqual(found, [], f"{pattern} should never be in the tree")


class TestSignalingHub(unittest.TestCase):
    """One call, two seats, and getting back to idle whatever happens."""

    def setUp(self):
        self.hub = SignalingHub()

    def ring(self):
        accepted, session, _ = self.hub.ring("an iPhone")
        self.assertTrue(accepted)
        return session

    def test_a_new_hub_is_idle_and_owns_nothing(self):
        self.assertIs(self.hub.state, CallState.IDLE)
        self.assertFalse(self.hub.live)

    def test_ringing_does_not_yet_own_the_camera(self):
        # The requirement in its strongest form: an authorised caller who has
        # dialled but not been picked up has not turned anything on. `live` is
        # what lends the Brio, and it must be false here.
        self.ring()
        self.assertIs(self.hub.state, CallState.RINGING)
        self.assertFalse(self.hub.live)

    def test_answering_takes_the_floor(self):
        session = self.ring()
        self.assertTrue(self.hub.answer(session))
        self.assertIs(self.hub.state, CallState.CONNECTING)
        self.assertTrue(self.hub.live)

    def test_a_second_phone_is_refused_while_a_call_is_up(self):
        # Version 1 is one phone and one Pi. A second caller must not become a
        # third participant in a conversation two people think is private.
        self.ring()
        accepted, _, why = self.hub.ring("a stranger's phone")
        self.assertFalse(accepted)
        self.assertIn("already", why)

    def test_answering_a_stale_session_does_nothing(self):
        self.ring()
        self.assertFalse(self.hub.answer("not-the-session"))
        self.assertIs(self.hub.state, CallState.RINGING)

    def test_hanging_up_returns_to_idle_and_releases(self):
        session = self.ring()
        self.hub.answer(session)
        self.assertTrue(self.hub.live)
        self.hub.hang_up(session, "the phone hung up")
        self.assertFalse(self.hub.live)
        # ENDED is a moment, not a resting place — the sweep takes it to idle,
        # and until it is idle no new call can arrive.
        self.hub.sweep()

    def test_hanging_up_twice_is_survivable(self):
        # Both peers send `bye`, and so does the page being torn down. All
        # three routinely arrive after the call is already down.
        session = self.ring()
        self.hub.answer(session)
        self.assertTrue(self.hub.hang_up(session))
        self.assertFalse(self.hub.hang_up(session))

    def test_both_seats_are_told_when_a_call_ends(self):
        session = self.ring()
        self.hub.answer(session)
        self.hub.hang_up(session, "the phone hung up")
        for role in (PI, PHONE):
            with self.subTest(role=role):
                messages, _ = self.hub.collect(role, 0, timeout=0.1)
                self.assertIn("bye", [m["type"] for m in messages])

    def test_a_ring_nobody_answers_expires(self):
        # The failure this prevents: a phone that dialled and vanished holding
        # the device busy forever, so nothing can ever call again.
        hub = SignalingHub(ring_timeout_s=0.05)
        hub.ring("an iPhone")
        time.sleep(0.06)
        self.assertTrue(hub.sweep())
        self.assertIs(hub.state, CallState.IDLE)
        accepted, _, _ = hub.ring("an iPhone")
        self.assertTrue(accepted, "the device must be callable again")

    # ── the Pi ringing the phone ─────────────────────────────────────

    def test_calling_out_owns_no_hardware_until_somebody_answers(self):
        # The mirror of the incoming rule, and the more important half: a phone
        # being rung has not agreed to anything, so the Pi must not have opened
        # its camera while it waits.
        ok, session, _ = self.hub.call_out("an iPhone")
        self.assertTrue(ok)
        self.assertIs(self.hub.state, CallState.CALLING)
        self.assertFalse(self.hub.live, "the Brio must not be open yet")
        self.assertTrue(self.hub.picked_up(session))
        self.assertTrue(self.hub.live)

    def test_an_outgoing_call_is_never_auto_answered(self):
        # There is deliberately no path from CALLING to CONNECTING except
        # `picked_up`, which only ever runs because a person tapped. If a
        # future change adds one, this fails.
        _, session, _ = self.hub.call_out("an iPhone")
        self.assertFalse(self.hub.answer(session),
                         "`answer` is the Pi picking up an INCOMING call and "
                         "must not advance an outgoing one")
        self.assertIs(self.hub.state, CallState.CALLING)

    def test_picking_up_a_stale_session_does_nothing(self):
        self.hub.call_out("an iPhone")
        self.assertFalse(self.hub.picked_up("not-the-session"))
        self.assertIs(self.hub.state, CallState.CALLING)

    def test_the_pi_is_told_when_the_phone_picks_up(self):
        # That message is what makes the Pi's page open the Brio, so without it
        # the call connects to a black rectangle.
        _, session, _ = self.hub.call_out("an iPhone")
        self.hub.picked_up(session)
        messages, _ = self.hub.collect(PI, 0, timeout=0.1)
        self.assertIn("answered", [m["type"] for m in messages])

    def test_a_call_out_nobody_answers_expires(self):
        hub = SignalingHub(call_out_timeout_s=0.05)
        hub.call_out("an iPhone")
        time.sleep(0.06)
        self.assertTrue(hub.sweep())
        self.assertIs(hub.state, CallState.IDLE)

    def test_calling_out_is_refused_while_a_call_is_up(self):
        self.ring()
        ok, _, why = self.hub.call_out("an iPhone")
        self.assertFalse(ok)
        self.assertIn("already", why)

    def test_an_incoming_call_is_refused_while_calling_out(self):
        # Both directions share one hub and one Brio, so they must exclude each
        # other rather than racing for it.
        self.hub.call_out("an iPhone")
        accepted, _, why = self.hub.ring("an iPhone")
        self.assertFalse(accepted)
        self.assertIn("already", why)

    def test_a_phone_that_vanishes_mid_call_ends_it(self):
        # The one way a connected call can be stuck forever: the phone stops
        # existing without hanging up — app killed, handset off, battery flat.
        # Nothing else covers it, because a connected call deliberately has no
        # deadline, and the cost is the Brio lent to a browser until somebody
        # restarts the assistant.
        hub = SignalingHub(phone_silent_s=0.05)
        _, session, _ = hub.ring("an iPhone")
        hub.answer(session)
        hub.collect(PHONE, 0, timeout=0.01)     # the phone is here
        hub.connected(session)
        self.assertTrue(hub.live)
        time.sleep(0.06)
        self.assertTrue(hub.sweep(), "a silent phone must end the call")
        self.assertFalse(hub.live, "the camera must be given back")

    def test_a_polling_phone_keeps_its_call(self):
        # The other half, and the one that matters more: a real conversation
        # must not be hung up on because it is quiet.
        hub = SignalingHub(phone_silent_s=5.0)
        _, session, _ = hub.ring("an iPhone")
        hub.answer(session)
        hub.connected(session)
        for _ in range(3):
            hub.collect(PHONE, 0, timeout=0.01)
            self.assertFalse(hub.sweep())
        self.assertTrue(hub.live)

    def test_the_pi_is_told_when_the_phone_vanishes(self):
        # Otherwise the screen keeps a peer connection open to nobody, and the
        # camera stays with the browser even though the hub has moved on.
        hub = SignalingHub(phone_silent_s=0.05)
        _, session, _ = hub.ring("an iPhone")
        hub.answer(session)
        hub.collect(PHONE, 0, timeout=0.01)
        hub.connected(session)
        time.sleep(0.06)
        hub.sweep()
        messages, _ = hub.collect(PI, 0, timeout=0.1)
        self.assertIn("bye", [m["type"] for m in messages])

    def test_silence_before_the_call_connects_is_not_a_vanished_phone(self):
        # `ringing` and `connecting` have their own deadlines. Applying the
        # liveness rule there would hang up on a phone that simply has not
        # started polling yet.
        hub = SignalingHub(phone_silent_s=0.01)
        _, session, _ = hub.ring("an iPhone")
        time.sleep(0.02)
        self.assertFalse(hub._phone_gone_locked())
        hub.answer(session)
        self.assertFalse(hub._phone_gone_locked())

    def test_a_connected_call_never_expires(self):
        # A long call is not a stuck one. A deadline that survived answering
        # would hang up on a conversation at an arbitrary moment.
        hub = SignalingHub(connect_timeout_s=0.05)
        _, session, _ = hub.ring("an iPhone")
        hub.answer(session)
        hub.connected(session)
        time.sleep(0.06)
        self.assertFalse(hub.sweep())
        self.assertIs(hub.state, CallState.CONNECTED)

    def test_reconnecting_still_owns_the_hardware(self):
        # The requirement is explicit that a brief network drop keeps the call
        # page up. Releasing the camera on a blip and reopening it would be a
        # visible restart of the picture on both ends.
        _, session, _ = self.hub.ring("an iPhone")
        self.hub.answer(session)
        self.hub.connected(session)
        self.assertTrue(self.hub.reconnecting(session))
        self.assertTrue(self.hub.live)

    # ── the mailboxes ────────────────────────────────────────────────

    def test_a_message_reaches_the_other_seat_only(self):
        self.hub.post(PI, {"type": "offer", "sdp": "v=0"})
        theirs, _ = self.hub.collect(PI, 0, timeout=0.1)
        mine, _ = self.hub.collect(PHONE, 0, timeout=0.1)
        self.assertEqual([m["type"] for m in theirs], ["offer"])
        self.assertEqual(mine, [])

    def test_the_cursor_does_not_replay_messages(self):
        self.hub.post(PI, {"type": "ice", "candidate": {}})
        first, cursor = self.hub.collect(PI, 0, timeout=0.1)
        self.assertEqual(len(first), 1)
        second, _ = self.hub.collect(PI, cursor, timeout=0.1)
        self.assertEqual(second, [], "a collected message must not come back")

    def test_a_poll_waits_and_then_answers_immediately(self):
        # The property that makes long-polling usable as signalling: a message
        # posted while a poll is parked wakes it, rather than waiting out the
        # timeout. If this regresses, every call gains 25 seconds of handshake.
        started = threading.Event()
        result = {}

        def wait():
            started.set()
            result["messages"], _ = self.hub.collect(PI, 0, timeout=5.0)

        thread = threading.Thread(target=wait)
        thread.start()
        self.assertTrue(started.wait(2.0))
        time.sleep(0.05)
        began = time.monotonic()
        self.hub.post(PI, {"type": "answer", "sdp": "v=0"})
        thread.join(3.0)
        self.assertEqual(len(result.get("messages", [])), 1)
        self.assertLess(time.monotonic() - began, 1.5)

    def test_an_empty_poll_gives_up_without_moving_the_cursor(self):
        messages, cursor = self.hub.collect(PI, 7, timeout=0.05)
        self.assertEqual(messages, [])
        self.assertEqual(cursor, 7)

    def test_a_message_for_a_stale_session_is_dropped(self):
        # A phone that reloaded mid-call posting candidates for the call it
        # used to be on. Delivering those would put dead candidates into a live
        # session.
        _, session, _ = self.hub.ring("an iPhone")
        self.hub.answer(session)
        self.assertFalse(self.hub.post(PI, {"type": "ice"}, "an-old-session"))
        self.assertTrue(self.hub.post(PI, {"type": "ice"}, session))

    def test_a_mailbox_nobody_reads_stays_bounded(self):
        for index in range(600):
            self.hub.post(PI, {"type": "ice", "n": index})
        messages, _ = self.hub.collect(PI, 0, timeout=0.1)
        self.assertLessEqual(len(messages), 256)
        # The newest survive, which is the half worth keeping.
        self.assertEqual(messages[-1]["n"], 599)

    def test_an_unknown_seat_is_not_a_seat(self):
        self.assertFalse(self.hub.post("kitchen-speaker", {"type": "offer"}))
        messages, _ = self.hub.collect("kitchen-speaker", 0, timeout=0.05)
        self.assertEqual(messages, [])

    def test_the_snapshot_carries_no_secret(self):
        # It goes to the screen and into /api/state, which the phone can read.
        # Pinned exactly rather than checked for absences, so a field added in
        # passing has to be looked at rather than sliding through.
        self.ring()
        self.assertEqual(set(self.hub.snapshot()),
                         {"state", "session", "caller", "since", "live",
                          "can_ring"})

    def test_the_hub_does_not_decide_whether_a_phone_can_be_rung(self):
        # `can_ring` is in the snapshot because the screen needs it, but the
        # hub knows nothing about notifications — it moves JSON between two
        # seats. The assistant fills it in. If this ever becomes True here, the
        # hub has grown a dependency it should not have.
        self.assertFalse(self.hub.snapshot()["can_ring"])


class TestTurnCredentials(unittest.TestCase):
    """Coturn's `use-auth-secret` scheme, checked without a relay.

    Worth testing precisely because it cannot be checked by making a call on
    the sofa: a credential Coturn rejects produces a call that works on the
    same network and fails on a different one, which is the single hardest
    failure here to reproduce deliberately.
    """

    SECRET = "0123456789abcdef"

    def test_the_username_is_expiry_colon_name(self):
        user, _ = turn.credentials(self.SECRET, "aipi5", ttl=3600, now=1000)
        self.assertEqual(user, "4600:aipi5")

    def test_the_password_is_base64_hmac_sha1_of_the_username(self):
        # Computed here the way Coturn computes it, from the specification
        # rather than from our own implementation — a test that calls the same
        # function twice proves only that it is deterministic.
        user, password = turn.credentials(self.SECRET, "aipi5", ttl=60, now=0)
        expected = base64.b64encode(
            hmac.new(self.SECRET.encode(), user.encode(), hashlib.sha1).digest()
        ).decode()
        self.assertEqual(password, expected)

    def test_credentials_expire(self):
        user, _ = turn.credentials(self.SECRET, "aipi5", ttl=3600, now=1000)
        self.assertEqual(int(user.split(":")[0]), 4600)
        self.assertGreater(int(user.split(":")[0]), 1000)

    def test_a_different_secret_gives_a_different_password(self):
        _, one = turn.credentials("secret-one", "aipi5", ttl=60, now=0)
        _, two = turn.credentials("secret-two", "aipi5", ttl=60, now=0)
        self.assertNotEqual(one, two)

    def test_the_secret_never_reaches_the_peer(self):
        # The property the whole scheme exists for. What goes to the phone is
        # a username and a derived password; the secret stays here.
        cfg = config_mod.CallConfig(
            turn_servers=({"urls": "turn:relay.example:3478",
                           "secret_env": "AIPI5_TEST_TURN"},))
        os.environ["AIPI5_TEST_TURN"] = "a-very-secret-value"
        self.addCleanup(os.environ.pop, "AIPI5_TEST_TURN", None)
        servers = turn.ice_servers(cfg)
        self.assertEqual(len(servers), 1)
        blob = json.dumps(servers)
        self.assertNotIn("a-very-secret-value", blob)
        self.assertIn("username", servers[0])
        self.assertIn("credential", servers[0])

    def test_a_secret_from_a_file_is_read(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "turn-secret"
        path.write_text("from-a-file\n", "utf-8")   # trailing newline on purpose
        cfg = config_mod.CallConfig(
            turn_servers=({"urls": "turn:relay.example:3478",
                           "secret_file": str(path)},))
        servers = turn.ice_servers(cfg)
        expected = turn.credentials("from-a-file", "aipi5")[0].split(":")[1]
        self.assertEqual(servers[0]["username"].split(":")[1], expected)

    def test_static_credentials_still_work(self):
        # A hosted relay may offer nothing else. Second choice, not refused.
        cfg = config_mod.CallConfig(
            turn_servers=({"urls": "turn:relay.example:3478",
                           "username": "u", "credential": "p"},))
        self.assertEqual(turn.ice_servers(cfg),
                         [{"urls": "turn:relay.example:3478",
                           "username": "u", "credential": "p"}])

    def test_stun_needs_no_credentials(self):
        cfg = config_mod.CallConfig(stun_servers=("stun:stun.example:3478",))
        self.assertEqual(turn.ice_servers(cfg),
                         [{"urls": "stun:stun.example:3478"}])

    def test_no_ice_servers_by_default(self):
        # Phase 2's case, and it must stay free: two devices on one subnet
        # connect on host candidates, and a STUN round trip would only add
        # latency to a call that was going to work.
        self.assertEqual(turn.ice_servers(config_mod.CallConfig()), [])

    def test_an_entry_without_urls_is_skipped_not_crashed(self):
        cfg = config_mod.CallConfig(turn_servers=({"username": "u"},))
        self.assertEqual(turn.ice_servers(cfg), [])

    def test_describe_names_the_relay_but_no_secret(self):
        os.environ["AIPI5_TEST_TURN"] = "a-very-secret-value"
        self.addCleanup(os.environ.pop, "AIPI5_TEST_TURN", None)
        cfg = config_mod.CallConfig(
            turn_servers=({"urls": "turn:relay.example:3478",
                           "secret_env": "AIPI5_TEST_TURN"},))
        described = turn.describe(cfg)
        self.assertNotIn("a-very-secret-value", json.dumps(described))
        self.assertEqual(described["turn"][0]["auth"], "secret")


class TestTheKeyThePushIsSignedWith(unittest.TestCase):
    """The one step between "registered" and "the phone rang".

    Everything either side of this was visible — the subscription is listed,
    the state moves to `calling`, the screen says so — while the push itself
    failed on a key that is perfectly valid and simply handed over the wrong
    way. It cost a real call that rang nothing.
    """

    def setUp(self):
        import tempfile
        from aipi5.call import push
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.keys = push.PushKeys(Path(self.tmp.name))
        if not self.keys.available:
            self.skipTest("py_vapid is not installed on this machine")

    def test_the_key_is_written_in_the_format_it_is_read_back_in(self):
        from py_vapid import Vapid02
        keys = self.keys.load_or_create()
        self.assertIsNotNone(keys)
        # `Vapid.from_string` — which is what pywebpush reaches for when handed
        # a *string* — strips the newlines and base64-decodes the whole PEM,
        # `-----BEGIN PRIVATE KEY-----` included, and reports the result as
        # "Could not deserialize key data". So the signing key is built here,
        # by the reader that understands what `PushKeys` writes.
        signing = Vapid02.from_pem(keys["private"].encode("ascii"))
        self.assertIsNotNone(signing.private_key)

    def test_the_contact_claim_is_a_domain_apple_will_accept(self):
        # Measured against Apple, not guessed: `mailto:aipi5@localhost` comes
        # back `403 BadJwtToken`, which names the token and not the address.
        from aipi5.call import push
        pusher = push.Pusher(self.keys, push.Subscriptions(Path(self.tmp.name)))
        self.assertTrue(pusher.subject.startswith(("mailto:", "https://")))
        address = pusher.subject.split(":", 1)[1]
        self.assertIn(".", address.split("@")[-1])

    def test_the_published_key_is_the_one_that_signs(self):
        # A public key that does not match the private half is a subscription
        # Apple accepts and a push it then rejects, days later.
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PublicFormat)
        from py_vapid import Vapid02
        keys = self.keys.load_or_create()
        signing = Vapid02.from_pem(keys["private"].encode("ascii"))
        raw = signing.public_key.public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint)
        self.assertEqual(
            base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="),
            self.keys.public())


class TestKeptAliveConnectionsStaySynchronised(unittest.TestCase):
    """A POST that does not read its body corrupts the *next* request.

    The bug this exists for, in full: `/call/v1/ring` ignored its body, and
    the phone posts `{}` to it. On a kept-alive HTTP/1.1 connection those two
    bytes stay in the socket, and the following request is parsed starting
    from them — `501 Unsupported method ('{}POST')`. Behind `tailscale serve`,
    which pools connections, that ate a `bye` and left a call up with the
    camera lent to a browser.

    It is invisible to any test that opens a fresh connection per request,
    which is why this one deliberately does not.
    """

    def setUp(self):
        import tempfile
        from aipi5.call.server import CallServer
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = Path(self.tmp.name) / "devices.json"

        devices = TrustedDevices(store)
        self.token = devices.pair("a phone")
        # Port 0: the OS picks a free one, so the suite never collides with a
        # real deployment or with itself running twice.
        # `devices` also decides where push keys and subscriptions are kept —
        # they live beside it. Left at its default, this suite writes a
        # subscription into the developer's real ~/.config/aipi5 and then
        # reads it back in the next test.
        cfg = config_mod.CallConfig(enabled=True, host="127.0.0.1", port=0,
                                    tls=False, devices=store)
        self.server = CallServer(cfg, hub=SignalingHub(), devices=devices)
        self.assertTrue(self.server.start(), self.server.error)
        self.addCleanup(self.server.stop)
        self.port = self.server._server.server_address[1]

    def connection(self):
        import http.client
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)

    def request(self, conn, method, path, body=None):
        headers = {"Authorization": f"Bearer {self.token}",
                   "Content-Type": "application/json"}
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        response.read()                 # drain, or the next one desynchronises
        return response.status

    def test_a_post_with_a_body_does_not_corrupt_the_next_request(self):
        conn = self.connection()
        self.addCleanup(conn.close)
        self.assertEqual(self.request(conn, "POST", "/call/v1/ring", "{}"), 200)
        # The one that used to come back 501. Same connection, deliberately.
        self.assertEqual(self.request(conn, "GET", "/call/v1/state"), 200)

    def test_several_posts_in_a_row_on_one_connection(self):
        conn = self.connection()
        self.addCleanup(conn.close)
        self.assertEqual(self.request(conn, "POST", "/call/v1/ring", "{}"), 200)
        self.assertEqual(
            self.request(conn, "POST", "/call/v1/send",
                         json.dumps({"message": {"type": "ice"}})), 200)
        self.assertEqual(self.request(conn, "POST", "/call/v1/bye", "{}"), 200)
        self.assertEqual(self.request(conn, "GET", "/call/v1/state"), 200)

    def test_a_refused_post_still_drains_its_body(self):
        # A 401 that leaves the body unread desynchronises just as badly, and
        # this is the path an unauthorised caller takes.
        conn = self.connection()
        self.addCleanup(conn.close)
        conn.request("POST", "/call/v1/ring", body='{"a":"b"}',
                     headers={"Authorization": "Bearer wrong-token",
                              "Content-Type": "application/json"})
        response = conn.getresponse()
        response.read()
        self.assertEqual(response.status, 401)
        self.assertEqual(self.request(conn, "GET", "/call/v1/state"), 200)

    def test_an_oversized_body_is_drained_before_it_is_refused(self):
        conn = self.connection()
        self.addCleanup(conn.close)
        huge = json.dumps({"pad": "x" * (MAX_BODY + 4096)})
        self.assertEqual(self.request(conn, "POST", "/call/v1/ring", huge), 413)
        self.assertEqual(self.request(conn, "GET", "/call/v1/state"), 200)

    def test_the_policy_allows_the_service_worker(self):
        # The service worker is what makes the Pi able to ring the phone, and
        # a policy that blocks it fails in the least helpful way available:
        # the phone reports that it cannot receive calls, and no request
        # reaches the Pi to say otherwise. `worker-src` falls back to
        # `script-src`, so `'unsafe-inline'` alone silently forbids loading
        # /sw.js — inline scripts allowed, script *files* not.
        conn = self.connection()
        self.addCleanup(conn.close)
        conn.request("GET", "/", headers={})
        response = conn.getresponse()
        response.read()
        policy = response.getheader("Content-Security-Policy")
        self.assertIn("worker-src 'self'", policy)
        self.assertIn("script-src 'self'", policy)
        # And the worker itself has to be fetchable without a token: iOS reads
        # it while the app is being installed, before anything is paired.
        self.assertEqual(self.request(conn, "GET", "/sw.js"), 200)

    def test_the_pi_says_whether_it_can_ring_this_phone(self):
        # The phone must not answer this from its own side. iOS keeps a push
        # subscription across launches and it outlives things the Pi's copy
        # does not, so a page that reads the local one believes it is set up
        # while the Pi lists no phone at all — and hides the button that is the
        # only way to fix it. The Pi is the authority; it says so on every poll.
        conn = self.connection()
        self.addCleanup(conn.close)
        conn.request("GET", "/call/v1/state",
                     headers={"Authorization": f"Bearer {self.token}"})
        response = conn.getresponse()
        self.assertFalse(json.loads(response.read())["can_ring"])

        self.assertTrue(self.server.subscriptions.register("a phone", {
            "endpoint": "https://web.push.apple.com/abc",
            "keys": {"p256dh": "a-public-key", "auth": "a-secret"},
        }))
        conn.request("GET", "/call/v1/state",
                     headers={"Authorization": f"Bearer {self.token}"})
        response = conn.getresponse()
        self.assertTrue(json.loads(response.read())["can_ring"])

    def test_the_settings_page_agrees_with_itself_about_ringing(self):
        # It used to publish `push.phones: ["a phone"]` and `call.can_ring:
        # false` in the same payload, because the hub's snapshot hardcodes the
        # field for the assistant to fill in later.
        described = self.server.describe()
        self.assertEqual(described["push"]["phones"], [])
        self.assertFalse(described["call"]["can_ring"])

        self.server.subscriptions.register("a phone", {
            "endpoint": "https://web.push.apple.com/abc",
            "keys": {"p256dh": "a-public-key", "auth": "a-secret"},
        })
        described = self.server.describe()
        self.assertEqual(described["push"]["phones"], ["a phone"])
        # Stated as the agreement rather than as `True`, because a machine
        # without pywebpush installed — which is every development machine here
        # — genuinely cannot ring anything, and should say so in both places.
        self.assertEqual(
            described["call"]["can_ring"],
            bool(described["push"]["available"] and described["push"]["phones"]))

    def test_registering_tells_the_screen_straight_away(self):
        # `can_ring` is published on state changes, not computed per request,
        # so a subscribe that does not announce itself leaves the Pi's own
        # dialler hidden until something unrelated moves the state.
        changes = []
        self.server.on_change = lambda: changes.append(1)
        conn = self.connection()
        self.addCleanup(conn.close)
        self.assertEqual(self.request(
            conn, "POST", "/call/v1/subscribe", json.dumps({"subscription": {
                "endpoint": "https://web.push.apple.com/abc",
                "keys": {"p256dh": "a-public-key", "auth": "a-secret"},
            }})), 200)
        self.assertEqual(len(changes), 1)

    def test_a_failure_to_subscribe_reaches_the_journal(self):
        # Nobody can open a console on the phone, so "no confirmation appeared"
        # is the whole of the evidence unless the reason is posted here.
        conn = self.connection()
        self.addCleanup(conn.close)
        with self.assertLogs("aipi5.call.server", level="WARNING") as caught:
            status = self.request(
                conn, "POST", "/call/v1/subscribe",
                json.dumps({"error": "permission is denied · installed=true"}))
        self.assertEqual(status, 200)
        self.assertIn("permission is denied", "\n".join(caught.output))
        # Reporting is not registering: nothing may be stored by it.
        self.assertEqual(self.server.subscriptions.names(), [])

    def test_head_answers_and_leaves_the_connection_usable(self):
        # Two things at once: HEAD used to be `501 Unsupported method`, and a
        # HEAD that wrote a body would desynchronise the connection the same
        # way an unread POST body does.
        conn = self.connection()
        self.addCleanup(conn.close)
        conn.request("HEAD", "/", headers={})
        response = conn.getresponse()
        body = response.read()
        self.assertEqual(response.status, 200)
        self.assertEqual(body, b"", "HEAD must send no body")
        self.assertGreater(int(response.getheader("Content-Length")), 0,
                           "but it must still report the real length")
        self.assertEqual(self.request(conn, "GET", "/call/v1/state"), 200)

    def test_head_is_not_routed_to_the_long_poll(self):
        # `/call/v1/poll` blocks for 25 s by design. A HEAD parked there would
        # be a way to exhaust threads without ever authenticating.
        conn = self.connection()
        self.addCleanup(conn.close)
        began = time.monotonic()
        conn.request("HEAD", "/call/v1/poll?since=0", headers={})
        response = conn.getresponse()
        response.read()
        self.assertEqual(response.status, 404)
        self.assertLess(time.monotonic() - began, 5.0, "it must not block")

    def test_a_body_that_is_not_json_is_refused_without_desynchronising(self):
        conn = self.connection()
        self.addCleanup(conn.close)
        self.assertEqual(
            self.request(conn, "POST", "/call/v1/ring", "not json at all"), 400)
        self.assertEqual(self.request(conn, "GET", "/call/v1/state"), 200)


class TestWhoTheRateLimiterCounts(unittest.TestCase):
    """`_peer`, which decides whose lockout is whose.

    Two ways to get this wrong and both are live once a proxy is in front:
    counting the proxy's own address makes the lockout global, so one bad
    device locks out the household; trusting a forwarded address from anywhere
    lets a caller claim a new one per request and never be limited at all.
    """

    def peer(self, client, headers=None):
        from aipi5.call.server import _Handler
        handler = _Handler.__new__(_Handler)
        handler.client_address = (client, 12345)
        handler.headers = headers or {}
        return _Handler._peer(handler)

    def test_a_direct_connection_is_its_own_address(self):
        self.assertEqual(self.peer("100.126.205.85"), "100.126.205.85")

    def test_a_forwarded_address_is_trusted_from_loopback(self):
        # The `tailscale serve` case: the proxy is on this machine.
        self.assertEqual(
            self.peer("127.0.0.1", {"X-Forwarded-For": "100.126.205.85"}),
            "100.126.205.85")

    def test_a_forwarded_address_is_ignored_from_anywhere_else(self):
        # Otherwise the lockout is trivially defeated by a header.
        self.assertEqual(
            self.peer("203.0.113.9", {"X-Forwarded-For": "1.2.3.4"}),
            "203.0.113.9")

    def test_the_first_hop_wins_in_a_chain(self):
        self.assertEqual(
            self.peer("127.0.0.1", {"X-Forwarded-For": "100.64.0.1, 10.0.0.9"}),
            "100.64.0.1")

    def test_loopback_with_no_header_is_still_loopback(self):
        self.assertEqual(self.peer("127.0.0.1"), "127.0.0.1")

    def test_a_huge_forwarded_header_is_capped(self):
        # It becomes a dictionary key, and it is attacker-controlled.
        self.assertLessEqual(
            len(self.peer("127.0.0.1", {"X-Forwarded-For": "a" * 5000})), 64)


class TestPlaintextIsRefusedOffLoopback(unittest.TestCase):
    """`tls: false` exists for a local proxy and for nothing else.

    The bearer token is the entire authorisation to switch on a camera in
    somebody's home. Over plaintext on a shared network it is readable by
    anything on the path, so a misconfiguration here has to be a refusal rather
    than a warning nobody reads.
    """

    def server(self, **overrides):
        from aipi5.call.server import CallServer
        settings = {"enabled": True, "tls": False, "host": "0.0.0.0"}
        settings.update(overrides)
        cfg = config_mod.CallConfig(**settings)
        devices = TrustedDevices(self.store)
        devices.pair("a phone")           # or it refuses for a different reason
        return CallServer(cfg, hub=SignalingHub(), devices=devices)

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "devices.json"

    def test_plaintext_on_a_public_interface_refuses_to_start(self):
        server = self.server(host="0.0.0.0")
        self.assertFalse(server.start())
        self.assertIn("refusing", server.error)

    def test_plaintext_on_a_lan_address_refuses_to_start(self):
        server = self.server(host="10.0.0.102")
        self.assertFalse(server.start())
        self.assertIn("refusing", server.error)

    def test_the_error_says_what_to_do(self):
        server = self.server(host="0.0.0.0")
        server.start()
        self.assertIn("127.0.0.1", server.error)

    def test_tls_defaults_on(self):
        # A deployment that says nothing about TLS must get it.
        self.assertTrue(config_mod.CallConfig().tls)


class TestCallConfig(unittest.TestCase):

    def test_calling_is_off_by_default(self):
        # The one default in this project whose failure mode is a camera
        # reachable from the network rather than a feature that does not work.
        self.assertFalse(config_mod.CallConfig().enabled)

    def test_the_capture_default_is_the_one_the_camera_can_do(self):
        # 720p30 needs MJPEG on the Brio 101; YUYV at this size is 5 fps.
        # Measured on the device — REPORT.md section 27a.
        cfg = config_mod.CallConfig()
        self.assertEqual((cfg.width, cfg.height, cfg.fps), (1280, 720, 30))

    def test_the_shipped_configuration_does_not_enable_calling(self):
        # Only meaningful in a checkout. A *deployment* enables calling on
        # purpose — that is the whole point of the setting — and its
        # `config/aipi5.yaml` is a local file with local edits, so asserting on
        # it there turns a correct configuration into a failing test. What this
        # guards is the committed default, and the presence of `.git` is what
        # distinguishes the two.
        if not (ROOT / ".git").exists():
            self.skipTest("a deployment, not a checkout — calling may be on")
        settings = config_mod.load()
        self.assertFalse(settings.call.enabled,
                         "config/aipi5.yaml must not ship with calling on")


if __name__ == "__main__":
    unittest.main()
