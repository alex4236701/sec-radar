import os
import re
import time
import json
import hashlib
import urllib.parse
import xml.etree.ElementTree as ET
import requests
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

# ==================== 環境變數與路徑設定 ====================
DISCORD_NEWS_WEBHOOK = os.environ.get("DISCORD_NEWS_WEBHOOK")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
HISTORY_FILE = "sent_news_log.txt"
WEEKEND_RUN_LOG = "weekend_last_run.txt"

TW_TZ = timezone(timedelta(hours=8))
UTC_TZ = timezone.utc

SEC_HEADERS = {
    "User-Agent": "ResearchBot/2.0 (compliance@alpharesearch.org)",
    "Accept-Encoding": "gzip, deflate"
}

COMPANY_NAME_CACHE = {}

# 1. 嚴格白名單：非官方一手通訊社或一線外電，直接秒殺（阻絕 TipRanks、MarketScreener 等二級農場）
TRUSTED_SOURCES = [
    # 官方一手通訊社
    "pr newswire", "business wire", "globenewswire",
    # 一線頂級外電與權威財經媒體
    "reuters", "bloomberg", "wall street journal", "wsj",
    "cnbc", "financial times", "marketwatch", "barron's", "associated press"
]

COMMON_WORD_TICKERS = {
    "ONTO", "CAT", "NOW", "ON", "IT", "ALL", "CAN", "BE", "GO", "ARE",
    "FOR", "OUT", "WELL", "RUN", "FAST", "OPEN", "PLAY", "SAVE", "APP",
    "REAL", "TRUE", "KEY", "KEYS", "FORM", "POST", "NET", "PLUG", "SO"
}

EXCLUDE_TITLE_PATTERNS = [
    r"\bto\s+report\b", r"\bschedules?\b", r"\bto\s+host\b", r"\bwebcast\b",
    r"\bconference\s+call\b", r"\binvestor\s+conference\b", r"\bpresentation\b",
    r"\bfireside\s+chat\b", r"\broadshow\b", r"\bannual\s+meeting\b",
    r"\bproxy\s+materials?\b", r"\bproxy\s+statement\b",
    r"\bclass\s+action\b", r"\blawsuit\b", r"\bshareholder\s+alert\b",
    r"\breminds\s+investors\b", r"\blead\s+plaintiff\b", r"\bdeadline\b",
    r"\binvestigates?\b", r"\binvestigation\s+into\b", r"\bnotifies\s+investors\b",
    r"\bencourages\s+investors\b", r"\bloss\s+submission\b",
    r"\brosen\b", r"\bpomerantz\b", r"\bglancy\b", r"\bschall\b",
    r"\bfaruqi\b", r"\bhagens\s+berman\b", r"\blevi\s+&\s+korsinsky\b",
    r"\bmarket\s+size\b", r"\bmarket\s+share\b", r"\bcagr\b",
    r"\bmarket\s+research\b", r"\bforecast\s+to\s+20\d\d\b", r"\btop\s+players\b",
    r"\bindustry\s+report\b",
    r"\bnamed\s+(a\s+)?winner\b", r"\bwins?\s+award\b", r"\bhonored\s+as\b",
    r"\brecognized\s+by\b", r"\brecognized\s+as\b", r"\bnamed\s+to\b",
    r"\bgreat\s+place\s+to\s+work\b", r"\bmagic\s+quadrant\b", r"\bforbes\b",
    r"\bfortune\s+500\b", r"\bfast\s+company\b",
    r"\besg\s+report\b", r"\bsustainability\s+report\b", r"\bcorporate\s+responsibility\b",
    r"\bcarbon\s+neutral\b", r"\bdonates?\b", r"\bdonation\b", r"\bfoundation\b",
    r"\bscholarship\b", r"\bdiversity\b", r"\binclusion\b",
    r"\bto\s+showcase\b", r"\bto\s+exhibit\b", r"\bto\s+demonstrate\b",
    r"\bexhibiting\s+at\b", r"\bbooth\b", r"\bwhitepaper\b",
    r"\bsurvey\s+finds\b", r"\bsurvey\s+reveals\b", r"\bpublishes\s+study\b",
    r"\bappoints?\b", r"\bnames?\s+new\b", r"\bcorrection\b", r"\badds\s+to\s+board\b"
]

