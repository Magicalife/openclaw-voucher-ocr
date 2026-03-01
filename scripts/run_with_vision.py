#!/usr/bin/env python3
"""voucher-ocr v0.5 runner.

Implements a 3-layer OCR flow with field-level adjudication:
- L1: Tesseract
- L2: PaddleOCR
- L3: Vision (only for triggered core fields)

Core required fields: amount, merchant
Optional fields: date, time
"""

from __future__ import annotations

import argparse
import ast
import base64
import copy
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


GATEWAY_TIMEOUT_MS = 20_000
SEND_TIMEOUT_MS = 180_000
POLL_INTERVAL_S = 1.0
MAX_ATTACHMENT_IMAGE_BYTES = 700_000
MIN_DOWNSCALE_DIM = 1200

CORE_FIELDS = ("amount", "merchant")
CONFIDENCE_FIELDS = ("amount", "date", "merchant")
PAYMENT_METHODS = {"wechat_pay", "alipay", "bank_transfer", "unknown"}
FIELD_CONF_THRESHOLD = 0.6

AMOUNT_RE = re.compile(r"^\d+(?:\.\d{1,2})?$")
MONEY_CANDIDATE_RE = re.compile(r"([¥￥$€])?\s*(-?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})|-?\d+(?:\.\d{1,2}))")
DATE_TIME_PATTERNS = [
    re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?\s*(\d{1,2}:\d{2}(?::\d{2})?)?"),
]
TIME_ONLY_RE = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b")

MERCHANT_LABEL_RE = re.compile(
    r"^(商户全称|商户|收款方|收款人|收款单位|对方|merchant|payee|store|shop)[:：\s]*",
    re.IGNORECASE,
)
MERCHANT_HINT_RE = re.compile(
    r"(商户|收款|店铺|merchant|payee|store|shop)",
    re.IGNORECASE,
)

TXN_ID_LABEL_RE = re.compile(
    r"(?:交易单号|交易号|订单号|流水号|凭证号|reference|ref|transaction\s*id|txn\s*id|order\s*id)[:：\s]*([A-Za-z0-9\-]{8,40})",
    re.IGNORECASE,
)
LONG_ID_RE = re.compile(r"\b\d{10,32}\b")

REFUND_HINT_RE = re.compile(r"(refund|退款|退回|已退|冲正)", re.IGNORECASE)
AMOUNT_HINT_RE = re.compile(r"(金额|合计|总计|实付|应付|支付|total|amount|paid|payment|rmb|cny|¥|￥)", re.IGNORECASE)
TRAP_HINT_RE = re.compile(r"(订单|流水|reference|ref|transaction|单号|id)", re.IGNORECASE)
# SERVICE_AMOUNT_RE: lines that carry a secondary/service amount (not the order total).
# When a line matches this AND a higher "合计/到手" total exists on another line,
# the service-line amount must NOT win over the order total.
SERVICE_AMOUNT_RE = re.compile(
    r"(保障|服务费|附加|权益|增值|insurance|service\s*fee|add[-\s]?on|protection|延保|碎屏|意外险|保险|手续费|优惠券|红包|折扣|满减|运费|配送费)",
    re.IGNORECASE,
)
# ORDER_TOTAL_RE: lines that are most likely the true order total.
ORDER_TOTAL_RE = re.compile(r"(合计|实付款|实付金额|到手价|订单金额|order\s*total|grand\s*total|总计|应付款)", re.IGNORECASE)

GARBAGE_MERCHANT_RE = re.compile(r"^[\W_\d]+$")
BAD_MERCHANT_TOKENS = {
    "单号",
    "商户单号",
    "交易单号",
    "订单号",
    "流水号",
    "交易号",
    "时间",
    "金额",
}

