# AI Betting Agent — v0

Sends a daily +EV football betting brief by email at ~10:00 Gibraltar time.

**Stack:** Python script + Gemini 2.5 Flash (with Google Search grounding) + Gmail SMTP + GitHub Actions cron.
**Cost:** $0/month.

## What it does (today)

Every morning the GitHub Actions cron runs `brief.py`. The script asks Gemini to:
1. Search the web for fixtures in the next 36h across the top-5 European leagues and men's internationals.
2. Pull form, xG, injuries, lineups, weather, referee notes, current odds.
3. Identify positive-EV opportunities and rank them by edge × confidence.
4. Format as a plain-text brief.

Then it emails the brief to `pmcclafferty0@gmail.com` via Gmail SMTP.

## What it does NOT do (yet)

No post-lineup updates, no in-play alerts, no interactive Q&A, no bet ledger, no learning loop. See `02_TechSpec_FootballBettingAgent.md` for the full v1 spec — those are the next things to add.

## Local setup

```bash
cd ~/Documents/AIbetting
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in keys
python brief.py        # sends a test email immediately
```

## GitHub Actions setup (one-time, required for the daily cron)

In the repo on GitHub:

1. Go to **Settings → Secrets and variables → Actions → New repository secret**.
2. Add two secrets:
   - `GEMINI_API_KEY` — your Gemini API key from https://aistudio.google.com/apikey
   - `SMTP_APP_PASSWORD` — the 16-char Gmail App Password (spaces optional)
3. The workflow runs at 08:00 UTC daily. Adjust `.github/workflows/morning-brief.yml` if you want a different time.
4. To trigger a manual run: **Actions tab → Morning Brief → Run workflow**.

## Gmail deliverability

After the first run, if the email lands in Promotions or Spam:
- Mark it "Not spam" / move to Primary tab
- Add `pmcclafferty0@gmail.com` to your Contacts (it's both sender and recipient here)
- Create a filter: `from:pmcclafferty0@gmail.com subject:"Daily Brief"` → never send to spam, mark important
