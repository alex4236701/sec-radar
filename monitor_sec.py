import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

# ==================== 環境變數與路徑設定 ====================
DISCORD_SEC_WEBHOOK = os.environ.get("DISCORD_SEC_WEBHOOK") or os.environ.get("DISCORD_NEWS_WEBHOOK")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
HISTORY_FILE = "sent_sec_log.txt"

TW_TZ = timezone(timedelta(hours=8))

# SEC 官方合規 User-Agent
SEC_HEADERS = {
    "User-Agent": "InstitutionalAlphaResearch/2.0 (yomin701@gmail.com)",
    "Accept-Encoding": "gzip, deflate"
}

# 核心標的 CIK 離線備援名冊（防禦 GitHub 雲端 IP 被 SEC 伺服器 403 阻擋）
CORE_FALLBACK_CIK = {
    "AAPL": "0000320193", "NVDA": "0001045810", "MSFT": "0000789019", "GOOGL": "0001652044",
    "AMZN": "0001018724", "TSLA": "0001318605", "QCOM": "0000804328", "AVGO": "0001730168",
    "INTC": "0000050863", "ARM": "0001973244", "TSM": "0001046179", "ASML": "0000937966",
    "BB": "0001070235", "TSEM": "0001178913", "CRWV": "0001769628", "PLTR": "0001321655",
    "IONQ": "0001824920", "ALAB": "0001936540", "GEV": "0001995446", "GNRC": "0001474735",
    "TTMI": "0001116942", "PL": "0001848124", "CRCL": "0001876042", "CSCO": "0000858877",
    "IBM": "0000051143", "TER": "0000097210", "WDC": "0000106040", "CGNX": "0000851205",
    "RDW": "0001818874", "RKLB": "0001819994", "FEIM": "0000039020", "UAMY": "0000101538"
}

TARGET_FORMS = {
    "8-K", "8-K/A",
    "6-K", "6-K/A",
    "10-Q", "10-Q/A", "NT 10-Q",
    "10-K", "10-K/A", "NT 10-K",
    "424B5", "424B7",
    "S-3", "S-3ASR", "S-3/A", "S-3ASR/A",
    "12b-25"
}

# 一級硬核條款：由 OpenAI GPT-4o-mini 深度解析
HIGH_IMPACT_8K_ITEMS = {
    "1.01", "1.02", "1.03", "1.05",
    "2.01", "2.02", "2.03", "2.04", "2.05", "2.06",
    "3.01", "3.02", "3.03",
    "4.01", "4.02",
    "5.01", "5.02"
}

# 二級自願條款：由 Gemini 3.6 Flash 解析（異常時自動無縫交由 OpenAI 救援）
SECONDARY_8K_ITEMS = {"7.01", "8.01"}

SUBSTANTIVE_8K_ITEMS = HIGH_IMPACT_8K_ITEMS | SECONDARY_8K_ITEMS | {"5.07"}

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
    mapping = dict(CORE_FALLBACK_CIK)
    url = "https://www.sec.gov/files/company_tickers.json"
    try:
        res = requests.get(url, headers=SEC_HEADERS, timeout=12)
        if res.status_code == 200:
            data = res.json()
            for item in data.values():
                mapping[item["ticker"].upper()] = str(item["cik_str"]).zfill(10)
            print(f"✅ 成功自 SEC 載入 {len(mapping)} 筆最新 CIK 對照表", flush=True)
            return mapping
    except Exception as e:
        print(f"⚠️ SEC 官方 CIK 下載受限 ({e})，啟用本地核心備援", flush=True)
    return mapping


def is_recent_filing(filing_date_str):
    try:
        filing_dt = datetime.strptime(filing_date_str, "%Y-%m-%d").date()
        today_utc = datetime.now(timezone.utc).date()
        return (today_utc - filing_dt).days <= MAX_LOOKBACK_DAYS
    except Exception:
        return False


