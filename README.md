# StoltBot

A Discord bot that runs a small online store and a household, built in Python with
[discord.py](https://discordpy.readthedocs.io/), WooCommerce's REST API and Google Apps Script.
It runs 24/7 on a small hosted server and is used every day by a real handmade diamond art shop.

## What it does

### 🛍️ Online store automation (WooCommerce)
- **New-order alerts**: posts every paid order in a private channel within a minute
- **`/addkit`** and a **photo listing helper**: drop a product photo in Discord, the bot cleans it up
  (rotates, trims, resizes) and creates a draft product on the website
- **Sold-out cleanup**: one-of-a-kind items are marked sold, pulled from Google Shopping, and announced
- **Google Shopping watchdog**: daily check for disapproved products, with Google's suggested fixes
- **Weekly sales summary** with sales tax collected and a CSV export for bookkeeping
- **Social poster** (optional): new products are shared to a Facebook Page automatically

### 💸 Personal finance (Google Sheets + Apps Script)
- `/spent`, `/income`, `/balance`, `/budget`, `/bills`: a budget that lives in a Google Sheet
- Bill reminders, a monthly "where the money went" report, and a **free-to-spend** number
- Grocery list, pantry inventory with low-stock restocking, meal planner, and store price tracker
- Vacation planner: savings goal, countdown, flights with automatic flight length and layovers, hotels and bookings

### 🔐 Study tools
- Daily CompTIA Security+ practice question, `/quiz`, and `/exam` practice exams
- Score tracking by domain, streaks, weakest-area practice, and review of missed questions

### 📄 Resume builder
- `/resume ...`: anyone in the server builds a resume step by step with pop-up forms, privately
- Download it as a **PDF** in a Modern or Classic style, with a `/resume check` that flags weak bullets and missing info
- Backup and restore with `/resume export` / `/resume import`

### 📧 Email automation (Apps Script)
- `gmail_bot.gs`: labels and archives personal mail, sends a daily summary
- `store_email_bot.gs`: sorts customer email, flags what needs a reply, drafts answers to common questions

## Tech
Python 3.11 · discord.py (slash commands, buttons, modals, scheduled tasks) · aiohttp · Pillow · fpdf2 ·
WooCommerce REST API · Google Apps Script (web app, time-based triggers, Gmail, Sheets) · Meta Graph API

## Security
No keys or passwords are stored in the code. Each connection reads its credentials from a local file
that is excluded by `.gitignore` (see `config-examples/` for the format), and the Apps Script web app
checks a shared secret kept in Script Properties. Admin-only commands use Discord permissions,
and personal replies are private ("only you can see this").

## Setup (short version)
1. Create a bot at the Discord Developer Portal; turn on the Server Members and Message Content intents.
2. `pip install -r requirements.txt`
3. Copy the files in `config-examples/` next to `discord_bot.py`, remove `.example` from the names, and fill them in.
   Only `token.txt` is required; each other file turns on its features.
4. `python discord_bot.py`
5. For the Google Sheets features, paste `google-apps-script/budget_bot.gs` into a sheet's Apps Script,
   run `setup`, add `SECRET` in Script Properties, and deploy it as a web app.

## About
Built by Paul Stolt as a hands-on project while learning Python, automation and security.
Available for freelance work: Discord bots, WooCommerce automations and Google Sheets tools.
