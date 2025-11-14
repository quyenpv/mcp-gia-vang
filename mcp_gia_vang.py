# mcp_gia_vang.py
# Server MCP tổng hợp cho giá vàng (SJC, Doji, PNJ, ...)
# Dựa trên mã nguồn từ github.com/TranTruongMMCII/gia-vang-hom-nay

from __future__ import annotations

import codecs
import json
import os
import re
import sys
import logging
from datetime import datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, cast
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

# Cấu hình logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("mcp-gia-vang")

# ===================================================================
# PHẦN 1: LOGIC TỪ CACHE.PY
# ===================================================================
try:
    import redis
except ImportError:
    redis = None  # type: ignore

Entry = Dict[str, object]
PriceSnapshot = Dict[str, Dict[str, Dict[str, Optional[int | str]]]]

_REDIS_CLIENT: Optional[Any] = None

def _default_cache_file() -> Path:
    """Xác định đường dẫn file cache mặc định. Sửa đổi để phù hợp với môi trường server."""
    # Trong môi trường server (như uvx), chúng ta có thể không có quyền ghi
    # Nhưng nếu chạy local, nó sẽ cố gắng lưu vào /tmp
    # Đối với uvx, Redis là lựa chọn tốt nhất
    return Path(os.getenv("PRICE_CACHE_FILE", "/tmp/last_prices.json"))

def _normalise_snapshot(data: object) -> PriceSnapshot:
    snapshot: PriceSnapshot = {}
    if not isinstance(data, dict):
        return snapshot

    for source, products in data.items():
        if not isinstance(products, dict):
            continue
        source_key = str(source)
        snapshot[source_key] = {}
        for product, payload in products.items():
            if not isinstance(payload, dict):
                continue
            product_key = str(product)
            # Đảm bảo giá trị là int hoặc None
            buy_val = payload.get("buy")
            sell_val = payload.get("sell")
            snapshot[source_key][product_key] = {
                "buy": int(buy_val) if isinstance(buy_val, (int, float, str) and str(buy_val).isdigit()) else None,
                "sell": int(sell_val) if isinstance(sell_val, (int, float, str) and str(sell_val).isdigit()) else None,
                "unit": payload.get("unit"),
            }
    return snapshot

def _get_redis_client() -> Optional[Any]:
    global _REDIS_CLIENT
    if redis is None:
        return None
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT

    url = os.getenv("REDIS_URL")
    host = os.getenv("REDIS_HOST")

    try:
        if url:
            client = redis.Redis.from_url(url, decode_responses=True)
        elif host:
            port = int(os.getenv("REDIS_PORT", "6379"))
            client = redis.Redis(
                host=host,
                port=port,
                username=os.getenv("REDIS_USERNAME") or None,
                password=os.getenv("REDIS_PASSWORD") or None,
                decode_responses=True,
            )
        else:
            return None

        client.ping()
    except (redis.RedisError, ValueError) as exc:
        logger.warning(f"[cache] Redis unavailable: {exc}")
        return None

    _REDIS_CLIENT = client
    logger.info("[cache] Redis client connected.")
    return _REDIS_CLIENT

def _redis_key() -> str:
    return os.getenv("REDIS_CACHE_KEY", "gold:last")

def _load_from_file(target: Path) -> PriceSnapshot:
    if not target.exists():
        return {}

    try:
        with target.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"[cache] Failed to load previous prices from file: {exc}")
        return {}

    return _normalise_snapshot(data)

def load_previous_prices(path: Optional[os.PathLike[str]] = None) -> PriceSnapshot:
    client = _get_redis_client()
    if client is not None:
        try:
            raw = client.get(_redis_key())
        except redis.RedisError as exc:
            logger.warning(f"[cache] Failed to read from Redis: {exc}")
        else:
            if not raw:
                return {}
            try:
                return _normalise_snapshot(json.loads(raw))
            except json.JSONDecodeError as exc:
                logger.warning(f"[cache] Invalid JSON in Redis cache: {exc}")
                return {}

    target = Path(path) if path else _default_cache_file()
    logger.info(f"[cache] Loading previous prices from file: {target}")
    return _load_from_file(target)

def _build_payload(entries: Iterable[Entry]) -> PriceSnapshot:
    payload: PriceSnapshot = {}
    for entry in entries:
        source = str(entry.get("source", "")).strip()
        product = str(entry.get("product", "")).strip()
        if not source or not product:
            continue

        payload.setdefault(source, {})[product] = {
            "buy": entry.get("buy"),
            "sell": entry.get("sell"),
            "unit": entry.get("unit"),
        }
    return payload

