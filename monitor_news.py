import os
import re
import time
import json
import hashlib
import urllib.parse
import xml.etree.ElementTree as ET
import requests
from datetime import datetime, timezone, timedelta

DISCORD_NEWS_WEBHOOK = os.environ.get("DISCORD_NEWS_WEBHOOK")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
HISTORY_FILE = "sent_news_log.txt"

# 台灣時區 (UTC+8)
TW_TZ = timezone(timedelta(hours=8))

# 易撞名股票之全名/交易所映射表，直接從搜尋源頭徹底阻絕無關生活單字
TICKER_ALIAS = {
    "CAT": '("Caterpillar" OR "NYSE:CAT")',
    "NOW": '("ServiceNow" OR "NYSE:NOW")',
    "ON": '("ON Semiconductor" OR "Nasdaq:ON")',
    "ALL": '("Allstate" OR "NYSE:ALL")',
    "IT": '("Gartner" OR "NYSE:IT")',
    "MET": '("MetLife" OR "NYSE:MET")',
    "KEYS": '("Keysight" OR "NYSE:KEYS")'
}

# 1. 負向黑名單：純會議日程、人事升遷、律所股東訴訟索賠（本地直接封存，$0 API）
EXCLUDE_TITLE_PATTERNS = [
    r"\bto\s+report\b", r"\bschedules?\b", r"\bto\s+host\b", r"\bwebcast\b",
    r"\bconference\s+call\b", r"\binvestor\s+conference\b", r"\bpresentation\b",
    r"\bprice\s+target\b", r"\brating\b", r"\bclass\s+action\b", r"\blawsuit\b",
    r"\bshareholder\s+alert\b", r"\breminds\s+investors\b",
    r"\bappoints?\b", r"\bnames?\s+new\b"
]

# 2. 正向白名單：涵蓋能引發股價重大波動的實質資本事件
SIGNAL_PATTERNS = [
    # A. 商業大單與客戶合約
    r"\bcontract\b", r"\border\b", r"\borders\b", r"\bdeal\b", r"\baward\b",
    r"\bawarded\b", r"\bagreement\b", r"\bprocurement\b", r"\bsupply\b",
    r"\bselected\s+by\b", r"\bpartnered\s+with\b", r"\bto\s+deploy\b", r"\bsecures?\b",

    # B. 重大併購與股權投資
    r"\bacquisition\b", r"\bacquires?\b", r"\binvests?\s+in\b", r"\bbuyout\b",
    r"\bstake\b", r"\bmerger\b",

    # C. 實質財報發布與財測調升
    r"\breports?\s+first\s+quarter\b", r"\breports?\s+second\s+quarter\b",
    r"\breports?\s+third\s+quarter\b", r"\breports?\s+fourth\s+quarter\b",
    r"\breports?\s+full\s+year\b", r"\bfinancial\s+results\b",
    r"\braises?\s+guidance\b", r"\braises?\s+outlook\b",

    # D. 庫藏股回購
    r"\brepurchase\b", r"\bbuyback\b", r"\bshare\s+repurchase\b",

    # E. 融資稀釋重磅事件（可轉債 / 現增 / 定價 / ATM 配售）
    r"\bconvertible\b", r"\bsenior\s+notes\b", r"\bpublic\s+offering\b",
    r"\bsecondary\s+offering\b", r"\bprices\s+offering\b", r"\bpricing\s+of\b",
    r"\bat-the-market\b", r"\batm\s+offering\b", r"\batm\s+facility\b",
    r"\bcommon\s+stock\s+offering\b",

    # F. 金額特徵
    r"\$\d+", r"\bmillion\b", r"\bbillion\b"
]

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

