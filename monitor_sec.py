import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests

# ==================== 環境變數與路徑設定 ====================
DISCORD_SEC_WEBHOOK = os.environ.get("DISCORD_SEC_WEBHOOK") or os.environ.get("DISCORD_NEWS_WEBHOOK")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# SEC 要求真實聯絡資訊，例如 "sec-radar your_email@example.com"，請放在 GitHub Secrets，不要寫在程式裡
SEC_USER_AGENT = (os.environ.get("SEC_USER_AGENT") or "").strip()

HISTORY_FILE = "sent_sec_log.txt"          # 已處理的申報案號（推播、併入每日通知、略過都會記）
DIGEST_FILE = "sec_daily_digest.json"      # 等待併入每日通知的例行申報
CIK_CACHE_FILE = "cik_cache.json"          # 代號對 CIK 的存檔，SEC 名單下載失敗時使用
TICKERS_FILE = "tickers.txt"
HOLDINGS_FILE = "holdings.txt"

TW_TZ = timezone(timedelta(hours=8))
UTC_TZ = timezone.utc
# 美東時間（只用來判斷「今天」的申報日期，夏令時間差一小時不影響）
ET_TZ = timezone(timedelta(hours=-4))

# ==================== AI 模型設定 ====================
# 主力：OpenAI（預設 gpt-4o-mini，最便宜）；失敗時改用 Gemini 備援
# 想換模型時，在 GitHub Secrets 設定 OPENAI_MODEL / GEMINI_MODEL，不必改檔案
OPENAI_MODEL = (os.environ.get("OPENAI_MODEL") or "").strip() or "gpt-4o-mini"
OPENAI_REASONING_EFFORT = (os.environ.get("OPENAI_REASONING_EFFORT") or "").strip()
GEMINI_MODEL = (os.environ.get("GEMINI_MODEL") or "").strip() or "gemini-3.6-flash"

# ==================== 可調整參數 ====================
MAX_LOOKBACK_DAYS = 4            # 只看最近幾天的申報（週一早上也能涵蓋上週五）
HISTORY_KEEP_DAYS = 30           # 紀錄保留天數，超過自動刪除
SEC_MIN_INTERVAL = 0.15          # 每次向 SEC 要資料至少間隔幾秒（SEC 上限每秒 10 次）
FETCH_FAIL_ALERT_RATIO = 0.20    # 抓取失敗比例超過此值，推播警報
ALERT_COOLDOWN_HOURS = 3         # 警報至少間隔幾小時，避免 SEC 封鎖時每小時洗版
MAX_CONSECUTIVE_FETCH_FAILS = 8  # 連續幾檔抓取失敗就判定被封鎖，提前結束並警報
DAILY_REPORT_HOUR_TW = 7         # 每天台灣時間幾點之後的第一次執行，推播健康回報與合併通知
MAX_AI_PER_DAY = 120             # 每天最多呼叫 AI 幾次（以 gpt-4o-mini 計，上限約每月 2 美元）
MAX_RUN_MINUTES = 20             # 整輪最多跑幾分鐘（workflow 上限 30 分鐘）
FORM144_CARD_MIN_VALUE = 1_000_000  # Form 144 預計賣出金額達到這個數字（美元）才單獨推卡；持股一律推卡
DOC_TEXT_LIMIT = 4500            # 送 AI 的原文長度上限（字元）

# 同一家公司的不同股票代號，統一成一個
TICKER_CANONICAL = {"GOOG": "GOOGL"}

STATE_VERSION = "2"

# 持股清單從 holdings.txt 讀取，做法和新聞版一致
HOLDINGS = set()

# CIK 最後備援（只在 SEC 名單下載失敗、而且也沒有存檔時才用）
CORE_FALLBACK_CIK = {
    "AAPL": "0000320193", "NVDA": "0001045810", "MSFT": "0000789019", "GOOGL": "0001652044",
    "AMZN": "0001018724", "TSLA": "0001318605", "QCOM": "0000804328", "AVGO": "0001730168",
    "INTC": "0000050863", "TSM": "0001046179", "ASML": "0000937966", "CSCO": "0000858877",
    "IBM": "0000051143",
}

