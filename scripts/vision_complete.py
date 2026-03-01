#!/usr/bin/env python3
"""Voucher OCR vision completion (layer 2/3).

This script is meant to run *inside OpenClaw* (so the `image` tool is available).
It reads an existing vouchers.jsonl produced by run_pipeline.py (layer 1), then:
- Layer 2: run vision extraction for records with missing/noisy merchant or datetime
- Layer 3: targeted retry with stronger preprocessing (bigger resize) if still missing

Model is pinned via scripts/vision_fallback.json (default: sub2api/gpt-5.2).

Outputs:
- Rewrites vouchers.jsonl (in-place) with vision-filled fields
- Writes vision_timings.jsonl
- Rewrites summary.txt (signed totals: refund negative)

Usage:
  vision_complete.py --folder <input_images_folder> --outdir <artifacts_dir>

Notes:
- Expects layer-1 preprocessing PNGs at <outdir>/pre/<stem>.png (created by run_pipeline.py)
- Uses sips for targeted retry preprocessing.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _load_cfg(skilldir: Path) -> Dict[str, Any]:
    return json.loads((skilldir / "scripts" / "vision_fallback.json").read_text(encoding="utf-8"))


def _read_jsonl(p: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def _write_jsonl(p: Path, rows: List[Dict[str, Any]]) -> None:
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _is_noise(s: Optional[str]) -> bool:
    if not s:
        return True
    t = s.strip()
    if len(t) < 2:
        return True
    if re.fullmatch(r"[\W_]+", t):
        return True
    if re.fullmatch(r"[<>|/\\=\-_.:]+", t):
        return True
    if ">" in t:
        return True
    if re.fullmatch(r"all\b.*", t, re.IGNORECASE):
        return True
    return False


def _rebuild_summary(vouchers: List[Dict[str, Any]], outdir: Path) -> None:
    total = 0.0
    lines: List[str] = []
    for rec in vouchers:
        idx = rec.get("n")
        merchant = rec.get("merchant_or_payee") or "UNKNOWN"
        amt = rec.get("amount")
        typ = rec.get("type")
        dt = rec.get("datetime")

        if isinstance(amt, (int, float)):
            if typ == "refund":
                total -= abs(float(amt))
            else:
                total += abs(float(amt))

        amt_disp = f"CNY {float(amt):.2f}" if isinstance(amt, (int, float)) else "CNY ?"
        dt_disp = dt or "(no datetime)"
        lines.append(f"{idx}) {merchant} | {amt_disp} | {dt_disp}")

    lines.append("")
    lines.append(f"Total: CNY {total:.2f}")
    (outdir / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _call_image(model: str, img_path: Path, prompt: str) -> Dict[str, Any]:
    # OpenClaw will provide a global callable `image` in tool runtime.
    fn = globals().get("image")
    if fn is None:
        raise RuntimeError("OpenClaw tool `image` not available in this runtime")
    res = fn(model=model, image=str(img_path), prompt=prompt)
    if isinstance(res, dict):
        return res
    if isinstance(res, str):
        return json.loads(res)
    raise RuntimeError(f"Unexpected image() result type: {type(res)}")


def _sips_png(src: Path, out_png: Path, max_dim: int) -> None:
    out_png.parent.mkdir(parents=True, exist_ok=True)
    # Convert to png and resize in-place
    import subprocess

    subprocess.run(["sips", "-s", "format", "png", str(src), "--out", str(out_png)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    subprocess.run(["sips", "-Z", str(max_dim), str(out_png), "--out", str(out_png)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--skilldir", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--retry-max-dim", type=int, default=4200)
    args = ap.parse_args()

    folder = Path(args.folder)
    outdir = Path(args.outdir)
    skilldir = Path(args.skilldir)

    cfg = _load_cfg(skilldir)
    model = cfg["model"]
    prompt = cfg["prompt"]

    vouchers_path = outdir / "vouchers.jsonl"
    vouchers = _read_jsonl(vouchers_path)

    timings: List[Dict[str, Any]] = []

    for rec in vouchers:
        fname = rec.get("file")
        if not fname:
            continue

        need_merchant = _is_noise(rec.get("merchant_or_payee"))
        need_dt = rec.get("datetime") in (None, "")
        if not (need_merchant or need_dt):
            continue

        stem = Path(fname).stem
        pre_png = outdir / "pre" / (stem + ".png")
        img_for_vision = pre_png if pre_png.exists() else (folder / fname)

        t0 = time.time()
        v = _call_image(model=model, img_path=img_for_vision, prompt=prompt)
        t1 = time.time()
        timings.append({"file": fname, "layer": 2, "t_s": round(t1 - t0, 3), "confidence": v.get("confidence")})

        if need_merchant and v.get("merchant_or_payee"):
            rec["merchant_or_payee"] = v.get("merchant_or_payee")
        if need_dt and v.get("datetime"):
            rec["datetime"] = v.get("datetime")
        if rec.get("amount") is None and v.get("amount") is not None:
            rec["amount"] = v.get("amount")
        if rec.get("type") in (None, "") and v.get("type"):
            rec["type"] = v.get("type")

        still_need_merchant = _is_noise(rec.get("merchant_or_payee"))
        still_need_dt = rec.get("datetime") in (None, "")
        if still_need_merchant or still_need_dt:
            big_png = outdir / "pre_big" / (stem + ".png")
            _sips_png(folder / fname, big_png, max_dim=args.retry_max_dim)

            t2 = time.time()
            v2 = _call_image(model=model, img_path=big_png, prompt=prompt)
            t3 = time.time()
            timings.append({"file": fname, "layer": 3, "t_s": round(t3 - t2, 3), "confidence": v2.get("confidence")})

            if still_need_merchant and v2.get("merchant_or_payee"):
                rec["merchant_or_payee"] = v2.get("merchant_or_payee")
            if still_need_dt and v2.get("datetime"):
                rec["datetime"] = v2.get("datetime")
            if rec.get("amount") is None and v2.get("amount") is not None:
                rec["amount"] = v2.get("amount")
            if rec.get("type") in (None, "") and v2.get("type"):
                rec["type"] = v2.get("type")

    _write_jsonl(vouchers_path, vouchers)
    _write_jsonl(outdir / "vision_timings.jsonl", timings)
    _rebuild_summary(vouchers, outdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
