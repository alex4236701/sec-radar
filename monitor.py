import os
import re
import time
import requests
from datetime import datetime, timezone, timedelta

# 雙頻道分流 Webhook 設定
DISCORD_SEC_WEBHOOK = os.environ.get("DISCORD_SEC_WEBHOOK")
DISCORD_NEWS_WEBHOOK = os.environ.get("DISCORD_NEWS_WEBHOOK")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")

def send_discord_embed(webhook_url, ticker, tag, title, summary, doc_url, color=0x3498DB):
    if not webhook_url:
        print(f"未設定目標 Webhook，略過 {tag} 發送。")
        return

    payload = {
        "username": "SEC & News Radar Bot",
        "avatar_url": "https://www.sec.gov/themes/custom/uswds_sec/assets/img/sec-logo.svg",
        "embeds": [
            {
                "title": f"📢 {ticker} 重大情報：{tag}",
                "url": doc_url,
                "color": color,
                "fields": [
                    {
                        "name": "📌 標的",
                        "value": f"`{ticker}`",
                        "inline": True
                    },
                    {
                        "name": "📄 類別",
                        "value": f"`{tag}`",
                        "inline": True
                    },
                    {
                        "name": "💡 AI 核心解讀",
                        "value": summary,
                        "inline": False
                    }
                ],
                "footer": {
                    "text": "SEC & Finnhub Automated Intelligence Radar"
                },
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        ]
    }

    try:
        res = requests.post(webhook_url, json=payload, timeout=15)
        res.raise_for_status()
    except Exception as e:
        print(f"發送 Discord 失敗: {e}")

def clean_html(raw_html):
    cleared = re.sub(r'<(style|script)[^>]*>.*?</\1>', '', raw_html, flags=re.DOTALL | re.IGNORECASE)
    cleared = re.sub(r'<[^>]+>', ' ', cleared)
    cleared = re.sub(r'&[a-z#0-9]+;', ' ', cleared)
    cleared = re.sub(r'\s+', ' ', cleared)
    return cleared.strip()

def summarize_with_ai(ticker, source_type, content_text):
    if not OPENAI_API_KEY:
        return "PASS"

    clean_text = clean_html(content_text)
    if len(clean_text) < 40:
        return "PASS"

    api_url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    prompt = f"""
你是一位專業美股買方研究員。請閱讀以下 {ticker} 的資訊（來源：{source_type}）：

審核規則：
1. 若這是一篇商業新聞，且內容僅為一般行銷宣傳、獲獎、例行參展、分析師評級調整、一般論壇演講，請直接回覆單字「PASS」，不要輸出任何其他內容。
2. 只有在涉及【具體商業大單】、【實質採購合約金額】、【具名合作夥伴簽約】、【產品交付排程】或 SEC 申報重大事實時才進行解讀。
3. 若符合重大標準，請以台灣繁體中文條列輸出（100 字內）：
   - 【實質動作】：客戶/合作方名稱、合約總額、交付或履約時程。
   - 【量化影響】：營收挹注預估或財務實質變動。

內容：
{clean_text[:12000]}
"""

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "你是一位專注於實質合約與數據定調的買方研究員。"},
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

def check_finnhub_news(ticker):
    if not FINNHUB_API_KEY or not DISCORD_NEWS_WEBHOOK:
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    from_str = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")

    url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={from_str}&to={today_str}&token={FINNHUB_API_KEY}"

    try:
        res = requests.get(url, timeout=10)
        news_items = res.json()
        if not isinstance(news_items, list):
            return

        for item in news_items[:3]:
            headline = item.get("headline", "")
            summary_text = item.get("summary", "")
            news_url = item.get("url", "")
            full_context = f"標題: {headline}\n內文: {summary_text}"

            ai_analysis = summarize_with_ai(ticker, "商業通訊/新聞室", full_context)

            if "PASS" not in ai_analysis and len(ai_analysis) > 10:
                send_discord_embed(
                    webhook_url=DISCORD_NEWS_WEBHOOK,
                    ticker=ticker,
                    tag="商業大單/官方新聞",
                    title=headline,
                    summary=ai_analysis,
                    doc_url=news_url,
                    color=0x2ECC71  # 商業新聞走綠色卡片
                )
                time.sleep(1)
    except Exception as e:
        print(f"Finnhub 查詢 {ticker} 失敗: {e}")

def check_sec_filings(ticker):
    if not DISCORD_SEC_WEBHOOK:
        return

    headers = {"User-Agent": "InstitutionalResearchUser investor@example.com"}
    try:
        cik_url = "https://www.sec.gov/files/company_tickers.json"
        res = requests.get(cik_url, headers=headers).json()

        cik = None
        for key, val in res.items():
            if val["ticker"] == ticker:
                cik = str(val["cik_str"]).zfill(10)
                break

        if not cik:
            return

        sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        sub_res = requests.get(sub_url, headers=headers).json()

        color_map = {
            "8-K": 0xE74C3C,
            "424B5": 0xE67E22,
            "424B7": 0x9B59B6,
            "10-Q": 0x3498DB,
            "10-K": 0x2ECC71
        }

        recent = sub_res["filings"]["recent"]
        for i in range(min(4, len(recent["form"]))):
            form = recent["form"][i]
            filing_date = recent["filingDate"][i]
            accession_number = recent["accessionNumber"][i].replace("-", "")
            primary_doc = recent["primaryDocument"][i]

            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            today = datetime.now().strftime("%Y-%m-%d")

            if filing_date in [yesterday, today]:
                if form in ["8-K", "10-Q", "10-K", "424B5", "424B7"]:
                    doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_number}/{primary_doc}"
                    doc_text = ""
                    try:
                        doc_res = requests.get(doc_url, headers=headers, timeout=10)
                        doc_text = doc_res.text
                    except Exception:
                        doc_text = ""

                    ai_summary = summarize_with_ai(ticker, f"SEC {form}", doc_text)
                    if "PASS" not in ai_summary:
                        send_discord_embed(
                            webhook_url=DISCORD_SEC_WEBHOOK,
                            ticker=ticker,
                            tag=f"SEC {form}",
                            title=f"{ticker} 官方申報",
                            summary=ai_summary,
                            doc_url=doc_url,
                            color=color_map.get(form, 0xE74C3C)
                        )
                        time.sleep(1)
    except Exception as e:
        print(f"處理 SEC {ticker} 錯誤: {e}")

def main():
    if not os.path.exists("tickers.txt"):
        return

    with open("tickers.txt", "r") as f:
        tickers = [line.strip().upper() for line in f if line.strip()]

    for ticker in tickers:
        check_sec_filings(ticker)
        check_finnhub_news(ticker)
        time.sleep(1)

if __name__ == "__main__":
    main()
