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
    #: A V4L2 node, an index, or "auto" to find the webcam by name.
    device: str = "auto"
    #: What that search matches on. The camera is a Logitech Brio 101.
    name_hint: str = "Brio"
    capture_width: int = 1280
    capture_height: int = 720
    fps: int = 30
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
    """When the screen goes away, and what is on it when it has.

    The two schedule fields are strings rather than parsed times because this
    is the file a person edits over ssh, and `"21:01"` is what they mean.
    `aipi5/screensaver/schedule.py` turns them into minutes and is the only
    place that knows how — section 22 asks for the schedule to live in one
    place in the configuration so it can be changed without touching the
    screensaver logic, and this is that place.
    """

    enabled: bool = True
    timeout_seconds: float = 60.0
    #: The specification's schedule. Inclusive start, and the night begins at
    #: 21:01 because section 24 puts 21:00 itself in the day.
    day_start: str = "07:00"
    night_start: str = "21:01"
    #: `photos` or `clock`. The day falls back to the clock on its own when
    #: there are no photographs to show, so this is for a household that wants
    #: the clock all day on purpose rather than for one with no Google account.
    day_mode: str = "photos"
    night_mode: str = "weather"


@dataclass(frozen=True)
class PhotosConfig:
    """The daytime slideshow, and the Google account behind it.

    **Nothing secret is in this dataclass or in the YAML that fills it.** The
    OAuth client and the refresh token live in two files under
    `~/.config/aipi5`, outside the repository, written 0600 — the same
    arrangement, and for the same reasons, as the video call's tokens. What is
    here is only where to look.
    """

    enabled: bool = True
    #: How long each photograph is up, before the crossfade. Section 11's
    #: 10–15 seconds; 15 is the specification's example default.
    interval_seconds: float = 15.0
    #: The crossfade itself. GPU-friendly opacity only — see the page.
    transition_ms: int = 1000
    shuffle: bool = True
    #: Two ceilings, and whichever is reached first wins. A count is what a
    #: person can reason about ("about three hundred pictures") and a byte
    #: total is what actually protects the SD card, since one 12-megapixel
    #: photograph is not the same size as another.
    max_photos: int = 300
    max_cache_mb: int = 512
    #: And a floor, which is the more important of the three. Cache cleanup may
    #: never take the running slideshow below this many photographs — a limit
    #: lowered in the YAML must not be able to empty the set somebody chose.
    #: Only replacing the selection or disconnecting the account does that, and
    #: both are things a person asked for.
    #:
    #: Deliberately **not** clamped to `max_photos`: a floor that shrank to
    #: match a lowered ceiling would protect nothing in exactly the case it is
    #: for. When the two conflict the floor wins, the cache stays above its
    #: ceiling, and both `_check` and the cache say so.
    min_photos: int = 50
    #: How often the sync thread looks for work. Bounded far below anything
    #: Google would object to: this is a photo frame, not a backup client.
    sync_minutes: float = 60.0
    #: What the download asks Google for. `w1600-h1000` is the 1280x800 panel
    #: with room for the blurred backdrop and for a display that is scaled —
    #: section 29 asks for enough and not more. Aspect ratio is preserved by
    #: the API; this is a bounding box, not a crop.
    download_size: str = "w1600-h1000"
    #: The cache. Under `~/.cache` rather than in the repository or in
    #: `~/.config`: these are re-downloadable copies of somebody else's
    #: photographs, and losing the lot costs a sync.
    cache_dir: Path = Path.home() / ".cache" / "aipi5" / "photos"
    #: The OAuth client — a *client id and secret for an installed app*, which
    #: Google's own documentation describes as not confidential, but which is
    #: still not ours to publish. Downloaded from the Cloud console and put
    #: here by hand; see scripts/link-google-photos.sh.
    client_file: Path = Path.home() / ".config" / "aipi5" / "google-photos-client.json"
    #: The refresh token, written by that script and read by nothing else.
    token_file: Path = Path.home() / ".config" / "aipi5" / "google-photos-token.json"
    #: The small date/album caption, off by default. Section 13 is explicit
    #: that it must not be mandatory and must not cover the photograph.
    show_info: bool = False


