import os
import re
import time
import json
import requests
from datetime import datetime, timezone, timedelta

# ==================== 環境變數與路徑設定 ====================
DISCORD_SEC_WEBHOOK = os.environ.get("DISCORD_SEC_WEBHOOK") or os.environ.get("DISCORD_NEWS_WEBHOOK")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
HISTORY_FILE = "sent_sec_log.txt"

TW_TZ = timezone(timedelta(hours=8))

# SEC 官方合規 User-Agent（包含真實聯絡管道）
SEC_HEADERS = {
    "User-Agent": "InstitutionalAlphaResearch/2.0 (yomin701@gmail.com)",
    "Accept-Encoding": "gzip, deflate"
}

# 專業買方雷達核心表單全覆蓋
TARGET_FORMS = {
    "8-K", "8-K/A",
    "6-K", "6-K/A",
    "10-Q", "10-Q/A", "NT 10-Q",
    "10-K", "10-K/A", "NT 10-K",
    "424B5", "424B7",
    "S-3", "S-3ASR", "S-3/A", "S-3ASR/A",
    "12b-25"
}

# 本地直接封存之無意義項目（人事、股東會）
IGNORE_ITEMS = {"5.02", "5.07"}

# 一級硬核條款：呼叫 OpenAI GPT-4o-mini
HIGH_IMPACT_8K_ITEMS = {
    "1.01", "1.02", "1.03", 
    "2.01", "2.02", "2.03", "2.04", "2.05", "2.06", 
    "3.01", "3.02", "3.03", 
    "4.01", "4.02"
}

# 二級次要條款：呼叫免費 Gemini 3.6 Flash
SECONDARY_8K_ITEMS = {"7.01", "8.01"}

# 允許進入處理流程的 8-K 項目聯集
SUBSTANTIVE_8K_ITEMS = HIGH_IMPACT_8K_ITEMS | SECONDARY_8K_ITEMS

# 最大申報追溯天數（徹底阻絕舊文件）
MAX_LOOKBACK_DAYS = 3


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

def is_recent_filing(filing_date_str):
    try:
        filing_dt = datetime.strptime(filing_date_str, "%Y-%m-%d").date()
        today_utc = datetime.now(timezone.utc).date()
        return (today_utc - filing_dt).days <= MAX_LOOKBACK_DAYS
    except Exception:
        return False

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
        for i in range(min(15, len(forms))):
            f_date = filing_dates[i]
            if not is_recent_filing(f_date):
                continue

            results.append({
                "form": forms[i],
                "accessionNumber": accession_numbers[i],
                "filingDate": f_date,
                "primaryDocument": primary_docs[i],
                "items": items_list[i] if i < len(items_list) else ""
            })
        return results
    except Exception:
        return []

def clean_html_to_text(html_content):
    text = re.sub(r"<style[\s\S]*?</style>", " ", html_content, flags=re.I)
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())

def fetch_doc_text_snippet(cik, accession_num, primary_doc):
    """抓取 8-K/6-K 內文純文字（前 4000 字），若主檔為空殼則自動嘗試穿透 Exhibit 99.1 附件"""
    acc_clean = accession_num.replace("-", "")
    base_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/"
    
    # 1. 抓取主申報文件
    main_doc_url = base_url + primary_doc
    main_text = ""
    try:
        res = requests.get(main_doc_url, headers=SEC_HEADERS, timeout=12)
        if res.status_code == 200:
            main_text = clean_html_to_text(res.text)
    except Exception:
        pass

    # 若主文件字數充足，直接回傳
    if len(main_text) >= 200:
        return main_text[:4000]

    # 2. 主文件過短（空殼備案），嘗試抓取常見的新聞稿 Exhibit 附件
    print("      ℹ️ [內文穿透] 主檔過短，嘗試搜尋 Exhibit 99.1 附件...", flush=True)
    potential_exhibits = [
        "ex99-1.htm", "ex-99.1.htm", "ex991.htm", "ex99_1.htm", 
        "ex-99-1.htm", "d991.htm", "exhibit99-1.htm"
    ]
    for ex_doc in potential_exhibits:
        try:
            ex_url = base_url + ex_doc
            res_ex = requests.get(ex_url, headers=SEC_HEADERS, timeout=8)
            if res_ex.status_code == 200:
                ex_text = clean_html_to_text(res_ex.text)
                if len(ex_text) >= 200:
                    print(f"      ✅ [附件命中] 成功取得附件 {ex_doc} 內文！", flush=True)
                    return ex_text[:4000]
        except Exception:
            continue

    return main_text[:4000] if main_text else ""


