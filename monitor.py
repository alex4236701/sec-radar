import os
import requests
from datetime import datetime, timedelta

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload)
        res.raise_for_status()
    except Exception as e:
        print(f"發送 Telegram 失敗: {e}")

def summarize_with_ai(ticker, form, content_text):
    if not GEMINI_API_KEY:
        return "未設定 AI 金鑰，直接查看原始連結。"
    
    # 依 API 要求切換為 gemini-3.6-flash
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    prompt = f"""
你是一位專業美股研究員。請閱讀以下 {ticker} 的 SEC {form} 申報部分內容，用台灣日常大白話繁體中文輸出重點：
1. 核心實質動作（例如：內部人賣出幾股、增資總額、簽訂重要合約）。
2. 對公司營運或財務的直接影響。
嚴禁行銷詞彙與無意義廢話，120 字以內直接講實質數據與動作。

申報內文節錄：
{content_text[:3500]}
"""
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        res = requests.post(api_url, headers=headers, json=payload, timeout=25)
        data = res.json()
        if "error" in data:
            err_msg = data['error'].get('message', '未知錯誤')
            print(f"Gemini API 回傳錯誤: {err_msg}")
            return f"API 錯誤：{err_msg}"
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"AI 解析連線異常: {e}")
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
                
                if filing_date in [yesterday, today]:
                    if form in ["8-K", "10-Q", "10-K", "4", "144", "424B5", "424B7"]:
                        doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_number}/{primary_doc}"
                        
                        doc_text = ""
                        try:
                            doc_res = requests.get(doc_url, headers=headers, timeout=10)
                            doc_text = doc_res.text
                        except Exception:
                            doc_text = "無法下載內文"
                        
                        ai_summary = summarize_with_ai(ticker, form, doc_text)
                        
                        msg = (
                            f"🚨 *SEC 雷達速報*\n\n"
                            f"公司：`{ticker}`\n"
                            f"表單：`{form}`\n"
                            f"日期：`{filing_date}`\n\n"
                            f"💡 *AI 白話解讀*：\n{ai_summary}\n\n"
                            f"[點此查看 SEC 原始申報]({doc_url})"
                        )
                        send_telegram_message(msg)
        except Exception as e:
            print(f"處理 {ticker} 錯誤: {e}")

if __name__ == "__main__":
    check_sec_filings()