# ==================== 表單分類 ====================
DIGEST_PERIODIC_FORMS = {"10-Q", "10-Q/A", "10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
LATE_FILING_FORMS = {"NT 10-Q", "NT 10-K", "NT 20-F", "NT 10-Q/A", "NT 10-K/A", "NT 20-F/A"}
FORM4_FORMS = {"4", "4/A"}
FORM144_FORMS = {"144", "144/A"}
SC13D_FORMS = {"SCHEDULE 13D", "SCHEDULE 13D/A", "SC 13D", "SC 13D/A"}
SC13G_FORMS = {"SCHEDULE 13G", "SCHEDULE 13G/A", "SC 13G", "SC 13G/A"}
OFFERING_FORMS = {"424B3", "424B4", "424B5", "424B7"}
REGISTRATION_FORMS = {"S-1", "S-1/A", "S-3", "S-3/A", "S-3ASR", "F-1", "F-1/A", "F-3", "F-3/A", "F-3ASR"}
CURRENT_REPORT_FORMS = {"8-K", "8-K/A", "6-K", "6-K/A"}

TARGET_FORMS = (DIGEST_PERIODIC_FORMS | LATE_FILING_FORMS | FORM4_FORMS | FORM144_FORMS
                | SC13D_FORMS | SC13G_FORMS | OFFERING_FORMS | REGISTRATION_FORMS | CURRENT_REPORT_FORMS)

# 舊版程式就有追蹤的表單；換新版第一次執行時，舊版沒追蹤的表單只記錄、不推舊申報
LEGACY_FORMS = {"8-K", "8-K/A", "6-K", "6-K/A", "10-Q", "10-Q/A", "NT 10-Q", "10-K", "10-K/A",
                "NT 10-K", "424B5", "424B7", "S-3", "S-3ASR", "S-3/A"}

# 8-K 條款：一級硬核條款
HIGH_IMPACT_8K_ITEMS = {
    "1.01", "1.02", "1.03", "1.05",
    "2.01", "2.02", "2.03", "2.04", "2.05", "2.06",
    "3.01", "3.02", "3.03",
    "4.01", "4.02",
    "5.01", "5.02"
}
# 8-K 條款：二級自願揭露
SECONDARY_8K_ITEMS = {"7.01", "8.01"}
# 8-K 條款：例行，併入每日通知
DIGEST_8K_ITEMS = {"5.07": "股東會投票結果"}

# 6-K 例行公告（本地過濾，不送 AI）：庫藏股買回進度、投票權總數、經理人交易申報等
ROUTINE_6K_PATTERNS = [
    r"transactions? in (?:its )?own shares",
    r"repurchase of (?:its )?own shares",
    r"purchase of (?:its )?own shares",
    r"share (?:re)?purchases? (?:program|programme|report|update)",
    r"(?:transactions|report|update|progress) (?:under|on|of) (?:its |the )?(?:current )?(?:share )?buy[- ]?back",
    r"share buy[- ]?back (?:report|update|transactions)",
    r"weekly (?:report|update) on (?:the )?share",
    r"total voting rights",
    r"managers'? transactions",
    r"director(?:s|'s)? dealings?",
    r"notification of (?:major )?holdings",
    r"block listing",
]
# 出現這些字眼代表「宣布新的買回計畫」之類的實質消息，不當成例行公告
NON_ROUTINE_6K_PATTERNS = [
    r"\b(?:new|launch(?:es|ed)?|announc(?:es|ed|ing)|authori[sz](?:es|ed|ation)|approv(?:es|ed|al)|increase[sd]?)\b.{0,40}buy[- ]?back",
    r"buy[- ]?back.{0,40}\b(?:new|launch(?:es|ed)?|authori[sz](?:es|ed|ation)|approv(?:es|ed|al))\b",
    r"\b(?:results|earnings|revenue|guidance|acquisition|merger|dividend)\b",
]


# ==================== SEC 連線 ====================
class SecFetchError(Exception):
    """連線失敗、被 SEC 拒絕、超時等，和「沒有申報」分開計算"""


class SecNotFound(Exception):
    """SEC 沒有這份資料（404），例如代號不在 SEC 申報"""


_last_sec_request = 0.0


def sec_get(url, timeout=20):
    """向 SEC 要資料；遇到限流或伺服器錯誤會等待重試。失敗丟出 SecFetchError，404 丟出 SecNotFound"""
    global _last_sec_request
    headers = {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    last_error = ""
    for attempt in range(3):
        wait = SEC_MIN_INTERVAL - (time.monotonic() - _last_sec_request)
        if wait > 0:
            time.sleep(wait)
        _last_sec_request = time.monotonic()
        try:
            res = requests.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            last_error = f"連線異常 {type(e).__name__}"
            time.sleep(2 * (attempt + 1))
            continue
        if res.status_code == 200:
            return res
        if res.status_code == 404:
            raise SecNotFound(url)
        last_error = f"HTTP {res.status_code}"
        if res.status_code in (403, 429, 500, 502, 503, 504):
            # 403 通常是被 SEC 判定為未申報身分的機器人或限流，等久一點再試
            time.sleep((6 if res.status_code in (403, 429) else 3) * (attempt + 1))
            continue
        break
    raise SecFetchError(last_error or "未知錯誤")


def filing_base_url(cik, accession):
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"


def filing_view_url(cik, accession, primary_doc):
    """給人看的網址（XML 表單會是 SEC 轉好的網頁版）"""
    return filing_base_url(cik, accession) + primary_doc if primary_doc else \
        filing_base_url(cik, accession) + f"{accession}-index.htm"


def filing_raw_url(cik, accession, primary_doc):
    """原始檔網址：Form 4、144、13D/G 的主文件路徑前面有 xsl 開頭的資料夾，拿掉才是原始 XML"""
    doc = primary_doc
    if "/" in doc and doc.split("/", 1)[0].lower().startswith("xsl"):
        doc = doc.split("/", 1)[1]
    return filing_base_url(cik, accession) + doc


# ==================== 文字處理 ====================
def clean_html_to_text(raw):
    text = re.sub(r"<ix:header[\s\S]*?</ix:header>", " ", raw, flags=re.I)   # 內嵌 XBRL 的隱藏資料
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    return " ".join(text.split())


def fetch_text(url):
    return clean_html_to_text(sec_get(url).text)


def strip_tags(s):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).replace("\xa0", " ").split())


def parse_index_page(page_html, index_url):
    """
    解析申報的 -index.htm 頁面（沿用 bot_interactive.py 的 fetch_filing_exhibits 寫法）
    回傳 {"docs": [{"desc", "name", "type", "url"}], "filed_by": [申報人名稱]}
    """
    row_pattern = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)
    td_pattern = re.compile(r"<td[^>]*>(.*?)</td>", re.I | re.S)
    a_pattern = re.compile(r'<a\s+[^>]*href=["\'](.*?)["\'][^>]*>(.*?)</a>', re.I | re.S)
    docs = []
    for tr in row_pattern.findall(page_html):
        tds = td_pattern.findall(tr)
        if len(tds) < 4:
            continue
        a_match = a_pattern.search(tds[2])
        if not a_match:
            continue
        href = a_match.group(1).strip()
        # 內嵌 XBRL 文件的連結是 /ix?doc=/Archives/...，拿掉前綴才是原始檔
        href = re.sub(r"^/ix\?doc=", "", href)
        docs.append({
            "desc": strip_tags(tds[1]),
            "name": strip_tags(a_match.group(2)),
            "type": strip_tags(tds[3]).upper(),
            "url": urljoin(index_url, href),
        })
    filed_by = []
    for span in re.findall(r'<span class="companyName">(.*?)</span>', page_html, re.I | re.S):
        text = strip_tags(span)
        if "(Filed by)" in text:
            name = text.split("(Filed by)")[0].strip()
            if name and name not in filed_by:
                filed_by.append(name)
    return {"docs": docs, "filed_by": filed_by}


def fetch_filing_index(cik, accession):
    index_url = filing_base_url(cik, accession) + f"{accession}-index.htm"
    return parse_index_page(sec_get(index_url).text, index_url)


def exhibit_docs(index_info, prefix="EX-99"):
    return [d for d in index_info["docs"]
            if d["type"].startswith(prefix) and d["url"].lower().endswith((".htm", ".html", ".txt"))]


def start_from_first_item(text):
    """8-K 主文件前面是封面勾選框，從第一個「Item x.xx」開始才是內容"""
    m = re.search(r"\bItem\s*\d\.\d{2}\b", text, re.I)
    return text[m.start():] if m else text


# ==================== AI 判讀 ====================
AI_USAGE = {"date": "", "count": 0}


def is_reasoning_model(model):
    return model.lower().startswith(("gpt-5", "o1", "o3", "o4"))


def openai_text(prompt, attempts=2):
    """呼叫 OpenAI，回傳文字；失敗回傳 None。不支援的參數會自動拿掉重試（做法同新聞版）"""
    if not OPENAI_API_KEY:
        return None
    reasoning = is_reasoning_model(OPENAI_MODEL)
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": "你是一位硬核買方機構研究員，講求事實與數據，說話自然順暢，嚴格輸出指定格式。"},
            {"role": "user", "content": prompt},
        ],
    }
    if not reasoning:
        payload["temperature"] = 0.1
    if OPENAI_REASONING_EFFORT and reasoning:
        payload["reasoning_effort"] = OPENAI_REASONING_EFFORT
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    tries = 0
    while tries < attempts:
        tries += 1
        try:
            res = requests.post("https://api.openai.com/v1/chat/completions",
                                headers=headers, json=payload, timeout=60 if reasoning else 25)
            if res.status_code == 429 or res.status_code >= 500:
                time.sleep(3 * tries)
                continue
            data = res.json()
            if "choices" not in data:
                err = data.get("error", data)
                msg = str(err.get("message", err) if isinstance(err, dict) else err)
                removed = False
                for param in ("temperature", "reasoning_effort"):
                    if param in payload and param in msg.lower():
                        payload.pop(param)
                        removed = True
                if removed:
                    tries -= 1
                    continue
                print(f"      ⚠️ [OpenAI 回傳錯誤] {msg[:150]}", flush=True)
                time.sleep(2)
                continue
            content = (data["choices"][0]["message"]["content"] or "").strip()
            if content:
                return content
        except Exception as e:
            print(f"      ⚠️ [OpenAI 呼叫異常] {type(e).__name__}", flush=True)
            time.sleep(2)
    return None