SIGNAL_PATTERNS = [
    r"\bcontract\b", r"\border\b", r"\borders\b", r"\bdeal\b", r"\baward\b",
    r"\bawarded\b", r"\bagreement\b", r"\bpact\b", r"\bprocurement\b", r"\bsupply\b",
    r"\bselected\s+by\b", r"\bpartner(?:ed|ing|ship|s)?\b", r"\bcollaboration\b",
    r"\balliance\b", r"\bjoint\s+venture\b", r"\bto\s+deploy\b", r"\bsecures?\b",
    r"\bpackaging\b", r"\bcooperat\w*\b", r"\bmou\b", r"\btie-up\b", r"\bmicro\s*led\b",
    r"\btakeover\b", r"\bacquisition\b", r"\bacquires?\b", r"\bbuyout\b",
    r"\binvest(?:ment|s|ing)?\b", r"\bstake\b", r"\bmerger\b",
    r"\brestructur\w*\b", r"\bsubsidiary\b", r"\bfoundry\b",
    r"\bpoison\s+pill\b", r"\bshareholder\s+rights\s+plan\b",
    r"\bunsolicited\b", r"\bproxy\s+contest\b", r"\bstrategic\s+alternatives\b",
    r"\bsells?\b", r"\bsold\b", r"\bsale\s+of\b", r"\bselling\b", r"\bto\s+sell\b",
    r"\bspinoff\b", r"\bspin-off\b", r"\bdivestiture\b", r"\bdivests?\b",
    r"\breports?\s+first\s+quarter\b", r"\breports?\s+second\s+quarter\b",
    r"\breports?\s+third\s+quarter\b", r"\breports?\s+fourth\s+quarter\b",
    r"\breports?\s+full\s+year\b", r"\bfinancial\s+results\b",
    r"\braises?\s+guidance\b", r"\braises?\s+outlook\b",
    r"\brepurchase\b", r"\bbuyback\b", r"\bshare\s+repurchase\b",
    r"\bconvertible\b", r"\bsenior\s+notes\b", r"\bpublic\s+offering\b",
    r"\bsecondary\s+offering\b", r"\bprices\s+offering\b", r"\bpricing\s+of\b",
    r"\bat-the-market\b", r"\batm\s+offering\b", r"\batm\s+facility\b",
    r"\bcommon\s+stock\s+offering\b",
    r"\blaunches\b", r"\bunveils\b", r"\bintroduces\b", r"\bnext-gen\b",
    r"\barchitecture\b", r"\bproduction\s+release\b", r"\bfda\s+approv\w*\b",
    r"\bclearance\b", r"\bbreakthrough\b",
    r"\$\d+", r"\bmillion\b", r"\bbillion\b", r"\bgrant\b", r"\bfunding\b",
    r"\bsubsid(?:y|ies)\b", r"\bchips\s+act\b",
    r"\blowers?\s+guidance\b", r"\bcuts?\s+guidance\b", r"\bslashes\b",
    r"\bwithdraws?\s+guidance\b", r"\bpreliminary\s+results\b",
    r"\brestatement\b", r"\brestates\b", r"\bdelays?\s+filing\b",
    r"\bresignation\s+of\s+independent\s+auditor\b",
    r"\bchapter\s+11\b", r"\bbankruptcy\b", r"\breverse\s+stock\s+split\b",
    r"\bdelisting\b", r"\bnon-compliance\b", r"\bdefault\b",
    r"\bsec\s+investigation\b", r"\bsubpoena\b", r"\bcomplete\s+response\s+letter\b",
    r"\bclinical\s+hold\b", r"\bverdict\b", r"\bsettlement\s+agreement\b",
    r"\bpatent\s+infringement\b", r"\bantitrust\b",
    r"\bworkforce\s+reduction\b"
]

