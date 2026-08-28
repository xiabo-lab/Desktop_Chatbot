"""Sending mail, and the three failures that each cost somebody an afternoon.

There is nothing here that talks to Google. What is worth testing is the part
that will be read at the wrong end of a week: Google returns generic messages
for three failures that have specific, fixable causes on this device, and a
reminder that silently stops arriving is the worst shape a bug can take.

The rest is refusals — an address that is not one, a header with a newline in
it — and the fact that none of it raises. A reminder that could not be emailed
still went out as a notification, and the person should be told which happened.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from aipi5.agent.mail import Mailer, Sent, _explain


class FakeAnswer:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.content = b"x"

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class TestExplainingGoogle(unittest.TestCase):
    """Each of these reads as something else until you have seen it once."""

    def test_the_api_being_switched_off_names_the_switch(self):
        answer = FakeAnswer(403, {"error": {
            "message": "Gmail API has not been used in project 12345 before "
                       "or it is disabled.", "status": "PERMISSION_DENIED"}})
        detail = _explain(answer)
        self.assertIn("not switched on", detail)
        self.assertIn("step 3", detail)

    def test_a_missing_scope_says_to_re_link(self):
        """Adding the scope in the console does not change a token already
        issued -- the link has to be made again, and nothing says so."""
        answer = FakeAnswer(403, {"error": {
            "message": "Request had insufficient authentication scopes.",
            "status": "PERMISSION_DENIED"}})
        detail = _explain(answer)
        self.assertIn("gmail.send", detail)
        self.assertIn("link-gmail.sh", detail)

    def test_invalid_grant_names_the_seven_day_trap(self):
        """The one that only appears a week after everything worked."""
        answer = FakeAnswer(400, {"error": "invalid_grant",
                                  "error_description": "Token has been expired "
                                                       "or revoked."})
        detail = _explain(answer)
        self.assertIn("Testing", detail)
        self.assertIn("seven days", detail)

    def test_anything_else_still_says_what_happened(self):
        answer = FakeAnswer(500, {"error": {"message": "backend error"}})
        self.assertIn("500", _explain(answer))

    def test_a_reply_that_is_not_json_does_not_raise(self):
        self.assertIn("429", _explain(FakeAnswer(429, None)))


class TestBeforeItIsSetUp(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_it_says_what_is_missing_rather_than_failing(self):
        mailer = Mailer(self.tmp / "token.json", self.tmp / "client.json")
        self.assertFalse(mailer.available())
        self.assertFalse(mailer.describe()["ready"])
        self.assertIn("no Google client file", mailer.describe()["detail"])

    def test_a_client_but_no_token_points_at_the_script(self):
        (self.tmp / "client.json").write_text("{}", encoding="utf-8")
        mailer = Mailer(self.tmp / "token.json", self.tmp / "client.json")
        self.assertIn("link-gmail.sh", mailer.describe()["detail"])

    def test_sending_before_setup_is_a_sentence_not_an_exception(self):
        mailer = Mailer(self.tmp / "token.json", self.tmp / "client.json")
        answer = mailer.send("a@b.com", "hello", "body")
        self.assertFalse(answer)
        self.assertIn("not set up", answer.detail)

    def test_a_corrupt_token_is_reported_rather_than_raising(self):
        (self.tmp / "client.json").write_text("{}", encoding="utf-8")
        (self.tmp / "token.json").write_text("{not json", encoding="utf-8")
        mailer = Mailer(self.tmp / "token.json", self.tmp / "client.json")
        self.assertIn("unreadable", mailer.describe()["detail"])

    def test_describe_names_no_credential(self):
        (self.tmp / "client.json").write_text("{}", encoding="utf-8")
        (self.tmp / "token.json").write_text(json.dumps(
            {"refresh_token": "1//SECRET-VALUE", "account": "a@b.com",
             "scope": "https://www.googleapis.com/auth/gmail.send"}),
            encoding="utf-8")
        mailer = Mailer(self.tmp / "token.json", self.tmp / "client.json")
        described = json.dumps(mailer.describe())
        self.assertIn("a@b.com", described)
        self.assertNotIn("SECRET-VALUE", described)


class TestRefusingBadInput(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "client.json").write_text("{}", encoding="utf-8")
        (self.tmp / "token.json").write_text("{}", encoding="utf-8")
        self.mailer = Mailer(self.tmp / "token.json", self.tmp / "client.json")

    def test_an_address_that_is_not_one_is_refused(self):
        for bad in ("", "   ", "not-an-address", "a" * 400 + "@b.com", None):
            with self.subTest(to=str(bad)[:30]):
                answer = self.mailer.send(bad, "s", "b")
                self.assertFalse(answer)
                self.assertIn("not an address", answer.detail)

    def test_a_newline_in_the_address_is_refused(self):
        """Header injection: a newline turns one recipient into a Bcc list."""
        for bad in ("a@b.com\nBcc: everyone@example.com",
                    "a@b.com\r\nSubject: something else"):
            with self.subTest(to=bad[:30]):
                self.assertFalse(self.mailer.send(bad, "s", "b"))

    def test_a_sent_result_is_falsey_until_it_is_true(self):
        self.assertFalse(Sent())
        self.assertTrue(Sent(ok=True))


if __name__ == "__main__":
    unittest.main()