def gemini_text(prompt):
    """Gemini 備援。金鑰放在請求標頭，不放網址，避免錯誤訊息把金鑰印進執行紀錄"""
    if not GEMINI_API_KEY:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 800, "thinkingConfig": {"thinkingLevel": "MINIMAL"}},
    }
    for attempt in range(2):
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=25)
            if res.status_code == 429:
                time.sleep(8)
                continue
            if res.status_code == 400 and "thinking" in res.text.lower() and "thinkingConfig" in payload["generationConfig"]:
                payload["generationConfig"].pop("thinkingConfig")   # 模型不支援思考設定就拿掉重試
                continue
            if res.status_code != 200:
                print(f"      ⚠️ [Gemini 回應異常 HTTP {res.status_code}]", flush=True)
                return None
            candidates = res.json().get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts if not p.get("thought", False)).strip()
                if text:
                    return text
            return None
        except Exception as e:
            print(f"      ⚠️ [Gemini 連線異常] {type(e).__name__}", flush=True)
            return None
    return None


AI_FAILED_TEXT = "• **【AI 解讀】**：AI 解讀暫時無法取得，請點擊標題閱讀原文。"
AI_QUOTA_TEXT = "• **【AI 解讀】**：今日 AI 解讀額度已用完（控制費用），請點擊標題閱讀原文。"


def ai_summarize(ticker, filing_context, doc_text, stats):
    """回傳摘要文字；AI 都失敗時回傳誠實的提示文字，不寫假內容"""
    if not doc_text or len(doc_text.strip()) < 40:
        return "• **【AI 解讀】**：申報內文過短或無法擷取，請點擊標題閱讀原文。"
    if not OPENAI_API_KEY and not GEMINI_API_KEY:
        return "• **【AI 解讀】**：未設定 AI 金鑰，請點擊標題閱讀原文。"
    if AI_USAGE["count"] >= MAX_AI_PER_DAY:
        stats["ai_quota"] += 1
        return AI_QUOTA_TEXT

    prompt = f"""
你是一位分毫不差的美股買方分析師。標的【{ticker}】提交了 SEC 官方申報（類型：{filing_context}）。
以下是該份文件的官方原文節錄：
\"\"\"{doc_text[:DOC_TEXT_LIMIT + 2500]}\"\"\"

【嚴格禁令】：
1. 嚴禁機械套話！絕對禁止寫「對手方為...」、「交易/合約性質為...」等生硬套話。
2. 嚴禁使用「提升市場地位、增強競爭力、帶來正面影響、後市可期、具戰略意義」等空洞公關廢話。
3. 原文沒寫的數字不要自己編。

【輸出要求（請像專業研究員用自然大白話直接講重點）】：
• **【核心要點】**：一句話白話講清楚到底發生了什麼事（融資金額與利率、重大合約、資產收購處分、財報數字、高層異動或關鍵時程）。（繁體中文，40-65 字）
• **【財務影響】**：直擊實質財務衝擊（營收貢獻、毛利變化、負債壓力、或股本稀釋風險）。（繁體中文，40-65 字）
"""
    AI_USAGE["count"] += 1
    stats["ai_calls"] += 1
    text = openai_text(prompt)
    if not text:
        if GEMINI_API_KEY:
            print("      🛡️ [備援] OpenAI 未成功，改用 Gemini", flush=True)
        text = gemini_text(prompt)
    if not text:
        stats["ai_failed"] += 1
        return AI_FAILED_TEXT
    return text


# ==================== 各類表單判讀 ====================
def holding_mark(ticker):
    return "★ 持股｜" if ticker in HOLDINGS else ""


def card(ticker, title, color, tag, summary):
    return {"action": "card",
            "intel": {"title": f"{holding_mark(ticker)}{title}：{ticker}"[:250],
                      "color": color, "tag": tag, "summary": summary}}


def digest(text):
    return {"action": "digest", "text": text}


def skip(reason):
    return {"action": "skip", "reason": reason}


def fmt_money(value):
    if value is None:
        return "未知"
    if value >= 1e9:
        return f"${value / 1e9:.2f}B"
    if value >= 1e6:
        return f"${value / 1e6:.2f}M"
    return f"${value:,.0f}"


def to_float(s):
    try:
        return float(str(s).replace(",", "").replace("$", "").strip())
    except Exception:
        return None


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def find_first(elem, name):
    for e in elem.iter():
        if local_name(e.tag) == name:
            return e
    return None


def find_text(elem, name, sub=None):
    """找第一個名稱相符的節點文字；sub 指定時取其底下 sub 節點的文字（例如 transactionShares/value）"""
    e = find_first(elem, name)
    if e is None:
        return None
    if sub:
        e = find_first(e, sub)
        if e is None:
            return None
    return (e.text or "").strip() or None


def parse_form4(xml_text):
    """回傳 (申報人, 身分, 公開市場買進股數, 買進金額)；只計算交易代碼 P"""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    owners, roles = [], []
    for ro in root.iter():
        if local_name(ro.tag) != "reportingOwner":
            continue
        name = find_text(ro, "rptOwnerName")
        if name:
            owners.append(name)
        if find_text(ro, "isDirector") in ("1", "true"):
            roles.append("董事")
        if find_text(ro, "isOfficer") in ("1", "true"):
            roles.append(f"高階主管（{find_text(ro, 'officerTitle') or '職稱未填'}）")
        if find_text(ro, "isTenPercentOwner") in ("1", "true"):
            roles.append("10% 以上大股東")
    shares_total, value_total = 0.0, 0.0
    for tx in root.iter():
        if local_name(tx.tag) != "nonDerivativeTransaction":
            continue
        if find_text(tx, "transactionCode") != "P":
            continue
        if find_text(tx, "transactionAcquiredDisposedCode", "value") not in (None, "A"):
            continue
        shares = to_float(find_text(tx, "transactionShares", "value")) or 0.0
        price = to_float(find_text(tx, "transactionPricePerShare", "value")) or 0.0
        shares_total += shares
        value_total += shares * price
    return "、".join(owners) or "未知", "、".join(dict.fromkeys(roles)) or "未註明", shares_total, value_total


def handle_form4(ticker, f, cik):
    xml_text = sec_get(filing_raw_url(cik, f["accessionNumber"], f["primaryDocument"])).text
    try:
        owner, role, shares, value = parse_form4(xml_text)
    except ET.ParseError:
        return skip("Form 4 格式無法解析")
    if shares <= 0:
        return skip("Form 4 非公開市場買進")
    avg = value / shares if shares else 0
    summary = (f"• **【申報人】**：{owner}（{role}）\n"
               f"• **【公開市場買進】**：{shares:,.0f} 股，均價約 ${avg:,.2f}，合計約 {fmt_money(value)}\n"
               f"• **【解讀】**：內部人自掏腰包在市場上買進（交易代碼 P），通常被視為對公司前景有信心的訊號。")
    return card(ticker, "🟢 【內部人公開市場買進】", 0x27AE60, f"{f['form']} (內部人持股變動)", summary)


