import os
import time
import requests
import yfinance as yf
from datetime import datetime, timezone, timedelta

DISCORD_NEWS_WEBHOOK = os.environ.get("DISCORD_NEWS_WEBHOOK")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

def send_discord_embed(ticker, title, summary, news_url, pub_date):
    if not DISCORD_NEWS_WEBHOOK:
        return
    payload = {
        "username": "Commercial News Bot",
        "avatar_url": "https://s.yimg.com/cv/apiv2/social/images/yahoo_default_logo.png",
        "embeds": [{
            "title": f"💰 商業大單快訊：{ticker}",
            "url": news_url,
            "color": 0x2ECC71,
            "fields": [
                {"name": "📌 標的", "value": f"`{ticker}`", "inline": True},
                {"name": "📅 發布日期", "value": f"`{pub_date}`", "inline": True},
                {"name": "📰 標題", "value": title[:200], "inline": False},
                {"name": "💡 大單核心解讀", "value": summary, "inline": False}
            ],
            "footer": {"text": "Yahoo Finance Official News Feed"},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }]
    }
    try:
        res = requests.post(DISCORD_NEWS_WEBHOOK, json=payload, timeout=15)
        res.raise_for_status()
        print(f"[{ticker}] 推播成功！")
    except Exception as e:
        print(f"[{ticker}] Discord 發送失敗: {e}")

def summarize_with_ai(ticker, text):
    if not OPENAI_API_KEY:
        return "PASS"
    api_url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    prompt = f"""
你是一位專業美股買方研究員。請審核這則新聞是否為標的【{ticker}】的直接實質大單：

審核規則（必須全部符合）：
1. 【主體身分防禦】：新聞主體必須是【{ticker}】公司本身。如果新聞主角是其他公司（例如只是內文順帶拿 {ticker} 當同業對比、提及過去歷史），一律直接回傳單字「PASS」。
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

def check_yahoo_news(ticker):
    try:
        t = yf.Ticker(ticker)
        news_list = t.news
        if not news_list:
            return

        # 鎖定過去 48 小時內的新聞
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=48)

        # 檢視最新的 3 則
        for item in news_list[:3]:
            # yfinance 新版結構通常在 content 欄位內，兼顧新舊結構相容
            content = item.get("content", item)
            
            title = content.get("title", "")
            summary_text = content.get("summary", "")
            
            # 取得文章連結
            click_url = ""
            if "clickThroughUrl" in content and content["clickThroughUrl"]:
                click_url = content["clickThroughUrl"].get("url", "")
            elif "canonicalUrl" in content and content["canonicalUrl"]:
                click_url = content["canonicalUrl"].get("url", "")
            if not click_url:
                click_url = item.get("link", "")

            # 取得發布時間
            pub_date_str = ""
            pub_time_raw = content.get("pubDate") or item.get("providerPublishTime")
            if isinstance(pub_time_raw, int):
                pub_dt = datetime.fromtimestamp(pub_time_raw, tz=timezone.utc)
                if pub_dt < cutoff_time:
                    continue
                pub_date_str = pub_dt.strftime("%Y-%m-%d")
            elif isinstance(pub_time_raw, str):
                pub_date_str = pub_time_raw[:10]

            context = f"標題: {title}\n摘要: {summary_text}"
            ai_res = summarize_with_ai(ticker, context)

            if "PASS" not in ai_res and len(ai_res) > 10:
                send_discord_embed(ticker, title, ai_res, click_url, pub_date_str)
                time.sleep(1)

    except Exception as e:
        print(f"[{ticker}] Yahoo 抓取異常: {e}")

def main():
    if not os.path.exists("tickers.txt"):
        return

    with open("tickers.txt", "r") as f:
        tickers = [line.strip().upper() for line in f if line.strip()]

    print(f"開始透過 Yahoo Finance 掃描 {len(tickers)} 檔標的...")
    for idx, ticker in enumerate(tickers, start=1):
        check_yahoo_news(ticker)
        # 輕量間隔 0.3 秒即可，快速且無頻率限制壓力
        time.sleep(0.3)

    print("新聞巡檢全數完成。")

if __name__ == "__main__":
    main()
