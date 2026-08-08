"""Current weather and a short forecast for one fixed place.

Open-Meteo, because of what it does not need: no API key, no account, no quota
to exhaust, and no billing relationship to keep alive on a device that is meant
to sit in a room for years. It is also the only kind of provider that can be
configured entirely in a YAML file with no secret beside it.

**Live data, never the model's memory.** Section 15 of the specification is
explicit, and the reason is worth stating: a language model asked what the
weather is will answer, fluently, with the climate. The model in this assistant
is never asked. It is handed the numbers this module fetched and asked to read
them out.

**Cached, because the screensaver asks constantly.** The clock on the
screensaver redraws every second and shares a panel with the temperature. A
naive implementation fetches the weather every time it draws, which is 86,400
requests a day for a number that changes four times an hour. Ten minutes of
cache turns that into 144 and costs nobody anything they can perceive.

Failure is a `None` and a log line, never an exception into the voice loop. The
assistant says it cannot reach the weather right now, which is true and
actionable, and the rest of the turn is unaffected.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

ENDPOINT = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes, which is what Open-Meteo returns instead of
# a description. The table is here rather than fetched because it is a fixed
# part of the standard, and the phrasing is chosen for being *said* — "light
# rain" rather than "Rain: Slight intensity", which is accurate and reads aloud
# like a database row.
WMO = {
    0: ("clear", "晴"),
    1: ("mostly clear", "大部晴朗"),
    2: ("partly cloudy", "多云"),
    3: ("overcast", "阴天"),
    45: ("foggy", "有雾"),
    48: ("freezing fog", "冻雾"),
    51: ("light drizzle", "小毛毛雨"),
    53: ("drizzle", "毛毛雨"),
    55: ("heavy drizzle", "大毛毛雨"),
    56: ("freezing drizzle", "冻毛毛雨"),
    57: ("heavy freezing drizzle", "强冻毛毛雨"),
    61: ("light rain", "小雨"),
    63: ("rain", "中雨"),
    65: ("heavy rain", "大雨"),
    66: ("freezing rain", "冻雨"),
    67: ("heavy freezing rain", "强冻雨"),
    71: ("light snow", "小雪"),
    73: ("snow", "中雪"),
    75: ("heavy snow", "大雪"),
    77: ("snow grains", "米雪"),
    80: ("light showers", "阵雨"),
    81: ("showers", "中阵雨"),
    82: ("heavy showers", "大阵雨"),
    85: ("light snow showers", "小阵雪"),
    86: ("snow showers", "阵雪"),
    95: ("a thunderstorm", "雷阵雨"),
    96: ("a thunderstorm with hail", "雷阵雨伴冰雹"),
    99: ("a severe thunderstorm with hail", "强雷阵雨伴冰雹"),
}


def describe_code(code: int | None, language: str = "en") -> str:
    """The WMO code as something worth hearing, in either language."""
    if code is None:
        return "unclear" if language == "en" else "不明"
    english, chinese = WMO.get(int(code), ("unsettled", "天气多变"))
    return english if language == "en" else chinese


@dataclass(frozen=True)
class Conditions:
    """Right now, at one place."""

    temperature: float
    feels_like: float
    humidity: int
    wind_mph: float
    code: int | None
    is_day: bool
    unit: str  # "F" or "C"

    def describe(self, language: str = "en") -> str:
        sky = describe_code(self.code, language)
        if language == "zh":
            return (f"现在{sky}，气温{self.temperature:.0f}度，"
                    f"体感{self.feels_like:.0f}度，湿度百分之{self.humidity}。")
        return (f"It's {sky}, {self.temperature:.0f} degrees, "
                f"feels like {self.feels_like:.0f}, humidity {self.humidity} percent.")


@dataclass(frozen=True)
class DayForecast:
    date: str          # ISO, as the API returns it
    high: float
    low: float
    code: int | None
    precipitation_chance: int

    def describe(self, language: str = "en") -> str:
        sky = describe_code(self.code, language)
        if language == "zh":
            return f"{sky}，最高{self.high:.0f}度，最低{self.low:.0f}度。"
        return f"{sky}, high {self.high:.0f}, low {self.low:.0f}."


@dataclass(frozen=True)
class Weather:
    """One fetch: what it is now and what the next few days hold."""

    place: str
    now: Conditions
    days: tuple[DayForecast, ...]
    fetched_at: float

    def summary(self, language: str = "en") -> str:
        """One or two sentences, for speech.

        Today's high and low are included because "what's the weather" almost
        always means "what am I in for", and the current temperature alone
        answers a narrower question than the one that was asked.
        """
        parts = [self.now.describe(language)]
        if self.days:
            today = self.days[0]
            if language == "zh":
                parts.append(f"今天最高{today.high:.0f}度，最低{today.low:.0f}度。")
            else:
                parts.append(f"Today's high is {today.high:.0f} "
                             f"and the low is {today.low:.0f}.")
        return " ".join(parts)

    def as_dict(self) -> dict:
        """The shape the model and the UI both receive.

        One representation, not two. The screensaver draws from the same
        dictionary the model is handed, so the temperature on the screen and
        the temperature in the spoken answer cannot disagree.
        """
        return {
            "place": self.place,
            "unit": self.now.unit,
            "current": {
                "temperature": round(self.now.temperature),
                "feels_like": round(self.now.feels_like),
                "humidity": self.now.humidity,
                "wind_mph": round(self.now.wind_mph),
                "conditions": describe_code(self.now.code, "en"),
                "conditions_zh": describe_code(self.now.code, "zh"),
                "code": self.now.code,
                "is_day": self.now.is_day,
            },
            "forecast": [
                {
                    "date": day.date,
                    "high": round(day.high),
                    "low": round(day.low),
                    "conditions": describe_code(day.code, "en"),
                    "conditions_zh": describe_code(day.code, "zh"),
                    "precipitation_chance": day.precipitation_chance,
                }
                for day in self.days
            ],
            "fetched_at": self.fetched_at,
        }


class WeatherService:
    """Fetches, caches, and never raises at the caller.

    The lock is not about correctness of the cache — a duplicate fetch would be
    harmless — but about the screensaver and a spoken question arriving at the
    same moment on different threads. Without it, walking up to the device and
    asking about the weather can fire two requests where one would do, at
    exactly the moment the device is busiest.
    """

    def __init__(self, location, cfg, session: requests.Session | None = None):
        self.location = location
        self.cfg = cfg
        # A session, opened once, for the same reason the OpenAI client is
        # built once: TLS handshakes are the expensive part of a small HTTPS
        # request and a new connection per call pays for one every time.
        self._session = session or requests.Session()
        self._lock = threading.Lock()
        self._cached: Weather | None = None

    def current(self, force: bool = False) -> Weather | None:
        """The weather, from cache when it is fresh enough.

        `force` is for the one case that genuinely wants a round trip: somebody
        has just asked out loud. Even then the answer is only a few minutes
        newer, so it is not the default.
        """
        with self._lock:
            cached = self._cached
            if cached is not None and not force:
                age = time.time() - cached.fetched_at
                if age < self.cfg.cache_seconds:
                    log.debug("weather from cache (%.0fs old)", age)
                    return cached

            fetched = self._fetch()
            if fetched is not None:
                self._cached = fetched
                return fetched

            # A failed refresh must not throw away a good answer from four
            # minutes ago. Stale weather is worth far more than no weather —
            # the screensaver has to draw something, and "it was 68 a few
            # minutes ago" is not wrong in any way that matters.
            if cached is not None:
                log.warning("weather fetch failed; keeping the cached reading "
                            "(%.0fs old)", time.time() - cached.fetched_at)
            return cached

    def _fetch(self) -> Weather | None:
        params = {
            "latitude": self.location.latitude,
            "longitude": self.location.longitude,
            "current": ("temperature_2m,relative_humidity_2m,apparent_temperature,"
                        "is_day,weather_code,wind_speed_10m"),
            "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                      "precipitation_probability_max"),
            "temperature_unit": self.location.temperature_unit,
            "wind_speed_unit": "mph",
            "timezone": self.location.timezone,
            "forecast_days": 4,
        }
        try:
            resp = self._session.get(ENDPOINT, params=params,
                                     timeout=self.cfg.timeout_s)
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as exc:
            log.warning("weather request failed: %s", exc)
            return None
        except ValueError as exc:
            log.warning("weather response was not JSON: %s", exc)
            return None

        try:
            return self._parse(payload)
        except (KeyError, TypeError, ValueError) as exc:
            # A provider that changed its response shape. Worth an exception
            # trace in the journal, because the fix is a code change and the
            # only clue anybody gets otherwise is a screensaver with no
            # temperature on it.
            log.exception("could not read the weather response: %s", exc)
            return None

    def _parse(self, payload: dict) -> Weather:
        current = payload["current"]
        daily = payload.get("daily", {})
        unit = self.location.degree_symbol

        days = []
        for index, date in enumerate(daily.get("time", [])):
            days.append(DayForecast(
                date=str(date),
                high=float(daily["temperature_2m_max"][index]),
                low=float(daily["temperature_2m_min"][index]),
                code=_int_or_none(daily.get("weather_code", [None] * (index + 1))[index]),
                precipitation_chance=int(
                    daily.get("precipitation_probability_max",
                              [0] * (index + 1))[index] or 0),
            ))

        return Weather(
            place=self.location.label,
            now=Conditions(
                temperature=float(current["temperature_2m"]),
                feels_like=float(current.get("apparent_temperature",
                                             current["temperature_2m"])),
                humidity=int(current.get("relative_humidity_2m", 0)),
                wind_mph=float(current.get("wind_speed_10m", 0.0)),
                code=_int_or_none(current.get("weather_code")),
                is_day=bool(current.get("is_day", 1)),
                unit=unit,
            ),
            days=tuple(days),
            fetched_at=time.time(),
        )

    def close(self) -> None:
        self._session.close()


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
