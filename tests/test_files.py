"""The transfer folder, and everything that must not be allowed to leave it.

Three kinds of failure are covered, and they are the three that a person
testing the feature by sending themselves a photo would never see.

**Escaping the folder.** `../../etc/passwd` is the version everybody tests;
`%2e%2e`, a name with a backslash in it, and a symlink somebody uploaded
earlier are the versions that work. The check is resolution, not inspection,
and these are what prove it.

**Half a file with a real name.** An upload from a phone on a train stops in
the middle. If a partial file is left holding the final name, the listing is
lying and there is nothing to say so — a 40 MB video that plays for nine
seconds and stops is the same as a working one until somebody watches it.

**A boundary split across two reads.** The upload parser holds a file in
memory nowhere, so it works on chunks, and the delimiter it looks for does not
respect chunk edges. Split at the wrong offset it writes rubbish into the file
or never finds the end of the part at all. It is tested at every offset.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from aipi5.core.config import FilesConfig
from aipi5.files.multipart import (LimitedStream, MultipartError, MultipartReader,
                                   boundary_of)
from aipi5.files.store import FileError, FileStore, human_size, sanitize_filename
from aipi5.files.tickets import Tickets


def store_in(directory, **overrides) -> FileStore:
    cfg = FilesConfig(root=Path(directory), **overrides)
    store = FileStore(cfg)
    store.start()
    return store


class TestNamesThatAreNotNames(unittest.TestCase):

    def test_traversal_in_every_spelling(self):
        for hostile in ("../../etc/passwd", "..\\..\\windows\\system32",
                        "../test", "....//passwd", "/etc/passwd",
                        "/home/fuwenxu/.ssh/id_rsa", "..", ".", "/",
                        "sub/dir/file.txt"):
            with self.subTest(hostile=hostile):
                cleaned = sanitize_filename(hostile)
                self.assertNotIn("/", cleaned)
                self.assertNotIn("\\", cleaned)
                self.assertNotIn("..", cleaned)

    def test_control_characters_and_nulls_go(self):
        self.assertEqual(sanitize_filename("photo\x00.jpg"), "photo.jpg")
        self.assertEqual(sanitize_filename("bad\x1bname\x7f.txt"), "badname.txt")
        self.assertEqual(sanitize_filename("line\nbreak.txt"), "linebreak.txt")

    def test_chinese_and_spaces_survive(self):
        # The whole point of sanitising by structure rather than by alphabet.
        for good in ("测试照片.jpg", "歌词.txt", "旅行视频.mp4",
                     "my holiday photo.jpeg", "报告 2026.pdf"):
            with self.subTest(good=good):
                self.assertEqual(sanitize_filename(good), good)

    def test_a_hidden_file_cannot_be_uploaded(self):
        # Not a traversal, but a file that would not appear in the listing it
        # was uploaded to appear in.
        self.assertEqual(sanitize_filename(".bashrc"), "bashrc")

    def test_a_very_long_name_is_cut_to_fit_with_its_extension(self):
        name = sanitize_filename("好" * 300 + ".jpeg")
        self.assertLessEqual(len(name.encode("utf-8")), 255)
        self.assertTrue(name.endswith(".jpeg"))
        # And not cut through the middle of a character.
        name.encode("utf-8").decode("utf-8")


class TestTheFolderIsTheBoundary(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "transfer"
        self.store = store_in(self.root)

    def test_it_creates_the_directory_and_says_it_is_ready(self):
        self.assertTrue(self.store.ready)
        self.assertTrue(self.root.is_dir())
        self.assertEqual(self.store.error, "")

    def test_nothing_resolves_outside_it(self):
        for hostile in ("../../etc/passwd", "../outside.txt", "..",
                        "/etc/passwd", "%2e%2e", ""):
            with self.subTest(hostile=hostile):
                try:
                    resolved = self.store.resolve(hostile)
                except FileError:
                    continue
                self.assertTrue(resolved.is_relative_to(self.root.resolve()))

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlinks here")
    def test_a_symlink_out_of_the_folder_is_not_a_file_here(self):
        secret = Path(self.tmp.name) / "id_rsa"
        secret.write_text("PRIVATE KEY", encoding="utf-8")
        try:
            (self.root / "innocent.txt").symlink_to(secret)
        except (OSError, NotImplementedError):
            self.skipTest("this machine will not make symlinks")
        # Resolution follows it out of the folder, so it is refused — and it is
        # not offered in the listing either.
        with self.assertRaises(FileError):
            self.store.open_for_download("innocent.txt")
        self.assertEqual(self.store.listing(), [])

    def test_the_listing_hides_partial_uploads(self):
        (self.root / "real.txt").write_bytes(b"hello")
        (self.root / ".half.mp4.123.uploading").write_bytes(b"\x00" * 10)
        self.assertEqual([f.name for f in self.store.listing()], ["real.txt"])

    def test_it_reports_type_size_and_date(self):
        (self.root / "photo.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 100)
        found = self.store.listing()[0]
        self.assertEqual(found.name, "photo.jpg")
        self.assertEqual(found.size, 103)
        self.assertEqual(found.type, "image/jpeg")
        self.assertIn("T", found.as_dict()["modified_iso"])

    def test_newest_first_by_default(self):
        for name in ("old.txt", "new.txt"):
            (self.root / name).write_bytes(b"x")
            time.sleep(0.01)
        os.utime(self.root / "old.txt", (time.time() - 600, time.time() - 600))
        self.assertEqual([f.name for f in self.store.listing()],
                         ["new.txt", "old.txt"])

    def test_the_revision_moves_when_the_folder_does(self):
        # What both screens watch so a file sent from the phone appears without
        # anybody pressing Refresh.
        before = self.store.revision()
        self.assertEqual(before, self.store.revision(), "it moved on its own")
        self.store.save("arrived.txt", [b"x"])
        after = self.store.revision()
        self.assertNotEqual(after, before, "an upload did not move the revision")
        self.store.delete("arrived.txt")
        self.assertNotEqual(self.store.revision(), after,
                            "a delete did not move the revision")

    def test_the_revision_notices_a_file_this_process_did_not_write(self):
        # scp, another session, somebody dropping one in over ssh. A counter
        # kept by this class would miss all three.
        before = self.store.revision()
        (self.root / "from-elsewhere.txt").write_bytes(b"hello")
        self.assertNotEqual(self.store.revision(), before)

    def test_it_can_be_sorted_by_name_and_size(self):
        (self.root / "b.txt").write_bytes(b"xxx")
        (self.root / "a.txt").write_bytes(b"x")
        self.assertEqual([f.name for f in self.store.listing(sort="name",
                                                             ascending=True)],
                         ["a.txt", "b.txt"])
        self.assertEqual([f.name for f in self.store.listing(sort="size")][0],
                         "b.txt")


class TestUploading(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "transfer"
        self.store = store_in(self.root)

    def test_a_file_arrives_whole(self):
        saved = self.store.save("notes.txt", [b"one ", b"two ", b"three"])
        self.assertEqual(saved.name, "notes.txt")
        self.assertEqual((self.root / "notes.txt").read_bytes(), b"one two three")
        self.assertEqual(saved.size, 13)

    def test_a_zero_byte_file_is_a_file(self):
        saved = self.store.save("empty.txt", [])
        self.assertEqual(saved.size, 0)
        self.assertTrue((self.root / "empty.txt").is_file())

    def test_nothing_is_overwritten(self):
        self.store.save("photo.jpg", [b"first"])
        second = self.store.save("photo.jpg", [b"second"])
        third = self.store.save("photo.jpg", [b"third"])
        self.assertEqual(second.name, "photo (1).jpg")
        self.assertEqual(third.name, "photo (2).jpg")
        self.assertEqual((self.root / "photo.jpg").read_bytes(), b"first")

    def test_an_interrupted_upload_leaves_nothing_behind(self):
        def chunks():
            yield b"the first half"
            raise ConnectionResetError("the phone went into a tunnel")

        with self.assertRaises(ConnectionResetError):
            self.store.save("video.mp4", chunks())
        # No final file, and no temporary one either.
        self.assertEqual(list(self.root.iterdir()), [])

    def test_a_file_over_the_limit_is_refused_mid_stream(self):
        small = store_in(Path(self.tmp.name) / "small", max_upload_bytes=10)

        def chunks():
            # No `Content-Length` was given, so the limit can only be enforced
            # while it arrives — which is the case an attacker would pick.
            for _ in range(10):
                yield b"x" * 8

        with self.assertRaises(FileError) as caught:
            small.save("big.bin", chunks())
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual(list((Path(self.tmp.name) / "small").iterdir()), [])

    def test_a_declared_size_over_the_limit_is_refused_before_a_byte_arrives(self):
        small = store_in(Path(self.tmp.name) / "small2", max_upload_bytes=10)
        touched = []

        def chunks():
            touched.append(True)
            yield b"x"

        with self.assertRaises(FileError) as caught:
            small.save("big.bin", chunks(), expected=1024)
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual(touched, [], "the body was read before it was refused")

    def test_an_upload_bigger_than_the_disk_is_refused(self):
        # The reserve is what makes this fire long before the disk is actually
        # full: filling the root filesystem takes the assistant down with it.
        huge = store_in(Path(self.tmp.name) / "huge",
                        reserve_bytes=2 ** 62, max_upload_bytes=2 ** 63)
        with self.assertRaises(FileError) as caught:
            huge.save("film.mov", [b"x"], expected=10 ** 9)
        self.assertEqual(caught.exception.status, 507)

    def test_a_hostile_name_cannot_choose_the_directory(self):
        saved = self.store.save("../../escaped.txt", [b"nope"])
        self.assertEqual(saved.name, "escaped.txt")
        self.assertTrue((self.root / "escaped.txt").is_file())
        self.assertFalse((self.root.parent / "escaped.txt").exists())

    def test_chinese_filenames_round_trip(self):
        saved = self.store.save("测试照片.jpg", [b"\xff\xd8"])
        self.assertEqual(saved.name, "测试照片.jpg")
        path, size, kind = self.store.open_for_download("测试照片.jpg")
        self.assertEqual(size, 2)
        self.assertEqual(kind, "image/jpeg")
        self.assertEqual(path.name, "测试照片.jpg")

    def test_uploads_are_not_executable(self):
        # A `.sh` is a normal thing to send. It is not a normal thing to be
        # able to run without being made runnable first.
        self.store.save("install.sh", [b"#!/bin/sh\necho hi\n"])
        mode = (self.root / "install.sh").stat().st_mode
        self.assertFalse(mode & 0o111, "an uploaded file arrived executable")

    def test_only_so_many_at_once(self):
        two = store_in(Path(self.tmp.name) / "busy", max_concurrent=2)
        self.assertTrue(two.begin_upload())
        self.assertTrue(two.begin_upload())
        self.assertFalse(two.begin_upload())
        two.end_upload()
        self.assertTrue(two.begin_upload())

    def test_deleting(self):
        self.store.save("gone.txt", [b"x"])
        self.assertEqual(self.store.delete("gone.txt"), "gone.txt")
        self.assertEqual(self.store.listing(), [])
        with self.assertRaises(FileError):
            self.store.delete("gone.txt")
        with self.assertRaises(FileError):
            self.store.delete("../../etc/passwd")


class TestWhenTheFolderCannotBeUsed(unittest.TestCase):

    def test_a_store_that_did_not_start_refuses_uploads_rather_than_crashing(self):
        cfg = FilesConfig(enabled=False)
        store = FileStore(cfg)
        self.assertFalse(store.start())
        self.assertFalse(store.ready)
        self.assertIn("switched off", store.error)
        with self.assertRaises(FileError) as caught:
            store.save("x.txt", [b"x"])
        self.assertEqual(caught.exception.status, 503)

    def test_it_says_so_in_describe_without_raising(self):
        store = FileStore(FilesConfig(enabled=False))
        store.start()
        described = store.describe()
        self.assertFalse(described["ready"])
        self.assertIsNone(described["storage"])


class TestTheMultipartReader(unittest.TestCase):

    BOUNDARY = b"----AIPI5Boundary7MA4YWxkTrZu0gW"

    def body(self, *files: tuple[str, bytes], field: bool = True) -> bytes:
        pieces = []
        if field:
            pieces.append(b"--" + self.BOUNDARY + b"\r\n"
                          b'Content-Disposition: form-data; name="note"\r\n\r\n'
                          b"from the phone\r\n")
        for name, content in files:
            pieces.append(
                b"--" + self.BOUNDARY + b"\r\n"
                b'Content-Disposition: form-data; name="file"; filename="'
                + name.encode("utf-8") + b'"\r\n'
                b"Content-Type: application/octet-stream\r\n\r\n"
                + content + b"\r\n")
        pieces.append(b"--" + self.BOUNDARY + b"--\r\n")
        return b"".join(pieces)

    def read(self, body: bytes, chunk: int = 64 * 1024):
        stream = LimitedStream(io.BytesIO(body), len(body))
        reader = MultipartReader(stream, self.BOUNDARY, chunk=chunk)
        found = []
        for part in reader.parts():
            content = b"".join(part.chunks())
            found.append((part.name, part.filename, content))
        return found

    def test_the_boundary_comes_out_of_the_content_type(self):
        self.assertEqual(
            boundary_of("multipart/form-data; boundary=" + self.BOUNDARY.decode()),
            self.BOUNDARY)
        self.assertEqual(
            boundary_of('multipart/form-data; boundary="quoted-one"'),
            b"quoted-one")
        self.assertIsNone(boundary_of("application/json"))
        self.assertIsNone(boundary_of(""))

    def test_a_field_and_a_file(self):
        found = self.read(self.body(("notes.txt", b"hello there")))
        self.assertEqual(found[0][:2], ("note", None))
        self.assertEqual(found[1], ("file", "notes.txt", b"hello there"))

    def test_several_files_in_one_body(self):
        found = self.read(self.body(("a.txt", b"aaa"), ("b.txt", b"bbb"),
                                    field=False))
        self.assertEqual([(f[1], f[2]) for f in found],
                         [("a.txt", b"aaa"), ("b.txt", b"bbb")])

    def test_a_unicode_filename(self):
        found = self.read(self.body(("测试照片.jpg", b"\xff\xd8\xff"), field=False))
        self.assertEqual(found[0][1], "测试照片.jpg")

    def test_binary_content_that_contains_crlf_and_dashes(self):
        # Content that looks like a boundary without being one — the case a
        # naive `split` gets wrong and quietly truncates a file at.
        nasty = b"\r\n--not-the-boundary\r\nstill the file\r\n--" + \
                self.BOUNDARY[:8] + b"\r\nand more"
        found = self.read(self.body(("tricky.bin", nasty), field=False))
        self.assertEqual(found[0][2], nasty)

    def test_a_boundary_split_across_reads_at_every_offset(self):
        # The bug this whole design is shaped around. One byte at a time is the
        # cruellest schedule available and it must still reassemble exactly.
        content = b"x" * 40 + b"\r\n--partial" + b"y" * 40
        body = self.body(("split.bin", content), field=False)
        for size in (1, 2, 3, 5, 7, 13, 17, 31, 64, 127, len(body) - 1):
            with self.subTest(chunk=size):
                found = self.read(body, chunk=size)
                self.assertEqual(found[0][2], content)

    def test_an_empty_file_part(self):
        found = self.read(self.body(("empty.txt", b""), field=False))
        self.assertEqual(found[0][2], b"")

    def test_a_truncated_body_is_an_error_not_a_short_file(self):
        # What a dropped connection looks like. The alternative — returning the
        # bytes that did arrive — is a corrupt file with a real name.
        body = self.body(("video.mp4", b"z" * 500), field=False)[:200]
        with self.assertRaises(MultipartError):
            self.read(body)

    def test_a_body_with_no_boundary_at_all(self):
        with self.assertRaises(MultipartError):
            self.read(b"not multipart at all")

    def test_the_stream_stops_at_content_length(self):
        # A body followed by the next request on a kept-alive connection: the
        # reader must not read into it.
        raw = self.body(("a.txt", b"aaa"), field=False)
        stream = LimitedStream(io.BytesIO(raw + b"POST /next HTTP/1.1\r\n"), len(raw))
        reader = MultipartReader(stream, self.BOUNDARY)
        found = [b"".join(p.chunks()) for p in reader.parts()]
        self.assertEqual(found, [b"aaa"])
        self.assertEqual(stream.remaining, 0)

    def test_a_part_nobody_reads_is_still_consumed(self):
        # The caller may reject a part — wrong field name, too big — and the
        # next one must still start in the right place.
        raw = self.body(("a.txt", b"aaa"), ("b.txt", b"bbb"), field=False)
        stream = LimitedStream(io.BytesIO(raw), len(raw))
        reader = MultipartReader(stream, self.BOUNDARY)
        names = []
        for part in reader.parts():
            names.append(part.filename)          # deliberately not reading it
        self.assertEqual(names, ["a.txt", "b.txt"])


class TestDownloadTickets(unittest.TestCase):

    def test_a_ticket_names_one_file_and_works_once(self):
        tickets = Tickets()
        ticket = tickets.issue("photo.jpg", "Fuwen iPhone")
        self.assertEqual(tickets.redeem(ticket), "photo.jpg")
        self.assertIsNone(tickets.redeem(ticket), "a used ticket worked twice")

    def test_an_expired_ticket_is_worth_nothing(self):
        tickets = Tickets(ttl_s=-1)
        self.assertIsNone(tickets.redeem(tickets.issue("photo.jpg")))

    def test_rubbish_is_refused(self):
        tickets = Tickets()
        tickets.issue("photo.jpg")
        for nonsense in ("", "  ", "../../etc/passwd", "x" * 200):
            self.assertIsNone(tickets.redeem(nonsense))

    def test_they_do_not_pile_up(self):
        tickets = Tickets()
        for index in range(200):
            tickets.issue(f"file-{index}.txt")
        self.assertLessEqual(len(tickets), 64)

    def test_two_threads_cannot_both_redeem_one(self):
        tickets = Tickets()
        ticket = tickets.issue("photo.jpg")
        results = []
        barrier = threading.Barrier(8)

        def redeem():
            barrier.wait()
            results.append(tickets.redeem(ticket))

        threads = [threading.Thread(target=redeem) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([r for r in results if r], ["photo.jpg"])


class TestTheRoutesOnThePhoneServer(unittest.TestCase):
    """What is reachable from the tailnet, which is the half that must not leak.

    A real server on a real socket, because the questions here are about
    headers, status codes and who is allowed — none of which a function call
    can answer.
    """

    def setUp(self):
        from aipi5.call.server import CallServer
        from aipi5.call.signaling import SignalingHub
        from aipi5.call.tokens import TrustedDevices
        from aipi5.core import config as config_mod

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        devices = TrustedDevices(root / "devices.json")
        self.token = devices.pair("a phone")
        self.store = store_in(root / "transfer")
        # A file to fetch, and a secret one level up that must stay unreachable.
        (self.store.root / "photo.jpg").write_bytes(b"\xff\xd8" + b"p" * 1000)
        (root / "id_rsa").write_bytes(b"PRIVATE KEY")

        cfg = config_mod.CallConfig(enabled=True, host="127.0.0.1", port=0,
                                    tls=False, devices=root / "devices.json")
        self.server = CallServer(cfg, hub=SignalingHub(), devices=devices,
                                 files=self.store)
        self.assertTrue(self.server.start(), self.server.error)
        self.addCleanup(self.server.stop)
        self.port = self.server._server.server_address[1]

    def connect(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        return conn

    def request(self, method, path, body=None, token=True, headers=None):
        conn = self.connect()
        head = dict(headers or {})
        if token:
            head["Authorization"] = f"Bearer {self.token}"
        if body is not None and "Content-Type" not in head:
            head["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=head)
        response = conn.getresponse()
        payload = response.read()
        return response, payload

    def json(self, method, path, body=None, token=True):
        response, payload = self.request(
            method, path, json.dumps(body) if body is not None else None, token)
        return response.status, (json.loads(payload) if payload else {})

    # ── authentication ───────────────────────────────────────────────

    def test_nothing_works_without_a_token(self):
        # The whole feature, one route at a time. This server is reachable from
        # every device on the tailnet.
        checks = [
            ("GET", "/call/v1/files", None),
            ("POST", "/call/v1/files/ticket", {"name": "photo.jpg"}),
            ("POST", "/call/v1/files/delete", {"name": "photo.jpg"}),
        ]
        for method, path, body in checks:
            with self.subTest(route=path):
                status, _ = self.json(method, path, body, token=False)
                self.assertEqual(status, 401)
        # And the file is still there.
        self.assertTrue((self.store.root / "photo.jpg").is_file())

    def test_an_upload_without_a_token_is_refused_and_the_socket_closed(self):
        body, content_type = multipart_body("sneaky.txt", b"x" * 4096)
        response, _ = self.request("POST", "/call/v1/files/upload", body,
                                   token=False,
                                   headers={"Content-Type": content_type})
        self.assertEqual(response.status, 401)
        self.assertEqual(self.store.listing()[0].name, "photo.jpg")
        self.assertEqual(len(self.store.listing()), 1)

    def test_a_wrong_token_is_no_better_than_none(self):
        conn = self.connect()
        conn.request("GET", "/call/v1/files",
                     headers={"Authorization": "Bearer " + "z" * 40})
        response = conn.getresponse()
        response.read()
        self.assertEqual(response.status, 401)

    # ── listing ──────────────────────────────────────────────────────

    def test_the_listing_carries_metadata_and_no_paths(self):
        status, payload = self.json("GET", "/call/v1/files")
        self.assertEqual(status, 200)
        entry = payload["files"][0]
        self.assertEqual(entry["name"], "photo.jpg")
        self.assertEqual(entry["type"], "image/jpeg")
        self.assertEqual(entry["size"], 1002)
        # A name, never a path: the browser has no business knowing where this
        # folder is on the disk.
        self.assertNotIn("/", entry["name"])
        self.assertNotIn("path", entry)
        self.assertIn("free", payload["storage"])

    # ── downloading ──────────────────────────────────────────────────

    def test_a_ticket_then_a_plain_link(self):
        status, issued = self.json("POST", "/call/v1/files/ticket",
                                   {"name": "photo.jpg"})
        self.assertEqual(status, 200)
        # The link carries no bearer token, which is the point of it.
        self.assertNotIn(self.token, issued["url"])

        response, body = self.request("GET", issued["url"], token=False)
        self.assertEqual(response.status, 200)
        self.assertEqual(len(body), 1002)
        self.assertIn("attachment",
                      response.getheader("Content-Disposition", ""))
        self.assertEqual(response.getheader("Accept-Ranges"), "bytes")

    def test_the_raw_route_needs_the_token_and_returns_the_bytes(self):
        # What the phone reads a file into memory with, so it can hand it to
        # the iOS share sheet. A navigation cannot carry a header; this is not
        # a navigation, which is the entire point of it — WebKit draws its own
        # dead-end download page for anything that *is* one.
        status, _ = self.json("GET", "/call/v1/files/raw/photo.jpg", token=False)
        self.assertEqual(status, 401)

        response, body = self.request("GET", "/call/v1/files/raw/photo.jpg")
        self.assertEqual(response.status, 200)
        self.assertEqual(len(body), 1002)

    def test_the_raw_route_cannot_leave_the_folder_either(self):
        from urllib.parse import quote
        for hostile in ("../id_rsa", "..%2fid_rsa", "%2e%2e%2fid_rsa"):
            with self.subTest(hostile=hostile):
                response, body = self.request(
                    "GET", "/call/v1/files/raw/" + quote(hostile, safe=""))
                self.assertIn(response.status, (400, 403, 404))
                self.assertNotIn(b"PRIVATE KEY", body)

    def test_a_ticket_cannot_be_used_twice(self):
        _, issued = self.json("POST", "/call/v1/files/ticket", {"name": "photo.jpg"})
        self.request("GET", issued["url"], token=False)
        response, _ = self.request("GET", issued["url"], token=False)
        self.assertEqual(response.status, 403)

    def test_an_invented_ticket_downloads_nothing(self):
        response, _ = self.request("GET", "/files/dl/" + "a" * 32, token=False)
        self.assertEqual(response.status, 403)

    def test_a_ticket_cannot_be_asked_for_outside_the_folder(self):
        for hostile in ("../id_rsa", "../../etc/passwd", "..%2fid_rsa",
                        "/etc/passwd"):
            with self.subTest(hostile=hostile):
                status, _ = self.json("POST", "/call/v1/files/ticket",
                                      {"name": hostile})
                self.assertIn(status, (400, 403, 404))

    def test_a_range_request_gets_the_range(self):
        _, issued = self.json("POST", "/call/v1/files/ticket", {"name": "photo.jpg"})
        response, body = self.request("GET", issued["url"], token=False,
                                      headers={"Range": "bytes=10-19"})
        self.assertEqual(response.status, 206)
        self.assertEqual(len(body), 10)
        self.assertEqual(response.getheader("Content-Range"), "bytes 10-19/1002")

    def test_the_phone_is_never_offered_anything_inline(self):
        # The phone has no viewer of ours — iOS previews a download itself — so
        # the tailnet-facing server has no `inline` at all. Nothing reachable
        # from off this machine can ask for it.
        _, issued = self.json("POST", "/call/v1/files/ticket", {"name": "photo.jpg"})
        response, _ = self.request("GET", issued["url"] + "?inline=1", token=False)
        self.assertTrue(
            response.getheader("Content-Disposition", "").startswith("attachment"))

    def test_a_unicode_name_survives_the_content_disposition(self):
        self.store.save("测试照片.jpg", [b"\xff\xd8"])
        _, issued = self.json("POST", "/call/v1/files/ticket",
                              {"name": "测试照片.jpg"})
        response, _ = self.request("GET", issued["url"], token=False)
        disposition = response.getheader("Content-Disposition", "")
        # The percent-encoded form is what carries it; the quoted one is a
        # fallback and must not contain anything that ends the parameter early.
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertEqual(disposition.count('"'), 2)

    # ── uploading ────────────────────────────────────────────────────

    def test_an_upload_arrives_and_is_listed(self):
        body, content_type = multipart_body("holiday.txt", b"sand and rain")
        response, payload = self.request("POST", "/call/v1/files/upload", body,
                                         headers={"Content-Type": content_type})
        self.assertEqual(response.status, 200, payload)
        self.assertEqual(json.loads(payload)["files"][0]["name"], "holiday.txt")
        self.assertEqual((self.store.root / "holiday.txt").read_bytes(),
                         b"sand and rain")

    def test_a_hostile_upload_name_lands_in_the_folder_anyway(self):
        body, content_type = multipart_body("../../../etc/cron.d/evil", b"boom")
        response, _ = self.request("POST", "/call/v1/files/upload", body,
                                   headers={"Content-Type": content_type})
        self.assertEqual(response.status, 200)
        self.assertTrue((self.store.root / "evil").is_file())
        self.assertEqual(sorted(p.name for p in self.store.root.iterdir()),
                         ["evil", "photo.jpg"])

    def test_something_that_is_not_multipart_is_refused(self):
        response, _ = self.request("POST", "/call/v1/files/upload", b'{"a":1}',
                                   headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 415)

    def test_deleting_over_the_wire(self):
        status, payload = self.json("POST", "/call/v1/files/delete",
                                    {"name": "photo.jpg"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["name"], "photo.jpg")
        self.assertFalse((self.store.root / "photo.jpg").exists())

    def test_delete_cannot_reach_out_of_the_folder(self):
        secret = Path(self.tmp.name) / "id_rsa"
        for hostile in ("../id_rsa", "../../etc/passwd"):
            with self.subTest(hostile=hostile):
                status, _ = self.json("POST", "/call/v1/files/delete",
                                      {"name": hostile})
                self.assertIn(status, (400, 403, 404))
        self.assertTrue(secret.is_file(), "a key outside the folder was deleted")

    def test_the_phones_state_carries_the_folder_revision_too(self):
        status, state = self.json("GET", "/call/v1/state")
        self.assertEqual(status, 200)
        first = state["files_rev"]
        self.store.save("sent-from-the-pi.txt", [b"x"])
        _, state = self.json("GET", "/call/v1/state")
        self.assertNotEqual(state["files_rev"], first)

    def test_the_call_routes_still_work_beside_it(self):
        # The constraint the whole feature is under: nothing else may change.
        status, payload = self.json("POST", "/call/v1/ring", {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        status, _ = self.json("POST", "/call/v1/bye", {})
        self.assertEqual(status, 200)


class TestTheRoutesOnTheScreensServer(unittest.TestCase):
    """The loopback half: no token, and still no way out of the folder."""

    def setUp(self):
        from aipi5.core import config as config_mod
        from aipi5.ui.server import WebUI
        from aipi5.ui.state import UiState

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = store_in(root / "transfer")
        (self.store.root / "notes.txt").write_bytes(b"hello from the Pi")
        (root / "id_rsa").write_bytes(b"PRIVATE KEY")

        cfg = config_mod.DisplayConfig(host="127.0.0.1", port=0)
        self.web = WebUI(cfg, state=UiState(), history=None,
                         info=lambda: {}, files=self.store)
        self.assertTrue(self.web.start())
        self.addCleanup(self.web.stop)
        self.port = self.web._server.server_address[1]

    def request(self, method, path, body=None, headers=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response, response.read()

    def test_the_screen_can_list_and_download_without_a_ticket(self):
        response, payload = self.request("GET", "/api/files")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(payload)["files"][0]["name"], "notes.txt")

        response, body = self.request("GET", "/api/files/download/notes.txt")
        self.assertEqual(response.status, 200)
        self.assertEqual(body, b"hello from the Pi")

    def test_a_percent_encoded_traversal_is_decoded_before_it_is_checked(self):
        # `%2e%2e%2f` is `../`. A check that runs before decoding is a check
        # that can be spelled around, so this is the one that matters.
        for hostile in ("%2e%2e%2fid_rsa", "..%2fid_rsa", "../id_rsa",
                        "%2e%2e/%2e%2e/etc/passwd"):
            with self.subTest(hostile=hostile):
                response, body = self.request(
                    "GET", "/api/files/download/" + hostile)
                self.assertIn(response.status, (400, 403, 404))
                self.assertNotIn(b"PRIVATE KEY", body)

    def test_a_unicode_name_can_be_downloaded_from_the_screen(self):
        self.store.save("旅行视频.mp4", [b"video"])
        from urllib.parse import quote
        response, body = self.request(
            "GET", "/api/files/download/" + quote("旅行视频.mp4"))
        self.assertEqual(response.status, 200)
        self.assertEqual(body, b"video")

    def test_a_photo_can_be_shown_on_the_screen(self):
        # The Pi already has the file, so "download" here copied it into
        # another folder on the same disk and showed nothing. Looking at it is
        # what somebody standing at the device wants, and that needs `inline`.
        self.store.save("photo.jpg", [b"\xff\xd8\xff"])
        response, body = self.request("GET",
                                      "/api/files/download/photo.jpg?inline=1")
        self.assertEqual(response.status, 200)
        self.assertTrue(
            response.getheader("Content-Disposition", "").startswith("inline"))
        self.assertEqual(response.getheader("Content-Type"), "image/jpeg")

    def test_only_pictures_video_and_sound_are_ever_shown_inline(self):
        # The reason `attachment` is the default: an uploaded page rendered at
        # this origin is somebody else's script running on the assistant's own
        # screen. Asking for `inline` must not be enough to get it.
        for name, content in (("evil.html", b"<script>alert(1)</script>"),
                              ("evil.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>"),
                              ("notes.txt", b"hello"),
                              ("book.pdf", b"%PDF-1.4"),
                              ("archive.zip", b"PK\x03\x04")):
            with self.subTest(name=name):
                self.store.save(name, [content])
                response, _ = self.request(
                    "GET", "/api/files/download/" + name + "?inline=1")
                self.assertTrue(
                    response.getheader("Content-Disposition", "")
                    .startswith("attachment"),
                    name + " was offered inline")

    def test_video_and_audio_may_be_played_on_the_screen(self):
        for name in ("clip.mp4", "song.mp3"):
            with self.subTest(name=name):
                self.store.save(name, [b"\x00\x00\x00\x18"])
                response, _ = self.request(
                    "GET", "/api/files/download/" + name + "?inline=1")
                self.assertTrue(
                    response.getheader("Content-Disposition", "").startswith("inline"))

    def test_without_the_parameter_it_is_still_a_download(self):
        self.store.save("photo.jpg", [b"\xff\xd8\xff"])
        response, _ = self.request("GET", "/api/files/download/photo.jpg")
        self.assertTrue(
            response.getheader("Content-Disposition", "").startswith("attachment"))

    def test_uploading_from_the_screen(self):
        body, content_type = multipart_body("from-screen.txt", b"typed here")
        response, payload = self.request("POST", "/api/files/upload", body,
                                         {"Content-Type": content_type})
        self.assertEqual(response.status, 200, payload)
        self.assertEqual((self.store.root / "from-screen.txt").read_bytes(),
                         b"typed here")

    def test_several_files_in_one_upload(self):
        body, content_type = multipart_body_many(
            [("one.txt", b"1"), ("two.txt", b"2"), ("测试.txt", b"3")])
        response, payload = self.request("POST", "/api/files/upload", body,
                                         {"Content-Type": content_type})
        self.assertEqual(response.status, 200, payload)
        self.assertEqual(len(json.loads(payload)["files"]), 3)
        self.assertEqual((self.store.root / "测试.txt").read_bytes(), b"3")

    def test_a_big_upload_streams_rather_than_buffering(self):
        # Twelve megabytes through a 64 KB parser. What is being checked is
        # that it arrives byte-for-byte; that it does so without a 12 MB
        # allocation is the design, and the chunked parser is what enforces it.
        payload_bytes = bytes(range(256)) * 48 * 1024      # 12 MiB, not random
        body, content_type = multipart_body("big.bin", payload_bytes)
        response, answer = self.request("POST", "/api/files/upload", body,
                                        {"Content-Type": content_type})
        self.assertEqual(response.status, 200, answer)
        landed = (self.store.root / "big.bin").read_bytes()
        self.assertEqual(len(landed), len(payload_bytes))
        self.assertEqual(landed, payload_bytes)

    def test_deleting_from_the_screen(self):
        response, _ = self.request("POST", "/api/files/delete",
                                   json.dumps({"name": "notes.txt"}),
                                   {"Content-Type": "application/json"})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.store.listing(), [])

    def test_the_state_poll_says_when_the_folder_changed(self):
        # The screen learns this from the poll it already makes twice a second,
        # rather than re-reading the listing on a timer of its own.
        response, payload = self.request("GET", "/api/state")
        first = json.loads(payload)["files_rev"]
        self.assertNotEqual(first, 0)

        self.store.save("new-arrival.txt", [b"x"])
        response, payload = self.request("GET", "/api/state")
        self.assertNotEqual(json.loads(payload)["files_rev"], first)

    def test_the_other_routes_are_untouched(self):
        response, payload = self.request("GET", "/api/state")
        self.assertEqual(response.status, 200)
        self.assertIn("assistant", json.loads(payload))


BOUNDARY = "----AIPI5TestBoundary6DsWq2"


def multipart_body(filename: str, content: bytes) -> tuple[bytes, str]:
    return multipart_body_many([(filename, content)])


def multipart_body_many(files) -> tuple[bytes, str]:
    pieces = []
    for filename, content in files:
        pieces.append(
            ("--" + BOUNDARY + "\r\n"
             'Content-Disposition: form-data; name="file"; filename="'
             + filename + '"\r\n'
             "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
            + content + b"\r\n")
    pieces.append(("--" + BOUNDARY + "--\r\n").encode("utf-8"))
    return b"".join(pieces), "multipart/form-data; boundary=" + BOUNDARY


class TestHumanSize(unittest.TestCase):

    def test_it_reads_like_a_size(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(999), "999 B")
        self.assertEqual(human_size(3 * 1024 ** 2), "3.0 MB")
        self.assertEqual(human_size(2 * 1024 ** 3), "2.0 GB")


if __name__ == "__main__":
    unittest.main()