def save_current_prices(entries: Iterable[Entry], path: Optional[os.PathLike[str]] = None) -> None:
    payload = _build_payload(entries)
    serialized = json.dumps(payload, ensure_ascii=False)

    client = _get_redis_client()
    redis_success = False
    if client is not None:
        try:
            client.set(_redis_key(), serialized)
            redis_success = True
            logger.info("[cache] Saved current prices to Redis.")
        except redis.RedisError as exc:
            logger.warning(f"[cache] Failed to write to Redis: {exc}")

    explicit_path = path is not None or os.getenv("PRICE_CACHE_FILE")
    if redis_success and not explicit_path:
        return  # Ưu tiên Redis và không cần lưu file nếu không được chỉ định rõ

    target = Path(path) if path else _default_cache_file()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            handle.write(serialized)
        logger.info(f"[cache] Saved current prices to file: {target}")
    except OSError as exc:
        logger.warning(f"[cache] Failed to save prices to file {target}: {exc}")

# ===================================================================
# PHẦN 2: LOGIC TỪ SOURCES.PY
# ===================================================================
SJC_URL = "https://sjc.com.vn/GoldPrice/Services/PriceService.ashx"
DOJI_XML_URL = "http://update.giavang.doji.vn/banggia/doji_92411/92411"
PHU_QUY_URL = "https://phuquygroup.vn/"
NGOC_THAM_URL = "https://ngoctham.com/bang-gia-vang/"
PNJ_URL = "https://edge-api.pnj.io/ecom-frontend/v1/get-gold-price?zone=00"

VNĐ_PER_CHI = "k VND/chỉ"

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
}

SJC_TARGETS = {
    "Vàng SJC 0.5 chỉ, 1 chỉ, 2 chỉ": "SJC miếng 0.5-2 chỉ",
    "Vàng nhẫn SJC 99,99% 1 chỉ, 2 chỉ, 5 chỉ": "SJC nhẫn 9999 1-5 chỉ",
    "Vàng nhẫn SJC 99,99% 0.5 chỉ, 0.3 chỉ": "SJC nhẫn 9999 0.3-0.5 chỉ",
}

DOJI_TARGETS = {
    "nhẫn tròn 9999": "Doji nhẫn tròn 9999",
}

PHU_QUY_TARGETS = {
    "nhẫn tròn phú quý": "Phú Quý nhẫn tròn 999.9",
}

NGOC_THAM_TARGETS = {
    "nhẫn 999.9": "Ngọc Thẩm nhẫn 999.9",
}

PNJ_TARGETS = {
    "N24K": "PNJ nhẫn trơn 999.9",
    "TL": "PNJ phúc lộc tài 999.9",
}

SOURCE_PRIORITY = {
    "SJC": 0,
    "Doji": 1,
    "PNJ": 2,
    "Phú Quý": 3,
    "Ngọc Thẩm": 4,
}

def _clean_number(value: object, *, multiplier: float = 1.0, divisor: float = 1.0) -> Optional[int]:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        numeric = float(value)
    elif isinstance(value, str):
        cleaned = re.sub(r"[^\d]", "", value)
        if not cleaned:
            return None
        numeric = float(cleaned)
    else:
        return None

    numeric = numeric * multiplier / divisor
    return int(round(numeric))

def _request_text(url: str, *, max_attempts: int = 3) -> Optional[str]:
    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=10)
            response.raise_for_status()
            content = response.content
            if content.startswith(codecs.BOM_UTF8):
                return content.decode("utf-8-sig")

            encoding = response.encoding or "utf-8"
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                return content.decode("utf-8", errors="replace")
        except requests.RequestException as exc:
            last_exc = exc
            continue

    if last_exc:
        logger.warning(f"[sources] Request failed for {url}: {last_exc}")
    return None

def _request_json(url: str) -> Optional[dict]:
    text = _request_text(url)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning(f"[sources] Failed to decode JSON from {url}: {exc}")
        return None

def fetch_sjc_prices() -> List[Dict[str, object]]:
    payload = _request_json(SJC_URL)
    if payload is None:
        return []

    results: List[Dict[str, object]] = []
    for item in payload.get("data", []):
        type_name = item.get("TypeName", "")
        if type_name not in SJC_TARGETS:
            continue
        if item.get("BranchName") != "Hồ Chí Minh":
            continue

        buy_vnd = _clean_number(item.get("BuyValue"), divisor=10) or _clean_number(item.get("Buy"), multiplier=100)
        sell_vnd = _clean_number(item.get("SellValue"), divisor=10) or _clean_number(item.get("Sell"), multiplier=100)

        results.append(
            {
                "source": "SJC",
                "product": SJC_TARGETS[type_name],
                "buy": buy_vnd,
                "sell": sell_vnd,
                "unit": VNĐ_PER_CHI,
            }
        )
    return results

