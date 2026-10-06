import hashlib
import json
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

# ==================== 環境變數與路徑設定 ====================
DISCORD_NEWS_WEBHOOK = os.environ.get("DISCORD_NEWS_WEBHOOK")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
# SEC 要求真實聯絡資訊，例如 "sec-radar your_email@example.com"，請放在 GitHub Secrets
SEC_USER_AGENT = (os.environ.get("SEC_USER_AGENT") or "").strip()

HISTORY_FILE = "sent_news_log.txt"
WEEKEND_RUN_LOG = "weekend_last_run.txt"

TW_TZ = timezone(timedelta(hours=8))
UTC_TZ = timezone.utc

# ==================== AI 模型設定（想換模型只改這裡）====================
# 常見選擇（都用同一把 OPENAI_API_KEY）：
#   "gpt-4o-mini"  目前使用，最便宜，但屬於舊模型
#   "gpt-5-mini"   新一代小模型，判斷力較好，費用約兩三倍（推理型模型，程式會自動調整參數）
#   "gpt-5-nano"   新一代最便宜的小模型
# 也可以在 GitHub Secrets 設定 OPENAI_MODEL 來覆蓋這裡的設定，不必改檔案
OPENAI_MODEL = (os.environ.get("OPENAI_MODEL") or "").strip() or "gpt-4o-mini"
# 推理型模型（gpt-5 系列）的思考深度；留空代表用模型預設值。
# 想更快更省可填 "low"；若模型不支援，程式會自動拿掉這個參數重試
OPENAI_REASONING_EFFORT = (os.environ.get("OPENAI_REASONING_EFFORT") or "").strip()

# ==================== 可調整參數 ====================
NEWS_WINDOW_HOURS = 36          # 只看最近幾小時內發布的新聞
DEDUP_WINDOW_HOURS = 72         # 和最近幾小時內「已推播」的新聞比對是否為同一事件
HISTORY_KEEP_DAYS = 14          # 歷史紀錄保留天數，超過自動刪除
MAX_PUSH_PER_TICKER = 2         # 單輪每檔最多推播幾則
MAX_PUSH_PER_HOLDING = 3        # 持股單輪最多推播幾則
# 每檔 24 小時內的推播上限：超過「軟上限」後只放行強催化劑，到「硬上限」就完全停止
DAILY_SOFT_CAP = 3
DAILY_HARD_CAP = 6
DAILY_SOFT_CAP_HOLDING = 4
DAILY_HARD_CAP_HOLDING = 7
# 持股的「不需催化劑也送 AI」放寬，只用在新聞量少的股票（本輪候選稿件少於這個數字）
# 新聞量大的持股（例如 AVGO、MU）本來就不缺報導，用一般規則即可，避免洗版
HOLDING_RELAX_MAX_CANDIDATES = 30
# 同一家公司的不同股票代號，統一成一個，避免同一則新聞推兩次
TICKER_CANONICAL = {"GOOG": "GOOGL"}
FETCH_FAIL_ALERT_RATIO = 0.20   # 抓取失敗比例超過此值，立即推播警報
HEARTBEAT_HOUR_TW = 6           # 每天台灣時間幾點之後的第一次執行，推播一則健康回報
SLEEP_BETWEEN_TICKERS = 0.8
WEEKEND_MIN_GAP_HOURS = 6.0     # 週末兩次掃描至少間隔幾小時（排程是每 8 小時，留緩衝給 GitHub 排程延遲）
MAX_CONSECUTIVE_FETCH_FAILS = 8  # 連續幾檔抓取失敗就判定被封鎖，提前結束並警報
MAX_AI_PER_TICKER = 8           # 單輪每檔最多送 AI 判讀幾則，其餘留到下次執行（控制費用）
MAX_RUN_MINUTES = 30            # 整輪最多跑幾分鐘（workflow 上限 40 分鐘），超過就提前結束並警報

# 持股清單從 holdings.txt 讀取（一行一個代號），持股變動只要改那個檔案，不用改程式
# 持股的待遇：放寬推播條件、每日上限較高、推播標題標「★ 持股」、每次最優先掃描
HOLDINGS_FILE = "holdings.txt"
HOLDINGS = set()

# ==================== 新聞常用名稱對照表 ====================
# 格式：代號: ([不分大小寫的名稱], [必須大小寫完全相同的名稱])
# 第二組用在名稱本身是普通英文字的情況，例如 Arm、Circle、Coherent
# 新增觀察股時不用手動加：程式會自動向 SEC 查名稱、請 AI 產生，存在 auto_aliases.json
# 只有自動結果不準時，才需要在這裡手動加一行（這裡的設定優先於自動結果）
TICKER_PROFILES = {
    # 科技巨頭
    "GOOGL": (["Alphabet", "Google", "Waymo"], []),
    "MSFT": (["Microsoft"], []),
    "TSLA": (["Tesla"], []),
    "AAPL": (["Apple"], []),
    "AMZN": (["Amazon", "AWS"], []),
    "META": (["Meta Platforms", "Facebook", "Instagram", "WhatsApp"], ["Meta"]),
    # AI 晶片與 IP
    "NVDA": (["Nvidia"], []),
    "AMD": (["Advanced Micro Devices"], []),
    "AVGO": (["Broadcom"], []),
    "MRVL": (["Marvell"], []),
    "QCOM": (["Qualcomm", "Snapdragon"], []),
    "ARM": (["Arm Holdings"], ["Arm"]),
    "INTC": (["Intel"], []),
    # 代工與封測
    "TSM": (["TSMC", "Taiwan Semiconductor"], []),
    "GFS": (["GlobalFoundries"], []),
    "TSEM": (["Tower Semiconductor"], []),
    "ASX": (["ASE Technology", "ASE Holding"], ["ASE"]),
    "AMKR": (["Amkor"], []),
    # 記憶體與儲存
    "MU": (["Micron"], []),
    "SKHY": (["SK hynix", "Hynix"], []),
    "SNDK": (["SanDisk"], []),
    "WDC": (["Western Digital"], []),
    "STX": (["Seagate"], []),
    "PENG": (["Penguin Solutions"], []),
    # 半導體設備
    "ASML": (["ASML"], []),
    "AMAT": (["Applied Materials"], []),
    "LRCX": (["Lam Research"], []),
    "KLAC": (["KLA Corp"], ["KLA"]),
    "ACLS": (["Axcelis"], []),
    "VECO": (["Veeco"], []),
    "ONTO": (["Onto Innovation"], []),
    "CAMT": (["Camtek"], []),
    "KLIC": (["Kulicke & Soffa", "Kulicke and Soffa", "Kulicke"], []),
    "FORM": (["FormFactor"], []),
    "COHU": (["Cohu"], []),
    "TER": (["Teradyne"], []),
    "AEHR": (["Aehr Test", "Aehr"], []),
    "MKSI": (["MKS Instruments", "MKS Inc"], ["MKS"]),
    "AEIS": (["Advanced Energy Industries"], ["Advanced Energy"]),
    "UCTT": (["Ultra Clean"], []),
    "ICHR": (["Ichor Holdings"], ["Ichor"]),
    "ENTG": (["Entegris"], []),
    # 網路連接與光通訊
    "ALAB": (["Astera Labs"], []),
    "CRDO": (["Credo Technology"], ["Credo"]),
    "MTSI": (["MACOM"], []),
    "SMTC": (["Semtech"], []),
    "LITE": (["Lumentum"], []),
    "COHR": (["Coherent Corp"], ["Coherent"]),
    "AAOI": (["Applied Optoelectronics"], []),
    "AXTI": (["AXT Inc"], ["AXT"]),
    "CIEN": (["Ciena"], []),
    "GLW": (["Corning"], []),
    "CSCO": (["Cisco"], []),
    "NOK": (["Nokia"], []),
    # 類比、射頻與功率
    "CRUS": (["Cirrus Logic"], []),
    "SWKS": (["Skyworks"], []),
    "QRVO": (["Qorvo"], []),
    "MPWR": (["Monolithic Power"], []),
    "VICR": (["Vicor"], []),
    "POWI": (["Power Integrations"], []),
    "VSH": (["Vishay"], []),
    "IFNNY": (["Infineon"], []),
    "ALGM": (["Allegro MicroSystems", "Allegro Micro"], []),
    "WOLF": (["Wolfspeed"], []),
    "NVTS": (["Navitas"], []),
    # 伺服器與資料中心硬體
    "DELL": (["Dell"], []),
    "HPE": (["Hewlett Packard Enterprise"], []),
    "VRT": (["Vertiv"], []),
    "TTMI": (["TTM Technologies"], ["TTM"]),
    # 新興雲端算力
    "CRWV": (["CoreWeave"], []),
    "NBIS": (["Nebius"], []),
    "WULF": (["TeraWulf"], []),
    "CIFR": (["Cipher Mining", "Cipher Digital"], []),
    # 企業軟體
    "PLTR": (["Palantir"], []),
    "NOW": (["ServiceNow"], []),
    "CRM": (["Salesforce"], []),
    "SNOW": (["Snowflake"], []),
    "DDOG": (["Datadog"], []),
    "IBM": (["IBM"], []),
    "ZETA": (["Zeta Global"], []),
    # 資安
    "PANW": (["Palo Alto Networks"], []),
    "FTNT": (["Fortinet"], []),
    "ZS": (["Zscaler"], []),
    "OKTA": (["Okta"], []),
    "NET": (["Cloudflare"], []),
    "AKAM": (["Akamai"], []),
    "RBRK": (["Rubrik"], []),
    "VRNS": (["Varonis"], []),
    # 通訊、物聯網與邊緣運算
    "TWLO": (["Twilio"], []),
    "BAND": (["Bandwidth Inc"], []),
    "IOT": (["Samsara"], []),
    "LTRX": (["Lantronix"], []),
    "BB": (["BlackBerry"], []),
    "AMBA": (["Ambarella"], []),
    "HIMX": (["Himax"], []),
    "VUZI": (["Vuzix"], []),
    "KEYS": (["Keysight"], []),
    "VIAV": (["Viavi"], []),
    # 機器人、無人機與國防
    "CGNX": (["Cognex"], []),
    "RRX": (["Regal Rexnord"], []),
    "OUST": (["Ouster"], []),
    "PDYN": (["Palladyne"], []),
    "AVAV": (["AeroVironment"], []),
    "ONDS": (["Ondas"], []),
    "UMAC": (["Unusual Machines"], []),
    "AMPX": (["Amprius"], []),
    "FEIM": (["Frequency Electronics"], []),
    "VELO": (["Velo3D"], []),
    # 太空
    "SPCX": (["SpaceX", "Starlink"], []),
    "RKLB": (["Rocket Lab"], []),
    "RDW": (["Redwire"], []),
    "PL": (["Planet Labs"], []),
    "BKSY": (["BlackSky"], []),
    "VSAT": (["Viasat"], []),
    "IRDM": (["Iridium"], []),
    # 關鍵礦物與材料
    "MP": (["MP Materials"], []),
    "USAR": (["USA Rare Earth"], []),
    "UUUU": (["Energy Fuels"], []),
    "PPTA": (["Perpetua Resources", "Perpetua"], []),
    "UAMY": (["United States Antimony", "US Antimony"], []),
    "MTRN": (["Materion"], []),
    # 能源與電力
    "LEU": (["Centrus"], []),
    "BE": (["Bloom Energy"], []),
    "FCEL": (["FuelCell Energy"], []),
    "EOSE": (["Eos Energy"], []),
    "TE": (["T1 Energy"], []),
    "GEV": (["GE Vernova"], []),
    "ETN": (["Eaton"], []),
    "PWR": (["Quanta Services"], []),
    "CAT": (["Caterpillar"], []),
    "CMI": (["Cummins"], []),
    "GNRC": (["Generac"], []),
    "HON": (["Honeywell"], []),
    "ROK": (["Rockwell Automation"], []),
    "LIN": (["Linde"], []),
    "ECL": (["Ecolab"], []),
    # 量子與數位資產
    "IONQ": (["IonQ"], []),
    "QNT": (["Quantinuum"], []),
    "CRCL": (["Circle Internet", "USDC"], ["Circle"]),
    "GLXY": (["Galaxy Digital"], []),
}

