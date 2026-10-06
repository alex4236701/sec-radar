import asyncio
import html
from io import BytesIO
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
import logging
import math
import os
import re
from statistics import mean
import threading
import time
from urllib.parse import quote, urljoin, urlparse
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
import feedparser
import pandas_market_calendars as mcal
import requests

LOG = logging.getLogger("news_bot")
ET = ZoneInfo("America/New_York")
TW = ZoneInfo("Asia/Taipei")
MAX_DAYS = 366
NEWS_LIMIT = 200
AUDIT_LIMIT = 200
MAX_HISTORY_FILES = 20
MAX_NEWS_REQUESTS = 30

# IBKR 設定：本機 TWS（Port 7497）
IB_HOST = "127.0.0.1"
IB_PORT = int(os.environ.get("IB_PORT", 7497))

WIRE_DOMAINS = {"prnewswire.com", "businesswire.com", "globenewswire.com",
                "newswire.ca", "accesswire.com", "newsfilecorp.com"}
WIRE_QUERY = "(" + " OR ".join("site:" + d for d in sorted(WIRE_DOMAINS)) + ")"
COMPANY_NEWS_ALIASES = {
    "SIMO": ("Silicon Motion", "Silicon Motion Technology"),
    "BB": ("BlackBerry Limited", "BlackBerry Ltd", "BlackBerry QNX")
}

TARGET_ITEMS = {
    "1.01", "1.02", "1.03", "2.01", "2.02", "2.03", "2.04", "2.05",
    "2.06", "3.01", "3.02", "3.03", "4.01", "4.02", "5.02", "8.01"
}
FORMS = {
    "10-Q", "10-K", "20-F", "40-F", "6-K", "S-3", "F-3",
    "424B5", "424B7", "EFFECT", "SC 13D", "SC 13G", "SCHEDULE 13D", "SCHEDULE 13G"
}

TARGET_ITEMS.add("5.07")
FORMS.update({"S-8", "DEF 14A", "DEFA14A", "S-1", "S-3ASR", "S-4", "F-1", "F-4", "424B4"})

# 核心修復：補齊所有全域鎖與快取變數的前綴底線，徹底根除 NameError
_sec_lock = threading.Lock()
_cache_lock = threading.Lock()
_spot_lock = asyncio.Lock()  # 現貨排隊鎖
_opt_lock = asyncio.Lock()   # 期權排隊鎖
_last_sec_request = 0.0
_company_cache = (0.0, {})


@dataclass
class Result:
    items: list = field(default_factory=list)
    status: str = "OK"
    notes: list = field(default_factory=list)
    excluded: list = field(default_factory=list)


def ticker_value(value):
    value = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,14}", value):
        raise ValueError("股票代號格式不正確")
    return value


def format_num_units(val: float) -> str:
    if val >= 100_000_000:
        n = val / 100_000_000
        n_str = f"{int(n)}" if n.is_integer() else f"{n:.2f}"
        return f"${n_str} 億"
    elif val >= 10_000:
        n = val / 10_000
        n_str = f"{int(n)}" if n.is_integer() else f"{n:.2f}"
        return f"${n_str} 萬"
    n_str = f"{int(val)}" if float(val).is_integer() else f"{val:.2f}"
    return f"${n_str}"


def format_price_num(val: float) -> str:
    if float(val).is_integer():
        return f"${int(val)}"
    return f"${val:.2f}"


def format_pct_str(val: float) -> str:
    sign = "+" if val > 0 else ""
    if float(val).is_integer():
        return f"{sign}{int(val)}%"
    return f"{sign}{val:.2f}%"


def format_ratio_str(val: float) -> str:
    if float(val).is_integer():
        return f"{int(val)}"
    return f"{val:.2f}"


def format_iv_str(val: float | None) -> str:
    if val is None or val <= 0.01:
        return "造市商未提供"
    pct = val * 100 if val <= 5.0 else val
    if float(pct).is_integer():
        return f"{int(pct)}%"
    return f"{pct:.2f}%"


def get_market_session_meta():
    now_et = datetime.now(ET)
    t = now_et.time()
    if dt_time(9, 30) <= t < dt_time(16, 0):
        return {
            "title_suffix": " ｜ 盤中即時主力戰報",
            "vol_label": "當日成交量 PCR",
            "trade_label": "當日合約總成交",
            "oi_label": "全場累積未平倉",
            "footer_mode": "即時盤中解析釋放",
            "section_order": "【盤中主力異常激進大單 (>= $3 萬)】",
            "section_cluster": "【盤中主力重兵集結合約】"
        }
    elif dt_time(16, 0) <= t < dt_time(20, 0):
        return {
            "title_suffix": " ｜ 收盤後當日現貨期權戰報",
            "vol_label": "今日收盤成交量 PCR",
            "trade_label": "今日合約總成交",
            "oi_label": "全場累積未平倉 (今日最新)",
            "footer_mode": "收盤後當日結算完成",
            "section_order": "【今日主力異常激進大單 (>= $3 萬)】",
            "section_cluster": "【今日主力重兵集結合約】"
        }
    else:
        return {
            "title_suffix": " ｜ 開盤前主力籌碼戰報 (前一交易日結算)",
            "vol_label": "前日成交量 PCR",
            "trade_label": "前日合約總成交",
            "oi_label": "全場累積未平倉 (前日底冊)",
            "footer_mode": "盤前戰報融合完成",
            "section_order": "【前日主力異常激進大單 (>= $3 萬)】",
            "section_cluster": "【前日主力重兵集結合約】"
        }


def date_range(start_text=None, end_text=None):
    def parse(value):
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", value):
            raise ValueError("日期請使用 YYYY-MM-DD")
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("日期不存在，請檢查年月日") from None

    end = parse(end_text) or datetime.now(ET).date()
    start = parse(start_text) or end - timedelta(days=29)
    if start > end:
        raise ValueError("起始日期不能晚於結束日期")
    if start.year < 1900 or end.year > 9998:
        raise ValueError("年份須介於 1900 至 9998")
    if (end - start).days + 1 > MAX_DAYS:
        raise ValueError("單次最多查詢 366 天，請縮小日期區間")
    return start, end


def sec_json(url):
    global _last_sec_request
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        raise ValueError("請設定 SEC_USER_AGENT，包含你的聯絡 Email")
    with _sec_lock:
        time.sleep(max(0, 0.25 - (time.monotonic() - _last_sec_request)))
        _last_sec_request = time.monotonic()
        response = requests.get(url, headers={"User-Agent": ua}, timeout=(5, 15))
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("SEC 回傳格式錯誤")
    return payload


def sec_text(url):
    global _last_sec_request
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        raise ValueError("請設定 SEC_USER_AGENT，包含你的聯絡 Email")
    with _sec_lock:
        time.sleep(max(0, 0.25 - (time.monotonic() - _last_sec_request)))
        _last_sec_request = time.monotonic()
        response = requests.get(url, headers={"User-Agent": ua}, timeout=(5, 15))
    response.raise_for_status()
    return response.text