@dataclass(frozen=True)
class KodamaLaunchConfig:
    enabled: bool = True
    service: str = "kodama-lite.service"
    start_timeout_s: float = 20.0


@dataclass(frozen=True)
class CallConfig:
    """The remote video call. Off by default, and that is deliberate.

    Every other feature here fails towards being useless; this one fails
    towards a camera and a microphone reachable from the network. A deployment
    that has not been configured for calling should not be listening, so the
    default is `enabled: false` and the server additionally refuses to start
    with no phone paired.
    """

    enabled: bool = False
    #: `0.0.0.0` because the phone is by definition not on this machine. This
    #: is the one listener in the project that is not loopback, which is why
    #: `aipi5/call/server.py` authenticates every route.
    #:
    #: Set to `127.0.0.1` with `tls: false` when something else is terminating
    #: TLS and proxying in — `tailscale serve` is the arrangement this was
    #: built for, and it is strictly safer: nothing accepts a connection from
    #: off this machine at all.
    host: str = "0.0.0.0"
    port: int = 8443
    #: Whether this server terminates TLS itself. Turning it off is refused
    #: unless `host` is loopback: a bearer token over plaintext on a shared
    #: network is a bearer token somebody else can read.
    tls: bool = True
    #: Written on first start if absent, and never committed — both are under
    #: ~/.config, which is outside the repository. See `aipi5/call/tls.py`.
    certificate: Path = Path.home() / ".config" / "aipi5" / "call-cert.pem"
    private_key: Path = Path.home() / ".config" / "aipi5" / "call-key.pem"
    #: The paired phones. Hashes only; see `aipi5/call/tokens.py`.
    devices: Path = Path.home() / ".config" / "aipi5" / "call-devices.json"
    #: Empty on a LAN. Phase 3 of the procedure fills these in.
    stun_servers: tuple[str, ...] = ()
    turn_servers: tuple[dict, ...] = ()
    #: What the Brio is asked for during a call. 1280x720 at 30 needs MJPEG on
    #: this camera — YUYV at 720p is capped at 5 fps by USB bandwidth, measured
    #: on the device. Chromium picks the format, and asking for 30 fps at 720p
    #: is what makes it pick MJPEG.
    width: int = 1280
    height: int = 720
    fps: int = 30


def _max_upload_bytes() -> int:
    """The upload ceiling, in bytes, honouring `AIPI5_FILE_MAX_UPLOAD_GB`.

    A default factory rather than a constant so the environment is read when
    the settings are built — including on the path where there is no YAML at
    all, which is the one a fresh device takes.
    """
    raw = os.environ.get("AIPI5_FILE_MAX_UPLOAD_GB", "").strip()
    if raw:
        try:
            gigabytes = float(raw)
            if gigabytes > 0:
                return int(gigabytes * 1024 ** 3)
            log.warning("AIPI5_FILE_MAX_UPLOAD_GB=%r is not positive; ignoring", raw)
        except ValueError:
            log.warning("AIPI5_FILE_MAX_UPLOAD_GB=%r is not a number; ignoring", raw)
    return 2 * 1024 ** 3


@dataclass(frozen=True)
class FilesConfig:
    """The folder the phone can put things in, and the limits around it.

    **Outside the repository, deliberately.** Uploads are somebody's photos and
    videos, not source, and a transfer directory inside a git checkout is one
    `git status` away from being confusing and one `git clean` away from being
    gone.
    """

    enabled: bool = True
    #: `~/Downloads/AIPI5`, expanded when the store is built so the same
    #: configuration works for whichever user the service runs as.
    root: Path = Path.home() / "Downloads" / "AIPI5"
    max_upload_bytes: int = field(default_factory=_max_upload_bytes)
    #: Never fill the root filesystem. The assistant writes a conversation
    #: database, Chromium writes a profile, and systemd writes a journal —
    #: none of which fail gracefully at zero bytes free.
    reserve_bytes: int = 2 * 1024 ** 3
    #: The Pi has four cores and one uplink; two at once is already generous.
    max_concurrent: int = 2


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
    photos: PhotosConfig = field(default_factory=PhotosConfig)
    kodama: KodamaLaunchConfig = field(default_factory=KodamaLaunchConfig)
    call: CallConfig = field(default_factory=CallConfig)
    files: FilesConfig = field(default_factory=FilesConfig)
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


