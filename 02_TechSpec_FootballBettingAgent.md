# 🛠 Technical Architecture & Implementation Blueprint
## Daily Football Betting Intelligence Agent

**Version:** 1.1
**Companion to:** `01_PRD_FootballBettingAgent.md`
**Status:** Approved for Build
**Last Updated:** 15 May 2026

> **Changelog v1.0 → v1.1:** Primary delivery channel changed from WhatsApp to email. WhatsApp Business Cloud API removed. Cloudflare Worker removed. Inbound interactive Q&A migrated to IMAP polling of a dedicated Gmail inbox. Deployment plan simplified (1–3 day Meta verification step eliminated). Environment variables updated.

---

## 1. System Overview

The agent is a serverless, event-driven Python application orchestrated across two runtime environments:

1. **Scheduled jobs** — GitHub Actions cron triggers for the 10:00 brief, 09:30 reflection, lineup polling, conditional watchers, in-play monitoring, and inbound email polling.
2. **Persistent state** — Supabase (free tier) hosts the database, the bet ledger, the conversation memory, the reflection memos, and the configuration tables.

The "brain" — all reasoning, synthesis, and natural-language generation — is performed by Google Gemini's free-tier API (with Groq-hosted Llama 3.3 70B as a hot fallback). All data acquisition is via free APIs, free-tier APIs, and disciplined web scraping with strict rate limiting. All output and inbound is via **email** (SMTP for sending, IMAP for receiving), using a dedicated Gmail account the agent owns.

This architecture has no always-on services — every component is either a scheduled job or a polled inbox. That keeps it within free-tier limits on every dependency.

---

## 2. Architecture Diagram

```mermaid
graph TB
    subgraph Triggers["⏰ Triggers — GitHub Actions Cron"]
        T1[10:00 daily — Morning Brief]
        T2[09:30 daily — Reflection]
        T3[every 60s in match window — Lineup Poll]
        T4[every 5min — Conditional Watchers]
        T5[every 60s — IMAP Inbound Poll]
        T6[every 60s during live matches — In-Play]
    end

    subgraph Core["🧠 Agent Core"]
        ORC[Orchestrator]
        ING[Ingestion Layer]
        REAS[Reasoning Layer<br/>Gemini → Groq Llama fallback]
        MEM[Memory Layer]
    end

    subgraph Data["📊 Data Sources — All Free"]
        BFAIR[Betfair Exchange API<br/>delayed prices]
        FBREF[FBref scraper<br/>xG, advanced stats]
        UND[Understat scraper<br/>xG, shot data]
        RSS[RSS Aggregator<br/>BBC, Sky, marca, kicker, L'Équipe, Gazzetta, The Athletic]
        SEARCH[Web Search<br/>fallback for breaking news]
        ODDS[Public Odds Comparison<br/>scraped politely]
        WEATHER[OpenWeather Free Tier]
        REF[Referee Data<br/>scraped from official league sites]
    end

    subgraph Storage["💾 Supabase Free Tier"]
        DB[(Postgres DB<br/>500MB)]
        TABLES[fixtures<br/>bets<br/>reflections<br/>conversations<br/>data_cache<br/>incidents<br/>config]
    end

    subgraph EmailIO["📧 Email I/O — Gmail"]
        SMTP[SMTP send<br/>via Gmail App Password]
        IMAP[IMAP poll<br/>paddybetting.agent@gmail.com]
    end

    subgraph Recipients["📬 Recipients"]
        PRIMARY[pmcclafferty0@gmail.com<br/>PRIMARY]
        FALLBACK[paddy@roitips.com<br/>FALLBACK]
    end

    T1 --> ORC
    T2 --> ORC
    T3 --> ORC
    T4 --> ORC
    T5 --> ORC
    T6 --> ORC

    ORC --> ING
    ING --> BFAIR
    ING --> FBREF
    ING --> UND
    ING --> RSS
    ING --> SEARCH
    ING --> ODDS
    ING --> WEATHER
    ING --> REF

    ING --> MEM
    MEM <--> DB
    ORC --> REAS
    REAS --> MEM
    REAS --> ORC

    ORC --> SMTP
    SMTP --> PRIMARY
    SMTP -.failure.-> FALLBACK

    IMAP --> ORC
```

---

## 3. Component Breakdown

### 3.1 The Orchestrator
- **File:** `agent/orchestrator.py`
- **Role:** Top-level controller. Each trigger calls one of its entry points: `run_morning_brief()`, `run_reflection()`, `run_lineup_check(fixture_id)`, `run_conditional_check()`, `run_inbound_poll()`, `run_inplay_check(fixture_id)`.
- **Pattern:** State machine per workflow. Idempotent — same inputs produce same outputs; safe to retry.
- **Concurrency:** Single-process. Workflows are serialized via Postgres advisory locks to prevent double-fires (GitHub Actions occasionally double-schedules).

### 3.2 The Ingestion Layer
- **Directory:** `agent/ingestion/`
- **Modules:** one per data source, each implementing a common interface:
  ```python
  class DataSource(Protocol):
      name: str
      reliability_score: int  # 1–5, for source-priority resolution

      async def fetch(self, fixture_id: str, **kwargs) -> SourceResult: ...
      async def health_check(self) -> bool: ...
  ```
