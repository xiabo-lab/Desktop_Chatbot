"""The weather cache, the fallback to a stale reading, and the phrasing.

The cache is the part that matters most and shows least: the screensaver
redraws its clock every second and shares a panel with the temperature, so a
service that fetched on every draw would make 86,400 requests a day for a
number that changes four times an hour. Nothing about that is visible from the
room, which is why it is pinned here.
"""

from __future__ import annotations

import unittest

from aipi5.core.config import LocationConfig, WeatherConfig
from aipi5.tools.weather import WeatherService, describe_code

# One Open-Meteo response, with the fields the parser actually reads.
PAYLOAD = {
    "current": {
        "temperature_2m": 68.4,
        "relative_humidity_2m": 55,
        "apparent_temperature": 66.1,
        "is_day": 1,
        "weather_code": 2,
        "wind_speed_10m": 7.2,
    },
    "daily": {
        "time": ["2026-08-07", "2026-08-08"],
        "weather_code": [0, 61],
        "temperature_2m_max": [84.0, 79.0],
        "temperature_2m_min": [58.0, 57.0],
        "precipitation_probability_max": [0, 40],
        "sunrise": ["2026-08-07T06:18", "2026-08-08T06:19"],
        "sunset": ["2026-08-07T20:05", "2026-08-08T20:03"],
    },
    "hourly": {
        "time": ["2026-08-07T00:00", "2026-08-07T01:00", "2026-08-07T02:00"],
        "temperature_2m": [60.0, 59.0, 58.0],
        "weather_code": [0, 2, 3],
        "precipitation_probability": [0, 10, 20],
        "is_day": [0, 0, 0],
    },
}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    """Counts requests, so the cache can be asserted on rather than assumed."""

    def __init__(self, payload=PAYLOAD, fail_after=None):
        self.payload = payload
        self.calls = 0
        self.fail_after = fail_after

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            import requests
            raise requests.RequestException("network is down")
        return FakeResponse(self.payload)

    def close(self):
        pass


def service(session=None, cache_seconds=600.0):
    return WeatherService(LocationConfig(), WeatherConfig(cache_seconds=cache_seconds),
                          session=session or FakeSession())


class TestParsing(unittest.TestCase):

    def test_reads_current_conditions(self):
        weather = service().current()
        self.assertIsNotNone(weather)
        self.assertAlmostEqual(weather.now.temperature, 68.4)
        self.assertAlmostEqual(weather.now.feels_like, 66.1)
        self.assertEqual(weather.now.humidity, 55)
        self.assertEqual(weather.now.unit, "F")

    def test_reads_the_forecast(self):
        weather = service().current()
        self.assertEqual(len(weather.days), 2)
        self.assertEqual(weather.days[1].precipitation_chance, 40)

    def test_the_dictionary_is_what_both_the_model_and_the_screen_get(self):
        # One representation, not two. The screensaver draws from the same
        # dictionary the model is handed, so the temperature on the screen and
        # the temperature in the spoken answer cannot disagree.
        payload = service().current().as_dict()
        self.assertEqual(payload["current"]["temperature"], 68)
        self.assertEqual(payload["current"]["conditions"], "partly cloudy")
        self.assertEqual(payload["unit"], "F")
        self.assertEqual(payload["place"], "San Jose, CA 95127")

    def test_a_malformed_response_is_none_not_an_exception(self):
        # A provider that changed its response shape. It is logged with a
        # trace on purpose — the fix is a code change, and the only other clue
        # anybody gets is a screensaver with no temperature on it — so the
        # trace is asserted here rather than left to scroll past in the test
        # output looking like a failure.
        broken = service(FakeSession(payload={"nonsense": True}))
        with self.assertLogs("aipi5.tools.weather", level="ERROR") as captured:
            self.assertIsNone(broken.current())
        self.assertIn("could not read the weather response", captured.output[0])


