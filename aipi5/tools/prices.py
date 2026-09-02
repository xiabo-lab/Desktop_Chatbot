"""What something costs right now, from somewhere that knows.

There is exactly one rule here and everything else is a consequence of it:
**a price is never answered from the model's memory.** A model asked what
Bitcoin is worth will produce a number, confidently, in the right currency,
with the right number of digits, and it will be whatever was true when the
model was trained. Nothing about the answer looks wrong. Somebody in a kitchen
hears a figure and has no way at all to tell that it is eight months old.

So this module either has a real quote with a timestamp on it, or it says it
could not check. There is no third branch, and the tool that calls it must not
invent one — see `_get_asset_price` in `aipi5/llm/tools.py`, which returns an
error rather than a guess and says so in a sentence somebody can act on.

**Coingecko, because it needs no key.** This device holds one API credential
and adding a second — with its own rotation, its own free-tier expiry and its
own way of failing at three in the morning — buys nothing for a question asked
twice a month. The public endpoint is rate limited rather than authenticated,
which the cache below is sized for.

**A short cache, and a stale one is still served.** Sixty seconds is far
fresher than anybody asking out loud needs and keeps a repeated question off
the network. When the provider is unreachable the last quote is offered
*with its real age*, because "about sixty-one thousand dollars, from four
minutes ago" is useful and "I could not check" is not — but a quote whose age
is hidden is the same failure as the model's memory, so the age always travels
with it and the caller is expected to say it.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

log = logging.getLogger(__name__)

PROVIDER = "coingecko"
ENDPOINT = "https://api.coingecko.com/api/v3/simple/price"

#: How long a quote is reused without asking again. Short, because the whole
#: point is that this is current; not zero, because the public endpoint is rate
#: limited and "what's bitcoin doing" gets asked three times in a row.
CACHE_S = 60.0

#: How old a cached quote may be before it is not worth offering at all. Past
#: this it is a number from another part of the day and saying it — even with
#: its age attached — is worse than saying the check failed.
STALE_S = 3600.0

TIMEOUT_S = 8.0

#: The assets this device knows the id of, by the words somebody would say.
#: A table rather than a pass-through: the provider takes an id like
#: `"bitcoin"`, and forwarding whatever the model wrote would be model output
#: reaching a URL. Every value here is a literal in this file.
ASSETS = {
    "bitcoin": "bitcoin",
    "btc": "bitcoin",
    "ethereum": "ethereum",
    "eth": "ethereum",
    "solana": "solana",
    "sol": "solana",
    "dogecoin": "dogecoin",
    "doge": "dogecoin",
    "cardano": "cardano",
    "ada": "cardano",
    "litecoin": "litecoin",
    "ltc": "litecoin",
}

#: Currencies this device will quote in. Also a table, and also for the reason
#: above: it goes into a query string.
CURRENCIES = ("usd", "eur", "gbp", "cny", "jpy", "twd", "hkd", "cad", "aud")

#: How each is said out loud, so the assistant does not read "usd".
CURRENCY_WORDS = {"usd": "US dollars", "eur": "euros", "gbp": "pounds",
                  "cny": "Chinese yuan", "jpy": "Japanese yen",
                  "twd": "New Taiwan dollars", "hkd": "Hong Kong dollars",
                  "cad": "Canadian dollars", "aud": "Australian dollars"}


class PriceError(RuntimeError):
    """No usable quote, with a sentence about why.

    Prose, because the caller reads it out loud. "I could not reach the price
    service" is something a person can act on; a `JSONDecodeError` is not.
    """


class Quote:
    """One price, and everything needed to say it honestly."""

    def __init__(self, asset: str, currency: str, price: float,
                 as_of: float, provider: str = PROVIDER, cached: bool = False):
        self.asset = asset
        self.currency = currency
        self.price = price
        self.as_of = as_of
        self.provider = provider
        self.cached = cached

    def age_s(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.as_of)

    def as_dict(self, now: float | None = None) -> dict:
        age = self.age_s(now)
        return {
            "asset": self.asset,
            "price": self.price,
            "currency": self.currency,
            "currency_spoken": CURRENCY_WORDS.get(self.currency, self.currency),
            "source": self.provider,
            "as_of": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.as_of)),
            "age_seconds": round(age),
            # Said out loud when it matters. A quote a minute old needs no
            # qualification; one from ten minutes ago does, and the difference
            # is a judgement this module makes rather than the model.
            "note": ("This is the last price I could get, from "
                     f"{round(age / 60)} minutes ago — the price service is not "
                     "answering right now."
                     if self.cached and age > 120 else ""),
        }


class PriceService:
    """One provider, a table of what it may be asked, and a short cache.

    Never raises out of `quote()` except `PriceError`, which carries a spoken
    sentence. Nothing here retries: the endpoint is rate limited, a person is
    standing there waiting, and a second attempt against a service that is
    down costs them another eight seconds of silence.
    """

    def __init__(self, session=None, clock=time.time, timeout_s: float = TIMEOUT_S,
                 cache_s: float = CACHE_S, endpoint: str = ENDPOINT):
        self._session = session or requests.Session()
        self.clock = clock
        self.timeout_s = timeout_s
        self.cache_s = cache_s
        self.endpoint = endpoint
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], Quote] = {}

    # ── what may be asked ───────────────────────────────────────────

    def resolve(self, asset: str, currency: str) -> tuple[str, str]:
        """(provider id, currency code). Raises `PriceError` on either.

        Both come out of the tables above, so what reaches the query string is
        a literal from this file whatever the model wrote.
        """
        key = str(asset or "").strip().lower()
        ident = ASSETS.get(key)
        if ident is None:
            raise PriceError(
                f"I do not have a price for {asset!r}. I can check "
                + ", ".join(sorted(set(ASSETS.values()))) + ".")
        code = str(currency or "usd").strip().lower()
        if code not in CURRENCIES:
            raise PriceError(
                f"I cannot quote in {currency!r}. I can use "
                + ", ".join(c.upper() for c in CURRENCIES) + ".")
        return ident, code

    # ── the quote ───────────────────────────────────────────────────

    def quote(self, asset: str, currency: str = "usd") -> Quote:
        ident, code = self.resolve(asset, currency)
        now = self.clock()

        with self._lock:
            cached = self._cache.get((ident, code))
        if cached is not None and now - cached.as_of < self.cache_s:
            log.debug("%s from cache (%.0fs old)", ident, now - cached.as_of)
            return Quote(ident, code, cached.price, cached.as_of, cached.provider,
                         cached=False)

        try:
            fresh = self._fetch(ident, code, now)
        except PriceError:
            # A stale quote offered *with its age* is useful; a stale quote
            # offered as though it were current is the same failure as
            # answering from memory, which is what this module exists to
            # prevent. So the age travels with it and the caller says it.
            if cached is not None and now - cached.as_of < STALE_S:
                log.info("%s: the provider failed; offering a %.0fs old price",
                         ident, now - cached.as_of)
                return Quote(ident, code, cached.price, cached.as_of,
                             cached.provider, cached=True)
            raise

        with self._lock:
            self._cache[(ident, code)] = fresh
        return fresh

    def _fetch(self, ident: str, code: str, now: float) -> Quote:
        try:
            response = self._session.get(
                self.endpoint,
                params={"ids": ident, "vs_currencies": code,
                        "include_last_updated_at": "true"},
                timeout=self.timeout_s,
                headers={"User-Agent": "AIPI5/1.0 (Raspberry Pi voice assistant)"},
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            log.warning("price for %s failed: %s", ident, exc)
            raise PriceError("I could not reach the price service just now")

        try:
            payload = response.json()
        except ValueError:
            log.warning("the price service sent something unreadable")
            raise PriceError("the price service sent something I could not read")

        row = payload.get(ident) if isinstance(payload, dict) else None
        if not isinstance(row, dict):
            raise PriceError(f"the price service does not know about {ident}")

        price = row.get(code)
        if not isinstance(price, (int, float)) or isinstance(price, bool):
            raise PriceError(f"the price service did not give a price in "
                             f"{code.upper()}")

        # The provider's own timestamp where it gave one. A quote stamped with
        # the moment it was *received* would report a five-minute-old figure as
        # current, which is the smaller version of the same lie.
        stamped = row.get("last_updated_at")
        as_of = float(stamped) if isinstance(stamped, (int, float)) else now
        if as_of > now + 60 or as_of < now - STALE_S:
            # **Refused, not restamped.** This used to set `as_of = now` on the
            # reasoning that the Pi has no real-time clock and a provider is
            # not wrong by an hour — but the two cases are indistinguishable
            # from here, and they are not equally safe. If the provider really
            # did send an hour-old figure, stamping it `now` turns demonstrably
            # stale money into an apparently current quote, which is the exact
            # lie this whole module exists to prevent. If instead our clock is
            # wrong, we cannot say how old anything is, and a price whose age
            # is unknown is not a price worth saying out loud.
            #
            # Either way the honest answer is that there is no current quote.
            # The caller falls back to the last cached one, which carries its
            # own real age.
            log.warning("%s came back stamped %s against a clock reading %s",
                        ident,
                        time.strftime("%Y-%m-%d %H:%M", time.localtime(as_of)),
                        time.strftime("%Y-%m-%d %H:%M", time.localtime(now)))
            raise PriceError(
                "the price service sent a price I cannot date, so I do not "
                "know whether it is current")

        log.info("%s = %s %s (as of %s)", ident, price, code.upper(),
                 time.strftime("%H:%M", time.localtime(as_of)))
        return Quote(ident, code, float(price), as_of)

    def describe(self) -> dict:
        """For the settings page and the preflight report."""
        return {"provider": PROVIDER, "assets": sorted(set(ASSETS.values())),
                "currencies": list(CURRENCIES), "cache_seconds": self.cache_s}

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:                            # noqa: BLE001
            log.debug("closing the price session failed", exc_info=True)