def fetch_filing_exhibits(cik, accession, index_url=None):
    cik_num = int(cik)
    acc_clean = accession.replace("-", "")
    if not index_url:
        index_url = f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{acc_clean}/{accession}-index.htm"

    try:
        html = sec_text(index_url)
        row_pattern = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
        td_pattern = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
        a_pattern = re.compile(r'<a\s+[^>]*href=["\'](.*?)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)

        exhibits = []
        for tr in row_pattern.findall(html):
            tds = td_pattern.findall(tr)
            if len(tds) < 4:
                continue
            raw_desc = re.sub(r"<[^>]+>", "", tds[1])
            desc = re.sub(r"&nbsp;?", " ", raw_desc, flags=re.I).strip()
            doc_type = re.sub(r"<[^>]+>", "", tds[3]).strip().upper()

            if re.match(r"EX-(?:1|2|3|4|10|99)(?:\.\d+)*(?!\w)", doc_type):
                a_match = a_pattern.search(tds[2])
                if not a_match:
                    continue
                href = a_match.group(1).strip()
                name = re.sub(r"<[^>]+>", "", a_match.group(2)).strip()
                full_url = urljoin(index_url, href)
                title = desc if desc else name
                exhibits.append((doc_type, title, full_url))
        return exhibits
    except Exception:
        return []


def clean_company_name(name):
    previous = None
    while previous != name:
        previous = name
        name = re.sub(r"(?:,\s*|\s+)(?:INCORPORATED|INC|CORPORATION|CORP|LIMITED|LTD|PLC|LLC|CO)\.?$", "", name.strip(), flags=re.I)
    return name.strip()


def company_info(ticker):
    global _company_cache
    with _cache_lock:
        timestamp, mapping = _company_cache
        if time.monotonic() - timestamp > 86400 or not mapping:
            data = sec_json("https://www.sec.gov/files/company_tickers.json")
            mapping = {
                str(v["ticker"]).upper(): v
                for v in data.values()
                if isinstance(v, dict) and all(k in v for k in ("ticker", "cik_str", "title"))
            }
            if not mapping:
                raise ValueError("SEC 公司清單格式錯誤")
            _company_cache = (time.monotonic(), mapping)
    item = mapping.get(ticker)
    return (str(item["cik_str"]).zfill(10), clean_company_name(item["title"])) if item else (None, None)


def company_aliases(ticker, name):
    aliases = list(COMPANY_NEWS_ALIASES.get(ticker, ()))
    if name and name.casefold() != ticker.casefold():
        aliases.append(name)
    if not aliases:
        aliases.append(name if name else ticker)
    return tuple(dict.fromkeys(aliases))


def words(text):
    return " ".join(re.findall(r"\w+", text.casefold()))


LIST_CONTEXT = re.compile(r"\b(?:among|including|such as|like|peers?|rivals?|competitors?|companies|stocks|names|customers?|mentions?|lists?|listed|e g)\b")
TITLE_START = re.compile(r"(?:(?:update|updated|breaking|exclusive|correcting|correction|corrected|press release|\d+)\s*)+")
STRONG_PREFIX = re.compile(r"\b(?:and|partners with|partnership with|to acquire|acquires|acquisition of|merger with|agreement with|sues|charges|investigates)\s*$")
# 多字公司名前面一個字必須是介系詞、連接詞這類「功能詞」，
# 避免「Optimum Energy Fuels」這種別家公司名稱裡剛好包含本公司名的情況
PREV_FUNCTION_WORDS = {"of", "by", "from", "with", "and", "at", "for", "to", "as", "in", "on", "the",
                       "about", "vs", "versus", "between", "into", "says", "said", "update", "updated"}
CORP_SUFFIX = re.compile(r"\s*(?:inc|incorporated|corp|corporation|ltd|limited|holdings|plc|group|co|s)\b")


def company_related(title, aliases, ticker=None):
    """標題是否在講這家公司。多字公司名出現在任何位置都算（列舉語境除外）；
    單字公司名（例如 Target、Block）容易和一般單字撞名，維持較嚴格的判斷。"""
    if ticker and re.search(
        r"\((?:nyse(?: american| arca| mkt)?|nasdaq|tsxv?|otc(?:qx|qb)?|cboe|asx)\s*:\s*" + re.escape(ticker) + r"\)",
        title, re.I):
        return True
    normalized = words(title)
    for alias in aliases:
        needle = words(alias)
        if not needle:
            continue
        multi_word = " " in needle
        for match in re.finditer(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", normalized):
            prefix = normalized[: match.start()].strip()
            if not prefix or TITLE_START.fullmatch(prefix):
                return True
            recent = " ".join(prefix.split()[-6:])
            if LIST_CONTEXT.search(recent):
                continue
            if multi_word and prefix.split()[-1] in PREV_FUNCTION_WORDS:
                return True
            if STRONG_PREFIX.search(prefix) or CORP_SUFFIX.match(normalized[match.end():]):
                return True
    return False


LAW_FIRM = re.compile(r"\b(?:law firm|law offices|rosen law|pomerantz|robbins geller|glancy prongay|bronstein|frank r\.? cruz)\b", re.I)
SOLICITATION = re.compile(r"\b(?:shareholder alert|investor alert|reminds investors|lead plaintiff|encourages investors|urges investors|recover losses)\b", re.I)
LEGAL_CONTEXT = re.compile(r"\b(?:shareholders?|investors?|securities|class action|lawsuit|litigation)\b", re.I)
GENERIC_OUTREACH = re.compile(r"\b(?:deadline|contact)\b", re.I)
PERIOD = r"(?:(?:first|second|third|fourth)[\s-]+quarter|q[1-4]|full[\s-]+year|fiscal(?:[\s-]+year)?|annual|quarterly)"
RESULT_TOPIC = rf"(?:financial[\s-]+results|{PERIOD}(?:\s+(?:fiscal\s+)?\d{{4}})?(?:\s+financial)?\s+results|earnings|revenue)"
RESULTS = re.compile(rf"\b(?:reports?|reported|announces?|announced|releases?)\b.{{0,100}}\b{RESULT_TOPIC}\b", re.I)
RESULT_DATE_NOTICE = re.compile(rf"\b(?:announces?|sets?|confirms?|schedules?)\b.{{0,50}}\bdate\s+(?:for|of)\b.{{0,100}}\b{RESULT_TOPIC}\b|\b(?:earnings|results)(?:\s+release)?\s+date\b", re.I)
EVENT_NOTICE = re.compile(r"\b(?:to attend|to participate|to present|will present|will attend|upcoming investor conferences?|call details|earnings (?:release )?date|results release date|to report|will report|schedules?|conference call|webcast)\b", re.I)
FUTURE_RESULTS = re.compile(r"\b(?:call details|earnings (?:release )?date|results release date|date (?:for|of)|to report|will report|schedules?|to announce|will announce)\b", re.I)


def is_noise(title):
    if SOLICITATION.search(title):
        return True
    if LAW_FIRM.search(title) and LEGAL_CONTEXT.search(title) and GENERIC_OUTREACH.search(title):
        return True
    if LAW_FIRM.search(title) and re.search(r"\b(?:investigat\w*|lawsuit|class action|claims?)\b", title, re.I):
        return True
    if EVENT_NOTICE.search(title) or RESULT_DATE_NOTICE.search(title):
        actual_results = RESULTS.search(title) and not (FUTURE_RESULTS.search(title) or RESULT_DATE_NOTICE.search(title))
        material_event = re.search(r"\b(?:raises guidance|lowers guidance|cuts guidance|acquisition|merger|settles|settlement)\b", title, re.I)
        return not bool(actual_results or material_event)
    return False


SCORES = [
    (r"\b(?:acquisition|acquires|to acquire|merger|takeover|divestiture)\b", 5),
    (r"\b(?:supply agreement|purchase order|contract|financing|offering|bankruptcy|settlement|subpoena|investigation|regulatory approval)\b", 4),
    (r"\b(?:guidance|outlook|financial results|quarterly results|earnings|resigns|steps down|ceo|cfo)\b", 3),
    (r"\b(?:launches|certification|milestone)\b", 2),
]


def source_domain(entry):
    href = entry.get("source", {}).get("href") or entry.get("link", "")
    try:
        host = (urlparse(href).hostname or "").lower()
    except ValueError:
        return None
    return next((d for d in WIRE_DOMAINS if host == d or host.endswith("." + d)), None)


def safe_url(url):
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("https", "http") or not parsed.hostname:
            return None
        return quote(url, safe=":/?=&%#@+;,$!~*'[]")
    except (TypeError, ValueError):
        return None


def read_rss(query):
    response = requests.get(
        "https://news.google.com/rss/search",
        params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=(5, 15),
    )
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    if not feed.get("version") or (feed.get("bozo") and not feed.get("entries")):
        raise ValueError("RSS 格式或解析錯誤")
    return feed.get("entries", []), bool(feed.get("bozo"))


def fetch_wire_news(ticker, aliases, start, end):
    if not aliases:
        raise ValueError("無法確認公司名稱，請設定公司新聞別名")
    windows = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=30), end)
        windows.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    windows.reverse()
    notes, collected = [], []
    failures = successes = calls = 0
    name_query = "(" + " OR ".join('"' + a.replace('"', " ") + '"' for a in aliases) + ")"
    while windows and calls < MAX_NEWS_REQUESTS:
        lo, hi = windows.pop(0)
        query = f"{name_query} {WIRE_QUERY} after:{lo - timedelta(days=1)} before:{hi + timedelta(days=2)}"
        calls += 1
        try:
            entries, damaged = read_rss(query)
            successes += 1
            if damaged:
                notes.append("部分 RSS 格式異常，僅採用可解析內容")
        except (requests.RequestException, ValueError, TypeError):
            failures += 1
            continue
        if len(entries) >= 100:
            if lo < hi:
                midpoint = lo + (hi - lo) // 2
                windows[0:0] = [(midpoint + timedelta(days=1), hi), (lo, midpoint)]
            else:
                notes.append("單日搜尋達回傳上限，可能缺少文章")
        collected.extend(entries)
    if windows:
        notes.append("已達分段查詢次數上限，部分區間未完整檢索")
    if failures:
        notes.append(f"有 {failures} 個查詢失敗，結果可能不完整")
    rows, seen = [], set()
    skipped = not_related = noise_dropped = 0
    excluded = []
    for entry in collected:
        try:
            title = entry.get("title", "").strip()
            source = entry.get("source", {}).get("title", "")
            suffix = " - " + source
            if source and title.casefold().endswith(suffix.casefold()):
                title = title[: -len(suffix)].strip()
            if not company_related(title, aliases, ticker):
                not_related += 1
                parsed_ex = entry.get("published_parsed")
                ex_date = datetime(*parsed_ex[:3]).date().isoformat() if parsed_ex else "日期不明"
                excluded.append((ex_date, source or "來源不明", title))
                continue
            if is_noise(title):
                noise_dropped += 1
                continue
            domain = source_domain(entry)
            link = safe_url(entry.get("link", ""))
            parsed = entry.get("published_parsed")
            if not domain or not link or not parsed:
                skipped += 1
                continue
            published = datetime(*parsed[:6], tzinfo=timezone.utc)
            local = published.astimezone(ET)
            if not start <= local.date() <= end:
                continue
            key = (words(title), local.date())
            if key in seen:
                continue
            seen.add(key)
            score = 1 + sum(weight for pattern, weight in SCORES if re.search(pattern, title, re.I))
            rows.append(dict(title=title, link=link, source=domain, published=local.strftime("%Y-%m-%d %H:%M %Z"), score=score, pub_dt=published))
        except (ValueError, TypeError, KeyError, AttributeError):
            skipped += 1
    if skipped:
        notes.append(f"{skipped} 筆候選缺少可驗證的來源、時間或連結，未納入")
    rows.sort(key=lambda row: row["pub_dt"], reverse=True)
    status = "ERROR" if failures and not successes else "PARTIAL" if notes else "OK" if rows else "EMPTY"
    excluded = sorted(set(excluded), reverse=True)
    notes.insert(0, f"Google RSS 原始回傳 {len(collected)} 則（含重複）；標題判定與本公司無關 {len(excluded)} 則（清單附在 TXT 最後）；"
                    f"法說會通知、律師事務所招攬等雜訊 {noise_dropped} 則")
    return Result(rows[:NEWS_LIMIT], status, list(dict.fromkeys(notes)), excluded)


def filing_rows(block):
    required = ("form", "filingDate", "accessionNumber", "primaryDocument")
    if not isinstance(block, dict) or not all(isinstance(block.get(k), list) for k in required):
        raise ValueError("SEC 申報欄位缺失")
    size = len(block["form"])
    if any(len(block[k]) != size for k in required):
        raise ValueError("SEC 申報欄位長度不一致")
    for i in range(size):
        row = {k: block[k][i] for k in required}
        row["items"] = block.get("items", [])[i] if i < len(block.get("items", [])) else ""
        descriptions = block.get("primaryDocDescription", [])
        row["description"] = descriptions[i] if isinstance(descriptions, list) and i < len(descriptions) else ""
        yield row


def fetch_target_filings(cik, start, end):
    data = sec_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    filings = data.get("filings", {})
    blocks = [filings.get("recent")]
    notes, selected = [], []
    for meta in filings.get("files", []):
        try:
            lo, hi = date.fromisoformat(meta["filingFrom"]), date.fromisoformat(meta["filingTo"])
            if lo <= end and hi >= start:
                selected.append(meta)
        except (ValueError, KeyError, TypeError):
            notes.append("部分 SEC 歷史索引無法解析")
    selected.sort(key=lambda m: m["filingTo"], reverse=True)
    if len(selected) > MAX_HISTORY_FILES:
        notes.append("歷史檔案超過單次上限，請縮小日期範圍")
    for meta in selected[:MAX_HISTORY_FILES]:
        name = meta.get("name", "")
        if not re.fullmatch(r"CIK\d+-submissions-\d+\.json", name):
            notes.append("部分歷史檔案名稱無法驗證")
            continue
        try:
            blocks.append(sec_json("https://data.sec.gov/submissions/" + name))
        except (requests.RequestException, ValueError):
            notes.append("部分 SEC 歷史檔案讀取失敗")
    rows, seen = [], set()
    valid_blocks = 0
    for block in blocks:
        try:
            candidates = list(filing_rows(block))
            valid_blocks += 1
        except (ValueError, TypeError):
            notes.append("部分 SEC 申報資料格式錯誤")
            continue
        for row in candidates:
            try:
                filed = date.fromisoformat(row["filingDate"])
                form = row["form"].upper()
                base = form.removesuffix("/A")
                items = set(re.findall(r"\d+\.\d+", row["items"] or ""))
                if not start <= filed <= end:
                    continue
                if base not in FORMS and base != "8-K":
                    continue
                accession = row["accessionNumber"]
                if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
                    raise ValueError("invalid accession")
                if accession in seen:
                    continue
                seen.add(accession)
                base_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"
                row["index_link"] = base_url + accession + "-index.htm"
                document = row.get("primaryDocument") or ""
                parts = document.split("/") if isinstance(document, str) else []
                usable = bool(parts) and all(p not in ("", ".", "..") for p in parts) and not any(c in document for c in (":", "\\", "?", "#"))
                row["document_link"] = base_url + quote(document, safe="/") if usable else None
                row["link"] = row["document_link"] or row["index_link"]
                rows.append(row)
            except (ValueError, TypeError, AttributeError):
                notes.append("部分 SEC 申報欄位無效，未納入")
    rows.sort(key=lambda r: (r["filingDate"], r["accessionNumber"]), reverse=True)
    if len(rows) > AUDIT_LIMIT:
        notes.append(f"找到 {len(rows)} 筆符合條件的申報，顯示最新 {AUDIT_LIMIT} 筆")

    target_rows = rows[:AUDIT_LIMIT]
    for r in target_rows:
        base = r["form"].upper().removesuffix("/A")
        if base in ("6-K", "8-K"):
            r["exhibits"] = fetch_filing_exhibits(cik, r["accessionNumber"], r.get("index_link"))
        else:
            r["exhibits"] = []

    status = "ERROR" if not valid_blocks else "PARTIAL" if notes else "OK" if rows else "EMPTY"
    return Result(target_rows, status, list(dict.fromkeys(notes)))