# 代號本身是普通字、貨幣、人名或其他機構縮寫的股票：
# 標題裡單獨出現代號不算數，必須寫成 $ARM、NASDAQ: ARM 或 (ARM) 才算，搜尋時也不用代號
STRICT_TICKERS = {
    "ARM", "BAND", "BB", "NOW", "SNOW", "NET", "IOT", "LITE", "NOK", "PL", "MP",
    "BE", "TE", "CAT", "HON", "ROK", "LIN", "QNT", "ZETA", "ONTO", "WOLF", "FORM",
    "KEYS", "TER", "ASX", "MU", "ZS", "VELO", "CRM", "LEU", "PENG", "ETN", "PWR",
    "CMI", "ECL", "GLW", "STX", "WDC", "GFS", "RRX"
}

# ==================== 來源分級 ====================
# 一級來源：通訊社、主流財經媒體、各產業專業媒體
PRIMARY_SOURCES = [
    # 通訊社與新聞稿
    "pr newswire", "business wire", "globenewswire", "accesswire", "newsfile",
    "reuters", "bloomberg", "associated press", "ap news",
    # 主流財經媒體
    "wall street journal", "wsj", "cnbc", "financial times", "marketwatch", "barron's",
    "investor's business daily", "fortune", "axios", "new york times", "the economist",
    "the information",
    # 科技與半導體
    "the verge", "techcrunch", "tom's hardware", "wccftech", "ars technica", "anandtech",
    "semiconductor engineering", "semiengineering", "nikkei", "digitimes", "trendforce",
    "ee times", "eetimes", "the register", "focus taiwan", "taipei times", "the elec",
    "korea economic daily", "the korea herald", "electrek", "servethehome",
    "the next platform", "data center dynamics", "datacenterdynamics", "crn",
    "siliconangle", "light reading", "lightwave", "fierce",
    # 資安
    "securityweek", "bleepingcomputer", "the record", "dark reading",
    # 太空與國防
    "spacenews", "breaking defense", "defense news", "defensescoop", "the war zone",
    # 能源、核能與礦業
    "world nuclear news", "utility dive", "mining.com", "mining weekly", "s&p global",
    # 加密資產
    "coindesk", "the block",
]

# 二級來源：會大量轉載 Zacks、Motley Fool 等評論文章，只放行命中「強催化劑」的新聞
SECONDARY_SOURCES = [
    "yahoo", "seeking alpha", "benzinga", "investing.com",
]

# ==================== 標題排除規則 ====================
# 第一類：一律排除（律師招募、農場文、預告排程、產業研報、公關軟文）
ALWAYS_JUNK_PATTERNS = [
    # 律師訴訟招募
    r"\bclass\s+action\b", r"\bshareholder\s+alert\b", r"\breminds\s+investors\b",
    r"\blead\s+plaintiff\b", r"\bloss\s+submission\b", r"\bsecurities\s+fraud\b",
    r"\binvestor\s+rights?\b", r"\blaw\s+offices?\s+of\b", r"\bnotifies\s+shareholders\b",
    r"\brosen\b", r"\bpomerantz\b", r"\bglancy\b", r"\bschall\b",
    r"\bfaruqi\b", r"\bhagens\s+berman\b", r"\blevi\s+&\s+korsinsky\b",
    r"\bbronstein\b", r"\bkaskela\b", r"\bblock\s+&\s+leviton\b",

    # 財經農場文與投資建議
    r"\bzacks\b", r"\bmotley\s+fool\b",
    r"\b(?:up|down|gains?|drops?|falls?|slips?|surges?|climbs?|plunges?)\s+\d+(?:\.\d+)?%\s+since\s+(?:(?:its|the|last)\s+)*earnings\b",
    r"\bcan\s+[\w\s.']+?\s+(?:rally|run|momentum|surge|gains?|streak)\s+continue\b",
    r"\bwill\s+the\s+(?:rally|surge|run|momentum|streak)\s+continue\b",
    r"\bahead\s+of\s+(?:its\s+)?earnings\b",
    r"\bbefore\s+(?:its\s+)?earnings\b",
    r"\bwhat\s+to\s+expect\s+(?:from|for)\s+earnings\b",
    r"\bearnings\s+(?:preview|whisper|scorecard|recap)\b",
    r"\bshould\s+you\s+(?:buy|sell|hold)\b",
    r"\bis\s+[\w\s.']+?\s+(?:stock\s+)?a\s+(?:good\s+|strong\s+|smart\s+|screaming\s+)?(?:buy|sell|bargain)\b",
    r"\bbuy,?\s+sell,?\s+or\s+hold\b",
    r"\bwhat(?:'?s|\s+is)\s+next\s+for\b",
    r"\bbetter\s+buy\b",
    r"\b\d+\s+reasons\b",
    r"\bstocks?\s+to\s+(?:buy|watch|own|hold)\b",
    r"\btop\s+\d+\s+[\w\s]*stocks\b",
    r"\bmillionaire\b",
    r"\bprice\s+target\b",
    r"\(preview\)", r"\bearnings\s+setup\b", r"\bset\s+for\s+earnings\b", r"\bpoised\s+to\s+beat\b",
    r"\bbeat\s+(?:earnings\s+)?estimates\s+again\b", r"\breasons?\s+why\b", r"\bin\s+focus\b",
    r"\bfair\s+value\b", r"\bundervalued\b", r"\bovervalued\b", r"\bm&a\s+watch\b",
    r"\bcramer\b", r"\blightning\s+round\b", r"\bmad\s+money\b",
    r"\breporting\s+date\b", r"\bearnings\s+(?:release\s+)?date\b", r"\bdate\s+(?:for|of)\s+[\w\s]*results\b",

    # 例行會議、電話會排程
    r"\bto\s+report\b", r"\bschedules?\b", r"\bto\s+host\b", r"\bwebcast\b",
    r"\bconference\s+call\b", r"\binvestor\s+conference\b", r"\bfireside\s+chat\b",
    r"\broadshow\b", r"\bannual\s+meeting\b", r"\bproxy\s+statement\b",

    # 產業研報、評選獲獎與公關軟文
    r"\bmarket\s+size\b", r"\bcagr\b", r"\bmarket\s+research\b",
    r"\bforecast\s+to\s+20\d\d\b", r"\btop\s+players\b", r"\bindustry\s+report\b",
    r"\bnamed\s+(?:a\s+)?winner\b", r"\bwins?\s+award\b", r"\bgreat\s+place\s+to\s+work\b",
    r"\besg\s+report\b", r"\bsustainability\s+report\b", r"\bcarbon\s+neutral\b",
    r"\bdonat\w*\b", r"\bwhitepaper\b", r"\bsurvey\s+finds\b",
    r"\bgoogle\.org\b", r"\bcharit\w*\b", r"\bnonprofits?\b", r"\bphilanthrop\w*\b",
    r"\bscholarships?\b", r"\bsponsorship\b", r"\bvolunteer\w*\b",
]

