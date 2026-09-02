#!/usr/bin/env python3
"""Generate or edit images with OpenAI's image models, and save them as files.

Standard library only, on purpose. The `openai` package is installed on the Pi
and is *not* installed on the development machine, and an artwork tool that
only runs on one of the two machines is an artwork tool that gets used once.
`urllib` is on both.

The API key is never handled here. It comes from
`aipi5.core.config.credentials()`, which is the project's one audited path for
it — environment first, then a gitignored key file beside the project — so
there is no second place a credential can be read from and no second place to
check when one is rotated.

    # a new picture, transparent background, trimmed to its own bounds
    python .claude/skills/image-gen/scripts/imagegen.py generate \\
        --prompt "a red boxing glove, flat cel shading, heavy black outline" \\
        --transparent --trim --size 1024x1024 --out artwork/glove.png

    # a new picture in the style of ones that already exist
    python .claude/skills/image-gen/scripts/imagegen.py edit \\
        --image artwork/boxing-poses/transparent-sheets/00-idle.png \\
        --prompt "the same fighter, same line weight, throwing an uppercut" \\
        --out artwork/uppercut.png

    # what models this key can actually reach
    python .claude/skills/image-gen/scripts/imagegen.py models

Every failure prints what the API said rather than a summary of it. The useful
errors here are all specific — an unknown model, a size the model does not
offer, a prompt the safety system declined — and each names its own fix.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

API = "https://api.openai.com/v1"

#: The newest image model this project's key has been seen to reach. Not
#: hard-coded anywhere else: `models` prints the live list, and `--model`
#: overrides, so a newer one needs no edit here to be used.
DEFAULT_MODEL = "gpt-image-2"

#: For drafts and for iterating on a prompt. Every image costs real money and
#: the difference between the two is large, so it is worth reaching for.
DRAFT_MODEL = "gpt-image-1-mini"


def api_key() -> str:
    """The project's key, through the project's own resolver."""
    try:
        from aipi5.core import config
    except ImportError:                                    # pragma: no cover
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise SystemExit("no OPENAI_API_KEY, and aipi5.core.config is not "
                             "importable from here")
        return key
    key = config.credentials()
    if not key:
        raise SystemExit(
            "no OpenAI key. Set OPENAI_API_KEY, or put one in a key file "
            "beside the project — see aipi5/core/config.py::KEY_FILES, all of "
            "which are gitignored.")
    return key


def post_json(path: str, payload: dict, timeout: float) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{API}{path}", data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key()}",
                 "Content-Type": "application/json"})
    return send(request, timeout)


def post_multipart(path: str, fields: dict, files: list[tuple[str, Path]],
                   timeout: float) -> dict:
    """`/images/edits` takes form data, so the body is built by hand.

    `files` is a list of `(field name, path)` rather than a dict because the
    edits endpoint takes several images under the *same* name, `image[]`, and
    that is how a picture gets drawn in the style of two references at once.
    """
    boundary = f"----aipi5-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\""
            f"\r\n\r\n{value}\r\n".encode("utf-8"))
    # `upload`, not `path`. Rebinding the parameter here posted the whole
    # request to `/v1<the image's own filename>`, which comes back as a 404
    # with an empty body — an error that says nothing about its cause and
    # sends you looking at the multipart encoding, where the fault is not.
    for name, upload in files:
        kind = mimetypes.guess_type(upload.name)[0] or "application/octet-stream"
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
            f"filename=\"{upload.name}\"\r\nContent-Type: {kind}\r\n\r\n"
            .encode("utf-8"))
        chunks.append(upload.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))

    request = urllib.request.Request(
        f"{API}{path}", data=b"".join(chunks), method="POST",
        headers={"Authorization": f"Bearer {api_key()}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    return send(request, timeout)


def send(request, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            message = json.loads(detail)["error"]["message"]
        except Exception:  # noqa: BLE001
            message = detail.strip()[:800]
        raise SystemExit(f"the API refused this ({exc.code}): {message}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"could not reach the API: {exc.reason}")


def save(data: dict, out: Path, trim: bool) -> list[Path]:
    images = data.get("data") or []
    if not images:
        raise SystemExit(f"the API returned no image: {json.dumps(data)[:400]}")

    written = []
    for index, item in enumerate(images):
        encoded = item.get("b64_json")
        if not encoded:
            raise SystemExit("the API returned a URL rather than image data; "
                             "this tool expects b64_json")
        path = out if len(images) == 1 else out.with_name(
            f"{out.stem}-{index + 1}{out.suffix}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(encoded))
        if trim:
            trim_to_content(path)
        written.append(path)
        print(f"  {path}  {path.stat().st_size / 1024:.1f} kB")
    usage = data.get("usage") or {}
    if usage:
        print(f"  tokens: {json.dumps(usage)}")
    return written


def trim_to_content(path: Path) -> None:
    """Crop away fully transparent margins.

    Worth doing on anything destined to be drawn at a computed position: a
    sprite with fifty invisible pixels down one side is a sprite whose centre
    is not where its picture is, and that is a bug that only shows up as
    everything being very slightly off.
    """
    try:
        from PIL import Image
    except ImportError:
        print("  (not trimmed: Pillow is not installed)")
        return
    with Image.open(path) as image:
        image = image.convert("RGBA")
        box = image.getbbox()
        if not box or box == (0, 0, image.width, image.height):
            return
        image.crop(box).save(path)
        print(f"  trimmed to {box[2] - box[0]}x{box[3] - box[1]}")


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--out", required=True, type=Path,
                        help="where to write it; several images get -1, -2 ...")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--draft", action="store_true",
                        help=f"use {DRAFT_MODEL}: much cheaper, for iterating "
                             "on a prompt before spending on the real one")
    parser.add_argument("--size", default="1024x1024",
                        help="1024x1024, 1536x1024, 1024x1536, or auto")
    parser.add_argument("--quality", default=None,
                        help="low, medium, high or auto — model's default if "
                             "not given")
    parser.add_argument("-n", "--count", type=int, default=1)
    parser.add_argument("--transparent", action="store_true",
                        help="ask for a transparent background rather than "
                             "chroma-keying one out afterwards")
    parser.add_argument("--format", default=None, choices=("png", "webp", "jpeg"),
                        help="png keeps alpha and is what a source wants; webp "
                             "keeps alpha at a fraction of the size and is what "
                             "this project ships. Defaults to png when "
                             "--transparent is set.")
    parser.add_argument("--trim", action="store_true",
                        help="crop fully transparent margins after saving")
    parser.add_argument("--timeout", type=float, default=300.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="a new picture from a prompt")
    add_common(generate)

    edit = sub.add_parser(
        "edit", help="a new picture from a prompt and one or more references")
    add_common(edit)
    edit.add_argument("--image", required=True, action="append", type=Path,
                      help="reference or source image; repeat for several")
    edit.add_argument("--mask", type=Path,
                      help="PNG whose transparent areas are the parts to "
                           "redraw; the rest is kept exactly")

    sub.add_parser("models", help="which image models this key can reach")

    args = parser.parse_args()

    if args.command == "models":
        data = send(urllib.request.Request(
            f"{API}/models",
            headers={"Authorization": f"Bearer {api_key()}"}), 60)
        names = sorted(m["id"] for m in data.get("data", []))
        for name in names:
            if "image" in name or "dall" in name:
                print(f"  {name}")
        return 0

    model = DRAFT_MODEL if args.draft else args.model
    payload = {
        "model": model,
        "prompt": args.prompt,
        "n": args.count,
        "size": args.size,
    }
    if args.quality:
        payload["quality"] = args.quality
    if args.transparent:
        payload["background"] = "transparent"
    if args.format:
        payload["output_format"] = args.format
    elif args.transparent:
        # A transparent background needs an alpha channel and the default
        # output format has none, so asking for one without the other quietly
        # returns an opaque picture.
        payload["output_format"] = "png"

    suffix = (payload.get("output_format") or "").lower()
    if suffix and args.out.suffix.lower().lstrip(".") not in (suffix, ""):
        print(f"  note: writing {suffix} data to {args.out.name}")

    print(f"{args.command} with {model}: {args.prompt[:70]}...")
    if args.command == "generate":
        data = post_json("/images/generations", payload, args.timeout)
    else:
        for path in args.image:
            if not path.is_file():
                raise SystemExit(f"no such reference image: {path}")
        fields = {key: str(value) for key, value in payload.items()}
        files = [("image[]", path) for path in args.image]
        if args.mask:
            files.append(("mask", args.mask))
        data = post_multipart("/images/edits", fields, files, args.timeout)

    save(data, args.out, args.trim)
    print("\nRecord the provenance: every generated file in this repository "
          "has a row in ASSET_LICENSES.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