def send_discord_embed(ticker, title, event_type, summary_bullets, news_url, pub_date_str, source_name):
    if not DISCORD_NEWS_WEBHOOK:
        return
    now_tw_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")

    # 依性質分流外觀與警告顏色
    if event_type == "DILUTION":
        card_title = f"⚠️ 資本融資與稀釋警報：{ticker}"
        embed_color = 0xE74C3C  # 紅色警示（可轉債/增發/ATM）
        type_desc = "股權融資/稀釋（可轉債、現增或 ATM）"
    elif event_type == "M&A":
        card_title = f"🤝 戰略併購/股權投資：{ticker}"
        embed_color = 0x9B59B6  # 紫色戰略擴張
        type_desc = "資本運作（收購/股權投資）"
    elif event_type == "EARNINGS":
        card_title = f"📊 正式財報/指引更新：{ticker}"
        embed_color = 0x3498DB  # 藍色業績
        type_desc = "官方財報或營收指引（Guidance）"
    else:
        card_title = f"💰 商業大單快訊：{ticker}"
        embed_color = 0x2ECC71  # 綠色實質營收
        type_desc = "實質營收合約（客戶下單）"

    payload = {
        "username": "Market Impact Radar",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/2965/2965879.png",
        "embeds": [{
            "title": card_title,
            "url": news_url,
            "color": embed_color,
            "fields": [
                {"name": "📌 標的", "value": f"`{ticker}`", "inline": True},
                {"name": "📅 發布時間 (台灣)", "value": f"`{pub_date_str}`", "inline": True},
                {"name": "🏷️ 交易性質", "value": f"`{type_desc}`", "inline": True},
                {"name": "📡 官方來源", "value": f"`{source_name}`", "inline": False},
                {"name": "📰 標題", "value": title[:200], "inline": False},
                {"name": "💡 核心要點解讀", "value": summary_bullets, "inline": False}
            ],
            "footer": {"text": f"Official Wire Feed • 推播時間: {now_tw_str}"}
        }]
    }
    try:
        res = requests.post(DISCORD_NEWS_WEBHOOK, json=payload, timeout=15)
        res.raise_for_status()
        print(f"     🎉 [推播成功] 已發送至 Discord！", flush=True)
    except Exception as e:
        print(f"     ❌ [Discord 失敗] {e}", flush=True)

def summarize_with_ai(ticker, text):
    if not OPENAI_API_KEY:
        return "PASS", ""
    api_url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    
    prompt = f"""
你是一位分毫不差的美股買方研究員。請審核這則官方通訊社新聞是否為【{ticker}】的重大市場衝擊事件：

【監控類別】：
1. 【ORDER】商業大單：外部客戶/政府向【{ticker}】採購產品、系統、簽訂重大供貨合約。
2. 【M&A】重大併購/投資：【{ticker}】收購同業、買下重要公司股權、或合併案。
3. 【DILUTION】資本稀釋融資：【{ticker}】發行可轉債（Convertible Notes）、增發新股（Public Offering）、宣布定價（Pricing）、或啟動市場即時配售（ATM Offering）。
4. 【EARNINGS】業績與資本回饋：正式公布季度財報核心數據、調升全年財測（Raises Guidance）、或啟動大額庫藏股回購（Buyback）。

【絕對駁回規則（命中任一條，一律回傳 PASS）】：
1. 純法說會/論壇日程公布、例行技術發表、非具名生態圈合作。
2. 股東律師集體訴訟索賠通告、內部高管升遷人事命令。
3. 【{ticker}】僅被當作同業對比提及，並非新聞主角。

【輸出格式要求】：
若不符合上述四大類，只回傳單字：PASS
若符合，嚴格依照以下 JSON 格式回傳，禁止任何多餘文字：
{{
  "type": "ORDER 或 M&A 或 DILUTION 或 EARNINGS",
  "summary": "以繁體中文條列兩點（80 字以內）：\n• 【核心動作】：融資規模/合約金額/併購標的、定價或折價細節。\n• 【市場衝擊】：對 {ticker} 之稀釋壓力、營收推升或財務影響。"
}}

新聞內容：
{text[:8000]}
"""
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "你是一位分毫不差的買方量化分析員，嚴密監控合約、併購與融資稀釋。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1
    }
    for attempt in range(3):
        try:
            res = requests.post(api_url, headers=headers, json=payload, timeout=25)
            data = res.json()
            if "error" in data:
                return "PASS", ""
            content = data["choices"][0]["message"]["content"].strip()
            if "PASS" in content:
                return "PASS", ""
            
            cleaned_json = re.sub(r"^```json\s*", "", content)
            cleaned_json = re.sub(r"\s*```$", "", cleaned_json)
            parsed = json.loads(cleaned_json)
            return parsed.get("type", "ORDER"), parsed.get("summary", "")
        except Exception:
            time.sleep(2)
    return "PASS", ""