# ==================== AI 深度解構核心 ====================
def analyze_primary_8k_with_openai(ticker, items_str, doc_text):
    """一級硬核條款：由 GPT-4o-mini 精確萃取金額與法律實質"""
    if not OPENAI_API_KEY or not doc_text:
        return "• 【實質動作】：官方一級 8-K 重大條款申報。\n• 【調閱指引】：請點擊卡片連結查核官方合約原文。"

    api_url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    prompt = f"""
你是一位精準的美股買方分析師。標的【{ticker}】發布了一級 8-K 重大申報（涉及項目：{items_str}）。
以下是該份文件的官方原文節錄：
\"\"\"{doc_text}\"\"\"

請精確萃取並以繁體中文條列兩點（120 字以內，嚴禁任何模板廢話，直接給出硬核事實）：
• 【核心動作】：交易對手是誰、合約/融資具體金額（百萬/億美元）、收購或處分標的、年利率或關鍵時程。
• 【買方評估】：對 {ticker} 之營收貢獻、資金流動性、負債壓力或股本稀釋衝擊。
"""
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "你是一位分毫不差的買方研究員，專注萃取 8-K 的具體金額、交易對手與條款數值。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1
    }

    for _ in range(2):
        try:
            res = requests.post(api_url, headers=headers, json=payload, timeout=20)
            data = res.json()
            if "choices" in data:
                return data["choices"][0]["message"]["content"].strip()
        except Exception:
            time.sleep(1)
            
    return "• 【核心動作】：重大營運合約或債務變更。\n• 【調閱指引】：請點擊連結查核具體金額細節。"


def analyze_secondary_with_gemini(ticker, filing_context, doc_text):
    """二級自願條款（8.01/7.01）與外國 6-K：由免費 Gemini 3.6 Flash 解析業務重點與潛在財務影響"""
    if not GEMINI_API_KEY:
        print("      ⚠️ [Gemini 略過] 系統未讀取到 GEMINI_API_KEY 環境變數", flush=True)
        return f"• 【自主揭露】：涉及備案 {filing_context}。\n• 【調閱指引】：請點擊連結查閱官方原件。"
        
    if not doc_text or len(doc_text.strip()) < 30:
        print("      ⚠️ [Gemini 略過] 內文過短或純屬索引目錄", flush=True)
        return f"• 【自主揭露】：涉及備案 {filing_context}（內文詳見官方附件）。\n• 【調閱指引】：請點擊卡片連結查閱原文附件。"

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={GEMINI_API_KEY}"
    
    prompt = f"""
請扮演美股買方分析師，閱讀以下標的【{ticker}】的 SEC 申報（類別：{filing_context}）：
\"\"\"{doc_text}\"\"\"

請精確萃取並以「繁體中文」條列出以下兩點（直接輸出，嚴禁任何開場白或推理自述）：
• 【核心要點】：具體發生了什麼事（如債券規模與利率、合約金額、併購投票日程或法說主題）。
• 【財務影響】：對公司資本結構、自由現金流或營運的具體影響（若僅為公關稿請寫無實質財務影響）。
"""

    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 800,
            # 關鍵修正：關閉思考過程輸出，強制直出結論
            "thinkingConfig": {
                "thinkingBudget": 0
            }
        }
    }

    for attempt in range(2):
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=20)
            if res.status_code != 200:
                print(f"      ❌ [Gemini API 報錯] HTTP {res.status_code}: {res.text}", flush=True)
                time.sleep(1)
                continue
                
            data = res.json()
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                # 排除思考標籤，只抓取真正的回傳文字
                real_text = ""
                for part in parts:
                    if "thought" not in part and "text" in part:
                        real_text += part["text"]
                
                # 如果沒有特別區分欄位，就拿第一個 text
                if not real_text and parts and "text" in parts[0]:
                    real_text = parts[0]["text"]

                if real_text.strip():
                    return real_text.strip()
            else:
                print(f"      ⚠️ [Gemini 回傳空結構] {data}", flush=True)
        except Exception as e:
            print(f"      ❌ [Gemini 連線異常] {e}", flush=True)
            time.sleep(1)

    return f"• 【自主揭露】：涉及項目 {filing_context}，內容已存檔。\n• 【調閱指引】：請點擊卡片標題查閱原文。"


