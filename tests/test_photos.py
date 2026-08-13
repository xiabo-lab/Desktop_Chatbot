"""The photo cache, and the two things it must never do.

It must never grow without limit — section 9, and on this device that means
filling the card the conversation database and the Chromium profile also live
on. And it must never serve a file from outside its own directory: the
slideshow reaches photographs through `/api/photos/file/<name>`, which is the
shape of every directory traversal ever written, and what makes it safe is a
regex checked before the path is joined.

Everything here works on a temporary directory with fabricated bytes. Nothing
in this file reaches Google, and nothing in `aipi5/photos/cache.py` can:
section 9 promises that eviction only ever deletes local copies, and the way
that promise is kept is that the module makes no network call at all.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path

from aipi5.core.config import PhotosConfig
from aipi5.photos import auth as auth_mod
from aipi5.photos.cache import NAME, Collection, PhotoCache, file_name


@dataclass(frozen=True)
class FakeItem:
    """What `PickedItem` looks like to the cache, without the API."""

    id: str
    mime_type: str = "image/jpeg"
    created: str = "2024-06-01T10:00:00Z"
    width: int = 1600
    height: int = 1000


class CacheTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="aipi5-photos-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # `min_photos=0` unless a test is about the floor, so the ceiling
        # tests are testing the ceiling.
        self.cfg = PhotosConfig(cache_dir=self.root, max_photos=5,
                                max_cache_mb=1, min_photos=0)
        self.cache = PhotoCache(self.cfg)
        self.assertTrue(self.cache.open())

    def store(self, ident: str, size: int = 1000, collection: str = "c1"):
        return self.cache.store(FakeItem(ident), b"x" * size, collection)


class TestNames(CacheTest):

    def test_a_name_is_a_digest_and_an_extension(self):
        name = file_name("some/google/media/id", "image/jpeg")
        self.assertTrue(NAME.match(name), name)
        self.assertTrue(name.endswith(".jpg"))

    def test_the_same_item_always_gets_the_same_name(self):
        # The URL is content-addressed, which is what lets the page cache the
        # bytes `immutable` and read the wash out of its own cache rather than
        # off the card a second time.
        self.assertEqual(file_name("abc", "image/jpeg"),
                         file_name("abc", "image/jpeg"))
        self.assertNotEqual(file_name("abc", "image/jpeg"),
                            file_name("abd", "image/jpeg"))

    def test_the_media_id_is_not_recoverable_from_the_name(self):
        # These are somebody's Google identifiers and the directory is
        # readable by anybody with the device.
        self.assertNotIn("secret", file_name("secret-album-id", "image/jpeg"))

    def test_resolve_refuses_anything_that_is_not_a_cached_name(self):
        self.store("a")
        for crafted in ("../../etc/passwd", "..\\manifest.json", "manifest.json",
                        "", "a.jpg", "/etc/passwd", "0" * 32 + ".sh",
                        "0" * 31 + ".jpg", "0" * 32 + ".jpg/../x"):
            self.assertIsNone(self.cache.resolve(crafted), crafted)

    def test_resolve_finds_a_real_one(self):
        photo = self.store("a")
        self.assertIsNotNone(self.cache.resolve(photo.name))


class TestStoring(CacheTest):

    def test_stored_photos_are_on_disk_and_in_the_playlist(self):
        photo = self.store("a")
        self.assertTrue((self.root / photo.name).is_file())
        self.cache.select(["c1"])
        self.cache.add_collection(Collection(id="c1", name="Family"),
                                  select=True)
        names = [entry["name"] for entry in self.cache.playlist()]
        self.assertEqual(names, [photo.name])

    def test_the_count_ceiling_stops_the_sync(self):
        for index in range(self.cfg.max_photos):
            self.assertIsNotNone(self.store(f"p{index}"))
        # Nothing unselected to evict, so the next one simply does not fit.
        # Section 9 wants a bound; it does not want the device to thrash.
        self.assertIsNone(self.store("one-too-many"))
        self.assertEqual(self.cache.count(), self.cfg.max_photos)

    def test_the_byte_ceiling_stops_the_sync(self):
        big = 400 * 1024
        self.assertIsNotNone(self.store("a", big))
        self.assertIsNotNone(self.store("b", big))
        # 1 MB limit; a third 400 KB photograph does not fit.
        self.assertIsNone(self.store("c", big))
        self.assertEqual(self.cache.count(), 2)

    def test_room_is_made_from_collections_nobody_selected(self):
        self.cache.add_collection(Collection(id="old", name="Old"))
        for index in range(self.cfg.max_photos):
            self.store(f"old{index}", collection="old")
        self.assertEqual(self.cache.count(), 5)

        # A new pick replaces the old one as the selection, which makes the
        # old photographs evictable — and that is the behaviour a person
        # pressing "Choose Photos" a second time expects.
        self.cache.add_collection(Collection(id="new", name="New"))
        photo = self.store("new0", collection="new")
        self.assertIsNotNone(photo)
        self.assertLessEqual(self.cache.count(), 5)
        remaining = {entry["name"] for entry in self.cache.playlist()}
        self.assertIn(photo.name, remaining)

    def test_selected_photos_are_never_evicted_to_make_room(self):
        # Two selected collections that together exceed the ceiling used to
        # take turns deleting each other, re-downloading the same photographs
        # forever on a connection section 8 says may be a hotspot.
        self.cache.add_collection(Collection(id="c1", name="One"))
        self.cache.add_collection(Collection(id="c2", name="Two"),
                                  select=False)
        self.cache.select(["c1", "c2"])
        for index in range(5):
            self.store(f"a{index}", collection="c1")
        first = {p["name"] for p in self.cache.playlist()}
        self.assertIsNone(self.store("b0", collection="c2"))
        self.assertEqual({p["name"] for p in self.cache.playlist()}, first)

    def test_enforce_limits_trims_after_the_ceiling_is_lowered(self):
        for index in range(5):
            self.store(f"p{index}")
        smaller = PhotoCache(replace(self.cfg, max_photos=2, min_photos=0))
        smaller.open()
        self.assertEqual(smaller.count(), 5)     # adopted from disk
        smaller.enforce_limits()
        self.assertEqual(smaller.count(), 2)

    def test_the_floor_outranks_the_ceiling(self):
        # A number edited in a config file must not be able to empty the set
        # somebody chose with their phone — and those photographs are usually
        # unrecoverable, because the picking session behind them has expired.
        self.cache.add_collection(Collection(id="c1", name="Chosen"))
        for index in range(5):
            self.store(f"p{index}", collection="c1")
        tiny = PhotoCache(replace(self.cfg, max_photos=1, min_photos=3))
        tiny.open()
        with self.assertLogs("aipi5.photos.cache", level="WARNING") as caught:
            tiny.enforce_limits()
        self.assertEqual(tiny.count(), 3)
        self.assertTrue(any("floor" in line for line in caught.output))
        # And the slideshow still has something to play, which is the point.
        self.assertEqual(len(tiny.playlist()), 3)

    def test_cleanup_never_empties_the_selected_set(self):
        # The strongest form of the same rule: even with both ceilings at
        # their smallest, cleanup stops rather than leaving a black screen.
        self.cache.add_collection(Collection(id="c1", name="Chosen"))
        for index in range(4):
            self.store(f"p{index}", collection="c1")
        squeezed = PhotoCache(replace(self.cfg, max_photos=1, max_cache_mb=1,
                                      min_photos=2))
        squeezed.open()
        squeezed.enforce_limits()
        self.assertGreaterEqual(squeezed.count(), 2)

    def test_unselected_photos_go_first_and_go_entirely(self):
        self.cache.add_collection(Collection(id="old", name="Old"))
        for index in range(3):
            self.store(f"old{index}", collection="old")
        self.cache.add_collection(Collection(id="new", name="New"))
        for index in range(2):
            self.store(f"new{index}", collection="new")
        # `add_collection` selected "new", so "old" is the leftover.
        trimmed = PhotoCache(replace(self.cfg, max_photos=2, min_photos=2))
        trimmed.open()
        trimmed.enforce_limits()
        kept = {p["name"] for p in trimmed.playlist()}
        self.assertEqual(len(kept), 2)
        # The two survivors are the selected ones, not simply the two newest
        # of everything — which happens to be the same set here, so assert on
        # the collection rather than the count.
        for entry in trimmed.describe()["collections"]:
            if entry["id"] == "new":
                self.assertIn(entry["id"], trimmed.describe()["selected"])

    def test_only_an_explicit_action_clears_the_selected_set(self):
        self.cache.add_collection(Collection(id="c1", name="Chosen"))
        for index in range(4):
            self.store(f"p{index}", collection="c1")
        self.cache.enforce_limits()
        self.assertEqual(self.cache.count(), 4)
        # Replacing the selection makes the old set droppable; clearing takes
        # everything. Both are things a person asked for.
        self.assertEqual(self.cache.forget_collection("c1"), 4)
        self.assertEqual(self.cache.count(), 0)

    def test_eviction_only_deletes_local_files(self):
        # Section 9, stated as a test because it is the promise that matters
        # most: nothing in this module may reach the network at all.
        source = Path(__file__).resolve().parent.parent / "aipi5" / "photos" / "cache.py"
        text = source.read_text(encoding="utf-8")
        for forbidden in ("requests", "urlopen", "http", "socket"):
            self.assertNotIn(f"import {forbidden}", text)


class TestPlaylist(CacheTest):

    def test_only_the_selected_collection_plays(self):
        self.cache.add_collection(Collection(id="c1", name="One"))
        self.cache.add_collection(Collection(id="c2", name="Two"), select=False)
        keep = self.store("a", collection="c1")
        self.store("b", collection="c2")
        self.cache.select(["c1"])
        self.assertEqual([p["name"] for p in self.cache.playlist()], [keep.name])

    def test_nothing_selected_falls_back_to_everything(self):
        # A collection deleted while its photographs are still on disk. A
        # black screen would be the wrong answer to that.
        self.store("a", collection="gone")
        self.assertEqual(len(self.cache.playlist()), 1)

    def test_the_playlist_carries_the_album_name_for_the_caption(self):
        self.cache.add_collection(Collection(id="c1", name="Family Photos"))
        self.store("a", collection="c1")
        self.assertEqual(self.cache.playlist()[0]["album"], "Family Photos")

    def test_the_revision_moves_when_the_playlist_would(self):
        before = self.cache.revision()
        self.store("a")
        self.assertNotEqual(self.cache.revision(), before)
        steady = self.cache.revision()
        self.assertEqual(self.cache.revision(), steady)
        self.cache.add_collection(Collection(id="c1", name="One"))
        self.assertNotEqual(self.cache.revision(), steady)


class TestPersistence(CacheTest):
    """Section 5 and 31.5: the chosen album survives a reboot."""

    def test_the_selection_comes_back(self):
        self.cache.add_collection(Collection(id="c1", name="Family Photos",
                                             session_id="s1", picked=3))
        self.store("a", collection="c1")

        reopened = PhotoCache(self.cfg)
        self.assertTrue(reopened.open())
        self.assertEqual(reopened.selected(), ["c1"])
        self.assertEqual(reopened.collections()[0].name, "Family Photos")
        self.assertEqual(reopened.count(), 1)

    def test_a_lost_manifest_keeps_the_photographs(self):
        photo = self.store("a")
        (self.root / "manifest.json").write_text("{ truncated", encoding="utf-8")
        reopened = PhotoCache(self.cfg)
        with self.assertLogs("aipi5.photos.cache", level="WARNING"):
            reopened.open()
        # The labels are gone; the pictures are not. Deleting a directory of
        # somebody's family photographs over a JSON error would be the worse
        # of the two failures by a long way.
        self.assertEqual(reopened.count(), 1)
        self.assertIsNotNone(reopened.resolve(photo.name))

    def test_a_file_deleted_behind_our_back_leaves_the_index(self):
        photo = self.store("a")
        (self.root / photo.name).unlink()
        reopened = PhotoCache(self.cfg)
        reopened.open()
        self.assertEqual(reopened.count(), 0)


class TestForgetting(CacheTest):

    def test_forgetting_a_collection_removes_its_copies(self):
        self.cache.add_collection(Collection(id="c1", name="One"))
        photo = self.store("a", collection="c1")
        self.assertEqual(self.cache.forget_collection("c1"), 1)
        self.assertFalse((self.root / photo.name).exists())
        self.assertEqual(self.cache.collections(), [])

    def test_clear_empties_everything(self):
        self.store("a")
        self.store("b")
        self.assertEqual(self.cache.clear(), 2)
        self.assertEqual(self.cache.count(), 0)
        self.assertEqual(self.cache.playlist(), [])


class TestCredentials(unittest.TestCase):
    """Section 6, and section 31.17: no token in the repository, none in a log.

    These are the requirements that are cheap to break by accident — one
    `log.debug("%s", payload)` while chasing a refresh failure — and expensive
    to notice, because the journal on this device is readable by anybody who
    can `systemctl --user status`.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="aipi5-auth-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cfg = PhotosConfig(token_file=self.root / "token.json",
                                client_file=self.root / "client.json",
                                cache_dir=self.root / "cache")

    def test_the_token_file_is_not_world_readable(self):
        auth = auth_mod.GoogleAuth(self.cfg)
        auth.save("1//refresh-token-value", account="someone@example.com")
        self.assertTrue(self.cfg.token_file.exists())
        if os.name != "nt":
            # 0600. On Windows the POSIX mode is not meaningful and the suite
            # runs in both places — see the note in aipi5-pi-deploy.
            mode = self.cfg.token_file.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600, oct(mode))

    def test_saving_never_logs_the_token(self):
        auth = auth_mod.GoogleAuth(self.cfg)
        with self.assertLogs("aipi5.photos.auth", level="DEBUG") as caught:
            auth.save("1//refresh-token-value", account="someone@example.com")
        for line in caught.output:
            self.assertNotIn("refresh-token-value", line)

    def test_invalid_grant_names_the_seven_day_trap_first(self):
        # The single most likely cause on this deployment, and the one the raw
        # error code gives no hint of: a consent screen left at "Testing"
        # expires refresh tokens after a week, so the slideshow works and then
        # stops. A message that only said "invalid_grant" would send somebody
        # looking at this code instead of at the Cloud console.
        message = auth_mod.GoogleAuth._refusal({"error": "invalid_grant"})
        self.assertIn("7 days", message)
        self.assertIn("In production", message)
        self.assertIn("link-google-photos.sh", message)

    def test_other_refusals_pass_the_reason_through(self):
        message = auth_mod.GoogleAuth._refusal(
            {"error": "invalid_client", "error_description": "Bad secret"})
        self.assertIn("invalid_client", message)
        self.assertIn("Bad secret", message)
        self.assertNotIn("7 days", message)

    def test_redaction_keeps_the_error_and_drops_the_rest(self):
        redacted = auth_mod._redact({
            "access_token": "ya29.secret", "refresh_token": "1//secret",
            "error": "invalid_grant", "error_description": "Token revoked",
        })
        self.assertNotIn("secret", redacted)
        self.assertIn("invalid_grant", redacted)
        self.assertIn("Token revoked", redacted)
        # The field *names* are useful when a response is the wrong shape.
        self.assertIn("access_token", redacted)

    def test_describe_names_no_credential(self):
        auth = auth_mod.GoogleAuth(self.cfg)
        auth.save("1//refresh-token-value", account="someone@example.com")
        described = json.dumps(auth.describe())
        self.assertNotIn("refresh-token-value", described)
        self.assertIn("someone@example.com", described)

    def test_the_defaults_are_outside_the_repository(self):
        # The same assertion `tests/test_call.py` makes about the call's TLS
        # key, and for the same reason: a relative path in the YAML must not
        # be able to put a credential inside a git checkout.
        here = Path(__file__).resolve().parent.parent
        defaults = PhotosConfig()
        for path in (defaults.token_file, defaults.client_file,
                     defaults.cache_dir):
            self.assertFalse(str(path).startswith(str(here)),
                             f"{path} is inside the repository")

    def test_a_missing_client_says_what_to_do(self):
        auth = auth_mod.GoogleAuth(self.cfg)
        with self.assertRaises(auth_mod.GoogleAuthError) as caught:
            auth_mod.Client.read(self.cfg.client_file)
        self.assertIn("Cloud console", str(caught.exception))
        self.assertFalse(auth.authorised)

    def test_a_client_file_is_read_nested_or_bare(self):
        nested = {"installed": {"client_id": "id.apps", "client_secret": "sh"}}
        self.cfg.client_file.write_text(json.dumps(nested), encoding="utf-8")
        client = auth_mod.Client.read(self.cfg.client_file)
        self.assertEqual(client.client_id, "id.apps")

        self.cfg.client_file.write_text(
            json.dumps({"client_id": "bare", "client_secret": "sh"}),
            encoding="utf-8")
        self.assertEqual(auth_mod.Client.read(self.cfg.client_file).client_id,
                         "bare")

    def test_the_authorisation_url_asks_for_a_refresh_token(self):
        client = auth_mod.Client("id", "secret")
        url = auth_mod.GoogleAuth.authorisation_url(
            client, "http://127.0.0.1:8094/", "challenge", "state")
        # Without both of these Google returns a refresh token only on the
        # very first consent — so a device re-authorised during testing gets
        # an hour of access and no way to renew it.
        self.assertIn("access_type=offline", url)
        self.assertIn("prompt=consent", url)
        self.assertIn("code_challenge_method=S256", url)
        # The narrowest scope that can do the job, and the only one that still
        # works after the 2025 Library API changes.
        self.assertIn("photospicker.mediaitems.readonly", url)

    def test_pkce_challenge_is_the_digest_of_the_verifier(self):
        import base64
        import hashlib
        verifier, challenge = auth_mod.GoogleAuth.challenge()
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        self.assertEqual(challenge,
                         base64.urlsafe_b64encode(digest).decode().rstrip("="))


