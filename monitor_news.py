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

# SEC 合規 User-Agent
SEC_HEADERS = {
    "User-Agent": "ResearchBot/2.0 (yomin701@gmail.com)",
    "Accept-Encoding": "gzip, deflate"
}

COMPANY_NAME_CACHE = {}

# 1. 負向黑名單：阻絕律所集體訴訟、例行日程、公關得獎與 SEO 研報
EXCLUDE_TITLE_PATTERNS = [
    # A. 純法說會、路演、會議日程與例行股東會材料
    r"\bto\s+report\b", r"\bschedules?\b", r"\bto\s+host\b", r"\bwebcast\b",
    r"\bconference\s+call\b", r"\binvestor\s+conference\b", r"\bpresentation\b",
    r"\bfireside\s+chat\b", r"\broadshow\b", r"\bannual\s+meeting\b",
    r"\bproxy\s+materials?\b", r"\bproxy\s+statement\b",

    # B. 律師事務所徵求原告、調查宣告與截止日提醒
    r"\bclass\s+action\b", r"\blawsuit\b", r"\bshareholder\s+alert\b",
    r"\breminds\s+investors\b", r"\blead\s+plaintiff\b", r"\bdeadline\b",
    r"\binvestigates?\b", r"\binvestigation\s+into\b", r"\bnotifies\s+investors\b",
    r"\bencourages\s+investors\b", r"\bloss\s+submission\b",
    r"\brosen\b", r"\bpomerantz\b", r"\bglancy\b", r"\bschall\b",
    r"\bfaruqi\b", r"\bhagens\s+berman\b", r"\blevi\s+&\s+korsinsky\b",

    # C. 蹭流量之第三方產業報告與例行評等
    r"\bmarket\s+size\b", r"\bmarket\s+share\b", r"\bcagr\b",
    r"\bmarket\s+research\b", r"\bforecast\s+to\s+20\d\d\b", r"\btop\s+players\b",
    r"\bindustry\s+report\b",

    # D. 企業公關得獎、最佳雇主、評鑑榮譽
    r"\bnamed\s+(a\s+)?winner\b", r"\bwins?\s+award\b", r"\bhonored\s+as\b",
    r"\brecognized\s+by\b", r"\brecognized\s+as\b", r"\bnamed\s+to\b",
    r"\bgreat\s+place\s+to\s+work\b", r"\bmagic\s+quadrant\b", r"\bforbes\b",
    r"\bfortune\s+500\b", r"\bfast\s+company\b",

    # E. ESG 報告、慈善捐贈與基金會活動
    r"\besg\s+report\b", r"\bsustainability\s+report\b", r"\bcorporate\s+responsibility\b",
    r"\bcarbon\s+neutral\b", r"\bdonates?\b", r"\bdonation\b", r"\bfoundation\b",
    r"\bscholarship\b", r"\bdiversity\b", r"\binclusion\b",

    # F. 展會擺攤、產品演示與白皮書
    r"\bto\s+showcase\b", r"\bto\s+exhibit\b", r"\bto\s+demonstrate\b",
    r"\bexhibiting\s+at\b", r"\bbooth\b", r"\bwhitepaper\b",
    r"\bsurvey\s+finds\b", r"\bsurvey\s+reveals\b", r"\bpublishes\s+study\b",

    # G. 常規人事任命與文字勘誤
    r"\bappoints?\b", r"\bnames?\s+new\b", r"\bcorrection\b", r"\badds\s+to\s+board\b"
]