- **Caching:** Every ingestion call is cached in the `data_cache` table with TTLs appropriate to volatility (lineups: 5 min; xG totals: 24h; weather: 1h; H2H: 7d).
- **Rate limiting:** Per-source token-bucket limiter persisted in a `rate_limits` Supabase table. FBref capped at 10 req/min; Understat at 15 req/min; RSS feeds polled every 15 min max.

### 3.3 The Reasoning Layer
- **File:** `agent/reasoning/llm_client.py`
- **Primary model:** Google Gemini 2.5 Flash (free tier — 1500 req/day at writing).
- **Fallback:** Groq-hosted Llama 3.3 70B (free tier — generous rate limits).
- **Tertiary:** If both fail, the agent emits a degraded brief with cached analysis + the day's reflection memo + a "Reasoning offline" header.
- **Prompt management:** All prompts are stored as version-controlled templates in `agent/prompts/`. See §6 for the full operational system prompt.
- **Token discipline:** Each daily morning brief should consume ≤80k input tokens total. To stay within this:
  - Pre-summarize raw data into structured JSON before LLM ingestion
  - Only feed the top-N candidate fixtures into the deep-reasoning pass (decide top-N candidates via a cheap first-pass scoring)
  - Cache reasoning artifacts where applicable

### 3.4 The Memory Layer
- **File:** `agent/memory/store.py`
- **Backing store:** Supabase Postgres.
- **Key responsibilities:**
  - Persist every pick + outcome to the bet ledger
  - Persist all email conversation turns (inbound + outbound)
  - Persist daily reflection memos
  - Provide retrieval: "last 30 days of bets in Bundesliga BTTS markets," "all conversations referencing Arsenal," etc.
  - Provide context-window assembly: load the bias-correction memo + relevant historical priors before each LLM call

### 3.5 The Output Layer (SMTP)
- **File:** `agent/output/email_sender.py`
- **Send flow:**
  1. Build `EmailMessage` with appropriate headers — `Subject`, `From`, `To`, `Importance`, `X-Priority`, `Message-ID`, `In-Reply-To` (for threaded replies)
  2. Connect to Gmail SMTP (`smtp.gmail.com:587`) via STARTTLS using app password
  3. Send to **pmcclafferty0@gmail.com**
  4. On 2xx → log delivery, done
  5. On failure → exponential backoff (5s, 15s, 60s)
  6. After 3rd failure → re-send to **paddy@roitips.com**
  7. If both fail → write to `incidents` table, queue for next-tick retry
- **Sender identity:** All outbound originates from the dedicated agent Gmail account (e.g. `paddybetting.agent@gmail.com`). The `From` field is consistent for deliverability and inbox-filter recognition.

### 3.6 The Inbound Layer (IMAP)
- **File:** `agent/output/email_receiver.py` (and `agent/workflows/interactive.py`)
- **Runtime:** GitHub Actions cron job runs every 60 seconds (`cron: */1 * * * *`) — wakes up, polls IMAP, processes any new messages, terminates.
- **Flow:**
  1. Connect to Gmail IMAP (`imap.gmail.com:993`) via SSL using app password
  2. Select `INBOX`, search for `UNSEEN` messages
  3. For each new message:
     - Parse headers (`From`, `Subject`, `Date`, `In-Reply-To`, `References`)
     - Filter out: self-addressed, no-reply, automated bounces
     - Persist to `conversations` table as inbound
     - If sender is the operator AND within 09:00–23:00 → trigger `handle_inbound_message()`
     - If outside hours → flag as queued
  4. Mark as `\Seen`
  5. Terminate
- **Threading preservation:** When the agent replies, it sets `In-Reply-To: {original Message-ID}` and `References: {original References + Message-ID}` so Gmail correctly threads the conversation.
- **Latency:** With a 60-second poll, median user-message → agent-reply latency is ~1–3 minutes (poll wait + processing + send). Within the §12 KPI target of <3 minutes.

---

## 4. Data Source Specifications

### 4.1 Betfair Exchange (Primary Odds Source)

| Field | Value |
|---|---|
| Endpoint | `https://api.betfair.com/exchange/betting/json-rpc/v1` |
| Authentication | App Key + session token (Betfair account required) |
| Cost | Free for delayed data. £299 one-time for live app key — **out of scope for $0 budget**. |
| Rate limit | 5 requests/sec |
| What we use | Football event listing, market catalogue, runner books, last-traded-price |
| Notes | Exchange prices reflect "true" sharp consensus better than bookmaker prices. Use Betfair-implied prob as a primary input to the +EV calc. |

### 4.2 FBref (Advanced Stats)

| Field | Value |
|---|---|
| URL pattern | `https://fbref.com/en/squads/{team_id}/...` |
| Auth | None — public site |
| Cost | Free, but aggressive scraping triggers blocks |
| Rate limit (self-imposed) | 1 req per 6 seconds; respect their robots.txt; cache aggressively |
| What we use | Team-level xG, xGA, set-piece stats, possession metrics, player minutes |
| Resilience | FBref occasionally rotates HTML structure. Maintain a `parsers/fbref/` directory with version-pinned selectors. Alert on parse failure. |

### 4.3 Understat (xG, Shot Data)

| Field | Value |
|---|---|
| URL pattern | `https://understat.com/match/{match_id}`, `https://understat.com/team/{team}` |
| Auth | None |
| Cost | Free |
| Rate limit (self-imposed) | 1 req per 4 seconds |
| What we use | Shot-level xG, expected goals timelines, fixture xG totals |
| Notes | Covers EPL, La Liga, Bundesliga, Serie A, Ligue 1, RPL. **Internationals not covered** — flag as gap. |

