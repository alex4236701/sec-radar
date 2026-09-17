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

DISCORD_NEWS_WEBHOOK = os.environ.get("DISCORD_NEWS_WEBHOOK")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
HISTORY_FILE = "sent_news_log.txt"

TW_TZ = timezone(timedelta(hours=8))

# SEC 合規 User-Agent（建議換成你真實的聯絡信箱以確保永不被 403 阻擋）
SEC_HEADERS = {
    "User-Agent": "ResearchBot/2.0 (yomin701@gmail.com)",
    "Accept-Encoding": "gzip, deflate"
}

COMPANY_NAME_CACHE = {}

WIRE_SITES_QUERY = "(site:prnewswire.com OR site:businesswire.com OR site:globenewswire.com)"

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

    # C. 通訊社轉載之二手分析師評等與 SEO 流量報告
    r"\bprice\s+target\b", r"\brating\b", r"\bupgrades?\b", r"\bdowngrades?\b",
    r"\bmarket\s+size\b", r"\bmarket\s+share\b", r"\bcagr\b",
    r"\bmarket\s+research\b", r"\bforecast\s+to\s+20\d\d\b", r"\btop\s+players\b",
    r"\bindustry\s+report\b",

    # D. 企業公關得獎、最佳雇主、評鑑榮譽
    r"\bnamed\s+(a\s+)?winner\b", r"\bwins?\s+award\b", r"\bhonored\s+as\b",
    r"\brecognized\s+by\b", r"\brecognized\s+as\b", r"\bnamed\s+to\b",
    r"\bgreat\s+place\s+to\s+work\b", r"\bmagic\s+quadrant\b", r"\bforbes\b",
    r"\bfortune\s+500\b", r"\bfast\s+company\b",

    # E. ESG 報告、慈善活動、永續發展與 DEI
    r"\besg\s+report\b", r"\bsustainability\s+report\b", r"\bcorporate\s+responsibility\b",
    r"\bcarbon\s+neutral\b", r"\bdonates?\b", r"\bdonation\b", r"\bfoundation\b",
    r"\bscholarship\b", r"\bdiversity\b", r"\binclusion\b",

    # F. 展會擺攤、概念展示與白皮書
    r"\bto\s+showcase\b", r"\bto\s+exhibit\b", r"\bto\s+demonstrate\b",
    r"\bexhibiting\s+at\b", r"\bbooth\b", r"\bwhitepaper\b",
    r"\bsurvey\s+finds\b", r"\bsurvey\s+reveals\b", r"\bpublishes\s+study\b",

    # G. 常規人事任命與文字勘誤
    r"\bappoints?\b", r"\bnames?\s+new\b", r"\bcorrection\b", r"\badds\s+to\s+board\b"
]

SIGNAL_PATTERNS = [
    # A. 商業大單與採購合約
    r"\bcontract\b", r"\border\b", r"\borders\b", r"\bdeal\b", r"\baward\b",
    r"\bawarded\b", r"\bagreement\b", r"\bprocurement\b", r"\bsupply\b",
    r"\bselected\s+by\b", r"\bpartnered\s+with\b", r"\bto\s+deploy\b", r"\bsecures?\b",

    # B. 重大併購、股權投資與控制權攻防
    r"\bacquisition\b", r"\bacquires?\b", r"\binvests?\s+in\b", r"\bbuyout\b",
    r"\bstake\b", r"\bmerger\b", r"\bpoison\s+pill\b", r"\bshareholder\s+rights\s+plan\b",
    r"\bunsolicited\b", r"\bproxy\s+contest\b", r"\bstrategic\s+alternatives\b",

    # C. 實質財報與業績指引調升
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

    # F. 重大產品量產、次世代架構與監管放行（修正邊界詞）
    r"\blaunches\b", r"\bunveils\b", r"\bintroduces\b", r"\bnext-gen\b",
    r"\barchitecture\b", r"\bproduction\s+release\b", r"\bfda\s+approv\w*\b",
    r"\bclearance\b", r"\bbreakthrough\b",

    # G. 實質金額與政府撥款補助
    r"\$\d+", r"\bmillion\b", r"\bbillion\b", r"\bgrant\b", r"\bfunding\b",

    # H. 突發利空預警、調降指引、延遲申報與會計審計異常
    r"\blowers?\s+guidance\b", r"\bcuts?\s+guidance\b", r"\bslashes\b",
    r"\bwithdraws?\s+guidance\b", r"\bpreliminary\s+results\b",
    r"\brestatement\b", r"\brestates\b", r"\bdelays?\s+filing\b",
    r"\bresignation\s+of\s+independent\s+auditor\b",

    # I. 破產重組、債務違約、退市警告與反向拆股（合股）
    r"\bchapter\s+11\b", r"\bbankruptcy\b", r"\breverse\s+stock\s+split\b",
    r"\bdelisting\b", r"\bnon-compliance\b", r"\bdefault\b",

    # J. 主管機關調查、重大訴訟判決與業務分拆
    r"\bsec\s+investigation\b", r"\bsubpoena\b", r"\bcomplete\s+response\s+letter\b",
    r"\bclinical\s+hold\b", r"\bverdict\b", r"\bsettlement\s+agreement\b",
    r"\bpatent\s+infringement\b", r"\bantitrust\b",
    r"\bspinoff\b", r"\bspin-off\b", r"\bdivestiture\b", r"\bdivests?\b",
    r"\bworkforce\s+reduction\b"
]

