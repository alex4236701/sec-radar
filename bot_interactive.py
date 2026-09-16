import os
import re
import requests
import discord
from discord import app_commands
from discord.ext import commands

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")

SEC_HEADERS = {
    "User-Agent": "InstitutionalAlphaResearch/2.0 (compliance@alpharesearch.org)",
    "Accept-Encoding": "gzip, deflate"
}

# 鎖定 5 大核心實質申報
# 1.01(重大商業合約) | 2.01(併購與資產處分) | 2.02(季報/財報) | 3.02/3.03(股權融資稀釋) | 8.01(實質重大揭露)
TARGET_ITEMS = {"1.01", "2.01", "2.02", "3.02", "3.03", "8.01"}
IGNORE_ITEMS = {"5.02", "5.07"}  # 排除人事更迭與股東會日程

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

def get_cik(ticker):
    url = "https://www.sec.gov/files/company_tickers.json"
    try:
        res = requests.get(url, headers=SEC_HEADERS, timeout=10)
        if res.status_code == 200:
            for item in res.json().values():
                if item["ticker"].upper() == ticker.upper():
                    return str(item["cik_str"]).zfill(10)
    except Exception:
        pass
    return None

def fetch_target_5_filings(cik):
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        res = requests.get(url, headers=SEC_HEADERS, timeout=10)
        if res.status_code != 200:
            return []
        
        recent = res.json().get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accs = recent.get("accessionNumber", [])
        dates = recent.get("filingDate", [])
        docs = recent.get("primaryDocument", [])
        items_list = recent.get("items", [])

        matched = []
        for i in range(len(forms)):
            form = forms[i]
            items = items_list[i] if i < len(items_list) else ""
            item_set = set(re.findall(r"\d+\.\d+", items))

            # 抓取 10-Q/10-K 正式財報，或符合 5 大項目的 8-K
            if form in ["10-Q", "10-K"] or (form == "8-K" and item_set and not item_set.issubset(IGNORE_ITEMS) and (item_set & TARGET_ITEMS)):
                matched.append({
                    "form": form,
                    "accessionNumber": accs[i],
                    "filingDate": dates[i],
                    "primaryDoc": docs[i],
                    "items": items
                })
            if len(matched) >= 5:
                break
        return matched
    except Exception:
        return []

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"🤖 機器人已上線：{bot.user.name}，斜線指令已同步完成！", flush=True)

@bot.tree.command(name="audit", description="即時調閱該公司最近 5 筆真正有用的 SEC 核心實質申報")
@app_commands.describe(ticker="股票代號 (例如: RKLB, ONTO, NVDA)")
async def audit(interaction: discord.Interaction, ticker: str):
    ticker = ticker.upper()
    await interaction.response.defer(thinking=True)

    cik = get_cik(ticker)
    if not cik:
        await interaction.followup.send(f"❌ 查無代號 `{ticker}` 的 SEC CIK 登記資料。")
        return

    filings = fetch_target_5_filings(cik)
    if not filings:
        await interaction.followup.send(f"⚠️ `{ticker}` 近期查無符合 5 大核心類別（大單/併購/財報/稀釋）的實質申報。")
        return

    embed = discord.Embed(
        title=f"📋 SEC 核心實質申報檢索：{ticker}",
        description="已鎖定最近 5 筆實質申報（排除純人事與股東會日程）：",
        color=0x3498DB
    )

    for idx, f in enumerate(filings, start=1):
        acc_clean = f["accessionNumber"].replace("-", "")
        doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{f['primaryDoc']}"
        items_desc = f" (項目代碼: {f['items']})" if f['items'] else ""
        
        embed.add_field(
            name=f"{idx}. 表單 {f['form']} • {f['filingDate']}",
            value=f"項目：`{f['form']}{items_desc}`\n🔗 [點此開啟 SEC 原始文件]({doc_url})",
            inline=False
        )

    await interaction.followup.send(embed=embed)

if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("❌ 錯誤：尚未設定 DISCORD_BOT_TOKEN 環境變數！")
    else:
        bot.run(DISCORD_BOT_TOKEN)