def parse_form144(xml_text):
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    seller = find_text(root, "nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold")
    relations = [(e.text or "").strip() for e in root.iter()
                 if local_name(e.tag) == "relationshipToIssuer" and (e.text or "").strip()]
    shares = to_float(find_text(root, "noOfUnitsSold"))
    value = to_float(find_text(root, "aggregateMarketValue"))
    sale_date = find_text(root, "approxSaleDate")
    return seller, "、".join(dict.fromkeys(relations)), shares, value, sale_date


def handle_form144(ticker, f, cik):
    seller, relation, shares, value, sale_date = None, "", None, None, None
    try:
        xml_text = sec_get(filing_raw_url(cik, f["accessionNumber"], f["primaryDocument"])).text
        seller, relation, shares, value, sale_date = parse_form144(xml_text)
    except (ET.ParseError, SecNotFound):
        pass
    who = f"{seller or '申報人未知'}" + (f"（{relation}）" if relation else "")
    share_text = f"{shares:,.0f} 股" if shares else "股數未知"
    if ticker in HOLDINGS or (value is not None and value >= FORM144_CARD_MIN_VALUE):
        summary = (f"• **【申報人】**：{who}\n"
                   f"• **【預計賣出】**：{share_text}，市值約 {fmt_money(value)}，預計日期 {sale_date or '未填'}\n"
                   f"• **【解讀】**：Form 144 是內部人「預告」要賣股，實際成交會在之後的 Form 4 揭露；"
                   f"常見原因包含既定的 10b5-1 售股計畫或股票獎酬變現。")
        return card(ticker, "🟠 【內部人預告賣股 Form 144】", 0xE67E22, f"{f['form']} (擬出售證券通知)", summary)
    return digest(f"Form 144 內部人預告賣股：{who}，{share_text}，約 {fmt_money(value)}")


PERCENT_TAG_RE = re.compile(r"<(?:\w+:)?(\w*[Pp]ercent\w*)[^>]*>\s*([\d.]+)\s*<", re.S)
PERCENT_TEXT_RE = re.compile(r"percent of class represented by amount in row \(?\d+\)?\s*:?\s*([\d.]+)\s*%", re.I)


def extract_13dg_percent(raw):
    m = PERCENT_TAG_RE.search(raw)
    if m:
        return to_float(m.group(2))
    m = PERCENT_TEXT_RE.search(clean_html_to_text(raw))
    return to_float(m.group(1)) if m else None


def handle_13dg(ticker, f, cik):
    form = f["form"]
    is_13d = form in SC13D_FORMS
    is_amend = form.endswith("/A")
    filers, percent = [], None
    try:
        filers = fetch_filing_index(cik, f["accessionNumber"])["filed_by"]
    except SecNotFound:
        pass
    try:
        raw = sec_get(filing_raw_url(cik, f["accessionNumber"], f["primaryDocument"])).text
        percent = extract_13dg_percent(raw)
    except SecNotFound:
        pass
    who = "、".join(filers[:3]) or "申報人未知"
    pct_text = f"{percent:.2f}%" if percent is not None else "未能擷取"
    if is_13d:
        summary = (f"• **【申報人】**：{who}\n"
                   f"• **【持股比例】**：{pct_text}{'（修正申報）' if is_amend else ''}\n"
                   f"• **【解讀】**：13D 代表持股超過 5% 且可能介入經營（例如要求董事席次、推動併購或改組），"
                   f"屬主動型大股東，值得細看原文的「交易目的」段落。")
        return card(ticker, "🟣 【主動型大股東 13D】", 0x8E44AD, f"{form} (持股 5% 以上・主動)", summary)
    if is_amend and ticker not in HOLDINGS:
        return digest(f"13G 修正：{who}，持股 {pct_text}")
    summary = (f"• **【申報人】**：{who}\n"
               f"• **【持股比例】**：{pct_text}{'（修正申報）' if is_amend else ''}\n"
               f"• **【解讀】**：13G 是被動型投資人（基金、機構）持股超過 5% 的申報，不打算介入經營。")
    return card(ticker, "🔵 【被動型大股東 13G】", 0x2980B9, f"{form} (持股 5% 以上・被動)", summary)


def first_money(text):
    """找封面上第一個像「發行總額」的金額（一百萬美元以上），略過每股價格這類小數字"""
    for m in re.finditer(r"(?:US)?\$\s?([\d,]+(?:\.\d+)?)(\s?(?:million|billion))?", text, re.I):
        value = to_float(m.group(1)) or 0
        if m.group(2) or value >= 1_000_000:
            return m.group(0).strip()
    return None


def classify_424b5(head):
    """
    依說明書封面判斷 424B5 是發股還是發債（只看封面，因為發債說明書後段也常提到普通股）
    回傳 (類型, 顏色, 標題, 解讀)
    """
    low = head.lower()
    if re.search(r"convertible (?:senior )?(?:unsecured )?(?:notes|debentures|preferred)", low):
        return ("CONVERT", 0xE67E22, "⚠️ 【可轉債發行】",
                "發行可轉換公司債：先借錢，未來可能轉成股票，有潛在稀釋；轉換價通常高於現價。")
    if re.search(r"at[- ]the[- ]market|equity distribution agreement|sales agreement|distribution agreement", low):
        return ("ATM", 0xE74C3C, "⚠️ 【ATM 市價增發股票】",
                "啟動或擴大 ATM（在市場上逐步賣新股）計畫，股本會持續增加，對每股盈餘有稀釋壓力。")
    debt = re.search(r"\b(?:notes|debentures|bonds) due\b|\bsenior (?:unsecured |secured )?notes\b|\d% notes\b", low)
    equity = re.search(r"common stock|ordinary shares|american depositary shares|\badss?\b|common shares|"
                       r"pre-funded warrants|shares of (?:our )?(?:class [a-z] )?common", low)
    if debt and (not equity or debt.start() < equity.start()):
        return ("DEBT", 0x7F8C8D, "🏦 【公司發債】",
                "發行公司債（借錢），不會增加股數、沒有股本稀釋；要留意利率高低與負債增加。")
    if equity:
        return ("EQUITY", 0xE74C3C, "⚠️ 【增發新股】",
                "公開發行新股（現金增資），股本增加，對每股盈餘有稀釋壓力；留意發行價相對市價的折價幅度。")
    return ("UNKNOWN", 0xE67E22, "⚠️ 【公開發行說明書 424B5】",
            "未能從封面判斷是發股或發債，請點擊標題確認。")


def offering_head(cik, f):
    text = fetch_text(filing_view_url(cik, f["accessionNumber"], f["primaryDocument"]))
    m = re.search(r"filed pursuant to rule 424\(b\)\(\d\)", text, re.I)
    start = m.start() if m else 0
    return text[start:start + 4000]


