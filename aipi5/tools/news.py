"""Local news for San Jose and Santa Clara County.

RSS, parsed with the standard library, from feeds named in the YAML. No API key
and no third-party news service, for the same reason the weather has none: this
is a device that has to keep working in a room without anybody maintaining a
billing relationship for it.

**Headlines, not articles.** Section 16 asks for three to five concise stories,
and the constraint behind that is speech. A read-aloud article is two minutes
during which the assistant is busy, the music is ducked, and the person who
asked has stopped listening. So this returns titles and the one- or two-sentence
description the feed already carries, and the model turns them into something
worth hearing. Fetching the article bodies is deliberately not done.

**Interleaved, not concatenated.** Feeds are read round-robin so a prolific
publisher cannot fill all five slots while a quieter local one contributes
nothing. Near-duplicate headlines — the same story from three outlets, which is
what a county's news actually looks like — are collapsed.

Failure of one feed is not failure of the tool: whatever answered is used, and
a feed that is down is logged and skipped.
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

# Feeds vary in whether they are RSS 2.0 or Atom, and Atom is namespaced.
# Rather than register namespaces and write two parsers, this matches on the
# local part of the tag — which is the only thing that differs between the two
# for the four fields wanted here.
_ATOM = "{http://www.w3.org/2005/Atom}"

# Tags in descriptions. Feeds put anchor tags, images and tracking pixels in
# there, and every one of them would be read out loud by a synthesiser.
_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")

# Google News appends " - Publisher" to every headline it syndicates. Useful in
# a reader, noise when five headlines are being read aloud one after another
# and the publisher is named separately anyway.
_TRAILING_SOURCE = re.compile(r"\s+-\s+[^-]{2,40}$")

# How much of a description survives into the summary request. Two sentences is
# what the model needs to write one; the rest is boilerplate, subscription
# pitches and photo credits.
DESCRIPTION_LIMIT = 400


@dataclass(frozen=True)
class Story:
    title: str
    summary: str
    source: str
    link: str
    published: str

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "summary": self.summary,
            "source": self.source,
            "link": self.link,
            "published": self.published,
        }


def _text(element, *names: str) -> str:
    """The first of `names` present on `element`, in either RSS or Atom form."""
    for name in names:
        for tag in (name, f"{_ATOM}{name}"):
            found = element.find(tag)
            if found is None:
                continue
            # Atom puts the URL of a link in an attribute rather than in the
            # body, which is the one place the two formats disagree about
            # where the value lives rather than what it is called.
            if found.text and found.text.strip():
                return found.text.strip()
            href = found.get("href")
            if href:
                return href.strip()
    return ""


def clean(text: str) -> str:
    """A feed field, as something a synthesiser can read.

    Entities are unescaped *before* tags are stripped, deliberately: a feed
    that escapes its markup — `&lt;p&gt;` — would otherwise keep its tags
    through the strip and then have them turned back into angle brackets, and
    the assistant would say "less than p greater than" out loud.
    """
    if not text:
        return ""
    text = html.unescape(text)
    text = _TAGS.sub(" ", text)
    text = html.unescape(text)
    return _SPACE.sub(" ", text).strip()


def _source_of(entry_source: str, feed_title: str, link: str) -> str:
    """Who published this, in order of how much it can be trusted.

    The `<source>` element when the feed carries one — Google News does, and it
    names the real publisher rather than Google. Then the feed's own title. The
    hostname is the last resort, and it is worth having: "mercurynews.com" said
    aloud is poor but it is not nothing.
    """
    if entry_source:
        return entry_source
    if feed_title:
        return feed_title
    host = urlparse(link).netloc
    return host[4:] if host.startswith("www.") else host


def parse_feed(xml_text: str, limit: int = 20) -> list[Story]:
    """Stories from one feed document. Never raises on bad XML.

    A feed that is truncated mid-download, or an error page served with a 200,
    is a normal thing to receive from a network and must not end the tool. It
    is logged and treated as an empty feed.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("feed is not parseable XML: %s", exc)
        return []

    feed_title = clean(_text(root.find("channel") or root, "title"))

    entries = root.findall("./channel/item")
    if not entries:
        entries = root.findall(f"{_ATOM}entry")

    stories: list[Story] = []
    for entry in entries[:limit]:
        title = clean(_text(entry, "title"))
        if not title:
            continue
        link = _text(entry, "link", "id")
        summary = clean(_text(entry, "description", "summary", "content"))
        if len(summary) > DESCRIPTION_LIMIT:
            summary = summary[:DESCRIPTION_LIMIT].rsplit(" ", 1)[0] + "…"
        source = clean(_text(entry, "source"))
        stories.append(Story(
            title=_TRAILING_SOURCE.sub("", title).strip() or title,
            summary=summary,
            source=_source_of(source, feed_title, link),
            link=link,
            published=clean(_text(entry, "pubDate", "published", "updated")),
        ))
    return stories


