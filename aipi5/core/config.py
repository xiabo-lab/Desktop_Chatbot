"""AIPI5's settings, and how they sit on top of AIA's.

There are two configurations in this assistant and keeping them apart is the
point of this module.

**AIA's** (`aia/core/config.py`) owns everything about sound: which microphone,
what gain, how long silence has to last before an utterance has ended, which
recogniser, which Piper voices. Those are measurements, not preferences — the
file says what breaks if each one is changed and the numbers came off this
hardware. AIPI5 does not restate any of them and does not override them.

**This one** owns everything AIPI5 adds: a 1280x800 display, a location, an
OpenAI model, a camera, a person detector, a screensaver timeout. It is YAML
rather than Python because these are settings a person changes on a device,
often over ssh, and editing a dataclass to change a screensaver timeout is a
worse experience than editing a line of YAML.

The one place they meet is `aia_config()`, which takes AIA's `Config` and
adjusts the two fields that are genuinely AIPI5's business: the retention
window (this project makes the same 24-hour promise but from its own setting)
and AIA's own web UI, which is switched off because AIPI5 serves its own and
two servers reading the same conversation is one more than anybody needs.

API credentials are never here. `OPENAI_API_KEY` comes from the environment,
which is what the systemd unit and `.env` supply — see `credentials()`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from aipi5.core import aia_bridge  # noqa: F401  — puts AIA on sys.path

from aia.core.config import CONFIG as AIA_CONFIG, Config as AiaConfig

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "models"
DEFAULT_CONFIG = ROOT / "config" / "aipi5.yaml"


class ConfigError(ValueError):
    """The configuration file is wrong in a way that cannot be defaulted past.

    Raised at startup, where there is somebody watching, rather than left to
    surface as a tool that quietly never works. A missing OpenAI key is *not*
    one of these — that is a degraded mode, and `preflight` reports it.
    """


# ── the sections ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class DisplayConfig:
    width: int = 1280
    height: int = 800
    host: str = "127.0.0.1"
    port: int = 8092

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"


@dataclass(frozen=True)
class LocationConfig:
    city: str = "San Jose"
    state: str = "CA"
    zip: str = "95127"
    latitude: float = 37.3708
    longitude: float = -121.8163
    timezone: str = "America/Los_Angeles"
    units: str = "fahrenheit"

    @property
    def label(self) -> str:
        return f"{self.city}, {self.state} {self.zip}".strip()

    @property
    def temperature_unit(self) -> str:
        return "fahrenheit" if self.units.lower().startswith("f") else "celsius"

    @property
    def degree_symbol(self) -> str:
        return "F" if self.temperature_unit == "fahrenheit" else "C"


@dataclass(frozen=True)
class OpenAIConfig:
    # The cheap, fast tier of GPT-5.6. See config/aipi5.yaml for the pricing
    # this was chosen from; the short version is that the model sits on the
    # slow path, so what it costs is a monthly figure and what it takes is a
    # person standing there waiting.
    model: str = "gpt-5.6-luna"
    vision_model: str = ""
    timeout_s: float = 20.0
    vision_timeout_s: float = 30.0
    max_retries: int = 1
    context_turns: int = 8
    context_idle_s: float = 600.0
    max_output_tokens: int = 400

    @property
    def vision(self) -> str:
        """Which model sees pictures.

        Empty means the conversational one. Kept as a property rather than
        defaulted at load time so that changing `model` in the YAML also
        changes the vision model, which is what somebody editing one line
        expects.
        """
        return self.vision_model or self.model


@dataclass(frozen=True)
class WeatherConfig:
    provider: str = "open-meteo"
    cache_seconds: float = 600.0
    timeout_s: float = 8.0


@dataclass(frozen=True)
class NewsConfig:
    feeds: tuple[str, ...] = ()
    max_stories: int = 5
    cache_seconds: float = 900.0
    timeout_s: float = 8.0


@dataclass(frozen=True)
class StoryConfig:
    default_minutes: int = 4
    max_minutes: int = 10


@dataclass(frozen=True)
class CameraConfig:
    enabled: bool = True
    capture_width: int = 1280
    capture_height: int = 720
    jpeg_quality: int = 80
    scratch: Path = Path("/dev/shm/aipi5-camera")
    warmup_ms: int = 400


@dataclass(frozen=True)
class PersonDetectionConfig:
    enabled: bool = True
    backend: str = "hailo"
    model: Path = MODELS / "yolov8n.hef"
    cpu_model: Path = MODELS / "ssd_mobilenet_v1.onnx"
    confidence: float = 0.45
    interval_ms: int = 500
    frames_to_appear: int = 2
    frames_to_disappear: int = 8


@dataclass(frozen=True)
class ScreensaverConfig:
    enabled: bool = True
    timeout_seconds: float = 60.0


@dataclass(frozen=True)
class KodamaLaunchConfig:
    enabled: bool = True
    service: str = "kodama-lite.service"
    start_timeout_s: float = 20.0


@dataclass(frozen=True)
class AssistantConfig:
    llm_enabled: bool = True
    retention_hours: float = 24.0


@dataclass(frozen=True)
class Settings:
    display: DisplayConfig = field(default_factory=DisplayConfig)
    location: LocationConfig = field(default_factory=LocationConfig)
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    story: StoryConfig = field(default_factory=StoryConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    person: PersonDetectionConfig = field(default_factory=PersonDetectionConfig)
    screensaver: ScreensaverConfig = field(default_factory=ScreensaverConfig)
    kodama: KodamaLaunchConfig = field(default_factory=KodamaLaunchConfig)
    assistant: AssistantConfig = field(default_factory=AssistantConfig)

    #: Where this was loaded from, for the settings page.
    source: Path | None = None

    def aia_config(self) -> AiaConfig:
        """AIA's configuration, adjusted for running inside AIPI5.

        Two changes and no more.

        `ui.enabled` goes off because AIPI5 serves its own page, on its own
        port, from the same conversation database. AIA's would work — it is
        read-only and harmless — but it would be a second thing to find, at a
        second address, showing the same transcript in a layout designed for a
        1920x440 strip.

        `retention.hours` follows this project's setting so there is one answer
        to "how long is anything kept", rather than a promise made in two files
        that can be edited independently.

        Everything else is AIA's exactly as measured, including the paths —
        which means the SenseVoice model, the Vosk wake model and both Piper
        voices are read out of the AIA checkout and are not fetched again here.
        """
        return replace(
            AIA_CONFIG,
            ui=replace(AIA_CONFIG.ui, enabled=False),
            retention=replace(AIA_CONFIG.retention,
                              hours=self.assistant.retention_hours),
        )


# ── loading ──────────────────────────────────────────────────────────


def _require_mapping(value: Any, where: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where} should be a block of settings, not {type(value).__name__}")
    return value


def _path(value: Any, fallback: Path) -> Path:
    """A path from the YAML, resolved against the project root if relative.

    Relative rather than absolute is the normal case in the file — `models/
    yolov8n.hef` reads better than a home directory somebody else does not
    have — and resolving it here means nothing downstream has to know where
    the project lives.
    """
    if value in (None, ""):
        return fallback
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else (ROOT / path)


def _positive(value: Any, fallback: float, where: str) -> float:
    """A number that has to be above zero, or the default with a complaint.

    Zero is the interesting failure rather than a negative one: a
    `cache_seconds: 0` means every screensaver tick hits the weather API, and
    an `interval_ms: 0` is a busy loop on a device whose whole design is about
    leaving cores free. Both look like a plausible thing to type.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        log.warning("%s is not a number (%r); using %s", where, value, fallback)
        return fallback
    if number <= 0:
        log.warning("%s must be above zero (got %s); using %s", where, number, fallback)
        return fallback
    return number