GENERIC_FIRST_WORDS = {
    "general", "american", "national", "global", "united", "first",
    "advanced", "international", "standard", "western", "pacific"
}

STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "about", "above", "after", "along",
    "at", "by", "for", "from", "in", "into", "of", "to", "with", "on", "its",
    "as", "stock", "shares", "tumbles", "jumps", "falls", "rises", "plunges",
    "by", "through", "announces", "announced"
}


# ==================== 工具函式 ====================
def clean_company_name(raw_name):
    name = re.sub(r"/ADR/?", "", raw_name, flags=re.IGNORECASE)
    cleaned = re.sub(
        r",?\s*(INC|CORP|LTD|HOLDINGS|CO|PLC|LLC|DE|AG|SE|SA|NV|GMBH|TECHNOLOGIES)\.?$", 
        "", 
        name, 
        flags=re.IGNORECASE
    ).strip()
    return cleaned if len(cleaned) >= 2 else raw_name.strip()

def preload_sec_company_names():
    global COMPANY_NAME_CACHE
    url = "https://www.sec.gov/files/company_tickers.json"
    try:
        res = requests.get(url, headers=SEC_HEADERS, timeout=15)
        if res.status_code == 200:
            for item in res.json().values():
                t = item["ticker"].upper()
                raw_title = item.get("title", "")
                COMPANY_NAME_CACHE[t] = clean_company_name(raw_title)
            print(f"✅ 成功自 SEC 預載入 {len(COMPANY_NAME_CACHE)} 檔官方公司全名！", flush=True)
    except Exception as e:
        print(f"⚠️ 預載入 SEC 名稱失敗: {e}", flush=True)

def load_sent_history():
    if not os.path.exists(HISTORY_FILE):
        return set(), []
    
    fingerprints = set()
    history_records = []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            clean_line = line.strip()
            if not clean_line:
                continue
            if "|||" in clean_line:
                parts = clean_line.split("|||")
                fingerprints.add(parts[0])
                if len(parts) >= 3:
                    history_records.append({"ticker": parts[1], "title": parts[2]})
            else:
                fingerprints.add(clean_line)
                
    return fingerprints, history_records

def save_sent_record(fingerprint, ticker, title):
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(f"{fingerprint}|||{ticker}|||{title}\n")

def make_news_fingerprint(ticker, title):
    clean_title = re.sub(r"[^\w\s]", "", title.lower())
    clean_title = " ".join(clean_title.split())
    raw_key = f"{ticker}_{clean_title}"
    return hashlib.md5(raw_key.encode("utf-8")).hexdigest()

def normalize_word(word):
    w = word.lower()
    w = re.sub(r"(ments?|ings?|ed|s)$", "", w)
    return w

def extract_core_words(title):
    words = re.findall(r"\b[a-zA-Z0-9$]+(?:\.[0-9]+)?\b", title.lower())
    return set(normalize_word(w) for w in words if w not in STOP_WORDS and len(w) > 2)

def is_duplicate_news(ticker, new_title, history_records):
    new_words = extract_core_words(new_title)
    if not new_words:
        return False

    for h in reversed(history_records[-150:]):
        if h["ticker"] != ticker:
            continue
            
        old_words = extract_core_words(h["title"])
        if not old_words:
            continue
            
        intersection = new_words & old_words
        union = new_words | old_words
        similarity = len(intersection) / len(union) if union else 0

        if similarity >= 0.45 or len(intersection) >= 3:
            return True

    return False

def is_junk_title(title):
    t_lower = title.lower()
    for pattern in EXCLUDE_TITLE_PATTERNS:
        if re.search(pattern, t_lower):
            return True
    return False

def has_high_impact_signal(text):
    t_lower = text.lower()
    for pattern in SIGNAL_PATTERNS:
        if re.search(pattern, t_lower):
            return True
    return False

def is_trusted_source(source_name):
    s_lower = source_name.lower().strip()
    return any(trusted in s_lower for trusted in TRUSTED_SOURCES)

