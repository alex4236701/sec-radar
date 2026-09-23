import hashlib
import json
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

# ==================== 環境變數與路徑設定 ====================
DISCORD_NEWS_WEBHOOK = os.environ.get("DISCORD_NEWS_WEBHOOK")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
HISTORY_FILE = "sent_news_log.txt"
WEEKEND_RUN_LOG = "weekend_last_run.txt"

TW_TZ = timezone(timedelta(hours=8))
UTC_TZ = timezone.utc

SEC_HEADERS = {
    "User-Agent": "ResearchDesk/3.0 (compliance@institutional-research.org)",
    "Accept-Encoding": "gzip, deflate"
}

# 核心標的備援對照庫：防範 GitHub Actions (微軟 Azure IP) 被 SEC 403 阻擋導致公司名全空
CORE_FALLBACK_NAMES = {
    "QCOM": "Qualcomm", "NVDA": "Nvidia", "AAPL": "Apple", "TSLA": "Tesla",
    "MSFT": "Microsoft", "GOOGL": "Alphabet", "AMZN": "Amazon", "ARM": "Arm Holdings",
    "AVGO": "Broadcom", "INTC": "Intel", "MRVL": "Marvell", "TSM": "TSMC",
    "ASML": "ASML", "AMAT": "Applied Materials", "LRCX": "Lam Research", "KLAC": "KLA",
    "MU": "Micron", "CRWD": "CrowdStrike", "PLTR": "Palantir", "IONQ": "IonQ",
    "ALAB": "Astera Labs", "GFS": "GlobalFoundries", "CAMT": "Camtek", "ONTO": "Onto Innovation",
    "COHR": "Coherent", "LITE": "Lumentum", "CRDO": "Credo Technology", "POWI": "Power Integrations",
    "VSH": "Vishay", "VICR": "Vicor", "WOLF": "Wolfspeed", "VRT": "Vertiv",
    "RDW": "Redwire", "RKLB": "Rocket Lab", "FEIM": "Frequency Electronics", "UAMY": "United States Antimony",
    "ECL": "Ecolab", "VIAV": "Viavi Solutions", "KEYS": "Keysight", "FORM": "FormFactor",
    "GEV": "GE Vernova", "GNRC": "Generac", "TTMI": "TTM Technologies", "CRCL": "Circle",
    "PL": "Planet Labs", "CRWV": "CoreWeave", "CSCO": "Cisco"
}

COMPANY_NAME_CACHE = {}

# 1. 權威外電與通訊社白名單 (納入半導體專門外電與主流財經聚合)
TRUSTED_SOURCES = [
    # 一手官方通訊社
    "pr newswire", "business wire", "globenewswire", "accesswire",
    # 一線頂級財經外電
    "reuters", "bloomberg", "wall street journal", "wsj",
    "cnbc", "financial times", "marketwatch", "barron's", "associated press", "ap news",
    # 財經主流聚合與專業平台
    "yahoo finance", "yahoo", "investor's business daily", "ibd", "seeking alpha", "benzinga", "investing.com",
    # 核心硬體、半導體與科技權威媒體
    "the verge", "techcrunch", "tom's hardware", "wccftech", "ars technica", "anandtech", "semiengineering"
]

COMMON_WORD_TICKERS = {
    "ONTO", "CAT", "NOW", "ON", "IT", "ALL", "CAN", "BE", "GO", "ARE",
    "FOR", "OUT", "WELL", "RUN", "FAST", "OPEN", "PLAY", "SAVE", "APP",
    "REAL", "TRUE", "KEY", "KEYS", "FORM", "POST", "NET", "PLUG", "SO"
}

# 2. 精準排除黑名單：精確封鎖「股東集體訴訟」與「公關軟文」，絕不誤殺專利授權戰與反壟斷調查
EXCLUDE_TITLE_PATTERNS = [
    r"\bclass\s+action\b", r"\bshareholder\s+alert\b", r"\breminds\s+investors\b",
    r"\blead\s+plaintiff\b", r"\bloss\s+submission\b", r"\bsecurities\s+fraud\b",
    r"\binvestor\s+rights?\b", r"\blaw\s+offices?\s+of\b", r"\bnotifies\s+shareholders\b",
    r"\brosen\b", r"\bpomerantz\b", r"\bglancy\b", r"\bschall\b",
    r"\bfaruqi\b", r"\bhagens\s+berman\b", r"\blevi\s+&\s+korsinsky\b",
    r"\bbronstein\b", r"\bkaskela\b", r"\bblock\s+&\s+leviton\b",
    r"\bto\s+report\b", r"\bschedules?\b", r"\bto\s+host\b", r"\bwebcast\b",
    r"\bconference\s+call\b", r"\binvestor\s+conference\b", r"\bfireside\s+chat\b",
    r"\broadshow\b", r"\bannual\s+meeting\b", r"\bproxy\s+statement\b",
    r"\bmarket\s+size\b", r"\bmarket\s+share\b", r"\bcagr\b", r"\bmarket\s+research\b",
    r"\bforecast\s+to\s+20\d\d\b", r"\btop\s+players\b", r"\bindustry\s+report\b",
    r"\bnamed\s+(a\s+)?winner\b", r"\bwins?\s+award\b", r"\bgreat\s+place\s+to\s+work\b",
    r"\besg\s+report\b", r"\bsustainability\s+report\b", r"\bcarbon\s+neutral\b",
    r"\bdonates?\b", r"\bwhitepaper\b", r"\bsurvey\s+finds\b", r"\badds\s+to\s+board\b"
]

