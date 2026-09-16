import os
import re
import time
import requests
from datetime import datetime, timezone, timedelta

DISCORD_SEC_WEBHOOK = os.environ.get("DISCORD_SEC_WEBHOOK")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

def send_discord_embed(ticker, form, summary, doc_url, color=0xE74C3C):
    if not DISCORD_SEC_WEBHOOK:
        return

    payload = {
        "username": "SEC & News Radar Bot",
        "avatar_url": "https://www.sec.gov/themes/custom/uswds_sec/assets/img/sec-logo.svg",
        "embeds": [
            {
                "title": f"📢 {ticker} 重大情報：SEC {form}",
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
                        "value": f"`SEC {form}`",
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
        res = requests.post(DISCORD_SEC_WEBHOOK, json=payload, timeout=15)
        res.raise_for_status()
    except Exception as e:
        print(f"發送 Discord 失敗: {e}")

def clean_html(raw_html):
    cleared = re.sub(r'<(style|script)[^>]*>.*?</\1>', '', raw_html, flags=re.DOTALL | re.IGNORECASE)
    cleared = re.sub(r'<[^>]+>', ' ', cleared)
    cleared = re.sub(r'&[a-z#0-9]+;', ' ', cleared)
    cleared = re.sub(r'\s+', ' ', cleared)
    return cleared.strip()

def summarize_with_ai(ticker, form, content_text):
    if not OPENAI_API_KEY:
        return "未設定 OPENAI_API_KEY"

    clean_text = clean_html(content_text)
    if len(clean_text) < 40:
        return "PASS"

    api_url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    prompt = f"""
你是一位專業美股買方研究員。請閱讀以下 {ticker} 的 SEC {form} 官方申報內容：

審核規則：
1. 嚴禁機械式標籤與填空廢話（嚴禁輸出類似「收購標的：無」、「例行程序：無」等內容），不要輸出任何 Markdown 大標題（如 ###）。
2. 直接以台灣繁體中文條列輸出（100 字內），格式嚴格固定為以下兩點：
   • 【實質動作】：交代具體簽約/申報對象、合約或交易總額、交付或租賃履約時程。若僅為一般例行申報且無重大財務變更，直接寫「例行程序申報，無實質營運或數據變動。」
   • 【量化影響】：說明該交易對產能、營收挹注、稀釋比例或實質財務的直接影響。

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

                    ai_summary = summarize_with_ai(ticker, form, doc_text)
                    if "PASS" not in ai_summary:
                        send_discord_embed(
                            ticker=ticker,
                            form=form,
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
        time.sleep(1)

if __name__ == "__main__":
    main()
