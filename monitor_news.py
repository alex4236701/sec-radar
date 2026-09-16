name: Commercial News Bot

on:
  schedule:
    # 1. 盤中高頻期（涵蓋開盤、收盤及冬夏令時間）：每小時 30 分執行一次
    # 對應台灣時間 21:30 到 05:30 -> UTC 13:30 至 21:30
    - cron: '30 13-21 * * *'

    # 2. 離峰與休市時段：每 4 小時執行一次
    # 對應台灣時間 09:30, 13:30, 17:30 -> UTC 01:30, 05:30, 09:30
    - cron: '30 1,5,9 * * *'

  workflow_dispatch:

jobs:
  run-news-bot:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - name: Check out repo
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.10'

      - name: Install dependencies
        run: |
          pip install requests yfinance

      - name: Run News Monitor Script
        env:
          DISCORD_NEWS_WEBHOOK: ${{ secrets.DISCORD_NEWS_WEBHOOK }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
        run: |
          python monitor_news.py

      - name: Commit and Push Sent History
        run: |
          git config --global user.name "github-actions[bot]"
          git config --global user.email "github-actions[bot]@users.noreply.github.com"
          git pull --rebase origin main || true
          git add sent_news_log.txt || true
          git diff --quiet && git diff --staged --quiet || (git commit -m "chore: update sent news history [skip ci]" && git push origin main)