# 3. 晶片、能源、衛星與重大催化劑信號庫
SIGNAL_PATTERNS = [
    r"\bcontract\b", r"\border\b", r"\borders\b", r"\bdeal\b", r"\baward\b",
    r"\bagreement\b", r"\bpact\b", r"\bprocurement\b", r"\bsupply\b",
    r"\bpartner(?:ed|ing|ship|s)?\b", r"\bcollaboration\b", r"\balliance\b",
    r"\bjoint\s+venture\b", r"\bto\s+deploy\b", r"\bpackaging\b", r"\bdesign\s+win\b",
    r"\btakeover\b", r"\bacquisition\b", r"\bacquires?\b", r"\bbuyout\b", r"\bbid\b",
    r"\bapproach\w*\b", r"\btalks?\b", r"\bpursues?\b", r"\bweighs?\b", r"\bexplor\w*\b",
    r"\binvest(?:ment|s|ing)?\b", r"\bstake\b", r"\bmerger\b", r"\brestructur\w*\b",
    r"\bsells?\b", r"\bsold\b", r"\bsale\s+of\b", r"\bspinoff\b", r"\bdivest\w*\b",
    r"\blicens(?:e|ing|ee)\b", r"\broyalt(?:y|ies)\b", r"\bpatent\b", r"\binfringement\b",
    r"\bantitrust\b", r"\bmonopoly\b", r"\bdoj\b", r"\bftc\b", r"\bprobe\b", r"\bsubpoena\b",
    r"\bexport\s+control\b", r"\bsanction\w*\b", r"\bchips\s+act\b", r"\binjunction\b",
    r"\blaunches\b", r"\bunveils\b", r"\bintroduces\b", r"\bnext-gen\b",
    r"\barchitecture\b", r"\bprocessor\b", r"\bchipset?\b", r"\bsnapdragon\b", r"\bbreakthrough\b",
    r"\bfinancial\s+results\b", r"\braises?\s+guidance\b", r"\blowers?\s+guidance\b",
    r"\bcuts?\s+guidance\b", r"\bbuyback\b", r"\brepurchase\b", r"\bconvertible\b",
    r"\bpublic\s+offering\b", r"\bat-the-market\b", r"\bchapter\s+11\b", r"\bbankruptcy\b",
    r"\bsmr\b", r"\breactor\b", r"\bpower\s+deal\b", r"\bdebt-funded\b"
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
    name = re.sub(r"/(?:DE|MD|ADR|CA|NY|NV|VA|PA|OH|TX)/?", "", raw_name, flags=re.IGNORECASE)
    cleaned = re.sub(
        r",?\s*(INC|CORP|LTD|HOLDINGS|CO|PLC|LLC|AG|SE|SA|NV|GMBH|TECHNOLOGIES|CORP\s*/DE)\.?$", 
        "", 
        name, 
        flags=re.IGNORECASE
    ).strip()
    return cleaned if len(cleaned) >= 2 else raw_name.strip()


def preload_sec_company_names():
    global COMPANY_NAME_CACHE
    url = "https://www.sec.gov/files/company_tickers.json"
    try:
        res = requests.get(url, headers=SEC_HEADERS, timeout=10)
        if res.status_code == 200:
            for item in res.json().values():
                t = item["ticker"].upper()
                raw_title = item.get("title", "")
                COMPANY_NAME_CACHE[t] = clean_company_name(raw_title)
            print(f"✅ 成功自 SEC 載入 {len(COMPANY_NAME_CACHE)} 檔公司名單", flush=True)
    except Exception as e:
        print(f"⚠️ SEC 官方名冊獲取受限 ({e})，使用本地備援名冊", flush=True)

    for t, name in CORE_FALLBACK_NAMES.items():
        if t not in COMPANY_NAME_CACHE:
            COMPANY_NAME_CACHE[t] = name


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
    return re.sub(r"(ments?|ings?|ed|s)$", "", w)


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


def is_within_36_hours(pub_date_raw):
    if not pub_date_raw or not pub_date_raw.strip():
        return False
    try:
        dt = parsedate_to_datetime(pub_date_raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now_utc = datetime.now(UTC_TZ)
        diff_hours = (now_utc - dt).total_seconds() / 3600
        return -1.0 <= diff_hours <= 36.0
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
                print(f"⏳ [週末節流] 距上次掃描僅 {elapsed_hours:.1f} 小時，尚需冷卻 {remaining_hours:.1f} 小時，跳過。", flush=True)
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
            "desc": "授權撤銷/反壟斷/制裁/專利敗訴/財測下修/破產"
        },
        "M&A": {
            "title": f"🤝 戰略併購/重大投資：{ticker}",
            "color": 0x9B59B6,
            "desc": "資本運作（洽談收購/資產出售/重組拆分/外部注資）"
        },
        "EARNINGS": {
            "title": f"📊 正式財報/指引更新：{ticker}",
            "color": 0x3498DB,
            "desc": "官方財報公布、財測調升或庫藏股回購"
        },
        "PRODUCT": {
            "title": f"🚀 旗艦產品/架構突破：{ticker}",
            "color": 0x1ABC9C,
            "desc": "次世代旗艦晶片發布 / 專利架構突破 / 監管核准"
        },
        "ORDER": {
            "title": f"💰 商業大單/授權快訊：{ticker}",
            "color": 0x2ECC71,
            "desc": "實質商業授權協議 / 先進封裝大單 / 客戶採購合約"
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
                {"name": "💡 買方深度解讀", "value": safe_summary, "inline": False}
            ],
            "footer": {"text": f"Market Radar • 推播時間: {now_tw_str}"}
        }]
    }
    try:
        res = requests.post(DISCORD_NEWS_WEBHOOK, json=payload, timeout=12)
        res.raise_for_status()
        print("      🎉 [推播成功] 已發送至 Discord！", flush=True)
    except Exception as e:
        print(f"      ❌ [Discord 發送失敗] {e}", flush=True)