def handle_offering(ticker, f, cik):
    form = f["form"]
    if form == "424B7":
        return card(ticker, "⚠️ 【既有股東轉售股票】", 0xE67E22, "424B7 (轉售說明書)",
                    "• **【核心要點】**：現有股東（創投、創辦人或機構）登記要賣出持股，錢不進公司。\n"
                    "• **【財務影響】**：不稀釋股本，但會增加市場上的賣壓，留意短期承接力道。")
    head = offering_head(cik, f)
    low = head.lower()
    prelim = "subject to completion" in low
    amount = first_money(head)
    amount_text = f"\n• **【封面金額】**：{amount}（僅供參考，以原文為準）" if amount else ""
    stage = "（初步說明書，尚未定價）" if prelim else ""

    if form == "424B5":
        kind, color, title, meaning = classify_424b5(head)
        if kind == "DEBT" and ticker not in HOLDINGS:
            return digest(f"424B5 公司發債{stage}" + (f"：{amount}" if amount else ""))
        return card(ticker, title, color, f"424B5 (公開發行補充說明書){stage}",
                    f"• **【判讀】**：{meaning}{amount_text}\n• **【依據】**：依說明書封面文字判斷，非 AI。")

    resale = re.search(r"selling (?:stock|share|security|unit)holders?", low)
    if form == "424B3" and re.search(r"prospectus supplement no\.?\s*\d+", low) and ticker not in HOLDINGS:
        return digest("424B3 既有說明書例行更新")
    if resale:
        return card(ticker, "⚠️ 【股東轉售登記】", 0xE67E22, f"{form} (轉售說明書){stage}",
                    "• **【判讀】**：登記讓既有股東（常見為私募投資人、可轉債或認股權證持有人）可以在市場上賣股，"
                    f"錢不進公司，但會增加賣壓。{amount_text}")
    if form == "424B4":
        return card(ticker, "⚠️ 【發行最終定價 424B4】", 0xE74C3C, "424B4 (定價說明書)",
                    "• **【判讀】**：IPO 或增資已定價的最終說明書，新股即將交割，股本增加。"
                    f"{amount_text}")
    return card(ticker, "⚠️ 【公開發行說明書】", 0xE67E22, f"{form}{stage}",
                f"• **【判讀】**：發行新證券的說明書，請點擊標題確認發行內容。{amount_text}")


def handle_registration(ticker, f, cik):
    form = f["form"]
    if form.endswith("/A"):
        return digest(f"{form} 註冊文件修正")
    if form in ("S-3ASR", "F-3ASR"):
        return card(ticker, "📄 【大型公司例行貨架登記】", 0x95A5A6, f"{form} (自動生效貨架登記)",
                    "• **【說明】**：只有大型公司能用的自動生效貨架登記，多數是每三年例行更新，"
                    "不代表馬上要發股；真正發行時會另有 424B 申報。")
    if form in ("S-3", "F-3"):
        return card(ticker, "📑 【融資額度登記（貨架）】", 0xF39C12, f"{form} (貨架登記申請)",
                    "• **【說明】**：申請未來三年內可隨時發行股票或債券的總額度。"
                    "中小型公司申請通常是融資前兆，市場常視為稀釋訊號。")
    # S-1 / F-1
    try:
        head = fetch_text(filing_view_url(cik, f["accessionNumber"], f["primaryDocument"]))[:6000].lower()
    except SecNotFound:
        head = ""
    if re.search(r"selling (?:stock|share|security)holders?", head):
        meaning = "登記讓既有股東轉售股票（錢不進公司），常見於私募或可轉債之後，會增加賣壓。"
    elif "initial public offering" in head:
        meaning = "首次公開發行（IPO）註冊。"
    else:
        meaning = "發行新股的註冊文件，中小型公司常見的融資方式，有股本稀釋風險。"
    return card(ticker, "⚠️ 【發行註冊 S-1/F-1】", 0xE67E22, f"{form} (證券發行註冊)", f"• **【判讀】**：{meaning}")


def is_routine_6k(texts):
    joined = " | ".join(t for t in texts if t).lower()
    if not any(re.search(p, joined) for p in ROUTINE_6K_PATTERNS):
        return False
    return not any(re.search(p, joined) for p in NON_ROUTINE_6K_PATTERNS)


def handle_6k(ticker, f, cik, stats):
    # 第一關：只看申報清單裡的文件說明（不用另外連線）
    if is_routine_6k([f.get("primaryDocDescription", "")]):
        stats["routine_6k"] += 1
        return skip("例行 6-K（文件說明）")
    index_info = fetch_filing_index(cik, f["accessionNumber"])
    exhibits = exhibit_docs(index_info)
    # 第二關：看附件說明
    if is_routine_6k([d["desc"] for d in exhibits] + [d["desc"] for d in index_info["docs"][:1]]):
        stats["routine_6k"] += 1
        return skip("例行 6-K（附件說明）")
    doc_text = ""
    for d in exhibits[:2]:
        doc_text += fetch_text(d["url"])[:DOC_TEXT_LIMIT] + " "
        if len(doc_text) >= DOC_TEXT_LIMIT:
            break
    if len(doc_text.strip()) < 200:
        doc_text = fetch_text(filing_view_url(cik, f["accessionNumber"], f["primaryDocument"]))
    # 第三關：看內文開頭（標題）
    if is_routine_6k([doc_text[:400]]):
        stats["routine_6k"] += 1
        return skip("例行 6-K（內文標題）")
    summary = ai_summarize(ticker, f"{f['form']} (外國發行人重大備案)", doc_text[:DOC_TEXT_LIMIT], stats)
    return card(ticker, "🌍 【外國公司重大公告 6-K】", 0x9B59B6, f"{f['form']} (外國發行人重大備案)", summary)


def build_8k_text(cik, f, index_info):
    main_text = start_from_first_item(fetch_text(filing_view_url(cik, f["accessionNumber"], f["primaryDocument"])))
    parts = [main_text[:2500]]
    for d in exhibit_docs(index_info)[:1]:
        if not d["url"].endswith(f["primaryDocument"]):
            parts.append("【附件新聞稿】" + fetch_text(d["url"])[:DOC_TEXT_LIMIT])
    return " ".join(parts)


def handle_8k(ticker, f, cik, stats):
    items_str = f["items"]
    tokens = set(re.findall(r"\d+\.\d+", items_str))
    if tokens and not (tokens & (HIGH_IMPACT_8K_ITEMS | SECONDARY_8K_ITEMS)):
        for item, label in DIGEST_8K_ITEMS.items():
            if item in tokens:
                return digest(f"8-K {label}（項目 {items_str}）")
        return skip(f"8-K 例行項目 {items_str}")
    index_info = fetch_filing_index(cik, f["accessionNumber"])
    doc_text = build_8k_text(cik, f, index_info)
    summary = ai_summarize(ticker, f"8-K 項目 {items_str or '未標示'}", doc_text, stats)
    if tokens & HIGH_IMPACT_8K_ITEMS:
        return card(ticker, "⚡ 【8-K 重大申報】", 0x2ECC71, f"{f['form']} (核心項目: {items_str})", summary)
    return card(ticker, "📑 【8-K 公告解讀】", 0x34495E, f"{f['form']} (項目: {items_str or '未標示'})", summary)


PERIODIC_LABELS = {"10-Q": "季報", "10-K": "年報", "20-F": "外國公司年報", "40-F": "加拿大公司年報"}