CURRENCY_SYMBOL_MAP = {
    "¥": "CNY",
    "￥": "CNY",
    "$": "USD",
    "€": "EUR",
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _run(
    cmd: List[str],
    *,
    env: Optional[Dict[str, str]] = None,
    check: bool = True,
    timeout: Optional[int] = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        joined = " ".join(cmd)
        if len(joined) > 900:
            joined = f"{joined[:420]} ...<truncated>... {joined[-240:]}"
        raise RuntimeError(
            "command failed: {}\nstdout:\n{}\nstderr:\n{}".format(
                joined,
                proc.stdout.strip(),
                proc.stderr.strip(),
            )
        )
    return proc


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _natural_key(p: Path) -> Tuple[int, str]:
    m = re.search(r"(\d+)", p.stem)
    return (int(m.group(1)) if m else 10**9, p.name)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _load_gateway_token(home: Path) -> Optional[str]:
    cfg_path = home / ".openclaw" / "openclaw.json"
    if not cfg_path.exists():
        return None
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    token = cfg.get("gateway", {}).get("auth", {}).get("token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def _build_env() -> Dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = "/opt/homebrew/bin:/usr/bin:/bin:" + env.get("PATH", "")
    if not env.get("OPENCLAW_GATEWAY_TOKEN"):
        token = _load_gateway_token(Path.home())
        if token:
            env["OPENCLAW_GATEWAY_TOKEN"] = token
    return env


def _gateway_call(method: str, params: Dict[str, Any], env: Dict[str, str], timeout_ms: int) -> Dict[str, Any]:
    cmd = [
        "openclaw",
        "gateway",
        "call",
        method,
        "--json",
        "--timeout",
        str(timeout_ms),
        "--params",
        json.dumps(params, ensure_ascii=False),
    ]
    proc = _run(cmd, env=env, check=True)
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError(f"gateway call {method} returned empty output")
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gateway call {method} returned non-JSON: {out}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"gateway call {method} returned unexpected payload")
    return payload


def _chat_send(
    session_key: str,
    message: str,
    run_id: str,
    env: Dict[str, str],
    image_path: Optional[Path] = None,
) -> None:
    params: Dict[str, Any] = {
        "sessionKey": session_key,
        "message": message,
        "deliver": False,
        "idempotencyKey": run_id,
    }

    if image_path and image_path.exists():
        src_for_send = image_path
        temp_image: Optional[Path] = None
        try:
            if image_path.stat().st_size > MAX_ATTACHMENT_IMAGE_BYTES:
                temp_image = Path(tempfile.gettempdir()) / f"vocr-send-{uuid.uuid4().hex}.jpg"
                _run(
                    [
                        "sips",
                        "-s",
                        "format",
                        "jpeg",
                        "--setProperty",
                        "formatOptions",
                        "72",
                        str(image_path),
                        "--out",
                        str(temp_image),
                    ],
                    check=False,
                )
                if temp_image.exists():
                    dim = 2600
                    while temp_image.stat().st_size > MAX_ATTACHMENT_IMAGE_BYTES and dim >= MIN_DOWNSCALE_DIM:
                        _run(["sips", "-Z", str(dim), str(temp_image), "--out", str(temp_image)], check=False)
                        dim = int(dim * 0.85)
                    src_for_send = temp_image

            mime = "image/jpeg" if str(src_for_send).lower().endswith((".jpg", ".jpeg")) else "image/png"
            b64 = base64.b64encode(src_for_send.read_bytes()).decode("ascii")
            params["attachments"] = [{"type": "image", "mimeType": mime, "content": b64}]
        finally:
            if temp_image and temp_image.exists():
                temp_image.unlink(missing_ok=True)

    _gateway_call("chat.send", params, env, SEND_TIMEOUT_MS)


def _chat_history(session_key: str, env: Dict[str, str]) -> List[Dict[str, Any]]:
    payload = _gateway_call(
        "chat.history",
        {"sessionKey": session_key, "limit": 50},
        env,
        GATEWAY_TIMEOUT_MS,
    )
    messages = payload.get("messages")
    return messages if isinstance(messages, list) else []


def _assistant_text(msg: Dict[str, Any]) -> str:
    texts: List[str] = []
    for item in msg.get("content", []):
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            texts.append(item["text"])
    return "\n".join(texts).strip()


def _extract_json_from_text(text: str) -> Dict[str, Any]:
    raw = text.strip()
    if not raw:
        raise ValueError("empty final text")
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    first = raw.find("{")
    last = raw.rfind("}")
    if first >= 0 and last > first:
        maybe = raw[first : last + 1]
        try:
            obj = json.loads(maybe)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

        # Fallback for non-strict JSON like single quotes / Python literals.
        try:
            obj2 = ast.literal_eval(maybe)
            if isinstance(obj2, dict):
                return obj2
        except Exception:
            pass

    raise ValueError("unable to parse JSON object from assistant text")


def _find_final_assistant(messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("stopReason") in ("stop", "error"):
            return msg
    return None


def _poll_vision_result(session_key: str, env: Dict[str, str], timeout_s: int) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        messages = _chat_history(session_key, env)
        final_msg = _find_final_assistant(messages)
        if not final_msg:
            time.sleep(POLL_INTERVAL_S)
            continue
        if final_msg.get("stopReason") == "error":
            raise RuntimeError(final_msg.get("errorMessage") or "assistant error")
        try:
            parsed = _extract_json_from_text(_assistant_text(final_msg))
            return parsed, final_msg
        except ValueError:
            # Most commonly this is the preceding `/model` ack; keep polling.
            time.sleep(POLL_INTERVAL_S)
            continue
    raise TimeoutError(f"vision call timed out for session {session_key}")


def _ensure_session_model(session_key: str, model: str, env: Dict[str, str], timeout_s: int) -> None:
    run_id = f"vocr-model-{uuid.uuid4().hex[:12]}"
    _chat_send(session_key, f"/model {model}", run_id, env)
    deadline = time.time() + timeout_s
    expected = f"Model set to {model}."
    while time.time() < deadline:
        messages = _chat_history(session_key, env)
        for msg in reversed(messages):
            if msg.get("role") != "assistant":
                continue
            if msg.get("stopReason") == "error":
                raise RuntimeError(msg.get("errorMessage") or "assistant error during /model")
            if msg.get("stopReason") != "stop":
                continue
            if expected in _assistant_text(msg):
                return
        time.sleep(POLL_INTERVAL_S)
    raise TimeoutError(f"model switch timed out for session {session_key}")


def _empty_schema() -> Dict[str, Any]:
    return {
        "amount": None,
        "currency": "CNY",
        "date": None,
        "time": None,
        "merchant": None,
        "txn_id": None,
        "payment_method": "unknown",
        "type": "expense",
        "confidence": {
            "amount": 0.0,
            "date": 0.0,
            "merchant": 0.0,
        },
        "evidence": "",
    }


def _clamp_conf(v: Any) -> float:
    try:
        x = float(v)
    except Exception:
        return 0.0
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return round(x, 4)


def _clean_line(s: str) -> str:
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _is_noise_line(s: str) -> bool:
    if not s:
        return True
    if len(s) < 2:
        return True
    if re.fullmatch(r"[\W_]+", s):
        return True
    if re.fullmatch(r"\d[\d\s,:./-]*", s):
        return True
    return False


def _normalize_date_parts(y: str, m: str, d: str) -> str:
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def _normalize_time_str(t: str) -> str:
    t = t.strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", t):
        t = t + ":00"
    hh, mm, ss = t.split(":")
    return f"{int(hh):02d}:{int(mm):02d}:{int(ss):02d}"


def _normalize_amount_text(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        x = abs(float(v))
        return f"{x:.2f}"
    if not isinstance(v, str):
        return None

    s = v.strip()
    if not s:
        return None

    m = MONEY_CANDIDATE_RE.search(s)
    if not m:
        return None
    num = m.group(2).replace(",", "")
    try:
        x = abs(float(num))
    except Exception:
        return None
    return f"{x:.2f}"


def _amount_to_float(v: Any) -> Optional[float]:
    s = _normalize_amount_text(v)
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None


def _normalize_merchant(v: Any) -> Optional[str]:
    if not isinstance(v, str):
        return None
    s = MERCHANT_LABEL_RE.sub("", v.strip())
    s = re.sub(r"\s+", " ", s)
    if not s:
        return None
    if s in BAD_MERCHANT_TOKENS:
        return None
    return s[:50]


def _merchant_key(v: Any) -> str:
    s = _normalize_merchant(v) or ""
    s = re.sub(r"[\s\-_:：·,.，。()（）\[\]<>《》|/\\]+", "", s)
    return s.lower()


def _normalize_payment_method(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ("微信", "wechat", "wxpay", "财付通")):
        return "wechat_pay"
    if any(k in t for k in ("支付宝", "alipay", "zhifubao")):
        return "alipay"
    if any(k in t for k in ("银行", "bank", "转账", "网银", "card")):
        return "bank_transfer"
    return "unknown"


def _normalize_type(text: str) -> str:
    return "refund" if REFUND_HINT_RE.search(text or "") else "expense"


def _schema_from_text(text: str, layer: str, avg_score: Optional[float] = None) -> Dict[str, Any]:
    schema = _empty_schema()
    lines = [_clean_line(x) for x in text.splitlines()]
    lines = [x for x in lines if x]

    # amount
    best_amount: Optional[Tuple[float, str, str, float]] = None
    for line in lines:
        for m in MONEY_CANDIDATE_RE.finditer(line):
            sym = m.group(1) or ""
            raw_num = m.group(2)
            if ":" in raw_num:
                continue
            try:
                val = abs(float(raw_num.replace(",", "")))
            except Exception:
                continue
            if val <= 0:
                continue

            score = 0.55
            if AMOUNT_HINT_RE.search(line):
                score += 0.25
            if TRAP_HINT_RE.search(line) and val > 999:
                score -= 0.2
            if 0.01 <= val <= 999999.99:
                score += 0.1
            # Rule: ORDER_TOTAL lines (合计/实付款/到手价 etc.) get a strong boost.
            # This ensures the true order total beats any secondary service-fee amount
            # that happens to appear on a different line (e.g. 京东保障项 ¥239 vs 合计 ¥1848).
            if ORDER_TOTAL_RE.search(line):
                score += 0.30
            # Rule: SERVICE_AMOUNT lines (保障/服务费/附加权益 etc.) get penalised.
            # They represent add-on fees, NOT the order total.
            if SERVICE_AMOUNT_RE.search(line):
                score -= 0.35
            score = max(0.0, min(0.99, score))
            cur = CURRENCY_SYMBOL_MAP.get(sym, "CNY")
            cand = (score, f"{val:.2f}", cur, line)
            if best_amount is None or cand[0] > best_amount[0]:
                best_amount = cand

    amount_evidence = ""
    if best_amount:
        schema["amount"] = best_amount[1]
        schema["currency"] = best_amount[2]
        schema["confidence"]["amount"] = _clamp_conf(best_amount[0])
        amount_evidence = best_amount[3]

    # date/time
    best_date_score = 0.0
    date_evidence = ""
    for line in lines:
        for patt in DATE_TIME_PATTERNS:
            m = patt.search(line)
            if not m:
                continue
            d = _normalize_date_parts(m.group(1), m.group(2), m.group(3))
            t_raw = m.group(4)
            if t_raw:
                t = _normalize_time_str(t_raw)
                conf = 0.92
            else:
                t_match = TIME_ONLY_RE.search(line)
                if t_match:
                    t = _normalize_time_str(t_match.group(1))
                    conf = 0.82
                else:
                    t = None
                    conf = 0.66

            if conf > best_date_score:
                schema["date"] = d
                schema["time"] = t
                schema["confidence"]["date"] = _clamp_conf(conf)
                date_evidence = line
                best_date_score = conf

    if not schema.get("date"):
        # Handle legacy full datetime text match.
        m = re.search(r"(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})\s+(\d{1,2}:\d{2}(?::\d{2})?)", text)
        if m:
            d = m.group(1).replace("/", "-").replace(".", "-")
            t = _normalize_time_str(m.group(2))
            schema["date"] = d
            schema["time"] = t
            schema["confidence"]["date"] = 0.8
            date_evidence = m.group(0)

    # merchant
    candidates: List[Tuple[float, str, str]] = []
    for idx, line in enumerate(lines[:80]):
        if _is_noise_line(line):
            continue
        mline = _normalize_merchant(line)
        if not mline:
            continue
        score = 0.35
        if MERCHANT_HINT_RE.search(line):
            score += 0.35
        if 2 <= len(mline) <= 30:
            score += 0.2
        if re.search(r"\d{4,}", mline):
            score -= 0.2
        score += max(0.0, 0.12 - idx * 0.005)
        score = max(0.0, min(0.95, score))
        candidates.append((score, mline, line))

    if candidates:
        candidates.sort(key=lambda t: t[0], reverse=True)
        schema["merchant"] = candidates[0][1]
        schema["confidence"]["merchant"] = _clamp_conf(candidates[0][0])
        merchant_evidence = candidates[0][2]
    else:
        merchant_evidence = ""

    # txn_id
    txn = None
    for line in lines:
        m = TXN_ID_LABEL_RE.search(line)
        if m:
            txn = m.group(1)
            break
    if not txn:
        m = LONG_ID_RE.search(text)
        if m:
            txn = m.group(0)
    schema["txn_id"] = txn

    # payment/type
    schema["payment_method"] = _normalize_payment_method(text)
    schema["type"] = _normalize_type(text)

    if avg_score is not None:
        # Use OCR confidence as a soft multiplier.
        mult = max(0.5, min(1.0, float(avg_score)))
        for f in CONFIDENCE_FIELDS:
            schema["confidence"][f] = _clamp_conf(schema["confidence"][f] * mult)

    schema["evidence"] = (
        f"{layer} amount: {amount_evidence or 'n/a'} | "
        f"{layer} date: {date_evidence or 'n/a'} | "
        f"{layer} merchant: {merchant_evidence or 'n/a'}"
    )
    return schema


def _is_merchant_soft_invalid(v: Any) -> bool:
    m = _normalize_merchant(v)
    if not m:
        return False
    if len(m) < 1 or len(m) > 50:
        return True
    if GARBAGE_MERCHANT_RE.fullmatch(m):
        return True
    return False


def _validate_and_normalize_schema(
    schema: Dict[str, Any],
    *,
    now_utc: datetime,
) -> Tuple[Dict[str, Any], Set[str], Set[str], List[str]]:
    s = copy.deepcopy(schema)
    trigger_fields: Set[str] = set()
    soft_invalid_fields: Set[str] = set()
    notes: List[str] = []

    s.setdefault("confidence", {})
    for f in CONFIDENCE_FIELDS:
        s["confidence"][f] = _clamp_conf(s["confidence"].get(f, 0.0))

    # payment_method enum
    pm = s.get("payment_method")
    if not isinstance(pm, str) or pm not in PAYMENT_METHODS:
        s["payment_method"] = "unknown"

    # type enum
    typ = s.get("type")
    if typ not in ("expense", "refund"):
        s["type"] = "expense"

    # amount + currency
    amount_raw = s.get("amount")
    amount_str = None
    currency = s.get("currency") if isinstance(s.get("currency"), str) else "CNY"
    if isinstance(amount_raw, str):
        m = MONEY_CANDIDATE_RE.search(amount_raw.strip())
        if m:
            sym = m.group(1)
            if sym in CURRENCY_SYMBOL_MAP:
                currency = CURRENCY_SYMBOL_MAP[sym]
            try:
                amount_str = f"{abs(float(m.group(2).replace(',', ''))):.2f}"
            except Exception:
                amount_str = None
    else:
        amount_str = _normalize_amount_text(amount_raw)

    if not amount_str or not AMOUNT_RE.fullmatch(amount_str):
        s["amount"] = None
        s["confidence"]["amount"] = 0.0
        trigger_fields.add("amount")
        notes.append("amount_format_invalid")
    else:
        val = float(amount_str)
        if not (0.01 <= val <= 999999.99):
            s["amount"] = None
            s["confidence"]["amount"] = 0.0
            trigger_fields.add("amount")
            notes.append("amount_out_of_range")
        else:
            s["amount"] = amount_str
    s["currency"] = currency or "CNY"

    # date/time
    date_s = s.get("date")
    time_s = s.get("time")
    if isinstance(date_s, str) and " " in date_s and not time_s:
        parts = date_s.split()
        if len(parts) >= 2:
            date_s = parts[0]
            time_s = parts[1]

    dt_obj: Optional[datetime] = None
    if isinstance(date_s, str) and date_s.strip():
        d_norm = date_s.replace("/", "-").replace(".", "-")
        m = re.fullmatch(r"(20\d{2})-(\d{1,2})-(\d{1,2})", d_norm)
        if m:
            date_s = _normalize_date_parts(m.group(1), m.group(2), m.group(3))
        else:
            date_s = None
    else:
        date_s = None

    if isinstance(time_s, str) and time_s.strip():
        try:
            time_s = _normalize_time_str(time_s)
        except Exception:
            time_s = None
    else:
        time_s = None

    if date_s and time_s:
        try:
            dt_obj = datetime.fromisoformat(f"{date_s}T{time_s}+00:00")
        except Exception:
            dt_obj = None

    if not dt_obj:
        s["date"] = None
        s["time"] = None
        s["confidence"]["date"] = 0.0
        notes.append("datetime_invalid")
    else:
        latest_ok = now_utc + timedelta(minutes=5)
        if dt_obj > latest_ok or dt_obj.year < 2010 or dt_obj.year > now_utc.year:
            s["date"] = None
            s["time"] = None
            s["confidence"]["date"] = 0.0
            notes.append("datetime_out_of_range")
        else:
            s["date"] = date_s
            s["time"] = time_s

    # merchant
    merchant = _normalize_merchant(s.get("merchant"))
    s["merchant"] = merchant
    if merchant is None:
        # Missing merchant is handled by missing-core logic (hard trigger).
        pass
    elif _is_merchant_soft_invalid(merchant):
        s["confidence"]["merchant"] = min(s["confidence"].get("merchant", 0.0), 0.3)
        soft_invalid_fields.add("merchant")
        notes.append("merchant_soft_invalid")

    return s, trigger_fields, soft_invalid_fields, notes


def _core_missing(schema: Dict[str, Any], field: str) -> bool:
    if field == "amount":
        return _amount_to_float(schema.get("amount")) is None
    if field == "date":
        return not (isinstance(schema.get("date"), str) and isinstance(schema.get("time"), str) and schema.get("date") and schema.get("time"))
    if field == "merchant":
        return not bool(_normalize_merchant(schema.get("merchant")))
    return True


def _amount_match(a: Any, b: Any) -> bool:
    aa = _amount_to_float(a)
    bb = _amount_to_float(b)
    if aa is None or bb is None:
        return False
    return abs(aa - bb) <= 0.01


def _date_match(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return (
        isinstance(a.get("date"), str)
        and isinstance(a.get("time"), str)
        and isinstance(b.get("date"), str)
        and isinstance(b.get("time"), str)
        and a.get("date") == b.get("date")
        and a.get("time") == b.get("time")
    )


def _merchant_match_strict(a: Any, b: Any) -> bool:
    ka = _merchant_key(a)
    kb = _merchant_key(b)
    return bool(ka and kb and ka == kb)


def _value_match(field: str, l1: Dict[str, Any], l2: Dict[str, Any]) -> bool:
    if field == "amount":
        return _amount_match(l1.get("amount"), l2.get("amount"))
    if field == "date":
        return _date_match(l1, l2)
    if field == "merchant":
        return _merchant_match_strict(l1.get("merchant"), l2.get("merchant"))
    return False


def _merge_l1_l2(l1: Dict[str, Any], l2: Dict[str, Any]) -> Dict[str, Any]:
    out = _empty_schema()

    for field in CORE_FIELDS:
        c1 = float(l1.get("confidence", {}).get(field, 0.0))
        c2 = float(l2.get("confidence", {}).get(field, 0.0))
        v1 = l1.get(field)
        v2 = l2.get(field)

        if _core_missing({field: v1, "date": l1.get("date"), "time": l1.get("time")}, field) and _core_missing(
            {field: v2, "date": l2.get("date"), "time": l2.get("time")}, field
        ):
            out[field] = None
            out["confidence"][field] = 0.0
            continue

        if c2 > c1:
            chosen = l2
            conf = c2
        else:
            chosen = l1
            conf = c1

        out[field] = chosen.get(field)
        out["confidence"][field] = _clamp_conf(conf)

    # date/time coupled fields
    if out.get("date") is None or out.get("time") is None:
        if isinstance(l1.get("date"), str) and isinstance(l1.get("time"), str):
            out["date"] = l1.get("date")
            out["time"] = l1.get("time")
        elif isinstance(l2.get("date"), str) and isinstance(l2.get("time"), str):
            out["date"] = l2.get("date")
            out["time"] = l2.get("time")

    # non-core: loose pick
    out["txn_id"] = l1.get("txn_id") or l2.get("txn_id")
    pm1 = l1.get("payment_method") if l1.get("payment_method") in PAYMENT_METHODS else "unknown"
    pm2 = l2.get("payment_method") if l2.get("payment_method") in PAYMENT_METHODS else "unknown"
    out["payment_method"] = pm1 if pm1 != "unknown" else pm2

    if l1.get("type") == "refund" or l2.get("type") == "refund":
        out["type"] = "refund"
    else:
        out["type"] = "expense"

    # currency prefers layer with higher amount confidence.
    if float(l2.get("confidence", {}).get("amount", 0.0)) > float(l1.get("confidence", {}).get("amount", 0.0)):
        out["currency"] = l2.get("currency") or "CNY"
    else:
        out["currency"] = l1.get("currency") or "CNY"

    out["evidence"] = f"merge l1/l2 | l1: {l1.get('evidence', '')} | l2: {l2.get('evidence', '')}"
    return out


def _determine_l3_fields(
    l1: Dict[str, Any],
    l2: Dict[str, Any],
    l1_rule_trigger: Set[str],
    l2_rule_trigger: Set[str],
    l1_soft_invalid: Set[str],
    l2_soft_invalid: Set[str],
) -> Tuple[List[str], Dict[str, Any]]:
    reasons: Dict[str, List[str]] = {f: [] for f in CORE_FIELDS}

    for f in CORE_FIELDS:
        if f in l1_rule_trigger or f in l2_rule_trigger:
            reasons[f].append("rule_invalid")

        missing_1 = _core_missing(l1, f)
        missing_2 = _core_missing(l2, f)
        if missing_1 or missing_2:
            reasons[f].append("missing")

        c1 = float(l1.get("confidence", {}).get(f, 0.0))
        c2 = float(l2.get("confidence", {}).get(f, 0.0))
        if c1 < FIELD_CONF_THRESHOLD or c2 < FIELD_CONF_THRESHOLD:
            if f == "merchant" and (f in l1_soft_invalid or f in l2_soft_invalid) and "missing" not in reasons[f]:
                reasons[f].append("soft_low_conf")
            else:
                reasons[f].append("low_conf")

        if not missing_1 and not missing_2 and not _value_match(f, l1, l2):
            reasons[f].append("mismatch")

    triggered: List[str] = []
    for f in CORE_FIELDS:
        rs = reasons[f]
        if not rs:
            continue
        hard_reasons = [x for x in rs if x not in ("soft_low_conf",)]
        if hard_reasons:
            triggered.append(f)

    compare = {
        "core": {
            f: {
                "l1": l1.get(f) if f != "date" else {"date": l1.get("date"), "time": l1.get("time")},
                "l2": l2.get(f) if f != "date" else {"date": l2.get("date"), "time": l2.get("time")},
                "match": _value_match(f, l1, l2) if f != "date" else _date_match(l1, l2),
                "l1_conf": float(l1.get("confidence", {}).get(f, 0.0)),
                "l2_conf": float(l2.get("confidence", {}).get(f, 0.0)),
                "reasons": reasons[f],
            }
            for f in CORE_FIELDS
        },
        "triggered_fields": triggered,
        "strict_match": len(triggered) == 0,
    }
    return triggered, compare


def _schema_from_vision_obj(obj: Dict[str, Any]) -> Dict[str, Any]:
    schema = _empty_schema()

    # Support new schema keys.
    for k in ("amount", "currency", "date", "time", "merchant", "txn_id", "payment_method", "type", "evidence"):
        if k in obj:
            schema[k] = obj.get(k)

    # Backward-compatible keys.
    if not schema.get("merchant") and isinstance(obj.get("merchant_or_payee"), str):
        schema["merchant"] = obj.get("merchant_or_payee")

    if (not schema.get("date") or not schema.get("time")) and isinstance(obj.get("datetime"), str):
        m = re.search(r"(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})\s+(\d{1,2}:\d{2}(?::\d{2})?)", obj["datetime"])
        if m:
            schema["date"] = m.group(1).replace("/", "-").replace(".", "-")
            schema["time"] = _normalize_time_str(m.group(2))

    conf = obj.get("confidence")
    if isinstance(conf, dict):
        for f in CONFIDENCE_FIELDS:
            schema["confidence"][f] = _clamp_conf(conf.get(f, schema["confidence"][f]))
    elif isinstance(conf, (int, float)):
        for f in CONFIDENCE_FIELDS:
            schema["confidence"][f] = _clamp_conf(conf)

    # Normalize amount to string format.
    schema["amount"] = _normalize_amount_text(schema.get("amount"))
    schema["merchant"] = _normalize_merchant(schema.get("merchant"))
    if not isinstance(schema.get("evidence"), str):
        schema["evidence"] = "vision"

    if schema.get("payment_method") not in PAYMENT_METHODS:
        schema["payment_method"] = "unknown"
    if schema.get("type") not in ("expense", "refund"):
        schema["type"] = "expense"
    if not isinstance(schema.get("currency"), str) or not schema.get("currency"):
        schema["currency"] = "CNY"

    return schema


def _apply_vision_patch(base: Dict[str, Any], vision: Dict[str, Any], fields: List[str]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for f in fields:
        if f == "amount":
            if vision.get("amount") is not None:
                out["amount"] = vision.get("amount")
                out["currency"] = vision.get("currency") or out.get("currency")
                out["confidence"]["amount"] = _clamp_conf(vision.get("confidence", {}).get("amount", out["confidence"].get("amount", 0.0)))
        elif f == "date":
            if isinstance(vision.get("date"), str) and isinstance(vision.get("time"), str):
                out["date"] = vision.get("date")
                out["time"] = vision.get("time")
                out["confidence"]["date"] = _clamp_conf(vision.get("confidence", {}).get("date", out["confidence"].get("date", 0.0)))
        elif f == "merchant":
            m = _normalize_merchant(vision.get("merchant"))
            if m:
                out["merchant"] = m
                out["confidence"]["merchant"] = _clamp_conf(
                    vision.get("confidence", {}).get("merchant", out["confidence"].get("merchant", 0.0))
                )

    if isinstance(vision.get("txn_id"), str) and vision.get("txn_id"):
        out["txn_id"] = vision.get("txn_id")
    if vision.get("payment_method") in PAYMENT_METHODS:
        out["payment_method"] = vision.get("payment_method")
    if vision.get("type") in ("expense", "refund"):
        out["type"] = vision.get("type")
    if isinstance(vision.get("evidence"), str) and vision.get("evidence"):
        out["evidence"] = vision.get("evidence")

    return out


def _build_vision_message(prompt: str, fields: List[str], l1: Dict[str, Any], l2: Dict[str, Any]) -> str:
    payload = {
        "triggered_fields": fields,
        "l1": {k: l1.get(k) for k in ["amount", "currency", "date", "time", "merchant", "txn_id", "payment_method", "type"]},
        "l2": {k: l2.get(k) for k in ["amount", "currency", "date", "time", "merchant", "txn_id", "payment_method", "type"]},
    }

    return (
        "You are adjudicating OCR conflicts for one payment voucher image. "
        f"{prompt.strip()} "
        "Only decide triggered core fields using the image as source of truth. "
        "Return exactly one strict JSON object with keys: "
        "amount,currency,date,time,merchant,txn_id,payment_method,type,confidence,evidence. "
        "confidence must be an object with keys amount,date,merchant (0-1). "
        "payment_method enum: wechat_pay/alipay/bank_transfer/unknown. "
        "type enum: expense/refund. "
        f"Conflict context: {json.dumps(payload, ensure_ascii=False)}"
    )


def _init_paddle_engine() -> Tuple[Optional[Any], Optional[str]]:
    try:
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        from paddleocr import PaddleOCR  # type: ignore
    except Exception as exc:
        return None, str(exc)

    attempts: List[Dict[str, Any]] = [
        {
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "text_detection_model_name": "PP-OCRv5_mobile_det",
            "text_recognition_model_name": "PP-OCRv5_mobile_rec",
        },
        {"lang": "ch", "use_textline_orientation": True},
        {"lang": "ch", "use_angle_cls": True},
        {"lang": "ch"},
    ]

    errors: List[str] = []
    for kwargs in attempts:
        try:
            engine = PaddleOCR(**kwargs)
            return engine, None
        except Exception as exc:
            errors.append(f"{kwargs}: {exc}")

    return None, " | ".join(errors[-3:]) if errors else "paddle init failed"


def _extract_paddle_lines(raw: Any) -> Tuple[List[str], List[float]]:
    lines: List[str] = []
    scores: List[float] = []

    if isinstance(raw, list):
        candidates = raw
    elif isinstance(raw, dict):
        candidates = [raw]
    else:
        candidates = []

    for item in candidates:
        if hasattr(item, "get"):
            txts = item.get("rec_texts")
            scs = item.get("rec_scores")
            if isinstance(txts, list):
                for i, t in enumerate(txts):
                    if isinstance(t, str) and t.strip():
                        lines.append(t.strip())
                    if isinstance(scs, list) and i < len(scs) and isinstance(scs[i], (int, float)):
                        scores.append(float(scs[i]))
                continue

        if isinstance(item, (list, tuple)) and item:
            # Legacy shape: [[box, [text, score]], ...]
            if isinstance(item[0], (list, tuple)):
                for sub in item:
                    if isinstance(sub, (list, tuple)) and len(sub) >= 2 and isinstance(sub[1], (list, tuple)):
                        t = sub[1][0] if len(sub[1]) >= 1 else None
                        c = sub[1][1] if len(sub[1]) >= 2 else None
                        if isinstance(t, str) and t.strip():
                            lines.append(t.strip())
                        if isinstance(c, (int, float)):
                            scores.append(float(c))
            elif isinstance(item[0], str):
                t = item[0]
                c = item[1] if len(item) >= 2 else None
                if isinstance(t, str) and t.strip():
                    lines.append(t.strip())
                if isinstance(c, (int, float)):
                    scores.append(float(c))

    return lines, scores


def _paddle_extract(
    engine: Optional[Any],
    image_path: Path,
    init_error: Optional[str],
    now_utc: datetime,
) -> Tuple[Dict[str, Any], str, Optional[str], float, str, Set[str], Set[str], List[str]]:
    start = time.time()
    if engine is None:
        empty = _empty_schema()
        return empty, "unavailable", init_error or "paddle unavailable", round(time.time() - start, 3), "", set(), set(), ["paddle_unavailable"]

    try:
        if hasattr(engine, "predict"):
            raw = engine.predict(str(image_path))
        else:
            try:
                raw = engine.ocr(str(image_path), cls=True)
            except TypeError:
                raw = engine.ocr(str(image_path))

        lines, scores = _extract_paddle_lines(raw)
        text_blob = "\n".join(lines)
        avg_score = (sum(scores) / len(scores)) if scores else None
        schema = _schema_from_text(text_blob, "L2", avg_score=avg_score)
        schema, rule_trigger, soft_invalid, notes = _validate_and_normalize_schema(schema, now_utc=now_utc)
        return schema, "ok", None, round(time.time() - start, 3), text_blob, rule_trigger, soft_invalid, notes
    except Exception as exc:
        empty = _empty_schema()
        return empty, "error", str(exc), round(time.time() - start, 3), "", set(CORE_FIELDS), set(), ["paddle_error"]


def _tesseract_extract(
    image_path: Path,
    txt_dir: Path,
    stem: str,
    now_utc: datetime,
) -> Tuple[Dict[str, Any], str, float, str, Set[str], Set[str], List[str]]:
    txt_dir.mkdir(parents=True, exist_ok=True)
    start = time.time()

    best_schema = _empty_schema()
    best_psm = "6"
    best_text = ""
    best_score = -1.0
    best_trigger: Set[str] = set(CORE_FIELDS)
    best_soft: Set[str] = set()
    best_notes: List[str] = []

    for psm in ("6", "11", "4"):
        out_txt = txt_dir / f"{stem}.psm{psm}.txt"
        out_base = out_txt.with_suffix("")
        _run(["tesseract", str(image_path), str(out_base), "-l", "chi_sim+eng", "--psm", psm], check=False)
        text = out_txt.read_text(encoding="utf-8", errors="ignore") if out_txt.exists() else ""

        schema = _schema_from_text(text, "L1")
        schema, rule_trigger, soft_invalid, notes = _validate_and_normalize_schema(schema, now_utc=now_utc)

        present = 0
        for f in CORE_FIELDS:
            if not _core_missing(schema, f):
                present += 1
        score = present + sum(float(schema.get("confidence", {}).get(f, 0.0)) for f in CORE_FIELDS)

        if score > best_score:
            best_score = score
            best_schema = schema
            best_psm = psm
            best_text = text
            best_trigger = rule_trigger
            best_soft = soft_invalid
            best_notes = notes

    best_txt = txt_dir / f"{stem}.txt"
    best_txt.write_text(best_text, encoding="utf-8")

    return best_schema, best_psm, round(time.time() - start, 3), best_text, best_trigger, best_soft, best_notes


def _import_cv2_modules() -> Tuple[Optional[Any], Optional[Any]]:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        return None, None
    return cv2, np


def _resize_by_policy(img: Any, cv2: Any, min_short: int = 800, max_long: int = 2000) -> Any:
    h, w = img.shape[:2]
    short_side = min(h, w)
    long_side = max(h, w)

    scale = 1.0
    if short_side < min_short:
        scale = min_short / float(short_side)
    if long_side * scale > max_long:
        scale = max_long / float(long_side)

    if abs(scale - 1.0) < 0.01:
        return img

    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    interp = cv2.INTER_LINEAR if scale > 1 else cv2.INTER_AREA
    return cv2.resize(img, (new_w, new_h), interpolation=interp)


def _deskew_image(img: Any, cv2: Any, np: Any) -> Any:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 180,
        threshold=120,
        minLineLength=max(30, gray.shape[1] // 5),
        maxLineGap=20,
    )

    if lines is None:
        return img

    angles: List[float] = []
    for ln in lines[:400]:
        x1, y1, x2, y2 = ln[0]
        angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
        if -15.0 <= angle <= 15.0 and abs(angle) >= 0.3:
            angles.append(angle)

    if not angles:
        return img

    median_angle = float(np.median(np.array(angles)))
    if abs(median_angle) < 0.5:
        return img

    h, w = img.shape[:2]
    center = (w // 2, h // 2)
    mat = cv2.getRotationMatrix2D(center, median_angle, 1.0)
    rotated = cv2.warpAffine(
        img,
        mat,
        (w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )
    return rotated


def _preprocess_and_cache(
    src: Path,
    pre_cache_dir: Path,
    compat_pre_path: Path,
) -> Tuple[Path, str, bool, float]:
    start = time.time()
    pre_cache_dir.mkdir(parents=True, exist_ok=True)
    compat_pre_path.parent.mkdir(parents=True, exist_ok=True)

    h = _sha256_file(src)
    cached_png = pre_cache_dir / f"{h}.png"
    existed_before = cached_png.exists()

    if not existed_before:
        cv2, np = _import_cv2_modules()
        if cv2 is None or np is None:
            tmp_png = pre_cache_dir / f"{h}.tmp.png"
            _run(["sips", "-s", "format", "png", str(src), "--out", str(tmp_png)], check=False)
            _run(["sips", "-Z", "2000", str(tmp_png), "--out", str(tmp_png)], check=False)
            if tmp_png.exists():
                tmp_png.replace(cached_png)
            else:
                raise RuntimeError(f"failed to preprocess image: {src}")
        else:
            img = cv2.imread(str(src))
            if img is None:
                raise RuntimeError(f"failed to read image for preprocessing: {src}")

            # 1) denoise
            img = cv2.GaussianBlur(img, (3, 3), 0)

            # 2) CLAHE
            lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
            l, a, b = cv2.split(lab)
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            l2 = clahe.apply(l)
            img = cv2.cvtColor(cv2.merge([l2, a, b]), cv2.COLOR_LAB2BGR)

            # 3) deskew
            img = _deskew_image(img, cv2, np)

            # 4) binarize
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            img = cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)

            # 5) normalize size
            img = _resize_by_policy(img, cv2, min_short=800, max_long=2000)

            ok = cv2.imwrite(str(cached_png), img)
            if not ok:
                raise RuntimeError(f"failed to write preprocessed image: {cached_png}")

    if not compat_pre_path.exists() or compat_pre_path.stat().st_mtime < cached_png.stat().st_mtime:
        shutil.copy2(cached_png, compat_pre_path)

    return cached_png, h, existed_before, round((time.time() - start) * 1000, 1)


def _summary_total(vouchers: List[Dict[str, Any]]) -> float:
    total = 0.0
    for rec in vouchers:
        amt = _amount_to_float(rec.get("amount"))
        if amt is None:
            continue
        if rec.get("type") == "refund":
            total -= abs(amt)
        else:
            total += abs(amt)
    return total


def _write_summary(vouchers: List[Dict[str, Any]], outdir: Path) -> None:
    lines: List[str] = []
    for rec in vouchers:
        n = rec.get("n")
        merchant = rec.get("merchant") or "UNKNOWN"
        amt = _amount_to_float(rec.get("amount"))
        date_s = rec.get("date") or "(no date)"
        time_s = rec.get("time") or "(no time)"
        dt = f"{date_s} {time_s}" if rec.get("date") and rec.get("time") else (date_s if rec.get("date") else "(no datetime)")
        manual = bool(rec.get("needs_manual_confirmation"))
        reason = rec.get("manual_reason") if manual else None
        prefix = "[MANUAL] " if manual else ""
        reason_part = f" ({reason})" if reason else ""
        amt_disp = f"CNY {abs(amt):.2f}" if amt is not None else "CNY ?"
        lines.append(f"{n}) {prefix}{merchant} | {amt_disp} | {dt}{reason_part}")

    lines.append("")
    lines.append(f"Total: CNY {_summary_total(vouchers):.2f}")
    (outdir / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _final_core_missing_fields(schema: Dict[str, Any]) -> List[str]:
    missing: List[str] = []
    for f in CORE_FIELDS:
        if _core_missing(schema, f):
            missing.append(f)
    return missing


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--skilldir", required=True)
    ap.add_argument("--vision-timeout-s", type=int, default=120)
    ap.add_argument("--force-vision", action="store_true")
    ap.add_argument("--require-paddle", action="store_true")
    args = ap.parse_args()

    folder = Path(args.folder)
    outdir = Path(args.outdir)
    skilldir = Path(args.skilldir)
    outdir.mkdir(parents=True, exist_ok=True)

    env = _build_env()
    if not env.get("OPENCLAW_GATEWAY_TOKEN"):
        raise RuntimeError("OPENCLAW_GATEWAY_TOKEN is missing; cannot call OpenClaw gateway")

    # Ensure corrections folder exists (for v0.6.0 write-back path).
    (skilldir / "artifacts" / "corrections").mkdir(parents=True, exist_ok=True)

    cfg = json.loads((skilldir / "scripts" / "vision_fallback.json").read_text(encoding="utf-8"))
    model = cfg.get("model", "sub2api/gpt-5.2")
    prompt = cfg.get("prompt", "").strip()
    if not prompt:
        raise RuntimeError("vision_fallback.json prompt is empty")

    rx = re.compile(r".*\.(jpg|jpeg|png)$", re.IGNORECASE)
    images = sorted([p for p in folder.iterdir() if p.is_file() and rx.search(p.name)], key=_natural_key)

    paddle_engine, paddle_init_error = _init_paddle_engine()
    if args.require_paddle and paddle_engine is None:
        raise RuntimeError(f"paddleocr unavailable: {paddle_init_error}")

    pre_cache_dir = outdir / "preprocessed"
    txt_dir = outdir / "txt"
    pre_compat_dir = outdir / "pre"

    vouchers: List[Dict[str, Any]] = []
    timings_rows: List[Dict[str, Any]] = []
    l1_rows: List[Dict[str, Any]] = []
    paddle_rows: List[Dict[str, Any]] = []
    paddle_timings: List[Dict[str, Any]] = []
    cross_rows: List[Dict[str, Any]] = []
    vision_rows: List[Dict[str, Any]] = []
    vision_timings: List[Dict[str, Any]] = []

    run_tag = uuid.uuid4().hex[:8]

    for idx, img in enumerate(images, start=1):
        now_utc = _now_utc()
        img_t0 = time.time()

        pre_compat = pre_compat_dir / f"{img.stem}.png"
        pre_png, img_hash, cache_hit, t_pre_ms = _preprocess_and_cache(img, pre_cache_dir, pre_compat)

        l1_schema, l1_psm, l1_t_s, l1_text, l1_rule_trigger, l1_soft_invalid, l1_notes = _tesseract_extract(
            pre_png,
            txt_dir,
            img.stem,
            now_utc,
        )

        l1_rows.append(
            {
                "file": img.name,
                "layer": 1,
                "status": "ok",
                "ocr_psm": l1_psm,
                "rule_trigger_fields": sorted(l1_rule_trigger),
                "soft_invalid_fields": sorted(l1_soft_invalid),
                "notes": l1_notes,
                "schema": l1_schema,
            }
        )

        paddle_schema, paddle_status, paddle_error, paddle_t_s, paddle_text, l2_rule_trigger, l2_soft_invalid, l2_notes = _paddle_extract(
            paddle_engine,
            pre_png,
            paddle_init_error,
            now_utc,
        )

        paddle_rows.append(
            {
                "file": img.name,
                "layer": 2,
                "status": paddle_status,
                "error": paddle_error,
                "rule_trigger_fields": sorted(l2_rule_trigger),
                "soft_invalid_fields": sorted(l2_soft_invalid),
                "notes": l2_notes,
                "schema": paddle_schema,
            }
        )
        paddle_timings.append(
            {
                "file": img.name,
                "layer": 2,
                "status": paddle_status,
                "error": paddle_error,
                "t_s": paddle_t_s,
            }
        )

        l3_fields, compare = _determine_l3_fields(
            l1_schema,
            paddle_schema,
            l1_rule_trigger,
            l2_rule_trigger,
            l1_soft_invalid,
            l2_soft_invalid,
        )

        if args.force_vision:
            for f in CORE_FIELDS:
                if f not in l3_fields:
                    l3_fields.append(f)

        cross_rows.append(
            {
                "file": img.name,
                "layer": "1_vs_2",
                "strict_match": len(l3_fields) == 0,
                "triggered_fields": l3_fields,
                "compare": compare,
                "l1": l1_schema,
                "l2": paddle_schema,
            }
        )

        merged = _merge_l1_l2(l1_schema, paddle_schema)

        need_vision = len(l3_fields) > 0
        print(
            f"[debug] {img.name}: strict={len(l3_fields)==0} triggered={l3_fields} force={args.force_vision} need_vision={need_vision}",
            flush=True,
        )

        vision_called = False
        vision_ok = False
        vision_error: Optional[str] = None
        vision_conf: Optional[Dict[str, float]] = None
        t_l3_s = 0.0

        if need_vision:
            vision_called = True
            session = f"agent:main:voucher-ocr:vision:{run_tag}:{idx}:l3"
            run_id = f"vocr-{uuid.uuid4().hex[:12]}"
            msg = _build_vision_message(prompt, l3_fields, l1_schema, paddle_schema)

            l3_t0 = time.time()
            try:
                _ensure_session_model(session, model, env, args.vision_timeout_s)
                _chat_send(session, msg, run_id, env, image_path=pre_png)
                raw_obj, final_msg = _poll_vision_result(session, env, args.vision_timeout_s)

                v_schema = _schema_from_vision_obj(raw_obj)
                v_schema, v_rule_trigger, _v_soft, v_notes = _validate_and_normalize_schema(v_schema, now_utc=_now_utc())
                patched = _apply_vision_patch(merged, v_schema, l3_fields)
                patched, final_rule_trigger, _f_soft, _f_notes = _validate_and_normalize_schema(patched, now_utc=_now_utc())

                unresolved = [f for f in l3_fields if f in final_rule_trigger or _core_missing(patched, f)]
                merged = patched
                vision_ok = len(unresolved) == 0
                if not vision_ok:
                    vision_error = f"vision_incomplete:{','.join(unresolved)}"

                vision_conf = {
                    f: float(v_schema.get("confidence", {}).get(f, 0.0)) for f in CORE_FIELDS
                }

                vision_rows.append(
                    {
                        "file": img.name,
                        "layer": 3,
                        "model": model,
                        "sessionKey": session,
                        "provider": final_msg.get("provider"),
                        "modelResolved": final_msg.get("model"),
                        "triggered_fields": l3_fields,
                        "rule_trigger_fields": sorted(v_rule_trigger),
                        "notes": v_notes,
                        "schema": v_schema,
                        "raw": raw_obj,
                    }
                )
            except Exception as exc:
                vision_ok = False
                vision_error = str(exc)

            t_l3_s = round(time.time() - l3_t0, 3)
            vision_timings.append(
                {
                    "file": img.name,
                    "layer": 3,
                    "model": model,
                    "status": "ok" if vision_ok else "error",
                    "error": vision_error,
                    "triggered_fields": l3_fields,
                    "confidence": vision_conf,
                    "t_s": t_l3_s,
                }
            )

        merged, final_rule_trigger, _final_soft, final_notes = _validate_and_normalize_schema(merged, now_utc=_now_utc())
        missing_core = _final_core_missing_fields(merged)

        manual = False
        manual_reason: Optional[str] = None
        if need_vision and not vision_ok:
            manual = True
            manual_reason = vision_error or "vision_failed"
        elif missing_core or final_rule_trigger:
            manual = True
            manual_reason = "core_fields_incomplete"

        if vision_called:
            processing_path = "L1+L2+L3"
        else:
            processing_path = "L1+L2"

        rec: Dict[str, Any] = {
            "n": idx,
            "file": img.name,
            "amount": merged.get("amount"),
            "currency": merged.get("currency") or "CNY",
            "date": merged.get("date"),
            "time": merged.get("time"),
            "merchant": merged.get("merchant"),
            "txn_id": merged.get("txn_id"),
            "payment_method": merged.get("payment_method"),
            "type": merged.get("type"),
            "confidence": merged.get("confidence"),
            "evidence": merged.get("evidence"),
            # Backward-compatible aliases used by older reports.
            "merchant_or_payee": merged.get("merchant"),
            "datetime": f"{merged.get('date')} {merged.get('time')}" if merged.get("date") and merged.get("time") else None,
            "ocr_psm": l1_psm,
            "raw_amount": _amount_to_float(merged.get("amount")),
            "when": _now_utc().isoformat(),
            "needs_manual_confirmation": manual,
            "manual_reason": manual_reason,
            "manual_missing_fields": missing_core,
            "source_image_hash": f"sha256:{img_hash}",
            "processing_path": processing_path,
            "l3_triggered_fields": l3_fields,
            "processing_time_ms": int(round((time.time() - img_t0) * 1000)),
            "layer2_paddle_status": paddle_status,
            "layer2_cross_validation": compare,
            "resolution_layer": "manual" if manual else ("vision" if need_vision else "tesseract+paddle"),
            "final_rule_trigger_fields": sorted(final_rule_trigger),
            "final_notes": final_notes,
        }

        vouchers.append(rec)

        timings_rows.append(
            {
                "file": img.name,
                "source_image_hash": f"sha256:{img_hash}",
                "preprocess_cache_hit": bool(cache_hit),
                "t_preprocess_ms": t_pre_ms,
                "t_l1_s": l1_t_s,
                "t_l2_s": paddle_t_s,
                "t_l3_s": t_l3_s,
                "t_total_s": round(time.time() - img_t0, 3),
                "processing_path": processing_path,
            }
        )

    _write_jsonl(outdir / "vouchers.jsonl", vouchers)
    _write_jsonl(outdir / "timings.jsonl", timings_rows)
    _write_jsonl(outdir / "l1_results.jsonl", l1_rows)
    _write_jsonl(outdir / "paddle_results.jsonl", paddle_rows)
    _write_jsonl(outdir / "paddle_timings.jsonl", paddle_timings)
    _write_jsonl(outdir / "cross_validation.jsonl", cross_rows)
    _write_jsonl(outdir / "vision_sub2api_gpt-5.2.jsonl", vision_rows)
    _write_jsonl(outdir / "vision_timings.jsonl", vision_timings)
    _write_summary(vouchers, outdir)

    (outdir / "expected_command.txt").write_text(
        f"{skilldir}/run.sh {folder}\n",
        encoding="utf-8",
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
