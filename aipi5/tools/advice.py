"""Whether to go outside, decided from the weather rather than by a model.

The weather page shows four judgements — UV, outdoor, umbrella, clothing — and
one sentence about whether to go out. Every one of them is a rule here.

**No model is asked.** Not because a model would answer badly, but because the
page must render the moment it opens, on a device that may be on a hotspot, and
an answer that arrives two seconds later is an answer that arrives after the
person has looked away. The rules also have the property a model does not: the
same weather always produces the same advice, so nobody has to wonder whether
the umbrella line changed because the forecast did.

Thresholds are the ones a person actually changes plans over, and they are
stated once, here, rather than scattered between a page and a prompt: 40% is
where rain stops being a rumour; 6 is where the WHO's UV scale turns from
moderate to high; 15 °C is roughly where a jacket stops being optional.

Everything works in Celsius internally and takes the unit it was given, because
the provider is configured in whichever unit the household reads and the rules
are not.
"""

from __future__ import annotations

#: WMO codes that mean rain is falling, in ascending seriousness.
RAINING = frozenset({51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82})
SNOWING = frozenset({71, 73, 75, 77, 85, 86})
STORMING = frozenset({95, 96, 99})
#: The subset of those that are worth staying in for.
SEVERE = frozenset({65, 67, 75, 82, 86, 95, 96, 99})

#: The WHO scale, which is what "is this a lot of UV" means.
UV_BANDS = ((3, "Low"), (6, "Moderate"), (8, "High"), (11, "Very High"))


def to_celsius(temperature: float, unit: str) -> float:
    """A temperature in whichever unit, as Celsius."""
    if (unit or "").upper().startswith("F"):
        return (temperature - 32.0) * 5.0 / 9.0
    return float(temperature)


def uv_band(uv: float | None) -> str:
    """`Low` … `Extreme`, or an empty string when there is no reading.

    Empty rather than `Low`: a missing number and a night-time zero are
    different facts, and only one of them means it is safe to stand outside at
    noon.
    """
    if uv is None:
        return ""
    for ceiling, name in UV_BANDS:
        if uv < ceiling:
            return name
    return "Extreme"


def umbrella(precipitation_chance: int | None, code: int | None) -> str:
    """`No`, `Optional`, `Recommended` or `Need`.

    Rain that is already falling outranks any probability: a 20% chance while
    it rains is a forecast losing an argument with the window.
    """
    if code in STORMING or code in RAINING:
        return "Need"
    chance = int(precipitation_chance or 0)
    if chance >= 70:
        return "Need"
    if chance >= 40:
        return "Recommended"
    if chance >= 20:
        return "Optional"
    return "No"


def clothing(feels_like_c: float, code: int | None, wind_mph: float = 0.0) -> str:
    """What to wear: `Rain Gear`, `Warm`, `Medium` or `Light`.

    Driven by the *feels-like* temperature, which is the one that decides
    whether somebody is cold — 10 °C in a 20 mph wind and 10 °C in still air
    are not the same errand.
    """
    if code in STORMING or code in RAINING or code in SNOWING:
        return "Rain Gear"
    if feels_like_c < 8:
        return "Warm"
    if feels_like_c < 17:
        return "Medium"
    return "Light"


def outdoor(code: int | None, precipitation_chance: int | None,
            feels_like_c: float, uv: float | None) -> str:
    """`Go`, `Caution` or `Avoid`, in the words the card has room for."""
    if code in SEVERE:
        return "Avoid"
    if feels_like_c <= -5 or feels_like_c >= 38:
        return "Avoid"
    if code in RAINING or code in SNOWING:
        return "Caution"
    if int(precipitation_chance or 0) >= 60:
        return "Caution"
    if uv is not None and uv >= 8:
        return "Caution"
    if feels_like_c <= 2 or feels_like_c >= 33:
        return "Caution"
    return "Go"


def activity(weather: dict) -> dict:
    """The four lines of the activity card, from one weather payload."""
    current = (weather or {}).get("current") or {}
    unit = (weather or {}).get("unit") or "F"
    today = ((weather or {}).get("forecast") or [{}])[0]

    feels_c = to_celsius(current.get("feels_like",
                                     current.get("temperature", 0)), unit)
    # The day's maximum where the hour has no number of its own: an afternoon
    # of rain is worth an umbrella at ten in the morning.
    chance = current.get("precipitation_chance")
    if chance is None:
        chance = today.get("precipitation_chance", 0)
    uv = current.get("uv_index")
    if uv is None:
        uv = today.get("uv_index_max")
    code = current.get("code")

    return {
        "uv": uv_band(uv),
        "outdoor": outdoor(code, chance, feels_c, uv),
        "umbrella": umbrella(chance, code),
        "clothing": clothing(feels_c, code, current.get("wind_mph", 0.0)),
    }


def should_go_outside(weather: dict) -> dict:
    """The advisory card: a verdict, a headline, and a sentence or two.

    Short on purpose. The card is read at a glance from across a room, and a
    paragraph there is a paragraph nobody finishes.
    """
    current = (weather or {}).get("current") or {}
    unit = (weather or {}).get("unit") or "F"
    today = ((weather or {}).get("forecast") or [{}])[0]
    marks = activity(weather)

    code = current.get("code")
    temperature = current.get("temperature")
    conditions = current.get("conditions") or "unsettled"
    chance = current.get("precipitation_chance")
    if chance is None:
        chance = today.get("precipitation_chance", 0)
    feels_c = to_celsius(current.get("feels_like", temperature or 0), unit)
    uv = current.get("uv_index")

    summary = "Temp: {}°{} | {} | Rain: {}%".format(
        "--" if temperature is None else round(temperature),
        unit, conditions.title(), int(chance or 0))

    if marks["outdoor"] == "Avoid":
        verdict = "no"
        headline = ("Severe weather — better indoors." if code in SEVERE
                    else "Too extreme out there right now.")
        detail = ("It is {} and the temperature feels like {}°{}. "
                  "Wait it out if you can.").format(
            conditions, round(current.get("feels_like", temperature or 0)), unit)
    elif marks["umbrella"] in ("Need", "Recommended"):
        verdict = "caution"
        headline = "Yes, but take an umbrella."
        detail = ("There is a {}% chance of rain and it is {}. "
                  "Nothing that should stop you, with a coat.").format(
            int(chance or 0), conditions)
    elif marks["outdoor"] == "Caution":
        verdict = "caution"
        if uv is not None and uv >= 8:
            headline = "Yes — but the sun is strong."
            detail = ("The UV index is {}. Sunscreen and a hat if you are out "
                      "for long.").format(round(uv))
        else:
            headline = "Yes, with a little care."
            detail = ("It is {} and feels like {}°{}. Dress for it and you "
                      "will be fine.").format(
                conditions, round(current.get("feels_like", temperature or 0)), unit)
    else:
        verdict = "yes"
        headline = "Good weather for going outside."
        detail = ("{} and {}°{}, with only a {}% chance of rain. "
                  "Nothing to plan around.").format(
            conditions.capitalize(), "--" if temperature is None
            else round(temperature), unit, int(chance or 0))

    return {"verdict": verdict, "headline": headline, "detail": detail,
            "summary": summary, **marks}
