---
name: voucher-ocr
version: 0.5.1
description: "Batch-extract voucher fields with unified schema using L1 Tesseract + L2 Paddle cross-check + field-level L3 Vision adjudication, with rule validation and preprocessing cache."
entry: run.sh
command-dispatch: tool
command-tool: voucher_ocr_run
command-arg-mode: raw
---

# voucher-ocr

Extract structured payment-voucher fields from images in a folder.

## What it does
- Scans `*.jpg/*.jpeg/*.png`.
- Runs a 3-layer OCR pipeline and outputs one unified schema per image:
  - `amount` (string, 2 decimals)
  - `currency`
  - `date` / `time`
  - `merchant`
  - `txn_id`
  - `payment_method` (`wechat_pay|alipay|bank_transfer|unknown`)
  - `type` (`expense|refund`)
  - `confidence` (field-level: amount/date/merchant)
  - `evidence`

## v0.5.1 processing flow
1) **L1 Tesseract**
   - OCR + structured parse to unified schema.
2) **L2 PaddleOCR**
   - OCR + structured parse to the same schema.
3) **Field-level diff + rules**
   - Compare required core fields (`amount/merchant`) with confidence thresholds.
   - Apply rule validation for amount/time/merchant.
4) **L3 Vision (only when needed)**
   - Trigger only on conflicted core fields.
   - Vision only patches triggered fields.
   - If L3 still fails/incomplete, stop at L3 and mark manual.
   - `date/time` are optional: parse when available, but missing date/time does not trigger L3/manual.

## Preprocessing and cache
- Preprocess pipeline before L1/L2:
  - denoise (Gaussian blur)
  - contrast enhancement (CLAHE)
  - deskew (Hough-based, ±15°)
  - Otsu binarization
  - size normalization (short side >= 800, long side <= 2000)
- Cached by source image SHA256 under:
  - `artifacts/latest/preprocessed/`
- L1/L2 share the same cached preprocessed image.

## Outputs
- `artifacts/latest/vouchers.jsonl`
- `artifacts/latest/timings.jsonl`
- `artifacts/latest/l1_results.jsonl`
- `artifacts/latest/paddle_results.jsonl`
- `artifacts/latest/paddle_timings.jsonl`
- `artifacts/latest/cross_validation.jsonl`
- `artifacts/latest/vision_sub2api_gpt-5.2.jsonl`
- `artifacts/latest/vision_timings.jsonl`
- `artifacts/latest/summary.txt`

## Traceability fields in vouchers.jsonl
- `source_image_hash`
- `processing_path` (`L1+L2` / `L1+L2+L3`)
- `l3_triggered_fields`
- `processing_time_ms`

## Usage
- Deterministic chat invocation: `/voucher_ocr /absolute/path/to/folder`
- Legacy chat invocation: `/skill voucher-ocr /absolute/path/to/folder`
- Direct invocation: `$HOME/.openclaw/workspace/skills/voucher-ocr/run.sh /absolute/path/to/folder`

## Amount Priority Rules (v0.5.1, 2026-03-01)
When multiple monetary amounts appear on a single voucher image, the following
scoring rules apply during L1/L2 text parsing (`_schema_from_text`):

| Condition on the line containing the amount | Score delta |
|---------------------------------------------|-------------|
| Matches `ORDER_TOTAL_RE` (合计/实付款/到手价/订单金额/grand total …) | **+0.30** |
| Matches `SERVICE_AMOUNT_RE` (保障/服务费/附加/权益/insurance/add-on …) | **-0.35** |
| Matches `AMOUNT_HINT_RE` (金额/合计/total/paid …) | +0.25 |
| Matches `TRAP_HINT_RE` (订单/流水/单号/id …) and val>999 | -0.20 |
| Amount in valid range [0.01, 999999.99] | +0.10 |
| Base score | 0.55 |

**Root cause this rule fixes:** On e-commerce order screenshots (e.g. JD.com),
pages may show both `合计 ¥1848` (order total) and a service/protection item
`到手 ¥239`. L1+L2 could lock onto the smaller service-line amount;
L1=L2 match → no L3 trigger → wrong amount shipped.
With this fix ORDER_TOTAL lines score ~0.99 and SERVICE lines score ~0.20,
ensuring the true order total wins.

## Notes
- Runtime is pinned to `/opt/homebrew/bin/python3.13` for Paddle compatibility.
- Requires macOS `sips` and `tesseract`.
- Vision adjudication uses OpenClaw gateway chat APIs with model pinning.
- Corrections directory is prepared for future human write-back:
  - `artifacts/corrections/`