def load(path: Path | str | None = None) -> Settings:
    """Read the configuration. A missing file is defaults, not an error.

    Defaults rather than a failure because every default in this module is the
    value the specification asks for — 1280x800, San Jose 95127, a 60 s
    screensaver timeout. A deployment that loses its YAML should come back up
    as the assistant it was, not refuse to start.

    A *malformed* file is a different matter and raises: that is somebody
    halfway through an edit, and starting with silently ignored settings is how
    a change appears not to have worked.
    """
    source = Path(path) if path else DEFAULT_CONFIG
    if not source.exists():
        log.warning("no configuration at %s; using built-in defaults", source)
        return Settings(news=NewsConfig(feeds=_DEFAULT_FEEDS))

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover — a broken install
        raise ConfigError(
            "PyYAML is not installed, so config/aipi5.yaml cannot be read. "
            "pip install -r requirements.txt"
        ) from exc

    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source} is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {source}: {exc}") from exc

    raw = _require_mapping(raw, str(source))
    return _from_mapping(raw, source)


def _from_mapping(raw: dict, source: Path | None) -> Settings:
    display = _require_mapping(raw.get("display"), "display")
    location = _require_mapping(raw.get("location"), "location")
    openai = _require_mapping(raw.get("openai"), "openai")
    weather = _require_mapping(raw.get("weather"), "weather")
    news = _require_mapping(raw.get("news"), "news")
    story = _require_mapping(raw.get("story"), "story")
    camera = _require_mapping(raw.get("camera"), "camera")
    person = _require_mapping(raw.get("person_detection"), "person_detection")
    screensaver = _require_mapping(raw.get("screensaver"), "screensaver")
    kodama = _require_mapping(raw.get("kodama"), "kodama")
    assistant = _require_mapping(raw.get("assistant"), "assistant")

    feeds = news.get("feeds") or list(_DEFAULT_FEEDS)
    if isinstance(feeds, str):
        feeds = [feeds]

    settings = Settings(
        display=DisplayConfig(
            width=int(display.get("width", 1280)),
            height=int(display.get("height", 800)),
            host=str(display.get("host", "127.0.0.1")),
            port=int(display.get("port", 8092)),
        ),
        location=LocationConfig(
            city=str(location.get("city", "San Jose")),
            state=str(location.get("state", "CA")),
            zip=str(location.get("zip", "95127")),
            latitude=float(location.get("latitude", 37.3708)),
            longitude=float(location.get("longitude", -121.8163)),
            timezone=str(location.get("timezone", "America/Los_Angeles")),
            units=str(location.get("units", "fahrenheit")),
        ),
        openai=OpenAIConfig(
            model=str(openai.get("model", "gpt-5.6-luna")),
            vision_model=str(openai.get("vision_model", "") or ""),
            timeout_s=_positive(openai.get("timeout_s", 20.0), 20.0, "openai.timeout_s"),
            vision_timeout_s=_positive(openai.get("vision_timeout_s", 30.0), 30.0,
                                       "openai.vision_timeout_s"),
            max_retries=max(0, int(openai.get("max_retries", 1))),
            context_turns=max(0, int(openai.get("context_turns", 8))),
            context_idle_s=_positive(openai.get("context_idle_s", 600.0), 600.0,
                                     "openai.context_idle_s"),
            max_output_tokens=max(1, int(openai.get("max_output_tokens", 400))),
        ),
        weather=WeatherConfig(
            provider=str(weather.get("provider", "open-meteo")),
            cache_seconds=_positive(weather.get("cache_seconds", 600.0), 600.0,
                                    "weather.cache_seconds"),
            timeout_s=_positive(weather.get("timeout_s", 8.0), 8.0, "weather.timeout_s"),
        ),
        news=NewsConfig(
            feeds=tuple(str(f) for f in feeds if str(f).strip()),
            max_stories=max(1, int(news.get("max_stories", 5))),
            cache_seconds=_positive(news.get("cache_seconds", 900.0), 900.0,
                                    "news.cache_seconds"),
            timeout_s=_positive(news.get("timeout_s", 8.0), 8.0, "news.timeout_s"),
        ),
        story=StoryConfig(
            default_minutes=max(1, int(story.get("default_minutes", 4))),
            max_minutes=max(1, int(story.get("max_minutes", 10))),
        ),
        camera=CameraConfig(
            enabled=bool(camera.get("enabled", True)),
            capture_width=int(camera.get("capture_width", 1280)),
            capture_height=int(camera.get("capture_height", 720)),
            jpeg_quality=max(1, min(95, int(camera.get("jpeg_quality", 80)))),
            scratch=_path(camera.get("scratch"), Path("/dev/shm/aipi5-camera")),
            warmup_ms=max(0, int(camera.get("warmup_ms", 400))),
        ),
        person=PersonDetectionConfig(
            enabled=bool(person.get("enabled", True)),
            backend=str(person.get("backend", "hailo")).strip().lower(),
            model=_path(person.get("model"), MODELS / "yolov8n.hef"),
            cpu_model=_path(person.get("cpu_model"), MODELS / "ssd_mobilenet_v1.onnx"),
            confidence=float(person.get("confidence", 0.45)),
            interval_ms=int(_positive(person.get("interval_ms", 500), 500, "interval_ms")),
            # Both floors are 1. A zero means "change state with no evidence at
            # all", which is exactly the flapping the debounce exists to stop.
            frames_to_appear=max(1, int(person.get("frames_to_appear", 2))),
            frames_to_disappear=max(1, int(person.get("frames_to_disappear", 8))),
        ),
        screensaver=ScreensaverConfig(
            enabled=bool(screensaver.get("enabled", True)),
            timeout_seconds=_positive(screensaver.get("timeout_seconds", 60.0), 60.0,
                                      "screensaver.timeout_seconds"),
        ),
        kodama=KodamaLaunchConfig(
            enabled=bool(kodama.get("enabled", True)),
            service=str(kodama.get("service", "kodama-lite.service")),
            start_timeout_s=_positive(kodama.get("start_timeout_s", 20.0), 20.0,
                                      "kodama.start_timeout_s"),
        ),
        assistant=AssistantConfig(
            llm_enabled=bool(assistant.get("llm_enabled", True)),
            retention_hours=_positive(assistant.get("retention_hours", 24.0), 24.0,
                                      "assistant.retention_hours"),
        ),
        source=source,
    )

    _check(settings)
    return settings