# ==================== AI 深度審核核心 ====================
def summarize_with_ai(ticker, text):
    if not OPENAI_API_KEY:
        print("      ❌ [環境變數警告] 未設定 OPENAI_API_KEY！", flush=True)
        return "PASS", "", False

    api_url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    company_name = COMPANY_NAME_CACHE.get(ticker.upper(), ticker)

    prompt = f"""
你是一位分毫不差的美股資深買方研究員。請對【{ticker} - {company_name}】的這則即時重大消息進行深層穿透式拆解：

【絕對嚴禁之廢話修辭（出現一律視為分析失敗）】：
嚴禁使用「提升市場地位、增強競爭力、帶來正面影響、後市可期、具戰略意義、有助長遠發展」等空洞公關話術！必須用具體的「金額、年限、技術指標、資產負債代價」說話。

【絕對駁回規則（命中任一條，一律回傳 PASS）】：
1. 歷史舊聞：回顧數週前或上一季度的歷史數據，非當前 24-36 小時突發事件。
2. 主體不符：新聞核心主角不是【{ticker} / {company_name}】。
3. 雜訊軟文：例行參展、無具體商業條款之純宣傳、律師股東集體訴訟招募。

【實質拆解維度要求】：
1. 【合約與事實】：對手方是誰（如政府、雲端巨頭 AWS、國防部）？交易性質是正式合約還是約束力極弱的備忘錄 (MoU)？有無披露金額或年限？
2. 【工程與架構】：牽涉的具體產品或架構是什麼（如 SMR 小型模組化反應爐、800G 光模組、低軌衛星遙測、高階 PCB 等）？
3. 【財務與利弊】：是實質挹注現金流，還是屬於高風險的舉債收購 (Debt-funded)、稀釋股本融資或面臨反壟斷監管審查？

【輸出格式要求】：
若不符合，只回傳單字：PASS
若符合，嚴格回傳以下純 JSON 物件（繁體中文，內容務求直擊本質，每項 50-80 字，嚴禁多餘包裝）：
{{
  "type": "ORDER 或 M&A 或 DILUTION 或 EARNINGS 或 PRODUCT 或 CRISIS",
  "fact": "交易對手、合約性質（正式合約/MoU）、金額時程與關鍵產品型號",
  "tech_angle": "背後的底層硬體、物理規格或工程架構本質",
  "financial_impact": "對毛利率、資產負債表（如舉債/稀釋）或營收認列之實質利弊分析"
}}

新聞快訊內容：
{text[:4500]}
"""
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "你是一位硬核的買方機構研究員，只講物理工程事實與實質財務利弊，嚴禁任何公關吹捧廢話，輸出嚴格 JSON。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1
    }
    
    for attempt in range(3):
        try:
            res = requests.post(api_url, headers=headers, json=payload, timeout=20)
            data = res.json()
            if "error" in data:
                print(f"      ⚠️ [OpenAI 報錯] {data['error'].get('message', '未知錯誤')}", flush=True)
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
            fact = parsed.get("fact", "").strip()
            tech = parsed.get("tech_angle", "").strip()
            fin = parsed.get("financial_impact", "").strip()
            
            if not fact or not fin:
                return "PASS", "", True
                
            formatted_summary = (
                f"• **【合約與事實】**：{fact}\n"
                f"• **【工程與架構】**：{tech}\n"
                f"• **【財務與利弊】**：{fin}"
            )
            return event_type, formatted_summary, True
        except Exception as e:
            print(f"      ⚠️ [連線重試 {attempt+1}/3] {e}", flush=True)
            time.sleep(2)
            
    return "PASS", "", False


