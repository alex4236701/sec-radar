import os
import requests
from datetime import datetime, timedelta

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
    except Exception as e:
        print(f"發送 Telegram 失敗: {e}")

def check_sec_filings():
    if not os.path.exists("tickers.txt"):
        print("找不到 tickers.txt 檔案")
        return

    with open("tickers.txt", "r") as f:
        tickers = [line.strip().upper() for line in f if line.strip()]

    print(f"正在檢查以下代號的 SEC 最新動態: {tickers}")
    
    headers = {"User-Agent": "InstitutionalResearchUser investor@example.com"}
    
    for ticker in tickers:
        try:
            cik_url = f"https://www.sec.gov/files/company_tickers.json"
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
                
                # 檢查是否為 24 小時內的新申報
                if filing_date == (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d") or filing_date == datetime.now().strftime("%Y-%m-%d"):
                    if form in ["8-K", "10-Q", "10-K", "4", "144", "424B5", "424B7"]:
                        doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_number}/{primary_doc}"
                        msg = f"🚨 *SEC 即時雷達警報*\n\n公司：`{ticker}`\n表單類型：`{form}`\n申報日期：{filing_date}\n[點此查看 SEC 官方原始文件]({doc_url})"
                        send_telegram_message(msg)
        except Exception as e:
            print(f"處理代號 {ticker} 時發生錯誤: {e}")

if __name__ == "__main__":
    check_sec_filings()