### 4.4 RSS / News Sources

The following are polled every 15 minutes via Python `feedparser`:

| Source | Feed URL | League coverage | Reliability score (1–5) |
|---|---|---|---|
| BBC Sport Football | `feeds.bbci.co.uk/sport/football/rss.xml` | EPL, internationals | 5 |
| Sky Sports Football | `skysports.com/rss/12040` | EPL | 5 |
| The Athletic (free items) | `theathletic.com/feed/` | All | 5 |
| ESPN FC | `espn.com/espn/rss/soccer/news` | All | 4 |
| Marca | `marca.com/rss/futbol.xml` | La Liga | 5 |
| AS | `as.com/rss/futbol/futbol.xml` | La Liga | 4 |
| Kicker | `kicker.de/news/aktuell/rss.xml` | Bundesliga | 5 |
| Gazzetta dello Sport | `gazzetta.it/rss/Calcio.xml` | Serie A | 5 |
| L'Équipe | `lequipe.fr/rss/actu_rss_Football.xml` | Ligue 1 | 5 |
| Goal.com | `goal.com/feeds/news?fmt=rss` | All | 3 |

**Source-priority resolution** for the §11 PRD rule:
When sources contradict, trust in this order: (1) official club channel, (2) official league channel, (3) the highest-reliability outlet's reporting from the relevant country, (4) earliest-publishing reliable outlet. After 10 minutes post-lineup publication, if two outlets at reliability 5 still contradict, downgrade the pick's confidence by 1 level.

### 4.5 Web Search Fallback

For anything not covered by the above (e.g., a specific manager press conference, a breaking story not yet on RSS), the agent uses the **Brave Search API free tier** (2,000 queries/month) or **DuckDuckGo HTML scraping** as ultimate fallback.

### 4.6 Weather

| Field | Value |
|---|---|
| Provider | OpenWeather (free tier — 1,000 calls/day) |
| Endpoint | `api.openweathermap.org/data/2.5/forecast` |
| What we use | 24h, 6h, 1h forecast for stadium coordinates |
| Stadium coords table | Pre-populated in `config_stadiums` table — one-time setup |

### 4.7 Referee Data

Scraped from each league's official site (e.g., `premierleague.com`, `laliga.com`). Stored in `config_referees` with rolling stats: cards/game, penalties/game, fouls/game, team-specific record.

### 4.8 Public Odds Comparison