# 第二類：股價走勢類標題。只有在「沒有強催化劑」時才排除
# 例如「Intel shares surge 20% on Nvidia $5 billion investment」雖然寫了漲幅，但有實質事件，必須放行
MOVE_JUNK_PATTERNS = [
    r"\bwhy\s+(?:is|did|are)\s+[\w\s]+\s+(?:up|down|falling|dropping|rising|surging|sliding|moving)\b",
    r"\bhere'?s\s+why\b",
    r"\bprofit[\s-]taking\b",
    r"\bytd\s+(?:run|gain|drop|loss|rally)\b",
    r"\b(?:stock|shares?)\s+(?:drops?|falls?|slips?|surges?|climbs?|rises?|slides?|tumbles?|jumps?|soars?|sinks?|down|up)\s+\d+(?:\.\d+)?%",
    r"\b(?:drops?|falls?|slips?|surges?|climbs?|rises?|slides?|tumbles?|jumps?|soars?|sinks?|down|up)\s+(?:by\s+)?\d+(?:\.\d+)?%\s+(?:as|after|amid|on|following|today|premarket|in\s+premarket|in\s+after[\s-]hours)\b",
    r"\bshares\s+(?:fall|drop|slip|slide|surge|jump|tumble|soar|sink|rally)\b",
    r"\bstock\s+(?:is\s+)?(?:soaring|sinking|plunging|surging|tumbling|rallying)\b",
    r"\b(?:stock|shares?)\s+(?:inches?|edges?|ticks?|creeps?)\s+(?:higher|lower|up|down)\b",
    r"\b(?:stock|shares?)\s+stays?\s+flat\b",
]

# ==================== 催化劑信號 ====================
# 強催化劑：財報與財測、併購、融資稀釋、破產、調查與禁令、高層異動、做空報告、大額合約等
STRONG_SIGNAL_PATTERNS = [
    # 財報與財測
    r"\bearnings\s+results\b", r"\bquarterly\s+results\b", r"\bfinancial\s+results\b",
    r"\breports?\s+(?:record\s+)?(?:first|second|third|fourth|q[1-4]|full[\s-]year|fiscal)?\s*(?:quarter\s+)?(?:fiscal\s+)?(?:20\d\d\s+)?results\b",
    r"\bq[1-4]\s+(?:results|earnings|revenue|sales)\b",
    r"\b(?:beats?|miss(?:es)?|tops?)\s+(?:\w+\s+)?(?:estimates|expectations|forecasts?)\b",
    r"\b(?:raises?|lifts?|boosts?|hikes?|lowers?|cuts?|slashes?|reduces?|trims?|withdraws?|reaffirms?)\s+(?:its\s+)?(?:full[\s-]year\s+|annual\s+|quarterly\s+|fiscal\s+|20\d\d\s+)?(?:guidance|outlook|forecast)\b",
    r"\b(?:guidance|outlook|forecast)\s+(?:raise|cut|hike)\b",
    r"\bmonthly\s+(?:revenue|sales)\b", r"\brecord\s+(?:revenue|sales|quarter)\b",
    r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+(?:revenue|sales)\b",
    r"\bbuyback\b", r"\brepurchase\s+(?:program|plan|authorization)\b",
    # 併購與投資
    r"\btakeover\b", r"\bacquisition\b", r"\bacquires?\b", r"\bto\s+acquire\b", r"\bbuyout\b",
    r"\bto\s+buy\b", r"\bin\s+talks\b", r"\bbids?\s+for\b", r"\bmakes?\s+(?:an?\s+)?(?:\w+\s+)?(?:bid|offer)\b", r"\bexplor\w*\s+(?:a\s+)?sale\b", r"\bmerger\b", r"\bmerge\b",
    r"\bstrategic\s+investment\b", r"\btakes?\s+(?:a\s+)?stake\b", r"\bstake\s+in\b",
    r"\binvest(?:s|ing|ment)?\s+\$\d", r"\bspin[\s-]?off\b", r"\bdivest\w*\b", r"\bsale\s+of\b",
    # 融資與稀釋
    r"\bpublic\s+offering\b", r"\bregistered\s+direct\b", r"\bprivate\s+placement\b",
    r"\bprices?\s+(?:\$[\d.,]+\s+(?:million|billion)\s+)?(?:upsized\s+)?offering\b",
    r"\bat[\s-]the[\s-]market\b", r"\bconvertible\s+(?:senior\s+)?notes\b",
    r"\b(?:stock|share|equity)\s+(?:offering|sale)\b", r"\bdilut\w*\b",
    # 破產與財務危機
    r"\bchapter\s+11\b", r"\bbankruptcy\b", r"\bgoing\s+concern\b", r"\bdelist\w*\b",
    r"\bdefault\b", r"\brestructur\w*\b",
    # 調查、訴訟、禁令
    r"\bantitrust\b", r"\bprobe\b", r"\binvestigation\b", r"\bsubpoena\b", r"\binjunction\b",
    r"\bsues\b", r"\blawsuit\b", r"\bpatent\s+infringement\b", r"\bfined\b", r"\bfines?\s+\$",
    r"\bexport\s+(?:control|ban|curb|restriction|license)s?\b", r"\bsanction\w*\b",
    r"\btariffs?\b", r"\bban(?:s|ned)?\b", r"\bentity\s+list\b",
    r"\bchips\s+act\b",
    # 高層異動
    r"\b(?:ceo|cfo|coo|cto|chief\s+executive|chief\s+financial|chairman|president)\b.*\b(?:resign\w*|steps?\s+down|depart\w*|retire\w*|ousted|fired|exit\w*|leav\w*|replac\w*|succe\w*|appoint\w*|names?|named|hires?)\b",
    r"\b(?:resign\w*|steps?\s+down|appoint\w*|names?|named|hires?)\b.*\b(?:ceo|cfo|chief\s+executive|chief\s+financial)\b",
    # 做空與重大負面事件
    r"\bshort[\s-]seller\b", r"\bshort\s+report\b", r"\bhindenburg\b", r"\bmuddy\s+waters\b",
    r"\bcitron\b", r"\bspruce\s+point\b", r"\bkerrisdale\b", r"\bgrizzly\b",
    r"\brecall\w*\b", r"\boutage\b", r"\bbreach\b", r"\bhack\w*\b", r"\bcyberattack\b",
    r"\blayoffs?\b", r"\bjob\s+cuts?\b", r"\bcuts?\s+\d[\d,]*\s+jobs\b",
    r"\bdowngrade[sd]?\s+to\s+(?:sell|underperform|underweight)\b",
    # 大額合約與訂單
    r"\bawarded\s+(?:an?\s+)?(?:\$[\d.,]+\s*(?:million|billion|bn|m)\s+)?contract\b",
    r"\b(?:wins?|secures?|lands?|signs?|inks?)\s+(?:an?\s+)?(?:\$[\d.,]+\s*(?:million|billion|bn|m)\s+)?(?:contract|deal|order)\b",
    r"\$[\d.,]+\s*(?:billion|bn)\b",
    r"\bmulti[\s-]?billion\b",
]

# 一般催化劑：產品發表、合作、設計案、部署等
SIGNAL_PATTERNS = [
    r"\brevenue\b", r"\beps\b", r"\bearnings\b", r"\bguidance\b", r"\boutlook\b",
    r"\bsigns?\s+(?:a\s+)?contract\b", r"\bsecures?\s+(?:a\s+)?contract\b",
    r"\bpurchase\s+order\b", r"\breceives?\s+(?:an?\s+)?order\b", r"\border\s+from\b",
    r"\b(?:inks?|strikes?|signs?|seals?)\s+(?:a\s+)?(?:deal|pact|agreement)\b",
    r"\bmulti[\s-]year\s+agreement\b", r"\bprocurement\s+contract\b", r"\bsupply\s+agreement\b",
    r"\bpartner(?:ed|ing|ship|s)?\s+with\b", r"\bcollaborat\w*\b",
    r"\bjoint\s+venture\b", r"\bto\s+deploy\b", r"\bdeploy\w*\b", r"\bdesign\s+win\b",
    r"\blicensing\s+agreement\b", r"\broyalt(?:y|ies)\b", r"\bselected\s+by\b",
    r"\blaunch(?:es|ed)?\b", r"\bunveil\w*\b", r"\bintroduc\w*\b", r"\bdebuts?\b",
    r"\bnext[\s-]gen\w*\b", r"\barchitecture\b", r"\bprocessor\b", r"\bchipset?s?\b",
    r"\bsmr\b", r"\bnuclear\s+reactor\b", r"\bpower\s+purchase\s+agreement\b",
    r"\bheadset\b", r"\bdevice\b", r"\bglasses\b", r"\bapproval\b", r"\bapproves?\b",
    r"\bcertif\w*\b", r"\bfda\b", r"\bfaa\b", r"\bfcc\b",
    r"\$[\d.,]+\s*(?:million|m)\b", r"\bdividend\b",
]

# ==================== 去重用詞表 ====================
STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "about", "above", "after", "along", "amid",
    "at", "by", "for", "from", "in", "into", "of", "to", "with", "on", "its", "it",
    "as", "is", "are", "be", "will", "over", "says", "say", "new", "stock", "shares",
    "tumbles", "jumps", "falls", "rises", "plunges", "surges", "soars", "slides",
    "through", "announces", "announced", "watch", "designed", "work", "use", "inc",
    "corp", "co", "ltd", "report", "reports", "this", "that", "than", "more", "has", "have"
}
VALID_SHORT_TECH_TERMS = {"ai", "vr", "ar", "ev", "ip", "5g", "6g", "os", "pc", "mr", "xr"}