def normalize_items_to_str(items_val):
    if not items_val:
        return ""
    if isinstance(items_val, list):
        return ",".join(str(x) for x in items_val if x)
    return str(items_val)


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
        for i in range(len(forms)):
            f_date = filing_dates[i]
            if not is_recent_filing(f_date):
                continue

            raw_item = items_list[i] if i < len(items_list) else ""
            results.append({
                "form": forms[i],
                "accessionNumber": accession_numbers[i],
                "filingDate": f_date,
                "primaryDocument": primary_docs[i],
                "items": normalize_items_to_str(raw_item)
            })
        return results
    except Exception:
        return []


def clean_html_to_text(html_content):
    text = re.sub(r"<style[\s\S]*?</style>", " ", html_content, flags=re.I)
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def fetch_doc_text_snippet(cik, accession_num, primary_doc, form_type=""):
    """
    抓取申報純文字：
    針對 6-K 或內容通常外掛在附件的表單，優先穿透 index.json 抓取真正的 EX-99 新聞稿
    """
    acc_clean = accession_num.replace("-", "")
    base_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/"

    # 先檢查是否有真實的 EX-99 新聞稿附件（特別是 6-K 或外殼 8-K）
    try:
        idx_res = requests.get(base_url + "index.json", headers=SEC_HEADERS, timeout=10)
        if idx_res.status_code == 200:
            items = idx_res.json().get("directory", {}).get("item", [])
            target_exhibit = None

            for it in items:
                fn = it.get("name", "").lower()
                desc = it.get("description", "").lower()
                doc_type = it.get("type", "").upper()

                # 鎖定 HTML 格式的 EX-99 / 新聞稿，避開圖片或 XML
                is_html = fn.endswith(".htm") or fn.endswith(".html")
                if is_html:
                    if "ex-99" in fn or "ex99" in fn or "99-1" in fn or "991" in fn:
                        target_exhibit = it.get("name")
                        break
                    if "99" in doc_type or "press release" in desc or "news release" in desc:
                        target_exhibit = it.get("name")
                        break

            if target_exhibit and target_exhibit != primary_doc:
                ex_res = requests.get(base_url + target_exhibit, headers=SEC_HEADERS, timeout=10)
                if ex_res.status_code == 200:
                    ex_text = clean_html_to_text(ex_res.text)
                    if len(ex_text) >= 200:
                        print(f"      ✅ [附件穿透成功] 鎖定新聞稿附件 {target_exhibit}（長度: {len(ex_text)} 字）！", flush=True)
                        return ex_text[:4500]
    except Exception:
        pass

    # 若無附件或穿透失敗，回退讀取主文件
    main_text = ""
    try:
        res = requests.get(base_url + primary_doc, headers=SEC_HEADERS, timeout=12)
        if res.status_code == 200:
            main_text = clean_html_to_text(res.text)
    except Exception:
        pass

    return main_text[:4500] if main_text else ""


# ==================== AI 解構核心 (OpenAI 一級) ====================
def analyze_primary_8k_with_openai(ticker, form_desc, doc_text):
    """一級硬核條款：由 GPT-4o-mini 精確萃取核心事實與實質財務影響"""
    if not OPENAI_API_KEY or not doc_text or len(doc_text.strip()) < 40:
        return (
            f"• **【核心要點】**：官方一級申報（{form_desc}），內文已完成存檔。\n"
            f"• **【財務影響】**：涉及核心合約或資本變動，請點擊標題查閱原文。"
        )

    api_url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    prompt = f"""
你是一位分毫不差的美股買方分析師。標的【{ticker}】發布了官方重大申報（涉及：{form_desc}）。
以下是該份文件的官方原文節錄：
\"\"\"{doc_text}\"\"\"

【嚴格禁令】：
1. 嚴禁機械套話！絕對禁止寫「對手方為...」、「交易/合約性質為...」、「合約性質為產品推出」等語句。
2. 嚴禁使用「提升市場地位、增強競爭力、帶來正面影響、後市可期、具戰略意義」等空洞公關廢話。

【輸出要求（請像專業研究員用自然大白話直接講重點）】：
• **【核心要點】**：一句話白話講清楚到底發生了什麼事（融資金額與利率、重大技術或商業合約、資產處分、或關鍵時程）。（繁體中文，40-65 字）
• **【財務影響】**：直擊實質財務衝擊（營收認列、毛利變化、負債壓力、或股本稀釋風險）。（繁體中文，40-65 字）
"""

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "你是一位硬核買方機構研究員，講求事實與數據，說話自然順暢，嚴格輸出指定格式。"},
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

    return (
        f"• **【核心要點】**：重大營運合約或債務變更（{form_desc}）。\n"
        f"• **【財務影響】**：請點擊連結查核官方合約與財務條款原文。"
    )