# 2. 全維度重大信號白名單
SIGNAL_PATTERNS = [
    # A. 商業大單與採購合約
    r"\bcontract\b", r"\border\b", r"\borders\b", r"\bdeal\b", r"\baward\b",
    r"\bawarded\b", r"\bagreement\b", r"\bprocurement\b", r"\bsupply\b",
    r"\bselected\s+by\b", r"\bpartnered\s+with\b", r"\bto\s+deploy\b", r"\bsecures?\b",

    # B. 重大併購、資產出售、股權剝離
    r"\bacquisition\b", r"\bacquires?\b", r"\binvests?\s+in\b", r"\bbuyout\b",
    r"\bstake\b", r"\bmerger\b", r"\bpoison\s+pill\b", r"\bshareholder\s+rights\s+plan\b",
    r"\bunsolicited\b", r"\bproxy\s+contest\b", r"\bstrategic\s+alternatives\b",
    r"\bsells?\b", r"\bsold\b", r"\bsale\s+of\b", r"\bselling\b", r"\bto\s+sell\b",
    r"\bspinoff\b", r"\bspin-off\b", r"\bdivestiture\b", r"\bdivests?\b",

    # C. 實質財報與業績指引
    r"\breports?\s+first\s+quarter\b", r"\breports?\s+second\s+quarter\b",
    r"\breports?\s+third\s+quarter\b", r"\breports?\s+fourth\s+quarter\b",
    r"\breports?\s+full\s+year\b", r"\bfinancial\s+results\b",
    r"\braises?\s+guidance\b", r"\braises?\s+outlook\b",

    # D. 庫藏股回購
    r"\brepurchase\b", r"\bbuyback\b", r"\bshare\s+repurchase\b",

    # E. 股權稀釋融資
    r"\bconvertible\b", r"\bsenior\s+notes\b", r"\bpublic\s+offering\b",
    r"\bsecondary\s+offering\b", r"\bprices\s+offering\b", r"\bpricing\s+of\b",
    r"\bat-the-market\b", r"\batm\s+offering\b", r"\batm\s+facility\b",
    r"\bcommon\s+stock\s+offering\b",

    # F. 重大產品量產、技術架構與監管核准
    r"\blaunches\b", r"\bunveils\b", r"\bintroduces\b", r"\bnext-gen\b",
    r"\barchitecture\b", r"\bproduction\s+release\b", r"\bfda\s+approv\w*\b",
    r"\bclearance\b", r"\bbreakthrough\b",

    # G. 實質金額與政府補助
    r"\$\d+", r"\bmillion\b", r"\bbillion\b", r"\bgrant\b", r"\bfunding\b",

    # H. 突發利空預警、調降指引與審計異常
    r"\blowers?\s+guidance\b", r"\bcuts?\s+guidance\b", r"\bslashes\b",
    r"\bwithdraws?\s+guidance\b", r"\bpreliminary\s+results\b",
    r"\brestatement\b", r"\brestates\b", r"\bdelays?\s+filing\b",
    r"\bresignation\s+of\s+independent\s+auditor\b",

    # I. 破產重組、債務違約、退市警告與反向拆股（合股）
    r"\bchapter\s+11\b", r"\bbankruptcy\b", r"\breverse\s+stock\s+split\b",
    r"\bdelisting\b", r"\bnon-compliance\b", r"\bdefault\b",

    # J. 主管機關立案與裁員
    r"\bsec\s+investigation\b", r"\bsubpoena\b", r"\bcomplete\s+response\s+letter\b",
    r"\bclinical\s+hold\b", r"\bverdict\b", r"\bsettlement\s+agreement\b",
    r"\bpatent\s+infringement\b", r"\bantitrust\b",
    r"\bworkforce\s+reduction\b"
]

GENERIC_FIRST_WORDS = {
    "general", "american", "national", "global", "united", "first",
    "advanced", "international", "standard", "western", "pacific"
}

# 停用詞（計算相似度時剔除無意義詞彙）
STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "about", "above", "after", "along",
    "at", "by", "for", "from", "in", "into", "of", "to", "with", "on", "its",
    "as", "stock", "shares", "tumbles", "jumps", "falls", "rises", "plunges"
}


# ==================== 基礎與比對工具函式 ====================
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
        print(f"⚠️ 預載入 SEC 名稱失敗（退回純代號比對）: {e}", flush=True)

