#!/usr/bin/env bash
#
# Find or fetch the person-detection model. Run it on the Pi.
#
#     ./scripts/get_person_model.sh          # find one for the fitted accelerator
#     ./scripts/get_person_model.sh --cpu    # the ONNX fallback, for a Pi with no HAT
#
# Usually this downloads nothing. `hailo-all` installs a set of compiled HEFs
# into /usr/share/hailo-models, and one of them is already the right model for
# whatever accelerator is fitted — so the job here is mostly to work out which
# one that is and print the line to put in the configuration.
#
# A HEF is compiled for a specific Hailo architecture and they are NOT
# interchangeable: an h8 model on an h8l device, or either on an h10, fails at
# configure time with an architecture mismatch. That is why this detects rather
# than assumes, and why no HEF is committed to this repository.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELS="$ROOT/models"
SYSTEM_MODELS="/usr/share/hailo-models"

log()  { printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }

if [[ "${1:-}" == "--cpu" ]]; then
  mkdir -p "$MODELS"
  log "Fetching the CPU fallback model"
  echo "  SSD-MobileNet v1, COCO. ~27 MB."
  curl -fL --progress-bar -o "$MODELS/ssd_mobilenet_v1.onnx" \
    "https://github.com/onnx/models/raw/main/validated/vision/object_detection_segmentation/ssd-mobilenetv1/model/ssd_mobilenet_v1_10.onnx"
  cat <<'NOTE'

  Then, in config/aipi5.yaml:
      person_detection:
        backend: cpu
NOTE
  exit 0
fi

# ── which accelerator is fitted ───────────────────────────────────────
#
# Read from the device rather than guessed from the product name. "AI HAT+"
# has shipped with more than one chip, and the suffix the model zoo uses
# (_h8 / _h8l / _h10) follows the chip.
suffix=""
arch=""
if command -v hailortcli >/dev/null 2>&1; then
  arch="$(hailortcli fw-control identify 2>/dev/null \
          | sed -n 's/.*[Dd]evice [Aa]rchitecture:[[:space:]]*//p' | tr -d '\r' | head -1)"
fi

case "${HAILO_ARCH:-$arch}" in
  *10H*|*10h*)  suffix="_h10" ;;
  *8L*|*8l*)    suffix="_h8l" ;;
  *8*)          suffix="_h8"  ;;
  "")
    warn "no Hailo device found (hailortcli could not identify one)."
    warn "Either the HAT is not seated, hailo-all is not installed, or this"
    warn "is a Pi without one. For the CPU path: $0 --cpu"
    exit 1 ;;
  *)
    warn "unrecognised Hailo architecture '${HAILO_ARCH:-$arch}'."
    warn "Set HAILO_ARCH to hailo8, hailo8l or hailo10h and run again."
    exit 1 ;;
esac

log "Accelerator: ${arch:-$HAILO_ARCH}  →  HEFs ending $suffix"

# ── is a usable one already installed ─────────────────────────────────
#
# Preference order is about what the model *is*, not how big it is. Anything
# COCO-trained gives class 0 = person, which is all this project reads; a
# segmentation or pose model would work too but does more per frame than a
# presence question needs.
found=""
for candidate in yolov8m yolov8s yolov8n yolov11m yolov11s yolov6n yolox_s_leaky; do
  path="$SYSTEM_MODELS/${candidate}${suffix}.hef"
  if [[ -f "$path" ]]; then found="$path"; break; fi
done

if [[ -n "$found" ]]; then
  log "Already installed — nothing to download"
  ls -lh "$found" | awk '{print "  "$9"  "$5}'
  cat <<NOTE

  Put this in config/aipi5.yaml:

      person_detection:
        backend: hailo
        model: $found

  Then restart and check which backend came up:

      systemctl --user restart aipi5
      journalctl --user -u aipi5 -n 40 | grep -i person

  The line to look for is "person detection started on the hailo backend".
  Anything else means it reported itself unavailable, which is deliberate —
  there is no silent fallback to the CPU, because an accelerator that quietly
  stopped being used is a regression nobody notices for months.

NOTE
  exit 0
fi

warn "no COCO detector ending $suffix in $SYSTEM_MODELS"
echo
echo "  What is there:"
ls "$SYSTEM_MODELS" 2>/dev/null | sed 's/^/    /' || echo "    (the directory does not exist — install hailo-all)"
cat <<NOTE

  Install the model package, which is where these come from:

      sudo apt install hailo-all

  Or download one compiled for this architecture from the Hailo Model Zoo and
  point person_detection.model at it. Person detection is optional: without it
  the assistant runs normally and the screensaver never engages.

NOTE
exit 1
