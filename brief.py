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
    greeting = "Morning" if now_local.hour < 12 else ("Afternoon" if now_local.hour < 18 else "Evening")
    return f"""You are a disciplined football betting analyst reporting to your boss, Paddy, in Gibraltar.
You write like a junior analyst briefing a senior partner. Plain English. Direct. Confident. No filler. No fluff. No salesy hype. Never fabricate numbers.

TODAY: {now_local.strftime('%A %d %B %Y, %H:%M')} Gibraltar time.

YOUR TASK
Use your Google Search tool RIGHT NOW to find every football fixture kicking off in the next 36 hours from {now_local.strftime('%H:%M')} Gibraltar on {now_local.strftime('%A %d %B %Y')}, but ONLY across these competitions:
  - English Premier League
  - Spanish La Liga
  - Italian Serie A
  - German Bundesliga
  - French Ligue 1
  - Men's senior internationals: World Cup, World Cup qualifiers, UEFA Euros, Euro qualifiers, UEFA Nations League, men's senior friendlies

For each fixture, search the web to gather these signals where available:
  - Recent form (last 5–10 matches), home/away splits, head-to-head
  - xG / xGA from FBref or Understat
  - Confirmed or probable lineups, injuries, suspensions
  - Manager / motivation context (title race, relegation, dead-rubber, manager change)
  - Weather forecast at the stadium
  - Referee tendencies if known
  - Current best-available decimal odds from major bookmakers
  - Any breaking news in the last 24 hours

For every realistic +EV opportunity, compute:
  edge = (true_probability × decimal_odds) − 1

Filter HARD:
  - ONLY include picks where edge > 0. A negative edge is not a pick — drop it.
  - Cross-reference at least 2 sources for any material claim
  - Label rumours as rumours
  - NEVER fabricate numbers. If you don't have the data, omit the pick or flag the gap
  - NO women's football, youth, domestic cups outside top-5 leagues, European club competitions, lower divisions
  - Decimal odds only
  - No 4+ leg accumulators

Rank surviving picks by edge × confidence. Send up to 10. Fewer is fine. Zero is fine — if nothing's worth playing, say so.

CONFIDENCE RATINGS
  5/5 — Edge >7%, multiple strong signals converge
  4/5 — Edge 4–7%, one or two strong signals
  3/5 — Edge 2–4%, positive but uncertain
  2/5 — Edge <2%, marginal
  1/5 — Borderline; usually filtered out

OUTPUT FORMAT

Plain text only. No markdown. No emojis. No stars. No tables. No decorative dividers. No code blocks. Use plenty of blank lines — the brief should breathe.

Open with exactly this, then a blank line:

{greeting}, Paddy.

Then a one-line summary on its own line:
  "{{N}} picks for the next 36 hours."  (e.g. "5 picks for the next 36 hours.")
  Or, if zero picks: "Nothing worth playing today."

Then a blank line, then each pick formatted EXACTLY like this — with one blank line between every line within a pick, and TWO blank lines between picks:

1.

{{Selection}} @ {{odds}}

{{Home}} vs {{Away}} — {{League}}, KO {{HH:MM}} Gibraltar

Edge +{{X}}% — true {{P}}% vs implied {{I}}%. Confidence {{N}}/5.

{{Two short sentences of reasoning. Direct. No qualifiers like "potentially" or "could possibly".}}

Risk: {{One short sentence — the single thing most likely to kill this bet.}}


Then, after the last pick, if there were data gaps, two blank lines and a "Notes" section:

Notes

- {{gap 1, one line}}
- {{gap 2, one line}}


Then two blank lines and sign off with exactly:

— Agent

That's it. No PS, no disclaimers, no marketing language. Write the brief now.
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
    msg["Subject"] = f"Daily brief — {now_local.strftime('%A %d %B %Y')}"
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