def load_sent_history():
    """載入歷史發送紀錄：回傳 (純指紋集合, 歷史結構清單)"""
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

def extract_core_words(title):
    words = re.findall(r"\b[a-zA-Z0-9$]+(?:\.[0-9]+)?\b", title.lower())
    return set(w for w in words if w not in STOP_WORDS and len(w) > 1)

def is_duplicate_news(ticker, new_title, history_records):
    """
    多層模糊語意去重防線：
    1. Jaccard 核心詞重疊率 > 65% 直接判定同事件
    2. 相同金額數字對位（防止不同通訊社只微調兩三個單字）
    """
    new_words = extract_core_words(new_title)
    if not new_words:
        return False

    new_numbers = set(re.findall(r"\$?\b\d+(?:\.\d+)?(?:b|m|k|billion|million)?\b", new_title.lower()))

    # 比對最近 120 筆同檔或全市場新聞
    for h in reversed(history_records[-120:]):
        if h["ticker"] != ticker:
            continue
            
        old_words = extract_core_words(h["title"])
        if not old_words:
            continue
            
        intersection = new_words & old_words
        union = new_words | old_words
        similarity = len(intersection) / len(union) if union else 0

        if similarity >= 0.65:
            return True

        old_numbers = set(re.findall(r"\$?\b\d+(?:\.\d+)?(?:b|m|k|billion|million)?\b", h["title"].lower()))
        if new_numbers and (new_numbers & old_numbers) and len(intersection) >= 3:
            return True

    return False

def is_junk_title(title):
    t_lower = title.lower()
    for pattern in EXCLUDE_TITLE_PATTERNS:
        if re.search(pattern, t_lower):
            return True
    return False

def has_high_impact_signal(title):
    t_lower = title.lower()
    for pattern in SIGNAL_PATTERNS:
        if re.search(pattern, t_lower):
            return True
    return False

def matches_target_entity(ticker, title):
    t_lower = title.lower()
    has_ticker = bool(re.search(rf"\b{re.escape(ticker.lower())}\b", t_lower))
    
    company_name = COMPANY_NAME_CACHE.get(ticker.upper(), "").lower()
    has_full_name = (company_name in t_lower) if len(company_name) > 3 else False
    
    words = company_name.split()
    first_word = words[0] if words else ""
    if first_word and first_word not in GENERIC_FIRST_WORDS and len(first_word) > 3:
        has_first_word = bool(re.search(rf"\b{re.escape(first_word)}\b", t_lower))
    else:
        has_first_word = False
        
    return has_ticker or has_full_name or has_first_word


