import os
import re
import time
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

# 排除專欄、投行升降評、以及非賣方大單之關鍵字黑名單
EXCLUDE_TITLE_PATTERNS = [
    r"\bbets?\b", r"\bstake\b", r"\bacquisition\b", r"\bacquires?\b", 
    r"\binvests?\s+in\b", r"\bbuyout\b", r"\bprice\s+target\b", r"\brating\b",
    r"\bshareholder\b", r"\bclass\s+action\b", r"\blawsuit\b"
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
    # 清理標題符號與多餘空白，確保相同標題產生唯一特徵雜湊
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

def send_discord_embed(ticker, title, summary, news_url, pub_date_str, source_name):
    if not DISCORD_NEWS_WEBHOOK:
        return
    now_tw_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")

    payload = {
        "username": "Newswire Commercial Bot",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/2965/2965879.png",
        "embeds": [{
            "title": f"🏛️ 官方通訊社大單：{ticker}",
            "url": news_url,
            "color": 0x1ABC9C,
            "fields": [
                {"name": "📌 標的", "value": f"`{ticker}`", "inline": True},
                {"name": "📅 發布時間 (台灣)", "value": f"`{pub_date_str}`", "inline": True},
                {"name": "📡 官方來源", "value": f"`{source_name}`", "inline": True},
                {"name": "📰 標題", "value": title[:200], "inline": False},
                {"name": "💡 大單核心解讀", "value": summary, "inline": False}
            ],
            "footer": {"text": f"Wire Feed • 推播時間: {now_tw_str}"}
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
        return "PASS"
    api_url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    
    prompt = f"""
你是一位講求實質金流的美股買方研究員。請審核這則官方通訊社新聞是否為【{ticker}】身為「賣方/供應商」，接獲外部客戶的「實質商業營收合約」：

【絕對駁回規則（命中任一條，強制回傳單字 PASS）】：
1. 【金流方向錯誤】：如果這是【{ticker}】掏錢（例如：收購公司、投資股權、採購設備支出、委託贊助），這屬於「支出/投資」，不是接單，強制回傳 PASS。
2. 【非營收大單】：例行參展發言、技術專利獲准、訴訟或集體訴訟通告、例行財報發布日程、高管人事任命，一律回傳 PASS。
3. 【主體非直接得標者】：新聞主角必須是【{ticker}】本身。若只是被拿來當同業對比，回傳 PASS。

【通過標準】：
必須是外部客戶、政府單位向【{ticker}】進行具名採購、簽訂重大供貨或商業履約合約，能為【{ticker}】帶來實質銷售營收。

若完全符合上述要求，請以繁體中文條列輸出（100 字以內，嚴禁客套話與推測）：
• 【實質動作】：客戶名稱、合約金額、採購產品與預計履約時程。
• 【營收影響】：預計為 {ticker} 帶來的實質營收貢獻或財務影響。

新聞內容：
{text[:8000]}
"""
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "你是一位分毫不差、嚴格過濾非營收新聞的買方分析員。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1
    }
    for attempt in range(3):
        try:
            res = requests.post(api_url, headers=headers, json=payload, timeout=25)
            data = res.json()
            if "error" in data:
                return "PASS"
            return data["choices"][0]["message"]["content"].strip()
        except Exception:
            time.sleep(2)
    return "PASS"

def fetch_google_wire_news(ticker):
    query = f'{ticker} (PR Newswire OR Business Wire OR GlobeNewswire OR PRNewswire OR BusinessWire) when:2d'
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

        if is_junk_title(title):
            print("     [排除] 命中黑名單關鍵字", flush=True)
            continue

        fingerprint = make_news_fingerprint(ticker, title)
        if fingerprint in sent_history:
            print("     [略過] 此新聞先前已推播過", flush=True)
            continue

        pub_tw_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
        if item["pub_date_raw"]:
            try:
                pub_utc = datetime.strptime(item["pub_date_raw"][:25].strip(), "%a, %d %b %Y %H:%M:%S")
                pub_utc = pub_utc.replace(tzinfo=timezone.utc)
                pub_tw_str = pub_utc.astimezone(TW_TZ).strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass

        context = f"標題: {title}\n來源: {item['source']}\n內容摘要: {item['snippet']}"
        ai_res = summarize_with_ai(ticker, context)

        if "PASS" in ai_res or len(ai_res) <= 10:
            print("     [AI裁定] PASS (非實質銷售大單/例行公關稿)", flush=True)
        else:
            print(f"     🎯 [AI放行] 判定為商業大單！準備發送通知...", flush=True)
            send_discord_embed(ticker, title, ai_res, item["url"], pub_tw_str, item["source"])
            save_sent_id(fingerprint)
            sent_history.add(fingerprint)
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
    print(f"🏛️ 啟動三大官方通訊社直連巡檢，清單共計：{total_count} 檔標的", flush=True)
    print(f"🕒 當前台灣時間：{datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"📦 已記錄歷史推播紀錄：{len(sent_history)} 條", flush=True)
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