# ==================== AI 解構核心 (Gemini 二級 + 自動救援鏈) ====================
def analyze_secondary_with_gemini(ticker, filing_context, doc_text):
    """
    二級自願條款（8.01/7.01）與外國 6-K：
    優先由官方 Gemini 3.6 Flash 解析；若遭遇 429、404 或連線超時，無縫切換 OpenAI 救援！
    """
    if not doc_text or len(doc_text.strip()) < 40:
        return f"• **【核心要點】**：涉及備案 {filing_context}（內文詳見官方附件）。\n• **【財務影響】**：請點擊卡片連結查閱原文附件。"

    # 若未設定 Gemini Key，直接走 OpenAI
    if not GEMINI_API_KEY:
        print("      ℹ️ [分流轉接] 未設定 GEMINI_API_KEY，直接調用 OpenAI 進行深度解析...", flush=True)
        return analyze_primary_8k_with_openai(ticker, filing_context, doc_text)

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={GEMINI_API_KEY}"

    prompt = f"""
你是一位分毫不差的美股買方分析師。標的【{ticker}】提交了 SEC 官方申報（類型：{filing_context}）。
以下是該份文件的官方原文節錄：
\"\"\"{doc_text}\"\"\"

【嚴格禁令】：
1. 嚴禁機械套話！絕對禁止寫「對手方為...」、「交易/合約性質為...」、「合約性質為產品推出」等生硬套話。
2. 嚴禁使用「提升市場地位、增強競爭力、帶來正面影響、後市可期、具戰略意義」等空洞公關廢話。

【輸出要求（請像專業研究員用自然大白話直接講重點）】：
• **【核心要點】**：一句話白話講清楚到底發生了什麼事（融資金額與利率、重大技術或商業合作、資產收購處分、或法說主題）。（繁體中文，40-65 字）
• **【財務影響】**：直擊實質財務衝擊（營收貢獻、毛利變化、負債壓力、或股本稀釋風險）。（繁體中文，40-65 字）
"""

    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }],
        "generationConfig": {
            "maxOutputTokens": 800,
            "thinkingConfig": {
                "thinking_level": "MINIMAL"
            }
        }
    }

    gemini_success = False
    for attempt in range(2):
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=22)

            if res.status_code == 429:
                print("      ⏳ [Gemini 速率限制] 冷卻 8 秒後重試...", flush=True)
                time.sleep(8)
                continue

            if res.status_code != 200:
                print(f"      ⚠️ [Gemini 響應異常 HTTP {res.status_code}] 準備啟動 OpenAI 備援...", flush=True)
                break

            data = res.json()
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                real_text = "".join([p["text"] for p in parts if "text" in p and not p.get("thought", False)])

                if not real_text and parts and "text" in parts[0]:
                    real_text = parts[0]["text"]

                if real_text.strip():
                    return real_text.strip()
        except Exception as e:
            print(f"      ⚠️ [Gemini 連線異常: {e}] 準備啟動 OpenAI 備援...", flush=True)
            break

    # 救援防線：只要 Gemini 失敗，零秒切換 OpenAI，杜絕廢話罐頭
    print("      🛡️ [自動救援啟動] Gemini 連線未果，全面啟用 OpenAI GPT-4o-mini 完成解析！", flush=True)
    return analyze_primary_8k_with_openai(ticker, filing_context, doc_text)