def _check(settings: Settings) -> None:
    """Invariants that span two sections, complained about once at startup."""
    # The specification is explicit that this display is 1280x800 and that the
    # old 1920x440 geometry must not be inherited. Not fatal — somebody may
    # genuinely have a different panel — but it is worth a line in the journal
    # rather than a UI that mysteriously does not fit.
    if (settings.display.width, settings.display.height) != (1280, 800):
        log.warning("display is configured as %dx%d; the UI is designed for 1280x800",
                    settings.display.width, settings.display.height)

    # A person who has just left has to stay gone for `frames_to_disappear`
    # frames before the timeout even starts. If that debounce is longer than
    # the timeout itself, the screensaver number in the file is not the delay
    # anybody experiences, and the difference is silent.
    debounce_s = settings.person.frames_to_disappear * settings.person.interval_ms / 1000
    if settings.screensaver.enabled and debounce_s > settings.screensaver.timeout_seconds:
        log.warning(
            "person_detection takes %.1fs to decide the room is empty, which is longer "
            "than the %.0fs screensaver timeout — the real delay is the sum, %.1fs",
            debounce_s, settings.screensaver.timeout_seconds,
            debounce_s + settings.screensaver.timeout_seconds)

    # The screensaver is the reason person detection exists on this device.
    # Asking for one without the other is legal and probably a mistake.
    if settings.screensaver.enabled and not settings.person.enabled:
        log.warning("the screensaver is enabled but person detection is not, so nothing "
                    "will ever bring it up or take it away")


