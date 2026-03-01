#!/usr/bin/env python3
"""Vision fallback helper for voucher-ocr.

This script is designed to be called by OpenClaw (so it can use the `image` tool)
from the skill wrapper when OCR parsing is missing/low-quality.

The model is intentionally pinned to sub2api/gpt-5.2 to ensure consistent results.

Inputs:
  --image /path/to/image

Output:
  Prints ONE line of strict JSON to stdout:
    {merchant_or_payee, amount, currency, datetime, type, confidence}
"""

import argparse
import json
import sys

PROMPT = (
    "Extract structured fields from this payment voucher/invoice screenshot. "
    "Return ONLY strict JSON with keys: merchant_or_payee (string|null), "
    "amount (number|null), currency (string|null), datetime (string|null, "
    "format 'YYYY-MM-DD HH:MM:SS' if possible), type ('expense'|'refund'|null), "
    "confidence (0-1). If text is Chinese, keep merchant in Chinese. "
    "If datetime missing, null."
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    args = ap.parse_args()

    # This script is a placeholder to document the intended OpenClaw tool call.
    # It should be invoked by the skill wrapper, not run standalone.
    out = {
        "_error": "vision_fallback must be invoked via OpenClaw image tool",
        "model": "sub2api/gpt-5.2",
        "image": args.image,
        "prompt": PROMPT,
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
