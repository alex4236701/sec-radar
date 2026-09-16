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
        "username": "SEC Radar Bot",
        "avatar_url": "https://www.sec.gov/themes/custom/uswds_sec/assets/img/sec-logo.svg",
        "embeds": [{
            "title": f"🚨 SEC 重大申報：{ticker} ({form})",
            "url": doc_url,
            "color": color,
            "fields": [
                {"name": "📌 標的", "value": f"`{ticker}`", "inline": True},
                {"name": "📄 表單", "value": f"`{form}`", "inline": True},
                {"name": "💡 AI 實質解讀", "value": summary, "inline": False}
            ],
            "footer": {"text": "SEC EDGAR Automated Radar"},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }]
    }
    try:
        res = requests.post(DISCORD_SEC_WEBHOOK, json=payload, timeout=15)
        res.raise_for_status()
    except Exception as e:
        print(f"SEC Discord 發送失敗: {e}")

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
    if len(clean_text) < 50:
        return "申報內文缺乏實質文字或非純文字結構，請參閱原始文件。"

    api_url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    prompt = f"""
你是一位專業美股買方研究員。請審核以下 {ticker} 的 SEC {form} 官方申報內容，直接拆解實質事實：
1. 嚴禁行銷詞彙與無意義客套。
2. 條列整理（最多 3 點）：
   - 【實質動作】：Item 項目、收購標的、增資/發債總額、合約簽署對象、高管異動。有數字必須寫出。
   - 【財務營運影響】：稀釋比例、新增負債、違約風險。
3. 例行程序無重大數字請直接寫「例行程序申報，無重大實質財務數據變更」。

內容：
{clean_text[:12000]}
"""
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "你是一位專注於 SEC 官方申報的買方研究員。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1
    }
    for attempt in range(3):
        try:
            res = requests.post(api_url, headers=headers, json=payload, timeout=25)
            data = res.json()
            if "error" in data:
                return f"OpenAI 錯誤: {data['error'].get('message')}"
            return data["choices"][0]["message"]["content"].strip()
        except Exception:
            time.sleep(2)
    return "AI 解析逾時"

def main():
    if not os.path.exists("tickers.txt"):
        return
    with open("tickers.txt", "r") as f:
        tickers = [line.strip().upper() for line in f if line.strip()]

    headers = {"User-Agent": "InstitutionalResearchUser investor@example.com"}
    try:
        cik_res = requests.get("https://www.sec.gov/files/company_tickers.json", headers=headers).json()
        cik_map = {val["ticker"]: str(val["cik_str"]).zfill(10) for val in cik_res.values()}
    except Exception as e:
        print(f"取得 CIK 清單失敗: {e}")
        return

    color_map = {"8-K": 0xE74C3C, "424B5": 0xE67E22, "424B7": 0x9B59B6, "10-Q": 0x3498DB, "10-K": 0x2ECC71}

    for ticker in tickers:
        cik = cik_map.get(ticker)
        if not cik:
            continue
        try:
            sub_res = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=headers).json()
            recent = sub_res["filings"]["recent"]
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            today = datetime.now().strftime("%Y-%m-%d")

            for i in range(min(4, len(recent["form"]))):
                form = recent["form"][i]
                filing_date = recent["filingDate"][i]
                if filing_date in [yesterday, today] and form in ["8-K", "10-Q", "10-K", "424B5", "424B7"]:
                    acc = recent["accessionNumber"][i].replace("-", "")
                    doc = recent["primaryDocument"][i]
                    doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}"
                    try:
                        doc_text = requests.get(doc_url, headers=headers, timeout=10).text
                    except Exception:
                        doc_text = ""
                    summary = summarize_with_ai(ticker, form, doc_text)
                    send_discord_embed(ticker, form, summary, doc_url, color_map.get(form, 0xE74C3C))
                    time.sleep(1)
        except Exception as e:
            print(f"處理 {ticker} 錯誤: {e}")

if __name__ == "__main__":
    main()
