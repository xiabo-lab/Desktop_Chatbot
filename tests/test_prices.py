"""A price, or nothing. Never the model's memory.

That is the whole of it. A model asked what Bitcoin is worth produces a
confident number, in the right currency, with the right number of digits, and
it is whatever was true when the model was trained. Nothing about the answer
looks wrong — which is what makes it the worst kind of failure this device can
have: somebody in a kitchen hears a figure and has no way at all to tell that
it is eight months old.

So every test here is about the boundary between "I have a quote with a
timestamp" and "I could not check", and about the one case in between: a cached
quote is worth offering *with its age attached* and worth nothing without it.

No network anywhere. The session is a fake.
"""

from __future__ import annotations

import json
import unittest

import requests

from aipi5.llm.tools import ToolBox
from aipi5.tools.prices import (ASSETS, CURRENCIES, PriceError, PriceService,
                                STALE_S)


class Clock:
    def __init__(self, now=1_756_800_000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeResponse:
    def __init__(self, payload=None, status=200, unreadable=False):
        self._payload = payload if payload is not None else {}
        self.status = status
        self.unreadable = unreadable

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status}")

    def json(self):
        if self.unreadable:
            raise ValueError("not JSON")
        return self._payload


class FakeSession:
    """`requests.Session.get`, scripted. Records the parameters it was given."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append({"url": url, "params": dict(params or {}),
                           "timeout": timeout})
        if not self.answers:
            raise requests.ConnectionError("network is down")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def quote_payload(ident="bitcoin", code="usd", price=61_000.0, at=None):
    row = {code: price}
    if at is not None:
        row["last_updated_at"] = at
    return {ident: row}


def service(*answers, clock=None, **kwargs):
    clock = clock or Clock()
    made = PriceService(session=FakeSession(*answers), clock=clock, **kwargs)
    made.clock_obj = clock
    return made


class TestWhatMayBeAsked(unittest.TestCase):
    """Both arguments come out of tables, so what reaches the query string is a
    literal from `prices.py` whatever the model wrote."""

    def test_an_asset_this_device_does_not_know_is_refused_by_name(self):
        with self.assertRaises(PriceError) as raised:
            service().quote("tesla")
        self.assertIn("do not have a price", str(raised.exception))
        self.assertIn("bitcoin", str(raised.exception))

    def test_a_ticker_and_a_name_reach_the_same_asset(self):
        self.assertEqual("bitcoin", ASSETS["btc"])
        self.assertEqual("bitcoin", ASSETS["bitcoin"])

    def test_a_currency_this_device_does_not_quote_in_is_refused(self):
        with self.assertRaises(PriceError) as raised:
            service().quote("bitcoin", "xbt")
        self.assertIn("cannot quote in", str(raised.exception))

    def test_nothing_the_model_wrote_reaches_the_query_string(self):
        made = service(FakeResponse(quote_payload()))
        made.quote("BTC ", " USD")
        params = made._session.calls[0]["params"]
        self.assertEqual("bitcoin", params["ids"])
        self.assertEqual("usd", params["vs_currencies"])

    def test_an_injected_asset_never_reaches_the_provider(self):
        made = service(FakeResponse(quote_payload()))
        with self.assertRaises(PriceError):
            made.quote("bitcoin&ids=../../secret")
        self.assertEqual([], made._session.calls)


class TestAGoodQuote(unittest.TestCase):

    def test_it_carries_price_currency_source_and_a_time(self):
        clock = Clock()
        made = service(FakeResponse(quote_payload(at=clock.now - 30)),
                       clock=clock)
        answer = made.quote("bitcoin").as_dict(clock.now)
        self.assertEqual(61_000.0, answer["price"])
        self.assertEqual("usd", answer["currency"])
        self.assertEqual("coingecko", answer["source"])
        self.assertIn("-", answer["as_of"])
        self.assertEqual(30, answer["age_seconds"])

    def test_the_currency_is_given_in_words_as_well(self):
        """So the assistant does not read "usd" out loud."""
        made = service(FakeResponse(quote_payload(code="cny", price=440_000.0)))
        answer = made.quote("bitcoin", "cny").as_dict()
        self.assertEqual("Chinese yuan", answer["currency_spoken"])

    def test_the_providers_own_timestamp_is_used_where_there_is_one(self):
        """Stamping a quote with the moment it was received reports a
        five-minute-old figure as current — the smaller version of the same
        lie."""
        clock = Clock()
        made = service(FakeResponse(quote_payload(at=clock.now - 300)),
                       clock=clock)
        self.assertEqual(300, made.quote("bitcoin").as_dict(clock.now)["age_seconds"])

    def test_a_provider_clock_that_disagrees_wildly_is_refused(self):
        """This test used to assert the opposite, and the opposite was wrong.

        The reasoning was that the Pi has no RTC and CoinGecko is not wrong by
        a year, so a timestamp that far out is a clock to distrust rather than
        a fact to report -- and the quote was restamped `now`. But a distrusted
        clock means the age is *unknown*, and restamping it says the age is
        zero. The two readings of a wild timestamp are "our clock is wrong" and
        "this price is old", they cannot be told apart from here, and only one
        of them is safe to guess at.

        See `TestAPriceThatCannotBeDated` for what happens instead.
        """
        clock = Clock()
        made = service(FakeResponse(quote_payload(at=clock.now - 400_000)),
                       clock=clock)
        with self.assertRaises(PriceError):
            made.quote("bitcoin")

    def test_a_fresh_quote_carries_no_apology(self):
        made = service(FakeResponse(quote_payload()))
        self.assertEqual("", made.quote("bitcoin").as_dict()["note"])


class TestTheCache(unittest.TestCase):

    def test_a_repeated_question_does_not_ask_again(self):
        """The public endpoint is rate limited and "what's bitcoin doing" gets
        asked three times in a row."""
        clock = Clock()
        made = service(FakeResponse(quote_payload()), clock=clock)
        made.quote("bitcoin")
        made.quote("btc")
        self.assertEqual(1, len(made._session.calls))

    def test_it_asks_again_once_the_cache_is_old(self):
        clock = Clock()
        made = service(FakeResponse(quote_payload()),
                       FakeResponse(quote_payload(price=62_000.0)), clock=clock)
        self.assertEqual(61_000.0, made.quote("bitcoin").price)
        clock.now += 120
        self.assertEqual(62_000.0, made.quote("bitcoin").price)

    def test_two_currencies_are_cached_separately(self):
        clock = Clock()
        made = service(FakeResponse(quote_payload()),
                       FakeResponse(quote_payload(code="eur", price=57_000.0)),
                       clock=clock)
        self.assertEqual(61_000.0, made.quote("bitcoin", "usd").price)
        self.assertEqual(57_000.0, made.quote("bitcoin", "eur").price)


class TestWhenTheProviderIsDown(unittest.TestCase):

    def test_no_network_at_all_is_a_sentence_and_not_a_traceback(self):
        with self.assertRaises(PriceError) as raised:
            service().quote("bitcoin")
        self.assertIn("could not reach", str(raised.exception))

    def test_a_timeout_is_the_same(self):
        with self.assertRaises(PriceError):
            service(requests.Timeout("timed out")).quote("bitcoin")

    def test_a_five_hundred_is_the_same(self):
        with self.assertRaises(PriceError):
            service(FakeResponse(status=503)).quote("bitcoin")

    def test_a_body_that_is_not_json_says_so(self):
        with self.assertRaises(PriceError) as raised:
            service(FakeResponse(unreadable=True)).quote("bitcoin")
        self.assertIn("could not read", str(raised.exception))

    def test_a_reply_with_no_row_for_the_asset_is_refused(self):
        with self.assertRaises(PriceError) as raised:
            service(FakeResponse({})).quote("bitcoin")
        self.assertIn("does not know about", str(raised.exception))

    def test_a_price_that_is_not_a_number_is_refused(self):
        for junk in ("61000", None, True, [61000]):
            with self.subTest(price=junk):
                with self.assertRaises(PriceError):
                    service(FakeResponse({"bitcoin": {"usd": junk}})).quote(
                        "bitcoin")

    def test_a_stale_quote_is_offered_with_its_age_and_a_note(self):
        """"About sixty-one thousand, from four minutes ago" is useful. The
        same figure with its age hidden is the model's memory by another
        route."""
        clock = Clock()
        made = service(FakeResponse(quote_payload()), clock=clock)
        made.quote("bitcoin")
        clock.now += 600                        # the session has no more answers
        answer = made.quote("bitcoin").as_dict(clock.now)
        self.assertEqual(61_000.0, answer["price"])
        self.assertEqual(600, answer["age_seconds"])
        self.assertIn("10 minutes ago", answer["note"])
        self.assertIn("not answering", answer["note"])

    def test_a_quote_older_than_an_hour_is_not_offered_at_all(self):
        clock = Clock()
        made = service(FakeResponse(quote_payload()), clock=clock)
        made.quote("bitcoin")
        clock.now += STALE_S + 60
        with self.assertRaises(PriceError):
            made.quote("bitcoin")

    def test_a_cached_quote_a_minute_old_needs_no_apology(self):
        """The judgement about when an age matters is made here rather than by
        the model, which would apologise for everything or for nothing."""
        clock = Clock()
        made = service(FakeResponse(quote_payload()), clock=clock)
        made.quote("bitcoin")
        clock.now += 90
        self.assertEqual("", made.quote("bitcoin").as_dict(clock.now)["note"])


class TestTheTool(unittest.TestCase):

    def call(self, box, **args):
        return json.loads(box.call("get_asset_price", json.dumps(args)))

    def test_it_answers_with_everything_needed_to_say_it_honestly(self):
        box = ToolBox(prices=service(FakeResponse(quote_payload())))
        answer = self.call(box, asset="bitcoin")
        self.assertTrue(answer["ok"])
        for field in ("price", "currency", "currency_spoken", "source", "as_of",
                      "age_seconds"):
            with self.subTest(field=field):
                self.assertIn(field, answer)

    def test_a_failure_is_an_error_and_never_a_number(self):
        """The one branch that must not exist. A tool that fell back to a
        guess would be indistinguishable from working."""
        answer = self.call(ToolBox(prices=service()), asset="bitcoin")
        self.assertFalse(answer["ok"])
        self.assertNotIn("price", answer)

    def test_a_null_currency_is_dollars_and_not_the_word_none(self):
        box = ToolBox(prices=service(FakeResponse(quote_payload())))
        self.assertEqual("usd", self.call(box, asset="btc",
                                          currency=None)["currency"])

    def test_the_enums_come_from_the_module_and_not_from_a_second_list(self):
        box = ToolBox(prices=service())
        schema = next(t for t in box.schemas() if t["name"] == "get_asset_price")
        self.assertEqual(sorted(set(ASSETS.values())),
                         schema["parameters"]["properties"]["asset"]["enum"])
        # `currency` is optional, so its enum carries a trailing None --
        # without it `{"type": ["string", "null"]}` is a lie, because JSON
        # Schema applies both keywords and null fails the enum. The point of
        # this test is that the values come from the module.
        self.assertEqual(list(CURRENCIES) + [None],
                         schema["parameters"]["properties"]["currency"]["enum"])

    def test_the_schema_says_never_to_answer_from_memory(self):
        box = ToolBox(prices=service())
        schema = next(t for t in box.schemas() if t["name"] == "get_asset_price")
        self.assertIn("never answer a price from memory", schema["description"])

    def test_it_is_not_offered_without_a_provider(self):
        self.assertEqual([], [t for t in ToolBox().schemas()
                              if t.get("name") == "get_asset_price"])


if __name__ == "__main__":
    unittest.main()


class TestAPriceThatCannotBeDated(unittest.TestCase):
    """A provider timestamp far from our own clock used to be replaced with
    `now`, on the reasoning that the Pi has no real-time clock and CoinGecko is
    not wrong by an hour.

    The two cases are indistinguishable from here and are not equally safe. If
    the provider really did send an hour-old figure, stamping it `now` turns
    demonstrably stale money into an apparently current quote — the exact lie
    this module exists to prevent, and worse than the "answering from memory"
    it was written against, because it carries a timestamp saying otherwise.
    If instead our clock is wrong, the age is unknown, and a price whose age is
    unknown is not one to say out loud.
    """

    def test_an_hour_old_stamp_is_refused_rather_than_restamped(self):
        clock = Clock()
        made = service(FakeResponse(quote_payload(at=clock.now - 7200)),
                       clock=clock)
        with self.assertRaises(PriceError) as raised:
            made.quote("bitcoin", "usd")
        self.assertIn("current", str(raised.exception))

    def test_a_stamp_from_the_future_is_refused_too(self):
        clock = Clock()
        made = service(FakeResponse(quote_payload(at=clock.now + 4000)),
                       clock=clock)
        with self.assertRaises(PriceError):
            made.quote("bitcoin", "usd")

    def test_the_last_real_quote_is_offered_with_its_own_age(self):
        """What the person gets instead: the previous price, honestly dated.
        Not silence, and not a fresh-looking number."""
        clock = Clock()
        made = service(FakeResponse(quote_payload(price=61_000.0, at=clock.now)),
                       FakeResponse(quote_payload(price=99_000.0,
                                                  at=clock.now - 7200)),
                       clock=clock)
        self.assertEqual(61_000.0, made.quote("bitcoin", "usd").price)
        clock.now += 300
        later = made.quote("bitcoin", "usd")
        self.assertEqual(61_000.0, later.price)
        self.assertTrue(later.cached)
        self.assertEqual(300, round(later.age_s(clock.now)))

    def test_a_small_disagreement_is_still_accepted(self):
        """Seconds of skew are normal and are not what this guards against."""
        clock = Clock()
        made = service(FakeResponse(quote_payload(at=clock.now - 30)),
                       clock=clock)
        self.assertEqual(30, round(made.quote("bitcoin", "usd").age_s(clock.now)))

    def test_no_stamp_at_all_is_still_dated_on_arrival(self):
        """A provider that sends none is a different case: nothing is being
        contradicted, and receipt time is the best available and is honest
        about being seconds old."""
        clock = Clock()
        made = service(FakeResponse(quote_payload(at=None)), clock=clock)
        self.assertEqual(0, round(made.quote("bitcoin", "usd").age_s(clock.now)))