def is_within_24_hours(pub_date_raw):
    """
    嚴格 24 小時時效守衛：
    1. 查無日期或解析失敗，一律直接丟棄（絕不放水）
    2. 發布時間超過 24 小時，強制直接判定為過期拋棄
    """
    if not pub_date_raw or not pub_date_raw.strip():
        return False
    try:
        dt = parsedate_to_datetime(pub_date_raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now_utc = datetime.now(UTC_TZ)
        diff_hours = (now_utc - dt).total_seconds() / 3600

        # 允許 1 小時伺服器時鐘誤差，超過 24 小時直接秒殺
        if diff_hours < -1.0 or diff_hours > 24.0:
            return False
        return True
    except Exception:
        return False

def matches_target_entity(ticker, raw_title):
    t_raw = raw_title
    t_lower = raw_title.lower()
    ticker_upper = ticker.upper()

    company_name = COMPANY_NAME_CACHE.get(ticker_upper, "").lower()
    has_full_name = (company_name in t_lower) if len(company_name) > 3 else False

    if ticker_upper in COMMON_WORD_TICKERS:
        has_strict_ticker = bool(re.search(rf"\b({ticker_upper}|NYSE:{ticker_upper}|NASDAQ:{ticker_upper})\b", t_raw))
        return has_full_name or has_strict_ticker

    has_ticker = bool(re.search(rf"\b{re.escape(ticker.lower())}\b", t_lower))
    
    words = company_name.split()
    first_word = words[0] if words else ""
    if first_word and first_word not in GENERIC_FIRST_WORDS and len(first_word) > 3:
        has_first_word = bool(re.search(rf"\b{re.escape(first_word)}\b", t_lower))
    else:
        has_first_word = False
        
    return has_ticker or has_full_name or has_first_word


# ==================== 週末節流機制 ====================
def should_skip_for_weekend_throttle():
    now_tw = datetime.now(TW_TZ)
    wd = now_tw.weekday()
    hr = now_tw.hour

    is_weekend = (wd == 5 and hr >= 12) or (wd == 6)
    if not is_weekend:
        return False

    if os.path.exists(WEEKEND_RUN_LOG):
        try:
            with open(WEEKEND_RUN_LOG, "r", encoding="utf-8") as f:
                last_run_ts = float(f.read().strip())
            elapsed_hours = (time.time() - last_run_ts) / 3600
            if elapsed_hours < 8.0:
                remaining_hours = 8.0 - elapsed_hours
                print(f"⏳ [週末節流] 距上次掃描僅過 {elapsed_hours:.1f} 小時，尚需冷卻 {remaining_hours:.1f} 小時，跳過。", flush=True)
                return True
        except Exception:
            pass

    with open(WEEKEND_RUN_LOG, "w", encoding="utf-8") as f:
        f.write(str(time.time()))
        
    print("🚀 [週末巡檢放行] 距離上次執行已達 8 小時，啟動本次掃描。", flush=True)
    return False


# ==================== DISCORD 推播 ====================
def send_discord_embed(ticker, title, event_type, summary_bullets, news_url, pub_date_str, source_name):
    if not DISCORD_NEWS_WEBHOOK:
        return
    now_tw_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")

    type_configs = {
        "DILUTION": {
            "title": f"⚠️ 資本融資與稀釋警報：{ticker}",
            "color": 0xE74C3C,
            "desc": "股權融資/稀釋（可轉債、現增、ATM）"
        },
        "CRISIS": {
            "title": f"🚨 重大黑天鵝/利空警報：{ticker}",
            "color": 0xC0392B,
            "desc": "財測下修/破產重組/合股/監管調查/會計異常"
        },
        "M&A": {
            "title": f"🤝 戰略併購/重大投資：{ticker}",
            "color": 0x9B59B6,
            "desc": "資本運作（收購/部門出售/重組拆分/外部大額注資）"
        },
        "EARNINGS": {
            "title": f"📊 正式財報/指引更新：{ticker}",
            "color": 0x3498DB,
            "desc": "官方財報、財測調升或庫藏股回購"
        },
        "PRODUCT": {
            "title": f"🚀 重大產品/技術突破：{ticker}",
            "color": 0x1ABC9C,
            "desc": "次世代旗艦產品發布 / 封裝合作 / 監管核准"
        },
        "ORDER": {
            "title": f"💰 商業大單/合作快訊：{ticker}",
            "color": 0x2ECC71,
            "desc": "實質營收合約（先進封裝協議/戰略合作/政府補助）"
        }
    }

    cfg = type_configs.get(event_type, type_configs["ORDER"])
    safe_summary = summary_bullets[:1000] if summary_bullets else "無內容摘要"

    payload = {
        "username": "Market Impact Radar",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/2965/2965879.png",
        "embeds": [{
            "title": cfg["title"],
            "url": news_url,
            "color": cfg["color"],
            "fields": [
                {"name": "📌 標的", "value": f"`{ticker}`", "inline": True},
                {"name": "📅 發布時間 (台灣)", "value": f"`{pub_date_str}`", "inline": True},
                {"name": "🏷️ 交易性質", "value": f"`{cfg['desc']}`", "inline": True},
                {"name": "📡 來源管道", "value": f"`{source_name}`", "inline": False},
                {"name": "📰 標題", "value": title[:200], "inline": False},
                {"name": "💡 買方核心解讀", "value": safe_summary, "inline": False}
            ],
            "footer": {"text": f"Market Radar • 推播時間: {now_tw_str}"}
        }]
    }
    try:
        res = requests.post(DISCORD_NEWS_WEBHOOK, json=payload, timeout=15)
        res.raise_for_status()
        print("      🎉 [推播成功] 已發送至 Discord！", flush=True)
    except Exception as e:
        print(f"      ❌ [Discord 失敗] {e}", flush=True)


# ==================== AI 審核核心 ====================
def summarize_with_ai(ticker, text):
    if not OPENAI_API_KEY:
        return "PASS", "", True
    api_url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    
    company_name = COMPANY_NAME_CACHE.get(ticker.upper(), ticker)
    
    prompt = f"""
你是一位分毫不差的美股買方研究員。請審核這則新聞是否為【{ticker} - {company_name}】的重大市場衝擊事件：

【絕對駁回規則（命中任一條，一律回傳 PASS）】：
1. 【歷史舊聞回溯】：若只是盤後評論或回顧數週前的歷史季度財報（非當前 24 小時突發事件），回傳 PASS。
2. 【主體非該公司】：新聞主角必須是【{ticker} / {company_name}】本身！
3. 【例行行銷軟文】：常規小版本更新、展會演講、無具體時程之概念展示，回傳 PASS。
4. 純法說會日程公布、律師集體訴訟招募、普通人事異動。

【符合監控的六大類別】：
1. 【ORDER】商業大單/合作：重大技術/封裝合作協議、客戶採購合約、政府補助款。
2. 【M&A】重大併購/資產出售/注資重組：收購、遭外部收購（Takeover）、重大股權投資（Investment）、業務出售/分拆。
3. 【DILUTION】資本稀釋融資：發行可轉債、增發新股、ATM 配售。
4. 【EARNINGS】業績與回饋：即時季度財報、調升全年指引、庫藏股回購。
5. 【PRODUCT】重大產品上市/監管突破：旗艦架構發布、封裝技術量產落地、重要監管批准。
6. 【CRISIS】利空預警與黑天鵝：下修財測、會計師辭職、延期申報、破產、合股（Reverse Split）。

【輸出格式要求】：
若不符合，只回傳單字：PASS
若符合，嚴格回傳以下純 JSON 物件，嚴禁包含任何其他文字：
{{
  "type": "ORDER 或 M&A 或 DILUTION 或 EARNINGS 或 PRODUCT 或 CRISIS",
  "action": "具體動作、合作或交易對手、金額或時程（繁體中文，40 字以內）",
  "impact": "對 {ticker} 之營收貢獻、製程升級、現金流或股本稀釋之實質影響（繁體中文，40 字以內）"
}}

新聞內容：
{text[:8000]}
"""
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "你是一位嚴謹的機構買方研究員，嚴格輸出指定 JSON 鍵值，嚴禁添加任何多餘字句。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1
    }
    
    for attempt in range(3):
        try:
            res = requests.post(api_url, headers=headers, json=payload, timeout=25)
            data = res.json()
            if "error" in data:
                time.sleep(2)
                continue
            content = data["choices"][0]["message"]["content"].strip()
            if "PASS" in content:
                return "PASS", "", True
            
            json_match = re.search(r"\{[\s\S]*\}", content)
            if not json_match:
                return "PASS", "", True

            parsed = json.loads(json_match.group(0))
            event_type = parsed.get("type", "ORDER")
            action = parsed.get("action", "").strip()
            impact = parsed.get("impact", "").strip()
            
            if not action or not impact:
                return "PASS", "", True
                
            formatted_summary = f"• 【核心要點】：{action}\n• 【財務影響】：{impact}"
            return event_type, formatted_summary, True
        except Exception:
            time.sleep(2)
            
    return "PASS", "", False