def clip(text, limit):
    text = str(text)
    if len(text.encode("utf-16-le")) // 2 <= limit:
        return text
    return text.encode("utf-16-le")[: (limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


def filing_field(row, number):
    base = row["form"].upper().removesuffix("/A")
    labels = {
        "6-K": "外國發行人資訊申報", "8-K": "美國發行人重大事件申報", "10-Q": "季度財務報告",
        "10-K": "年度財務報告", "20-F": "外國發行人年度報告／註冊文件", "40-F": "加拿大發行人年度報告／註冊文件",
        "S-3": "證券註冊文件", "F-3": "外國發行人證券註冊文件", "424B5": "招股說明書補充文件",
        "424B7": "招股說明書補充文件", "424B4": "招股說明書", "S-1": "證券註冊文件",
        "S-3ASR": "自動生效證券註冊文件", "S-4": "併購相關證券註冊文件", "F-1": "外國發行人證券註冊文件",
        "F-4": "外國發行人併購相關證券註冊文件", "EFFECT": "註冊生效通知", "SC 13D": "實益持股揭露",
        "SCHEDULE 13D": "實益持股揭露", "SC 13G": "實益持股簡式揭露", "SCHEDULE 13G": "實益持股簡式揭露"
    }
    label = labels.get(base, "SEC 申報文件")
    if row["form"].upper().endswith("/A"):
        label += "（修正版）"

    lines = [
        f"案號：`{row['accessionNumber']}`",
        f"類型：{label}",
    ]
    if base == "8-K" and row.get("items"):
        lines.append("申報項目：" + clip(row["items"], 100))

    exhibits = row.get("exhibits", [])
    if exhibits:
        lines.append("重大附件／新聞稿：")
        for doc_type, title, link in exhibits[:3]:
            lines.append(f"• [{doc_type}] [{clip(title, 80)}](<{link}>)")
    else:
        description = row.get("description")
        if isinstance(description, str) and description.strip() and description.strip().upper() != row["form"].upper():
            lines.append("SEC 文件描述：" + clip(description.strip(), 160))

    if row.get("document_link"):
        lines.append(f"[開啟 SEC 主文件](<{row['document_link']}>)")
    lines.append(f"[查看申報索引與附件](<{row['index_link']}>)")
    return f"{number}. {row['form']} · {row['filingDate']}", "\n".join(lines)


def build_embeds(title, description, fields, color):
    def new_embed():
        return discord.Embed(title=clip(title, 256), description=clip(description, 1200), color=color)

    embed, pages = new_embed(), []
    for name, value in fields:
        name, value = clip(name, 256), clip(value, 1024)
        units = len((str(embed.to_dict()) + name + value).encode("utf-16-le")) // 2
        if embed.fields and (len(embed.fields) >= 20 or units > 5500):
            pages.append(embed)
            embed = new_embed()
        embed.add_field(name=name or "—", value=value or "—", inline=False)
    pages.append(embed)
    return pages


# ----------------------------------------------------
# 現貨量價與結構分析運算引擎 (毫秒級盤口現價校準版)
# ----------------------------------------------------
def bar_date_str(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if hasattr(value, "year"):
        return value.isoformat()
    value = str(value)
    if len(value) == 8:
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value[:10]


def calc_spot_structure_sync(ticker: str):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    from ib_insync import IB, Stock

    ib = IB()
    try:
        try:
            ib.connect(IB_HOST, IB_PORT, clientId=703, timeout=12, readonly=True)
            ib.reqMarketDataType(1)  # 全力啟用付費即時串流數據
        except Exception:
            raise ValueError(f"無法連線至本地 TWS，請確認 TWS 已登入且 API (Port {IB_PORT}) 已就緒")

        stock = Stock(ticker, "SMART", "USD")
        qualified = ib.qualifyContracts(stock)
        if not qualified:
            raise ValueError(f"找不到代號 `{ticker}` 的股票合約，請確認代號正確性")

        bars = ib.reqHistoricalData(
            qualified[0],
            endDateTime="",
            durationStr="2 M",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
            timeout=15
        )
        if not bars or len(bars) < 21:
            raise ValueError(f"`{ticker}` 歷史日 K 資料不足（需至少 21 個交易日）")

        calendar = mcal.get_calendar("NYSE")
        now_et = datetime.now(ET)
        schedule = calendar.schedule(start_date=(now_et - timedelta(days=60)).date(), end_date=now_et.date())

        eligible_sessions = []
        for idx, row in schedule.iterrows():
            sess_open = row.market_open.to_pydatetime().astimezone(ET)
            sess_close = row.market_close.to_pydatetime().astimezone(ET)
            eligible_sessions.append({"date": idx.date().isoformat(), "open": sess_open, "close": sess_close})

        last_bar_date = bar_date_str(bars[-1].date)
        is_today_bar = (eligible_sessions and last_bar_date == eligible_sessions[-1]["date"])

        if is_today_bar:
            cur_sess = eligible_sessions[-1]
            if now_et >= cur_sess["close"]:
                weight = 1.0
                status_label = "今日收盤數據"
            elif now_et <= cur_sess["open"]:
                weight = 1.0
                status_label = "開盤前（昨日收盤基準）"
            else:
                fraction = (now_et - cur_sess["open"]).total_seconds() / (cur_sess["close"] - cur_sess["open"]).total_seconds()
                minutes = max(0, min(1, fraction)) * 390
                knots = [(0, 0), (30, .22), (90, .38), (270, .66), (360, .82), (390, 1)]
                weight = 1.0
                for (a, x), (b, y) in zip(knots, knots[1:]):
                    if minutes <= b:
                        weight = max(.04, x + (minutes - a) / (b - a) * (y - x))
                        break
                status_label = "盤中即時估算"
        else:
            weight = 1.0
            status_label = "非交易時段（前一日收盤基準）"

        history = bars[-21:-1]
        current = bars[-1]

        mkt_p = None
        try:
            m_ticker = ib.reqMktData(qualified[0], "", False, False)
            for _ in range(8):
                ib.sleep(0.15)
                p = m_ticker.marketPrice() or m_ticker.last or getattr(m_ticker, "delayedLast", None) or m_ticker.close
                if p and not math.isnan(p) and p > 0:
                    mkt_p = float(p)
                    break
            ib.cancelMktData(qualified[0])
        except Exception:
            pass

        price = mkt_p if mkt_p else float(current.close)
        prev_close = float(history[-1].close)
        change_pct = (price / prev_close - 1) * 100

        avg_vol = mean(float(b.volume) for b in history)
        cur_vol = float(current.volume)
        ratio = cur_vol / (avg_vol * weight) if avg_vol > 0 else 0.0
        amount = cur_vol * price

        spread = float(current.high) - float(current.low)
        clv = ((2 * price - float(current.high) - float(current.low)) / spread) if spread > 0 else 0.0
        mid_k = (float(current.high) + float(current.low)) / 2

        high20 = max(float(b.high) for b in history)
        low20 = min(float(b.low) for b in history)

        if price >= high20:
            struct_status = "🔥 帶量突破前 20 日高點"
        elif price <= low20:
            struct_status = "🩸 帶量跌破前 20 日低點"
        else:
            dist_high = ((high20 / price) - 1) * 100
            dist_str = f"{int(dist_high)}%" if float(dist_high).is_integer() else f"{dist_high:.2f}%"
            struct_status = f"區間推進（距高點 {dist_str}）"

        return {
            "ticker": ticker,
            "price": price,
            "change_pct": change_pct,
            "ratio": ratio,
            "amount": amount,
            "clv": clv,
            "mid_k": mid_k,
            "struct_status": struct_status,
            "high20": high20,
            "low20": low20,
            "status_label": status_label,
            "date": last_bar_date
        }
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


# ----------------------------------------------------
# 雲端期權行情備援引擎 (剔除 0.0625 與 0.1667 假占位符)
# ----------------------------------------------------
def fetch_options_backup(ticker: str, cur_price: float):
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    session.headers.update(headers)

    crumb = None
    try:
        session.get("https://fc.yahoo.com", timeout=3)
    except Exception:
        pass

    try:
        crumb_resp = session.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=3)
        if crumb_resp.status_code == 200 and crumb_resp.text:
            crumb = crumb_resp.text.strip()
    except Exception:
        crumb = None

    url = f"https://query2.finance.yahoo.com/v7/finance/options/{ticker}"
    params = {"crumb": crumb} if crumb else {}
    resp = session.get(url, params=params, timeout=5)
    resp.raise_for_status()
    payload = resp.json()
    result = payload.get("optionChain", {}).get("result", [])[0]

    quote_data = result.get("quote", {})
    price = quote_data.get("regularMarketPrice", cur_price)

    exp_timestamps = result.get("expirationDates", [])
    now_ts = datetime.now(timezone.utc).timestamp()
    
    ts_with_days = [(ts, (ts - now_ts) / 86400) for ts in exp_timestamps if (ts - now_ts) >= 0]
    target_ts = []
    if ts_with_days:
        target_ts.append(ts_with_days[0][0])
        monthly = [ts for ts, d in ts_with_days if 20 <= d <= 45]
        if monthly:
            target_ts.append(monthly[0])
        elif len(ts_with_days) > 1:
            target_ts.append(ts_with_days[1][0])

    near_exp_str = datetime.fromtimestamp(target_ts[0], tz=timezone.utc).strftime("%Y%m%d") if target_ts else ""
    far_exp_str = datetime.fromtimestamp(target_ts[1], tz=timezone.utc).strftime("%Y%m%d") if len(target_ts) > 1 else ""

    total_call_vol = 0
    total_put_vol = 0
    total_call_oi = 0
    total_put_oi = 0

    near_contracts = []
    far_contracts = []
    call_oi_map = {}
    put_oi_map = {}
    big_orders = []
    atm_ivs = []

    for idx, ts in enumerate(target_ts):
        exp_date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")
        sub_url = f"https://query2.finance.yahoo.com/v7/finance/options/{ticker}?date={ts}"
        if crumb:
            sub_url += f"&crumb={crumb}"
        try:
            sub_r = session.get(sub_url, timeout=5).json()
            opt_block = sub_r["optionChain"]["result"][0]["options"][0]
        except Exception:
            continue

        combined_contracts = opt_block.get("calls", []) + opt_block.get("puts", [])
        combined_contracts.sort(key=lambda x: abs(float(x.get("strike") or 0) - price))
        for c in combined_contracts:
            c_iv = float(c.get("impliedVolatility") or 0)
            bid = float(c.get("bid") or 0)
            ask = float(c.get("ask") or 0)
            vol = int(c.get("volume") or 0)
            if abs(c_iv - 0.0625) < 0.001 or abs(c_iv - 0.1667) < 0.001:
                continue
            if 0.08 <= c_iv <= 3.5 and (bid > 0 or ask > 0 or vol > 0):
                atm_ivs.append(c_iv)
                if len(atm_ivs) >= 8:
                    break

        for c in opt_block.get("calls", []):
            vol = int(c.get("volume") or 0)
            oi = int(c.get("openInterest") or 0)
            strike = float(c.get("strike") or 0)
            last_p = float(c.get("lastPrice") or c.get("ask") or 0)

            total_call_vol += vol
            total_call_oi += oi
            if oi > 0:
                call_oi_map[strike] = call_oi_map.get(strike, 0) + oi

            if vol > 0 or oi > 0:
                item = {
                    "exp": exp_date_str, "strike": strike, "right": "Call",
                    "vol": vol, "oi": oi, "amt": vol * last_p * 100
                }
                if idx == 0:
                    near_contracts.append(item)
                else:
                    far_contracts.append(item)

            trade_amt = vol * last_p * 100
            if trade_amt >= 30_000:
                is_opening = "⚡ 新開倉 (Vol > OI)" if oi > 0 and vol > oi else "持倉換手"
                big_orders.append({
                    "exp": exp_date_str, "strike": strike, "right": "Call",
                    "amt": trade_amt, "vol": vol, "oi": oi,
                    "action": "主力掃單進攻", "note": is_opening
                })

        for p in opt_block.get("puts", []):
            vol = int(p.get("volume") or 0)
            oi = int(p.get("openInterest") or 0)
            strike = float(p.get("strike") or 0)
            last_p = float(p.get("lastPrice") or p.get("ask") or 0)

            total_put_vol += vol
            total_put_oi += oi
            if oi > 0:
                put_oi_map[strike] = put_oi_map.get(strike, 0) + oi

            if vol > 0 or oi > 0:
                item = {
                    "exp": exp_date_str, "strike": strike, "right": "Put",
                    "vol": vol, "oi": oi, "amt": vol * last_p * 100
                }
                if idx == 0:
                    near_contracts.append(item)
                else:
                    far_contracts.append(item)

            trade_amt = vol * last_p * 100
            if trade_amt >= 30_000:
                is_opening = "⚡ 新開倉 (Vol > OI)" if oi > 0 and vol > oi else "持倉換手"
                big_orders.append({
                    "exp": exp_date_str, "strike": strike, "right": "Put",
                    "amt": trade_amt, "vol": vol, "oi": oi,
                    "action": "主力對沖防守", "note": is_opening
                })

    vol_pcr = (total_put_vol / total_call_vol) if total_call_vol > 0 else 0.0
    oi_pcr = (total_put_oi / total_call_oi) if total_call_oi > 0 else 0.0
    avg_iv = mean(atm_ivs[:6]) if atm_ivs else None

    call_cand = max(call_oi_map.items(), key=lambda x: x[1]) if call_oi_map else (0.0, 0)
    call_wall = call_cand[0] if (call_cand[1] >= 50 and price > 0 and abs(call_cand[0] - price) / price <= 0.35) else 0.0

    put_cand = max(put_oi_map.items(), key=lambda x: x[1]) if put_oi_map else (0.0, 0)
    put_wall = put_cand[0] if (put_cand[1] >= 50 and price > 0 and abs(put_cand[0] - price) / price <= 0.35) else 0.0

    top_near = sorted(near_contracts, key=lambda x: (x["vol"], x["oi"]), reverse=True)[:2]
    top_far = sorted(far_contracts, key=lambda x: (x["vol"], x["oi"]), reverse=True)[:2]

    if vol_pcr < 0.7:
        bias = "🔥 偏多共識（買權進攻主導）"
        color = 0x2ECC71
    elif vol_pcr > 1.2:
        bias = "🩸 偏空防守（避險賣權主導）"
        color = 0xE74C3C
    else:
        bias = "⚖️ 多空平衡拉鋸"
        color = 0x3498DB

    return {
        "ticker": ticker,
        "price": price,
        "is_cold": False,
        "iv": avg_iv,
        "bias": bias,
        "color": color,
        "vol_pcr": vol_pcr,
        "oi_pcr": oi_pcr,
        "call_vol": total_call_vol,
        "put_vol": total_put_vol,
        "call_oi": total_call_oi,
        "put_oi": total_put_oi,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "top_near": top_near,
        "top_far": top_far,
        "near_exp": near_exp_str,
        "far_exp": far_exp_str,
        "big_orders": sorted(big_orders, key=lambda x: x["amt"], reverse=True)[:4]
    }


# ----------------------------------------------------
# 期權運算引擎 (完整採樣 TWS 即時與歷史資料)
# ----------------------------------------------------
def calc_options_structure_sync(ticker: str):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    from ib_insync import IB, Stock, Option

    ib = IB()
    cur_price = 0.0
    tws_success = False
    result_data = None
    connected = False
    iv_val = None

    try:
        for cid in [704, 706, 707]:
            try:
                ib.connect(IB_HOST, IB_PORT, clientId=cid, timeout=4, readonly=True)
                ib.reqMarketDataType(1)
                connected = True
                break
            except Exception:
                continue

        if not connected:
            LOG.warning("TWS 連線佔線或尚未就緒，自動平滑切換至雲端備援")
        else:
            stock = Stock(ticker, "SMART", "USD")
            qualified = ib.qualifyContracts(stock)
            if qualified:
                stock_ticker = ib.reqMktData(qualified[0], "100,101,106", False, False)
                for _ in range(12):
                    ib.sleep(0.25)
                    p = stock_ticker.marketPrice() or stock_ticker.last or getattr(stock_ticker, "delayedLast", None) or stock_ticker.close
                    if p and not math.isnan(p) and p > 0:
                        cur_price = float(p)
                    cand_iv = getattr(stock_ticker, "impliedVol", None) or getattr(stock_ticker, "impliedVolatility", None)
                    if cand_iv and not math.isnan(cand_iv) and cand_iv > 0.01:
                        iv_val = float(cand_iv)
                        if cur_price > 0:
                            break

                if not cur_price or math.isnan(cur_price) or cur_price <= 0:
                    bars = ib.reqHistoricalData(qualified[0], "", "2 D", "1 day", "TRADES", True, 1)
                    if bars:
                        cur_price = float(bars[-1].close)

                ib.cancelMktData(qualified[0])

                chains = ib.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId)
                if chains and cur_price > 0:
                    smart_chain = next((c for c in chains if c.exchange == "SMART"), chains[0])
                    expirations = sorted(smart_chain.expirations)
                    now_dt = datetime.now(ET).date()

                    exp_with_days = []
                    for exp_str in expirations:
                        try:
                            exp_date = datetime.strptime(exp_str, "%Y%m%d").date()
                            d_left = (exp_date - now_dt).days
                            if d_left >= 0:
                                exp_with_days.append((exp_str, d_left))
                        except Exception:
                            continue

                    target_exps = []
                    if exp_with_days:
                        target_exps.append(exp_with_days[0][0])
                        monthly_cands = [exp for exp, days in exp_with_days if 20 <= days <= 45]
                        if monthly_cands:
                            target_exps.append(monthly_cands[0])
                        elif len(exp_with_days) > 1:
                            target_exps.append(exp_with_days[1][0])

                    near_exp_str = target_exps[0] if target_exps else ""
                    far_exp_str = target_exps[1] if len(target_exps) > 1 else ""

                    strikes = sorted(smart_chain.strikes)
                    near_strikes = sorted(strikes, key=lambda s: abs(s - cur_price))[:4]
                    near_strikes = sorted(near_strikes)

                    opt_contracts = []
                    for exp in target_exps:
                        for s in near_strikes:
                            opt_contracts.append(Option(ticker, exp, s, "C", "SMART"))
                            opt_contracts.append(Option(ticker, exp, s, "P", "SMART"))

                    qualified_opts = ib.qualifyContracts(*opt_contracts) if opt_contracts else []
                    tickers = [ib.reqMktData(c, "100,101,106", False, False) for c in qualified_opts]

                    for _ in range(15):
                        ib.sleep(0.25)
                        has_call = any(((getattr(t, "openInterest", 0) or 0) > 0 or (getattr(t, "volume", 0) or 0) > 0) for t in tickers if t.contract.right == "C")
                        has_put = any(((getattr(t, "openInterest", 0) or 0) > 0 or (getattr(t, "volume", 0) or 0) > 0) for t in tickers if t.contract.right == "P")
                        if has_call and has_put:
                            break

                    total_sample_call_vol = 0
                    total_sample_put_vol = 0
                    total_sample_call_oi = 0
                    total_sample_put_oi = 0

                    near_contracts = []
                    far_contracts = []
                    call_oi_map = {}
                    put_oi_map = {}
                    big_orders = []
                    tws_atm_ivs = []

                    for t in tickers:
                        c = t.contract
                        exp = c.lastTradeDateOrContractMonth

                        mg = getattr(t, "modelGreeks", None)
                        if mg and getattr(mg, "impliedVol", None) and not math.isnan(mg.impliedVol) and 0.08 <= mg.impliedVol <= 3.5:
                            tws_atm_ivs.append(mg.impliedVol)

                        raw_v = getattr(t, "volume", None) or getattr(t, "delayedVolume", 0)
                        vol = int(raw_v) if raw_v and not math.isnan(raw_v) and raw_v > 0 else 0

                        raw_oi = getattr(t, "openInterest", None) or 0
                        oi = int(raw_oi) if raw_oi and not math.isnan(raw_oi) and raw_oi > 0 else 0

                        price = t.marketPrice() or t.last or getattr(t, "delayedLast", 0) or t.close or 0.0
                        if math.isnan(price):
                            price = 0.0

                        trade_amt = vol * price * 100
                        item = {
                            "exp": exp, "strike": c.strike,
                            "right": "Call" if c.right == "C" else "Put",
                            "vol": vol, "oi": oi, "amt": trade_amt
                        }

                        if vol > 0 or oi > 0:
                            if exp == near_exp_str:
                                near_contracts.append(item)
                            else:
                                far_contracts.append(item)

                        if c.right == "C":
                            total_sample_call_vol += vol
                            total_sample_call_oi += oi
                            if oi > 0:
                                call_oi_map[c.strike] = call_oi_map.get(c.strike, 0) + oi
                        else:
                            total_sample_put_vol += vol
                            total_sample_put_oi += oi
                            if oi > 0:
                                put_oi_map[c.strike] = put_oi_map.get(c.strike, 0) + oi

                        if trade_amt >= 30_000:
                            ask_p = t.ask if (t.ask and not math.isnan(t.ask)) else getattr(t, "delayedAsk", 0)
                            bid_p = t.bid if (t.bid and not math.isnan(t.bid)) else getattr(t, "delayedBid", 0)

                            if ask_p and price >= ask_p:
                                direction = "主動買進 (打在 Ask)"
                            elif bid_p and price <= bid_p:
                                direction = "主動賣出 (打在 Bid)"
                            else:
                                direction = "盤中撮合換手"

                            is_opening = "⚡ 新開倉 (Vol > OI)" if oi > 0 and vol > oi else "持倉換手"
                            big_orders.append({
                                "exp": exp, "strike": c.strike,
                                "right": "Call" if c.right == "C" else "Put",
                                "amt": trade_amt, "vol": vol, "oi": oi,
                                "action": direction, "note": is_opening
                            })

                    for c in qualified_opts:
                        ib.cancelMktData(c)

                    if not iv_val and tws_atm_ivs:
                        iv_val = mean(tws_atm_ivs)

                    call_cand = max(call_oi_map.items(), key=lambda x: x[1]) if call_oi_map else (0.0, 0)
                    call_wall = call_cand[0] if (call_cand[1] >= 50 and cur_price > 0 and abs(call_cand[0] - cur_price) / cur_price <= 0.35) else 0.0

                    put_cand = max(put_oi_map.items(), key=lambda x: x[1]) if put_oi_map else (0.0, 0)
                    put_wall = put_cand[0] if (put_cand[1] >= 50 and cur_price > 0 and abs(put_cand[0] - cur_price) / cur_price <= 0.35) else 0.0

                    top_near = sorted(near_contracts, key=lambda x: (x["vol"], x["oi"]), reverse=True)[:2]
                    top_far = sorted(far_contracts, key=lambda x: (x["vol"], x["oi"]), reverse=True)[:2]

                    calc_call_vol = total_sample_call_vol
                    calc_put_vol = total_sample_put_vol
                    calc_call_oi = total_sample_call_oi
                    calc_put_oi = total_sample_put_oi

                    if calc_call_vol == 0 and calc_put_vol == 0:
                        try:
                            closed_snap = fetch_options_backup(ticker, cur_price)
                            if closed_snap and (closed_snap["call_vol"] + closed_snap["put_vol"] > 0):
                                calc_call_vol = closed_snap["call_vol"]
                                calc_put_vol = closed_snap["put_vol"]
                                calc_call_oi = closed_snap["call_oi"]
                                calc_put_oi = closed_snap["put_oi"]
                                vol_pcr = closed_snap["vol_pcr"]
                                oi_pcr = closed_snap["oi_pcr"]
                                top_near = closed_snap["top_near"]
                                top_far = closed_snap["top_far"]
                                big_orders = closed_snap["big_orders"]
                                bias = closed_snap["bias"]
                                color = closed_snap["color"]
                                if not iv_val and closed_snap.get("iv"):
                                    iv_val = closed_snap["iv"]
                            else:
                                vol_pcr = 0.0
                                oi_pcr = (calc_put_oi / calc_call_oi) if calc_call_oi > 0 else 0.0
                                bias = "🛡️ 偏多佈局" if oi_pcr < 0.7 else "🛡️ 偏空避險" if oi_pcr > 1.2 else "⚖️ 多空均衡"
                                color = 0x2ECC71 if oi_pcr < 0.7 else 0xE74C3C if oi_pcr > 1.2 else 0x3498DB
                        except Exception:
                            vol_pcr = 0.0
                            oi_pcr = (calc_put_oi / calc_call_oi) if calc_call_oi > 0 else 0.0
                            bias = "🛡️ 偏多佈局" if oi_pcr < 0.7 else "🛡️ 偏空避險" if oi_pcr > 1.2 else "⚖️ 多空均衡"
                            color = 0x2ECC71 if oi_pcr < 0.7 else 0xE74C3C if oi_pcr > 1.2 else 0x3498DB
                    else:
                        vol_pcr = (calc_call_vol and calc_put_vol / calc_call_vol) if calc_call_vol > 0 else 0.0
                        oi_pcr = (calc_put_oi / calc_call_oi) if calc_call_oi > 0 else 0.0
                        if vol_pcr < 0.7:
                            bias = "🔥 偏多共識（買權進攻主導）"
                            color = 0x2ECC71
                        elif vol_pcr > 1.2:
                            bias = "🩸 偏空防守（避險賣權主導）"
                            color = 0xE74C3C
                        else:
                            bias = "⚖️ 多空平衡拉鋸"
                            color = 0x3498DB

                    if not iv_val:
                        try:
                            snap_iv = fetch_options_backup(ticker, cur_price).get("iv")
                            if snap_iv:
                                iv_val = snap_iv
                        except Exception:
                            pass

                    result_data = {
                        "ticker": ticker,
                        "price": cur_price,
                        "is_cold": False,
                        "iv": iv_val,
                        "bias": bias,
                        "color": color,
                        "vol_pcr": vol_pcr,
                        "oi_pcr": oi_pcr,
                        "call_vol": calc_call_vol,
                        "put_vol": calc_put_vol,
                        "call_oi": calc_call_oi,
                        "put_oi": calc_put_oi,
                        "call_wall": call_wall,
                        "put_wall": put_wall,
                        "top_near": top_near,
                        "top_far": top_far,
                        "near_exp": near_exp_str,
                        "far_exp": far_exp_str,
                        "big_orders": sorted(big_orders, key=lambda x: x["amt"], reverse=True)[:4]
                    }
                    tws_success = True

    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass

    if tws_success and result_data:
        return result_data

    try:
        return fetch_options_backup(ticker, cur_price)
    except Exception as e:
        raise ValueError(f"期權行情解析失敗：{e}")


# ==================== DISCORD 機器人本體設定 ====================
class NewsBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.work_slots = asyncio.Semaphore(2)

    async def setup_hook(self):
        guild_id = os.environ.get("DISCORD_GUILD_ID", "1259818682058805249")
        guild = discord.Object(id=int(guild_id))
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)