def parse_filing_intelligence(ticker, form_type, items_str, cik, accession_num, primary_doc):
    clean_form = form_type.upper().strip()

    # 1. 財報拖延黑天鵝
    if clean_form in ["12B-25", "NT 10-Q", "NT 10-K"]:
        return {
            "title": f"🚨 【重大黑天鵝】財報難產延期申報：{ticker}",
            "color": 0xC0392B,
            "tag": f"{form_type} (財報拖延 / 審計異常預警)",
            "summary": (
                f"• 【核心警報】：{ticker} 正式向 SEC 申報無法如期繳交定期財報。\n"
                f"• 【實質風險】：通常涉及內部控制缺失、審計障礙或潛在財務重編，留意二級市場跳空拋壓。"
            )
        }

    # 2. 定期財報
    if clean_form.startswith("10-Q") or clean_form.startswith("10-K"):
        period_type = "季度報告" if "10-Q" in clean_form else "年度報告"
        if "/A" in clean_form:
            period_type += " 修正案"
        return {
            "title": f"📊 【官方定期財報】定期申報就緒：{ticker}",
            "color": 0x3498DB,
            "tag": f"{form_type} ({period_type})",
            "summary": (
                f"• 【核心動作】：{ticker} 正式提交法定 {period_type}。\n"
                f"• 【查核要點】：請點擊卡片連結調閱原文確認 GAAP 毛利、營收指引與自由現金流結構。"
            )
        }

    # 3. 增發融資與股東釋出
    if clean_form == "424B5":
        return {
            "title": f"⚠️ 【資本稀釋警報】增發定價/ATM 啟動：{ticker}",
            "color": 0xE67E22,
            "tag": "424B5 (公開發行補充說明書)",
            "summary": (
                f"• 【核心動作】：{ticker} 提交 424B5 補充說明書，正式啟動現增、可轉債發行或 ATM 配售。\n"
                f"• 【市場衝擊】：留意新股發行折價幅度，防範股本增加對 EPS 產生稀釋壓力。"
            )
        }

    if clean_form == "424B7":
        return {
            "title": f"⚠️ 【籌碼釋出警報】現有股東轉讓：{ticker}",
            "color": 0xE67E22,
            "tag": "424B7 (轉讓股權說明書)",
            "summary": (
                f"• 【核心動作】：{ticker} 申報現有特定股東、創始團隊或機構之轉讓說明書。\n"
                f"• 【籌碼影響】：涉及非公司端募集資金之持股釋出，留意二級市場短期承接力道。"
            )
        }

    # 4. 貨架登記
    if clean_form.startswith("S-3"):
        return {
            "title": f"📑 【融資水龍頭打開】貨架註冊生效：{ticker}",
            "color": 0xF39C12,
            "tag": f"{form_type} (貨架登記申請)",
            "summary": (
                f"• 【核心動作】：{ticker} 申請綜合貨架登記，取得未來三年內隨時融資發行新股/債券之總額度。\n"
                f"• 【估值影響】：市場通常視為未來資本稀釋前兆，小盤股多伴隨承壓反應。"
            )
        }

    # 5. 外國 ADR 重大事件（直接調用免費 Gemini 3.6 Flash 自動摘要）
    if clean_form.startswith("6-K"):
        print(f"      🌍 [外國 6-K] 檢測到 {ticker} ADR 申報，調用免費 Gemini 3.6 Flash 摘要...", flush=True)
        doc_text = fetch_doc_text_snippet(cik, accession_num, primary_doc)
        gemini_analysis = analyze_secondary_with_gemini(ticker, f"{form_type} (外國重大備案)", doc_text)
        return {
            "title": f"🌍 【外國 ADR 官方重大事件】：{ticker}",
            "color": 0x9B59B6,  # 專屬紫
            "tag": f"{form_type} (外國發行人重大備案)",
            "summary": gemini_analysis
        }

    # 6. 本土 8-K：分流智慧解構架構
    if clean_form.startswith("8-K"):
        item_tokens = set(re.findall(r"\d+\.\d+", items_str))

        # 分支 A：命中一級硬核條款 -> 付費 GPT-4o-mini 深度萃取
        if item_tokens & HIGH_IMPACT_8K_ITEMS:
            print("      💎 [高價值 8-K] 命中一級硬核條款，調用 OpenAI 審核內文...", flush=True)
            doc_text = fetch_doc_text_snippet(cik, accession_num, primary_doc)
            ai_analysis = analyze_primary_8k_with_openai(ticker, items_str, doc_text)
            return {
                "title": f"⚡ 【實質 8-K 重大申報】：{ticker}",
                "color": 0x2ECC71,  # 實質綠
                "tag": f"{form_type} (核心項目: {items_str})",
                "summary": ai_analysis
            }

        # 分支 B：二級自願揭露（8.01/7.01） -> 免費 Gemini 3.6 Flash 提取重點與財務影響
        if item_tokens & SECONDARY_8K_ITEMS:
            print("      💡 [二級 8-K] 命中 8.01/7.01 自願揭露，調用免費 Gemini 3.6 Flash 摘要...", flush=True)
            doc_text = fetch_doc_text_snippet(cik, accession_num, primary_doc)
            gemini_analysis = analyze_secondary_with_gemini(ticker, f"8-K 項目 {items_str}", doc_text)
            return {
                "title": f"📑 【8-K 自願揭露解讀】：{ticker}",
                "color": 0x34495E,  # 深藍灰
                "tag": f"{form_type} (自願備案: {items_str})",
                "summary": gemini_analysis
            }

        # 分支 C：其他非核心雜項代碼
        return {
            "title": f"📑 【8-K 例行申報】：{ticker}",
            "color": 0x95A5A6,
            "tag": f"{form_type} (項目: {items_str})",
            "summary": f"• 【例行備案】：涉及項目代碼 {items_str}。\n• 【調閱指引】：請點擊標題查閱原文。"
        }

    return None