def fetch_google_wire_news(ticker):
    # 如果代碼容易與常見英文單字撞名，自動使用公司全名/交易所代碼搜尋
    search_target = TICKER_ALIAS.get(ticker, ticker)
    query = f'{search_target} (PR Newswire OR Business Wire OR GlobeNewswire OR PRNewswire OR BusinessWire) when:2d'
    encoded_query = urllib.parse.quote(query)
    rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        res = requests.get(rss_url, headers=headers, timeout=12)
        if res.status_code != 200:
            return []
        
        root = ET.fromstring(res.content)
        items = []
        for item in root.findall("./channel/item")[:3]:
            title = item.find("title").text if item.find("title") is not None else ""
            link = item.find("link").text if item.find("link") is not None else ""
            pub_date = item.find("pubDate").text if item.find("pubDate") is not None else ""
            source = item.find("source").text if item.find("source") is not None else "Newswire"
            description = item.find("description").text if item.find("description") is not None else ""
            
            clean_title = re.sub(r"\s+-\s+.*$", "", title)

            items.append({
                "title": clean_title,
                "url": link,
                "pub_date_raw": pub_date,
                "source": source,
                "snippet": description
            })
        return items
    except Exception:
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
        
        # 1. 歷史記憶庫去重（審查過的一律本地跳過，$0 API）
        if fingerprint in sent_history:
            print("     [記憶庫略過] 此新聞已完成歷史審查，略過", flush=True)
            continue

        # 2. 本機黑名單過濾（律師索賠、法說會時程日程、人事命令）
        if is_junk_title(title):
            print("     [本地過濾] 命中公關/人事/訴訟黑名單，跳過", flush=True)
            save_sent_id(fingerprint)
            sent_history.add(fingerprint)
            continue

        # 3. 本機白名單檢查（涵蓋大單、併購、財報、可轉債、現增、ATM）
        if not has_high_impact_signal(title):
            print("     [本地過濾] 無重大財務或合約特徵詞，跳過", flush=True)
            save_sent_id(fingerprint)
            sent_history.add(fingerprint)
            continue

        print("     ⚡ [命中重大事件] 提交 GPT 進行金流與性質深審...", flush=True)
        
        pub_tw_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
        if item["pub_date_raw"]:
            try:
                pub_utc = datetime.strptime(item["pub_date_raw"][:25].strip(), "%a, %d %b %Y %H:%M:%S")
                pub_utc = pub_utc.replace(tzinfo=timezone.utc)
                pub_tw_str = pub_utc.astimezone(TW_TZ).strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass

        context = f"標題: {title}\n來源: {item['source']}\n內容摘要: {item['snippet']}"
        event_type, summary_text = summarize_with_ai(ticker, context)

        save_sent_id(fingerprint)
        sent_history.add(fingerprint)

        if event_type == "PASS" or len(summary_text) <= 10:
            print("     [AI裁定] PASS (非核心實質事件)", flush=True)
        else:
            print(f"     🎯 [AI放行] 判定為 {event_type} 事件！準備推播...", flush=True)
            send_discord_embed(ticker, title, event_type, summary_text, item["url"], pub_tw_str, item["source"])
            time.sleep(1)

def main():
    if not os.path.exists("tickers.txt"):
        print("❌ 錯誤：找不到 tickers.txt！", flush=True)
        return

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
        time.sleep(0.3)

    print("==========================================", flush=True)
    print(f"✅ 全量巡檢完成！共計掃描 {total_count} 檔標的。", flush=True)
    print("==========================================", flush=True)

if __name__ == "__main__":
    main()
