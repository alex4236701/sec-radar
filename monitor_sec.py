import os
import re
import time
import json
import requests
from datetime import datetime, timezone, timedelta

DISCORD_SEC_WEBHOOK = os.environ.get("DISCORD_SEC_WEBHOOK") or os.environ.get("DISCORD_NEWS_WEBHOOK")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# 與新聞共用同一份歷史紀錄檔
HISTORY_FILE = "sent_news_log.txt"

# 台灣時區 (UTC+8)
TW_TZ = timezone(timedelta(hours=8))

# SEC 合規 Header
SEC_HEADERS = {
    "User-Agent": "InstitutionalAlphaResearch/2.0 (compliance@alpharesearch.org)",
    "Accept-Encoding": "gzip, deflate"
}

# 絕對忽略的 8-K 項目 (人事變動、年會投票等例行公事)
IGNORE_ITEMS = {"5.02", "5.07"}

def load_sec_history():
    if not os.path.exists(HISTORY_FILE):
        return set()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def save_sec_id(accession_num):
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(f"{accession_num}\n")

def get_cik_mapping():
    url = "https://www.sec.gov/files/company_tickers.json"
    try:
        res = requests.get(url, headers=SEC_HEADERS, timeout=15)
        if res.status_code == 200:
            data = res.json()
            mapping = {}
            for item in data.values():
                mapping[item["ticker"].upper()] = str(item["cik_str"]).zfill(10)
            return mapping
    except Exception as e:
        print(f"⚠️ CIK 映射下載失敗：{e}", flush=True)
    return {}

def fetch_sec_filings(cik):
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        res = requests.get(url, headers=SEC_HEADERS, timeout=15)
        if res.status_code != 200:
            return []
        data = res.json()
        recent = data.get("filings", {}).get("recent", {})
        
        forms = recent.get("form", [])
        accession_numbers = recent.get("accessionNumber", [])
        filing_dates = recent.get("filingDate", [])
        primary_docs = recent.get("primaryDocument", [])
        items_list = recent.get("items", [])

        results = []
        for i in range(min(5, len(forms))):
            results.append({
                "form": forms[i],
                "accessionNumber": accession_numbers[i],
                "filingDate": filing_dates[i],
                "primaryDocument": primary_docs[i],
                "items": items_list[i] if i < len(items_list) else ""
            })
        return results
    except Exception:
        return []

def send_sec_discord_embed(ticker, form_type, filing_date, accession_num, cik, primary_doc, summary_text):
    if not DISCORD_SEC_WEBHOOK:
        return
    now_tw_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
    acc_clean = accession_num.replace("-", "")
    filing_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{primary_doc}"

    payload = {
        "username": "SEC EDGAR Automated Radar",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/2620/2620578.png",
        "embeds": [{
            "title": f"🚨 SEC 重大申報速報：{ticker} ({form_type})",
            "url": filing_url,
            "color": 0xC0392B,
            "fields": [
                {"name": "📌 申報代號", "value": f"`{ticker}`", "inline": True},
                {"name": "📄 表單種類", "value": f"`{form_type}`", "inline": True},
                {"name": "📅 申報日期", "value": f"`{filing_date}`", "inline": True},
                {"name": "💡 買方核心解讀", "value": summary_text, "inline": False}
            ],
            "footer": {"text": f"SEC EDGAR • 案號: {accession_num} • 推播時間: {now_tw_str}"}
        }]
    }
    try:
        res = requests.post(DISCORD_SEC_WEBHOOK, json=payload, timeout=15)
        res.raise_for_status()
        print(f"     🎉 [SEC 推播成功] 已發送至 Discord！", flush=True)
    except Exception as e:
        print(f"     ❌ [SEC Discord 失敗] {e}", flush=True)