# ==================== DISCORD 推播 ====================
def send_sec_discord_embed(ticker, form_type, filing_date, accession_num, cik, primary_doc, intel):
    if not DISCORD_SEC_WEBHOOK:
        return
    now_tw_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
    acc_clean = accession_num.replace("-", "")
    filing_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{primary_doc}"

    payload = {
        "username": "SEC EDGAR Intelligence",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/2620/2620578.png",
        "embeds": [{
            "title": intel["title"],
            "url": filing_url,
            "color": intel["color"],
            "fields": [
                {"name": "📌 標的代號", "value": f"`{ticker}`", "inline": True},
                {"name": "📄 官方表單", "value": f"`{intel['tag']}`", "inline": True},
                {"name": "📅 申報日期", "value": f"`{filing_date}`", "inline": True},
                {"name": "💡 買方核心解讀", "value": intel["summary"][:1000], "inline": False}
            ],
            "footer": {"text": f"SEC EDGAR 原文直達 • 案號: {accession_num} • 推播: {now_tw_str}"}
        }]
    }
    try:
        res = requests.post(DISCORD_SEC_WEBHOOK, json=payload, timeout=15)
        res.raise_for_status()
        print("      🎉 [SEC 推播成功] 已發送至 Discord！", flush=True)
    except Exception as e:
        print(f"      ❌ [SEC Discord 失敗] {e}", flush=True)


# ==================== 巡檢核心邏輯 ====================
def check_ticker_sec(ticker, cik, sent_history):
    filings = fetch_sec_filings(cik)
    if not filings:
        print("近期無核心申報")
        return

    valid_filings = [f for f in filings if f["form"] in TARGET_FORMS]
    if not valid_filings:
        print("近期無目標表單")
        return

    print(f"獲取到 {len(valid_filings)} 則近期申報")

    for filing in valid_filings:
        form = filing["form"]
        accession = filing["accessionNumber"]
        filing_date = filing["filingDate"]
        items = filing["items"]

        # 唯一案號去重（歷史已發過的一律略過）
        if accession in sent_history:
            continue

        print(f"   ↳ 審核表單: {form} | 案號: {accession} (日期: {filing_date}, 項目: {items or '無'})", flush=True)

        # 8-K 本地排噪：人事或股東會直接跳過（6-K 不受此限）
        if form.startswith("8-K"):
            item_tokens = set(re.findall(r"\d+\.\d+", items))
            if item_tokens and item_tokens.issubset(IGNORE_ITEMS):
                print("      [本地過濾] 純人事變更/股東會議程，跳過", flush=True)
                save_sec_id(accession)
                sent_history.add(accession)
                continue

            if item_tokens and not (item_tokens & SUBSTANTIVE_8K_ITEMS):
                print("      [本地過濾] 非核心實質項目代碼，跳過", flush=True)
                save_sec_id(accession)
                sent_history.add(accession)
                continue

        # 解析情報（精準分流）
        intel = parse_filing_intelligence(ticker, form, items, cik, accession, filing["primaryDocument"])
        if not intel:
            continue

        print(f"      🎯 [實質申報] 判定為 {form} 重大文件！推播至 Discord...", flush=True)
        send_sec_discord_embed(ticker, form, filing_date, accession, cik, filing["primaryDocument"], intel)
        
        # 紀錄已發案號
        save_sec_id(accession)
        sent_history.add(accession)
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
    print(f"🛡️ 嚴格日期防禦機制：僅放行最近 {MAX_LOOKBACK_DAYS} 天以內之最新申報", flush=True)
    print("==========================================", flush=True)

    cik_map = get_cik_mapping()

    for idx, ticker in enumerate(tickers, start=1):
        cik = cik_map.get(ticker)
        print(f"[{idx:03d}/{len(tickers):03d}] 檢索標的：{ticker:5s} ... ", end="", flush=True)
        if not cik:
            print("❌ 無法取得 CIK")
            continue
        check_ticker_sec(ticker, cik, sent_history)
        
        # 嚴格遵循 SEC 官方每秒 10 次請求上限，安全休眠
        time.sleep(0.12)

    print("==========================================", flush=True)
    print("✅ SEC 全量申報巡檢完成！", flush=True)
    print("==========================================", flush=True)

if __name__ == "__main__":
    main()
