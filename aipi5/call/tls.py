"""The certificate the phone connects over.

`getUserMedia` refuses to run outside a secure context, and a LAN address over
plain HTTP is not one — so the phone half of a call cannot exist without TLS,
even on a home network with nobody listening. That is the whole reason this
file is here.

**Self-signed, generated on the device, and never in the repository.** There is
no certificate authority that will issue for `aipi5.local`, and the alternative
— a real certificate for a real domain — would mean pointing DNS at a home
network, which is phase 3's problem and not a prerequisite for proving two-way
video works. The phone accepts the certificate once, by hand; iOS and Android
both then treat the origin as secure, which is all `getUserMedia` is asking.

**`openssl` rather than a Python library.** `cryptography` is not installed on
this Pi and is a compiled dependency; `openssl` is already there because
everything else on the system needs it. This costs one subprocess, once, at
first start.

The subject alternative names matter more than anything else here. A modern
browser ignores the certificate's common name entirely and matches only against
the SAN list, so a certificate without the address the phone actually typed is
a certificate the phone refuses even after the user accepts the warning.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import subprocess
from pathlib import Path

from aipi5.call import tailscale

log = logging.getLogger(__name__)

#: How long a generated certificate lasts. Long, because the failure mode of a
#: short one is a phone that stops being able to call on a day nobody changed
#: anything, and this certificate protects a LAN hop rather than a bank.
DAYS = 3650


def local_addresses() -> list[str]:
    """Every address a phone might reasonably reach this Pi on.

    The LAN address is found by asking the kernel which source address it would
    use to reach a public address — a UDP `connect` that sends no packet.
    Parsing `ip addr` or trusting `gethostbyname` both go wrong here:
    `gethostbyname` on a Pi commonly answers 127.0.1.1, which is exactly the
    address a phone cannot use.

    Tailnet addresses come after it and matter more, because they are the ones
    that work when the phone is not in the house. Leaving them out of the
    certificate is what makes a call over 5G fail with a name mismatch on an
    address that is otherwise perfectly reachable.
    """
    found: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 53))     # TEST-NET-1; nothing is sent
        found.append(probe.getsockname()[0])
    except OSError:
        log.debug("could not determine the outward address", exc_info=True)
    finally:
        probe.close()

    for address in tailscale.addresses():
        if address not in found:
            found.append(address)
    return found


def ensure(cert: Path, key: Path, hostname: str = "") -> bool:
    """Make sure a usable certificate and key exist. True when they do.

    Regenerated only when missing. A certificate that is replaced on every
    start would mean the phone re-accepting a new fingerprint every time the
    assistant restarted, which trains somebody to accept certificate warnings
    without reading them — the opposite of what the warning is for.
    """
    if cert.exists() and key.exists():
        return True

    host = hostname or socket.gethostname()
    names = {host, f"{host}.local", "localhost"}
    # The tailnet name is the one that works from a cellular network, so it
    # belongs in the certificate even though Tailscale would normally issue a
    # real one. This path is the fallback for a tailnet where HTTPS
    # certificates have not been enabled: the phone still reaches the Pi, and
    # a self-signed certificate that at least *names* the address it is served
    # on costs one warning instead of an unrecoverable mismatch.
    tailnet = tailscale.dns_name()
    if tailnet:
        names.add(tailnet)
    addresses = set(local_addresses()) | {"127.0.0.1"}

    entries = [f"DNS:{n}" for n in sorted(names)]
    for address in sorted(addresses):
        try:
            ipaddress.ip_address(address)
        except ValueError:
            continue
        entries.append(f"IP:{address}")
    san = ",".join(entries)

    cert.parent.mkdir(parents=True, exist_ok=True)
    log.info("generating a self-signed certificate for %s", san)
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(key), "-out", str(cert), "-days", str(DAYS),
             "-subj", f"/CN={host}", "-addext", f"subjectAltName={san}"],
            check=True, capture_output=True, timeout=120,
        )
    except FileNotFoundError:
        log.error("openssl is not installed; the phone cannot connect without "
                  "a certificate (sudo apt install openssl)")
        return False
    except subprocess.CalledProcessError as exc:
        log.error("could not generate a certificate: %s",
                  exc.stderr.decode("utf-8", "replace").strip())
        return False
    except subprocess.TimeoutExpired:
        log.error("generating a certificate timed out")
        return False

    key.chmod(0o600)
    log.info("certificate written to %s", cert)
    return True


def fingerprint(cert: Path) -> str:
    """The SHA-256 fingerprint, for checking against what the phone shows.

    Worth having because accepting a certificate warning is the one moment in
    this feature where a person is asked to make a security decision, and the
    only way to make that decision correctly is to compare it with something
    the Pi printed.
    """
    try:
        done = subprocess.run(
            ["openssl", "x509", "-in", str(cert), "-noout", "-fingerprint",
             "-sha256"], check=True, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.decode("ascii", "replace").strip().split("=", 1)[-1]
