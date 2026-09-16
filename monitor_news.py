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

# 易撞名股票映射表：徹底阻絕澳洲證交所 (ASX)、物聯網 (IoT)、貓咪 (CAT) 等單字干擾
TICKER_ALIAS = {
    "CAT": '("Caterpillar" OR "NYSE:CAT")',
    "IOT": '("Samsara" OR "NYSE:IOT")',
    "ASX": '("ASE Technology" OR "NYSE:ASX")',
    "NOW": '("ServiceNow" OR "NYSE:NOW")',
    "ON": '("ON Semiconductor" OR "Nasdaq:ON")',
    "ALL": '("Allstate" OR "NYSE:ALL")',
    "IT": '("Gartner" OR "NYSE:IT")',
    "MET": '("MetLife" OR "NYSE:MET")',
    "KEYS": '("Keysight" OR "NYSE:KEYS")'
}

# 1. 負向黑名單：純會議日程、人事升遷、律所索賠（$0 本地封存）
EXCLUDE_TITLE_PATTERNS = [
    r"\bto\s+report\b", r"\bschedules?\b", r"\bto\s+host\b", r"\bwebcast\b",
    r"\bconference\s+call\b", r"\binvestor\s+conference\b", r"\bpresentation\b",
    r"\bprice\s+target\b", r"\brating\b", r"\bclass\s+action\b", r"\blawsuit\b",
    r"\bshareholder\s+alert\b", r"\breminds\s+investors\b",
    r"\bappoints?\b", r"\bnames?\s+new\b"
]