bot = NewsBot()


def run_query(mode, ticker, start, end):
    if mode == "news" and ticker in COMPANY_NEWS_ALIASES:
        name = COMPANY_NEWS_ALIASES[ticker][0]
        return name, fetch_wire_news(ticker, company_aliases(ticker, name), start, end)
    cik, name = company_info(ticker)
    if not cik:
        raise ValueError("SEC 公司清單找不到此代號，請確認代號或設定已確認的新聞別名")
    result = fetch_wire_news(ticker, company_aliases(ticker, name), start, end) if mode == "news" else fetch_target_filings(cik, start, end)
    return name, result


# ---------- TXT 開頭提示與結尾標記 ----------
# Discord 內建的文字預覽只載入檔案前 50 KB，超過的部分不顯示。
# 開頭先寫總筆數，結尾加「全部結束」標記，看不到結尾就代表預覽被截斷。
def export_header_line(count):
    return (f"【本檔共 {count} 筆。Discord 內建預覽只顯示前 50 KB，內容較多時會被截斷，"
            f"請下載後用記事本開啟；檔案最後一行應為「全部結束」。】")


def export_footer_lines(count, extra=""):
    return ["", "=" * 60, f"—— 全部結束：共 {count} 筆{extra} ——"]


def build_export_text(mode, ticker, name, start, end, result):
    label = "商業通訊精選" if mode == "news" else "SEC 申報"
    lines = [
        export_header_line(len(result.items)),
        "",
        f"{label}：{ticker}",
        f"公司：{name}",
        f"查詢區間：{start.isoformat()} 至 {end.isoformat()}",
        "日期基準：新聞按美東時間；SEC 按申報日期",
        f"狀態：{result.status}；本次匯出 {len(result.items)} 筆",
        "範圍：本次查詢回傳的結果，受原有篩選與筆數上限限制。",
    ]
    if mode == "news":
        lines.append("新聞網址為 Google News RSS 回傳的完整連結，可能轉址至來源網站。")
    for note in result.notes:
        lines.append(f"備註：{note}")
    if not result.items:
        lines.append("本次取得的資料中沒有符合條件的項目。")

    for number, row in enumerate(result.items, 1):
        lines.extend(["", "=" * 60, f"第 {number} 筆"])
        if mode == "news":
            lines.extend([
                f"標題：{row['title']}",
                f"日期：{row['published']}",
                f"來源：{row['source']}",
                f"完整網址：{row['link']}",
            ])
        else:
            description = str(row.get("description") or "").strip()
            title = row["form"]
            if description and description.upper() != title.upper():
                title += " — " + description
            lines.extend([
                f"標題：{title}",
                f"日期：{row['filingDate']}",
                f"案號：{row['accessionNumber']}",
                f"完整網址：{row['link']}",
                f"索引與附件網址：{row['index_link']}",
            ])
            if row.get("items"):
                lines.append(f"申報項目：{row['items']}")
            for doc_type, exhibit_title, link in row.get("exhibits", []):
                lines.extend([
                    f"附件標題：{doc_type} — {exhibit_title}",
                    f"附件日期：{row['filingDate']}（所屬申報日期）",
                    f"附件完整網址：{link}",
                ])
    extra = ""
    if mode == "news" and result.excluded:
        lines.extend(["", "#" * 60,
                      f"以下 {len(result.excluded)} 則因標題判定與本公司無關而排除，僅供人工檢查是否誤刪：",
                      "#" * 60])
        for ex_date, ex_source, ex_title in result.excluded:
            lines.append(f"{ex_date}｜{ex_source}｜{ex_title}")
        extra = f"（另附排除清單 {len(result.excluded)} 則）"
    lines.extend(export_footer_lines(len(result.items), extra))
    return "\r\n".join(lines) + "\r\n"


