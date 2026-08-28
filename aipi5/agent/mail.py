"""Sending one email, from the assistant's side of the fence.

**This runs in `aipi5.service`, as `fuwenxu` — not in the agent.** The agent
decides that a reminder should go out and what it should say; it never holds the
credential that sends it, because `~/.config/aipi5` is inside a home the agent
user cannot traverse. So a compromised agent can schedule mail and still cannot
reach the account, which is a better property than any amount of care taken with
a token it did have.

`gmail.send` and nothing else. That scope is write-only: there is no read call
it authorises, so this cannot list, search or open the mailbox it sends from.
Worth stating because "let the Pi use my Gmail" sounds larger than what it is.

Hand-rolled against `requests`, deliberately, the same way
`aipi5/photos/auth.py` is. The whole need is one refresh and one POST; Google's
client libraries pull a dependency tree that has already broken this device's
system `pyOpenSSL` once.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

import requests

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/gmail.send"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = ("https://gmail.googleapis.com/gmail/v1/users/me/messages/send")
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

DEFAULT_TOKEN = Path.home() / ".config" / "aipi5" / "gmail-token.json"
DEFAULT_CLIENT = Path.home() / ".config" / "aipi5" / "google-photos-client.json"

TIMEOUT_S = 20.0
#: Refresh a little early rather than discovering expiry mid-send.
REFRESH_MARGIN_S = 300.0
MAX_SUBJECT = 200
MAX_BODY = 20_000


@dataclass
class Sent:
    ok: bool = False
    detail: str = ""
    message_id: str = ""

    def __bool__(self) -> bool:
        return self.ok


class Mailer:
    """One account, one scope, one thing it can do.

    Never raises. A reminder that could not be emailed still went out as a
    notification, and the person should be told which happened rather than
    handed a traceback.
    """

    def __init__(self, token_file: Path = DEFAULT_TOKEN,
                 client_file: Path = DEFAULT_CLIENT):
        self.token_file = Path(token_file)
        self.client_file = Path(client_file)
        self._access = ""
        self._expires = 0.0
        self._address = ""

    # ── whether it is set up at all ─────────────────────────────────

    def available(self) -> bool:
        return self.token_file.exists() and self.client_file.exists()

    def describe(self) -> dict:
        """For the settings page. Names no credential."""
        if not self.client_file.exists():
            return {"ready": False,
                    "detail": "no Google client file; see docs/gmail-setup.md"}
        if not self.token_file.exists():
            return {"ready": False,
                    "detail": "not linked yet; run scripts/link-gmail.sh"}
        try:
            saved = json.loads(self.token_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"ready": False, "detail": f"the token is unreadable ({exc})"}
        return {"ready": True, "account": saved.get("account", ""),
                "scope": saved.get("scope", ""),
                "linked": saved.get("obtained", 0)}

    # ── the one thing it does ───────────────────────────────────────

    def send(self, to: str, subject: str, body: str) -> Sent:
        if not self.available():
            return Sent(detail="email is not set up on this device; "
                               "see docs/gmail-setup.md")
        to = (to or "").strip()
        if "@" not in to or len(to) > 320 or "\n" in to or "\r" in to:
            return Sent(detail=f"{to!r} is not an address I can send to")
        subject = (subject or "").replace("\n", " ").replace("\r", " ")[:MAX_SUBJECT]
        body = (body or "")[:MAX_BODY]

        access = self._token()
        if not access:
            return Sent(detail="could not get a token for the mail account")

        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject or "AIPI5"
        if self._address:
            message["From"] = self._address
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

        try:
            answer = requests.post(
                SEND_URL, timeout=TIMEOUT_S,
                headers={"Authorization": f"Bearer {access}"},
                json={"raw": raw})
        except requests.RequestException as exc:
            return Sent(detail=f"the mail could not be sent ({exc})")

        if answer.status_code == 200:
            return Sent(ok=True, detail=f"sent to {to}",
                        message_id=(answer.json() or {}).get("id", ""))
        return Sent(detail=_explain(answer))

    # ── the token ───────────────────────────────────────────────────

    def _token(self) -> str:
        if self._access and time.time() < self._expires - REFRESH_MARGIN_S:
            return self._access
        try:
            saved = json.loads(self.token_file.read_text(encoding="utf-8"))
            client = json.loads(self.client_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("the Gmail credentials are unreadable: %s", exc)
            return ""
        client = client.get("installed") or client.get("web") or client
        self._address = saved.get("account", "")

        try:
            answer = requests.post(TOKEN_URL, timeout=TIMEOUT_S, data={
                "client_id": client.get("client_id", ""),
                "client_secret": client.get("client_secret", ""),
                "refresh_token": saved.get("refresh_token", ""),
                "grant_type": "refresh_token"})
        except requests.RequestException as exc:
            log.error("the Gmail token could not be refreshed: %s", exc)
            return ""
        if answer.status_code != 200:
            log.error("refusing the Gmail token: %s", _explain(answer))
            return ""

        payload = answer.json() or {}
        self._access = payload.get("access_token", "")
        self._expires = time.time() + float(payload.get("expires_in", 0) or 0)
        return self._access

    def address(self) -> str:
        """Who this sends as. Fetched once, and only when asked."""
        if self._address:
            return self._address
        access = self._token()
        if not access:
            return ""
        try:
            answer = requests.get(
                PROFILE_URL, timeout=TIMEOUT_S,
                headers={"Authorization": f"Bearer {access}"})
            if answer.status_code == 200:
                self._address = (answer.json() or {}).get("emailAddress", "")
        except requests.RequestException:
            pass
        return self._address


def _explain(answer) -> str:
    """A Google failure as a sentence somebody could act on.

    Three of these have a specific cause on this device and a generic message
    from Google, and each has cost somebody an afternoon somewhere.
    """
    try:
        payload = answer.json() or {}
    except ValueError:
        payload = {}
    error = payload.get("error")
    if isinstance(error, dict):
        detail = error.get("message", "")
        status = error.get("status", "")
    else:
        detail = payload.get("error_description", "") or str(error or "")
        status = str(error or "")

    if "has not been used" in detail or "is disabled" in detail:
        return ("the Gmail API is not switched on for this Google project — "
                "step 3 in docs/gmail-setup.md")
    if "insufficient authentication scopes" in detail.lower() \
            or status == "PERMISSION_DENIED":
        return ("this token does not carry gmail.send — add the scope and "
                "re-run scripts/link-gmail.sh (steps 2 and 4)")
    if status == "invalid_grant" or "invalid_grant" in detail:
        return ("Google has revoked the link. The usual cause is an OAuth "
                "consent screen still in Testing, which expires refresh "
                "tokens after seven days — step 1 in docs/gmail-setup.md")
    return f"Gmail refused it ({answer.status_code}: {detail or 'no reason given'})"