# ==================== 稿件檢索與巡檢邏輯 ====================
def fetch_google_wire_news(ticker):
    company_name = COMPANY_NAME_CACHE.get(ticker.upper())
    
    if company_name and company_name.upper() != ticker.upper() and len(company_name) >= 3:
        search_target = f'"{company_name}" OR "{ticker}"'
    else:
        search_target = f'"{ticker}"'

    # 強制鎖定在過去 24 小時內 (when:1d)
    query = f"{search_target} when:1d"
    encoded_query = urllib.parse.quote(query)
    rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    for retry in range(2):
        try:
            res = requests.get(rss_url, headers=headers, timeout=12)
            if res.status_code == 429:
                time.sleep(3)
                continue
            if res.status_code != 200 or not res.content:
                return []
            
            root = ET.fromstring(res.content)
            channel = root.find("channel")
            if channel is None:
                return []

            items = []
            for item in channel.findall("item")[:15]:
                raw_title = item.findtext("title") or ""
                link = item.findtext("link") or ""
                pub_date = item.findtext("pubDate") or ""
                source = item.findtext("source") or "Unknown"
                description = item.findtext("description") or ""
                
                clean_desc = re.sub(r"<[^>]+>", " ", description).strip()

                items.append({
                    "raw_title": raw_title,
                    "url": link,
                    "pub_date_raw": pub_date,
                    "source": source,
                    "snippet": clean_desc
                })
            return items
        except Exception:
            time.sleep(1)
    return []