# 常見縮寫，不算「具體型號」
COMMON_ACRONYMS = {
    "AI", "GPU", "GPUS", "CPU", "CPUS", "CEO", "CFO", "COO", "CTO", "US", "USA", "UK", "EU",
    "IPO", "ETF", "SEC", "DOJ", "FTC", "FDA", "FAA", "FCC", "NASA", "DOD", "Q1", "Q2", "Q3", "Q4",
    "EPS", "ATM", "M&A", "PC", "PCS", "EV", "EVS", "TV", "AR", "VR", "XR", "5G", "6G", "LLC", "INC", "NYSE", "NASDAQ"
}

SEC_NAME_CACHE = {}   # 代號 → [備援名稱]

VALID_EVENT_TYPES = {"ORDER", "M&A", "DILUTION", "EARNINGS", "PRODUCT", "CRISIS", "LEADERSHIP"}


# ==================== 公司名稱與比對 ====================
def clean_company_name(raw_name):
    name = re.sub(r"\s*/[A-Z]{2,3}/?\s*$", "", raw_name.strip(), flags=re.IGNORECASE)
    for _ in range(2):
        name = re.sub(
            r",?\s*(INC|CORP|CORPORATION|LTD|LIMITED|HOLDINGS?|CO|PLC|LLC|AG|SE|SA|NV|N\.V|GMBH|GROUP)\.?$",
            "", name, flags=re.IGNORECASE
        ).strip()
    return name if len(name) >= 3 else raw_name.strip()


# ==================== 新代號自動產生新聞名稱 ====================
# 不在 TICKER_PROFILES 的代號，程式會先向 SEC 查法定名稱，再請 AI 產生新聞常用名稱，
# 結果存在 auto_aliases.json，之後直接沿用，不用你手動查。
AUTO_ALIAS_FILE = "auto_aliases.json"
AUTO_PROFILES = {}
AUTO_NAMED_THIS_RUN = []
AUTO_RETRY_UNKNOWN_DAYS = 7
GENERIC_NAME_WORDS = {
    "energy", "technology", "technologies", "systems", "group", "global", "international", "holdings",
    "solutions", "american", "national", "united", "general", "first", "advanced", "digital", "power",
    "resources", "materials", "networks", "software", "semiconductor", "devices", "industries",
    "robotics", "aerospace", "defense", "mining", "motors", "labs", "therapeutics", "pharmaceuticals",
    "electronics", "communications", "capital", "financial", "partners", "enterprises", "brands",
    "inc", "corp", "company", "ai", "data", "cloud", "quantum", "space", "nuclear", "solar", "battery"
}


def load_auto_profiles():
    AUTO_PROFILES.clear()
    if not os.path.exists(AUTO_ALIAS_FILE):
        return
    try:
        with open(AUTO_ALIAS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            AUTO_PROFILES.update(data)
    except Exception as e:
        print(f"⚠️ {AUTO_ALIAS_FILE} 讀取失敗（{e}），將重新產生", flush=True)


def save_auto_profiles():
    tmp = AUTO_ALIAS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(AUTO_PROFILES, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, AUTO_ALIAS_FILE)


def fetch_sec_official_names(tickers):
    names = {}
    if not tickers:
        return names
    if not SEC_USER_AGENT:
        print("ℹ️ 未設定 SEC_USER_AGENT，新代號只能靠 AI 判斷公司名稱（較不準確）", flush=True)
        return names
    try:
        res = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"},
            timeout=12
        )
        if res.status_code == 200:
            wanted = set(tickers)
            for item in res.json().values():
                t = str(item.get("ticker", "")).upper()
                if t in wanted:
                    names[t] = clean_company_name(item.get("title", ""))
        else:
            print(f"⚠️ SEC 名稱下載失敗（HTTP {res.status_code}）", flush=True)
    except Exception as e:
        print(f"⚠️ SEC 名稱下載失敗（{e}）", flush=True)
    return names


def clean_alias_list(values):
    out = []
    if not isinstance(values, list):
        return out
    for v in values:
        v = str(v).strip()
        if not (2 <= len(v) <= 40):
            continue
        if v.lower() in GENERIC_NAME_WORDS:
            continue
        if v.lower() not in [x.lower() for x in out]:
            out.append(v)
    return out[:5]


def ask_ai_for_aliases(ticker, official_name):
    if not OPENAI_API_KEY:
        return None
    prompt = f"""You help match news headlines to a US-listed stock.
Ticker: {ticker}
Official SEC registrant name: {official_name or "unknown"}

Return ONLY a JSON object:
{{"known": true or false,
  "names": ["names that English news headlines commonly use for this company, matched case-insensitively"],
  "case_sensitive_names": ["names that are also ordinary English words, so they should only match when capitalized, e.g. Arm, Circle"],
  "ticker_is_common_word": true or false}}

Rules:
1. Up to 4 names in total. Put the short name headlines use most often first (e.g. "TSMC" for Taiwan Semiconductor Manufacturing, "Google" and "Alphabet" for Alphabet Inc.).
2. Do not include generic words alone (e.g. "Energy", "Technology"), and do not include product names that other companies also use.
3. ticker_is_common_word is true when the ticker is an ordinary word, currency, common abbreviation or another organization's acronym (e.g. NOW, NET, SNOW, ASX, NOK).
4. If you do not recognize the company and the official name is unknown, set known to false and leave the lists empty. Never guess."""
    content = openai_chat_json([{"role": "user", "content": prompt}], temperature=0, attempts=2)
    if content is None:
        return None
    try:
        return json.loads(content)
    except Exception:
        return None


def heuristic_names_from_sec(official_name):
    """AI 失敗時的備援：用 SEC 名稱，以及名稱第一個字（不是通用字才用）"""
    if not official_name:
        return []
    names = [official_name]
    first = official_name.split()[0]
    if len(first) > 3 and first.lower() not in GENERIC_NAME_WORDS and first.lower() != official_name.lower():
        names.append(first)
    return names


def prepare_names_for_missing(tickers):
    """替不在 TICKER_PROFILES 的代號準備新聞名稱：先看已存的自動結果，沒有就向 SEC 查名稱再請 AI 產生"""
    load_auto_profiles()
    missing = [t for t in tickers if t not in TICKER_PROFILES]
    if not missing:
        return

    today = datetime.now(TW_TZ).date()

    def needs_refresh(t):
        entry = AUTO_PROFILES.get(t)
        if not entry:
            return True
        if entry.get("names") or entry.get("cs_names"):
            return False
        try:
            created = datetime.strptime(entry.get("created", ""), "%Y-%m-%d").date()
            return (today - created).days >= AUTO_RETRY_UNKNOWN_DAYS
        except Exception:
            return True

    todo = [t for t in missing if needs_refresh(t)]
    official = fetch_sec_official_names(todo)
    changed = False

    for t in todo:
        result = ask_ai_for_aliases(t, official.get(t))
        if result is None:
            # AI 這次失敗：先用 SEC 名稱頂著，不存檔，下次再試
            fallback = heuristic_names_from_sec(official.get(t))
            if fallback:
                SEC_NAME_CACHE[t] = fallback
            print(f"⚠️ {t} 自動產生名稱失敗，本次暫用：{fallback or '只用代號比對'}", flush=True)
            continue
        names = clean_alias_list(result.get("names"))
        cs_names = clean_alias_list(result.get("case_sensitive_names"))
        if not names and not cs_names and official.get(t):
            names = heuristic_names_from_sec(official.get(t))
        AUTO_PROFILES[t] = {
            "names": names,
            "cs_names": cs_names,
            "strict": bool(result.get("ticker_is_common_word")),
            "official_name": official.get(t, ""),
            "created": today.isoformat(),
        }
        changed = True
        shown = "、".join(names + cs_names) if (names or cs_names) else "無法辨識，只用代號比對"
        AUTO_NAMED_THIS_RUN.append(f"{t}：{shown}")
        print(f"🤖 已自動產生 {t} 的新聞名稱：{shown}", flush=True)

    if changed:
        save_auto_profiles()

    for t in missing:
        if AUTO_PROFILES.get(t, {}).get("strict"):
            STRICT_TICKERS.add(t)


def get_names(ticker):
    if ticker in TICKER_PROFILES:
        names, cs_names = TICKER_PROFILES[ticker]
        return list(names), list(cs_names)
    auto = AUTO_PROFILES.get(ticker)
    if auto and (auto.get("names") or auto.get("cs_names")):
        return list(auto.get("names", [])), list(auto.get("cs_names", []))
    return list(SEC_NAME_CACHE.get(ticker, [])), []


def get_display_name(ticker):
    names, cs_names = get_names(ticker)
    all_names = names + cs_names
    return all_names[0] if all_names else ticker


# 別家公司名稱剛好包含本公司名稱時，先把它拿掉再比對（例如 Clean Energy Fuels 不是 Energy Fuels）
ENTITY_EXCLUDE_PHRASES = {
    "UUUU": ["Clean Energy Fuels"],
    "ARM": ["Arm and Hammer", "Arm & Hammer"],
    "MKSI": ["MKS Hospitality"],
}