def _key(title: str) -> frozenset[str]:
    """A headline reduced to what makes it that story rather than another.

    Two outlets covering the same council vote write two different sentences
    about it and share most of their nouns. Comparing the sets of words longer
    than three characters catches that, where comparing the strings does not.
    Short words are dropped because "the", "and" and "for" are shared by every
    headline ever written and would pull unrelated ones together.
    """
    words = re.findall(r"[a-z0-9一-鿿]+", title.lower())
    return frozenset(w for w in words if len(w) > 3)


def _duplicate(key: frozenset[str], seen: list[frozenset[str]]) -> bool:
    """Is this headline substantially one that has already been taken?

    Jaccard over the keyword sets, at 0.6. Chosen to be well clear of the
    ordinary overlap between two unrelated local stories, which share a place
    name at most, while still catching the same story told twice.
    """
    if not key:
        return False
    for other in seen:
        if not other:
            continue
        union = len(key | other)
        if union and len(key & other) / union >= 0.6:
            return True
    return False


def interleave(feeds: list[list[Story]], limit: int) -> list[Story]:
    """One story from each feed in turn, skipping repeats.

    Round-robin rather than concatenation is what stops the busiest feed from
    being the only one heard: Google News alone returns fifty stories a day for
    this query, and a plain `sum(feeds, [])[:5]` would never reach the local
    paper at all.
    """
    picked: list[Story] = []
    keys: list[frozenset[str]] = []
    depth = max((len(f) for f in feeds), default=0)
    for index in range(depth):
        for feed in feeds:
            if index >= len(feed):
                continue
            story = feed[index]
            key = _key(story.title)
            if _duplicate(key, keys):
                log.debug("skipping duplicate headline %r", story.title)
                continue
            picked.append(story)
            keys.append(key)
            if len(picked) >= limit:
                return picked
    return picked


class NewsService:
    """Fetches the configured feeds, caches the result, never raises."""

    def __init__(self, cfg, session: requests.Session | None = None):
        self.cfg = cfg
        self._session = session or requests.Session()
        self._lock = threading.Lock()
        self._cached: list[Story] = []
        self._cached_at = 0.0

    def headlines(self, force: bool = False) -> list[Story]:
        with self._lock:
            age = time.time() - self._cached_at
            if self._cached and not force and age < self.cfg.cache_seconds:
                log.debug("news from cache (%.0fs old)", age)
                return list(self._cached)

            feeds = [self._fetch(url) for url in self.cfg.feeds]
            stories = interleave([f for f in feeds if f], self.cfg.max_stories)
            if stories:
                self._cached = stories
                self._cached_at = time.time()
                return list(stories)

            if self._cached:
                log.warning("every news feed failed; keeping %d cached headline(s)",
                            len(self._cached))
            return list(self._cached)

    def _fetch(self, url: str) -> list[Story]:
        try:
            resp = self._session.get(
                url, timeout=self.cfg.timeout_s,
                # Some publishers serve a block page to the default
                # python-requests agent. Naming the device is honest and gets
                # the feed they publish for readers.
                headers={"User-Agent": "AIPI5/1.0 (Raspberry Pi voice assistant)"},
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("news feed %s failed: %s", url, exc)
            return []
        stories = parse_feed(resp.text)
        log.debug("%d story(ies) from %s", len(stories), url)
        return stories

    def as_dicts(self, force: bool = False) -> list[dict]:
        """What the model is handed. Titles and blurbs, no article bodies."""
        return [story.as_dict() for story in self.headlines(force)]

    def close(self) -> None:
        self._session.close()