def audit_filing_with_ai(ticker, form_type, filing_date, items_str):
    if not OPENAI_API_KEY:
        return "PASS"
    api_url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    prompt = f"""
你是一位嚴格把關的美股買方分析師。標的【{ticker}】向 SEC 申報了表單【{form_type}】，申報日期為【{filing_date}】，涉及項目代碼：【{items_str}】。

【絕對駁回規則（直接回傳單字 PASS）】：
1. 【例行人事變動】：純董事會席次調整、高階主管離職/新聘（Item 5.02），回傳 PASS。
2. 【股東會投票日程】：純年度股東會投票結果或法說會簡報公告（Item 5.07 / Item 7.01），回傳 PASS。
3. 【無實質財務影響】：僅為法規程序性補正、無具體營收數字或重大資本重組，回傳 PASS。

【必須通過之重大事件】：
- 重大客戶採購/合約簽署（Item 1.01）。
- 實質收購、重大資產出售（Item 2.01）。
- 季度業績發布、調升指引（Item 2.02）。
- 可轉債發行、現增、重大融資稀釋（Item 3.02 / 3.03）。

若符合重大事件，請以繁體中文條列兩點（80 字以內，嚴禁模板廢話）：
• 【實質動作】：交易對手、涉及金額、採購/併購標的或融資具體工具。
• 【量化影響】：對 {ticker} 之實質營收貢獻、資金流動性或股本稀釋壓力。
"""
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "你是一位分毫不差的買方研究員，無重大財務金流影響的申報一律回傳 PASS。"},
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

def check_ticker_sec(ticker, cik, sent_history):
    filings = fetch_sec_filings(cik)
    if not filings:
        print("未獲取到近期申報", flush=True)
        return

    for filing in filings:
        form = filing["form"]
        accession = filing["accessionNumber"]
        filing_date = filing["filingDate"]
        items = filing["items"]

        # 只看 8-K 重大申報
        if form != "8-K":
            continue

        # 1. 唯一案號去重（歷史審查過的一律略過）
        if accession in sent_history:
            continue

        print(f"\n   ↳ 審核 8-K 申報 案號: {accession} (項目: {items})", flush=True)

        # 2. 本地項目過濾：人事 (5.02) 或股東會 (5.07)，本地直接封存
        item_tokens = set(re.findall(r"\d+\.\d+", items))
        if item_tokens and item_tokens.issubset(IGNORE_ITEMS):
            print("     [本地過濾] 僅為人事變動/股東會例行公事，略過", flush=True)
            save_sec_id(accession)
            sent_history.add(accession)
            continue

        # 3. 提交 GPT 進行實質金流審核
        print("     ⚡ [命中重大 8-K 申報] 提交 GPT 進行實質金流審核...", flush=True)
        ai_res = audit_filing_with_ai(ticker, form, filing_date, items)

        # 記錄案號
        save_sec_id(accession)
        sent_history.add(accession)

        if "PASS" in ai_res or len(ai_res) <= 10:
            print("     [AI裁定] PASS (非重大營收/金流事件)", flush=True)
        else:
            print(f"     🎯 [AI放行] 判定為重大財務事件！推播至 Discord...", flush=True)
            send_sec_discord_embed(ticker, form, filing_date, accession, cik, filing["primaryDocument"], ai_res)
            time.sleep(1)

def main():
    if not os.path.exists("tickers.txt"):
        print("❌ 錯誤：找不到 tickers.txt！", flush=True)
        return

    with open("tickers.txt", "r") as f:
        tickers = [line.strip().upper() for line in f if line.strip()]

    sent_history = load_sec_history()
    print("==========================================", flush=True)
    print(f"🏛️ 啟動 SEC EDGAR 官方申報巡檢，共計：{len(tickers)} 檔標的", flush=True)
    print(f"🕒 當前台灣時間：{datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"📦 已記錄歷史申報案號：{len(sent_history)} 條", flush=True)
    print("==========================================", flush=True)

    cik_map = get_cik_mapping()

    for idx, ticker in enumerate(tickers, start=1):
        cik = cik_map.get(ticker)
        if not cik:
            continue
        print(f"[{idx:03d}/{len(tickers):03d}] 檢索標的：{ticker:5s} ... ", end="", flush=True)
        check_ticker_sec(ticker, cik, sent_history)
        time.sleep(0.2)

    print("==========================================", flush=True)
    print("✅ SEC 全量申報巡檢完成！", flush=True)
    print("==========================================", flush=True)

if __name__ == "__main__":
    main()
