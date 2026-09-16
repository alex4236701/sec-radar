import os
import re
import time
import requests
import xml.etree.ElementTree as ET
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

def clean_html(raw_html):
    cleared = re.sub(r'<(style|script)[^>]*>.*?</\1>', '', raw_html, flags=re.DOTALL | re.IGNORECASE)
    cleared = re.sub(r'<[^>]+>', ' ', cleared)
    cleared = re.sub(r'&[a-z#0-9]+;', ' ', cleared)
    cleared = re.sub(r'\s+', ' ', cleared)
    return cleared.strip()

# Form 4 專用解析器：免 AI、精確抓取高管交易數字
def parse_form_4(xml_text):
    try:
        root = ET.fromstring(xml_text)
        rpt_owner = root.findtext(".//rptOwner/reportingOwnerId/rptOwnerName", default="內部人")
        officer_title = root.findtext(".//reportingOwnerRelationship/officerTitle", default="高管/董事")
        
        tx_nodes = root.findall(".//nonDerivativeTransaction")
        if not tx_nodes:
            return f"👤 {rpt_owner} ({officer_title})：無一般股普通買賣交易紀錄。"

        details = []
        for tx in tx_nodes[:3]:
            code = tx.findtext(".//transactionCoding/transactionCode", default="")
            shares = tx.findtext(".//transactionShares/value", default="0")
            price = tx.findtext(".//transactionPricePerShare/value", default="0")
            ad_code = tx.findtext(".//transactionAcquiredDisposedCode/value", default="")
            
            action = "買入 🟢" if ad_code == "A" else "賣出 🔴"
            try:
                shares_num = float(shares)
                price_num = float(price)
                total_val = shares_num * price_num
                val_str = f"，總計約 ${total_val:,.0f}" if total_val > 0 else ""
                details.append(f"• {action} {shares_num:,.0f} 股 (每股 ${price_num:,.2f}{val_str})")
            except Exception:
                details.append(f"• {action} {shares} 股")

        return f"👤 *內部人*：{rpt_owner} ({officer_title})\n" + "\n".join(details)
    except Exception:
        return "內部人交易明細解析失敗，請查閱原始文件。"

# 8-K / 10-Q 專用：只有重大長文才呼叫 AI
def summarize_with_ai(ticker, form, content_text):
    if not GEMINI_API_KEY:
        return "未設定 AI 金鑰，直接查看原始連結。"
    
    clean_text = clean_html(content_text)
    if len(clean_text) < 50:
        return "文件未含實質內文。"

    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    prompt = f"""
你是一位專業美股研究員。請閱讀以下 {ticker} 的 SEC {form} 重大公告，用繁體中文大白話輸出重點：
1. 核心實質動作（收購、訴訟、融資、重大人事變更或簽約）。
2. 對公司營運或財務的直接影響。
嚴禁行銷詞彙與無意義廢話，120 字以內直接講具體數據與進展。

申報內文節錄：
{clean_text[:6000]}
"""
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        res = requests.post(api_url, headers=headers, json=payload, timeout=25)
        data = res.json()
        if "error" in data:
            return f"AI 暫時繁忙：{data['error'].get('message', '未知錯誤')}"
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        return f"AI 連線異常：{str(e)}"

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
                    if form in ["8-K", "10-Q", "10-K", "4", "144"]:
                        doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_number}/{primary_doc}"
                        
                        summary_block = ""
                        # Form 4：走 Python 本地解析，不耗費任何 AI 額度
                        if form == "4":
                            # SEC Form 4 的原始檔通常為 XML 格式
                            xml_url = doc_url.replace(".html", ".xml")
                            try:
                                r = requests.get(xml_url, headers=headers, timeout=10)
                                if r.status_code == 200:
                                    summary_block = "📊 *內部人異動明細*：\n" + parse_form_4(r.text)
                                else:
                                    summary_block = "📊 *內部人異動*：申報 Form 4（點擊連結看明細）"
                            except Exception:
                                summary_block = "📊 *內部人異動*：申報 Form 4（點擊連結看明細）"
                        
                        # Form 144：預計賣股通知，直接標註免調用 AI
                        elif form == "144":
                            summary_block = "⚠️ *賣股意向通知*：內部人申報 Form 144，預告未來 3 個月內可能在公開市場出售持股。"
                        
                        # 8-K / 10-Q / 10-K：真正重大的事件才呼叫 AI
                        else:
                            try:
                                doc_res = requests.get(doc_url, headers=headers, timeout=10)
                                doc_text = doc_res.text
                            except Exception:
                                doc_text = ""
                            ai_res = summarize_with_ai(ticker, form, doc_text)
                            summary_block = f"💡 *AI 白話解讀*：\n{ai_res}"
                            time.sleep(2)
                        
                        msg = (
                            f"🚨 *SEC 雷達速報*\n\n"
                            f"公司：`{ticker}`\n"
                            f"表單：`{form}`\n"
                            f"日期：`{filing_date}`\n\n"
                            f"{summary_block}\n\n"
                            f"[點此查看 SEC 原始申報]({doc_url})"
                        )
                        send_telegram_message(msg)
        except Exception as e:
            print(f"處理 {ticker} 錯誤: {e}")

if __name__ == "__main__":
    check_sec_filings()
