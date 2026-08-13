"""One Google account, authorised once, remembered afterwards.

OAuth 2.0 for an installed application, with PKCE, spoken directly over HTTPS
rather than through `google-auth`. Two reasons for that: the whole of what this
project needs is an authorisation URL, one token exchange and a refresh, which
is about a hundred lines of `requests`; and the Google client libraries pull a
dependency tree onto a device where `pywebpush` pinning `cryptography` has
already broken the system `pyOpenSSL` once (see `aipi5-pi-deploy`).

**What is written down, and where.**

    ~/.config/aipi5/google-photos-client.json   the OAuth client, by hand
    ~/.config/aipi5/google-photos-token.json    the refresh token, 0600

Both are outside the repository — `_secret_path` in `aipi5/core/config.py`
resolves relative settings under `~/.config/aipi5` precisely so a relative name
in the YAML cannot put either inside a git checkout — and `.gitignore` names
them as a backstop.

**No token is ever logged, at any level.** Not truncated, not at DEBUG. Section
6 asks for that and it is easy to lose by accident: `log.debug("token response
%s", payload)` is a natural line to write while debugging a refresh and it puts
a long-lived credential into the journal, which on this device is world-
readable to anybody who can `systemctl --user status`. `_redact` exists so the
useful half of a failure can still be logged.

**The access token stays in memory.** It lives about an hour, it is re-derived
from the refresh token whenever it is needed, and writing it to disk would only
add a second secret to protect for no benefit.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

#: The only scope this project asks for, and the narrowest one that can do the
#: job. It grants read access to the items a person picks in the Google Photos
#: picker and to nothing else — not the library, not albums, not uploads. A
#: device sitting in somebody's living room should hold the smallest grant that
#: works, and after the 2025 API changes this is also the only one that would
#: work: `photoslibrary.readonly` no longer returns anything but 403.
SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"

#: Refresh a little before the hour is up, so a download that starts at 59
#: minutes does not fail on a token that expires mid-request.
REFRESH_MARGIN_S = 300.0

#: A bound on a hang rather than a target, the same as everywhere else here.
TIMEOUT_S = 20.0


class GoogleAuthError(RuntimeError):
    """Something a person has to fix: no client file, a revoked grant."""


def _redact(payload) -> str:
    """A token response with the secrets taken out, safe for the journal.

    The half that is worth logging is `error`, `error_description` and which
    fields came back. The half that must never be is every value that is a
    credential — so this lists key *names* and copies only the two error
    fields through.
    """
    if not isinstance(payload, dict):
        return f"<{type(payload).__name__}>"
    keep = {k: payload[k] for k in ("error", "error_description", "error_uri")
            if k in payload}
    keep["fields"] = sorted(payload)
    return json.dumps(keep, ensure_ascii=False)


@dataclass(frozen=True)
class Client:
    """The OAuth client, as downloaded from the Google Cloud console.

    A "Desktop app" client. Its secret is not a secret in the sense a server's
    is — Google's own documentation says installed-app clients cannot keep one
    — but it is still not ours to publish, so it lives in a file with the
    token rather than in this repository.
    """

    client_id: str
    client_secret: str

    @classmethod
    def read(cls, path: Path) -> "Client":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise GoogleAuthError(
                f"no Google OAuth client at {path}. Create a Desktop app "
                f"client in the Google Cloud console, enable the Photos "
                f"Picker API, download the JSON and save it there.") from exc
        except (OSError, ValueError) as exc:
            raise GoogleAuthError(f"cannot read {path}: {exc}") from exc

        # The console's download nests everything under "installed" (or
        # "web"). Accept the bare shape too, because somebody hand-writing
        # this file will write the bare shape and be right to.
        block = raw.get("installed") or raw.get("web") or raw
        client_id = str(block.get("client_id", "")).strip()
        client_secret = str(block.get("client_secret", "")).strip()
        if not client_id or not client_secret:
            raise GoogleAuthError(
                f"{path} has no client_id/client_secret — it does not look "
                f"like an OAuth client downloaded from the Cloud console")
        return cls(client_id, client_secret)


class GoogleAuth:
    """Holds the refresh token and hands out access tokens.

    Thread-safe because two threads want it: the sync thread downloading
    photographs, and an HTTP handler answering the settings page's "am I
    connected?". The lock is held across the refresh request, which is the
    point — two threads noticing an expired token at the same moment should
    produce one round trip, not two.
    """

    def __init__(self, cfg, session: requests.Session | None = None):
        self.cfg = cfg
        self._session = session or requests.Session()
        self._lock = threading.Lock()
        self._access: str = ""
        self._access_until: float = 0.0
        self._error: str = ""
        self._token: dict = {}
        self._load()

    # ── the stored grant ─────────────────────────────────────────────

    def _load(self) -> None:
        try:
            raw = json.loads(self.cfg.token_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._error = "not authorised"
            return
        except (OSError, ValueError) as exc:
            self._error = f"the saved authorisation is unreadable: {exc}"
            log.warning("google photos: %s", self._error)
            return
        if not isinstance(raw, dict) or not raw.get("refresh_token"):
            self._error = "the saved authorisation has no refresh token"
            log.warning("google photos: %s", self._error)
            return
        self._token = raw
        self._error = ""
        log.info("google photos: authorised as %s",
                 raw.get("account") or "an unnamed account")

    def reload(self) -> None:
        """Re-read the token file, after the linking script has written one.

        The script runs in a separate process — it has to, it needs a terminal
        — so the running assistant only learns it succeeded by looking again.
        Called from the settings page's Reconnect, and cheap.
        """
        with self._lock:
            self._access, self._access_until = "", 0.0
            self._token = {}
            self._load()

    @property
    def authorised(self) -> bool:
        return bool(self._token.get("refresh_token"))

    @property
    def account(self) -> str:
        return str(self._token.get("account") or "")

    @property
    def error(self) -> str:
        return self._error

    def describe(self) -> dict:
        """For `/api/system`. Names no credential, by construction."""
        return {
            "authorised": self.authorised,
            "account": self.account,
            "error": self._error,
            "client_file": str(self.cfg.client_file),
            "client_present": self.cfg.client_file.exists(),
            "scope": SCOPE,
        }

    # ── access tokens ────────────────────────────────────────────────

    def token(self) -> str:
        """A usable access token, refreshing if the last one is nearly out.

        Raises `GoogleAuthError` when there is nothing to refresh from or the
        grant has been revoked, because every caller has to stop in both cases
        and the difference between them is only what the message says.
        """
        with self._lock:
            if self._access and time.time() < self._access_until:
                return self._access
            refresh = str(self._token.get("refresh_token") or "")
            if not refresh:
                raise GoogleAuthError(self._error or "not authorised")
            client = Client.read(self.cfg.client_file)
            payload = self._post(TOKEN_ENDPOINT, {
                "client_id": client.client_id,
                "client_secret": client.client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            })
            access = str(payload.get("access_token") or "")
            if not access:
                self._error = self._refusal(payload)
                log.error("google photos: %s", self._error)
                raise GoogleAuthError(self._error)
            expires = float(payload.get("expires_in") or 3600)
            self._access = access
            self._access_until = time.time() + max(60.0, expires - REFRESH_MARGIN_S)
            self._error = ""
            log.debug("google photos: access token refreshed, good for %.0f min",
                      expires / 60)
            return self._access

    @staticmethod
    def _refusal(payload: dict) -> str:
        """Why Google would not renew, in terms somebody can act on.

        **`invalid_grant` almost always means one specific, non-obvious
        thing here**, and it is worth naming rather than passing the raw code
        through: an OAuth consent screen left at publishing status *Testing*
        issues refresh tokens that expire after **seven days**, whatever else
        is configured. So the slideshow works for a week, stops, and the only
        clue is a four-word error code.

        The other causes are the ordinary ones — the grant revoked from the
        Google account page, or six months unused — and they need the same
        action, so the message covers all three in the order they are likely.
        """
        code = str(payload.get("error") or "no access token")
        if code == "invalid_grant":
            return ("Google will not renew the authorisation (invalid_grant). "
                    "The usual cause is the OAuth consent screen still being "
                    "at publishing status \"Testing\", which expires refresh "
                    "tokens after 7 days — set it to \"In production\". "
                    "Otherwise the access was revoked from the Google account. "
                    "Either way: run ./scripts/link-google-photos.sh again.")
        detail = str(payload.get("error_description") or "")
        return (f"the Google authorisation was refused ({code}"
                f"{': ' + detail if detail else ''})")

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token()}"}

    def _post(self, url: str, data: dict) -> dict:
        try:
            response = self._session.post(url, data=data, timeout=TIMEOUT_S)
        except requests.RequestException as exc:
            raise GoogleAuthError(f"cannot reach Google: {exc}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            log.warning("google photos: token endpoint said %d %s",
                        response.status_code, _redact(payload))
        return payload if isinstance(payload, dict) else {}

    # ── the one-time consent, driven by scripts/link-google-photos.sh ──

    @staticmethod
    def challenge() -> tuple[str, str]:
        """A PKCE verifier and its S256 challenge.

        PKCE on a flow that also sends a client secret looks redundant and is
        not: the secret in an installed app is extractable by anybody holding
        the device, so the verifier is the part that actually binds the
        redirect back to the process that started it.
        """
        verifier = base64.urlsafe_b64encode(os.urandom(48)).decode().rstrip("=")
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")

    @staticmethod
    def authorisation_url(client: Client, redirect_uri: str,
                          challenge: str, state: str) -> str:
        """Where to send the person's browser.

        `access_type=offline` with `prompt=consent` is what guarantees a
        refresh token comes back. Google returns one only on the first consent
        otherwise, so an account that has authorised this client before — while
        testing, say — would complete the flow perfectly and leave the device
        with an hour of access and no way to renew it.
        """
        return AUTH_ENDPOINT + "?" + urlencode({
            "client_id": client.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        })

    def exchange(self, client: Client, code: str, verifier: str,
                 redirect_uri: str, account: str = "") -> None:
        """Turn the one-time code into a refresh token, and save it.

        Writes 0600 and creates the parent directory with 0700. The mode is set
        with `os.open` before anything is written rather than chmod-ed after,
        so the token is never on disk world-readable even briefly.
        """
        payload = self._post(TOKEN_ENDPOINT, {
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        })
        refresh = str(payload.get("refresh_token") or "")
        if not refresh:
            raise GoogleAuthError(
                f"Google did not return a refresh token: {_redact(payload)}")
        self.save(refresh, account, payload.get("scope", SCOPE))

    def save(self, refresh_token: str, account: str = "",
             scope: str = SCOPE) -> None:
        path = self.cfg.token_file
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        body = json.dumps({
            "refresh_token": refresh_token,
            "account": account,
            "scope": scope,
            "obtained": time.time(),
        }, ensure_ascii=False)
        # Written to a neighbour and renamed, so a crash halfway through
        # leaves the previous authorisation intact rather than a truncated
        # file that reads as "never authorised".
        temporary = path.with_suffix(".tmp")
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(body)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        os.replace(temporary, path)
        log.info("google photos: authorisation saved to %s", path)
        self.reload()

    def forget(self) -> None:
        """Disconnect: tell Google, then delete the file.

        In that order, and the revoke is best-effort. A device that cannot
        reach the network must still be able to forget an account — the file is
        the thing that matters locally — but leaving a live grant behind when
        the network *is* there would mean "Disconnect" that does not
        disconnect.
        """
        refresh = str(self._token.get("refresh_token") or "")
        if refresh:
            try:
                self._session.post(REVOKE_ENDPOINT, data={"token": refresh},
                                   timeout=TIMEOUT_S)
            except requests.RequestException as exc:
                log.warning("google photos: could not revoke the token "
                            "with Google (%s); deleting it locally anyway", exc)
        self.cfg.token_file.unlink(missing_ok=True)
        with self._lock:
            self._token = {}
            self._access, self._access_until = "", 0.0
            self._error = "not authorised"
        log.info("google photos: the account has been disconnected")

    def close(self) -> None:
        self._session.close()


def new_state() -> str:
    """A CSRF value for the redirect, and the loopback listener's shared key."""
    return secrets.token_urlsafe(24)
