# openclaw-voucher-ocr

> **An OpenClaw skill for batch-extracting structured fields from payment voucher images using a 3-layer OCR pipeline.**

---

## What is this?

`voucher-ocr` is an [OpenClaw](https://openclaw.ai) skill that processes folders of payment screenshots (WeChat Pay, Alipay, JD.com orders, bank transfers, invoices, etc.) and extracts structured data — amount, currency, merchant, date, transaction ID, payment method — into a unified JSONL schema.

It runs a **3-layer pipeline** for high accuracy:

| Layer | Engine | Role |
|-------|--------|------|
| L1 | Tesseract OCR | Fast baseline extraction |
| L2 | PaddleOCR | Cross-validation & confidence scoring |
| L3 | Vision model (via OpenClaw) | Adjudication when L1 ≠ L2 |

---

## Features

- ✅ Supports WeChat Pay, Alipay, JD.com orders, bank transfers, invoices
- ✅ Chinese + English voucher support
- ✅ Multi-amount disambiguation: correctly picks `合计/实付款` (order total) over service fees or add-ons
- ✅ Preprocessing pipeline: denoise → CLAHE contrast → deskew → binarize → normalize
- ✅ SHA256-based preprocessing cache (skip re-processing unchanged images)
- ✅ Field-level confidence scoring
- ✅ L3 Vision triggered only when needed (cost-efficient)
- ✅ JSONL output — easy to pipe into Excel, databases, or further processing

---

## Requirements

- macOS (arm64 or x86_64)
- [OpenClaw](https://openclaw.ai) (gateway running locally)
- Python 3.13 at `/opt/homebrew/bin/python3.13`
- `tesseract` (`brew install tesseract tesseract-lang`)
- `paddlepaddle` + `paddleocr` (Python packages)
- `jq` (`brew install jq`)
- macOS `sips` (built-in)

---

## Installation

```bash
# Clone into your OpenClaw skills directory
git clone https://github.com/Magicalife/openclaw-voucher-ocr.git \
  ~/.openclaw/workspace/skills/voucher-ocr

chmod +x ~/.openclaw/workspace/skills/voucher-ocr/run.sh
```

---

## Usage

### Via OpenClaw chat
```
/voucher_ocr /absolute/path/to/your/voucher-images
```

### Direct invocation
```bash
~/.openclaw/workspace/skills/voucher-ocr/run.sh /path/to/voucher-images
```

---

## Output

All results are written to `artifacts/latest/`:

| File | Contents |
|------|----------|
| `vouchers.jsonl` | One record per image — the final unified schema |
| `summary.txt` | Human-readable totals and per-image overview |
| `l1_results.jsonl` | Raw Tesseract parse results |
| `paddle_results.jsonl` | Raw PaddleOCR parse results |
| `cross_validation.jsonl` | L1 vs L2 field-level diff |
| `vision_*.jsonl` | L3 Vision adjudication results |
| `timings.jsonl` | Per-image processing times |

### Example `vouchers.jsonl` record

```json
{
  "n": 1,
  "file": "receipt_01.jpg",
  "amount": "1848.00",
  "currency": "CNY",
  "date": "2026-02-28",
  "time": "14:32:11",
  "merchant": "华硕网络京东自营旗舰店",
  "txn_id": "3405283016026157",
  "payment_method": "wechat_pay",
  "type": "expense",
  "confidence": { "amount": 0.99, "date": 0.92, "merchant": 0.95 },
  "evidence": "...",
  "processing_path": "L1+L2+L3",
  "processing_time_ms": 16628
}
```

---

## Amount Disambiguation Rule (v0.5.1)

E-commerce screenshots (e.g. JD.com) often show multiple amounts:
- `合计 ¥1848` — the actual order total ✅
- `保障服务 到手 ¥239` — a service/protection add-on ❌

The scoring engine applies priority rules to always pick the true order total:

| Line type | Score delta |
|-----------|-------------|
| Contains `合计 / 实付款 / 订单金额 / grand total` | **+0.30** |
| Contains `保障 / 服务费 / 权益 / insurance / add-on` | **-0.35** |
| Contains generic amount hint (`金额 / total / paid`) | +0.25 |
| Contains trap hint (`订单 / 单号 / id`) with val>999 | -0.20 |
| Valid range [0.01, 999999.99] | +0.10 |

---

## License

MIT

---

## Contributing

Issues and PRs welcome. If you encounter a voucher type that produces wrong results, please open an issue with a **anonymized** screenshot (blur/remove personal info before uploading).