async def send_export(interaction, mode, ticker, name, start, end, result, message):
    filename = f"{ticker}_{mode}_{start.isoformat()}_{end.isoformat()}.txt"
    content = build_export_text(mode, ticker, name, start, end, result)
    with BytesIO(content.encode("utf-8-sig")) as buffer:
        attachment = discord.File(buffer, filename=filename)
        try:
            await interaction.followup.send(content=message, file=attachment)
        except discord.HTTPException:
            LOG.exception("TXT 匯出發送失敗：%s %s", mode, ticker)
            try:
                await interaction.followup.send(
                    "⚠️ TXT 附件未送出，請確認機器人在此頻道具有「附加檔案」權限，或稍後重新查詢。"
                )
            except discord.HTTPException:
                pass
        finally:
            attachment.close()


async def respond(interaction, mode, ticker, start_date, end_date):
    await interaction.response.defer(thinking=True)
    try:
        ticker = ticker_value(ticker)
        start, end = date_range(start_date, end_date)
        try:
            await asyncio.wait_for(bot.work_slots.acquire(), timeout=2)
        except asyncio.TimeoutError:
            await interaction.followup.send("目前查詢較多，請稍後再試。")
            return
        try:
            name, result = await asyncio.to_thread(run_query, mode, ticker, start, end)
        finally:
            bot.work_slots.release()
    except ValueError as exc:
        await interaction.followup.send(clip(f"⚠️ {exc}", 1800))
        return
    except requests.RequestException:
        LOG.warning("Upstream request failed for %s", mode)
        await interaction.followup.send("❌ 資料服務連線失敗，請稍後再試；這不表示查無資料。")
        return
    except Exception:
        LOG.exception("Query failed")
        await interaction.followup.send("❌ 查詢處理失敗，請查看機器人執行紀錄。")
        return

    if result.status == "ERROR":
        await interaction.followup.send("❌ 資料抓取或解析失敗，無法判斷區間內是否有資料。")
        return

    description = f"公司：{name}\n日期：{start.isoformat()} 至 {end.isoformat()}（新聞按美東時間；SEC 按申報日期）"
    description += (
        f"\n依標題篩選公司相關事件，按發布時間由新到舊，最多顯示 {NEWS_LIMIT} 則。Google RSS 非完整新聞資料庫。"
        if mode == "news"
        else "\n已檢查近期資料與區間重疊的歷史檔案；僅顯示設定的申報類型。"
    )
    if result.notes:
        description += "\n" + "；".join(result.notes)
    if not result.items:
        label = "本次檢索未找到符合條件的資料。" if result.status != "PARTIAL" else "本次結果不完整，且已取得資料中沒有符合條件的項目。"
        await send_export(
            interaction, mode, ticker, name, start, end, result,
            clip(label + "\n" + description, 1900),
        )
        return

    fields = []
    for i, row in enumerate(result.items, 1):
        if mode == "news":
            fields.append((f"{i}. {row['title']}", f"來源：{row['source']}\n時間：{row['published']}\n[開啟新聞](<{row['link']}>)"))
        else:
            fields.append(filing_field(row, i))

    title = ("商業通訊精選" if mode == "news" else "SEC 申報") + f"：{ticker}"
    pages = build_embeds(title, description, fields, 0x2ECC71 if mode == "news" else 0x2B82D9)

    total_items = len(result.items)
    total_pages = len(pages)
    sent_pages = 0

    for page_number, embed in enumerate(pages, start=1):
        embed.set_footer(text=f"第 {page_number}/{total_pages} 頁・共 {total_items} 則")
        try:
            await interaction.followup.send(embed=embed, wait=True)
            sent_pages += 1
        except discord.HTTPException:
            LOG.exception("發送失敗：%s %s，第 %s/%s 頁", mode, ticker, page_number, total_pages)
            try:
                await interaction.followup.send(f"❌ {ticker} 發送中斷：已確認送出 {sent_pages}/{total_pages} 頁，第 {page_number} 頁發送失敗。")
            except discord.HTTPException:
                pass
            return

    await send_export(
        interaction, mode, ticker, name, start, end, result,
        f"✅ {ticker} 已發送完成：共 {total_items} 則（{total_pages} 頁）。\n"
        "📄 TXT 已附上，包含每筆標題、日期與完整網址，可下載後貼到 ChatGPT。\n"
        "⚠️ Discord 預覽只顯示前 50 KB，請下載後再看完整內容。",
    )


# ==================== IBKR 新聞雷達（/ibnews）====================
IB_NEWS_CLIENT_ID = 712
IB_NEWS_PROVIDERS = ("DJ-N", "BRFUPDN")   # 道瓊個股新聞、Briefing.com 分析師評等
IB_NEWS_PAGE_SIZE = 300                   # IBKR 單次最多回傳 300 則
IB_NEWS_MAX_PAGES = 15                    # 往前翻頁上限
IB_NEWS_LIMIT = 200                       # TXT 最多輸出幾則（去重後）
IB_NEWS_EMBED_LIMIT = 100                 # Discord 訊息最多顯示幾則（完整清單看 TXT）
IB_NEWS_MAX_ARTICLES = 40                 # 最多抓幾篇新聞稿全文
IB_NEWS_ARTICLE_CHARS = 1500              # 每篇全文摘錄字數上限
_ibnews_lock = asyncio.Lock()

IB_META_RE = re.compile(r"^\{([^}]*)\}")
IB_CONT_RE = re.compile(r"\s*-\d+-\s*$")                    # 長文續篇，例如「-2-」
IB_TICKER_TAIL_RE = re.compile(r"\s*>[A-Z0-9.\-]+(?:\s+>?[A-Z0-9.\-]+)*\s*$")  # 結尾的「>UUUU」「>EFR.T ASM.AU」

IB_CATEGORY_WEIGHT = {
    "公司新聞稿": 2,
    "第三方新聞稿": 0,
    "新聞稿（未確認發布者）": 1,
    "分析師評等": 1,
    "內部人交易": 1,
    "道瓊快訊": 1,
    "SEC 申報提醒": 0,
    "媒體報導": 0,
}


def ib_to_utc(value):
    if isinstance(value, str):
        value = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def ib_parse_headline(raw):
    """拆出 {A:...:L:en} 標頭，回傳（標題, 語言字串）。"""
    raw = raw or ""
    meta = ""
    match = IB_META_RE.match(raw)
    if match:
        meta = match.group(1)
        raw = raw[match.end():]
    parts = meta.split(":")
    fields = dict(zip(parts[::2], parts[1::2]))
    lang = fields.get("L", "en").strip() or "en"
    return raw.strip(), lang


def ib_clean_title(headline):
    title = html.unescape(headline)          # 例如「&amp;」→「&」
    title = re.sub(r"^[*!\s]+", "", title)
    title = re.sub(r"^press release:\s*", "", title, flags=re.I)
    title = IB_TICKER_TAIL_RE.sub("", title)
    return title.strip()


IB_MEDIA_TAIL_RE = re.compile(r"\s--\s*[A-Za-z][A-Za-z.'& ]{1,30}$")   # 例如「-- IBD」「-- Barrons.com」


def ib_category(provider, headline):
    if re.match(r"^press release:", headline, re.I):
        return "新聞稿"            # 抓全文後再判斷是公司自己發的還是第三方
    if provider == "BRFUPDN":
        return "分析師評等"
    if IB_MEDIA_TAIL_RE.search(headline):
        return "媒體報導"
    if re.search(
        r"\b(?:price target|initiated|initiates|upgraded|downgraded|upgrades|downgrades|"
        r"maintained at|reiterated|coverage|rating)\b", headline, re.I):
        return "分析師評等"
    if re.search(r"\bfiles\s+(?:8-?k|10-?q|10-?k|s-3|424b\d?|6-?k|20-?f|40-?f)\b", headline, re.I):
        return "SEC 申報提醒"
    if re.search(r"^(?:ceo|cfo|coo|cto|cao|clo|cmo|chmn|vice chmn|pres\w*|evp|svp|vp|dir|director|holder|officer|chair\w*|gc|secy|treas\w*|exec\w*|founder|10% owner)\b"
                 r".*\b(?:buys|sells|surrenders|registers|acquires|disposes|exercises|gifts)\b", headline, re.I):
        return "內部人交易"
    if headline.startswith("*"):
        return "道瓊快訊"
    return "媒體報導"


def ib_broker_key(provider, title):
    """取出券商名稱的前 6 個英文字母，用來合併同一天、同一家券商的多則評等新聞。"""
    if provider == "BRFUPDN":
        m = re.match(r"^(.+?)\s+(?:initiated|upgraded|downgraded|reiterated|resumed|assumed|raised|lowered|"
                     r"reinstated|maintained|transferred)\b", title, re.I)
    else:
        m = re.search(r"\bby\s+(.+?)\.?$", title, re.I)
    if not m:
        return None
    letters = re.sub(r"[^a-z]", "", m.group(1).lower())
    return letters[:6] or None


def ib_merge_ratings(items):
    """同一天、同一家券商的評等（道瓊目標價、道瓊評等、Briefing.com）合併成一則。"""
    groups, order = {}, []
    for r in items:
        key = None
        if r["category"] == "分析師評等":
            broker = ib_broker_key(r["provider"], r["title"])
            if broker:
                key = (r["time"][:10], broker)
        if key is None:
            order.append(r)
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)
    merged = []
    for entry in order:
        if not isinstance(entry, tuple):
            merged.append(entry)
            continue
        rows = groups[entry]
        if len(rows) == 1:
            merged.append(rows[0])
            continue
        brf = [x for x in rows if x["provider"] == "BRFUPDN"]
        main = dict(brf[0] if brf else rows[0])
        if not brf:
            main["title"] = "；".join(dict.fromkeys(x["title"] for x in rows))
        else:
            # Briefing.com 通常不寫原目標價，道瓊有「From $X」時補在後面
            extra = [x["title"] for x in rows if x["provider"] != "BRFUPDN" and re.search(r"\bfrom \$", x["title"], re.I)]
            if extra:
                main["title"] = "；".join(dict.fromkeys([main["title"]] + extra))
        main["time"] = min(x["time"] for x in rows)
        main["provider"] = "、".join(dict.fromkeys(x["provider"] for x in rows))
        main["article_id"] = "、".join(x["article_id"] for x in rows)
        main["score"] = max(x["score"] for x in rows)
        merged.append(main)
    return merged


def ib_strip_html(text):
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def ib_is_issuer(excerpt, aliases):
    """新聞稿慣例是「城市, 日期 /PRNewswire/ -- 發布公司 ...」，看「--」後面緊接的是不是本公司。
    找不到這個格式時，才退回看開頭 500 字有沒有提到本公司。"""
    head = excerpt[:1200]
    m = re.match(r"\s*Issued on behalf of ([^\n.]+)", head, re.I)      # 付費宣傳稿會寫明委託人
    if m:
        return ib_mentions(m.group(1), aliases)
    m = re.search(r"/(?:PRNewswire|CNW|GLOBE NEWSWIRE|Business Wire|ACCESSWIRE|Newsfile)[^/]*/\s*[-–—]+\s*", head, re.I)
    if not m:
        m = re.search(r"\d{4}\s*[-–—]{1,2}\s+", head)
    if m:
        issuer = re.split(r"\(|,| [-–—]+ | today\b| announced\b| an? \b| the \b", head[m.end():m.end() + 100],
                          maxsplit=1, flags=re.I)[0]
        return ib_mentions(issuer, aliases)
    return ib_mentions(excerpt[:300], aliases)