def _secret_path(value: Any, name: str) -> Path:
    """A path for something that must never land in the repository.

    Deliberately *not* `_path`. That one resolves a relative setting against
    the project root, which is right for a model file and exactly wrong for a
    private key and a device store — a relative `call-key.pem` in the YAML
    would otherwise put a TLS private key inside a git checkout. Relative
    values here resolve under `~/.config/aipi5` instead, and a test asserts
    that the defaults are outside the tree.
    """
    base = Path.home() / ".config" / "aipi5"
    if value in (None, ""):
        return base / name
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else (base / path)


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


def _one_of(value: Any, allowed: tuple[str, ...], fallback: str,
            where: str) -> str:
    """A setting from a fixed list, lower-cased, or the default with a warning.

    Not an exception, for the same reason a missing file is not one: a typo in
    `day_mode` should leave a device showing the specification's screensaver
    and a line saying so, rather than an assistant that will not start.
    """
    text = str(value or "").strip().lower()
    if text in allowed:
        return text
    log.warning("%s is %r, which is not one of %s; using %s",
                where, value, ", ".join(allowed), fallback)
    return fallback


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
    photos = _require_mapping(raw.get("photos"), "photos")
    kodama = _require_mapping(raw.get("kodama"), "kodama")
    call = _require_mapping(raw.get("call"), "call")
    files = _require_mapping(raw.get("files"), "files")
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
            device=str(camera.get("device", "auto") or "auto"),
            name_hint=str(camera.get("name_hint", "Brio") or ""),
            capture_width=int(camera.get("capture_width", 1280)),
            capture_height=int(camera.get("capture_height", 720)),
            fps=int(_positive(camera.get("fps", 30), 30, "camera.fps")),
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
            # Not validated here. `ScheduleManager` parses these and falls back
            # to the specification's times with a warning if they are not
            # HH:MM, which keeps one module responsible for what a time means.
            day_start=str(screensaver.get("day_start", "07:00")),
            night_start=str(screensaver.get("night_start", "21:01")),
            day_mode=_one_of(screensaver.get("day_mode", "photos"),
                             ("photos", "clock"), "photos",
                             "screensaver.day_mode"),
            night_mode=_one_of(screensaver.get("night_mode", "weather"),
                               ("weather", "clock", "photos"), "weather",
                               "screensaver.night_mode"),
        ),
        photos=PhotosConfig(
            enabled=bool(photos.get("enabled", True)),
            interval_seconds=_positive(photos.get("interval_seconds", 15.0), 15.0,
                                       "photos.interval_seconds"),
            transition_ms=int(_positive(photos.get("transition_ms", 1000), 1000,
                                        "photos.transition_ms")),
            shuffle=bool(photos.get("shuffle", True)),
            max_photos=int(_positive(photos.get("max_photos", 300), 300,
                                     "photos.max_photos")),
            max_cache_mb=int(_positive(photos.get("max_cache_mb", 512), 512,
                                       "photos.max_cache_mb")),
            # **Taken as written, never clamped to `max_photos`.** A floor
            # that quietly shrank to match a lowered ceiling would protect
            # nothing in the one situation it exists for. `_check` warns when
            # the two conflict; the floor wins and the cache stays over.
            min_photos=int(_positive(photos.get("min_photos", 50), 50,
                                     "photos.min_photos")),
            sync_minutes=_positive(photos.get("sync_minutes", 60.0), 60.0,
                                   "photos.sync_minutes"),
            download_size=str(photos.get("download_size", "w1600-h1000")),
            cache_dir=(Path(str(photos["cache_dir"])).expanduser()
                       if photos.get("cache_dir")
                       else Path.home() / ".cache" / "aipi5" / "photos"),
            # `_secret_path`, not `_path`: a relative name here must land in
            # ~/.config/aipi5 and never inside the checkout. These two files
            # are an OAuth client and a refresh token.
            client_file=_secret_path(photos.get("client_file"),
                                     "google-photos-client.json"),
            token_file=_secret_path(photos.get("token_file"),
                                    "google-photos-token.json"),
            show_info=bool(photos.get("show_info", False)),
        ),
        kodama=KodamaLaunchConfig(
            enabled=bool(kodama.get("enabled", True)),
            service=str(kodama.get("service", "kodama-lite.service")),
            start_timeout_s=_positive(kodama.get("start_timeout_s", 20.0), 20.0,
                                      "kodama.start_timeout_s"),
        ),
        call=CallConfig(
            enabled=bool(call.get("enabled", False)),
            host=str(call.get("host", "0.0.0.0")),
            port=int(call.get("port", 8443)),
            tls=bool(call.get("tls", True)),
            certificate=_secret_path(call.get("certificate"), "call-cert.pem"),
            private_key=_secret_path(call.get("private_key"), "call-key.pem"),
            devices=_secret_path(call.get("devices"), "call-devices.json"),
            stun_servers=tuple(str(s) for s in (call.get("stun_servers") or ())),
            turn_servers=tuple(t for t in (call.get("turn_servers") or ())
                               if isinstance(t, dict)),
            width=int(call.get("width", 1280)),
            height=int(call.get("height", 720)),
            fps=int(call.get("fps", 30)),
        ),
        files=FilesConfig(
            enabled=bool(files.get("enabled", True)),
            root=(Path(str(files["root"])).expanduser() if files.get("root")
                  else Path.home() / "Downloads" / "AIPI5"),
            # The environment wins over the file here, which is the opposite of
            # everywhere else in this module and is what the variable is for:
            # a one-off large transfer without editing YAML on a device with no
            # keyboard. Absent, `_max_upload_bytes` returns the default and the
            # file has its usual say.
            max_upload_bytes=(_max_upload_bytes()
                              if os.environ.get("AIPI5_FILE_MAX_UPLOAD_GB", "").strip()
                              else int(_positive(files.get("max_upload_gb", 2.0), 2.0,
                                                 "files.max_upload_gb") * 1024 ** 3)),
            reserve_bytes=int(_positive(files.get("reserve_gb", 2.0), 2.0,
                                        "files.reserve_gb") * 1024 ** 3),
            max_concurrent=max(1, int(files.get("max_concurrent", 2))),
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

    # The slideshow cannot run without a Google account, and the failure is
    # otherwise invisible: the day screensaver simply shows the clock, which
    # is also what a working device does at night. Said once at boot rather
    # than left for somebody to notice.
    # A floor above the ceiling is legal — the floor wins, deliberately — but
    # it is much more likely to be somebody having edited one and forgotten
    # the other, and the consequence is a cache that never comes down to the
    # size they asked for.
    if settings.photos.min_photos > settings.photos.max_photos:
        log.warning(
            "photos.min_photos (%d) is above photos.max_photos (%d). The floor "
            "wins — cleanup will not take the slideshow below %d photos — so "
            "the cache will sit above its own limit.",
            settings.photos.min_photos, settings.photos.max_photos,
            settings.photos.min_photos)

    if settings.screensaver.day_mode == "photos" and settings.photos.enabled \
            and not settings.photos.token_file.exists():
        log.info("no Google Photos authorisation at %s, so the daytime "
                 "screensaver will show the clock until ./scripts/"
                 "link-google-photos.sh has been run",
                 settings.photos.token_file)


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
