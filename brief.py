"""Daily Football Betting Intelligence Agent — v0.

Generates a +EV betting brief for the next 36 hours and emails it to the operator.
Uses Gemini 2.5 Flash with Google Search grounding as the single research engine.
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types

TZ = ZoneInfo("Europe/Gibraltar")
MODEL = "gemini-2.5-flash"


def build_prompt(now_local: datetime) -> str:
    return f"""You are a disciplined, quant-minded football betting research agent.
You report to one operator: Paddy in Gibraltar. You write in plain English, take positions, and never fabricate numbers.

TODAY: {now_local.strftime('%A %d %B %Y, %H:%M')} Gibraltar time.

YOUR TASK
Use your Google Search tool RIGHT NOW to find every football fixture kicking off in the next 36 hours from {now_local.strftime('%H:%M')} Gibraltar on {now_local.strftime('%A %d %B %Y')}, but ONLY across these competitions:
  - English Premier League
  - Spanish La Liga
  - Italian Serie A
  - German Bundesliga
  - French Ligue 1
  - Men's senior internationals: World Cup, World Cup qualifiers, UEFA Euros, Euro qualifiers, UEFA Nations League, men's senior friendlies

For each fixture, search the web to gather as many of these signals as you can:
  - Recent form (last 5–10 matches), home/away splits, head-to-head
  - xG / xGA from FBref or Understat
  - Confirmed or probable lineups, injuries, suspensions
  - Manager / motivation context (title race, relegation, dead-rubber risk, recent manager change)
  - Weather forecast at the stadium
  - Referee tendencies if the referee is known
  - Current best-available decimal odds from major bookmakers (Bet365, William Hill, Pinnacle, Betfair Exchange)
  - Any breaking news in the last 24 hours

For every realistic positive expected value opportunity you find, compute:
  edge = (true_probability × decimal_odds) − 1

Then filter HARD:
  - ONLY include picks where edge > 0
  - Cross-reference at least 2 sources for any material claim (injuries, lineups)
  - Label rumours as rumours
  - NEVER fabricate numbers. If you don't have data, say so explicitly
  - NO women's football, youth, domestic cups outside the top-5 leagues, European club competitions, lower divisions
  - Decimal odds only (e.g. 2.50x). Never fractional or American
  - No 4+ leg accumulators in the daily top picks

Rank surviving picks by edge × confidence and select the top 10. If you find fewer than 10 +EV opportunities, send fewer. If you find zero, send a "no-bet day" brief. Celebrate restraint; never invent bets to fill a quota.

CONFIDENCE RATINGS
  5/5 — Edge >7%, multiple strong signals converge
  4/5 — Edge 4–7%, one or two strong signals
  3/5 — Edge 2–4%, math is positive but uncertain
  2/5 — Edge <2%, marginal
  1/5 — Borderline; usually filtered out

OUTPUT FORMAT (plain text email body — no markdown, no HTML, no code fences)

🏆 DAILY BRIEF — {now_local.strftime('%A, %d %B %Y').upper()}
🕙 Generated {now_local.strftime('%H:%M')} Gibraltar | Window: next 36h

──────────────────────────
🎯 TODAY'S TOP <N> PICKS
(ranked by edge × confidence)

#1 — <HOME> vs <AWAY> (<LEAGUE>)
KO <HH:MM> Gibraltar
PICK: <Market> <Selection> @ <odds>x
True prob: <X>% | Implied: <Y>% | Edge: +<Z>%
Confidence: <★★★★☆ 4/5>
Why: <2–3 plain-English sentences>
Watch: <one risk factor that would invalidate this>

#2 — ...
[repeat for each pick]

──────────────────────────
ℹ️ DATA NOTES
<List any signals or sources you could not access — be honest about gaps>

— The Agent

Write the brief now. Plain text only. Calm confidence, no hedging filler, no greeting.
"""


def generate_brief() -> str:
    api_key = os.environ["GEMINI_API_KEY"]
    now_local = datetime.now(TZ)
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=MODEL,
        contents=build_prompt(now_local),
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.4,
        ),
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty response")
    return text


def send_email(body: str) -> None:
    sender = os.environ["EMAIL_FROM"]
    recipient = os.environ["EMAIL_TO"]
    password = os.environ["SMTP_APP_PASSWORD"].replace(" ", "")
    now_local = datetime.now(TZ)

    msg = EmailMessage()
    msg["Subject"] = f"🏆 Daily Brief — {now_local.strftime('%A, %d %B %Y')}"
    msg["From"] = sender
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="aibettingagent.local")
    msg.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls(context=context)
        server.login(sender, password)
        server.send_message(msg)


def main() -> int:
    try:
        body = generate_brief()
    except Exception as exc:
        print(f"Failed to generate brief: {exc}", file=sys.stderr)
        return 1

    try:
        send_email(body)
    except Exception as exc:
        print(f"Failed to send email: {exc}", file=sys.stderr)
        print("\n--- Brief that failed to send ---\n", file=sys.stderr)
        print(body, file=sys.stderr)
        return 2

    print(f"Brief sent to {os.environ['EMAIL_TO']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
