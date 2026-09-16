import os
import time
import requests
from datetime import datetime, timezone, timedelta

DISCORD_NEWS_WEBHOOK = os.environ.get("DISCORD_NEWS_WEBHOOK")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")

def send_discord_embed(ticker, title, summary, news_url):
    if not DISCORD_NEWS_WEBHOOK:
        return
    payload = {
        "username": "Commercial News Bot",
        "avatar_url": "https://finnhub.io/static/img/finnhub_logo.png",
        "embeds": [{
            "title": f"💰 商業大單快訊：{ticker}",
            "url": news_url,
            "color": 0x2ECC71,
            "fields": [
                {"name": "📌 標的", "value": f"`{ticker}`", "inline": True},
                {"name": "📰 標題", "value": title[:200], "inline": False},
                {"name": "💡 大單核心解讀", "value": summary, "inline": False}
            ],
            "footer": {"text": "Finnhub Official Wire Monitor"},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }]
    }
    try:
        res = requests.post(DISCORD_NEWS_WEBHOOK, json=payload, timeout=15)
        res.raise_for_status()
    except Exception as e:
        print(f"新聞 Discord 發送失敗: {e}")

def summarize_with_ai(ticker, text):
    if not OPENAI_API_KEY:
        return "PASS"
    api_url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    prompt = f"""
你是一位專業美股買方研究員。請審查以下 {ticker} 的商業新聞：
1. 若僅為例行公關、評級、參展、一般演講，直接回覆單字「PASS」。
2. 只有涉及【實質大額採購合約】、【具名合作夥伴簽約】、【產品交期與金額】時才解讀。
3. 輸出重點（80 字內）：
   - 【實質動作】：客戶名稱、合約金額、時程。
   - 【營收影響】：實質財務貢獻預估。

內容：
{text[:8000]}
"""
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "你是一位專注過濾商業大單的買方分析員。"},
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

def main():
    if not os.path.exists("tickers.txt") or not FINNHUB_API_KEY:
        return
    with open("tickers.txt", "r") as f:
        tickers = [line.strip().upper() for line in f if line.strip()]

    today_str = datetime.now().strftime("%Y-%m-%d")
    from_str = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")

    for ticker in tickers:
        url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={from_str}&to={today_str}&token={FINNHUB_API_KEY}"
        try:
            res = requests.get(url, timeout=10)
            items = res.json()
            if not isinstance(items, list):
                continue
            for item in items[:3]:
                headline = item.get("headline", "")
                summary_text = item.get("summary", "")
                news_url = item.get("url", "")
                context = f"標題: {headline}\n摘要: {summary_text}"

                ai_res = summarize_with_ai(ticker, context)
                if "PASS" not in ai_res and len(ai_res) > 10:
                    send_discord_embed(ticker, headline, ai_res, news_url)
                    time.sleep(1)
        except Exception as e:
            print(f"Finnhub 查詢 {ticker} 錯誤: {e}")
        time.sleep(1)

if __name__ == "__main__":
    main()
