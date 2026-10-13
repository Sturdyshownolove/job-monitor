# 🤖 Germany Job Monitor — Async Job Scraper & Telegram Bot

An asynchronous, fault-tolerant job scraper and monitoring pipeline built with **Python 3.12+**, **Playwright**, **aiohttp**, **SQLAlchemy (asyncio)**, and **Telegram Bot notifications**.

## 🚀 Key Features
- **Multi-Source Scraping:** Indeed, LinkedIn, StepStone, and RemoteRocketship.
- **Smart Filtering:** Keyword matching, deduplication (content hash + URL), and German/English language requirement analyzer.
- **Resilience:** Retry policies with exponential backoff and jitter, rate limiting, and user-agent rotation.
- **Telegram Alerts:** Instant notifications with direct job application links.
- **Web UI & Netlify Sync:** Lightweight dashboard for reviewing discovered positions.
- **Obsidian Architecture Vault:** Complete architectural documentation included in `JobMonitor-Obsidian-Vault/`.

## ⚙️ Quick Start
1. Navigate to the `project/` directory:
   ```bash
   cd project
   ```
2. Install dependencies (using [uv](https://github.com/astral-sh/uv) or pip):
   ```bash
   uv sync
   # or: pip install -e .
   ```
3. Copy environment configuration and enter your tokens:
   ```bash
   cp .env.example .env
   ```
4. Run the scraper service:
   ```bash
   python app.py
   ```


## 📄 License
Copyright © 2026 Anıl Aygün. All rights reserved.
See [LICENSE](LICENSE) for terms of use.