def contains_phrase(text, phrase, case_sensitive=False):
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.search(rf"(?<![\w$]){re.escape(phrase)}(?!\w)", text, flags) is not None


def matches_ticker_symbol(ticker, title):
    t = re.escape(ticker)
    explicit = (
        rf"(?:\${t}\b"
        rf"|\b(?:NASDAQ|NYSE|NYSE\s+American|NYSEAMERICAN|AMEX|OTC|OTCQX|OTCQB)\s*:\s*{t}\b"
        rf"|\(\s*{t}\s*\))"
    )
    if re.search(explicit, title, flags=re.IGNORECASE):
        return True
    if ticker in STRICT_TICKERS:
        return False
    # 一般代號：大小寫必須完全相同（標題寫 NVDA 才算）
    return re.search(rf"(?<![\w$]){t}(?!\w)", title) is not None


def matches_target_entity(ticker, title):
    for ex in ENTITY_EXCLUDE_PHRASES.get(ticker, []):
        title = re.sub(re.escape(ex), " ", title, flags=re.IGNORECASE)
    names, cs_names = get_names(ticker)
    for n in names:
        if contains_phrase(title, n):
            return True
    for n in cs_names:
        if contains_phrase(title, n, case_sensitive=True):
            return True
    return matches_ticker_symbol(ticker, title)


def build_search_query(ticker):
    names, cs_names = get_names(ticker)
    terms, seen = [], set()
    for n in names + cs_names:
        key = n.lower()
        if key not in seen:
            seen.add(key)
            terms.append(f'"{n}"')
        if len(terms) >= 4:
            break
    if ticker not in STRICT_TICKERS and ticker.lower() not in seen:
        terms.append(f'"{ticker}"')
    if not terms:
        terms.append(f'"{ticker}"')
    return " OR ".join(terms) + " when:2d"


# ==================== 標題過濾 ====================
def matches_any(text, patterns):
    t_lower = text.lower()
    return any(re.search(p, t_lower) for p in patterns)


def get_source_tier(source_name):
    s = source_name.lower().strip()
    if any(src in s for src in PRIMARY_SOURCES):
        return "PRIMARY"
    if any(src in s for src in SECONDARY_SOURCES):
        return "SECONDARY"
    return None


def parse_pub_time(pub_date_raw):
    try:
        dt = parsedate_to_datetime(pub_date_raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC_TZ)
        return dt
    except Exception:
        return datetime(1970, 1, 1, tzinfo=UTC_TZ)


def is_within_hours(pub_date_raw, hours):
    if not pub_date_raw or not pub_date_raw.strip():
        return False
    try:
        dt = parsedate_to_datetime(pub_date_raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC_TZ)
        diff_hours = (datetime.now(UTC_TZ) - dt).total_seconds() / 3600
        return -1.0 <= diff_hours <= hours
    except Exception:
        return False


def evaluate_title(ticker, title, snippet, source_tier, relax=None):
    """
    回傳 (是否放行送 AI, 原因)
    規則：
      1. 一律排除類（律師、農場文、排程、研報、軟文）→ 排除
      2. 股價走勢類 → 沒有強催化劑才排除
      3. 二級來源 → 必須命中強催化劑（持股只需一般催化劑）
      4. 一級來源 → 必須命中催化劑（持股不需要）
    """
    if matches_any(title, ALWAYS_JUNK_PATTERNS):
        return False, "排除規則"

    combined = f"{title} {snippet}"
    strong = matches_any(combined, STRONG_SIGNAL_PATTERNS)
    normal = strong or matches_any(combined, SIGNAL_PATTERNS)
    is_holding = (ticker in HOLDINGS) if relax is None else relax

    if matches_any(title, MOVE_JUNK_PATTERNS) and not strong:
        return False, "股價走勢文"

    if source_tier == "SECONDARY":
        if strong or (is_holding and normal):
            return True, "強催化劑" if strong else "持股放寬"
        return False, "二級來源無強催化劑"

    if normal or is_holding:
        return True, "強催化劑" if strong else ("催化劑" if normal else "持股放寬")
    return False, "無催化劑"


# ==================== 去重 ====================
def make_news_fingerprint(ticker, title):
    # 與舊版完全相同的算法，舊紀錄才能繼續沿用
    clean_title = re.sub(r"[^\w\s]", "", title.lower())
    clean_title = " ".join(clean_title.split())
    raw_key = f"{ticker}_{clean_title}"
    return hashlib.md5(raw_key.encode("utf-8")).hexdigest()


def normalize_word(word):
    w = word.lower().lstrip("$")
    if any(ch.isdigit() for ch in w):
        return w.replace(",", "")
    return re.sub(r"(ments?|ings?|ed|s)$", "", w)


def normalize_money(title):
    """$60bn、$60B、$60 billion 統一成「60 billion」，讓不同媒體的寫法能互相比對"""
    t = re.sub(r"\$?(\d+(?:\.\d+)?)\s*(?:bn|b|billion)\b", r"\1 billion", title, flags=re.IGNORECASE)
    t = re.sub(r"\$?(\d+(?:\.\d+)?)\s*(?:mn|mln|m|million)\b", r"\1 million", t, flags=re.IGNORECASE)
    return t


def extract_core_words(title):
    title = normalize_money(title)
    words = re.findall(r"\$?[a-zA-Z0-9]+(?:[.,][0-9]+)?", title)
    core = set()
    for w in words:
        lw = w.lower()
        if lw in STOP_WORDS:
            continue
        norm = normalize_word(lw)
        if not norm:
            continue
        if any(ch.isdigit() for ch in norm) or len(norm) > 2 or norm in VALID_SHORT_TECH_TERMS:
            core.add(norm)
    return core


def get_alias_tokens(ticker):
    names, cs_names = get_names(ticker)
    tokens = {ticker.lower()}
    for n in names + cs_names:
        for w in re.findall(r"[a-zA-Z0-9]+", n):
            tokens.add(normalize_word(w))
    return tokens


def extract_specific_tokens(title, ticker):
    """具體識別詞：含數字的字（$5、H200、18A）或非常見縮寫的全大寫字（CPX、HBM4）"""
    tokens = set()
    for w in re.findall(r"\$?[A-Za-z0-9]+(?:[.,][0-9]+)?", normalize_money(title)):
        bare = w.lstrip("$")
        if bare.upper() == ticker:
            continue
        if any(ch.isdigit() for ch in bare):
            tokens.add(normalize_word(bare))
        elif len(bare) >= 2 and bare.isupper() and bare not in COMMON_ACRONYMS:
            tokens.add(bare.lower())
    return tokens


def recent_sent_titles(ticker, history_entries):
    cutoff = datetime.now(UTC_TZ) - timedelta(hours=DEDUP_WINDOW_HOURS)
    rows = [h for h in history_entries
            if h["status"] == "SENT" and h["ticker"] == ticker and h["time"] >= cutoff]
    rows.sort(key=lambda h: h["time"], reverse=True)
    return [h["title"] for h in rows]


def count_sent_last_24h(ticker, history_entries):
    cutoff = datetime.now(UTC_TZ) - timedelta(hours=24)
    return sum(1 for h in history_entries
               if h["status"] == "SENT" and h["ticker"] == ticker and h["time"] >= cutoff)


def is_duplicate_news(ticker, new_title, history_entries):
    """
    只和「最近 72 小時內、同一檔、實際推播過」的標題比對。
    比對前先拿掉公司名稱本身（否則同一家公司的所有新聞都會因為名字相同而被判重複）。
    """
    alias_tokens = get_alias_tokens(ticker)
    new_words = extract_core_words(new_title) - alias_tokens
    if len(new_words) < 2:
        return False
    new_specific = extract_specific_tokens(new_title, ticker) - alias_tokens
    cutoff = datetime.now(UTC_TZ) - timedelta(hours=DEDUP_WINDOW_HOURS)

    for h in history_entries:
        if h["status"] != "SENT" or h["ticker"] != ticker or h["time"] < cutoff:
            continue
        old_words = extract_core_words(h["title"]) - alias_tokens
        if not old_words:
            continue
        union = new_words | old_words
        similarity = len(new_words & old_words) / len(union) if union else 0
        if similarity >= 0.35:
            return True
        old_specific = extract_specific_tokens(h["title"], ticker) - alias_tokens
        # 共享同一個具體數字或型號（例如 $5 billion、H200、CPX）時，門檻放寬
        if new_specific & old_specific and similarity >= 0.20:
            return True
    return False


# ==================== 歷史紀錄 ====================
# 新格式：時間|||狀態|||指紋|||代號|||標題
# 狀態：SENT（已推播）、PASS（AI 判定不推）、LEGACY（舊版紀錄，只用來避免重複送 AI）
HEARTBEAT_PREFIX = "@@HEARTBEAT"


def load_history():
    entries, heartbeat_date = [], ""
    if not os.path.exists(HISTORY_FILE):
        return entries, heartbeat_date
    now = datetime.now(UTC_TZ)
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith(HEARTBEAT_PREFIX):
                parts = line.split("|||")
                if len(parts) >= 2:
                    heartbeat_date = parts[1].strip()
                continue
            parts = line.split("|||")
            if len(parts) == 5 and parts[1] in {"SENT", "PASS", "LEGACY"}:
                try:
                    t = datetime.fromisoformat(parts[0])
                except Exception:
                    t = now
                entries.append({"time": t, "status": parts[1], "fp": parts[2],
                                "ticker": parts[3], "title": parts[4]})
            elif len(parts) >= 3:
                entries.append({"time": now, "status": "LEGACY", "fp": parts[0],
                                "ticker": parts[1], "title": parts[2]})
            else:
                entries.append({"time": now, "status": "LEGACY", "fp": parts[0],
                                "ticker": "", "title": ""})
    return entries, heartbeat_date