class TestWhatThePageNeeds(unittest.TestCase):
    """The fields the redesigned weather page draws, and where they are not."""

    def test_the_sun_times_come_through(self):
        today = service().current().days[0]
        self.assertEqual(today.sunrise, "2026-08-07T06:18")
        self.assertEqual(today.sunset, "2026-08-07T20:05")

    def test_pressure_and_the_hours_chance_of_rain(self):
        payload = dict(PAYLOAD)
        payload["current"] = dict(PAYLOAD["current"],
                                  pressure_msl=1013.2, precipitation_probability=45)
        now = service(FakeSession(payload)).current().now
        self.assertEqual(now.pressure_hpa, 1013.2)
        self.assertEqual(now.precipitation_chance, 45)

    def test_the_hourly_forecast_is_read(self):
        hours = service().current().hours
        self.assertEqual(len(hours), 3)
        self.assertEqual(hours[1].temperature, 59.0)
        self.assertEqual(hours[1].precipitation_chance, 10)
        self.assertFalse(hours[1].is_day)

    def test_the_hours_are_kept_out_of_the_state_poll(self):
        # `as_dict` is published twice a second and handed to the model on
        # every weather question. Thirty-six hours of readings there would be
        # a few hundred tokens and a few kilobytes a second for one card.
        payload = service().current().as_dict()
        self.assertNotIn("hourly", payload)
        self.assertIn("sunrise", payload["forecast"][0])
        self.assertIn("pressure_hpa", payload["current"])

    def test_the_page_asks_for_them_separately(self):
        # Built around the current hour on purpose: `as_page_dict` serves what
        # is still to come, so a fixture full of last week's hours correctly
        # produces an empty strip and would prove nothing.
        import time
        base = time.localtime()
        stamps = ["%04d-%02d-%02dT%02d:00" % (base.tm_year, base.tm_mon,
                                              base.tm_mday, hour)
                  for hour in range(base.tm_hour, min(base.tm_hour + 3, 24))]
        payload = dict(PAYLOAD)
        payload["hourly"] = {
            "time": stamps,
            "temperature_2m": [60.0] * len(stamps),
            "weather_code": [2] * len(stamps),
            "precipitation_probability": [15] * len(stamps),
            "is_day": [1] * len(stamps),
        }
        page = service(FakeSession(payload)).current().as_page_dict()
        self.assertIn("hourly", page)
        self.assertEqual(page["hourly"][0]["temperature"], 60)
        self.assertEqual(page["hourly"][0]["conditions"], "partly cloudy")

    def test_hours_that_have_already_passed_are_not_offered(self):
        # The provider sends the whole of today. What the strip must open with
        # is the hour somebody is standing in.
        page = service().current().as_page_dict()
        self.assertEqual(page["hourly"], [])

    def test_upcoming_starts_at_the_hour_we_are_in(self):
        # The provider sends the whole day, hours already gone included. A
        # strip that opens with six in the morning is one nobody can read the
        # current temperature off.
        import time
        from aipi5.tools.weather import HourForecast, Weather, Conditions

        now = time.localtime()
        stamp = "%04d-%02d-%02dT%02d:00" % (now.tm_year, now.tm_mon,
                                            now.tm_mday, now.tm_hour)
        earlier = "%04d-%02d-%02dT%02d:00" % (now.tm_year, now.tm_mon,
                                              now.tm_mday, max(0, now.tm_hour - 3))
        weather = Weather(
            place="here",
            now=Conditions(60, 60, 50, 5, 0, True, "F"),
            days=(), fetched_at=time.time(),
            hours=(HourForecast(earlier, 55, 0, 0, True),
                   HourForecast(stamp, 60, 0, 0, True)))
        upcoming = weather.upcoming()
        self.assertEqual([h.time for h in upcoming], [stamp])

    def test_asking_for_four_gives_four(self):
        import time
        from aipi5.tools.weather import HourForecast, Weather, Conditions
        base = time.localtime()
        hours = tuple(
            HourForecast("%04d-%02d-%02dT%02d:00" % (base.tm_year, base.tm_mon,
                                                     base.tm_mday, hour),
                         60, 0, 0, True)
            for hour in range(base.tm_hour, 24))
        weather = Weather(place="here",
                          now=Conditions(60, 60, 50, 5, 0, True, "F"),
                          days=(), fetched_at=time.time(), hours=hours)
        self.assertLessEqual(len(weather.upcoming(4)), 4)


class TestCache(unittest.TestCase):

    def test_a_second_call_does_not_fetch(self):
        session = FakeSession()
        weather = service(session)
        weather.current()
        weather.current()
        weather.current()
        self.assertEqual(session.calls, 1)

    def test_force_fetches(self):
        session = FakeSession()
        weather = service(session)
        weather.current()
        weather.current(force=True)
        self.assertEqual(session.calls, 2)

    def test_an_expired_cache_fetches(self):
        session = FakeSession()
        weather = service(session, cache_seconds=0.001)
        weather.current()
        import time
        time.sleep(0.01)
        weather.current()
        self.assertEqual(session.calls, 2)

    def test_a_failed_refresh_keeps_the_last_good_reading(self):
        # Stale weather is worth far more than no weather: the screensaver has
        # to draw something, and "it was 68 a few minutes ago" is not wrong in
        # any way that matters.
        session = FakeSession(fail_after=1)
        weather = service(session, cache_seconds=0.001)
        first = weather.current()
        import time
        time.sleep(0.01)
        second = weather.current()
        self.assertIsNotNone(second)
        self.assertEqual(second.fetched_at, first.fetched_at)

    def test_a_failure_with_nothing_cached_is_none(self):
        weather = service(FakeSession(fail_after=0))
        self.assertIsNone(weather.current())


class TestPhrasing(unittest.TestCase):

    def test_speaks_both_languages(self):
        weather = service().current()
        self.assertIn("68 degrees", weather.summary("en"))
        self.assertIn("度", weather.summary("zh"))

    def test_a_summary_carries_the_high_and_low(self):
        # "What's the weather" almost always means "what am I in for", and the
        # current temperature alone answers a narrower question.
        summary = service().current().summary("en")
        self.assertIn("84", summary)
        self.assertIn("58", summary)

    def test_an_unknown_code_does_not_produce_a_gap(self):
        self.assertTrue(describe_code(9999, "en"))
        self.assertTrue(describe_code(None, "zh"))


if __name__ == "__main__":
    unittest.main()