# ==================== 週末 8 小時節流核心 ====================
def should_skip_for_weekend_throttle():
    """
    週末節流守衛：
    台灣時間週六中午 12:00 ～ 週一清晨 06:00 為休市期，
    物理限制每隔 8 小時才允許執行一次全量掃描。
    """
    now_tw = datetime.now(TW_TZ)
    wd = now_tw.weekday()  # 0=週一, 5=週六, 6=週日
    hr = now_tw.hour

    is_weekend = (wd == 5 and hr >= 12) or (wd == 6) or (wd == 0 and hr < 6)
    if not is_weekend:
        return False  # 平日正常通行

    if os.path.exists(WEEKEND_RUN_LOG):
        try:
            with open(WEEKEND_RUN_LOG, "r", encoding="utf-8") as f:
                last_run_ts = float(f.read().strip())
            elapsed_hours = (time.time() - last_run_ts) / 3600
            if elapsed_hours < 8.0:
                remaining_hours = 8.0 - elapsed_hours
                print(f"⏳ [週末節流] 距上次掃描僅過 {elapsed_hours:.1f} 小時，尚需冷卻 {remaining_hours:.1f} 小時，主動略過。", flush=True)
                return True
        except Exception:
            pass

    # 記錄本次放行時間戳
    with open(WEEKEND_RUN_LOG, "w", encoding="utf-8") as f:
        f.write(str(time.time()))
        
    print("🚀 [週末巡檢放行] 距離上次執行已達 8 小時，啟動本次週末定期掃描。", flush=True)
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
            "desc": "財測下修/破產重組/合股/監管調查/會計爆雷"
        },
        "M&A": {
            "title": f"🤝 戰略併購/資產出售：{ticker}",
            "color": 0x9B59B6,
            "desc": "資本運作（收購/部門出售/業務分拆）"
        },
        "EARNINGS": {
            "title": f"📊 正式財報/指引更新：{ticker}",
            "color": 0x3498DB,
            "desc": "官方財報、財測調升或庫藏股回購"
        },
        "PRODUCT": {
            "title": f"🚀 重大產品/技術突破：{ticker}",
            "color": 0x1ABC9C,
            "desc": "次世代旗艦產品發布 / FDA 監管核准"
        },
        "ORDER": {
            "title": f"💰 商業大單快訊：{ticker}",
            "color": 0x2ECC71,
            "desc": "實質營收合約（客戶下單/政府採購）"
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
                {"name": "📡 官方來源", "value": f"`{source_name}`", "inline": False},
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


# ==================== AI 深度判定核心 ====================
def summarize_with_ai(ticker, text):
    if not OPENAI_API_KEY:
        return "PASS", "", True
    api_url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    
    company_name = COMPANY_NAME_CACHE.get(ticker.upper(), ticker)
    
    prompt = f"""
你是一位分毫不差的美股買方研究員。請審核這則新聞是否為【{ticker} - {company_name}】的重大市場衝擊事件：

【絕對駁回規則（命中任一條，一律回傳 PASS）】：
1. 【主體非該公司】：新聞主角必須是【{ticker} / {company_name}】本身！
   - 若只是提到日常單字（如 onto、cat、it）或產業詞彙，回傳 PASS。
   - 若其他公司交易，僅在內文順帶提及【{ticker}】，回傳 PASS。
2. 【例行行銷軟文】：常規小版本更新、展會演講、無時程的純概念展示，回傳 PASS。
3. 純法說會日程公布、律師集體訴訟招募（Lawsuit Alert）、普通人事升遷。

【符合監控的六大類別】：
1. 【ORDER】商業大單：外部客戶/政府向【{ticker}】採購、簽訂重大供貨合約、獲得擴產補助款。
2. 【M&A】重大併購/資產出售：【{ticker}】收購公司、出售/剝離業務部門（Sale of Business/Divestiture）、業務分拆（Spinoff）。
3. 【DILUTION】資本稀釋融資：【{ticker}】發行可轉債、增發新股、宣布定價、或啟動 ATM 配售。
4. 【EARNINGS】業績與回饋：【{ticker}】公布季度財報、調升全年財測、或啟動庫藏股回購。
5. 【PRODUCT】重大產品上市/監管突破：【{ticker}】發布旗艦架構（量產時程突破）或取得重要監管放行（如 FDA）。
6. 【CRISIS】利空預警與黑天鵝：調降/撤回財測、會計師辭職、延遲申報財報、破產清算、收到下市警告、反壟斷阻擋或合股（Reverse Split）。

【輸出格式要求】：
若不符合，只回傳單字：PASS
若符合，嚴格依照以下 JSON 格式回傳，禁止多餘文字：
{{
  "type": "ORDER 或 M&A 或 DILUTION 或 EARNINGS 或 PRODUCT 或 CRISIS",
  "summary": "以繁體中文條列兩點（100 字以內，直切本質）：\\n• 【核心要點】：具體事件、交易對手、金額或時程。\\n• 【財務影響】：對 {ticker} 之營收貢獻、毛利率、現金流或股本稀釋之實質影響。"
}}

新聞內容：
{text[:8000]}
"""
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "你是一位嚴謹的機構買方研究員，直接輸出指定 JSON，嚴禁添加任何開場白。"},
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
            return parsed.get("type", "ORDER"), parsed.get("summary", ""), True
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

    query = f"({search_target}) when:2d"
    encoded_query = urllib.parse.quote(query)
    rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
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
            for item in channel.findall("item")[:5]:
                title = item.findtext("title") or ""
                link = item.findtext("link") or ""
                pub_date = item.findtext("pubDate") or ""
                source = item.findtext("source") or "Newswire"
                description = item.findtext("description") or ""
                
                clean_title = re.sub(r"\s+[\-–—]\s+.*$", "", title).strip()
                clean_desc = re.sub(r"<[^>]+>", " ", description).strip()

                items.append({
                    "title": clean_title,
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

    print(f"獲取到 {len(wire_items)} 則稿件", flush=True)

    for item in wire_items:
        title = item["title"]
        print(f"   ↳ 審核標題: {title[:55]}...", flush=True)

        fingerprint = make_news_fingerprint(ticker, title)
        
        # 本地防線 1：精確指紋去重
        if fingerprint in sent_fingerprints:
            print("      [記憶庫略過] 此新聞精確指紋已記錄，略過", flush=True)
            continue

        # 本地防線 2：模糊語意與相同事件去重（封殺不同轉載網站的微調標題）
        if is_duplicate_news(ticker, title, history_records):
            print("      [相似度攔截] 檢測到同事件相近標題（轉載重複稿），跳過", flush=True)
            save_sent_record(fingerprint, ticker, title)
            sent_fingerprints.add(fingerprint)
            continue

        # 本地防線 3：實體對位
        if not matches_target_entity(ticker, title):
            print("      [本地過濾] 標題非該公司實體，跳過", flush=True)
            continue

        # 本地防線 4：排除公關人事、研報黑名單
        if is_junk_title(title):
            print("      [本地過濾] 命中升級版黑名單，跳過", flush=True)
            save_sent_record(fingerprint, ticker, title)
            sent_fingerprints.add(fingerprint)
            continue

        # 本地防線 5：檢查是否包含實質大單、併購、賣業務等特徵
        if not has_high_impact_signal(title):
            print("      [本地過濾] 無重大財務、合約或利空特徵詞，跳過", flush=True)
            continue

        print("      ⚡ [命中重大事件] 提交 GPT 進行主體與性質深審...", flush=True)
        
        pub_tw_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
        if item["pub_date_raw"]:
            try:
                pub_dt = parsedate_to_datetime(item["pub_date_raw"])
                pub_tw_str = pub_dt.astimezone(TW_TZ).strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass

        context = f"標題: {title}\n來源: {item['source']}\n內容摘要: {item['snippet']}"
        event_type, summary_text, is_api_ok = summarize_with_ai(ticker, context)

        if not is_api_ok:
            print("      ⚠️ [API 異常] GPT 審核中斷，暫不標記，等待下次重審", flush=True)
            continue

        # 寫入歷史紀錄庫（包含標題以供後續比對相似度）
        save_sent_record(fingerprint, ticker, title)
        sent_fingerprints.add(fingerprint)
        history_records.append({"ticker": ticker, "title": title})

        if event_type == "PASS" or len(summary_text) <= 10:
            print("      [AI裁定] PASS (主體不符/非核心事件)", flush=True)
        else:
            print(f"      🎯 [AI放行] 判定為 {event_type} 事件！準備推播...", flush=True)
            send_discord_embed(ticker, title, event_type, summary_text, item["url"], pub_tw_str, item["source"])
            time.sleep(1)


# ==================== 主程式進入點 ====================
def main():
    now_tw = datetime.now(TW_TZ)
    print("==========================================", flush=True)
    print(f"🕒 當前台灣時間：{now_tw.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

    # 週末 8 小時節流守衛判定
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
        time.sleep(1)

    print("==========================================", flush=True)
    print(f"✅ 全量巡檢完成！共計掃描 {total_count} 檔標的。", flush=True)
    print("==========================================", flush=True)

if __name__ == "__main__":
    main()