# ==================== 表單情報分類分流 ====================
def parse_filing_intelligence(ticker, form_type, items_str, cik, accession_num, primary_doc):
    clean_form = form_type.upper().strip()

    # 1. 財報拖延黑天鵝
    if clean_form in ["12B-25", "NT 10-Q", "NT 10-K"]:
        return {
            "title": f"🚨 【重大黑天鵝】財報難產延期申報：{ticker}",
            "color": 0xC0392B,
            "tag": f"{form_type} (財報拖延 / 審計異常預警)",
            "summary": (
                f"• **【核心要點】**：{ticker} 正式向 SEC 申報無法如期繳交定期財報。\n"
                f"• **【財務影響】**：通常涉及內部控制缺失、審計障礙或潛在財務重編，留意二級市場跳空拋壓。"
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
                f"• **【核心要點】**：{ticker} 正式提交法定 {period_type}。\n"
                f"• **【財務影響】**：請點擊卡片連結調閱原文確認 GAAP 毛利、營收指引與自由現金流結構。"
            )
        }

    # 3. 增發融資與股東釋出
    if clean_form == "424B5":
        return {
            "title": f"⚠️ 【資本稀釋警報】增發定價/ATM 啟動：{ticker}",
            "color": 0xE67E22,
            "tag": "424B5 (公開發行補充說明書)",
            "summary": (
                f"• **【核心要點】**：{ticker} 提交 424B5 補充說明書，正式啟動現增、可轉債發行或 ATM 配售。\n"
                f"• **【財務影響】**：留意新股發行折價幅度，防範股本增加對 EPS 產生稀釋壓力。"
            )
        }

    if clean_form == "424B7":
        return {
            "title": f"⚠️ 【籌碼釋出警報】現有股東轉讓：{ticker}",
            "color": 0xE67E22,
            "tag": "424B7 (轉讓股權說明書)",
            "summary": (
                f"• **【核心要點】**：{ticker} 申報現有特定股東、創始團隊或機構之轉讓說明書。\n"
                f"• **【財務影響】**：涉及非公司端募集資金之持股釋出，留意二級市場短期承接力道。"
            )
        }

    # 4. 貨架登記
    if clean_form.startswith("S-3"):
        return {
            "title": f"📑 【融資水龍頭打開】貨架註冊生效：{ticker}",
            "color": 0xF39C12,
            "tag": f"{form_type} (貨架登記申請)",
            "summary": (
                f"• **【核心要點】**：{ticker} 申請綜合貨架登記，取得未來三年內隨時融資發行新股/債券之總額度。\n"
                f"• **【財務影響】**：市場通常視為未來資本稀釋前兆，小盤股多伴隨承壓反應。"
            )
        }

    # 5. 外國 ADR 6-K（啟用穿透與雙引擎解析）
    if clean_form.startswith("6-K"):
        print(f"      🌍 [外國 6-K] 檢測到 {ticker} ADR 申報，穿透附件並交由 AI 解析...", flush=True)
        doc_text = fetch_doc_text_snippet(cik, accession_num, primary_doc, form_type="6-K")
        gemini_analysis = analyze_secondary_with_gemini(ticker, f"{form_type} (外國重大備案)", doc_text)
        return {
            "title": f"🌍 【外國 ADR 官方重大事件】：{ticker}",
            "color": 0x9B59B6,
            "tag": f"{form_type} (外國發行人重大備案)",
            "summary": gemini_analysis
        }

    # 6. 本土 8-K：分流與穿透
    if clean_form.startswith("8-K"):
        item_tokens = set(re.findall(r"\d+\.\d+", items_str))

        # 分支 A：一級硬核條款 -> OpenAI GPT-4o-mini
        if item_tokens & HIGH_IMPACT_8K_ITEMS:
            print("      💎 [一級 8-K] 命中硬核條款，調用 OpenAI 審核內文...", flush=True)
            doc_text = fetch_doc_text_snippet(cik, accession_num, primary_doc, form_type="8-K")
            ai_analysis = analyze_primary_8k_with_openai(ticker, f"8-K 項目 {items_str}", doc_text)
            return {
                "title": f"⚡ 【實質 8-K 重大申報】：{ticker}",
                "color": 0x2ECC71,
                "tag": f"{form_type} (核心項目: {items_str})",
                "summary": ai_analysis
            }

        # 分支 B：二級自願揭露 (8.01/7.01) -> Gemini 優先，OpenAI 備援
        if item_tokens & SECONDARY_8K_ITEMS:
            print("      💡 [二級 8-K] 命中 8.01/7.01 自願揭露，穿透附件並提交 AI...", flush=True)
            doc_text = fetch_doc_text_snippet(cik, accession_num, primary_doc, form_type="8-K")
            gemini_analysis = analyze_secondary_with_gemini(ticker, f"8-K 項目 {items_str}", doc_text)
            return {
                "title": f"📑 【8-K 自願揭露解讀】：{ticker}",
                "color": 0x34495E,
                "tag": f"{form_type} (自願備案: {items_str})",
                "summary": gemini_analysis
            }

        # 分支 C：若 items 為空但為 8-K，依然調用 AI，絕不輕易套用罐頭文字
        doc_text = fetch_doc_text_snippet(cik, accession_num, primary_doc, form_type="8-K")
        if doc_text and len(doc_text) > 100:
            analysis = analyze_secondary_with_gemini(ticker, f"8-K 補充申報", doc_text)
            return {
                "title": f"📑 【8-K 官方申報解讀】：{ticker}",
                "color": 0x95A5A6,
                "tag": f"{form_type} (項目: {items_str or '補充披露'})",
                "summary": analysis
            }

        return {
            "title": f"📑 【8-K 例行申報】：{ticker}",
            "color": 0x95A5A6,
            "tag": f"{form_type} (項目: {items_str})",
            "summary": f"• **【核心要點】**：涉及例行備案代碼 {items_str}。\n• **【財務影響】**：請點擊標題查閱原文。"
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
                {"name": "💡 買方深度解讀", "value": intel["summary"][:1000], "inline": False}
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

        if accession in sent_history:
            continue

        print(f"   ↳ 審核表單: {form} | 案號: {accession} (日期: {filing_date}, 項目: {items or '無'})", flush=True)

        if form.startswith("8-K"):
            item_tokens = set(re.findall(r"\d+\.\d+", items))
            if item_tokens and not (item_tokens & SUBSTANTIVE_8K_ITEMS):
                print("      [本地過濾] 非核心實質項目代碼，跳過", flush=True)
                save_sec_id(accession)
                sent_history.add(accession)
                continue

        intel = parse_filing_intelligence(ticker, form, items, cik, accession, filing["primaryDocument"])
        if not intel:
            continue

        print(f"      🎯 [實質申報] 判定為 {form} 重大文件！推播至 Discord...", flush=True)
        send_sec_discord_embed(ticker, form, filing_date, accession, cik, filing["primaryDocument"], intel)

        save_sec_id(accession)
        sent_history.add(accession)
        time.sleep(1)


# ==================== 主程式進入點 ====================
def main():
    if not os.path.exists("tickers.txt"):
        print("❌ 錯誤：找不到 tickers.txt！", flush=True)
        return

    with open("tickers.txt", "r", encoding="utf-8-sig") as f:
        tickers = [line.strip().upper() for line in f if line.strip() and not line.strip().startswith("#")]

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
        time.sleep(0.12)

    print("==========================================", flush=True)
    print("✅ SEC 全量申報巡檢完成！", flush=True)
    print("==========================================", flush=True)


if __name__ == "__main__":
    main()
