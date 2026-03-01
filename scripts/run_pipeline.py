#!/usr/bin/env python3
"""voucher-ocr skill runner.

Implements a pragmatic 3-layer pipeline:
1) Tesseract OCR (deterministic) with multi-PSM retries to extract fields.
2) Heuristics-based parsing/cleanup (amount/datetime/merchant).
3) (Optional) Vision fallback can be implemented by the skill wrapper; this
   script stays local/offline by design.

Current behavior (v0.3):
- Scans a folder for images
- Preprocesses (PNG + resize)
- Runs tesseract with PSM 6->11->4
- Parsing improvements:
  - amount: prefers near keywords (amount/total), ignores common traps
  - datetime: supports multiple common formats (YYYY-MM-DD HH:MM[:SS], etc.)
  - merchant: prefers lines containing merchant/payee/store keywords; filters
    obvious noise like arrows/symbol-only lines.
- Writes artifacts:
  - vouchers.jsonl
  - timings.jsonl
  - summary.txt (numbered list + total)

Usage:
  run_pipeline.py --folder /path/to/folder --outdir /path/to/artifacts
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(cmd: List[str]) -> Tuple[int, str]:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    stdout_text = p.stdout.decode("utf-8", errors="replace") if p.stdout else ""
    return p.returncode, stdout_text


def preprocess_to_png(src: Path, out_png: Path, max_dim: int = 2400) -> None:
    out_png.parent.mkdir(parents=True, exist_ok=True)
    run(["sips", "-s", "format", "png", str(src), "--out", str(out_png)])
    run(["sips", "-Z", str(max_dim), str(out_png), "--out", str(out_png)])


def ocr_tesseract(png: Path, out_txt: Path, lang: str = "chi_sim+eng", psm: str = "6") -> str:
    outbase = out_txt.with_suffix("")
    run(["tesseract", str(png), str(outbase), "-l", lang, "--psm", psm])
    if out_txt.exists():
        return out_txt.read_text(encoding="utf-8", errors="ignore")
    return ""


def _clean_line(s: str) -> str:
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _is_noise_line(s: str) -> bool:
    if not s:
        return True
    if len(s) < 2:
        return True
    if re.fullmatch(r"[-+]?\d[\d,\.:]*", s):
        return True
    if re.fullmatch(r"[\W_]+", s):
        return True
    if re.fullmatch(r"[<>|/\\=\-_.:]+", s):
        return True
    if re.search(r"20\d{2}[-/.]\d{2}[-/.]\d{2}", s):
        return True
    return False


def parse_amount(text: str) -> Optional[float]:
    # Prefer amounts close to typical labels; otherwise fall back to max magnitude.
    # This helps avoid picking IDs/order numbers as "amount".
    low = text.lower()
    candidates: List[Tuple[int, float]] = []

    traps = ("order", "订单", "流水", "交易单号", "reference", "ref", "no.")
    label_re = re.compile(r"(amount|total|sum|paid|payment|rmb|cny|\u00a5|\u5143|\u91d1\u989d|\u5408\u8ba1|\u5e94\u4ed8|\u5b9e\u4ed8)")
    num_re = re.compile(r"(-?\d{1,3}(?:,\d{3})*(?:\.\d{2})|-?\d+(?:\.\d{2}))")

    for mm in num_re.finditer(text):
        s = mm.group(1)
        if s.startswith("202"):
            continue
        if ":" in s:
            continue
        try:
            v = float(s.replace(",", ""))
        except Exception:
            continue

        start = max(0, mm.start() - 40)
        end = min(len(text), mm.end() + 40)
        window = low[start:end]

        # Skip numbers likely to be order/reference-like if nearby keywords suggest so.
        if any(t in window for t in traps) and abs(v) > 999:
            continue

        score = 0
        if label_re.search(window):
            score += 5
        if 0 < abs(v) < 200000:
            score += 1
        candidates.append((score, v))

    if not candidates:
        return None

    candidates.sort(key=lambda t: (t[0], abs(t[1])), reverse=True)
    return candidates[0][1]


def parse_datetime(text: str) -> Optional[str]:
    # Accept several common formats:
    # - 2026-01-27 13:57:52
    # - 2026-01-27 13:57
    # - 2026/01/27 13:57(:52)
    # - 2026.01.27 13:57(:52)
    patterns = [
        r"(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})\s+(\d{1,2}:\d{2}:\d{2})",
        r"(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})\s+(\d{1,2}:\d{2})",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            d = m.group(1).replace("/", "-").replace(".", "-")
            t = m.group(2)
            if re.fullmatch(r"\d{1,2}:\d{2}", t):
                t = t + ":00"
            # normalize hour to 2 digits
            hh, rest = t.split(":", 1)
            t = f"{int(hh):02d}:{rest}"
            return f"{d} {t}"
    return None


def detect_refund(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in ("refund", "refunded", "\u9000\u6b3e", "\u9000\u56de", "\u5df2\u9000", "\u51b2\u6b63"))


def guess_merchant_from_ocr(text: str) -> Optional[str]:
    lines = [_clean_line(x) for x in text.splitlines()]
    lines = [x for x in lines if not _is_noise_line(x)]
    if not lines:
        return None

    prefer = re.compile(r"(\u6536\u6b3e\u65b9|\u5546\u6237|\u5e97\u94fa|\u6536\u6b3e\u4eba|\u5bf9\u65b9|\u6536\u6b3e\u8d26\u6237|\u6536\u6b3e\u5355\u4f4d|merchant|payee|to\b|store|shop)", re.IGNORECASE)

    # Score each line; pick the most likely merchant/payee line.
    best: Tuple[int, str] = (-10**9, lines[0])
    for i, l in enumerate(lines[:40]):
        score = 0
        if prefer.search(l):
            score += 5
        if 2 <= len(l) <= 48:
            score += 2
        if re.search(r"\d{4,}", l):
            score -= 2
        if re.search(r"(cny|rmb|\u00a5|\u5143)", l, re.IGNORECASE):
            score -= 1
        score += max(0, 3 - i // 3)  # earlier lines slightly preferred
        if score > best[0]:
            best = (score, l)

    return best[1][:120]


def natural_key(p: Path) -> Tuple[int, str]:
    m = re.search(r"(\d+)", p.stem)
    return (int(m.group(1)) if m else 10**9, p.name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--pattern", default=r".*\.(jpg|jpeg|png)$")
    args = ap.parse_args()

    folder = Path(args.folder)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    vouchers_path = outdir / "vouchers.jsonl"
    timings_path = outdir / "timings.jsonl"
    summary_path = outdir / "summary.txt"

    for p in (vouchers_path, timings_path, summary_path):
        if p.exists():
            p.unlink()

    rx = re.compile(args.pattern, re.IGNORECASE)
    imgs = sorted([p for p in folder.iterdir() if p.is_file() and rx.search(p.name)], key=natural_key)

    total = 0.0
    lines: List[str] = []

    for idx, img in enumerate(imgs, start=1):
        t0 = time.time()

        pre_png = outdir / "pre" / (img.stem + ".png")
        preprocess_to_png(img, pre_png)
        t1 = time.time()

        txt_path = outdir / "txt" / (img.stem + ".txt")
        txt_path.parent.mkdir(parents=True, exist_ok=True)

        used_psm = None
        text = ""
        amount = None
        for psm in ("6", "11", "4"):
            text = ocr_tesseract(pre_png, txt_path, psm=psm)
            amount = parse_amount(text)
            used_psm = psm
            if amount is not None:
                break

        dt = parse_datetime(text)
        refund = detect_refund(text)
        merchant = guess_merchant_from_ocr(text)

        signed = None
        if amount is not None:
            signed = -abs(amount) if refund else abs(amount)
            total += signed

        rec: Dict[str, Any] = {
            "n": idx,
            "file": img.name,
            "merchant_or_payee": merchant,
            "amount": abs(amount) if amount is not None else None,
            "currency": "CNY",
            "type": "refund" if refund else "expense",
            "datetime": dt,
            "ocr_psm": used_psm,
            "raw_amount": amount,
            "when": now_iso(),
        }

        with vouchers_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        with timings_path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "file": img.name,
                        "t_preprocess_s": round(t1 - t0, 3),
                        "t_total_s": round(time.time() - t0, 3),
                        "ocr_psm": used_psm,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

        merch_disp = merchant or "UNKNOWN"
        amt_disp = f"CNY {abs(amount):.2f}" if amount is not None else "CNY ?"
        dt_disp = dt or "(no datetime)"
        lines.append(f"{idx}) {merch_disp} | {amt_disp} | {dt_disp}")

    lines.append("")
    lines.append(f"Total: CNY {total:.2f}")
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