# 2. 正向白名單：大單 + 併購 + 財報 + 融資稀釋 + 重大產品上市/監管核准
SIGNAL_PATTERNS = [
    # A. 商業大單與客戶合約
    r"\bcontract\b", r"\border\b", r"\borders\b", r"\bdeal\b", r"\baward\b",
    r"\bawarded\b", r"\bagreement\b", r"\bprocurement\b", r"\bsupply\b",
    r"\bselected\s+by\b", r"\bpartnered\s+with\b", r"\bto\s+deploy\b", r"\bsecures?\b",

    # B. 重大併購與股權投資
    r"\bacquisition\b", r"\bacquires?\b", r"\binvests?\s+in\b", r"\bbuyout\b",
    r"\bstake\b", r"\bmerger\b",

    # C. 實質財報與財測調升
    r"\breports?\s+first\s+quarter\b", r"\breports?\s+second\s+quarter\b",
    r"\breports?\s+third\s+quarter\b", r"\breports?\s+fourth\s+quarter\b",
    r"\breports?\s+full\s+year\b", r"\bfinancial\s+results\b",
    r"\braises?\s+guidance\b", r"\braises?\s+outlook\b",

    # D. 庫藏股回購
    r"\brepurchase\b", r"\bbuyback\b", r"\bshare\s+repurchase\b",

    # E. 融資稀釋（可轉債 / 現增 / ATM）
    r"\bconvertible\b", r"\bsenior\s+notes\b", r"\bpublic\s+offering\b",
    r"\bsecondary\s+offering\b", r"\bprices\s+offering\b", r"\bpricing\s+of\b",
    r"\bat-the-market\b", r"\batm\s+offering\b", r"\batm\s+facility\b",
    r"\bcommon\s+stock\s+offering\b",

    # F. 重大產品與監管審批（次世代旗艦、量產發布、FDA 藥證）
    r"\blaunches\b", r"\bunveils\b", r"\bintroduces\b", r"\bnext-gen\b",
    r"\barchitecture\b", r"\bproduction\s+release\b", r"\bfda\s+approv",
    r"\bclearance\b", r"\bbreakthrough\b",

    # G. 金額特徵
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

    if event_type == "DILUTION":
        card_title = f"⚠️ 資本融資與稀釋警報：{ticker}"
        embed_color = 0xE74C3C  # 紅色
        type_desc = "股權融資/稀釋（可轉債、現增或 ATM）"
    elif event_type == "M&A":
        card_title = f"🤝 戰略併購/股權投資：{ticker}"
        embed_color = 0x9B59B6  # 紫色
        type_desc = "資本運作（收購/股權投資）"
    elif event_type == "EARNINGS":
        card_title = f"📊 正式財報/指引更新：{ticker}"
        embed_color = 0x3498DB  # 藍色
        type_desc = "官方財報或營收指引（Guidance）"
    elif event_type == "PRODUCT":
        card_title = f"🚀 重大產品/技術突破：{ticker}"
        embed_color = 0x1ABC9C  # 藍綠色
        type_desc = "次世代旗艦產品上市 / 監管批准"
    else:
        card_title = f"💰 商業大單快訊：{ticker}"
        embed_color = 0x2ECC71  # 亮綠色
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

【絕對駁回規則（命中任一條，一律回傳 PASS）】：
1. 【主體非該公司】：新聞主角必須是【{ticker}】這家公司本身！
   - 若新聞只是提到產業詞彙（如「IoT 物聯網」業務、「CAT 貓咪」飼料），回傳 PASS。
   - 若新聞是其他公司在澳洲證券交易所（ASX）上市或融資，回傳 PASS。
   - 若其他公司進行交易，僅在內文把【{ticker}】當成同業或產業名詞提及，一律強制回傳 PASS。
2. 【例行行銷軟文】：常規軟體小版本更新、例行展會演講、無具體時程的純概念展示，回傳 PASS。
3. 純法說會/論壇時程公布、律師集體訴訟通告、內部高管人事升遷。

【符合監控的五大類別】：
1. 【ORDER】商業大單：外部客戶/政府向【{ticker}】採購產品、簽訂重大供貨合約。
2. 【M&A】重大併購/投資：【{ticker}】收購同業、買下重要公司股權、或合併案。
3. 【DILUTION】資本稀釋融資：【{ticker}】發行可轉債、增發新股、宣布定價、或啟動 ATM 配售。
4. 【EARNINGS】業績與資本回饋：【{ticker}】公布季度財報、調升全年財測、或啟動庫藏股回購。
5. 【PRODUCT】重大產品上市/監管突破：【{ticker}】正式發布重大次世代架構/旗艦新產品（公佈量產時程或規格突破），或取得重要監管放行（如 FDA 藥證核准）。

【輸出格式要求】：
若不符合上述五大類，只回傳單字：PASS
若符合，嚴格依照以下 JSON 格式回傳，禁止多餘文字：
{{
  "type": "ORDER 或 M&A 或 DILUTION 或 EARNINGS 或 PRODUCT",
  "summary": "以繁體中文條列兩點（80 字以內）：\n• 【核心動作】：產品型號/融資規模/合約金額/併購標的及具體時程。\n• 【市場衝擊】：對 {ticker} 之營收貢獻、競爭優勢或稀釋壓力。"
}}

新聞內容：
{text[:8000]}
"""
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "你是一位分毫不差的買方量化分析員，嚴格確認新聞主體是否為指定股票，絕不腦補。"},
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
    search_target = TICKER_ALIAS.get(ticker, ticker)
    query = f'{search_target} ("PR Newswire" OR "Business Wire" OR "GlobeNewswire") when:2d'
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
        
        if fingerprint in sent_history:
            print("     [記憶庫略過] 此新聞已完成歷史審查，略過", flush=True)
            continue

        if is_junk_title(title):
            print("     [本地過濾] 命中公關/人事/訴訟黑名單，跳過", flush=True)
            save_sent_id(fingerprint)
            sent_history.add(fingerprint)
            continue

        if not has_high_impact_signal(title):
            print("     [本地過濾] 無重大財務或合約特徵詞，跳過", flush=True)
            save_sent_id(fingerprint)
            sent_history.add(fingerprint)
            continue

        print("     ⚡ [命中重大事件] 提交 GPT 進行主體與性質深審...", flush=True)
        
        pub_tw_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
        if item["pub_date_raw"]:
            try:
                # 標準 RFC 2822 解析，精準防禦所有時區文字格式
                pub_dt = parsedate_to_datetime(item["pub_date_raw"])
                pub_tw_str = pub_dt.astimezone(TW_TZ).strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass

        context = f"標題: {title}\n來源: {item['source']}\n內容摘要: {item['snippet']}"
        event_type, summary_text = summarize_with_ai(ticker, context)

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
