"""The Google Photos Picker API, which is how an app reaches somebody's photos.

Since the Library API's read scopes were removed in March 2025 this is the only
supported route to a person's existing photographs, and it inverts the model:
the app does not browse and select, the person does. What the app gets is a
*session* — a URL to send them to, and afterwards a list of what they chose.

    POST   /v1/sessions              → { id, pickerUri, pollingConfig, … }
    GET    /v1/sessions/{id}         → … mediaItemsSet: true when they finish
    GET    /v1/mediaItems?sessionId= → the picked items
    DELETE /v1/sessions/{id}

Two expiries matter and they are not the same one:

* **The session** lives until `expireTime`, and when it goes the picked items
  go with it. Everything must be downloaded before then.
* **A `baseUrl`** is good for sixty minutes. Listing again yields fresh ones,
  so a long download is a re-list rather than a failure — as long as the
  session is still alive.

The download URL takes a size suffix: `=w1600-h1000` bounds the image without
cropping it, which is section 29's requirement and section 10's both at once —
the aspect ratio is preserved by the server, so nothing here has to think about
portrait against landscape.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import requests

from aipi5.photos.auth import GoogleAuthError

log = logging.getLogger(__name__)

BASE = "https://photospicker.googleapis.com/v1"

TIMEOUT_S = 20.0
#: A photograph at 1600x1000 is a few hundred kilobytes; the ceiling is for a
#: slow link rather than a large file, and it is generous because section 8's
#: whole premise is that this device is sometimes on a 5G hotspot.
DOWNLOAD_TIMEOUT_S = 120.0
#: One page of picked items. The API's maximum is 100.
PAGE_SIZE = 100
#: Never walk more than this many pages in one sync. A person picking twenty
#: thousand photographs is not a case worth serving, and an unbounded loop
#: against a paginated API is how a background thread becomes permanent.
MAX_PAGES = 60


class PickerError(RuntimeError):
    """The API said no. `expired` distinguishes the one that is not a fault."""

    def __init__(self, message: str, *, status: int = 0, expired: bool = False):
        super().__init__(message)
        self.status = status
        self.expired = expired


@dataclass(frozen=True)
class Session:
    """A picking session, as the API describes it."""

    id: str
    picker_uri: str
    expire_time: str = ""
    media_items_set: bool = False
    poll_interval_s: float = 5.0
    timeout_s: float = 1800.0

    @classmethod
    def parse(cls, payload: dict) -> "Session":
        polling = payload.get("pollingConfig") or {}
        return cls(
            id=str(payload.get("id", "")),
            picker_uri=str(payload.get("pickerUri", "")),
            expire_time=str(payload.get("expireTime", "")),
            media_items_set=bool(payload.get("mediaItemsSet")),
            poll_interval_s=_duration(polling.get("pollInterval"), 5.0),
            timeout_s=_duration(polling.get("timeoutIn"), 1800.0),
        )


@dataclass(frozen=True)
class PickedItem:
    """One photograph the person chose.

    Only what the slideshow actually uses is kept. The API returns rather more
    — filename, contributor, camera make — and none of it reaches the screen:
    section 13 asks for the overlay to be minimal and optional, so this carries
    the creation time and the size and stops there.
    """

    id: str
    base_url: str
    mime_type: str
    created: str = ""
    width: int = 0
    height: int = 0

    @property
    def is_photo(self) -> bool:
        """Videos are skipped. A photo frame that plays a silent clip for
        fifteen seconds and moves on is worse than one that does not."""
        return self.mime_type.startswith("image/")

    @classmethod
    def parse(cls, payload: dict) -> "PickedItem | None":
        media = payload.get("mediaFile") or {}
        metadata = media.get("mediaFileMetadata") or {}
        base_url = str(media.get("baseUrl", ""))
        if not base_url:
            return None
        return cls(
            id=str(payload.get("id", "")),
            base_url=base_url,
            mime_type=str(media.get("mimeType", "")),
            created=str(payload.get("createTime", "")),
            width=int(metadata.get("width") or 0),
            height=int(metadata.get("height") or 0),
        )


@dataclass
class Picked:
    """A page-walked list of everything one session holds."""

    items: list[PickedItem] = field(default_factory=list)
    truncated: bool = False


class PickerClient:
    """Sessions and picked items, over one HTTPS session.

    Every method raises `PickerError` and none of them raise `requests`
    exceptions at the caller — the sync thread has to treat "the Wi-Fi is down"
    and "Google refused" the same way, which is to keep the cache and try later.
    """

    def __init__(self, auth, session: requests.Session | None = None):
        self.auth = auth
        self._http = session or requests.Session()

    def _request(self, method: str, url: str, **kwargs) -> dict:
        try:
            headers = self.auth.headers()
        except GoogleAuthError as exc:
            raise PickerError(str(exc)) from exc
        try:
            response = self._http.request(method, url, headers=headers,
                                          timeout=TIMEOUT_S, **kwargs)
        except requests.RequestException as exc:
            raise PickerError(f"cannot reach Google Photos: {exc}") from exc

        if response.status_code == 404 or response.status_code == 410:
            # A session that has expired, or one deleted from the other end.
            # Not an error anybody can act on and not worth an exception
            # trace — the caller falls back to the cache, which is the whole
            # design. Flagged so it can say the right thing.
            raise PickerError("the picking session has expired",
                              status=response.status_code, expired=True)
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                detail = str((body.get("error") or {}).get("message", ""))[:200]
            except ValueError:
                detail = response.text[:200]
            raise PickerError(f"Google Photos returned {response.status_code}"
                              f"{': ' + detail if detail else ''}",
                              status=response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise PickerError("Google Photos did not return JSON") from exc
        return payload if isinstance(payload, dict) else {}

    # ── sessions ─────────────────────────────────────────────────────

    def create(self) -> Session:
        session = Session.parse(self._request("POST", f"{BASE}/sessions", json={}))
        if not session.picker_uri:
            raise PickerError("Google Photos opened a session with no picker URL")
        # The id, never the URL. `pickerUri` carries a one-time token that
        # authorises picking into this session, so it belongs on the screen as
        # a QR code and nowhere else — certainly not in a journal that anyone
        # on the device can read.
        log.info("google photos: picking session %s opened", session.id)
        return session

    def get(self, session_id: str) -> Session:
        return Session.parse(self._request("GET", f"{BASE}/sessions/{session_id}"))

    def delete(self, session_id: str) -> None:
        """Tidy up a session that was abandoned or has been fully downloaded.

        Best-effort: it expires on its own, and a device that cannot reach
        Google must not be stuck holding a session it believes is live.
        """
        try:
            self._request("DELETE", f"{BASE}/sessions/{session_id}")
        except PickerError as exc:
            log.debug("google photos: could not delete session %s (%s)",
                      session_id, exc)

    # ── what was picked ──────────────────────────────────────────────

    def items(self, session_id: str) -> Picked:
        """Everything picked in one session, following `nextPageToken`."""
        found = Picked()
        token = ""
        for page in range(MAX_PAGES):
            params = {"sessionId": session_id, "pageSize": PAGE_SIZE}
            if token:
                params["pageToken"] = token
            payload = self._request("GET", f"{BASE}/mediaItems", params=params)
            for raw in payload.get("mediaItems") or []:
                item = PickedItem.parse(raw)
                if item is not None and item.is_photo:
                    found.items.append(item)
            token = str(payload.get("nextPageToken") or "")
            if not token:
                return found
        found.truncated = True
        log.warning("google photos: stopped after %d pages (%d photos); the "
                    "rest of the selection is ignored", MAX_PAGES,
                    len(found.items))
        return found

    def download(self, item: PickedItem, size: str) -> bytes:
        """One photograph, at the size the screen needs.

        The `Authorization` header is required on a `baseUrl` fetch — this is
        not a public link — which is also why the page cannot be handed these
        URLs directly and the bytes are cached locally instead.
        """
        url = f"{item.base_url}={size}"
        try:
            headers = self.auth.headers()
        except GoogleAuthError as exc:
            raise PickerError(str(exc)) from exc
        try:
            response = self._http.get(url, headers=headers, stream=True,
                                      timeout=DOWNLOAD_TIMEOUT_S)
        except requests.RequestException as exc:
            raise PickerError(f"cannot download a photo: {exc}") from exc
        if response.status_code == 403:
            # A `baseUrl` older than sixty minutes. The caller re-lists.
            raise PickerError("the download link has expired",
                              status=403, expired=True)
        if response.status_code >= 400:
            raise PickerError(f"downloading a photo returned "
                              f"{response.status_code}",
                              status=response.status_code)
        try:
            return response.content
        except requests.RequestException as exc:
            raise PickerError(f"the download was interrupted: {exc}") from exc

    def close(self) -> None:
        self._http.close()


def _duration(value, fallback: float) -> float:
    """A protobuf duration — `"5s"`, `"1800s"` — as seconds."""
    text = str(value or "").strip()
    if text.endswith("s"):
        text = text[:-1]
    try:
        seconds = float(text)
    except ValueError:
        return fallback
    return seconds if seconds > 0 else fallback