def ib_mentions(text, aliases):
    normalized = " " + words(text or "") + " "
    return any(words(a) and (" " + words(a) + " ") in normalized for a in aliases)


# ---------- 標題翻譯（只翻標題，英文原文保留） ----------
_ib_title_cache = {}
_ib_title_cache_lock = threading.Lock()

# 實測：clients5 可用；translate.googleapis.com 在大量請求後會回 429（暫時封鎖）
GOOGLE_ENDPOINTS = [
    ("https://clients5.google.com/translate_a/t",
     {"client": "dict-chrome-ex", "sl": "en", "tl": "zh-TW"}),
    ("https://translate.googleapis.com/translate_a/single",
     {"client": "gtx", "sl": "en", "tl": "zh-TW", "dt": "t"}),
]


class _RateLimited(Exception):
    pass


def _translate_google(text, endpoint):
    url, base = endpoint
    response = requests.get(url, params={**base, "q": text},
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=(5, 10))
    if response.status_code == 429:
        raise _RateLimited()
    response.raise_for_status()
    data = response.json()
    if "translate_a/single" in url:
        return "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()
    first = data[0] if isinstance(data, list) and data else data
    while isinstance(first, list) and first:
        first = first[0]
    return str(first).strip() if first else None


def _translate_deepl(texts, key):
    host = "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"
    output = []
    for i in range(0, len(texts), 50):
        chunk = texts[i:i + 50]
        response = requests.post(
            host + "/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {key}"},
            data={"text": chunk, "source_lang": "EN", "target_lang": "ZH-HANT"},
            timeout=(5, 20),
        )
        response.raise_for_status()
        output.extend(t["text"] for t in response.json()["translations"])
    return output


def _protect_names(title, ticker, aliases):
    """把公司名換成股票代號再翻，避免 Energy Fuels 被直譯成「能源燃料」。"""
    text = title
    for alias in sorted(aliases, key=len, reverse=True):
        if alias and alias.casefold() != ticker.casefold():
            text = re.sub(r"(?<!\w)" + re.escape(alias) + r"(?:,?\s+(?:Inc|Corp|Ltd|Co)\.?)?(?!\w)", ticker, text, flags=re.I)
    text = re.sub(re.escape(ticker) + r"\s*\(" + re.escape(ticker) + r"\)", ticker, text)   # 「UUUU (UUUU)」→「UUUU」
    return text


# ---------- 翻譯前後的修正規則 ----------
# 道瓊常用縮寫，送翻譯前先展開（例如 Rev 會被誤譯成「修訂」、Shr 被譯成「小」）
IB_ABBREVIATIONS = [
    (r"\b([1-4])Q\b", r"Q\1"),
    (r"\bRev\b", "Revenue"),
    (r"\b(?:Loss)/Shr\b", "Loss Per Share"),
    (r"\b(?:Profit|EPS|Earnings|Net)/Shr\b", "Earnings Per Share"),
    (r"\bShr\b", "Share"),
    (r"\bAdj\b", "Adjusted"),
    (r"\bBd\b", "Board"),
    (r"\bChmn\b", "Chairman"),
    (r"\bMgmt\b", "Management"),
    (r"\bOps\b", "Operations"),
    (r"\bYr\b", "Year"),
    (r"\bMos\b", "Months"),
    (r"\bStk\b", "Stock"),
    (r"\bProc\b", "Proceeds"),
    (r"\bSees a Steal\b", "Sees a Bargain"),
    (r"\ba Steal\b", "a Bargain"),
    (r"\bShares (?:Gain|Rise|Jump|Surge|Soar|Climb|Rally|Advance)\b", "Stock Price Rises"),
    (r"\bShares (?:Fall|Drop|Slide|Tumble|Sink|Slump|Plunge|Decline)\b", "Stock Price Falls"),
]

# 容易被當成一般英文單字誤譯的專有名詞（例如 Anthropic 被譯成「人擇」「人為」）。
# 翻譯時暫時換成代碼，翻完再換回原文。可自行增加。
IB_PROTECTED_TERMS = ["Anthropic", "Fluidstack", "CoreWeave", "Corvex", "Neocloud", "Core42", "Nebius", "Crusoe"]

IB_INSIDER_ROLES = {
    "ceo": "執行長", "cfo": "財務長", "coo": "營運長", "cto": "技術長", "cao": "會計長", "clo": "法務長",
    "gc": "法務長", "cmo": "行銷長", "chmn": "董事長", "chair": "董事長", "chairman": "董事長",
    "dir": "董事", "director": "董事", "officer": "高階主管", "holder": "大股東", "pres": "總裁",
    "president": "總裁", "vp": "副總裁", "evp": "執行副總裁", "svp": "資深副總裁", "secy": "秘書",
    "treas": "財務主管", "founder": "創辦人", "exec": "主管",
}
IB_INSIDER_VERBS = {
    "buys": "在市場買進", "sells": "賣出", "acquires": "獲配", "disposes": "處分",
    "surrenders": "繳回抵稅", "registers": "預告出售", "exercises": "履約取得", "gifts": "贈與",
}


def ib_rule_translate(title):
    """固定格式的標題直接套中文，不送翻譯；不符合格式回傳 None。"""
    m = re.match(r"^Symbol for (.+?) Now (\S+)$", title)
    if m:
        return f"{m.group(1)} 交易代號改為 {m.group(2)}"
    m = re.match(r"^(Vice Chmn|\S+)\s+(.+?)\s+(Buys|Sells|Acquires|Disposes|Surrenders|Registers|Exercises|Gifts)"
                 r"\s+([\d,]+)\s+Of\s+.+$", title, re.I)
    if m:
        role_raw, person, verb, shares = m.groups()
        if role_raw.lower() == "vice chmn":
            role = "副董事長"
        else:
            parts = [IB_INSIDER_ROLES.get(p.lower()) for p in role_raw.split("/")]
            role = "兼".join(parts) if all(parts) else role_raw
        text = f"{role} {person} {IB_INSIDER_VERBS[verb.lower()]} {shares} 股"
        if verb.lower() == "registers":
            text += "（Form 144 預告，不代表已經賣出）"
        return text
    m = re.match(r"^.+?\bCOM, Inst Holders, ([1-4])Q (\d{4})", title, re.I)
    if m:
        return f"機構持股統計（{m.group(2)} 年第 {m.group(1)} 季）"
    m = re.match(r"^CFA High Yield:\s*Insider Review For Week Ended (.+)$", title, re.I)
    if m:
        return f"CFA 高收益專欄：內部人交易週報（截至 {m.group(1)} 當週，全市場）"
    m = re.match(r"^Substantial Insider (Purchases|Sales): Morning Report$", title, re.I)
    if m:
        return "內部人大額" + ("買進" if m.group(1).lower() == "purchases" else "賣出") + "彙整：晨間報告（全市場）"
    return None


def ib_prepare_source(text, ticker=None):
    for pattern, repl in IB_ABBREVIATIONS:
        text = re.sub(pattern, repl, text)
    placeholders = {}
    if ticker and re.search(r"(?<!\w)" + re.escape(ticker) + r"(?!\w)", text):
        text = re.sub(r"(?<!\w)" + re.escape(ticker) + r"(?!\w)", "QZXZ", text)
        placeholders["QZXZ"] = ticker
    for i, term in enumerate(IB_PROTECTED_TERMS):
        if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.I):
            code = "QZX" + "ABCDEFGHJKLMNPRSTUVWY"[i % 21]
            text = re.sub(r"(?<!\w)" + re.escape(term) + r"(?!\w)", code, text, flags=re.I)
            placeholders[code] = term
    return text, placeholders


def ib_restore_terms(text, placeholders):
    for code, term in placeholders.items():
        text = re.sub(code, term, text, flags=re.I)
    return text


def translate_titles(items, ticker, name=None):
    """替每則新聞加上 title_zh，回傳一句說明文字。"""
    from concurrent.futures import ThreadPoolExecutor

    aliases = company_aliases(ticker, name) if name else ()
    titles = list(dict.fromkeys(r["title"] for r in items))
    with _ib_title_cache_lock:
        todo = [t for t in titles if t not in _ib_title_cache]
    # 固定格式的標題直接套中文，不送翻譯
    results, tails, sources, holders = {}, {}, {}, {}
    for t in todo:
        fixed = ib_rule_translate(t)
        if fixed:
            results[t] = fixed
            continue
        body, tail = t, ""
        m = IB_MEDIA_TAIL_RE.search(t)          # 「-- IBD」這類媒體名不翻（IBD 會被譯成腸道疾病）
        if m:
            body, tail = t[:m.start()], m.group(0).replace("--", "").strip()
        tails[t] = tail
        prepared, holders[t] = ib_prepare_source(_protect_names(body, ticker, aliases), ticker)
        sources[t] = prepared
    engine = "Google"
    key = os.environ.get("DEEPL_API_KEY", "").strip()
    pending = [t for t in todo if t not in results]
    if pending and key:
        try:
            results.update(zip(pending, _translate_deepl([sources[t] for t in pending], key)))
            engine = "DeepL"
        except Exception as exc:
            LOG.warning("DeepL 翻譯失敗，改用 Google：%s", exc)

    for endpoint in GOOGLE_ENDPOINTS:
        remaining = [t for t in todo if t not in results]
        if not remaining:
            break
        blocked = threading.Event()

        def one(text):
            if blocked.is_set():
                return text, None
            try:
                return text, _translate_google(sources[text], endpoint)
            except _RateLimited:
                blocked.set()
            except Exception as exc:
                LOG.warning("Google 翻譯失敗（%s）：%s", endpoint[0], exc)
            return text, None

        with ThreadPoolExecutor(max_workers=3) as pool:
            for text, zh in pool.map(one, remaining):
                if zh:
                    results[text] = zh
        if blocked.is_set():
            LOG.warning("Google 翻譯被暫時限制（429）：%s", endpoint[0])

    # 還有失敗的，放慢速度用第一個網址再試一輪
    remaining = [t for t in todo if t not in results]
    if remaining:
        time.sleep(2)
        for text in remaining:
            try:
                zh = _translate_google(sources[text], GOOGLE_ENDPOINTS[0])
                if zh:
                    results[text] = zh
            except _RateLimited:
                break
            except Exception:
                pass
            time.sleep(0.4)

    for t, tail in tails.items():
        if t in results:
            results[t] = ib_restore_terms(results[t], holders.get(t, {}))
            if tail:
                results[t] = f"{results[t]}（{tail}）"

    with _ib_title_cache_lock:
        _ib_title_cache.update(results)
        if len(_ib_title_cache) > 5000:
            _ib_title_cache.clear()
        cache = dict(_ib_title_cache)
    failed = 0
    for r in items:
        r["title_zh"] = cache.get(r["title"]) or results.get(r["title"])
        if not r["title_zh"]:
            failed += 1
    note = f"中文標題由 {engine} 自動翻譯（公司名以代號 {ticker} 表示），僅供快速瀏覽，內容以英文原文為準"
    if failed:
        note += f"；{failed} 則翻譯失敗，只顯示英文"
    return note


