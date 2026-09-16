import os
import re
import time
import requests
from datetime import datetime, timezone, timedelta

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

def send_discord_embed(ticker, form, filing_date, summary, doc_url):
    if not DISCORD_WEBHOOK_URL:
        print("未設定 DISCORD_WEBHOOK_URL，略過發送。")
        return

    # 卡片側邊色彩配置
    color_map = {
        "8-K": 0xE74C3C,    # 警戒紅：重大事件、收購、突發
        "424B5": 0xE67E22,  # 警示橘：公司發新股/發債籌資
        "424B7": 0x9B59B6,  # 風險紫：早期大股東出清持股
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
                        "name": "💡 AI 深度解讀 (GPT-4o-mini)",
                        "value": summary,
                        "inline": False
                    }
                ],
                "footer": {
                    "text": "SEC EDGAR Automated Intelligence Radar"
                },
                "timestamp": datetime.now(timezone.utc).isoformat()
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
    if not OPENAI_API_KEY:
        return "未設定 OPENAI_API_KEY，請直接查閱原始連結。"

    clean_text = clean_html(content_text)
    if len(clean_text) < 50:
        return "申報內文缺乏實質文字或非純文字結構，請參閱原始文件。"

    api_url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    prompt = f"""
你是一位專業美股買方研究員。請審核以下 {ticker} 的 SEC {form} 官方申報內容，直接拆解實質工程、財務或營運事實。
強制遵守以下要求：
1. 嚴禁任何行銷修辭、空洞廢話或「公司表示、致力於」等無意義描述。
2. 條列式整理（最多 3 點）：
   - 【實質動作】：交代確切事項（如：涉及的 Item 項目、收購標的名稱、增資/發債具體總額、合約簽署對象與履行期限、高管姓名與異動職位）。若有具體金額或股數，必須直接列出數字。
   - 【財務與營運影響】：量化說明影響（如：稀釋比例、新增負債、營收貢獻或違約風險）。
3. 若公告僅為例行展示或無實質數字，請直接寫「例行程序申報，無重大實質財務數據變更」。

申報內文節錄：
{clean_text[:12000]}
"""

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "你是一位專注於 SEC 官方申報的買方研究員，僅以客觀數據與具體事實定調。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1
    }

    for attempt in range(3):
        try:
            res = requests.post(api_url, headers=headers, json=payload, timeout=25)
            data = res.json()

            if "error" in data:
                err_msg = data["error"].get("message", "未知錯誤")
                return f"OpenAI API 錯誤：{err_msg}"

            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            if attempt == 2:
                return f"連線異常：{str(e)}"
            time.sleep(3)

    return "AI 解析逾時，請點原始連結查看。"

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

                # 僅鎖定高價值重大表單
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
                        time.sleep(1)
        except Exception as e:
            print(f"處理 {ticker} 錯誤: {e}")

if __name__ == "__main__":
    check_sec_filings()
