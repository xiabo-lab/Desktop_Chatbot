"""The AI HAT+ 2, which there is exactly one of and which two things want.

**A Hailo-10H permits one `VDevice`. Not one per process — one.** Measured on
this device, and it is the fact the whole module exists for:

    second VDevice, second process   HAILO_OUT_OF_PHYSICAL_DEVICES (74)
    second VDevice, same process     HAILO_OUT_OF_PHYSICAL_DEVICES (74)
    one VDevice, two InferModels     person 28 ms, pose 32 ms, both fine

The middle row is the one that catches people. `HailoSchedulingAlgorithm.
ROUND_ROBIN` reads like it makes the accelerator shareable, and it does — but
it timeshares *models configured on one virtual device*, not virtual devices.
Two `VDevice(params)` calls with round-robin set on both fail exactly as
readily as two without it.

So the device is opened once, here, and handed out. Two callers today:

    aipi5/vision/person_detection.py    for the whole life of the process
    aipi5/motion/hailo_pose.py          while an AI Motion game is open

Reference counted rather than opened at startup and never closed, because a Pi
with `person_detection.backend: disabled` and nobody playing a game should not
be holding an accelerator open — and because section 30 requires a game to give
back what it took, which is only meaningful if the count can reach zero.

The counterpart to this is that **nothing else on the machine may open the
device while the assistant is running**. That was already true and is why
`scripts/probe_pose.py` says to stop the service first; it is now true for a
documented reason rather than by accident.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

_lock = threading.Lock()
_vdevice = None
_holders = 0
#: Why the last attempt failed, for the settings page and the game's error
#: screen. Kept after a failure so the reason survives the call that produced it.
_error = ""


class AcceleratorUnavailable(RuntimeError):
    """No Hailo device, or it would not open. The message goes on screen."""


def acquire(who: str):
    """The shared `VDevice`, opening it if this is the first caller.

    Raises `AcceleratorUnavailable` rather than returning None, because every
    caller either gets a device or has an error to show, and there is nothing
    useful to do with a half-open one. `who` appears in the log so that a
    device still held at shutdown names what is holding it.
    """
    global _vdevice, _holders, _error

    with _lock:
        if _vdevice is not None:
            _holders += 1
            log.debug("accelerator: %s joined (%d holders)", who, _holders)
            return _vdevice

        try:
            from hailo_platform import HailoSchedulingAlgorithm, VDevice
        except ImportError as exc:
            _error = (f"HailoRT's Python bindings are not installed ({exc}). "
                      "Install hailo-all on the Pi.")
            raise AcceleratorUnavailable(_error) from exc

        try:
            params = VDevice.create_params()
            # Round-robin so the models configured on this one device timeshare
            # rather than starving each other. This is what makes presence
            # detection and pose estimation coexist; it is *not* what would
            # make two VDevices coexist, and nothing here should be read as
            # implying it does.
            params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
            _vdevice = VDevice(params)
        except Exception as exc:
            _error = str(exc)
            raise AcceleratorUnavailable(
                f"could not open the AI HAT+ 2: {exc}") from exc

        _holders = 1
        _error = ""
        log.info("accelerator: opened for %s", who)
        return _vdevice


def release(who: str) -> None:
    """Give back one reference. The device closes when the last one goes.

    Never raises. This runs on teardown paths — a game exiting, a service
    stopping — where an exception would be reported instead of whatever
    actually went wrong first.
    """
    global _vdevice, _holders

    with _lock:
        if _holders <= 0:
            log.warning("accelerator: %s released it more times than it took "
                        "it", who)
            return
        _holders -= 1
        if _holders:
            log.debug("accelerator: %s let go (%d holders left)", who, _holders)
            return
        device, _vdevice = _vdevice, None

    if device is None:
        return
    try:
        device.release()
        log.info("accelerator: closed after %s", who)
    except Exception:
        log.debug("releasing the Hailo device failed", exc_info=True)


def holders() -> int:
    with _lock:
        return _holders


def error() -> str:
    with _lock:
        return _error


def identify() -> dict:
    """What the accelerator says it is. Safe to call at any time.

    Uses the *physical* device rather than the virtual one, and opens it only
    for the length of the query. Two reasons: `Device.scan()` is how HailoRT 5
    enumerates the bus — `VDevice` has no `scan` and reaching for one gives an
    AttributeError that reads exactly like a missing accelerator — and a
    firmware identify does not disturb an inference in flight, so the settings
    page can ask while a game is running.
    """
    try:
        from hailo_platform import Device
    except ImportError as exc:
        return {"available": False, "error": f"HailoRT is not installed ({exc})"}

    try:
        device_ids = Device.scan()
        if not device_ids:
            return {"available": False,
                    "error": "no Hailo device found on the PCIe bus"}
        device = Device(device_ids[0])
        try:
            identity = device.control.identify()
            return {
                "available": True,
                "architecture": str(getattr(identity, "device_architecture", "")),
                "firmware": str(getattr(identity, "firmware_version", "")),
                "serial": str(getattr(identity, "serial_number", "")),
                "device_id": str(device_ids[0]),
                "devices": len(device_ids),
                "holders": holders(),
            }
        finally:
            device.release()
    except Exception as exc:
        # The likeliest cause by far is that this process already holds the
        # device through `acquire()` — the firmware allows the query, but a
        # driver-level open can still collide. Reported rather than raised.
        return {"available": False, "error": str(exc), "holders": holders()}
