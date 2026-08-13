"""The weather page's judgements, which are rules and therefore testable.

Worth a file of its own because these are the sentences somebody acts on — an
umbrella taken or not taken, a walk started or not started — and because they
are exactly the kind of logic that looks obviously right while being wrong at
one end. Rain already falling with a 10% forecast, a UV index of zero at
midnight, a missing reading: each of those has a correct answer and none of
them is the answer the simple version gives.

No network and no model. That is the point of them.
"""

from __future__ import annotations

import unittest

from aipi5.tools.advice import (activity, clothing, outdoor, should_go_outside,
                                to_celsius, umbrella, uv_band)


def weather(**current) -> dict:
    """A payload shaped like `Weather.as_dict`, with today attached."""
    base = {"temperature": 68, "feels_like": 66, "humidity": 50,
            "wind_mph": 6, "conditions": "clear", "code": 0, "is_day": True,
            "uv_index": 4.0, "pressure_hpa": 1013.0,
            "precipitation_chance": 5}
    base.update(current)
    return {"unit": "F", "current": base,
            "forecast": [{"high": 75, "low": 55, "precipitation_chance": 10,
                          "uv_index_max": 5.0, "code": 0}]}


class TestUnits(unittest.TestCase):

    def test_fahrenheit_becomes_celsius(self):
        self.assertAlmostEqual(to_celsius(32, "F"), 0.0)
        self.assertAlmostEqual(to_celsius(212, "F"), 100.0)
        self.assertAlmostEqual(to_celsius(68, "F"), 20.0)

    def test_celsius_is_left_alone(self):
        self.assertAlmostEqual(to_celsius(20, "C"), 20.0)


class TestUvBand(unittest.TestCase):

    def test_the_who_scale(self):
        self.assertEqual(uv_band(0), "Low")
        self.assertEqual(uv_band(2.9), "Low")
        self.assertEqual(uv_band(3), "Moderate")
        self.assertEqual(uv_band(6), "High")
        self.assertEqual(uv_band(8), "Very High")
        self.assertEqual(uv_band(11), "Extreme")

    def test_no_reading_is_not_a_low_reading(self):
        # Zero is what night is; None is the provider not saying. A page that
        # cannot tell them apart shows "Low" at noon under a broken sensor.
        self.assertEqual(uv_band(None), "")


class TestUmbrella(unittest.TestCase):

    def test_by_probability(self):
        self.assertEqual(umbrella(0, 0), "No")
        self.assertEqual(umbrella(25, 0), "Optional")
        self.assertEqual(umbrella(45, 0), "Recommended")
        self.assertEqual(umbrella(80, 0), "Need")

    def test_rain_falling_now_outranks_the_forecast(self):
        # A 10% chance while it is raining is a forecast losing an argument
        # with the window.
        self.assertEqual(umbrella(10, 63), "Need")
        self.assertEqual(umbrella(0, 95), "Need")

    def test_a_missing_probability_is_not_a_crash(self):
        self.assertEqual(umbrella(None, 0), "No")


class TestClothing(unittest.TestCase):

    def test_by_how_it_feels(self):
        self.assertEqual(clothing(25, 0), "Light")
        self.assertEqual(clothing(12, 0), "Medium")
        self.assertEqual(clothing(2, 0), "Warm")

    def test_rain_and_snow_win(self):
        self.assertEqual(clothing(25, 63), "Rain Gear")
        self.assertEqual(clothing(-5, 73), "Rain Gear")


class TestOutdoor(unittest.TestCase):

    def test_a_pleasant_day(self):
        self.assertEqual(outdoor(0, 5, 20, 3), "Go")

    def test_a_thunderstorm_is_not_a_walk(self):
        self.assertEqual(outdoor(95, 90, 20, 1), "Avoid")
        self.assertEqual(outdoor(65, 90, 15, 1), "Avoid")

    def test_extremes_of_temperature(self):
        self.assertEqual(outdoor(0, 0, 40, 5), "Avoid")
        self.assertEqual(outdoor(0, 0, -10, 1), "Avoid")
        self.assertEqual(outdoor(0, 0, 35, 5), "Caution")

    def test_strong_sun_is_a_caution_not_a_refusal(self):
        self.assertEqual(outdoor(0, 0, 25, 9), "Caution")


class TestTheActivityCard(unittest.TestCase):

    def test_a_clear_mild_day(self):
        marks = activity(weather())
        self.assertEqual(marks, {"uv": "Moderate", "outdoor": "Go",
                                 "umbrella": "No", "clothing": "Light"})

    def test_it_falls_back_to_the_days_numbers(self):
        # Open-Meteo does not always send an hourly probability with `current`.
        # The day's maximum is the honest substitute: an afternoon of rain is
        # worth an umbrella at ten in the morning.
        payload = weather(precipitation_chance=None, uv_index=None)
        payload["forecast"][0]["precipitation_chance"] = 80
        payload["forecast"][0]["uv_index_max"] = 9.0
        marks = activity(payload)
        self.assertEqual(marks["umbrella"], "Need")
        self.assertEqual(marks["uv"], "Very High")

    def test_it_survives_an_empty_payload(self):
        # The page renders before the first fetch returns, and a rule that
        # raises there takes the whole card with it.
        marks = activity({})
        self.assertIn(marks["outdoor"], ("Go", "Caution", "Avoid"))
        self.assertEqual(marks["uv"], "")


class TestShouldGoOutside(unittest.TestCase):

    def test_a_good_day_says_so(self):
        advice = should_go_outside(weather())
        self.assertEqual(advice["verdict"], "yes")
        self.assertIn("Good weather", advice["headline"])
        self.assertIn("Temp: 68°F", advice["summary"])
        self.assertIn("Rain: 5%", advice["summary"])

    def test_rain_gets_an_umbrella_rather_than_a_refusal(self):
        advice = should_go_outside(weather(precipitation_chance=60,
                                           conditions="cloudy", code=3))
        self.assertEqual(advice["verdict"], "caution")
        self.assertIn("umbrella", advice["headline"].lower())
        self.assertIn("60%", advice["detail"])

    def test_a_storm_says_stay_in(self):
        advice = should_go_outside(weather(code=95, conditions="a thunderstorm",
                                           precipitation_chance=95))
        self.assertEqual(advice["verdict"], "no")
        self.assertIn("indoors", advice["headline"].lower())

    def test_strong_sun_is_mentioned_by_name(self):
        advice = should_go_outside(weather(uv_index=9.5))
        self.assertEqual(advice["verdict"], "caution")
        self.assertIn("sun", advice["headline"].lower())
        self.assertIn("10", advice["detail"])

    def test_every_verdict_carries_all_four_marks(self):
        # The card draws both halves from one call; a missing key there is a
        # blank line on the screen rather than an error anybody sees.
        for payload in (weather(), weather(code=95), weather(uv_index=None),
                        weather(precipitation_chance=None), {}):
            advice = should_go_outside(payload)
            for key in ("verdict", "headline", "detail", "summary",
                        "uv", "outdoor", "umbrella", "clothing"):
                self.assertIn(key, advice)

    def test_it_never_says_none_degrees(self):
        advice = should_go_outside({"unit": "F", "current": {}, "forecast": []})
        self.assertNotIn("None", advice["summary"])
        self.assertNotIn("None", advice["detail"])


if __name__ == "__main__":
    unittest.main()