GENERIC_FIRST_WORDS = {
    "general", "american", "national", "global", "united", "first",
    "advanced", "international", "standard", "western", "pacific"
}

def preload_sec_company_names():
    global COMPANY_NAME_CACHE
    url = "https://www.sec.gov/files/company_tickers.json"
    try:
        res = requests.get(url, headers=SEC_HEADERS, timeout=15)
        if res.status_code == 200:
            for item in res.json().values():
                t = item["ticker"].upper()
                raw_title = item.get("title", "")
                clean_title = re.sub(
                    r",?\s*(INC|CORP|LTD|HOLDINGS|CO|PLC|LLC|DE)\.?$", 
                    "", 
                    raw_title, 
                    flags=re.IGNORECASE
                ).strip()
                COMPANY_NAME_CACHE[t] = clean_title
            print(f"✅ 成功自 SEC 預載入 {len(COMPANY_NAME_CACHE)} 檔官方公司全名！", flush=True)
    except Exception as e:
        print(f"⚠️ 預載入 SEC 名稱失敗（將退回純代號比對）: {e}", flush=True)

def load_sent_history():
    if not os.path.exists(HISTORY_FILE):
        return set()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def save_sent_id(item_id):
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(f"{item_id}\n")

def make_news_fingerprint(ticker, title):
    clean_title = re.sub(r"[^\w\s]", "", title.lower())
    clean_title = " ".join(clean_title.split())
    raw_key = f"{ticker}_{clean_title}"
    return hashlib.md5(raw_key.encode("utf-8")).hexdigest()

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
    
    # 若首詞是通用詞，避免誤判，不單用首詞比對
    if first_word and first_word not in GENERIC_FIRST_WORDS and len(first_word) > 3:
        has_first_word = bool(re.search(rf"\b{re.escape(first_word)}\b", t_lower))
    else:
        has_first_word = False
        
    return has_ticker or has_full_name or has_first_word

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
            "title": f"🤝 戰略併購/資本運作：{ticker}",
            "color": 0x9B59B6,
            "desc": "資本運作（收購/合併/業務分拆/股東攻防）"
        },
        "EARNINGS": {
            "title": f"📊 正式財報/指引更新：{ticker}",
            "color": 0x3498DB,
            "desc": "官方財報、財測調升或庫藏股回購"
        },
        "PRODUCT": {
            "title": f"🚀 重大產品/技術監管突破：{ticker}",
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
                {"name": "💡 核心要點解讀", "value": safe_summary, "inline": False}
            ],
            "footer": {"text": f"Official Wire Feed • 推播時間: {now_tw_str}"}
        }]
    }
    try:
        res = requests.post(DISCORD_NEWS_WEBHOOK, json=payload, timeout=15)
        res.raise_for_status()
        print("      🎉 [推播成功] 已發送至 Discord！", flush=True)
    except Exception as e:
        print(f"      ❌ [Discord 失敗] {e}", flush=True)

def summarize_with_ai(ticker, text):
    if not OPENAI_API_KEY:
        return "PASS", "", True
    api_url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    
    company_name = COMPANY_NAME_CACHE.get(ticker.upper(), ticker)
    
    prompt = f"""
你是一位分毫不差的美股買方研究員。請審核這則官方通訊社新聞是否為【{ticker} - {company_name}】的重大市場衝擊事件：

【絕對駁回規則（命中任一條，一律回傳 PASS）】：
1. 【主體非該公司】：新聞主角必須是【{ticker} / {company_name}】這家公司本身！
   - 若新聞只是提到日常單字（如 onto、cat、it）或產業詞彙，回傳 PASS。
   - 若其他公司進行交易，僅在內文把【{ticker}】當成同業或客戶順帶提及，回傳 PASS。
2. 【例行行銷軟文】：常規軟體小版本更新、例行展會演講、無具體時程的純概念展示，回傳 PASS。
3. 純法說會/論壇時程公布、律師集體訴訟招募（Lawsuit Alert）、內部普通高管人事異動。

【符合監控的六大類別】：
1. 【ORDER】商業大單：外部客戶/政府向【{ticker}】採購產品、簽訂重大供貨合約、獲得擴產補助款。
2. 【M&A】重大併購/資本運作：【{ticker}】收購同業、買下重要股權、重大業務分拆（Spinoff）、或遭遇敵意收購/啟動毒藥丸。
3. 【DILUTION】資本稀釋融資：【{ticker}】發行可轉債、增發新股、宣布定價、或啟動 ATM 配售。
4. 【EARNINGS】業績與回饋：【{ticker}】公布季度財報、調升全年財測、或啟動庫藏股回購。
5. 【PRODUCT】重大產品上市/監管突破：【{ticker}】正式發布重大旗艦產品架構（公布量產時程或規格突破），或取得 FDA 藥證等關鍵核准。
6. 【CRISIS】利空預警與黑天鵝：調降/撤回財測、會計師辭職、延遲申報財報、破產清算、收到下市警告、反壟斷阻擋或反轉合股（Reverse Split）。

【輸出格式要求】：
若不符合上述六大類，只回傳單字：PASS
若符合，嚴格依照以下 JSON 格式回傳，禁止多餘文字：
{{
  "type": "ORDER 或 M&A 或 DILUTION 或 EARNINGS 或 PRODUCT 或 CRISIS",
  "summary": "以繁體中文條列兩點（80 字以內）：\\n• 【核心動作】：產品型號/合約金額/融資規模/下修幅度及具體時程。\\n• 【市場衝擊】：對 {ticker} 之營收貢獻、毛利影響、稀釋壓力或營運風險。"
}}

新聞內容：
{text[:8000]}
"""
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "你是一位嚴謹的機構買方研究員，確認新聞主體是否為指定股票，絕不腦補。"},
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

