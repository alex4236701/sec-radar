import os
import re
import time
import json
import requests
from datetime import datetime, timezone, timedelta

# ==================== 環境變數與路徑設定 ====================
DISCORD_SEC_WEBHOOK = os.environ.get("DISCORD_SEC_WEBHOOK") or os.environ.get("DISCORD_NEWS_WEBHOOK")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
HISTORY_FILE = "sent_sec_log.txt"  # 獨立歷史紀錄檔，避免與新聞相互污染

TW_TZ = timezone(timedelta(hours=8))

# SEC 合規 Header
SEC_HEADERS = {
    "User-Agent": "InstitutionalAlphaResearch/2.0 (compliance@alpharesearch.org)",
    "Accept-Encoding": "gzip, deflate"
}

# 監控目標表單
TARGET_FORMS = {"8-K", "10-Q", "10-K", "424B5", "424B7"}

# 8-K 實質項目代碼與例行排噪代碼
SUBSTANTIVE_8K_ITEMS = {"1.01", "2.01", "2.02", "3.02", "3.03", "8.01"}
IGNORE_ITEMS = {"5.02", "5.07"}


# ==================== 基礎工具函式 ====================
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
        # 取最新 8 筆申報進行過濾
        for i in range(min(8, len(forms))):
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


# ==================== DISCORD 推播與 AI 審核 ====================
def send_sec_discord_embed(ticker, form_type, filing_date, accession_num, cik, primary_doc, summary_text):
    if not DISCORD_SEC_WEBHOOK:
        return
    now_tw_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
    acc_clean = accession_num.replace("-", "")
    filing_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{primary_doc}"

    # 依表單性質定義標籤與色彩
    if form_type in ["424B5", "424B7"]:
        color = 0xE74C3C  # 警戒紅
        tag = "股權融資 / 現增稀釋"
    elif form_type in ["10-Q", "10-K"]:
        color = 0x3498DB  # 沉穩藍
        tag = "官方定期財報"
    else:
        color = 0x2ECC71  # 實質綠
        tag = "重大商業 / 營運合約"

    payload = {
        "username": "SEC EDGAR Automated Radar",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/2620/2620578.png",
        "embeds": [{
            "title": f"🚨 SEC 實質申報快訊：{ticker} ({form_type})",
            "url": filing_url,
            "color": color,
            "fields": [
                {"name": "📌 標的代號", "value": f"`{ticker}`", "inline": True},
                {"name": "📄 表單性質", "value": f"`{form_type} ({tag})`", "inline": True},
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
    # 若為 10-Q/10-K 或 424B，直接給出白話定調，節省 Token 並加速處理
    if form_type in ["10-Q", "10-K"]:
        return f"• 【實質動作】：{ticker} 提交官方正式定期財務報告 ({form_type})。\n• 【量化影響】：請查閱官方財務報表確認 GAAP 淨利潤、營收與現金流數據。"
    if form_type == "424B5":
        return f"• 【實質動作】：{ticker} 提交 424B5 公開發行補充說明書（涉及新股增發、公司債或 ATM 配售機制）。\n• 【量化影響】：留意二級市場流動性供給與每股盈餘 (EPS) 稀釋壓力。"
    if form_type == "424B7":
        return f"• 【實質動作】：{ticker} 提交 424B7 現有股東或特定投資人轉讓持股說明書。\n• 【量化影響】：涉及內部人或主要機構部位釋出，注意二級市場籌碼面拋壓。"

    if not OPENAI_API_KEY:
        return "• 【實質動作】：官方實質 8-K 申報。\n• 【量化影響】：請點擊連結調閱具體合約內容。"

    api_url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    prompt = f"""
你是一位嚴格把關的美股買方分析師。標的【{ticker}】向 SEC 申報了表單【{form_type}】，申報日期為【{filing_date}】，涉及項目代碼：【{items_str}】。

【絕對駁回規則（直接回傳單字 PASS）】：
1. 【例行人事變動】：純董事會席次調整、高階主管離職/新聘（Item 5.02），回傳 PASS。
2. 【股東會投票日程】：純年度股東會投票結果或法說會簡報公告（Item 5.07 / Item 7.01），回傳 PASS。
3. 【無實質財務影響】：僅為程序性補正、無具體營收數字或重大資本重組，回傳 PASS。

【必須通過之重大事件】：
- 重大客戶採購/合約簽署（Item 1.01）。
- 實質收購、重大資產出售（Item 2.01）。
- 季度業績發布、調升指引（Item 2.02）。
- 可轉債發行、現增、重大融資稀釋（Item 3.02 / 3.03）。

若符合重大事件，請以繁體中文條列兩點（80 字以內，禁止模板廢話）：
• 【實質動作】：交易對手、涉及金額、採購/併購標的或融資工具。
• 【量化影響】：對 {ticker} 之實質營收貢獻、資金流動性或股本稀釋壓力。
"""
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "你是一位分毫不差的買方研究員，無重大財務影響的申報一律回傳 PASS。"},
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


# ==================== 巡檢核心邏輯 ====================
def check_ticker_sec(ticker, cik, sent_history):
    filings = fetch_sec_filings(cik)
    if not filings:
        print("未獲取到申報")
        return

    # 先做白名單預篩
    valid_filings = [f for f in filings if f["form"] in TARGET_FORMS]
    if not valid_filings:
        print("無核心申報")
        return

    print(f"獲取到 {len(valid_filings)} 則核心申報")

    for filing in valid_filings:
        form = filing["form"]
        accession = filing["accessionNumber"]
        filing_date = filing["filingDate"]
        items = filing["items"]

        # 1. 唯一案號去重（歷史審查過的一律略過）
        if accession in sent_history:
            continue

        print(f"   ↳ 審核表單: {form} | 案號: {accession} (項目: {items or '無'})", flush=True)

        # 2. 8-K 本地排噪：人事 (5.02) 或股東會 (5.07)，本地直接封存
        if form == "8-K":
            item_tokens = set(re.findall(r"\d+\.\d+", items))
            if item_tokens and item_tokens.issubset(IGNORE_ITEMS):
                print("     [本地過濾] 純人事變更/股東會議程，跳過", flush=True)
                save_sec_id(accession)
                sent_history.add(accession)
                continue
            
            # 若不是目標實質項目，亦直接略過
            if item_tokens and not (item_tokens & SUBSTANTIVE_8K_ITEMS):
                print("     [本地過濾] 非核心實質項目代碼，跳過", flush=True)
                save_sec_id(accession)
                sent_history.add(accession)
                continue

        # 3. 提交 GPT 進行實質金流審核（或取得定期財報/現增之標準解讀）
        print(f"     ⚡ [命中重大申報：{form}] 進行買方實質審核...", flush=True)
        ai_res = audit_filing_with_ai(ticker, form, filing_date, items)

        # 記錄案號防重複
        save_sec_id(accession)
        sent_history.add(accession)

        if "PASS" in ai_res or len(ai_res) <= 10:
            print("     [AI裁定] PASS (無實質市場衝擊)", flush=True)
        else:
            print(f"     🎯 [放行] 判定為實質申報！推播至 Discord...", flush=True)
            send_sec_discord_embed(ticker, form, filing_date, accession, cik, filing["primaryDocument"], ai_res)
            time.sleep(1)


# ==================== 主程式進入點 ====================
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
        print(f"[{idx:03d}/{len(tickers):03d}] 檢索標的：{ticker:5s} ... ", end="", flush=True)
        if not cik:
            print("❌ 無法取得 CIK")
            continue
        check_ticker_sec(ticker, cik, sent_history)
        time.sleep(0.15)

    print("==========================================", flush=True)
    print("✅ SEC 全量申報巡檢完成！", flush=True)
    print("==========================================", flush=True)

if __name__ == "__main__":
    main()