def fetch_ib_news_sync(ticker, start, end, name=None):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    from ib_insync import IB, Stock

    ib = IB()
    notes = []
    try:
        try:
            ib.connect(IB_HOST, IB_PORT, clientId=IB_NEWS_CLIENT_ID, timeout=12, readonly=True)
        except Exception:
            raise ValueError(f"無法連線至本地 TWS，請確認 TWS 已登入且 API (Port {IB_PORT}) 已就緒")

        available = {p.code for p in ib.reqNewsProviders()}
        codes = [c for c in IB_NEWS_PROVIDERS if c in available]
        if not codes:
            raise ValueError("帳戶目前沒有 DJ-N 或 BRFUPDN 新聞權限")
        missing = [c for c in IB_NEWS_PROVIDERS if c not in available]
        if missing:
            notes.append("帳戶無以下新聞來源權限，已略過：" + "、".join(missing))

        qualified = ib.qualifyContracts(Stock(ticker, "SMART", "USD"))
        if not qualified:
            raise ValueError(f"找不到代號 `{ticker}` 的股票合約，請確認代號正確性")
        con_id = qualified[0].conId

        # 查詢區間：美東日期 → UTC 時間
        end_next = end + timedelta(days=1)
        start_utc = datetime(start.year, start.month, start.day, tzinfo=ET).astimezone(timezone.utc)
        end_utc = datetime(end_next.year, end_next.month, end_next.day, tzinfo=ET).astimezone(timezone.utc)

        # 往前翻頁抓取
        raw_items, seen_ids = [], set()
        cursor = end_utc
        pages = 0
        reached_limit = True
        while pages < IB_NEWS_MAX_PAGES:
            pages += 1
            # 實測：IBKR 回傳的是「第一個時間參數之前」最新的 N 則，
            # 所以第一個參數放翻頁游標（較晚的時間），第二個放查詢起點。
            batch = ib.reqHistoricalNews(
                con_id, "+".join(codes),
                cursor.strftime("%Y-%m-%d %H:%M:%S"),
                start_utc.strftime("%Y-%m-%d %H:%M:%S"),
                IB_NEWS_PAGE_SIZE,
            )
            if not batch:
                reached_limit = False
                break
            new_count, oldest = 0, None
            for n in batch:
                if n.articleId in seen_ids:
                    continue
                seen_ids.add(n.articleId)
                new_count += 1
                t = ib_to_utc(n.time)
                raw_items.append((t, n.providerCode, n.articleId, n.headline))
                if oldest is None or t < oldest:
                    oldest = t
            if new_count == 0 or oldest is None or oldest <= start_utc:
                reached_limit = False
                break
            cursor = oldest - timedelta(seconds=1)
        if reached_limit:
            notes.append("已達翻頁上限，查詢區間較早的部分可能未完整抓取，請縮小日期範圍")

        # 清理、分類、去重
        rows = {}
        skipped_lang = skipped_cont = skipped_range = 0
        for t, provider, article_id, raw in raw_items:
            headline, lang = ib_parse_headline(raw)
            langs = [x.strip().lower() for x in lang.split(",")]
            if "en" not in langs or re.search(r"[\u3040-\u30ff\u3400-\u9fff]", headline):
                skipped_lang += 1
                continue
            if IB_CONT_RE.search(headline):
                skipped_cont += 1
                continue
            local = t.astimezone(ET)
            if not start <= local.date() <= end:
                skipped_range += 1
                continue
            category = ib_category(provider, headline)
            title = ib_clean_title(headline)
            if not title:
                continue
            score = 1 + sum(w for p, w in SCORES if re.search(p, title, re.I)) + IB_CATEGORY_WEIGHT.get(category, 0)
            row = dict(
                title=title, time=local.strftime("%Y-%m-%d %H:%M"), category=category,
                provider=provider, article_id=article_id, score=score, pub_dt=t, excerpt=None,
            )
            key = (words(title), local.date())
            old = rows.get(key)
            if old is None or (category == "新聞稿" and old["category"] != "新聞稿"):
                rows[key] = row

        items = sorted(rows.values(), key=lambda r: r["pub_dt"], reverse=True)
        before_merge = len(items)
        items = ib_merge_ratings(items)
        if before_merge > len(items):
            notes.append(f"同一天、同一家券商的評等已合併，減少 {before_merge - len(items)} 則重複")

        # 新聞稿抓全文摘錄
        press_count = sum(1 for r in items if r["category"] == "新聞稿")
        fetched = failed = 0
        for r in items:
            if r["category"] != "新聞稿" or fetched >= IB_NEWS_MAX_ARTICLES:
                continue
            try:
                article = ib.reqNewsArticle(r["provider"], r["article_id"])
                fetched += 1
                r["fetched"] = True
                if article and getattr(article, "articleType", 0) == 0 and article.articleText:
                    r["excerpt"] = clip(ib_strip_html(article.articleText), IB_NEWS_ARTICLE_CHARS)
            except Exception:
                failed += 1

        # 判斷新聞稿是公司自己發布，還是第三方新聞稿裡提到公司
        aliases = company_aliases(ticker, name) if name else (ticker,)
        for r in items:
            if r["category"] != "新聞稿":
                continue
            own = ib_mentions(r["title"], aliases) or ib_is_issuer(r["excerpt"] or "", aliases)
            if own:
                r["category"] = "公司新聞稿"
            elif not r.get("excerpt"):
                # 沒讀到全文（超過抓取上限或讀取失敗），無法確認是誰發的，留在主清單
                r["category"] = "新聞稿（未確認發布者）"
            else:
                r["category"] = "第三方新聞稿"
            if r["category"] == "第三方新聞稿":
                r["excerpt"] = None   # 第三方新聞稿只用摘錄判斷發布者，不寫進 TXT，節省 AI 閱讀量
            r["score"] += IB_CATEGORY_WEIGHT[r["category"]]

        # 第三方新聞稿（多為付費宣傳稿）排到最後
        items = [r for r in items if r["category"] != "第三方新聞稿"] + \
                [r for r in items if r["category"] == "第三方新聞稿"]

        counts = {}
        for r in items:
            counts[r["category"]] = counts.get(r["category"], 0) + 1
        if counts:
            notes.append("分類：" + "、".join(f"{k} {v}" for k, v in counts.items()))

        notes.append(f"IBKR 原始回傳 {len(raw_items)} 則（翻頁 {pages} 次）")
        if raw_items and not rows:
            earliest = min(x[0] for x in raw_items).astimezone(ET).strftime("%Y-%m-%d")
            latest = max(x[0] for x in raw_items).astimezone(ET).strftime("%Y-%m-%d")
            notes.append(f"IBKR 回傳的新聞日期為 {earliest} 至 {latest}，沒有落在查詢區間內")
        dj_times = [x[0] for x in raw_items if x[1] == "DJ-N"]
        if dj_times and not reached_limit:
            dj_first = min(dj_times).astimezone(ET).date()
            if dj_first > start + timedelta(days=14):
                notes.append(f"道瓊 DJ-N 最早只回傳到 {dj_first.isoformat()}，更早的新聞 IBKR 沒有提供，空窗期請依 SEC 申報與公司官網新聞稿補查")
        if skipped_range:
            notes.append(f"{skipped_range} 則不在查詢日期內，已略過")
        if skipped_lang:
            notes.append(f"略過 {skipped_lang} 則非英文版本")
        if skipped_cont:
            notes.append(f"略過 {skipped_cont} 則長文續篇（-2-、-3-）")
        if failed:
            notes.append(f"{failed} 篇新聞稿全文讀取失敗")
        if press_count > IB_NEWS_MAX_ARTICLES:
            notes.append(f"新聞稿共 {press_count} 則，只抓最新 {IB_NEWS_MAX_ARTICLES} 則的全文")
        if len(items) > IB_NEWS_LIMIT:
            core = {"公司新聞稿", "分析師評等", "內部人交易", "SEC 申報提醒"}
            ordered = ([r for r in items if r["category"] in core]
                       + [r for r in items if r["category"] not in core and r["category"] != "第三方新聞稿"]
                       + [r for r in items if r["category"] == "第三方新聞稿"])
            keep = {id(r) for r in ordered[:IB_NEWS_LIMIT]}
            notes.append(f"去重後共 {len(items)} 則，超過上限 {IB_NEWS_LIMIT} 則：公司新聞稿、分析師評等、內部人交易、SEC 申報提醒優先全數保留，"
                         "道瓊快訊與媒體報導只留最新部分，其餘省略")
            items = ([r for r in items if id(r) in keep and r["category"] != "第三方新聞稿"]
                     + [r for r in items if id(r) in keep and r["category"] == "第三方新聞稿"])

        status = "PARTIAL" if reached_limit or failed else "OK" if items else "EMPTY"
        return Result(items[:IB_NEWS_LIMIT], status, list(dict.fromkeys(notes)))
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def build_ib_export_text(ticker, name, start, end, result):
    lines = [
        export_header_line(len(result.items)),
        "",
        f"IBKR 新聞：{ticker}",
        f"公司：{name}",
        f"查詢區間：{start.isoformat()} 至 {end.isoformat()}",
        "時間基準：美東時間（由 IBKR 回傳的 UTC 時間換算）",
        "來源：Dow Jones Global Equity Trader（DJ-N）、Briefing.com Analyst Actions（BRFUPDN）",
        f"狀態：{result.status}；本次匯出 {len(result.items)} 筆",
        "處理：已去除重複、長文續篇與非英文版本；公司新聞稿附全文摘錄（前段），第三方新聞稿只列標題。",
        "類別：公司新聞稿、新聞稿（未確認發布者）、第三方新聞稿（他家發布、內文提到本公司）、分析師評等、內部人交易、道瓊快訊、SEC 申報提醒、媒體報導。",
    ]
    for note in result.notes:
        lines.append(f"備註：{note}")
    if not result.items:
        lines.append("本次取得的資料中沒有符合條件的項目。")
    third_party_started = False
    for number, row in enumerate(result.items, 1):
        if row["category"] == "第三方新聞稿" and not third_party_started:
            third_party_started = True
            lines.extend(["", "#" * 60,
                          "以下為第三方新聞稿：由其他公司或宣傳機構發布、內文提到本公司，多為付費宣傳稿，只列標題，不附全文摘錄。",
                          "#" * 60])
        lines.extend([
            "", "=" * 60, f"第 {number} 筆",
            f"標題：{row['title']}",
            f"中文標題：{row.get('title_zh') or '（翻譯失敗）'}",
            f"時間：{row['time']}（美東）",
            f"類別：{row['category']}",
            f"來源：{row['provider']}",
            f"文章 ID：{row['article_id']}",
            f"重要性初分：{row['score']}（標題關鍵字與類別粗估，僅供篩選參考）",
        ])
        if row.get("excerpt"):
            lines.append("全文摘錄：")
            lines.append(row["excerpt"])
    lines.extend(export_footer_lines(len(result.items)))
    return "\r\n".join(lines) + "\r\n"


async def send_ib_export(interaction, ticker, name, start, end, result, message):
    filename = f"{ticker}_ibnews_{start.isoformat()}_{end.isoformat()}.txt"
    content = build_ib_export_text(ticker, name, start, end, result)
    with BytesIO(content.encode("utf-8-sig")) as buffer:
        attachment = discord.File(buffer, filename=filename)
        try:
            await interaction.followup.send(content=message, file=attachment)
        except discord.HTTPException:
            LOG.exception("IBKR 新聞 TXT 發送失敗：%s", ticker)
            try:
                await interaction.followup.send("⚠️ TXT 附件未送出，請確認機器人在此頻道具有「附加檔案」權限。")
            except discord.HTTPException:
                pass
        finally:
            attachment.close()


# ==================== DISCORD 指令註冊接口 ====================
@bot.tree.command(name="news", description="查詢公司相關商業通訊，預設近30天，最多366天")
@app_commands.describe(ticker="股票代號", start_date="起日 YYYY-MM-DD", end_date="迄日 YYYY-MM-DD，包含當日")
async def news(interaction: discord.Interaction, ticker: str, start_date: str | None = None, end_date: str | None = None):
    await respond(interaction, "news", ticker, start_date, end_date)


@bot.tree.command(name="audit", description="查詢SEC近期及歷史申報，預設近30天，最多366天")
@app_commands.describe(ticker="股票代號", start_date="起日 YYYY-MM-DD", end_date="迄日 YYYY-MM-DD，包含當日")
async def audit(interaction: discord.Interaction, ticker: str, start_date: str | None = None, end_date: str | None = None):
    await respond(interaction, "audit", ticker, start_date, end_date)


