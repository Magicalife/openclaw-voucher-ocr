# openclaw-voucher-ocr

> **An OpenClaw skill for batch-extracting structured fields from payment voucher images using a 3-layer OCR pipeline.**
>
> **一个 OpenClaw Skill，通过三层 OCR 流水线从支付凭证截图中批量提取结构化字段。**

---

## Table of Contents / 目录

- [What is this? / 这是什么？](#what-is-this--这是什么)
- [Use Cases / 使用场景](#use-cases--使用场景)
- [Features / 功能特性](#features--功能特性)
- [Architecture / 技术架构](#architecture--技术架构)
- [How It Works / 运行逻辑](#how-it-works--运行逻辑)
- [Requirements / 环境要求](#requirements--环境要求)
- [Installation / 安装](#installation--安装)
- [Usage / 使用方法](#usage--使用方法)
- [Output / 输出说明](#output--输出说明)
- [Amount Disambiguation / 金额消歧规则](#amount-disambiguation--金额消歧规则)
- [License / 许可证](#license--许可证)

---

## What is this? / 这是什么？

**EN:** `voucher-ocr` is an [OpenClaw](https://openclaw.ai) skill that processes folders of payment screenshots and extracts structured data into a unified JSONL schema. It supports WeChat Pay, Alipay, JD.com orders, bank transfers, and general invoices.

**CN:** `voucher-ocr` 是一个 [OpenClaw](https://openclaw.ai) Skill，用于处理一批支付截图，自动提取金额、商户、日期、交易号、支付方式等结构化字段，输出统一的 JSONL 格式，方便后续导入 Excel、数据库或进一步分析。支持微信支付、支付宝、京东订单、银行转账、普通发票等常见凭证类型。

---

## Use Cases / 使用场景

**EN:**
- 📊 Monthly expense reconciliation — batch-process all payment screenshots into a spreadsheet
- 🧾 Reimbursement automation — extract amounts, dates, merchants for expense reports
- 📦 Order tracking — pull JD.com / Taobao order details from screenshots
- 🏢 Bookkeeping assistance — feed raw receipts into accounting workflows

**CN:**
- 📊 月度报销对账 —— 批量处理所有支付截图，自动生成汇总表
- 🧾 报销单自动化 —— 提取金额、日期、商户，生成报销明细
- 📦 订单信息归档 —— 从京东/淘宝订单截图中提取订单详情
- 🏢 财务记账辅助 —— 将原始票据直接接入记账流程

---

## Features / 功能特性

**EN:**
- ✅ WeChat Pay, Alipay, JD.com, bank transfer, invoice support
- ✅ Chinese + English voucher recognition
- ✅ Multi-amount disambiguation: correctly picks `合计/实付款` over service fees
- ✅ Preprocessing pipeline: denoise → CLAHE contrast → deskew → binarize → normalize
- ✅ SHA256-based preprocessing cache (skip re-processing unchanged images)
- ✅ Field-level confidence scoring per image
- ✅ L3 Vision adjudication triggered only on conflict (cost-efficient)
- ✅ Full traceability: source hash, processing path, triggered fields, timing

**CN:**
- ✅ 支持微信支付、支付宝、京东订单、银行转账、普通发票
- ✅ 中英文凭证均可识别
- ✅ 多金额消歧：自动区分订单合计与附加服务费，取正确金额
- ✅ 图像预处理流水线：降噪 → CLAHE 对比度增强 → 自动纠偏 → 二值化 → 尺寸归一化
- ✅ SHA256 预处理缓存，相同图片跳过重复处理
- ✅ 字段级置信度评分
- ✅ L3 Vision 仅在冲突时触发，控制 API 调用成本
- ✅ 完整可追溯：来源哈希、处理路径、触发字段、各阶段耗时

---

## Architecture / 技术架构

```
Input Folder (*.jpg / *.png)
        │
        ▼
┌─────────────────────────────┐
│   Preprocessing Pipeline    │  denoise → CLAHE → deskew → binarize → normalize
│   (cached by SHA256)        │
└─────────────┬───────────────┘
              │
       ┌──────┴──────┐
       ▼             ▼
┌────────────┐  ┌────────────┐
│  L1        │  │  L2        │
│ Tesseract  │  │ PaddleOCR  │
│  OCR       │  │  OCR       │
└─────┬──────┘  └──────┬─────┘
      │                │
      └────────┬────────┘
               ▼
┌──────────────────────────────┐
│  Field-level Cross-Validation│  amount / merchant diff + confidence thresholds
│  + Rule Validation           │  amount range, format, date validity checks
└──────────────┬───────────────┘
               │
       ┌───────┴────────┐
       │ Conflict found?│
       └───────┬────────┘
          YES  │   NO
               │    └──────────────────────┐
               ▼                           ▼
┌──────────────────────────┐   ┌───────────────────────┐
│  L3 Vision Adjudication  │   │  Merge L1/L2 result   │
│  (OpenClaw image tool)   │   │  (high confidence)    │
│  patches only triggered  │   └───────────┬───────────┘
│  fields                  │               │
└──────────────┬───────────┘               │
               └──────────────┬────────────┘
                               ▼
                   ┌───────────────────────┐
                   │   Unified Schema      │
                   │   vouchers.jsonl      │
                   │   + summary.txt       │
                   └───────────────────────┘
```

---

## How It Works / 运行逻辑

### EN

1. **Scan** — all image files in the input folder are collected and sorted.
2. **Preprocess** — each image goes through a standard pipeline (denoise, contrast enhancement, deskew, binarize, size normalization). Results are cached by SHA256 so unchanged images are not reprocessed.
3. **L1 (Tesseract)** — fast OCR extracts raw text; a rule-based parser converts it to the unified schema with confidence scores.
4. **L2 (PaddleOCR)** — a second OCR pass with better Chinese text accuracy produces its own schema.
5. **Cross-validation** — L1 and L2 results are compared field by field. Fields where they agree (and meet confidence thresholds) are accepted. Disagreements or low-confidence fields are flagged for L3.
6. **Rule validation** — amount format/range, date validity, merchant sanity checks.
7. **L3 Vision (conditional)** — only triggered for flagged fields. The OpenClaw `image` tool sends the preprocessed image to a vision model, which patches only the triggered fields. This minimises API cost while maximising accuracy on hard cases.
8. **Output** — final merged result is written to `vouchers.jsonl`. A human-readable `summary.txt` is also generated.

### CN

1. **扫描** —— 收集输入目录下的所有图片文件并排序。
2. **预处理** —— 每张图片经过标准化流水线（降噪、对比度增强、纠偏、二值化、尺寸归一化），结果按 SHA256 缓存，相同图片不重复处理。
3. **L1（Tesseract）** —— 快速 OCR 提取原始文字，规则解析器转换为统一 schema 并附置信度评分。
4. **L2（PaddleOCR）** —— 第二次 OCR，对中文文字识别精度更高，产出独立 schema。
5. **交叉验证** —— 逐字段对比 L1 与 L2 结果，双方一致且置信度达标的字段直接采用；不一致或低置信度字段标记为 L3 待处理。
6. **规则校验** —— 金额格式/范围校验、日期有效性、商户名合理性检查。
7. **L3 Vision（按需触发）** —— 仅对标记字段触发。OpenClaw `image` 工具将预处理图发送给视觉模型，只修补被标记的字段，控制 API 调用成本同时最大化准确率。
8. **输出** —— 最终合并结果写入 `vouchers.jsonl`，同时生成人类可读的 `summary.txt`。

---

## Requirements / 环境要求

| Requirement | Version | Notes |
|-------------|---------|-------|
| macOS | arm64 / x86_64 | Required for `sips` |
| [OpenClaw](https://openclaw.ai) | Latest | Gateway must be running |
| Python | 3.13 at `/opt/homebrew/bin/python3.13` | Paddle compatibility |
| Tesseract | Any recent | `brew install tesseract tesseract-lang` |
| PaddleOCR | Latest | `pip install paddlepaddle paddleocr` |
| jq | Any | `brew install jq` |

---

## Installation / 安装

```bash
# Clone into your OpenClaw skills directory
# 克隆到 OpenClaw skills 目录
git clone https://github.com/Magicalife/openclaw-voucher-ocr.git \
  ~/.openclaw/workspace/skills/voucher-ocr

chmod +x ~/.openclaw/workspace/skills/voucher-ocr/run.sh
```

---

## Usage / 使用方法

### Via OpenClaw chat / 通过 OpenClaw 对话触发
```
/voucher_ocr /absolute/path/to/your/voucher-images
```

### Direct invocation / 直接命令行调用
```bash
~/.openclaw/workspace/skills/voucher-ocr/run.sh /path/to/voucher-images
```

---

## Output / 输出说明

All results are written to `artifacts/latest/`:

| File | Contents / 内容 |
|------|----------------|
| `vouchers.jsonl` | Final unified schema, one record per image / 每张图一条最终结果 |
| `summary.txt` | Human-readable totals and overview / 人类可读汇总 |
| `l1_results.jsonl` | Raw Tesseract parse results / Tesseract 原始结果 |
| `paddle_results.jsonl` | Raw PaddleOCR parse results / PaddleOCR 原始结果 |
| `cross_validation.jsonl` | L1 vs L2 field-level diff / 字段级交叉验证详情 |
| `vision_*.jsonl` | L3 Vision adjudication results / L3 视觉仲裁结果 |
| `timings.jsonl` | Per-image processing times / 各图处理耗时 |

### Example record / 示例输出

```json
{
  "n": 1,
  "file": "receipt_01.jpg",
  "amount": "1848.00",
  "currency": "CNY",
  "date": "2026-02-28",
  "time": "14:32:11",
  "merchant": "华硕网络京东自营旗舰店",
  "txn_id": "34052830****6157",
  "payment_method": "wechat_pay",
  "type": "expense",
  "confidence": { "amount": 0.99, "date": 0.92, "merchant": 0.95 },
  "evidence": "合计¥1848 | 转账时间 2026-02-28 14:32:11 | 华硕网络京东自营旗舰店",
  "processing_path": "L1+L2+L3",
  "processing_time_ms": 16628
}
```

---

## Amount Disambiguation / 金额消歧规则

**EN:** E-commerce screenshots (e.g. JD.com) often show multiple amounts on the same page:
- `合计 ¥1848` — the actual order total ✅
- `保障服务 到手 ¥239` — a service/protection add-on ❌

Without disambiguation, OCR engines may lock onto the smaller service-line amount.
The scoring engine applies priority rules to always pick the true order total:

**CN:** 电商订单截图（如京东）常在同一页面显示多个金额，例如：
- `合计 ¥1848` —— 实际订单总金额 ✅
- `保障服务 到手 ¥239` —— 附加服务/保障项金额 ❌

不加干预时，OCR 可能误取较小的服务项金额。本规则通过评分确保始终选取真实订单总额：

| Line type / 行类型 | Score delta / 分值调整 |
|-------------------|----------------------|
| `合计 / 实付款 / 订单金额 / grand total` | **+0.30** |
| `保障 / 服务费 / 附加权益 / insurance / add-on` | **-0.35** |
| Generic amount hint / 通用金额关键词 (`金额 / total / paid`) | +0.25 |
| Trap hint / 陷阱关键词 (`订单 / 单号 / id`) with val>999 | -0.20 |
| Valid range / 有效范围 [0.01, 999999.99] | +0.10 |
| Base / 基础分 | 0.55 |

---

## License / 许可证

MIT

---

## Contributing / 贡献

**EN:** Issues and PRs welcome. If you encounter a voucher type that produces wrong results, please open an issue with an **anonymized** screenshot (blur or remove all personal information before uploading).

**CN:** 欢迎提 Issue 和 PR。如果遇到识别错误的凭证类型，请提 Issue 并附上**已脱敏**的截图（上传前请遮盖或删除所有个人信息）。