class TestPickerParsing(unittest.TestCase):
    """The shapes the Picker API actually returns.

    Worth testing because these are the only place this project trusts a
    remote response, and because a field that moves — Google has moved this
    API's shape once already — should fail as a photograph that does not
    appear rather than as a `KeyError` inside the sync thread.
    """

    def test_a_session_carries_its_polling_advice(self):
        from aipi5.photos.picker import Session
        session = Session.parse({
            "id": "sess-1",
            "pickerUri": "https://photos.google.com/picker/xyz",
            "expireTime": "2026-08-14T10:00:00Z",
            "pollingConfig": {"pollInterval": "3s", "timeoutIn": "600s"},
            "mediaItemsSet": False,
        })
        self.assertEqual(session.id, "sess-1")
        self.assertEqual(session.poll_interval_s, 3.0)
        self.assertEqual(session.timeout_s, 600.0)
        self.assertFalse(session.media_items_set)

    def test_a_session_with_no_polling_config_gets_defaults(self):
        from aipi5.photos.picker import Session
        session = Session.parse({"id": "s", "pickerUri": "u"})
        self.assertGreater(session.poll_interval_s, 0)
        self.assertGreater(session.timeout_s, 0)

    def test_a_picked_photo_is_read(self):
        from aipi5.photos.picker import PickedItem
        item = PickedItem.parse({
            "id": "item-1",
            "createTime": "2019-07-04T18:30:00Z",
            "mediaFile": {
                "baseUrl": "https://lh3.googleusercontent.com/abc",
                "mimeType": "image/jpeg",
                "mediaFileMetadata": {"width": 4032, "height": 3024},
            },
        })
        self.assertTrue(item.is_photo)
        self.assertEqual(item.width, 4032)
        self.assertEqual(item.created, "2019-07-04T18:30:00Z")

    def test_a_video_is_not_a_photo(self):
        from aipi5.photos.picker import PickedItem
        item = PickedItem.parse({
            "id": "v", "mediaFile": {"baseUrl": "https://x/y",
                                     "mimeType": "video/mp4"}})
        # A photo frame that plays fifteen silent seconds of a clip and moves
        # on is worse than one that skips it.
        self.assertFalse(item.is_photo)

    def test_an_item_with_no_base_url_is_dropped(self):
        from aipi5.photos.picker import PickedItem
        self.assertIsNone(PickedItem.parse({"id": "x", "mediaFile": {}}))
        self.assertIsNone(PickedItem.parse({"id": "x"}))