# Used when there is no configuration file at all, so that a deployment which
# has lost its YAML still knows where San Jose's news comes from.
_DEFAULT_FEEDS = (
    "https://news.google.com/rss/search?q=San+Jose+OR+%22Santa+Clara+County%22"
    "+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://www.mercurynews.com/location/california/bay-area/santa-clara-county/feed/",
    "https://sanjosespotlight.com/feed/",
)


# Where a key file may live, if there is no `OPENAI_API_KEY` in the
# environment. Searched in this order; the first that exists and holds
# something wins.
#
# `openai API.txt` is here because that is what this deployment actually has —
# the key arrived as a file in the project directory. Naming it explicitly is
# better than the alternatives: a wildcard over `*.txt` would eventually read
# somebody's notes and try to authenticate with them, and pretending the file
# is not there would mean an assistant that cannot talk on a device where the
# credential is sitting in plain sight.
#
# Every one of these is in `.gitignore`. That is what keeps the specification's
# "never commit API credentials" true while still reading one from a file, and
# it is the part to check if this list is ever extended.
KEY_FILES = (
    "openai API.txt",
    "openai_api_key.txt",
    ".openai_key",
)

# What an OpenAI key looks like. Checked so that a file holding a shell export
# line, a JSON blob or somebody's note to themselves is rejected here — where
# the message can say which file was wrong — rather than at the API, where it
# comes back as a 401 that reads like the key has been revoked.
_KEY_PREFIXES = ("sk-",)