def check_and_process_ticker(ticker, sent_fingerprints, history_records):
    wire_items = fetch_google_wire_news(ticker)
    if not wire_items:
        print("獲取到 0 則稿件", flush=True)
        return

    print(f"獲取到 {len(wire_items)} 則候選稿件", flush=True)

    for item in wire_items:
        raw_title = item["raw_title"]
        source_name = item["source"]
        print(f"   ↳ 審核標題: {raw_title[:55]}...", flush=True)

        # 1. 嚴格白名單防線：非一手通訊社或一線外電直接秒殺
        if not is_trusted_source(source_name):
            print(f"      [來源過濾] 來源非權威白名單 ({source_name})，跳過", flush=True)
            continue

        # 2. 嚴格 24 小時守衛：超過 24 小時或解析失敗，一律直接拋棄
        if not is_within_24_hours(item["pub_date_raw"]):
            print("      [時效過濾] 發布時間已逾 24 小時或無效時間戳，跳過", flush=True)
            continue

        # 3. 主體核對
        if not matches_target_entity(ticker, raw_title):
            print("      [本地過濾] 標題非該公司主體，跳過", flush=True)
            continue

        clean_title = re.sub(r"\s+[\-–—]\s+[^\-–—]+$", "", raw_title).strip()
        fingerprint = make_news_fingerprint(ticker, clean_title)
        
        # 4. 精確指紋去重
        if fingerprint in sent_fingerprints:
            print("      [記憶庫略過] 此新聞指紋已記錄，略過", flush=True)
            continue

        # 5. 語意去重（相同事件直接攔截）
        if is_duplicate_news(ticker, clean_title, history_records):
            print("      [相似度攔截] 檢測到同事件相近報導，跳過", flush=True)
            save_sent_record(fingerprint, ticker, clean_title)
            sent_fingerprints.add(fingerprint)
            continue

        # 6. 黑名單過濾
        if is_junk_title(clean_title):
            print("      [本地過濾] 命中公關/人事黑名單，跳過", flush=True)
            save_sent_record(fingerprint, ticker, clean_title)
            sent_fingerprints.add(fingerprint)
            continue

        # 7. 實質信號過濾
        title_has_signal = has_high_impact_signal(clean_title)
        snippet_lower = item['snippet'].lower()
        has_money = bool(re.search(r"\$\d+(?:\.\d+)?\s*(?:billion|million|b|m)\b", snippet_lower))
        has_strict_action = bool(re.search(r"\b(takeover|acquisition|merger|contract|investment|foundry|partnership|packaging)\b", snippet_lower))
        snippet_has_signal = has_money and has_strict_action

        if not (title_has_signal or snippet_has_signal):
            print("      [本地過濾] 無重大合約、封裝協議或財務特徵詞，跳過", flush=True)
            continue

        print("      ⚡ [命中重大事件] 提交 GPT 進行實質深審...", flush=True)
        
        pub_tw_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
        if item["pub_date_raw"]:
            try:
                pub_dt = parsedate_to_datetime(item["pub_date_raw"])
                pub_tw_str = pub_dt.astimezone(TW_TZ).strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass

        context = f"標題: {clean_title}\n來源: {source_name}\n內容摘要: {item['snippet']}"
        event_type, summary_text, is_api_ok = summarize_with_ai(ticker, context)

        if not is_api_ok:
            print("      ⚠️ [API 異常] 審核中斷，暫不標記等待下次重試", flush=True)
            continue

        save_sent_record(fingerprint, ticker, clean_title)
        sent_fingerprints.add(fingerprint)
        history_records.append({"ticker": ticker, "title": clean_title})

        if event_type == "PASS" or len(summary_text) <= 10:
            print("      [AI裁定] PASS (主體不符/非核心事件/歷史舊聞)", flush=True)
        else:
            print(f"      🎯 [AI放行] 判定為 {event_type} 事件！準備推播...", flush=True)
            send_discord_embed(ticker, clean_title, event_type, summary_bullets=summary_text, news_url=item["url"], pub_date_str=pub_tw_str, source_name=source_name)
            time.sleep(1)