def fetch_google_wire_news(ticker):
    company_name = COMPANY_NAME_CACHE.get(ticker.upper())
    if company_name and company_name.upper() != ticker.upper():
        search_target = f'("{ticker}" OR "{company_name}")'
    else:
        search_target = f'"{ticker}"'

    query = f'{search_target} {WIRE_SITES_QUERY} when:2d'
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
            for item in channel.findall("item")[:3]:
                title = item.findtext("title") or ""
                link = item.findtext("link") or ""
                pub_date = item.findtext("pubDate") or ""
                source = item.findtext("source") or "Newswire"
                description = item.findtext("description") or ""
                
                clean_title = re.sub(r"\s+-\s+.*$", "", title)
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

def check_and_process_ticker(ticker, sent_history):
    wire_items = fetch_google_wire_news(ticker)
    if not wire_items:
        print("獲取到 0 則官方新聞", flush=True)
        return

    print(f"獲取到 {len(wire_items)} 則通訊社稿件", flush=True)

    for item in wire_items:
        title = item["title"]
        print(f"   ↳ 審核標題: {title[:55]}...", flush=True)

        fingerprint = make_news_fingerprint(ticker, title)
        
        if fingerprint in sent_history:
            print("     [記憶庫略過] 此新聞已完成歷史審查，略過", flush=True)
            continue

        if not matches_target_entity(ticker, title):
            print("     [本地過濾] 標題非該公司實體（排除單字/撞名），跳過", flush=True)
            save_sent_id(fingerprint)
            sent_history.add(fingerprint)
            continue

        if is_junk_title(title):
            print("     [本地過濾] 命中升級版黑名單（律所/評等/公關/展會），跳過", flush=True)
            save_sent_id(fingerprint)
            sent_history.add(fingerprint)
            continue

        if not has_high_impact_signal(title):
            print("     [本地過濾] 無重大財務、合約或利空特徵詞，跳過", flush=True)
            save_sent_id(fingerprint)
            sent_history.add(fingerprint)
            continue

        print("     ⚡ [命中重大事件] 提交 GPT 進行主體與性質深審...", flush=True)
        
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
            print("     ⚠️ [API 異常] GPT 審核超時或伺服器中斷，暫不標記，等待下次排程重審", flush=True)
            continue

        save_sent_id(fingerprint)
        sent_history.add(fingerprint)

        if event_type == "PASS" or len(summary_text) <= 10:
            print("     [AI裁定] PASS (主體不符/非核心事件)", flush=True)
        else:
            print(f"     🎯 [AI放行] 判定為 {event_type} 事件！準備推播...", flush=True)
            send_discord_embed(ticker, title, event_type, summary_text, item["url"], pub_tw_str, item["source"])
            time.sleep(1)

def main():
    if not os.path.exists("tickers.txt"):
        print("❌ 錯誤：找不到 tickers.txt！", flush=True)
        return

    preload_sec_company_names()

    with open("tickers.txt", "r") as f:
        tickers = [line.strip().upper() for line in f if line.strip()]

    sent_history = load_sent_history()
    total_count = len(tickers)
    print("==========================================", flush=True)
    print(f"🏛️ 啟動通訊社全維度重大事件巡檢，清單共計：{total_count} 檔標的", flush=True)
    print(f"🕒 當前台灣時間：{datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"📦 已記錄歷史審查紀錄：{len(sent_history)} 條", flush=True)
    print("==========================================", flush=True)

    for idx, ticker in enumerate(tickers, start=1):
        print(f"[{idx:03d}/{total_count:03d}] 檢索標的：{ticker:5s} ... ", end="", flush=True)
        check_and_process_ticker(ticker, sent_history)
        time.sleep(1)

    print("==========================================", flush=True)
    print(f"✅ 全量巡檢完成！共計掃描 {total_count} 檔標的。", flush=True)
    print("==========================================", flush=True)

if __name__ == "__main__":
    main()