For non-Betfair bookmaker prices, the agent uses publicly-available odds-comparison feeds (e.g., the free APIs published by **OddsPortal-equivalent** aggregators, or polite scraping of comparison sites that don't prohibit it in their ToS). **No scraping of individual bookmaker price pages**, since most explicitly prohibit this in their ToS.

---

## 5. Database Schema

All tables in Supabase Postgres. Schema designed for the $0 budget (compact, no oversized columns).

```sql
-- Configuration & static reference data
create table config_stadiums (
  team_name text primary key,
  league text not null,
  stadium_name text not null,
  latitude double precision not null,
  longitude double precision not null,
  surface text default 'natural',
  capacity int
);

create table config_referees (
  referee_id text primary key,
  full_name text not null,
  league text,
  cards_per_game_30d numeric(4,2),
  pens_per_game_30d numeric(4,2),
  last_updated timestamptz
);

create table config_elite_clubs (
  team_name text primary key,
  added_at timestamptz default now()
);

-- Fixture & match data
create table fixtures (
  fixture_id text primary key,         -- our canonical ID (e.g., 'epl-2026-05-15-arsmci')
  home_team text not null,
  away_team text not null,
  league text not null,
  kickoff_utc timestamptz not null,
  referee_id text references config_referees(referee_id),
  status text default 'scheduled',     -- scheduled | in_play | finished | postponed
  home_score int,
  away_score int,
  last_updated timestamptz default now()
);

-- Bet ledger — the heart of the learning loop
create table bets (
  bet_id uuid primary key default gen_random_uuid(),
  created_at timestamptz default now(),
  source text not null,                -- 'agent_pick' | 'user_bet'
  fixture_id text references fixtures(fixture_id),
  market text not null,
  selection text not null,
  odds_at_recommendation numeric(7,3) not null,
  true_probability_estimate numeric(5,4),  -- 0.0000 to 1.0000
  implied_probability numeric(5,4),
  edge numeric(6,4),                       -- e.g., 0.0834 = 8.34%
  confidence smallint check (confidence between 1 and 5),
  reasoning text not null,
  data_sources_used jsonb,                  -- ["betfair","fbref","rss:bbc"]
  data_gaps jsonb,                          -- ["xg-bundesliga-blocked"]
  outcome text,                             -- 'win' | 'loss' | 'push' | 'void' | null
  outcome_logged_at timestamptz,
  pnl_units numeric(7,3),                   -- assuming 1u stake
  post_mortem_note text
);
create index idx_bets_fixture on bets(fixture_id);
create index idx_bets_source on bets(source);
create index idx_bets_created on bets(created_at desc);

-- Reflection memos — daily learning artifacts
create table reflections (
  reflection_date date primary key,
  generated_at timestamptz default now(),
  calibration_json jsonb not null,         -- the calibration table for the day
  bucket_performance_json jsonb not null,  -- ROI by league/market/etc
  memo_text text not null                  -- the LLM-generated bias correction memo
);

-- Email conversation history (inbound + outbound)
create table conversations (
  message_id uuid primary key default gen_random_uuid(),
  occurred_at timestamptz default now(),
  direction text not null,                 -- 'inbound' | 'outbound'
  channel text not null default 'email',   -- 'email' (v1) | future channels
  email_message_id text,                   -- the RFC 5322 Message-ID for threading
  email_in_reply_to text,                  -- the In-Reply-To header value
  email_subject text,
  body text not null,
  related_fixture_id text references fixtures(fixture_id),
  related_bet_id uuid references bets(bet_id),
  metadata jsonb
);
create index idx_conv_occurred on conversations(occurred_at desc);
create index idx_conv_message_id on conversations(email_message_id);

-- Cached source data — TTL-managed
create table data_cache (
  cache_key text primary key,              -- e.g., 'fbref:team:arsenal:xg:2026-05-14'
  source text not null,
  fetched_at timestamptz default now(),
  ttl_seconds int not null,
  payload jsonb not null
);

-- Incident log
create table incidents (
  incident_id uuid primary key default gen_random_uuid(),
  occurred_at timestamptz default now(),
  severity text not null,                  -- 'info' | 'warning' | 'error' | 'critical'
  component text not null,                 -- 'ingestion.fbref', 'output.email', etc.
  description text not null,
  resolved_at timestamptz
);

-- Rate limiter state
create table rate_limits (
  source text primary key,
  last_call_at timestamptz,
  tokens_remaining int
);
```

---

## 6. The Agent's Operational System Prompt

This is the **system prompt fed to the LLM** on every reasoning call. It is the single most important artifact in this build — it defines the agent's personality, discipline, and constraints.

```
# IDENTITY

You are the Daily Football Betting Intelligence Agent — a disciplined,
quant-minded research assistant whose sole job is to identify positive
expected value (+EV) betting opportunities in men's football across the
top 5 European leagues and international fixtures.

You report to ONE operator: Paddy, based in Gibraltar. You speak to him
through email. You are not a chatbot. You are a sparring partner.

# YOUR JOB

For any given fixture or set of fixtures, you:

1. Synthesize every available signal — form, xG, lineups, injuries,
   weather, referee, market movement, contextual factors, sentiment.
2. Estimate the true probability of relevant outcomes.
3. Compare against bookmaker implied probability.
4. Flag opportunities where edge = (true_prob × decimal_odds) − 1 > 0.
5. Rank flagged opportunities by edge × confidence.
6. Write tight, plain-English reasoning that Paddy can scan in 10 seconds.

# HARD CONSTRAINTS — NEVER VIOLATE

- NEVER recommend a bet with negative expected value.
- NEVER recommend martingale, loss-chasing, or "double-up" strategies.
- NEVER feature 4+ leg accumulators as primary daily picks.
- NEVER act on a single unverified source for material claims.
  Cross-reference at least 2 sources. Label rumors as rumors.
- NEVER fabricate statistics. If data is missing, say "I don't have
  current data on X" — explicitly, every time.
- NEVER recommend bets on women's football, youth competitions, or
  domestic cup competitions (v1 scope).
- NEVER use fractional or American odds. Decimal only (e.g., 2.50x).
- NEVER place bets. You only recommend; Paddy executes.

# YOUR PERSONALITY

- Conversational, not corporate. Use "I" naturally.
- Confident but never overconfident. Calibrated.
- Willing to disagree with Paddy. Sycophancy is a bug.
- Plain English over jargon. Numbers when they matter, not for show.
- Honest about uncertainty: "I'm 50/50 on this — here's why" beats fake
  certainty every time.
- Dry humor permitted. Excessive cheerfulness is forbidden.

# CONFIDENCE RATINGS

You assign a confidence rating to every pick:
- 5/5: I would bet this myself, large stake. Multiple strong signals
  converge. Edge >7%.
- 4/5: Strong conviction. Edge 4–7%. One or two signals provide most
  of the case.
- 3/5: Moderate. Edge 2–4%. The math is positive but I'm not certain.
- 2/5: Marginal. Edge <2%. Including only if I have nothing stronger.
- 1/5: Borderline. Usually filtered out — included only if explicitly
  asked.

You ARE calibrated against your own history. Before assigning a rating,
silently consult the calibration table loaded into your context. If your
4/5 picks have been hitting at 50% lately when they should hit at 70%,
DAMPEN your rating. Be honest with yourself.

# THE BIAS-CORRECTION MEMO

At the start of every analysis, you have been given a "Bias Correction
Memo" reflecting what you've gotten wrong recently. Apply it. If the
memo says "you've been over-confident on Bundesliga BTTS," dampen those
specifically.

# DATA TRANSPARENCY

If a data source was unavailable during your analysis (e.g., FBref
rate-limited), say so in the brief footer. Never paper over gaps with
guesses.

When you cite a stat, the source should be inferable. Example:
"Inter's xG over their last 10 home games is 2.3 (Understat) vs.
opponent's xGA of 1.6 — that's a meaningful mismatch."

# OUTPUT FORMATS

You will be invoked in one of several modes. Match the format exactly:

[MODE: morning_brief]
Output the format defined in PRD §8.1. Pre-pended by yesterday's recap
and the bias-correction note. Top 10 picks (or fewer if not enough +EV).
Output will be sent as an email — write plain text suitable for an
email body. No HTML.

[MODE: post_lineup]
Output PRD §8.2 format. Single fixture. CONFIRMED / REVISED / RETRACTED
/ NEW.

[MODE: conditional_alert]
Output PRD §8.3 format. Short and surgical. This is a high-importance
email — keep the body skimmable.

[MODE: inplay]
Output PRD §8.4 format. Time-sensitive. Note validity window. Optimize
for a phone notification preview.

[MODE: interactive_reply]
Conversational, free-form. Plain text. No rigid template. Speak like a
smart, opinionated friend who knows football and stats. Disagree with
Paddy when warranted. This will be sent as a threaded email reply —
your output is the body only; subject and headers are set elsewhere.

# WHAT YOU WILL RECEIVE

For each reasoning call you'll receive a structured payload containing:

- Mode (one of the above)
- Fixture(s) under analysis with full metadata
- Aggregated data summary (form, xG, lineups, injuries, weather,
  referee, market data)
- Source reliability annotations
- Data gaps array (sources that failed)
- Your bias-correction memo for today
- Your recent calibration table
- (For interactive replies) Email thread history + recent bet context

You will respond with:
- The output formatted per the mode (email body, plain text)
- A structured JSON block at the end listing each pick:
  {
    "picks": [
      {
        "fixture_id": "...",
        "market": "...",
        "selection": "...",
        "odds": 2.50,
        "true_prob": 0.45,
        "edge": 0.125,
        "confidence": 4,
        "reasoning": "..."
      }
    ]
  }
  This JSON is parsed and written to the bet ledger automatically, then
  stripped from the body before the email is sent.

# REMEMBER

The compounding value of this work is in the LEDGER. Every pick you
make is logged. Every outcome is recorded. Every day you read your own
mistakes and adjust. Over time, you become a better analyst than any
single human could be — not because you're smart, but because you're
DISCIPLINED.

Be disciplined.
```

---

## 7. The Daily Reflection Prompt Template

Run at 09:30 daily. Feeds prior 24h–90d bet data into the LLM with this prompt:

```
# REFLECTION TASK

Below is your bet ledger from the past 90 days. Today is {today}.

Your task: read the ledger, compute the patterns, and write a
≤500-word Bias Correction Memo for today's analysis.

The memo should:
1. State your aggregate ROI over: last 7d, 30d, 90d
2. Identify ONE specific bucket where you've been over-confident
   (league × market type × situation). Quantify the miscalibration.
3. Identify ONE specific bucket where you've been under-confident
   or your edge estimates have been conservative.
4. List 2–3 concrete adjustments to apply TODAY:
   - "Dampen Bundesliga BTTS confidence ratings by 1 level"
   - "Be more aggressive on La Liga away dogs vs. mid-table"
   - "Stop flagging anything from Source X — it's been
     contradicted twice this week"
5. Note any recurring data gaps that are hurting analysis.

Be specific. Use numbers. No empty platitudes. No "stay disciplined"
filler. Concrete corrections only.

---

[LEDGER DATA INJECTED HERE]

---

Write the memo now.
```

---

## 8. The Interactive Q&A Prompt Template

```
# CONTEXT FOR THIS REPLY

You are mid-conversation with Paddy via email.

Recent email thread (last 10 messages, oldest first):
{conversation_history}

Today's picks (if any reference made to "your picks"):
{todays_picks_summary}

Paddy's recent bet activity (if any reference to "my bets"):
{user_bets_last_7d}

Today's bias-correction memo (always loaded):
{bias_memo}

Paddy just sent this email:
Subject: {inbound_subject}
Body:
"{inbound_body}"

# YOUR TASK

Write a reply email body. Plain text. No template. No headers.

If he's asking about a fixture you haven't analyzed yet, run the full
analysis on the fly using the tools available to you, then respond
with your view.

If he's proposing a bet, evaluate it honestly. If you disagree, say
so. If he's making a point you agree with, build on it rather than
just agreeing.

Length: as short as possible to be useful. Email format — concise.
A few paragraphs at most. If the question warrants depth, give depth.
If it warrants "yeah, I'd take that," give that.

Do NOT include greeting fluff like "Hi Paddy, thanks for your email."
This is an ongoing thread. Just answer.

Sign off with "— The Agent" at the bottom.

Now write the reply body.
```

---

## 9. Repository Structure

```
betting-agent/
├── README.md
├── pyproject.toml
├── .env.example
├── .github/
│   └── workflows/
│       ├── morning-brief.yml      # cron: 0 10 * * * Europe/Gibraltar
│       ├── reflection.yml         # cron: 30 9 * * *
│       ├── lineup-poll.yml        # cron: */1 * * * * (controlled internally)
│       ├── conditional-watch.yml  # cron: */5 * * * *
│       ├── inplay-watch.yml       # cron: */1 * * * *
│       └── inbound-poll.yml       # cron: */1 * * * *  ← NEW in v1.1 (IMAP poll)
│
├── agent/
│   ├── __init__.py
│   ├── orchestrator.py            # entry points for each workflow
│   ├── config.py                  # env vars, league configs
│   │
│   ├── ingestion/
│   │   ├── base.py                # DataSource Protocol
│   │   ├── betfair.py
│   │   ├── fbref.py
│   │   ├── understat.py
│   │   ├── rss.py
│   │   ├── search.py
│   │   ├── odds_comparison.py
│   │   ├── weather.py
│   │   └── referees.py
│   │
│   ├── reasoning/
│   │   ├── llm_client.py          # Gemini primary, Llama fallback
│   │   ├── pick_extractor.py      # parses structured JSON from response
│   │   └── calibrator.py          # calibration math
│   │
│   ├── memory/
│   │   ├── store.py               # Supabase client wrapper
│   │   ├── ledger.py              # bets table operations
│   │   ├── reflections.py
│   │   ├── conversations.py
│   │   └── cache.py
│   │
│   ├── output/
│   │   ├── email_sender.py        # SMTP send with fallback
│   │   ├── email_receiver.py      # IMAP poll
│   │   └── formatters.py          # the §8 format generators (PRD)
│   │
│   ├── prompts/
│   │   ├── system_prompt.md       # the §6 system prompt
│   │   ├── reflection.md
│   │   ├── interactive.md
│   │   └── modes/
│   │       ├── morning_brief.md
│   │       ├── post_lineup.md
│   │       ├── conditional_alert.md
│   │       └── inplay.md
│   │
│   └── workflows/
│       ├── morning_brief.py
│       ├── reflection.py
│       ├── lineup.py
│       ├── conditional.py
│       ├── interactive.py         # handles inbound poll + reply
│       └── inplay.py
│
├── migrations/
│   ├── 001_initial_schema.sql     # the §5 schema
│   ├── 002_seed_stadiums.sql
│   └── 003_seed_elite_clubs.sql
│
└── tests/
    ├── test_ingestion.py
    ├── test_reasoning.py
    ├── test_output.py
    └── fixtures/
```

> Note: The `worker/` directory present in v1.0 has been **removed**. No Cloudflare Worker is needed in v1.1 — inbound polling is handled by a GitHub Actions cron.

---

## 10. Deployment Plan

### 10.1 Infrastructure Checklist

| Item | Provider | Plan | Setup time |
|---|---|---|---|
| Database | Supabase | Free tier (500MB) | 10 min |
| Code hosting + scheduler | GitHub | Free for public/personal repos | 5 min |
| Outbound email (SMTP) | Gmail | Free, app password required | 10 min |
| Inbound email (IMAP) | Gmail (dedicated agent account) | Free | 10 min |
| LLM (primary) | Google AI Studio (Gemini) | Free tier | 5 min |
| LLM (fallback) | Groq | Free tier | 5 min |
| Weather | OpenWeather | Free (1k calls/day) | 5 min |
| Web search | Brave Search API | Free (2k queries/mo) | 5 min |
| Betfair API | Betfair | Free (delayed data) | 30 min — requires Betfair account |

**Total estimated setup time: ~2 hours** (no third-party verification lags now that WhatsApp is removed).

### 10.2 Step-by-Step Setup

**Day 1 — Accounts & Keys (≈2 hours total)**
1. Create the dedicated Gmail account for the agent (e.g., `paddybetting.agent@gmail.com`). Enable 2FA. Generate an **App Password** for SMTP/IMAP access (Account → Security → 2-Step Verification → App passwords).
2. Create Supabase project; copy connection string and service role key.
3. Create GitHub repo; init from this spec.
4. Sign up for Google AI Studio; generate Gemini API key.
5. Sign up for Groq; generate API key.
6. Sign up for OpenWeather; generate API key.
7. Sign up for Brave Search; generate API key.
8. Open a Betfair account; register for a developer App Key (delayed data tier).

**Day 2 — Database & Core**
9. Run migrations against Supabase (§5 schema).
10. Seed `config_stadiums` (one-time data load — geocode each top-5 club's stadium).
11. Seed `config_elite_clubs` from §4.3 of PRD.
12. Implement `agent/memory/` layer.
13. Implement `agent/ingestion/` modules one at a time, with cache.
14. Implement `agent/reasoning/llm_client.py` with both Gemini and Groq.

**Day 3 — Workflows**
15. Implement `morning_brief` workflow end-to-end.
16. Test against historical fixtures (last weekend).
17. Implement reflection workflow.
18. Implement lineup, conditional, interactive, inplay workflows.

**Day 4 — Email I/O**
19. Implement `email_sender.py`. Send a test email from the agent Gmail account to **pmcclafferty0@gmail.com**. Confirm it lands in the **Primary** inbox tab (not Promotions, not Spam).
20. Implement `email_receiver.py` (IMAP poll). Send a test email *to* the agent inbox from `pmcclafferty0@gmail.com`. Confirm the poller picks it up within 60 seconds.
21. Test fallback delivery to **paddy@roitips.com**.
22. Test the full interactive loop: send email → poller picks it up → orchestrator processes → reply lands threaded in original Gmail conversation.

**Day 5 — Gmail Filters & Deliverability Hygiene** (operator-side, done once)
23. In Paddy's `pmcclafferty0@gmail.com` inbox, create the following filters:

    | If… | Then… |
    |---|---|
    | `from:paddybetting.agent@gmail.com subject:"[IN-PLAY]"` | Star, mark important, never send to spam, apply label "Bet Agent / In-Play" |
    | `from:paddybetting.agent@gmail.com subject:"[ALERT]"` | Star, mark important, never send to spam, apply label "Bet Agent / Alerts" |
    | `from:paddybetting.agent@gmail.com subject:"Daily Brief"` | Mark important, never send to spam, apply label "Bet Agent / Briefs" |
    | `from:paddybetting.agent@gmail.com` (catch-all) | Never send to spam, apply label "Bet Agent" |

24. Send 3–5 test emails from the agent account and **manually mark each "Not spam" / "Move to Primary tab"** in Paddy's inbox to warm deliverability.
25. Add `paddybetting.agent@gmail.com` to Paddy's Gmail Contacts (improves deliverability and notification priority).

**Day 6 — Schedules & Soak**
26. Enable GitHub Actions cron schedules.
27. Run for 48h in shadow mode (briefs generated and sent to a test alias of Paddy's email).
28. Promote to production: switch send target to live primary.

**Day 7 onward — Soak test**
29. Monitor daily for 2 weeks.
30. Tune confidence thresholds based on early calibration data.
31. Begin official 14-day acceptance test (§13 of PRD).

---

## 11. Environment Variables

Copy `.env.example` to `.env`:

```bash
# Supabase
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=

# LLM
GEMINI_API_KEY=
GROQ_API_KEY=

# Email — outbound (SMTP)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=paddybetting.agent@gmail.com
SMTP_APP_PASSWORD=                  # 16-char Gmail app password
EMAIL_FROM=paddybetting.agent@gmail.com
EMAIL_PRIMARY_TO=pmcclafferty0@gmail.com
EMAIL_FALLBACK_TO=paddy@roitips.com

# Email — inbound (IMAP)
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USER=paddybetting.agent@gmail.com
IMAP_APP_PASSWORD=                  # same app password as SMTP
IMAP_INBOX=INBOX
IMAP_OPERATOR_FROM=pmcclafferty0@gmail.com   # only process emails from this sender

# Data Sources
BETFAIR_APP_KEY=
BETFAIR_USERNAME=
BETFAIR_PASSWORD=
OPENWEATHER_KEY=
BRAVE_SEARCH_KEY=

# Agent config
TIMEZONE=Europe/Gibraltar
QUIET_HOURS_START=23:00
QUIET_HOURS_END=09:00
```

> **Security note:** Gmail App Passwords bypass 2FA. Store the env var **only** in GitHub Actions Secrets (Settings → Secrets and variables → Actions) — never in source. If the password leaks, revoke immediately in Google Account → Security → App passwords.

---

## 12. Monitoring, Logging & Cost Control

### 12.1 Logging

Every workflow run writes a log entry to the `incidents` table at severity `info` on success, `warning`/`error`/`critical` on failure. A daily 09:25 health check (just before reflection) summarizes overnight incidents into a single line included in the morning brief footer if any are critical.

### 12.2 Cost Monitoring

A nightly cron compiles usage stats:
- Gemini API: tokens used today / monthly free quota remaining
- OpenWeather: calls today / daily quota
- Brave Search: queries today / monthly quota
- Supabase: DB size / 500MB free tier
- Gmail SMTP: emails sent today (Gmail has a soft limit of ~500/day for free accounts — well above any expected use)

If any quota crosses **80%** of free tier, the agent emails Paddy a warning. If any crosses **95%**, the agent throttles or fails over to alternates.

### 12.3 Free Tier Limit Reality Check

| Service | Limit | Estimated daily usage | Safety margin |
|---|---|---|---|
| Gemini 2.5 Flash | 1,500 req/day | ~200 req/day | 7× headroom |
| Groq Llama 3.3 70B | Generous (varies) | ~50 req/day fallback only | Plenty |
| Gmail SMTP send | ~500/day (free account soft limit) | ~30/day est. | 16× headroom |
| Gmail IMAP poll | Unlimited (within reason) | 1,440 polls/day (every min) | OK |
| OpenWeather | 1,000 calls/day | ~50 calls/day | 20× headroom |
| Brave Search | 2,000 queries/mo | ~10/day est. | 6× headroom |
| Supabase storage | 500 MB | ~100MB/year est. | Years of headroom |
| GitHub Actions | 2,000 min/mo (free) | ~500 min/mo est. | OK — monitor |

**Most likely first bottleneck:** GitHub Actions minutes. Every cron invocation costs ~1 min. With every-minute polls running 24/7 (inbound + lineup + in-play during matches), monthly usage can creep up. Mitigation: consolidate the every-minute polls into a single workflow that does multiple jobs per invocation.

---

## 13. Security & Privacy

- All secrets in environment variables / GitHub Actions Secrets; never committed to git.
- Supabase Row Level Security enabled on all tables (single-user, so policies are simple but enforced).
- Gmail SMTP/IMAP use **App Passwords** scoped to mail access only. 2FA enabled on the agent Gmail account.
- IMAP poller verifies sender (`IMAP_OPERATOR_FROM` env var) — emails from anyone other than Paddy are logged but **never processed** as instructions. This is the primary defense against prompt injection via email.
- The agent's Gmail account should NEVER be used for any other purpose (no signups, no subscriptions). It exists only for this agent.
- No user data ever leaves the system other than the strict subset needed for LLM analysis (which is sent to Gemini/Groq under their respective enterprise privacy terms — verify these acceptable to Paddy at setup time).
- Database backups: Supabase free tier provides daily automatic backups (7-day retention). Sufficient.
- No telemetry to third-party analytics. The agent does not phone home.

### 13.1 Email-Specific Threat Model

Email opens a few new attack surfaces vs. WhatsApp:

| Threat | Mitigation |
|---|---|
| Spoofed inbound from non-operator | Strict `IMAP_OPERATOR_FROM` filter. SPF/DKIM checks not enforced by Gmail-as-IMAP-client, but the From-address filter is sufficient since attacker would also need to spoof DMARC-passing mail to `paddybetting.agent@gmail.com` — extremely unlikely at this scale. |
| Prompt injection via email body | The agent treats inbound bodies as user data, never as system instructions. The system prompt is fixed code-side. |
| Spam / phishing reaching the agent inbox | IMAP poller filters and ignores anything not matching `IMAP_OPERATOR_FROM`. Logged for audit but never acted on. |
| Agent's outbound flagged as spam | Day 5 deliverability hygiene checklist mitigates. Ongoing monitoring of inbox-vs-spam rate. |

---

## 14. Maintenance & Iteration

### 14.1 Weekly
- Review the calibration table — is the agent improving?
- Review the incidents log — any source breaking repeatedly?
- Spot-check 5 random picks for reasoning quality.
- Check Gmail deliverability — is anything landing in spam/promotions?

### 14.2 Monthly
- Re-validate scraper selectors (FBref, Understat) — these break ~quarterly.
- Review the elite club list — any club's status changed?
- Audit free-tier usage trends — approaching any limits? GitHub Actions minutes especially.
- Re-warm Gmail deliverability if any agent emails have landed in spam.

### 14.3 Seasonally
- Refresh stadium/referee/league config tables.
- Major version review — what's working, what's not, what should change for v2?
- Consider: is there a free-tier or low-cost data source worth adding now?

### 14.4 v2 Roadmap (Decided Upgrades)

| Feature | Trigger to build |
|---|---|
| Champions League + Europa League coverage | After 60 days stable v1 |
| Paid X/Twitter API integration | If $200/mo becomes acceptable |
| OddsJam or The Odds API premium | If $30–50/mo becomes acceptable |
| Push-channel re-introduction (WhatsApp/Telegram) for time-critical alerts | If in-play email latency becomes a real problem |
| Image ingestion (betslip parsing) | When user requests it |
| Web dashboard for visual analytics | If interactive demand for review grows |

---

## 15. The "First Working Day" Checklist

The absolute minimum to call this build "live":

- [ ] Supabase project exists, all tables created, seeded with stadiums + elite clubs
- [ ] Dedicated agent Gmail account created (e.g., `paddybetting.agent@gmail.com`) with 2FA + App Password
- [ ] Test email sent from agent account → lands in `pmcclafferty0@gmail.com` Primary tab
- [ ] Test email sent from `pmcclafferty0@gmail.com` → agent account → IMAP poller picks it up
- [ ] Fallback delivery to `paddy@roitips.com` verified
- [ ] Gmail filters in §10.2 step 23 set up on operator side
- [ ] Gemini API key works, basic completion verified
- [ ] At least one ingestion source (Betfair) returning real data
- [ ] Orchestrator runs `run_morning_brief()` end-to-end without error
- [ ] Brief delivered to inbox with at least 3 picks
- [ ] Inbound poll tested — sending an email gets a threaded reply within 3 minutes
- [ ] One bet logged to the ledger
- [ ] One reflection memo generated (even if empty Day 1)

When all 12 checks pass: live.

---

## 16. Known Limitations (Honest Inventory)

This system, at $0 budget, has the following genuine limitations. They are not bugs — they are scope decisions:

1. **No real-time X/Twitter signal.** Breaking injury/lineup news from beat writers will reach the agent with 30–90 minute lag via RSS. This is the single biggest signal gap vs. a paid version.
2. **Delayed Betfair Exchange prices.** Live prices cost £299 one-time. For value-betting analysis, delayed prices are largely sufficient.
3. **No StatsBomb/Opta event-level data.** xG and shot data come from FBref/Understat, which are post-match and aggregated rather than event-level. Sufficient for value betting; insufficient for live-edge alpha.
4. **Web scraping fragility.** FBref/Understat change layouts occasionally. Expect ~1 day of agent degradation per quarter while parsers are updated.
5. **Email latency for time-critical alerts.** Email typical end-to-end is 5–30 seconds, with occasional minute-plus delays. For in-play +EV (where prices move in seconds), this is genuinely a limitation. The 5-minute validity window in alerts is the mitigation. If you find it material, re-introducing a push channel (WhatsApp/Telegram) is a top v2 candidate.
6. **Interactive Q&A latency** is ~1–3 minutes (IMAP poll cadence) vs. ~30 seconds for a push channel. Acceptable for analytical conversations, less ideal for rapid back-and-forth.
7. **Gmail deliverability dependency.** If Google ever rate-limits the agent's account, briefs may be delayed. The fallback address mitigates partial outages; warming and filter setup mitigate spam-folder risk.

None of these are blocking. All are explicitly acknowledged so there are no surprises post-launch.

---

## 17. Document Control

- **v1.0** — Initial implementation blueprint, 15 May 2026
- **v1.1** — Delivery channel migrated from WhatsApp to email; Cloudflare Worker removed; IMAP polling added; deployment plan simplified, 15 May 2026
- **Companion to:** `01_PRD_FootballBettingAgent.md` v1.1
- **Sign-off:** Paddy (operator)

---

*End of Technical Architecture & Implementation Blueprint.*