# ==================== 稿件檢索與巡檢邏輯 ====================
def fetch_google_wire_news(ticker):
    company_name = COMPANY_NAME_CACHE.get(ticker.upper())
    
    if company_name and company_name.upper() != ticker.upper() and len(company_name) >= 3:
        search_target = f'"{company_name}" OR "{ticker}" OR "${ticker}"'
    else:
        search_target = f'"{ticker}" OR "${ticker}"'

    encoded_query = urllib.parse.quote(search_target)
    rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }

    for retry in range(2):
        try:
            res = requests.get(rss_url, headers=headers, timeout=12)
            if res.status_code == 429:
                print(" ⚠️ [Google 頻率限制 429] 降速冷卻 8 秒...", flush=True)
                time.sleep(8)
                continue
            if res.status_code != 200 or not res.content:
                return []
            
            root = ET.fromstring(res.content)
            channel = root.find("channel")
            if channel is None:
                return []

            items = []
            for item in channel.findall("item")[:20]:
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
        print("0 則稿件", flush=True)
        return

    print(f"候選稿件 {len(wire_items)} 則", flush=True)

    for item in wire_items:
        raw_title = item["raw_title"]
        source_name = item["source"]

        # 1. 權威白名單過濾
        if not is_trusted_source(source_name):
            continue

        # 2. 寬限至 36 小時時效守衛
        if not is_within_36_hours(item["pub_date_raw"]):
            continue

        # 3. 主體核對
        if not matches_target_entity(ticker, raw_title):
            continue

        clean_title = re.sub(r"\s+[\-–—]\s+[^\-–—]+$", "", raw_title).strip()
        fingerprint = make_news_fingerprint(ticker, clean_title)
        
        # 4. 精確指紋去重
        if fingerprint in sent_fingerprints:
            continue

        # 5. 語意去重
        if is_duplicate_news(ticker, clean_title, history_records):
            save_sent_record(fingerprint, ticker, clean_title)
            sent_fingerprints.add(fingerprint)
            continue

        # 6. 黑名單過濾
        if is_junk_title(clean_title):
            save_sent_record(fingerprint, ticker, clean_title)
            sent_fingerprints.add(fingerprint)
            continue

        # 7. 實質信號過濾
        title_has_signal = has_high_impact_signal(clean_title)
        snippet_has_signal = has_high_impact_signal(item['snippet'])

        if not (title_has_signal or snippet_has_signal):
            continue

        print(f"      ⚡ [命中重大事件] {clean_title[:50]}... 提交 GPT 審核...", flush=True)
        
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
            continue

        save_sent_record(fingerprint, ticker, clean_title)
        sent_fingerprints.add(fingerprint)
        history_records.append({"ticker": ticker, "title": clean_title})

        if event_type == "PASS" or len(summary_text) <= 10:
            print("      [AI裁定] PASS (主體不符/非核心衝擊事件)", flush=True)
        else:
            print(f"      🎯 [AI放行] 判定為 {event_type} 事件！發送 Discord...", flush=True)
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
        print("❌ 錯誤：找不到 tickers.txt 檔案！", flush=True)
        return

    preload_sec_company_names()

    with open("tickers.txt", "r", encoding="utf-8-sig") as f:
        tickers = [line.strip().upper() for line in f if line.strip() and not line.strip().startswith("#")]

    sent_fingerprints, history_records = load_sent_history()
    total_count = len(tickers)
    print(f"🏛️ 啟動全維度重大事件巡檢，清單共計：{total_count} 檔標的", flush=True)
    print(f"📦 已記錄歷史審查紀錄：{len(sent_fingerprints)} 條", flush=True)
    print("==========================================", flush=True)

    for idx, ticker in enumerate(tickers, start=1):
        print(f"[{idx:03d}/{total_count:03d}] 檢索標的：{ticker:5s} ... ", end="", flush=True)
        check_and_process_ticker(ticker, sent_fingerprints, history_records)
        time.sleep(0.6)

    print("==========================================", flush=True)
    print(f"✅ 全量巡檢完成！共計掃描 {total_count} 檔標的。", flush=True)
    print("==========================================", flush=True)


if __name__ == "__main__":
    main()
