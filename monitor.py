import os
import re
import time
import requests
from datetime import datetime, timedelta

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

def send_discord_embed(ticker, form, filing_date, summary, doc_url):
    if not DISCORD_WEBHOOK_URL:
        print("未設定 DISCORD_WEBHOOK_URL，略過發送。")
        return

    # 針對不同申報設定卡片側邊顏色（十六進位整數）
    color_map = {
        "8-K": 0xE74C3C,    # 警戒紅：重大事件、收購、突發
        "424B5": 0xE67E22,  # 警示橘：公司發新股/發債稀釋
        "424B7": 0x9B59B6,  # 風險紫：早期大股東倒貨離場
        "10-Q": 0x3498DB,   # 營運藍：季度財報
        "10-K": 0x2ECC71    # 財報綠：年度財報
    }
    embed_color = color_map.get(form, 0x95A5A6)

    payload = {
        "username": "SEC Radar Bot",
        "avatar_url": "https://www.sec.gov/themes/custom/uswds_sec/assets/img/sec-logo.svg",
        "embeds": [
            {
                "title": f"🚨 SEC 重大申報速報：{ticker} ({form})",
                "url": doc_url,
                "color": embed_color,
                "fields": [
                    {
                        "name": "📌 申報代號",
                        "value": f"`{ticker}`",
                        "inline": True
                    },
                    {
                        "name": "📄 表單種類",
                        "value": f"`{form}`",
                        "inline": True
                    },
                    {
                        "name": "📅 申報日期",
                        "value": f"`{filing_date}`",
                        "inline": True
                    },
                    {
                        "name": "💡 AI 核心解讀",
                        "value": summary,
                        "inline": False
                    }
                ],
                "footer": {
                    "text": "SEC EDGAR Automated Intelligence Radar"
                },
                "timestamp": datetime.utcnow().isoformat()
            }
        ]
    }

    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
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
    if not GEMINI_API_KEY:
        return "未設定 GEMINI_API_KEY，請直接查閱原始連結。"

    clean_text = clean_html(content_text)
    if len(clean_text) < 50:
        return "申報內文缺乏實質文字或非純文字結構，請參閱原始文件。"

    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    prompt = f"""
你是一位專業美股研究員。請閱讀以下 {ticker} 的 SEC {form} 申報純文字內容，用台灣日常大白話繁體中文輸出重點：
1. 核心實質動作（例如：增資總額與每股定價、大股東出清持股規模、收購合併標的、重大合約金額）。
2. 實質財務或營運影響（股權稀釋比例、負債變動、對獲利的影響）。
嚴禁行銷詞彙與無意義廢話，120 字以內直接講具體數據與動作。

申報內文節錄：
{clean_text[:6000]}
"""
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        res = requests.post(api_url, headers=headers, json=payload, timeout=25)
        data = res.json()
        if "error" in data:
            err_msg = data['error'].get('message', '未知錯誤')
            return f"API 錯誤：{err_msg}"
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        return f"連線異常：{str(e)}"

def check_sec_filings():
    if not os.path.exists("tickers.txt"):
        return

    with open("tickers.txt", "r") as f:
        tickers = [line.strip().upper() for line in f if line.strip()]

    headers = {"User-Agent": "InstitutionalResearchUser investor@example.com"}

    for ticker in tickers:
        try:
            cik_url = "https://www.sec.gov/files/company_tickers.json"
            res = requests.get(cik_url, headers=headers).json()

            cik = None
            for key, val in res.items():
                if val["ticker"] == ticker:
                    cik = str(val["cik_str"]).zfill(10)
                    break

            if not cik:
                continue

            sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            sub_res = requests.get(sub_url, headers=headers).json()

            recent = sub_res["filings"]["recent"]
            for i in range(min(5, len(recent["form"]))):
                form = recent["form"][i]
                filing_date = recent["filingDate"][i]
                accession_number = recent["accessionNumber"][i].replace("-", "")
                primary_doc = recent["primaryDocument"][i]

                yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                today = datetime.now().strftime("%Y-%m-%d")

                # 僅鎖定五大關鍵重大申報，徹底排除 4 與 144
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

                        send_discord_embed(
                            ticker=ticker,
                            form=form,
                            filing_date=filing_date,
                            summary=ai_summary,
                            doc_url=doc_url
                        )
                        time.sleep(2)
        except Exception as e:
            print(f"處理 {ticker} 錯誤: {e}")

if __name__ == "__main__":
    check_sec_filings()