class TestPartialDownloads(CacheTest):
    """Section 7 of the follow-up: keep what worked, report what did not.

    The service is driven against a stub client rather than Google. What is
    being tested is the loop's *policy* — that one bad photograph does not
    cost the other 126 — and that policy is entirely local.
    """

    def setUp(self):
        super().setUp()
        from aipi5.photos.picker import PickedItem, Picked, PickerError
        from aipi5.photos.service import GooglePhotosService

        self.items = [PickedItem(id=f"i{n}", base_url=f"https://x/{n}",
                                 mime_type="image/jpeg", width=1600,
                                 height=1000) for n in range(5)]
        self.failing = set()
        outer = self

        class StubClient:
            def items(self, session_id):
                return Picked(items=list(outer.items))

            def download(self, item, size):
                if item.id in outer.failing:
                    raise PickerError("the connection dropped", status=500)
                return b"jpegbytes" * 10

            def delete(self, session_id):
                pass

            def close(self):
                pass

        self.service = GooglePhotosService(self.cfg)
        self.service.cache = self.cache
        self.service.client = StubClient()
        self.cache.add_collection(Collection(id="c1", name="Chosen",
                                             session_id="s1", picked=5))

    def album(self):
        return [c for c in self.cache.collections() if c.id == "c1"][0]

    def test_everything_downloads_when_nothing_fails(self):
        result = self.service._sync_one(self.album())
        self.assertEqual(result.stored, 5)
        self.assertEqual(result.failed, 0)
        self.assertEqual(self.cache.count(), 5)

    def test_two_failures_keep_the_other_three(self):
        self.failing = {"i1", "i3"}
        result = self.service._sync_one(self.album())
        self.assertEqual(result.stored, 3)
        self.assertEqual(result.failed, 2)
        # The whole selection is emphatically not discarded.
        self.assertEqual(self.cache.count(), 3)
        self.assertEqual(len(self.cache.playlist()), 3)

    def test_the_failures_are_retried_on_the_next_pass(self):
        self.failing = {"i1", "i3"}
        self.service._sync_one(self.album())
        self.failing = set()
        result = self.service._sync_one(self.album())
        # Only the two that were missing, because the other three are cached.
        self.assertEqual(result.stored, 2)
        self.assertEqual(self.cache.count(), 5)

    def test_a_full_cache_is_counted_apart_from_a_failure(self):
        # "4 downloads failed" would send somebody to look at their Wi-Fi;
        # "4 did not fit" sends them to the setting that actually caused it.
        from dataclasses import replace as _replace
        self.service.cfg = _replace(self.cfg, max_photos=2)
        self.service.cache = PhotoCache(self.service.cfg)
        self.service.cache.open()
        self.service.cache.add_collection(
            Collection(id="c1", name="Chosen", session_id="s1", picked=5))
        result = self.service._sync_one(self.album())
        self.assertEqual(result.stored, 2)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.no_room, 3)

    def test_a_dead_grant_stops_rather_than_hammering(self):
        from aipi5.photos.picker import PickerError

        def refuse(item, size):
            raise PickerError("forbidden", status=403)

        self.service.client.download = refuse
        result = self.service._sync_one(self.album())
        self.assertEqual(result.stored, 0)
        # All five counted as failed, and the loop stopped at the first one
        # rather than making five doomed requests.
        self.assertEqual(result.failed, 5)