def _key_from_file() -> tuple[str, Path | None]:
    """A key read off disk, and which file it came from.

    Reads the first non-empty line and strips it, so a file with a trailing
    newline — which every editor writes — works, and so does one with a
    comment line above the key.
    """
    # An explicitly named file is the only one considered. Falling through to
    # the defaults after it would mean a deployment that pointed
    # OPENAI_API_KEY_FILE at the wrong path came up working anyway, on a key
    # from a file nobody had mentioned — which is exactly the sort of thing
    # that is discovered when the *other* key is rotated.
    explicit = os.environ.get("OPENAI_API_KEY_FILE", "").strip()
    if explicit:
        candidates = [Path(explicit).expanduser()]
    else:
        candidates = [ROOT / name for name in KEY_FILES]

    for path in candidates:
        try:
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                candidate = line.strip()
                if not candidate or candidate.startswith("#"):
                    continue
                if not candidate.startswith(_KEY_PREFIXES):
                    log.warning("%s does not look like an OpenAI key (it should "
                                "start with 'sk-'); ignoring it", path.name)
                    break
                return candidate, path
        except OSError as exc:
            log.warning("cannot read %s: %s", path, exc)
    return "", None


def credentials() -> str:
    """The OpenAI API key. Never from the YAML, never logged.

    The environment first — that is what the systemd unit supplies and it is
    the form that leaves no copy on disk — then a key file beside the project.

    Two rules hold whichever way it arrives. The value is never written to a
    log, an error message or the settings page; `describe_credentials()` exists
    so that "is there a key, and where did it come from" can be answered
    without the key itself appearing anywhere. And every filename it can be
    read from is in `.gitignore`, so a credential in the project directory is
    still a credential outside source control.

    An empty string is a valid answer and means degraded mode: every Kodama
    command, the weather, the clock and the screensaver still work, and
    `preflight` says plainly in the journal that conversation does not.
    """
    from_env = os.environ.get("OPENAI_API_KEY", "").strip()
    if from_env:
        return from_env
    return _key_from_file()[0]


def describe_credentials() -> dict:
    """Whether there is a key and where it came from — never what it is.

    The settings page and the startup log both want to say something about the
    credential, and neither may say the credential. `suffix` is the last four
    characters, which is the amount every provider's own dashboard shows: it is
    enough to tell two keys apart when swapping them and not enough to use.
    """
    from_env = os.environ.get("OPENAI_API_KEY", "").strip()
    if from_env:
        return {"present": True, "source": "OPENAI_API_KEY", "suffix": from_env[-4:]}
    key, path = _key_from_file()
    if key:
        return {"present": True, "source": path.name if path else "file",
                "suffix": key[-4:]}
    return {"present": False, "source": None, "suffix": None}
