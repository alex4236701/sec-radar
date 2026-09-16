import os
import time
import requests
from datetime import datetime, timezone, timedelta

DISCORD_NEWS_WEBHOOK = os.environ.get("DISCORD_NEWS_WEBHOOK")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")

print(f"=== 環境變數檢查 ===")
print(f"DISCORD_NEWS_WEBHOOK 是否存在: {'是' if DISCORD_NEWS_WEBHOOK else '否'}")
print(f"OPENAI_API_KEY 是否存在: {'是' if OPENAI_API_KEY else '否'}")
print(f"FINNHUB_API_KEY 是否存在: {'是' if FINNHUB_API_KEY else '否'}")

def send_discord_embed(ticker, title, summary, news_url, pub_date):
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
                {"name": "📅 發布日期", "value": f"`{pub_date}`", "inline": True},
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
        print(f"[{ticker}] 成功發送 Discord 推播！")
    except Exception as e:
        print(f"[{ticker}] Discord 發送失敗: {e}")

def summarize_with_ai(ticker, text):
    if not OPENAI_API_KEY:
        return "PASS"
    api_url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    prompt = f"""
你是一位專業美股買方研究員。請審查以下 {ticker} 的商業新聞：

審核規則：
1. 若新聞純屬例行公關發言、參加展會、分析師升降評、一般演講，直接回覆單字「PASS」。
2. 只有涉及【實質大額採購合約】、【具名合作夥伴簽約】、【產品交付時程與金額】時才解讀。
3. 若符合重大標準，以繁體中文條列輸出（100 字內）：
   • 【實質動作】：客戶/合作方名稱、合約金額、時程。
   • 【營收影響】：實質財務貢獻預估。

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
                print(f"[{ticker}] OpenAI 報錯: {data['error'].get('message')}")
                return "PASS"
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"[{ticker}] OpenAI 連線失敗: {e}")
            time.sleep(2)
    return "PASS"

def main():
    if not os.path.exists("tickers.txt"):
        print("找不到 tickers.txt！")
        return
    with open("tickers.txt", "r") as f:
        tickers = [line.strip().upper() for line in f if line.strip()]

    # 放寬查詢天數至 7 天，確保一定能抓到近期的代表性新聞
    today_str = datetime.now().strftime("%Y-%m-%d")
    from_str = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    print(f"開始掃描，查詢日期區間: {from_str} 至 {today_str}，共 {len(tickers)} 檔標的")

    for ticker in tickers:
        url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={from_str}&to={today_str}&token={FINNHUB_API_KEY}"
        try:
            res = requests.get(url, timeout=10)
            items = res.json()
            
            if not isinstance(items, list):
                print(f"[{ticker}] Finnhub 回應非清單格式，回傳內容: {items}")
                continue
                
            print(f"[{ticker}] 抓到 {len(items)} 則新聞")
            if len(items) == 0:
                continue

            # 檢查最新的 3 則
            for item in items[:3]:
                headline = item.get("headline", "")
                summary_text = item.get("summary", "")
                news_url = item.get("url", "")
                pub_time = item.get("datetime", 0)
                pub_date = datetime.fromtimestamp(pub_time, tz=timezone.utc).strftime("%Y-%m-%d") if pub_time else today_str
                
                context = f"標題: {headline}\n摘要: {summary_text}"
                ai_res = summarize_with_ai(ticker, context)
                
                print(f"  -> 標題: {headline[:40]}...")
                print(f"  -> AI 判定: {'PASS (過濾略過)' if 'PASS' in ai_res else '重大消息，準備推播'}")

                if "PASS" not in ai_res and len(ai_res) > 10:
                    send_discord_embed(ticker, headline, ai_res, news_url, pub_date)
                    time.sleep(1)
        except Exception as e:
            print(f"[{ticker}] 執行過程發生異常: {e}")
        time.sleep(1)

if __name__ == "__main__":
    main()