def handle_filing(ticker, f, cik, stats):
    form = f["form"].upper().strip()
    if form in LATE_FILING_FORMS:
        base = form.replace("NT ", "").replace("/A", "")
        return card(ticker, "🚨 【財報延遲申報】", 0xC0392B, f"{form} (無法如期繳交 {base})",
                    f"• **【核心要點】**：{ticker} 向 SEC 申報無法如期繳交 {base} 定期報告。\n"
                    "• **【財務影響】**：可能涉及內部控制缺失、審計問題或財務重編，也可能只是併購等技術性延誤，請看原文說明的理由。")
    if form in DIGEST_PERIODIC_FORMS:
        base = form.replace("/A", "")
        return digest(f"{form} {PERIODIC_LABELS.get(base, '定期報告')}" + ("修正" if form.endswith("/A") else ""))
    if form in FORM4_FORMS:
        return handle_form4(ticker, f, cik)
    if form in FORM144_FORMS:
        return handle_form144(ticker, f, cik)
    if form in SC13D_FORMS or form in SC13G_FORMS:
        return handle_13dg(ticker, f, cik)
    if form in OFFERING_FORMS:
        return handle_offering(ticker, f, cik)
    if form in REGISTRATION_FORMS:
        return handle_registration(ticker, f, cik)
    if form.startswith("6-K"):
        return handle_6k(ticker, f, cik, stats)
    if form.startswith("8-K"):
        return handle_8k(ticker, f, cik, stats)
    return skip("非追蹤表單")


# ==================== DISCORD 推播 ====================
def post_to_discord(payload):
    """回傳 True 代表推播成功；遇到 Discord 速率限制會依 retry_after 等待後重試"""
    if not DISCORD_SEC_WEBHOOK:
        print("      ❌ [環境變數警告] 未設定 DISCORD_SEC_WEBHOOK", flush=True)
        return False
    for attempt in range(4):
        try:
            res = requests.post(DISCORD_SEC_WEBHOOK, json=payload, timeout=15)
            if res.status_code == 429:
                try:
                    wait = float(res.json().get("retry_after", 2))
                except Exception:
                    wait = 2.0
                print(f"      ⏳ [Discord 速率限制] 等待 {wait:.1f} 秒後重試", flush=True)
                time.sleep(min(wait + 0.5, 30))
                continue
            if 200 <= res.status_code < 300:
                return True
            print(f"      ❌ [Discord 發送失敗] HTTP {res.status_code}", flush=True)
            time.sleep(2)
        except Exception as e:
            print(f"      ❌ [Discord 連線異常] {type(e).__name__}", flush=True)
            time.sleep(2)
    return False


def send_filing_card(ticker, f, cik, intel):
    now_tw_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
    payload = {
        "username": "SEC EDGAR Intelligence",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/2620/2620578.png",
        "embeds": [{
            "title": intel["title"],
            "url": filing_view_url(cik, f["accessionNumber"], f["primaryDocument"]),
            "color": intel["color"],
            "fields": [
                {"name": "📌 標的代號", "value": f"`{ticker}`", "inline": True},
                {"name": "📄 官方表單", "value": f"`{intel['tag'][:200]}`", "inline": True},
                {"name": "📅 申報日期", "value": f"`{f['filingDate']}`", "inline": True},
                {"name": "💡 重點解讀", "value": intel["summary"][:1000], "inline": False},
            ],
            "footer": {"text": f"SEC EDGAR 原文直達 • 案號: {f['accessionNumber']} • 推播: {now_tw_str}"},
        }],
    }
    return post_to_discord(payload)


def chunk_lines(lines, limit=3800):
    chunks, cur = [], ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        chunks.append(cur)
    return chunks


def health_lines(stats, total):
    failed = stats["fetch_failed"]
    failed_text = ("、".join(failed[:15]) + ("…" if len(failed) > 15 else "")) if failed else "無"
    lines = [
        f"掃描標的：{total} 檔（SEC 對照表來源：{stats['cik_source']}）",
        f"連線失敗：{len(failed)} 檔（{failed_text}）",
        f"成功查詢：{stats['sec_ok']} 檔，其中近期有追蹤表單 {stats['with_filings']} 檔",
        f"推播卡片：{stats['pushed']} 則；推播失敗（下次重試）：{stats['push_failed']} 則",
        f"併入每日通知：{stats['digested']} 則；略過例行 6-K：{stats['routine_6k']} 則；略過其他例行申報：{stats['skipped']} 則",
        f"AI 呼叫：{stats['ai_calls']} 次（今日累計 {AI_USAGE['count']}/{MAX_AI_PER_DAY}），AI 失敗：{stats['ai_failed']} 次",
    ]
    if stats["doc_failed"]:
        lines.append(f"申報內文抓取失敗（下次重試）：{stats['doc_failed']} 則")
    if stats["errors"]:
        lines.append(f"程式處理錯誤：{stats['errors']} 則（請查看 GitHub 執行紀錄）")
    if stats["not_sec"]:
        lines.append(f"不在 SEC 申報而略過：{'、'.join(stats['not_sec'])}")
    if stats["timed_out"]:
        lines.append(f"因執行超時未掃描：{len(stats['timed_out'])} 檔")
    return lines


def send_report(stats, total, is_alert, digest_items):
    """每日健康回報（附上例行申報合併通知）或異常警報；全部送出成功才回傳 True"""
    now_tw_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
    lines = health_lines(stats, total)
    if is_alert:
        title = "🚨 SEC 巡檢異常：本次結果不完整"
        color = 0xC0392B
        if stats["fatal"]:
            lines.insert(0, stats["fatal"])
        elif stats["timed_out"]:
            lines.append("執行時間過長，可能是 SEC 回應變慢或限流。")
        else:
            lines.append("連線失敗比例過高，可能是 SEC 對 GitHub 雲端 IP 限流，或 SEC_USER_AGENT 設定有誤。")
    else:
        title = "✅ SEC 巡檢每日健康回報"
        color = 0x7F8C8D

    if digest_items is not None:
        lines.append("")
        if digest_items:
            lines.append(f"**📋 例行申報合併通知（共 {len(digest_items)} 則）**")
            ordered = sorted(digest_items, key=lambda d: (not d.get("holding"), d["ticker"], d["date"]))
            for d in ordered:
                star = "★ " if d.get("holding") else ""
                lines.append(f"{star}`{d['ticker']}` [{d['text']}]({d['url']})｜{d['date']}")
        else:
            lines.append("📋 例行申報合併通知：無")

    chunks = chunk_lines(lines)
    ok = True
    for i, chunk in enumerate(chunks):
        payload = {
            "username": "SEC EDGAR Intelligence",
            "embeds": [{
                "title": title if i == 0 else f"{title}（續 {i + 1}/{len(chunks)}）",
                "color": color,
                "description": chunk,
                "footer": {"text": f"SEC EDGAR Radar • {now_tw_str}"},
            }],
        }
        ok = post_to_discord(payload) and ok
        time.sleep(1)
    return ok


# ==================== 紀錄檔 ====================
# 一行一筆：案號|||處理日期|||狀態（SENT 推播、DIGEST 併入每日通知、SKIP 略過、SEEN 首次啟用只記錄）
# 以 @@ 開頭的是狀態資訊（版本、每日回報日期、上次警報時間、今日 AI 用量）
def load_state():
    state = {"history": {}, "version": "", "heartbeat": "", "last_alert": "", "ai_date": "", "ai_count": 0}
    if not os.path.exists(HISTORY_FILE):
        return state
    today = datetime.now(UTC_TZ).strftime("%Y-%m-%d")
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("|||")
            if line.startswith("@@"):
                key = parts[0]
                if key == "@@VERSION" and len(parts) >= 2:
                    state["version"] = parts[1]
                elif key == "@@HEARTBEAT" and len(parts) >= 2:
                    state["heartbeat"] = parts[1]
                elif key == "@@ALERT" and len(parts) >= 2:
                    state["last_alert"] = parts[1]
                elif key == "@@AI" and len(parts) >= 3:
                    state["ai_date"], state["ai_count"] = parts[1], int(parts[2]) if parts[2].isdigit() else 0
                continue
            # 舊版紀錄只有案號，沒有日期：視為今天處理，30 天後自動清除
            state["history"][parts[0]] = (parts[1] if len(parts) >= 2 else today,
                                          parts[2] if len(parts) >= 3 else "LEGACY")
    return state