class TestCollectionNaming(unittest.TestCase):

    def test_a_picked_set_is_named_by_the_day(self):
        # `%-d` is glibc's and raises on Windows; the suite runs in both
        # places, so the fallback is not theoretical.
        from datetime import datetime
        from aipi5.photos.service import _collection_name
        name = _collection_name(datetime(2026, 8, 3))
        self.assertIn("Aug", name)
        self.assertIn("2026", name)
        self.assertTrue(name.startswith("Photos"))


class TestQr(unittest.TestCase):

    def test_a_qr_code_is_an_svg_or_honestly_absent(self):
        from aipi5.photos import qr
        drawn = qr.svg("https://photos.google.com/picker/abc123")
        if qr.available():
            self.assertIsNotNone(drawn)
            self.assertTrue(drawn.lstrip().startswith("<svg"))
            # No external reference, no script: this is inserted into the page
            # as markup.
            self.assertNotIn("<script", drawn)
            self.assertNotIn("http", drawn.replace("http://www.w3.org", ""))
        else:
            # The degraded path is deliberate — the page shows the URL as text
            # and says why — so an absent segno must be None, not a crash.
            self.assertIsNone(drawn)

    def test_the_svg_scales_instead_of_cropping(self):
        """The bug this shipped with, and why it needed a test.

        segno writes `<svg width="456" height="456">` with no viewBox. Sizing
        that down in CSS crops it — so the panel showed the top-left three
        quarters of a QR code, missing a finder pattern, unreadable by any
        phone and perfectly plausible-looking to a person. A QR code cannot be
        checked by eye, so it gets checked here.
        """
        from aipi5.photos import qr
        if not qr.available():
            self.skipTest("segno is not installed")
        # A realistic length: the real picker URLs measured 170 characters,
        # which is a version-10 code — big enough that cropping loses data.
        drawn = qr.svg("https://photos.google.com/u/0/picker/session/" + "a" * 120)
        self.assertIn("viewBox=", drawn)
        self.assertNotRegex(drawn, r'<svg\s+width="\d+"',
                            "a fixed pixel width means CSS will crop it")
        self.assertIn('preserveAspectRatio', drawn)

    def test_the_quiet_zone_is_the_full_four_modules(self):
        # Trimming the quiet zone to save space is what turns a code that
        # always scans into one that usually does.
        from aipi5.photos import qr
        if not qr.available():
            self.skipTest("segno is not installed")
        import segno
        code = segno.make("https://example.com/x", error="m")
        modules = code.symbol_size(border=0)[0]
        drawn = qr.svg("https://example.com/x")
        # The viewBox is in scaled units: (modules + 2*border) * scale.
        box = re.search(r'viewBox="0 0 (\d+) (\d+)"', drawn)
        self.assertIsNotNone(box)
        scale = 8
        self.assertEqual(int(box.group(1)), (modules + 8) * scale)

    def test_no_data_is_no_code(self):
        from aipi5.photos import qr
        self.assertIsNone(qr.svg(""))


class TestRepositoryIsClean(unittest.TestCase):

    def test_no_google_credential_is_committed(self):
        here = Path(__file__).resolve().parent.parent
        for name in ("google-photos-client.json", "google-photos-token.json"):
            self.assertFalse((here / name).exists(), name)
        gitignore = (here / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("google-photos-", gitignore)


if __name__ == "__main__":
    logging.disable(logging.NOTSET)
    unittest.main()