def format_history_line(entry):
    title = entry["title"].replace("|||", " ").replace("\n", " ")
    return f"{entry['time'].isoformat(timespec='seconds')}|||{entry['status']}|||{entry['fp']}|||{entry['ticker']}|||{title}"


def append_history(entry, history_entries, seen_fps):
    history_entries.append(entry)
    seen_fps.add(entry["fp"])
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(format_history_line(entry) + "\n")


def rewrite_history(history_entries, heartbeat_date):
    """刪除超過保留天數的紀錄，並把舊格式紀錄轉成新格式"""
    cutoff = datetime.now(UTC_TZ) - timedelta(days=HISTORY_KEEP_DAYS)
    kept = [e for e in history_entries if e["time"] >= cutoff]
    tmp_file = HISTORY_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        if heartbeat_date:
            f.write(f"{HEARTBEAT_PREFIX}|||{heartbeat_date}\n")
        for e in kept:
            f.write(format_history_line(e) + "\n")
    os.replace(tmp_file, HISTORY_FILE)
    return len(history_entries) - len(kept)


# ==================== 讀取清單 ====================
def read_code_list(path):
    """讀取一行一個代號的清單；略過空行與 # 開頭的註解，去除重複但保留順序"""
    codes = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            t = line.strip().upper()
            if not t or t.startswith("#"):
                continue
            t = TICKER_CANONICAL.get(t, t)
            if t not in codes:
                codes.append(t)
    return codes


def load_holdings():
    HOLDINGS.clear()
    if not os.path.exists(HOLDINGS_FILE):
        print(f"ℹ️ 找不到 {HOLDINGS_FILE}，本次不套用持股放寬規則", flush=True)
        return []
    holdings = read_code_list(HOLDINGS_FILE)
    HOLDINGS.update(holdings)
    print(f"⭐ 持股：{'、'.join(holdings) if holdings else '（清單是空的）'}", flush=True)
    return holdings


# ==================== 週末節流機制 ====================
def should_skip_for_weekend_throttle():
    now_tw = datetime.now(TW_TZ)
    wd = now_tw.weekday()
    hr = now_tw.hour

    is_weekend = (wd == 5 and hr >= 12) or (wd == 6)
    if not is_weekend:
        return False

    if os.path.exists(WEEKEND_RUN_LOG):
        try:
            with open(WEEKEND_RUN_LOG, "r", encoding="utf-8") as f:
                last_run_ts = float(f.read().strip())
            elapsed_hours = (time.time() - last_run_ts) / 3600
            if elapsed_hours < WEEKEND_MIN_GAP_HOURS:
                remaining_hours = WEEKEND_MIN_GAP_HOURS - elapsed_hours
                print(f"⏳ [週末節流] 距上次掃描僅 {elapsed_hours:.1f} 小時，尚需冷卻 {remaining_hours:.1f} 小時，跳過。", flush=True)
                return True
        except Exception:
            pass

    with open(WEEKEND_RUN_LOG, "w", encoding="utf-8") as f:
        f.write(str(time.time()))

    print(f"🚀 [週末巡檢放行] 距離上次執行已達 {WEEKEND_MIN_GAP_HOURS:.0f} 小時，啟動本次掃描。", flush=True)
    return False


# ==================== DISCORD 推播 ====================
def post_to_discord(payload):
    """回傳 True 代表推播成功；遇到 Discord 速率限制會依指示等待後重試"""
    if not DISCORD_NEWS_WEBHOOK:
        print("      ❌ [環境變數警告] 未設定 DISCORD_NEWS_WEBHOOK", flush=True)
        return False
    for attempt in range(4):
        try:
            res = requests.post(DISCORD_NEWS_WEBHOOK, json=payload, timeout=15)
            if res.status_code == 429:
                try:
                    wait = float(res.json().get("retry_after", 2))
                except Exception:
                    wait = 2.0
                print(f"      ⏳ [Discord 速率限制] 等待 {wait:.1f} 秒後重試", flush=True)
                time.sleep(min(wait + 0.5, 30))
                continue
            if 200 <= res.status_code < 300:
                return True
            print(f"      ❌ [Discord 發送失敗] HTTP {res.status_code}", flush=True)
            time.sleep(2)
        except Exception as e:
            print(f"      ❌ [Discord 連線異常] {e}", flush=True)
            time.sleep(2)
    return False


TYPE_CONFIGS = {
    "DILUTION": {"label": "⚠️ 資本融資與稀釋警報", "color": 0xE74C3C,
                 "desc": "股權融資/稀釋（可轉債、現增、ATM）"},
    "CRISIS": {"label": "🚨 重大利空警報", "color": 0xC0392B,
               "desc": "調查/反壟斷/制裁/禁令/做空報告/財測下修/破產"},
    "M&A": {"label": "🤝 併購/重大投資", "color": 0x9B59B6,
            "desc": "收購/資產出售/重組拆分/外部注資"},
    "EARNINGS": {"label": "📊 財報/財測更新", "color": 0x3498DB,
                 "desc": "官方財報公布、財測調整或庫藏股"},
    "PRODUCT": {"label": "🚀 產品/技術發表", "color": 0x1ABC9C,
                "desc": "新產品發表 / 技術突破 / 監管核准"},
    "ORDER": {"label": "💰 訂單/合作/授權", "color": 0x2ECC71,
              "desc": "客戶合約 / 採購訂單 / 授權協議 / 合作"},
    "LEADERSHIP": {"label": "👤 高層異動", "color": 0xF1C40F,
                   "desc": "執行長、財務長等高層任命或離職"},
}


def send_news_embed(ticker, title, event_type, summary, news_url, pub_date_str, source_name, title_zh=""):
    now_tw_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
    cfg = TYPE_CONFIGS.get(event_type, TYPE_CONFIGS["ORDER"])
    holding_mark = "★ 持股｜" if ticker in HOLDINGS else ""

    if title_zh and title_zh != title:
        display_title = f"{title_zh}\n({title[:180]})"
    else:
        display_title = title[:200]

    payload = {
        "username": "Market Impact Radar",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/2965/2965879.png",
        "embeds": [{
            "title": f"{holding_mark}{cfg['label']}：{ticker}",
            "url": news_url,
            "color": cfg["color"],
            "fields": [
                {"name": "📌 標的", "value": f"`{ticker}`", "inline": True},
                {"name": "📅 發布時間 (台灣)", "value": f"`{pub_date_str}`", "inline": True},
                {"name": "🏷️ 事件性質", "value": f"`{cfg['desc']}`", "inline": True},
                {"name": "📡 來源管道", "value": f"`{source_name}`", "inline": False},
                {"name": "📰 標題", "value": display_title[:1000], "inline": False},
                {"name": "💡 重點解讀", "value": (summary or "無內容摘要")[:1000], "inline": False}
            ],
            "footer": {"text": f"Market Radar • 推播時間: {now_tw_str}"}
        }]
    }
    return post_to_discord(payload)


def send_run_summary(stats, total, is_alert):
    now_tw_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
    failed = stats["fetch_failed"]
    failed_text = "、".join(failed[:15]) + ("…" if len(failed) > 15 else "") if failed else "無"
    lines = [
        f"掃描標的：{total} 檔",
        f"抓取失敗：{len(failed)} 檔（{failed_text}）",
        f"送 AI 判讀：{stats['ai_checked']} 則",
        f"AI 判定不推：{stats['ai_pass']} 則",
        f"成功推播：{stats['pushed']} 則",
        f"AI 呼叫失敗：{stats['ai_error']} 則",
        f"超過單檔上限、留到下次判讀：{stats['ai_deferred']} 則",
        f"已達單檔每日推播上限而略過：{stats['daily_capped']} 則",
        f"推播失敗（下次重試）：{stats['push_failed']} 則",
    ]
    if stats["timed_out"]:
        lines.append(f"因執行超時未掃描：{len(stats['timed_out'])} 檔")
    if AUTO_NAMED_THIS_RUN:
        lines.append("新代號已自動產生新聞名稱：" + "；".join(AUTO_NAMED_THIS_RUN))
    if is_alert:
        title = "🚨 新聞巡檢異常：本次結果不完整"
        color = 0xC0392B
        if stats["timed_out"]:
            lines.append("執行時間過長，可能是 Google 回應變慢或限流。")
        else:
            lines.append("抓取失敗比例過高，可能是 Google 對 GitHub 雲端 IP 限流。")
    else:
        title = "✅ 新聞巡檢每日健康回報"
        color = 0x7F8C8D
        if stats["pushed"] == 0:
            lines.append("本次無重大事項。")
    payload = {
        "username": "Market Impact Radar",
        "embeds": [{
            "title": title,
            "color": color,
            "description": "\n".join(lines),
            "footer": {"text": f"Market Radar • {now_tw_str}"}
        }]
    }
    return post_to_discord(payload)


# ==================== AI 判讀 ====================
def is_reasoning_model(model):
    m = model.lower()
    return m.startswith(("gpt-5", "o1", "o3", "o4"))