def append_line(line):
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def record(state, accession, status):
    today = datetime.now(UTC_TZ).strftime("%Y-%m-%d")
    state["history"][accession] = (today, status)
    append_line(f"{accession}|||{today}|||{status}")


def save_meta(state, key, *values):
    append_line("|||".join([key, *[str(v) for v in values]]))


def rewrite_state(state):
    cutoff = (datetime.now(UTC_TZ) - timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
    kept = {k: v for k, v in state["history"].items() if v[0] >= cutoff}
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if state["version"]:
            f.write(f"@@VERSION|||{state['version']}\n")
        if state["heartbeat"]:
            f.write(f"@@HEARTBEAT|||{state['heartbeat']}\n")
        if state["last_alert"]:
            f.write(f"@@ALERT|||{state['last_alert']}\n")
        f.write(f"@@AI|||{AI_USAGE['date']}|||{AI_USAGE['count']}\n")
        for acc, (d, s) in sorted(kept.items(), key=lambda kv: kv[1][0]):
            f.write(f"{acc}|||{d}|||{s}\n")
    os.replace(tmp, HISTORY_FILE)
    return len(state["history"]) - len(kept)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def add_digest(item):
    items = load_json(DIGEST_FILE, [])
    items.append(item)
    save_json(DIGEST_FILE, items)


# ==================== 清單與 CIK ====================
def read_code_list(path):
    codes = []
    if not os.path.exists(path):
        return codes
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            t = line.strip().upper()
            if not t or t.startswith("#"):
                continue
            t = TICKER_CANONICAL.get(t, t)
            if t not in codes:
                codes.append(t)
    return codes


def load_cik_mapping(tickers):
    """
    回傳 (代號→CIK, 不在 SEC 申報的代號, 無法確認的代號, 來源說明)
    優先用 SEC 最新名單；下載失敗時用上次的存檔；都沒有才用程式內的少量備援
    """
    cache = load_json(CIK_CACHE_FILE, {"ciks": {}, "not_sec": []})
    try:
        data = sec_get("https://www.sec.gov/files/company_tickers.json").json()
        live = {}
        for item in data.values():
            live[str(item["ticker"]).upper()] = str(item["cik_str"]).zfill(10)
        mapping, not_sec = {}, []
        for t in tickers:
            cik = live.get(t) or live.get(t.replace(".", "-"))
            if cik:
                mapping[t] = cik
            else:
                not_sec.append(t)
        new_cache = {"ciks": mapping, "not_sec": not_sec}
        if new_cache["ciks"] != cache.get("ciks") or new_cache["not_sec"] != cache.get("not_sec"):
            save_json(CIK_CACHE_FILE, new_cache)
        print(f"✅ 已自 SEC 載入最新代號對照表（{len(live)} 筆）", flush=True)
        return mapping, not_sec, [], "SEC 最新名單"
    except (SecFetchError, SecNotFound, ValueError, KeyError, AttributeError) as e:
        print(f"⚠️ SEC 代號對照表下載失敗（{e}），改用存檔", flush=True)
    mapping, not_sec, unknown = {}, [], []
    cached_not_sec = set(cache.get("not_sec", []))
    for t in tickers:
        cik = cache.get("ciks", {}).get(t) or CORE_FALLBACK_CIK.get(t)
        if cik:
            mapping[t] = cik
        elif t in cached_not_sec:
            not_sec.append(t)
        else:
            unknown.append(t)
    return mapping, not_sec, unknown, "存檔（SEC 名單下載失敗）"


def is_recent_filing(filing_date_str):
    try:
        filing_dt = datetime.strptime(filing_date_str, "%Y-%m-%d").date()
        return (datetime.now(UTC_TZ).date() - filing_dt).days <= MAX_LOOKBACK_DAYS
    except Exception:
        return False


def normalize_items_to_str(items_val):
    if not items_val:
        return ""
    if isinstance(items_val, list):
        return ",".join(str(x) for x in items_val if x)
    return str(items_val)


def fetch_sec_filings(cik):
    """回傳近期申報清單；連線失敗丟出 SecFetchError，查無此 CIK 丟出 SecNotFound"""
    data = sec_get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    n = len(forms)

    def col(name):
        values = recent.get(name, [])
        return values + [""] * (n - len(values))

    accession, dates, docs = col("accessionNumber"), col("filingDate"), col("primaryDocument")
    items, descs = col("items"), col("primaryDocDescription")
    results = []
    for i in range(n):
        if not is_recent_filing(dates[i]):
            continue
        results.append({
            "form": forms[i],
            "accessionNumber": accession[i],
            "filingDate": dates[i],
            "primaryDocument": docs[i],
            "primaryDocDescription": descs[i] or "",
            "items": normalize_items_to_str(items[i]),
        })
    return results


# ==================== 巡檢核心邏輯 ====================
def process_ticker(ticker, cik, state, stats, bootstrap):
    filings = fetch_sec_filings(cik)
    stats["sec_ok"] += 1
    targets = [f for f in filings if f["form"].upper() in TARGET_FORMS and f["accessionNumber"] not in state["history"]]
    if not targets:
        print("近期無新的追蹤表單", flush=True)
        return
    stats["with_filings"] += 1
    print(f"{len(targets)} 則新申報", flush=True)
    today_et = datetime.now(ET_TZ).strftime("%Y-%m-%d")

    for f in targets:
        acc = f["accessionNumber"]
        form = f["form"].upper()
        print(f"   ↳ {form} | {acc} | {f['filingDate']} | 項目: {f['items'] or '無'}", flush=True)

        # 換新版第一次執行：舊版沒追蹤的表單，今天以前的只記錄不推，避免一次推出一大堆舊申報
        if bootstrap and form not in LEGACY_FORMS and f["filingDate"] < today_et:
            record(state, acc, "SEEN")
            print("      [首次啟用] 舊申報只記錄、不推播", flush=True)
            continue

        try:
            decision = handle_filing(ticker, f, cik, stats)
        except SecNotFound:
            decision = skip("SEC 找不到申報文件")
        except SecFetchError as e:
            stats["doc_failed"] += 1
            print(f"      ⚠️ [內文抓取失敗] {e}，下次重試", flush=True)
            continue
        except Exception as e:
            stats["errors"] += 1
            print(f"      ❌ [程式處理錯誤] {type(e).__name__}: {e}", flush=True)
            continue

        if decision["action"] == "card":
            if send_filing_card(ticker, f, cik, decision["intel"]):
                stats["pushed"] += 1
                record(state, acc, "SENT")
                print("      🎉 [推播成功]", flush=True)
            else:
                stats["push_failed"] += 1
                print("      ❌ [推播失敗] 不寫入紀錄，下次重試", flush=True)
            time.sleep(1)
        elif decision["action"] == "digest":
            add_digest({"date": f["filingDate"], "ticker": ticker, "form": form, "text": decision["text"],
                        "url": filing_view_url(cik, acc, f["primaryDocument"]), "holding": ticker in HOLDINGS})
            stats["digested"] += 1
            record(state, acc, "DIGEST")
            print(f"      📋 [併入每日通知] {decision['text']}", flush=True)
        else:
            if "例行 6-K" not in decision["reason"]:
                stats["skipped"] += 1
            record(state, acc, "SKIP")
            print(f"      [略過] {decision['reason']}", flush=True)


def new_stats():
    return {"fetch_failed": [], "not_sec": [], "timed_out": [], "sec_ok": 0, "with_filings": 0,
            "pushed": 0, "push_failed": 0, "digested": 0, "skipped": 0, "routine_6k": 0,
            "ai_calls": 0, "ai_failed": 0, "ai_quota": 0, "doc_failed": 0, "errors": 0,
            "cik_source": "", "fatal": ""}


def finish_with_report(state, stats, total):
    """決定要不要送每日回報或警報，並存回紀錄檔"""
    now_tw = datetime.now(TW_TZ)
    today_tw = now_tw.strftime("%Y-%m-%d")
    fail_ratio = len(stats["fetch_failed"]) / total if total else 0
    is_alert = bool(stats["fatal"]) or fail_ratio >= FETCH_FAIL_ALERT_RATIO or bool(stats["timed_out"])
    need_daily = state["heartbeat"] != today_tw and now_tw.hour >= DAILY_REPORT_HOUR_TW

    alert_ok_to_send = True
    if state["last_alert"]:
        try:
            last = datetime.fromisoformat(state["last_alert"])
            alert_ok_to_send = datetime.now(UTC_TZ) - last >= timedelta(hours=ALERT_COOLDOWN_HOURS)
        except Exception:
            pass

    if need_daily:
        digest_items = load_json(DIGEST_FILE, [])
        if send_report(stats, total, is_alert, digest_items):
            state["heartbeat"] = today_tw
            save_meta(state, "@@HEARTBEAT", today_tw)
            save_json(DIGEST_FILE, [])
            if is_alert:
                state["last_alert"] = datetime.now(UTC_TZ).isoformat(timespec="seconds")
    elif is_alert and alert_ok_to_send:
        if send_report(stats, total, True, None):
            state["last_alert"] = datetime.now(UTC_TZ).isoformat(timespec="seconds")
            save_meta(state, "@@ALERT", state["last_alert"])
    elif is_alert:
        print(f"ℹ️ 本次異常，但距上次警報未滿 {ALERT_COOLDOWN_HOURS} 小時，不重複推播", flush=True)

    return rewrite_state(state)


# ==================== 主程式進入點 ====================
def main():
    now_tw = datetime.now(TW_TZ)
    print("==========================================", flush=True)
    print(f"🕒 當前台灣時間：{now_tw.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

    state = load_state()
    today_utc = datetime.now(UTC_TZ).strftime("%Y-%m-%d")
    AI_USAGE["date"] = today_utc
    AI_USAGE["count"] = state["ai_count"] if state["ai_date"] == today_utc else 0
    stats = new_stats()

    if not SEC_USER_AGENT:
        print("❌ 未設定 SEC_USER_AGENT（GitHub Secrets），SEC 會拒絕連線，本次停止", flush=True)
        stats["fatal"] = "未設定 SEC_USER_AGENT，請到 GitHub Secrets 新增（內容例如：sec-radar 你的Email）。"
        finish_with_report(state, stats, 0)
        sys.exit(1)

    if not os.path.exists(TICKERS_FILE):
        print(f"❌ 錯誤：找不到 {TICKERS_FILE}！", flush=True)
        sys.exit(1)

    watchlist = read_code_list(TICKERS_FILE)
    holdings = read_code_list(HOLDINGS_FILE)
    HOLDINGS.update(holdings)
    print(f"⭐ 持股：{'、'.join(holdings) if holdings else '（找不到 holdings.txt 或清單是空的）'}", flush=True)
    # 持股排最前面優先掃描；持股就算不在 tickers.txt 也會自動加入
    tickers = holdings + [t for t in watchlist if t not in HOLDINGS]
    total = len(tickers)

    bootstrap = state["version"] != STATE_VERSION
    print(f"🏛️ 啟動 SEC EDGAR 申報巡檢，共 {total} 檔標的；已記錄 {len(state['history'])} 筆申報", flush=True)
    if bootstrap:
        print("🆕 新版第一次執行：新增追蹤的表單只記錄今天以前的申報、不推播", flush=True)
    print("==========================================", flush=True)

    cik_map, not_sec, unknown, source = load_cik_mapping(tickers)
    stats["cik_source"] = source
    stats["not_sec"] = list(not_sec)
    stats["fetch_failed"].extend(unknown)
    if not_sec:
        print(f"ℹ️ 不在 SEC 申報、略過：{'、'.join(not_sec)}", flush=True)

    run_start = time.monotonic()
    consecutive_fails = 0
    seen_ciks = set()
    for idx, ticker in enumerate(tickers, start=1):
        cik = cik_map.get(ticker)
        if not cik or cik in seen_ciks:
            continue
        seen_ciks.add(cik)
        print(f"[{idx:03d}/{total:03d}] {ticker:5s} ... ", end="", flush=True)
        try:
            process_ticker(ticker, cik, state, stats, bootstrap)
            consecutive_fails = 0
        except SecNotFound:
            print("SEC 查無申報資料，略過", flush=True)
            stats["not_sec"].append(ticker)
            consecutive_fails = 0
        except (SecFetchError, ValueError) as e:
            print(f"⚠️ 連線失敗（{e}）", flush=True)
            stats["fetch_failed"].append(ticker)
            consecutive_fails += 1

        if consecutive_fails >= MAX_CONSECUTIVE_FETCH_FAILS:
            remaining = [t for t in tickers[idx:] if t in cik_map]
            stats["fetch_failed"].extend(remaining)
            print(f"🛑 連續 {consecutive_fails} 檔連線失敗，判定被 SEC 限流，略過剩餘 {len(remaining)} 檔", flush=True)
            break
        if time.monotonic() - run_start > MAX_RUN_MINUTES * 60 and idx < total:
            stats["timed_out"] = tickers[idx:]
            print(f"🛑 已執行超過 {MAX_RUN_MINUTES} 分鐘，略過剩餘 {len(stats['timed_out'])} 檔", flush=True)
            break

    # 有成功查到資料才算完成首次啟用，否則下次仍維持「舊申報只記錄」的保護
    if stats["sec_ok"] > 0:
        state["version"] = STATE_VERSION
    removed = finish_with_report(state, stats, total)

    print("==========================================", flush=True)
    print(f"✅ 巡檢完成：成功查詢 {stats['sec_ok']} 檔，連線失敗 {len(stats['fetch_failed'])} 檔，"
          f"推播 {stats['pushed']} 則，併入每日通知 {stats['digested']} 則，AI {stats['ai_calls']} 次，"
          f"清除過期紀錄 {removed} 筆", flush=True)
    print("==========================================", flush=True)


if __name__ == "__main__":
    main()
