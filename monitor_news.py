import os
import re
import time
import hashlib
import requests
import yfinance as yf
from datetime import datetime, timezone, timedelta

DISCORD_NEWS_WEBHOOK = os.environ.get("DISCORD_NEWS_WEBHOOK")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
HISTORY_FILE = "sent_news_log.txt"

# 台灣時區 (UTC+8)
TW_TZ = timezone(timedelta(hours=8))

def load_sent_history():
    if not os.path.exists(HISTORY_FILE):
        return set()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def save_sent_id(item_id):
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(f"{item_id}\n")

def make_news_fingerprint(ticker, title):
    # 清理標題標點符號與多餘空格，即使新聞來源稍微修改字詞也能精準鎖定
    clean_title = re.sub(r"[^\w\s]", "", title.lower())
    clean_title = " ".join(clean_title.split())
    raw_key = f"{ticker}_{clean_title}"
    return hashlib.md5(raw_key.encode("utf-8")).hexdigest()

def send_discord_embed(ticker, title, summary, news_url, pub_date_str):
    if not DISCORD_NEWS_WEBHOOK:
        return
    now_tw_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")

    payload = {
        "username": "Commercial News Bot",
        "avatar_url": "https://s.yimg.com/cv/apiv2/social/images/yahoo_default_logo.png",
        "embeds": [{
            "title": f"💰 商業大單快訊：{ticker}",
            "url": news_url,
            "color": 0x2ECC71,
            "fields": [
                {"name": "📌 標的", "value": f"`{ticker}`", "inline": True},
                {"name": "📅 發布時間 (台灣)", "value": f"`{pub_date_str}`", "inline": True},
                {"name": "📰 標題", "value": title[:200], "inline": False},
                {"name": "💡 大單核心解讀", "value": summary, "inline": False}
            ],
            "footer": {"text": f"Yahoo Finance Feed • 推播時間: {now_tw_str}"}
        }]
    }
    try:
        res = requests.post(DISCORD_NEWS_WEBHOOK, json=payload, timeout=15)
        res.raise_for_status()
        print(f" -> [{ticker}] Discord 推播成功！", flush=True)
    except Exception as e:
        print(f" -> [{ticker}] Discord 發送失敗: {e}", flush=True)

def summarize_with_ai(ticker, text):
    if not OPENAI_API_KEY:
        return "PASS"
    api_url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    prompt = f"""
你是一位專業美股買方研究員。請審核這則新聞是否為標的【{ticker}】的直接實質大單：

審核規則（必須全部符合）：
1. 【主體身分防禦（極重要）】：新聞主體必須是【{ticker}】公司本身。如果新聞主角是其他公司（例如只是內文順帶拿 {ticker} 當同業對比、提及過去歷史），一律直接回傳單字「PASS」。
2. 【實質內容過濾】：若是例行公關發言、展會動態、分析師升降評、專利或法律訴訟、一般專訪，一律回傳單字「PASS」。
3. 只有當【{ticker}】本身簽下【實質大額採購合約】、【具名客戶合作簽約】、【重大專案交期與金額確定】時才進行解讀。
4. 符合上述所有條件時，以繁體中文條列輸出（100 字內）：
   • 【實質動作】：客戶/合作方名稱、合約金額、履約時程。
   • 【營收影響】：對該公司的實質財務貢獻預估。

新聞內容：
{text[:8000]}
"""
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "你是一位極度嚴謹、嚴防標的張冠李戴的美股買方分析員。"},
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

def check_and_process_news(ticker, sent_history):
    try:
        t = yf.Ticker(ticker)
        news_list = t.news
        if not news_list:
            print("0 則新聞", flush=True)
            return

        print(f"{len(news_list)} 則新聞", end=" ", flush=True)

        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=48)

        for item in news_list[:3]:
            content = item.get("content", item)
            title = content.get("title", "")
            summary_text = content.get("summary", "")

            click_url = ""
            if "clickThroughUrl" in content and content["clickThroughUrl"]:
                click_url = content["clickThroughUrl"].get("url", "")
            elif "canonicalUrl" in content and content["canonicalUrl"]:
                click_url = content["canonicalUrl"].get("url", "")
            if not click_url:
                click_url = item.get("link", "")

            # 產生唯一指紋並進行去重比對
            fingerprint = make_news_fingerprint(ticker, title)
            if fingerprint in sent_history:
                continue

            pub_date_tw_str = ""
            pub_time_raw = content.get("pubDate") or item.get("providerPublishTime")
            if isinstance(pub_time_raw, int):
                pub_utc_dt = datetime.fromtimestamp(pub_time_raw, tz=timezone.utc)
                if pub_utc_dt < cutoff_time:
                    continue
                pub_tw_dt = pub_utc_dt.astimezone(TW_TZ)
                pub_date_tw_str = pub_tw_dt.strftime("%Y-%m-%d %H:%M")
            elif isinstance(pub_time_raw, str):
                try:
                    clean_str = pub_time_raw.replace("Z", "+00:00")
                    pub_utc_dt = datetime.fromisoformat(clean_str)
                    pub_tw_dt = pub_utc_dt.astimezone(TW_TZ)
                    pub_date_tw_str = pub_tw_dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pub_date_tw_str = pub_time_raw[:16]

            if not pub_date_tw_str:
                pub_date_tw_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")

            context = f"標題: {title}\n摘要: {summary_text}"
            ai_res = summarize_with_ai(ticker, context)

            if "PASS" not in ai_res and len(ai_res) > 10:
                send_discord_embed(ticker, title, ai_res, click_url, pub_date_tw_str)
                save_sent_id(fingerprint)
                sent_history.add(fingerprint)
                time.sleep(1)

        print(flush=True)
    except Exception as e:
        print(f"抓取異常: {e}", flush=True)

def main():
    if not os.path.exists("tickers.txt"):
        print("❌ 錯誤：找不到 tickers.txt！", flush=True)
        return

    with open("tickers.txt", "r") as f:
        tickers = [line.strip().upper() for line in f if line.strip()]

    sent_history = load_sent_history()
    total_count = len(tickers)
    print("==========================================", flush=True)
    print(f"🚀 開始新聞巡檢，清單共計：{total_count} 檔標的", flush=True)
    print(f"🕒 當前台灣時間：{datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"📦 已攔截歷史推播紀錄：{len(sent_history)} 條", flush=True)
    print("==========================================", flush=True)

    for idx, ticker in enumerate(tickers, start=1):
        print(f"[{idx:03d}/{total_count:03d}] 正在掃描標的：{ticker:5s} ... 獲取到 ", end="", flush=True)
        check_and_process_news(ticker, sent_history)
        time.sleep(0.3)

    print("==========================================", flush=True)
    print(f"✅ 全量巡檢完成！共計掃描 {total_count} 檔標的。", flush=True)
    print("==========================================", flush=True)

if __name__ == "__main__":
    main()