def openai_chat_json(messages, temperature=0.1, attempts=3):
    """
    呼叫 OpenAI，要求回傳 JSON 文字；失敗回傳 None。
    不同模型支援的參數不同（推理型模型不接受 temperature），這裡會自動調整：
    遇到「參數不支援」的錯誤，就拿掉那個參數重試。
    """
    if not OPENAI_API_KEY:
        return None
    reasoning = is_reasoning_model(OPENAI_MODEL)
    payload = {
        "model": OPENAI_MODEL,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    if not reasoning:
        payload["temperature"] = temperature
    if OPENAI_REASONING_EFFORT and reasoning:
        payload["reasoning_effort"] = OPENAI_REASONING_EFFORT
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    timeout = 60 if reasoning else 25

    tries = 0
    while tries < attempts:
        tries += 1
        try:
            res = requests.post("https://api.openai.com/v1/chat/completions",
                                headers=headers, json=payload, timeout=timeout)
            if res.status_code == 429 or res.status_code >= 500:
                time.sleep(3 * tries)
                continue
            data = res.json()
            if "error" in data or "choices" not in data:
                err = data.get("error", data)
                msg = str(err.get("message", err) if isinstance(err, dict) else err)
                lower = msg.lower()
                removed = False
                for param in ("temperature", "reasoning_effort", "response_format"):
                    if param in payload and param in lower:
                        payload.pop(param)
                        removed = True
                        print(f"      ℹ️ [模型 {OPENAI_MODEL} 不支援 {param}] 已自動移除後重試", flush=True)
                if removed:
                    tries -= 1   # 調整參數不算一次失敗
                    continue
                print(f"      ⚠️ [OpenAI 回傳錯誤] {msg[:150]}", flush=True)
                time.sleep(2)
                continue
            return data["choices"][0]["message"]["content"] or ""
        except Exception as e:
            print(f"      ⚠️ [OpenAI 呼叫異常] {e}", flush=True)
            time.sleep(2)
    return None


def summarize_with_ai(ticker, context, recent_titles=None):
    """
    回傳 (狀態, 事件類型, 摘要, 中文標題)
    狀態：OK（放行）、PASS（不推）、ERROR（呼叫失敗，下次重試）
    """
    if not OPENAI_API_KEY:
        print("      ❌ [環境變數警告] 未設定 OPENAI_API_KEY！", flush=True)
        return "ERROR", "", "", ""

    company_name = get_display_name(ticker)
    recent_block = "\n".join(f"{i}. {t}" for i, t in enumerate((recent_titles or [])[:10], 1)) or "（無）"
    holding_note = "6. 這檔是使用者的持股，門檻可以略為放寬，但第 4 點仍然適用。\n" if ticker in HOLDINGS else ""
    prompt = f"""
你是一位嚴謹的美股買方研究員。請判讀【{ticker} - {company_name}】的這則即時消息。

【資訊限制，最重要】：
你只看得到新聞標題與一小段摘要（通常就是標題本身），看不到內文。
1. 只能寫標題或摘要中明確出現的事實與數字。嚴禁自行補充金額、比例、客戶名稱、時程或任何標題沒寫的細節。
2. 【財務影響】若標題資訊不足以判斷，請直接寫「標題未提及金額或條款」，不要推測。
3. 若依一般常識可以合理說明方向（例如發行新股會稀釋股權、取得訂單會增加營收），可以寫，但要用「可能」並點出依據。

【嚴禁句型】：
1. 嚴禁「對手方為...」、「交易/合約性質為...」等生硬套話。
2. 嚴禁「提升市場地位、增強競爭力、帶來正面影響、後市可期、具戰略意義」等空洞廢話。

【重要性門檻（最常出錯的地方，請嚴格把關）】：
只放行「會影響這家公司營收、獲利、估值或重大風險」的消息，並以公司規模衡量：
1. 超大型公司（市值數千億美元以上，例如 Google、Amazon、Microsoft、Apple、Nvidia、Meta、Tesla、Broadcom、TSMC）：
   金額低於約 10 億美元的合約、投資、授權、內容採購、行銷合作，以及一般產品功能更新，一律 PASS；
   除非涉及重大策略轉向、核心業務的大客戶，或監管、法律、出口管制等重大風險。
2. 中型公司：金額相對其年營收不顯著（例如低於年營收 2%）的消息，PASS。
3. 小型公司（市值約 50 億美元以下）：幾百萬美元的訂單、合約或融資就可能重要，可以放行。
4. 一律 PASS：慈善捐款、贊助、獎項、員工活動、非執行長或財務長的一般人事、產品小改版、別家公司只是在宣傳中提到本公司、
   與大學或研究機構的學術合作、零售商自行調降售價、分析師對未來幾年的營收比重預測。
5. 拿不準時問自己：一位專業基金經理看到這則消息，會不會因此重新檢視這檔持股？不會就 PASS。
{holding_note}
【駁回規則（命中任一條，type 一律填 PASS）】：
1. 歷史回顧：回顧上一季財報、過去幾週走勢、「Since last earnings」類文章。
2. 純股價走勢：只描述漲跌幾 %、獲利了結、大盤或板塊連動，沒有說明具體事件。
3. 評論與建議：該不該買、值得買的股票清單、分析師調整評等或目標價、產業趨勢評論、無具體內容的公關宣傳、律師集體訴訟招募。
4. 主體不符：新聞主角不是【{ticker} / {company_name}】，只是順帶提到。
   例如「台積電擴產帶動某供應商接單」的主角是供應商；「某新創被選為 Salesforce 合作夥伴」的主角是新創；這類一律 PASS。
   但如果本公司是訴訟的原告或被告、交易的買方或賣方、合約的一方，就算本公司不是標題第一個字，也算主角。
5. 已推播過的事件：對照下方「最近三天已推播過的本公司新聞」，如果這則只是同一事件的改寫、後續報導或股價反應，
   而且沒有新的實質資訊，一律 PASS。若有新的實質進展（例如傳聞變成正式宣布、交易正式完成、出現新的金額或條款），可以放行。
6. 舊聞：對照下方「今天日期」與「發布時間」，內容明顯是兩天以前的事件（例如十月才報導第二季財報結果、財報電話會議逐字稿整理），一律 PASS。

【分類守則】：
CRISIS：只限政府或監管機構調查、反壟斷、制裁或出口禁令、專利禁令、重大訴訟、做空機構報告、正式破產、官方下修財測，
以及本公司系統或客戶資料遭駭、外洩等重大資安事件（對資料、資安、雲端類公司尤其重要，就算消息尚未證實也要放行並註明）。一般股價下跌不算。
EARNINGS：只限今天或昨天剛公布的官方季度財報、財測調整、庫藏股計畫。
DILUTION：發行新股、可轉債、ATM、私募等股權融資。
M&A：收購、合併、出售資產、分拆、取得或出售大額持股。
LEADERSHIP：執行長、財務長等高層任命或離職。
ORDER：客戶合約、採購訂單、授權協議、合作。
PRODUCT：新產品、新技術發表、監管核准。

【寫財務影響前，先想清楚三件事（最容易寫錯）】：
1. 本公司在這則消息裡是「收錢的一方」還是「付錢的一方」？
   購電合約、採購、租用、投資別家公司，是本公司的支出或資本支出，不是營收；
   別家公司因為本公司擴產而接單，增加的是那家供應商的營收，不是本公司的。
2. 標題本身已經寫出財務事實時（例如上調營收預測、和解金額、增資金額、求償金額），要直接寫出來，
   並和公司規模比較輕重。例如 6 億美元和解對美光不算小但可承受；10 億英鎊訴訟對 Google 影響有限，不要誇大成「重大財務風險」。
3. 標題真的沒有足夠資訊時，只寫「標題未提及金額或條款」，不要硬湊推測。

【撰寫要求（像朋友聊天一樣自然講重點，繁體中文）】：
title_zh：把英文標題翻成繁體中文，保留型號與代號。
action：一句話白話說明發生什麼事，40 到 60 字。有對象或金額就自然帶出，沒有就不用硬湊。
impact：實質財務影響（營收、毛利、負債、稀釋），40 到 60 字，遵守上方資訊限制。

【輸出格式】：只輸出 JSON 物件，不要任何其他文字。
不符合時輸出：{{"type": "PASS"}}
符合時輸出：
{{"type": "ORDER 或 M&A 或 DILUTION 或 EARNINGS 或 PRODUCT 或 CRISIS 或 LEADERSHIP", "title_zh": "...", "action": "...", "impact": "..."}}

今天日期（台灣時間）：{datetime.now(TW_TZ).strftime("%Y-%m-%d")}

最近三天已推播過的本公司新聞：
{recent_block}

新聞快訊內容：
{context[:4500]}
"""
    messages = [
        {"role": "system", "content": "你是嚴謹的買方研究員，只根據提供的資訊說話，絕不編造細節，只輸出 JSON。"},
        {"role": "user", "content": prompt}
    ]

    for attempt in range(2):
        content = openai_chat_json(messages, temperature=0.1, attempts=3)
        if content is None:
            return "ERROR", "", "", ""
        try:
            content = content.strip()
            json_match = re.search(r"\{[\s\S]*\}", content)
            if not json_match:
                if content.upper().startswith("PASS"):
                    return "PASS", "", "", ""
                continue

            parsed = json.loads(json_match.group(0))
            event_type = str(parsed.get("type", "")).strip().upper()
            if event_type == "PASS":
                return "PASS", "", "", ""
            if event_type not in VALID_EVENT_TYPES:
                event_type = "ORDER"

            title_zh = str(parsed.get("title_zh", "")).strip()
            action = str(parsed.get("action", "")).strip()
            impact = str(parsed.get("impact", "")).strip()
            if not action or not impact:
                return "PASS", "", "", ""

            summary = f"• **【核心要點】**：{action}\n• **【財務影響】**：{impact}"
            return "OK", event_type, summary, title_zh
        except Exception as e:
            print(f"      ⚠️ [AI 回覆格式異常] {e}", flush=True)

    return "ERROR", "", "", ""


# ==================== 新聞抓取 ====================
def fetch_google_wire_news(ticker):
    """回傳 (稿件清單, 是否抓取成功)。抓取失敗與「真的沒有新聞」會分開計算。"""
    query = build_search_query(ticker)
    rss_url = f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&hl=en-US&gl=US&ceid=US:en"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }

    for attempt in range(3):
        try:
            res = requests.get(rss_url, headers=headers, timeout=15)
            if res.status_code == 429 or res.status_code >= 500:
                wait = 8 * (attempt + 1)
                print(f" ⚠️ [Google HTTP {res.status_code}] 冷卻 {wait} 秒...", end="", flush=True)
                time.sleep(wait)
                continue
            if res.status_code != 200 or not res.content:
                return [], False

            root = ET.fromstring(res.content)
            channel = root.find("channel")
            if channel is None:
                return [], False

            items = []
            for item in channel.findall("item"):
                description = item.findtext("description") or ""
                items.append({
                    "raw_title": item.findtext("title") or "",
                    "url": item.findtext("link") or "",
                    "pub_date_raw": item.findtext("pubDate") or "",
                    "source": item.findtext("source") or "Unknown",
                    "snippet": re.sub(r"<[^>]+>", " ", description).strip(),
                })
            return items, True
        except Exception:
            time.sleep(2)
    return [], False


# ==================== 巡檢邏輯 ====================
def check_and_process_ticker(ticker, seen_fps, history_entries, stats):
    wire_items, ok = fetch_google_wire_news(ticker)
    if not ok:
        stats["fetch_failed"].append(ticker)
        print("❌ 抓取失敗", flush=True)
        return
    if not wire_items:
        print("0 則稿件", flush=True)
        return

    print(f"候選稿件 {len(wire_items)} 則", flush=True)
    max_push = MAX_PUSH_PER_HOLDING if ticker in HOLDINGS else MAX_PUSH_PER_TICKER
    pushed_this_round = 0
    ai_this_round = 0
    relax = ticker in HOLDINGS and len(wire_items) < HOLDING_RELAX_MAX_CANDIDATES

    # Google 預設依相關度排序，改成最新的優先處理
    wire_items.sort(key=lambda x: parse_pub_time(x["pub_date_raw"]), reverse=True)

    for item in wire_items:
        if pushed_this_round >= max_push:
            break

        source_name = item["source"]
        source_tier = get_source_tier(source_name)
        if not source_tier:
            continue
        if not is_within_hours(item["pub_date_raw"], NEWS_WINDOW_HOURS):
            continue

        # 拿掉 Google 標題尾巴的「 - 來源名稱」，避免來源名稱被誤認成公司名稱
        clean_title = re.sub(r"\s+[\-–—]\s+[^\-–—]+$", "", item["raw_title"]).strip()
        if not clean_title or not matches_target_entity(ticker, clean_title):
            continue

        fingerprint = make_news_fingerprint(ticker, clean_title)
        if fingerprint in seen_fps:
            continue

        passed, reason = evaluate_title(ticker, clean_title, item["snippet"], source_tier, relax=relax)
        if not passed:
            continue

        if is_duplicate_news(ticker, clean_title, history_entries):
            print(f"      [同事件改寫] 略過：{clean_title[:60]}", flush=True)
            continue

        sent_24h = count_sent_last_24h(ticker, history_entries)
        soft_cap = DAILY_SOFT_CAP_HOLDING if ticker in HOLDINGS else DAILY_SOFT_CAP
        hard_cap = DAILY_HARD_CAP_HOLDING if ticker in HOLDINGS else DAILY_HARD_CAP
        if sent_24h >= hard_cap or (sent_24h >= soft_cap and reason != "強催化劑"):
            stats["daily_capped"] += 1
            continue

        if ai_this_round >= MAX_AI_PER_TICKER:
            stats["ai_deferred"] += 1
            continue
        ai_this_round += 1
        print(f"      ⚡ [{reason}] {clean_title[:60]}... 提交 AI 判讀", flush=True)
        stats["ai_checked"] += 1

        pub_tw_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
        try:
            pub_tw_str = parsedate_to_datetime(item["pub_date_raw"]).astimezone(TW_TZ).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass

        context = f"標題: {clean_title}\n來源: {source_name}\n發布時間（台灣）: {pub_tw_str}\n摘要: {item['snippet']}"
        recent_titles = recent_sent_titles(ticker, history_entries)
        status, event_type, summary, title_zh = summarize_with_ai(ticker, context, recent_titles)

        if status == "ERROR":
            stats["ai_error"] += 1
            print("      ⚠️ [AI 失敗] 不寫入紀錄，下次重試", flush=True)
            continue

        entry = {"time": datetime.now(UTC_TZ), "fp": fingerprint, "ticker": ticker, "title": clean_title}

        if status == "PASS":
            stats["ai_pass"] += 1
            entry["status"] = "PASS"
            append_history(entry, history_entries, seen_fps)
            print("      [AI 裁定] PASS", flush=True)
            continue

        print(f"      🎯 [AI 放行] {event_type}，發送 Discord...", flush=True)
        ok_push = send_news_embed(ticker, clean_title, event_type, summary, item["url"],
                                  pub_tw_str, source_name, title_zh)
        if ok_push:
            stats["pushed"] += 1
            entry["status"] = "SENT"
            append_history(entry, history_entries, seen_fps)
            pushed_this_round += 1
            print("      🎉 [推播成功]", flush=True)
            time.sleep(1)
        else:
            stats["push_failed"] += 1
            print("      ❌ [推播失敗] 不寫入紀錄，下次重試", flush=True)


# ==================== 主程式進入點 ====================
def main():
    now_tw = datetime.now(TW_TZ)
    print("==========================================", flush=True)
    print(f"🕒 當前台灣時間：{now_tw.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

    if should_skip_for_weekend_throttle():
        print("==========================================", flush=True)
        return

    if not os.path.exists("tickers.txt"):
        print("❌ 錯誤：找不到 tickers.txt 檔案！", flush=True)
        return

    watchlist = read_code_list("tickers.txt")
    holdings = load_holdings()
    # 持股排最前面優先掃描；持股就算不在 tickers.txt 也會自動加入
    tickers = holdings + [t for t in watchlist if t not in HOLDINGS]

    prepare_names_for_missing(tickers)

    history_entries, heartbeat_date = load_history()
    seen_fps = {e["fp"] for e in history_entries}
    total = len(tickers)
    stats = {"fetch_failed": [], "timed_out": [], "ai_checked": 0, "ai_pass": 0,
             "ai_error": 0, "ai_deferred": 0, "daily_capped": 0, "pushed": 0, "push_failed": 0}
    run_start = time.monotonic()

    print(f"🏛️ 啟動重大事件巡檢，清單共計：{total} 檔標的", flush=True)
    print(f"📦 歷史紀錄：{len(history_entries)} 條", flush=True)
    print("==========================================", flush=True)

    consecutive_fails = 0
    for idx, ticker in enumerate(tickers, start=1):
        print(f"[{idx:03d}/{total:03d}] 檢索標的：{ticker:5s} ... ", end="", flush=True)
        before = len(stats["fetch_failed"])
        check_and_process_ticker(ticker, seen_fps, history_entries, stats)
        consecutive_fails = consecutive_fails + 1 if len(stats["fetch_failed"]) > before else 0
        if time.monotonic() - run_start > MAX_RUN_MINUTES * 60 and idx < total:
            stats["timed_out"] = tickers[idx:]
            print(f"🛑 已執行超過 {MAX_RUN_MINUTES} 分鐘，略過剩餘 {len(stats['timed_out'])} 檔", flush=True)
            break
        if consecutive_fails >= MAX_CONSECUTIVE_FETCH_FAILS:
            remaining = tickers[idx:]
            stats["fetch_failed"].extend(remaining)
            print(f"🛑 連續 {consecutive_fails} 檔抓取失敗，判定被 Google 限流，略過剩餘 {len(remaining)} 檔", flush=True)
            break
        time.sleep(SLEEP_BETWEEN_TICKERS)

    # 健康回報：抓取失敗比例過高時立即警報；否則每天送一則
    fail_ratio = len(stats["fetch_failed"]) / total if total else 0
    today_tw = datetime.now(TW_TZ).strftime("%Y-%m-%d")
    is_alert = fail_ratio >= FETCH_FAIL_ALERT_RATIO or bool(stats["timed_out"])
    need_heartbeat = heartbeat_date != today_tw and datetime.now(TW_TZ).hour >= HEARTBEAT_HOUR_TW
    if is_alert or need_heartbeat:
        if send_run_summary(stats, total, is_alert) and need_heartbeat:
            heartbeat_date = today_tw

    removed = rewrite_history(history_entries, heartbeat_date)

    print("==========================================", flush=True)
    print(f"✅ 巡檢完成：掃描 {total} 檔，抓取失敗 {len(stats['fetch_failed'])} 檔，"
          f"送 AI {stats['ai_checked']} 則，推播 {stats['pushed']} 則，"
          f"清除過期紀錄 {removed} 條", flush=True)
    print("==========================================", flush=True)


if __name__ == "__main__":
    main()