# ==================== 主程式進入點 ====================
def main():
    now_tw = datetime.now(TW_TZ)
    print("==========================================", flush=True)
    print(f"🕒 當前台灣時間：{now_tw.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

    if should_skip_for_weekend_throttle():
        print("==========================================", flush=True)
        return

    if not os.path.exists("tickers.txt"):
        print("❌ 錯誤：找不到 tickers.txt！", flush=True)
        return

    preload_sec_company_names()

    with open("tickers.txt", "r") as f:
        tickers = [line.strip().upper() for line in f if line.strip()]

    sent_fingerprints, history_records = load_sent_history()
    total_count = len(tickers)
    print(f"🏛️ 啟動全維度重大事件巡檢，清單共計：{total_count} 檔標的", flush=True)
    print(f"📦 已記錄歷史審查紀錄：{len(sent_fingerprints)} 條", flush=True)
    print("==========================================", flush=True)

    for idx, ticker in enumerate(tickers, start=1):
        print(f"[{idx:03d}/{total_count:03d}] 檢索標的：{ticker:5s} ... ", end="", flush=True)
        check_and_process_ticker(ticker, sent_fingerprints, history_records)
        time.sleep(0.3)

    print("==========================================", flush=True)
    print(f"✅ 全量巡檢完成！共計掃描 {total_count} 檔標的。", flush=True)
    print("==========================================", flush=True)

if __name__ == "__main__":
    main()