def fetch_doji_prices() -> List[Dict[str, object]]:
    xml_text = _request_text(DOJI_XML_URL)
    if not xml_text:
        return []

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        logger.warning(f"[sources] Failed to parse Doji XML: {exc}")
        return []

    results: List[Dict[str, object]] = []
    for row in root.findall(".//Row"):
        name = row.attrib.get("Name", "").lower()
        target_label = None
        for needle, label in DOJI_TARGETS.items():
            if needle in name:
                target_label = label
                break
        if not target_label:
            continue

        buy_vnd = _clean_number(row.attrib.get("Buy"), multiplier=1000)
        sell_vnd = _clean_number(row.attrib.get("Sell"), multiplier=1000)

        results.append(
            {
                "source": "Doji",
                "product": target_label,
                "buy": buy_vnd,
                "sell": sell_vnd,
                "unit": VNĐ_PER_CHI,
            }
        )
    return results

def fetch_phu_quy_prices() -> List[Dict[str, object]]:
    html = _request_text(PHU_QUY_URL)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    results: List[Dict[str, object]] = []
    seen: set[str] = set()

    for row in soup.select("#priceList tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        name = cells[0].get_text(strip=True)
        key = name.lower()

        target_label = None
        for needle, label in PHU_QUY_TARGETS.items():
            if needle in key:
                target_label = label
                break
        if not target_label or target_label in seen:
            continue

        seen.add(target_label)
        buy_vnd = _clean_number(cells[1].get_text(strip=True))
        sell_vnd = _clean_number(cells[2].get_text(strip=True))

        results.append(
            {
                "source": "Phú Quý",
                "product": target_label,
                "buy": buy_vnd,
                "sell": sell_vnd,
                "unit": VNĐ_PER_CHI,
            }
        )
    return results

def fetch_ngoc_tham_prices() -> List[Dict[str, object]]:
    html = _request_text(NGOC_THAM_URL)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    results: List[Dict[str, object]] = []
    seen: set[str] = set()

    for row in soup.select("#gold-price-menu table.price-table tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        name = cells[0].get_text(strip=True)
        key = name.lower()

        target_label = None
        for needle, label in NGOC_THAM_TARGETS.items():
            if needle in key:
                target_label = label
                break
        if not target_label or target_label in seen:
            continue

        seen.add(target_label)
        buy_vnd = _clean_number(cells[1].get_text(strip=True))
        sell_vnd = _clean_number(cells[2].get_text(strip=True))

        results.append(
            {
                "source": "Ngọc Thẩm",
                "product": target_label,
                "buy": buy_vnd,
                "sell": sell_vnd,
                "unit": VNĐ_PER_CHI,
            }
        )
    return results

def fetch_pnj_prices() -> List[Dict[str, object]]:
    payload = _request_json(PNJ_URL)
    if payload is None:
        return []

    results: List[Dict[str, object]] = []
    for item in payload.get("data", []):
        code = item.get("masp")
        target_label = PNJ_TARGETS.get(code)
        if not target_label:
            continue

        buy_vnd = _clean_number(item.get("giamua"), multiplier=1000)
        sell_vnd = _clean_number(item.get("giaban"), multiplier=1000)

        results.append(
            {
                "source": "PNJ",
                "product": target_label,
                "buy": buy_vnd,
                "sell": sell_vnd,
                "unit": VNĐ_PER_CHI,
            }
        )
    return results

def fetch_gold_price_entries() -> List[Dict[str, object]]:
    """Hàm chính lấy tất cả giá vàng từ các nguồn."""
    entries: List[Dict[str, object]] = []
    for fetcher in (
        fetch_sjc_prices,
        fetch_doji_prices,
        fetch_phu_quy_prices,
        fetch_ngoc_tham_prices,
        fetch_pnj_prices,
    ):
        try:
            entries.extend(fetcher())
        except Exception as e:
            logger.error(f"Failed to fetch from {fetcher.__name__}: {e}")

    if not entries:
        return []

    entries.sort(key=lambda item: (SOURCE_PRIORITY.get(str(item["source"]), 99), str(item["product"])))
    return entries

# ===================================================================
# PHẦN 3: LOGIC TỪ DISPATCHER.PY (ĐỊNH DẠNG)
# ===================================================================

def _format_currency(value: Optional[int]) -> str:
    if value is None:
        return "--"
    thousands = int(round(value / 1000))
    return f"{thousands:,}".replace(",", ".")

def _format_difference(current: Optional[int], previous: Optional[int]) -> Optional[str]:
    if current is None or previous is None:
        return None

    diff = current - previous
    diff_thousands = int(round(diff / 1000))
    if diff_thousands == 0:
        return "0"
    return f"{diff_thousands:+,}".replace(",", ".")

def _format_value(current: Optional[int], previous: Optional[int]) -> str:
    current_text = _format_currency(current)
    if current is None:
        return current_text
    if previous is None:
        return current_text

    # Bỏ qua _format_currency(previous) vì không cần hiển thị
    diff_text = _format_difference(current, previous)
    if diff_text is None:
        return current_text
    return f"{current_text} ({diff_text})"

def _format_section(
    source: str,
    rows: List[Dict[str, object]],
    previous_lookup: PriceSnapshot,
) -> str:
    unit = rows[0].get("unit", "k VND/chỉ")
    previous_by_product = previous_lookup.get(source, {})
    product_width = max(len("Sản phẩm"), *(len(str(row["product"])) for row in rows))
    buy_values: List[str] = []
    sell_values: List[str] = []

    for row in rows:
        product_name = str(row["product"])
        previous_entry = previous_by_product.get(product_name, {})
        buy_values.append(_format_value(cast(Optional[int], row.get("buy")), cast(Optional[int], previous_entry.get("buy"))))
        sell_values.append(_format_value(cast(Optional[int], row.get("sell")), cast(Optional[int], previous_entry.get("sell"))))

    buy_width = max(len("Mua"), *(len(value) for value in buy_values))
    sell_width = max(len("Bán"), *(len(value) for value in sell_values))

    lines = [
        f"{source} ({unit})",
        f"{'Sản phẩm'.ljust(product_width)}  {'Mua'.rjust(buy_width)}  {'Bán'.rjust(sell_width)}",
        f"{'-' * product_width}  {'-' * buy_width}  {'-' * sell_width}",
    ]

    for row, buy_text, sell_text in zip(rows, buy_values, sell_values):
        product = str(row["product"]).ljust(product_width)
        buy = buy_text.rjust(buy_width)
        sell = sell_text.rjust(sell_width)
        lines.append(f"{product}  {buy}  {sell}")

    # Sử dụng markdown code block thay vì <pre> cho MCP
    return "```\n" + '\n'.join(lines) + "\n```"

def _build_message(
    entries: List[Dict[str, object]],
    previous_lookup: PriceSnapshot,
) -> str:
    tz = timezone(timedelta(hours=7))
    timestamp = datetime.now(tz).strftime("%d/%m/%Y %H:%M:%S GMT+7")
    header = f"Cập nhật giá vàng lúc {timestamp}:"

    sections: List[str] = []
    for source, group in groupby(entries, key=lambda item: str(item["source"])):
        rows = list(group)
        if rows:
            sections.append(_format_section(source, rows, previous_lookup))

    return "\n\n".join([header, *sections]) if sections else header

# ===================================================================
# PHẦN 4: MCP SERVER VÀ TOOLS
# ===================================================================

# Khởi tạo MCP Server
server = FastMCP("gia-vang")

@server.tool()
def get_gold_prices() -> str:
    """
    Lấy giá vàng từ các nguồn (SJC, Doji, PNJ...) và trả về một
    thông báo văn bản đã định dạng, bao gồm so sánh với
    lần cập nhật trước (nếu có).
    
    Cache (Redis hoặc file) được sử dụng để lưu giá trị trước đó.
    """
    try:
        logger.info("Tool 'get_gold_prices' starting...")
        
        # 1. Lấy giá hiện tại
        current_entries = fetch_gold_price_entries()
        if not current_entries:
            logger.warning("No prices retrieved from any source.")
            return "Lỗi: Không thể lấy được dữ liệu giá vàng từ bất kỳ nguồn nào."
        
        logger.info(f"Fetched {len(current_entries)} entries successfully.")

        # 2. Tải giá trước đó từ cache
        previous_lookup = load_previous_prices()
        if not previous_lookup:
            logger.info("No previous prices found in cache.")
        else:
            logger.info("Loaded previous prices from cache.")

        # 3. Xây dựng tin nhắn so sánh
        message = _build_message(current_entries, previous_lookup)
        
        # 4. Lưu giá hiện tại vào cache
        save_current_prices(current_entries)
        
        logger.info("Tool 'get_gold_prices' completed.")
        return message
        
    except Exception as e:
        logger.error(f"Error in 'get_gold_prices' tool: {e}", exc_info=True)
        return f"Lỗi máy chủ khi đang lấy giá vàng: {e}"

# ===================================================================
# PHẦN 5: MAIN ENTRY POINT (ĐỂ CHẠY BẰNG uvx)
# ===================================================================

def main():
    """Hàm main để chạy server (được gọi bởi uvx)."""
    logger.info("Đang khởi động MCP Gia Vang Server...")
    # Sửa lỗi UTF-8 trên Windows (nếu chạy local)
    if sys.platform == "win32":
        sys.stderr.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
        
    server.run(transport="stdio")

if __name__ == "__main__":
    main()