@bot.tree.command(name="spot", description="查詢美股今日量價結構、預估量比與防守分水嶺")
@app_commands.describe(ticker="股票代號，例如 NVDA、VICR 或 TWLO")
async def spot(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer(thinking=True)
    try:
        ticker = ticker_value(ticker)

        async with _spot_lock:
            data = await asyncio.to_thread(calc_spot_structure_sync, ticker)

        color = 0x2ECC71 if data["change_pct"] >= 0 else 0xE74C3C
        ratio_str = f"{data['ratio']:.2f}x"
        embed = discord.Embed(
            title=f"📊 現貨結構分析：${data['ticker']}",
            color=color
        )
        embed.description = (
            f"**${data['ticker']}** `{format_price_num(data['price'])}` ({format_pct_str(data['change_pct'])})\n\n"
            f"• **盤面狀態**：`{data['status_label']}`（日期：`{data['date']}`）\n"
            f"• **即時量比**：`{ratio_str}`\n"
            f"• **預估金額**：**{format_num_units(data['amount'])}**\n"
            f"• **結構狀態**：{data['struct_status']}\n"
            f"• **防守分水嶺**：`{format_price_num(data['mid_k'])}`（K 棒中軸位）\n"
            f"• **CLV 盤口位置**：`{data['clv']:+.2f}`（-1.0 至 +1.0）\n"
            f"• **前 20 日高低點**：最高 `{format_price_num(data['high20'])}` ｜ 最低 `{format_price_num(data['low20'])}`"
        )
        embed.set_footer(text=f"查詢時間：美東時間 {datetime.now(ET).strftime('%H:%M:%S')}")
        await interaction.followup.send(embed=embed)

    except ValueError as exc:
        await interaction.followup.send(f"⚠️ {exc}")
    except Exception as exc:
        LOG.exception("Spot query failed")
        await interaction.followup.send(f"❌ 查詢處理失敗：{exc}")


@bot.tree.command(name="opt", description="查詢美股近 1 個月主力期權籌碼、PCR 與異常大單")
@app_commands.describe(ticker="股票代號，例如 NVDA、IONQ 或 PLTR")
async def opt(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer(thinking=True)
    try:
        sym = ticker_value(ticker)

        async with _opt_lock:
            data = await asyncio.to_thread(calc_options_structure_sync, sym)

        if data.get("is_cold", False):
            embed = discord.Embed(title=f"🎯 期權主力確認：${data['ticker']}", color=0x95A5A6)
            embed.description = data["msg"]
            embed.set_footer(text=f"查詢時間：美東 {datetime.now(ET).strftime('%H:%M:%S')} ｜ 全市場快檢完成")
            await interaction.followup.send(embed=embed)
            return

        session_meta = get_market_session_meta()
        embed = discord.Embed(
            title=f"🎯 期權主力籌碼確認：${data['ticker']}{session_meta['title_suffix']}",
            color=data["color"]
        )
        vol_pcr_str = format_ratio_str(data["vol_pcr"])
        oi_pcr_str = format_ratio_str(data["oi_pcr"])
        iv_str = format_iv_str(data.get("iv"))
        call_wall_str = f"`{format_price_num(data['call_wall'])}` 附近（未平倉最大壓力）" if data["call_wall"] > 0 else "無顯著集結（未達防禦規模）"
        put_wall_str = f"`{format_price_num(data['put_wall'])}` 附近（未平倉最大支撐）" if data["put_wall"] > 0 else "無顯著集結（未達防禦規模）"

        desc_lines = [
            f"**${data['ticker']}** `{format_price_num(data['price'])}` ｜ 隱含波動率 IV：`{iv_str}`\n",
            f"• **全市場多空偏向**：**{data['bias']}**",
            f"• **{session_meta['vol_label']}**：`{vol_pcr_str}` ｜ **累積未平倉 PCR**：`{oi_pcr_str}`",
            f"• **{session_meta['trade_label']}**：Call `{data['call_vol']}` 張 ｜ Put `{data['put_vol']}` 張",
            f"• **{session_meta['oi_label']}**：Call `{data['call_oi']}` 張 ｜ Put `{data['put_oi']}` 張",
            f"• **期權重力天花板 (Call Wall)**：{call_wall_str}",
            f"• **期權下檔防守線 (Put Wall)**：{put_wall_str}"
        ]

        if data.get("top_near") or data.get("top_far"):
            desc_lines.append(f"\n🔥 **{session_meta['section_cluster']}**")
            if data.get("top_near"):
                desc_lines.append(f"⚡ **近週前線博弈 (`{data['near_exp']}`)**：")
                for c in data["top_near"]:
                    strike_str = format_price_num(c["strike"])
                    desc_lines.append(f"  • `{strike_str} {c['right']}` ｜ 成交 `{c['vol']}` 張 ｜ 底冊 OI {c['oi']} 張")
            if data.get("top_far"):
                desc_lines.append(f"🎯 **遠月主力伏擊 (`{data['far_exp']}`)**：")
                for c in data["top_far"]:
                    strike_str = format_price_num(c["strike"])
                    desc_lines.append(f"  • `{strike_str} {c['right']}` ｜ 成交 `{c['vol']}` 張 ｜ 底冊 OI {c['oi']} 張")

        if data["big_orders"]:
            desc_lines.append(f"\n⚡ **{session_meta['section_order']}**")
            for o in data["big_orders"]:
                amt_str = format_num_units(o["amt"])
                strike_str = format_price_num(o["strike"])
                desc_lines.append(
                    f"• `[{o['exp']}] {strike_str} {o['right']}` ｜ 權利金 **{amt_str}** "
                    f"({o['vol']} 張 ｜ {o['action']} ｜ {o['note']})"
                )
        else:
            desc_lines.append(f"\n🧊 **【主力大單狀態】**：核心合約無單筆大於 $3 萬美元之激進掃單。")

        embed.description = "\n".join(desc_lines)
        now_et = datetime.now(ET)
        now_tw = now_et.astimezone(TW)
        footer_text = f"查詢時間：美東 {now_et.strftime('%H:%M:%S')} (台灣 {now_tw.strftime('%H:%M:%S')}) ｜ {session_meta['footer_mode']}"
        embed.set_footer(text=footer_text)
        await interaction.followup.send(embed=embed)

    except ValueError as exc:
        await interaction.followup.send(f"⚠️ {exc}")
    except Exception as exc:
        LOG.exception("Opt query failed")
        await interaction.followup.send(f"❌ 查詢處理失敗：{exc}")


@bot.command(name="opt")
async def opt_prefix_cmd(ctx, ticker: str = None):
    if not ticker:
        await ctx.send("請輸入股票代號，例如：`!opt NVDA`")
        return

    loading_msg = await ctx.send(f"🔍 正在向市場檢驗 **${ticker.upper()}** 全市場期權總量與主力籌碼...")
    try:
        sym = ticker_value(ticker)
        async with _opt_lock:
            data = await asyncio.to_thread(calc_options_structure_sync, sym)

        if data.get("is_cold", False):
            embed = discord.Embed(title=f"🎯 期權主力確認：${data['ticker']}", color=0x95A5A6)
            embed.description = data["msg"]
            embed.set_footer(text=f"查詢時間：美東 {datetime.now(ET).strftime('%H:%M:%S')} ｜ 全市場快檢完成")
            await loading_msg.edit(content=None, embed=embed)
            return

        session_meta = get_market_session_meta()
        embed = discord.Embed(
            title=f"🎯 期權主力籌碼確認：${data['ticker']}{session_meta['title_suffix']}",
            color=data["color"]
        )
        vol_pcr_str = format_ratio_str(data["vol_pcr"])
        oi_pcr_str = format_ratio_str(data["oi_pcr"])
        iv_str = format_iv_str(data.get("iv"))
        call_wall_str = f"`{format_price_num(data['call_wall'])}` 附近（未平倉最大壓力）" if data["call_wall"] > 0 else "無顯著集結（未達防禦規模）"
        put_wall_str = f"`{format_price_num(data['put_wall'])}` 附近（未平倉最大支撐）" if data["put_wall"] > 0 else "無顯著集結（未達防禦規模）"

        desc_lines = [
            f"**${data['ticker']}** `{format_price_num(data['price'])}` ｜ 隱含波動率 IV：`{iv_str}`\n",
            f"• **全市場多空偏向**：**{data['bias']}**",
            f"• **{session_meta['vol_label']}**：`{vol_pcr_str}` ｜ **累積未平倉 PCR**：`{oi_pcr_str}`",
            f"• **{session_meta['trade_label']}**：Call `{data['call_vol']}` 張 ｜ Put `{data['put_vol']}` 張",
            f"• **{session_meta['oi_label']}**：Call `{data['call_oi']}` 張 ｜ Put `{data['put_oi']}` 張",
            f"• **期權重力天花板 (Call Wall)**：{call_wall_str}",
            f"• **期權下檔防守線 (Put Wall)**：{put_wall_str}"
        ]

        if data.get("top_near") or data.get("top_far"):
            desc_lines.append(f"\n🔥 **{session_meta['section_cluster']}**")
            if data.get("top_near"):
                desc_lines.append(f"⚡ **近週前線博弈 (`{data['near_exp']}`)**：")
                for c in data["top_near"]:
                    strike_str = format_price_num(c["strike"])
                    desc_lines.append(f"  • `{strike_str} {c['right']}` ｜ 成交 `{c['vol']}` 張 ｜ 底冊 OI {c['oi']} 張")
            if data.get("top_far"):
                desc_lines.append(f"🎯 **遠月主力伏擊 (`{data['far_exp']}`)**：")
                for c in data["top_far"]:
                    strike_str = format_price_num(c["strike"])
                    desc_lines.append(f"  • `{strike_str} {c['right']}` ｜ 成交 `{c['vol']}` 張 ｜ 底冊 OI {c['oi']} 張")

        if data["big_orders"]:
            desc_lines.append(f"\n⚡ **{session_meta['section_order']}**")
            for o in data["big_orders"]:
                amt_str = format_num_units(o["amt"])
                strike_str = format_price_num(o["strike"])
                desc_lines.append(
                    f"• `[{o['exp']}] {strike_str} {o['right']}` ｜ 權利金 **{amt_str}** "
                    f"({o['vol']} 張 ｜ {o['action']} ｜ {o['note']})"
                )
        else:
            desc_lines.append(f"\n🧊 **【主力大單狀態】**：核心合約無單筆大於 $3 萬美元之激進掃單。")

        embed.description = "\n".join(desc_lines)
        now_et = datetime.now(ET)
        now_tw = now_et.astimezone(TW)
        footer_text = f"查詢時間：美東 {now_et.strftime('%H:%M:%S')} (台灣 {now_tw.strftime('%H:%M:%S')}) ｜ {session_meta['footer_mode']}"
        embed.set_footer(text=footer_text)
        await loading_msg.edit(content=None, embed=embed)
    except Exception as exc:
        await loading_msg.edit(content=f"⚠️ 查詢失敗：{exc}")


@bot.tree.command(name="ibnews", description="從 IBKR（道瓊、Briefing.com）查詢個股新聞，預設近30天，最多366天")
@app_commands.describe(ticker="股票代號", start_date="起日 YYYY-MM-DD", end_date="迄日 YYYY-MM-DD，包含當日")
async def ibnews(interaction: discord.Interaction, ticker: str, start_date: str | None = None, end_date: str | None = None):
    await interaction.response.defer(thinking=True)
    try:
        ticker = ticker_value(ticker)
        start, end = date_range(start_date, end_date)
        try:
            _, name = await asyncio.to_thread(company_info, ticker)
        except Exception:
            name = None
        if ticker in COMPANY_NEWS_ALIASES:
            name = COMPANY_NEWS_ALIASES[ticker][0]
        async with _ibnews_lock:
            result = await asyncio.to_thread(fetch_ib_news_sync, ticker, start, end, name)
        if result.items:
            try:
                result.notes.append(await asyncio.to_thread(translate_titles, result.items, ticker, name))
            except Exception:
                LOG.exception("標題翻譯失敗")
        name = name or ticker
    except ValueError as exc:
        await interaction.followup.send(clip(f"⚠️ {exc}", 1800))
        return
    except Exception:
        LOG.exception("IBKR news query failed")
        await interaction.followup.send("❌ IBKR 新聞查詢失敗，請查看機器人執行紀錄。")
        return

    description = (
        f"公司：{name}\n日期：{start.isoformat()} 至 {end.isoformat()}（美東時間）\n"
        f"來源：道瓊 DJ-N、Briefing.com 分析師評等；已去除重複與續篇。"
    )
    if result.notes:
        description += "\n" + "；".join(result.notes)

    if not result.items:
        await send_ib_export(interaction, ticker, name, start, end, result,
                             clip("本次檢索未找到符合條件的資料。\n" + description, 1900))
        return

    main_items = [r for r in result.items if r["category"] != "第三方新聞稿"]
    third_count = len(result.items) - len(main_items)
    shown = main_items[:IB_NEWS_EMBED_LIMIT] or result.items[:IB_NEWS_EMBED_LIMIT]
    fields = [
        (f"{i}. [{r['category']}] {r.get('title_zh') or r['title']}",
         (f"原文：{r['title']}\n" if r.get("title_zh") else "") + f"時間：{r['time']}（美東）｜來源：{r['provider']}")
        for i, r in enumerate(shown, 1)
    ]
    if third_count:
        description += f"\n第三方新聞稿 {third_count} 則不在訊息中顯示，放在 TXT 最後。"
    if len(main_items) > len(shown):
        description += f"\n訊息只顯示最新 {len(shown)} 則，完整清單請看 TXT。"
    pages = build_embeds(f"IBKR 新聞：{ticker}", description, fields, 0xF1C40F)

    total_pages = len(pages)
    for page_number, embed in enumerate(pages, start=1):
        embed.set_footer(text=f"第 {page_number}/{total_pages} 頁・共 {len(result.items)} 則")
        try:
            await interaction.followup.send(embed=embed, wait=True)
        except discord.HTTPException:
            LOG.exception("IBKR 新聞發送失敗：%s 第 %s 頁", ticker, page_number)
            break

    await send_ib_export(
        interaction, ticker, name, start, end, result,
        f"✅ {ticker} IBKR 新聞完成：共 {len(result.items)} 則。\n📄 TXT 已附上（含新聞稿全文摘錄），可直接貼給 AI。\n"
        "⚠️ Discord 預覽只顯示前 50 KB，請下載後再看完整內容。",
    )


@bot.event
async def on_ready():
    LOG.info("Bot online: %s", bot.user)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # ib_insync 連線時會用 INFO 印出整份帳戶與持倉（updatePortfolio）和連線過程，
    # 這些資料機器人用不到，只保留警告與錯誤，畫面比較乾淨
    logging.getLogger("ib_insync").setLevel(logging.WARNING)
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print("❌ 尚未設定 DISCORD_BOT_TOKEN 環境變數")
        raise SystemExit(1)
    if not os.environ.get("SEC_USER_AGENT"):
        print("⚠️ 尚未設定 SEC_USER_AGENT 環境變數，/audit 與 /news 會無法使用")
    bot.run(token)
