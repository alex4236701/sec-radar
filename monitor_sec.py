import os
import re
import time
import json
import requests
from datetime import datetime, timezone, timedelta

# ==================== 環境變數與路徑設定 ====================
DISCORD_SEC_WEBHOOK = os.environ.get("DISCORD_SEC_WEBHOOK") or os.environ.get("DISCORD_NEWS_WEBHOOK")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
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

# 值得花費 Token 審核的「硬核財務與營運條款」
HIGH_IMPACT_8K_ITEMS = {
    "1.01", "1.02", "1.03", 
    "2.01", "2.02", "2.03", "2.04", "2.05", "2.06", 
    "3.01", "3.02", "3.03", 
    "4.01", "4.02"
}

# 8-K 包含之實質項目（含 8.01，但 8.01 走本地免 Token 通道）
SUBSTANTIVE_8K_ITEMS = HIGH_IMPACT_8K_ITEMS | {"8.01"}

# 官方 SEC Item 代碼之買方意義對照庫
ITEM_DEFINITIONS = {
    "1.01": "【重大合約】：簽署實質商業採購、策略合作或關鍵供貨協議",
    "1.02": "【合約終止】：重要重大商業合約遭到終止",
    "1.03": "【破產重組】：公司或重要子公司進入破產保護程序",
    "2.01": "【資本運作】：完成實質併購或重大業務部門/資產出售（Divestiture）",
    "2.02": "【業績公布】：公布最新季度財務業績、財測指引或法說數據",
    "2.03": "【新增債務】：承擔重大直接財務債務或表外融資安排",
    "2.04": "【違約加速】：發生債務違約、融資觸發加速清償條款",
    "2.05": "【重組裁員】：退出業務、啟動重組計畫或重大裁員資遣費用",
    "2.06": "【資產減損】：確認大額資產減損或商譽減記（Impairment）",
    "3.01": "【下市警告】：收到交易所不合規通知或下市處分警告",
    "3.02": "【股權銷售】：未註冊股權銷售（私募發行、增發或可轉債融資）",
    "3.03": "【權利變更】：股東權益實質重大變更（如啟動毒藥丸防衛）",
    "4.01": "【審計變更】：獨立會計師事務所閃辭或遭到更換（重大警訊）",
    "4.02": "【財報失效】：先前發布之官方財務報表不可信賴（即將重編假帳）",
    "8.01": "【自主重大】：公司自願公告之重大市場未公開事項（新聞稿存檔）"
}

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

def fetch_8k_text_snippet(cik, accession_num, primary_doc):
    """專為高價值 8-K 打造的輕量內文抓取器（只抓前 4000 字純文字）"""
    acc_clean = accession_num.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{primary_doc}"
    try:
        res = requests.get(url, headers=SEC_HEADERS, timeout=12)
        if res.status_code == 200:
            text = re.sub(r"<[^>]+>", " ", res.text)
            text = " ".join(text.split())
            return text[:4000]
    except Exception:
        pass
    return ""


# ==================== AI 深度解構核心 ====================
def analyze_8k_with_ai(ticker, items_str, doc_text):
    """交由 GPT 解析 8-K 內文中的金額、交易對手與實質條款"""
    if not OPENAI_API_KEY or not doc_text:
        return "• 【實質動作】：官方實質 8-K 重大條款申報。\n• 【調閱指引】：請點擊卡片連結查核官方合約原文。"

    api_url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    prompt = f"""
你是一位精準的美股買方分析師。標的【{ticker}】發布了 8-K 重大申報（涉及項目：{items_str}）。
以下是該份文件的官方原文節錄：
\"\"\"{doc_text}\"\"\"

請精確萃取並以繁體中文條列兩點（100 字以內，嚴禁任何模板廢話，直接給出硬核事實）：
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

    # 5. 外國 ADR 重大事件
    if clean_form.startswith("6-K"):
        return {
            "title": f"🌍 【外國 ADR 官方重大事件】：{ticker}",
            "color": 0x9B59B6,
            "tag": f"{form_type} (外國發行人重大備案)",
            "summary": (
                f"• 【核心動作】：外國掛牌實體 {ticker} 發布重大營運進展、資產處分、合約或本國重大備案。\n"
                f"• 【調閱指引】：ADR 重大事件在 SEC 無 Item 代碼，請立即點擊下方連結檢閱 6-K 附件原文。"
            )
        }

    # 6. 本土 8-K：Token 分流護城河架構
    if clean_form.startswith("8-K"):
        item_tokens = set(re.findall(r"\d+\.\d+", items_str))

        # A. 命中硬核條款：值得花費 Token，抓取內文讓 GPT 深度萃取金額與細節
        if item_tokens & HIGH_IMPACT_8K_ITEMS:
            print("      💎 [高價值 8-K] 命中硬核條款，調用 AI 審核內文...", flush=True)
            doc_text = fetch_8k_text_snippet(cik, accession_num, primary_doc)
            ai_analysis = analyze_8k_with_ai(ticker, items_str, doc_text)
            return {
                "title": f"⚡ 【實質 8-K 重大申報】：{ticker}",
                "color": 0x2ECC71,  # 實質綠
                "tag": f"{form_type} (核心項目: {items_str})",
                "summary": ai_analysis
            }

        # B. 僅有 8.01/7.01 等自主揭露：本地代碼硬解，零 Token 消耗
        matched_descs = [ITEM_DEFINITIONS[it] for it in item_tokens if it in ITEM_DEFINITIONS]
        if matched_descs:
            desc_lines = "\n".join(f"• {d}" for d in matched_descs)
        else:
            desc_lines = f"• 【自主揭露】：涉及備案代碼 {items_str or '8.01'}"

        return {
            "title": f"📑 【8-K 例行/自主揭露】：{ticker}",
            "color": 0x95A5A6,  # 中性灰
            "tag": f"{form_type} (自願揭露: {items_str or '8.01'})",
            "summary": f"{desc_lines}\n• 【調閱指引】：此申報未觸發硬核合約或融資條款，請點擊連結查閱原件。"
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
                print("     [本地過濾] 純人事變更/股東會議程，跳過", flush=True)
                save_sec_id(accession)
                sent_history.add(accession)
                continue

            if item_tokens and not (item_tokens & SUBSTANTIVE_8K_ITEMS):
                print("     [本地過濾] 非核心實質項目代碼，跳過", flush=True)
                save_sec_id(accession)
                sent_history.add(accession)
                continue

        # 解析情報（精準分流：高價值 8-K 才調用 AI）
        intel = parse_filing_intelligence(ticker, form, items, cik, accession, filing["primaryDocument"])
        if not intel:
            continue

        print(f"     🎯 [實質申報] 判定為 {form} 重大文件！推播至 Discord...", flush=True)
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
