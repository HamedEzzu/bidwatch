# BidWatch — AI Freelance Job Scout

**BidWatch watches job boards so you don't have to.** It fetches new remote job
postings, scores each one against your own skill profile, drafts a tailored proposal
for the ones that fit, and sends you a single notification per match. You do the one
thing a machine should not do for you: **bid or skip**.

Built with the [Strands Agents SDK](https://strandsagents.com) on **Amazon Bedrock**
for the AWS "Agents for Humans" hackathon (Professional Agents track).

---

## The problem

Freelancers lose hours every week to the same loop: refresh the board, scan the new
postings, mentally discard the ones that don't fit (wrong stack, budget too low, a
deal-breaker in the fine print), then write a proposal from scratch for the two that
survive. The scanning is mechanical. The judgement at the end is not.

BidWatch automates the mechanical part and stops precisely where judgement begins.

## What it does

On every run:

1. **Fetches** the newest postings from the Remote OK public API.
2. **Discards** anything it has already seen on a previous run.
3. **Loads** your profile — skills, rates, deal-breakers, and the voice you write in.
4. **Scores** each new posting 0–100 for fit, with a one-line rationale.
5. **Drafts** a proposal (under 150 words, in your voice) for anything above the threshold.
6. **Notifies** you exactly once per qualifying posting, with the summary, score,
   rationale, draft, and a direct link to the listing.

If nothing qualifies, it sends nothing and ends the run quietly.

> **Product principle, enforced in both code and prompt: BidWatch drafts, the human
> sends.** The agent has no tool that can contact a client. Its only outbound channel
> is a notification to you.

---

## Architecture

```mermaid
flowchart TD
    S([Scheduler — every N minutes, default 30]) --> A

    subgraph A[BidWatch Agent · Strands loop · Bedrock zai.glm-5]
        L[Model reasoning loop]
    end

    L -->|1| F[fetch_job_postings]
    L -->|2| D[filter_new_postings]
    L -->|3| P[load_profile]
    L -->|4| SC[score_posting]
    L -->|5| DR[draft_proposal]
    L -->|6| N[send_notification]

    F <--> ROK[(Remote OK public JSON API)]
    D <--> DB[(bidwatch.db · seen posting ids)]
    P <--> PM[/profile.md/]
    SC <--> M1{{Bedrock model}}
    DR <--> M1
    N --> TG[Telegram Bot API]
    N --> CO[Console fallback]

    TG --> H([Human: bid or skip])
    CO --> H
```

Strands runs the reasoning loop: the model picks a tool, the tool executes locally,
the result goes back to the model, repeat. Full diagrams — including the run
sequence — are in [docs/architecture.md](docs/architecture.md).

### Tools

| Tool | Signature | What it does |
|---|---|---|
| `fetch_job_postings` | `(tag: str = "dev", limit: int = 20) -> list[dict]` | Pulls the newest listings from Remote OK, skips the legal-notice element, strips HTML, truncates long descriptions, normalizes each posting. Returns `[]` on any network or HTTP error. |
| `filter_new_postings` | `(postings: list[dict]) -> list[dict]` | Returns only postings whose ids aren't in the local SQLite store, then records them. Run twice, and the second run returns nothing. |
| `load_profile` | `() -> str` | Reads `profile.md` fresh on every call, so you can edit it without restarting. Returns a clear error string if it's missing. |
| `score_posting` | `(posting_json: str, profile: str) -> str` | Model reasoning: returns `{"score": 0-100, "rationale": "..."}`. Deal-breaker matches are forced below 20. Malformed model output is parsed defensively and defaults low. |
| `draft_proposal` | `(posting_json: str, profile: str) -> str` | Model reasoning: a proposal under 150 words that opens on the client's actual problem and never claims a skill absent from the profile. |
| `send_notification` | `(message: str) -> str` | Sends one Telegram message; prints to the console when Telegram isn't configured. |

### Project layout

```
bidwatch/
├── agent.py                    # Strands agent, system prompt, run paths
├── scheduler.py                # --once / --dry-run / interval loop
├── config.py                   # model, region, source, thresholds, limits
├── profile.md                  # your skills, rates, deal-breakers, voice
├── tools/
│   ├── fetch.py                # Remote OK adapter (all source-specific code)
│   ├── store.py                # SQLite dedupe store
│   ├── profile.py              # profile loading
│   ├── scoring.py              # fit scoring + defensive parsing
│   ├── drafting.py             # proposal drafting
│   ├── notify.py               # Telegram + console fallback
│   └── llm.py                  # shared Bedrock client + token accounting
├── fixtures/sample_postings.json   # for --dry-run
├── tests/test_tools.py         # 22 tests, no network, no model calls
└── docs/architecture.md
```

---

## Setup from a clean clone

```bash
git clone https://github.com/HamedEzzu/bidwatch.git
cd bidwatch

python3.12 -m venv venv
source venv/bin/activate           # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

**AWS access.** You need credentials with Bedrock access in your region:

```bash
aws configure                      # or export AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
```

The account needs `AmazonBedrockFullAccess` (or equivalent `bedrock:InvokeModel`
permission) and model access enabled for the model in `config.py`.

**Optional — Telegram.** BidWatch runs end to end with no Telegram credentials at
all; it simply prints notifications to the console. To get them on your phone:

```bash
cp .env.example .env
```

1. Message **@BotFather** on Telegram, send `/newbot`, follow the prompts, copy the
   token into `TELEGRAM_BOT_TOKEN`.
2. Send your new bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `message.chat.id`
   into `TELEGRAM_CHAT_ID`.

**Your profile.** Edit [`profile.md`](profile.md) — strong skills, things you're
willing but not expert in, what you won't bid on, your rates, and your proposal
voice. This file is the whole basis for scoring and drafting, so it's worth ten
careful minutes. It's read fresh on every run; no restart needed.

---

## Running it

```bash
# Free: full pipeline against bundled fixtures, zero model calls.
python scheduler.py --dry-run

# One real cycle: live feed, real scoring and drafting.
python scheduler.py --once

# One real cycle, deterministic Python orchestration instead of the model loop.
python scheduler.py --once --mode pipeline

# Watch a different tag.
python scheduler.py --once --tag dotnet

# Scheduled: runs forever, every RUN_INTERVAL_MINUTES. Ctrl-C to stop.
python scheduler.py
```

Run `--once` twice in a row and the second run notifies about nothing: every posting
was already recorded in `bidwatch.db`.

Tests:

```bash
pytest -q
```

### A real run

`python scheduler.py --once` against the live feed, with no Telegram configured:

```
INFO bidwatch.scheduler: Cycle start — mode=agent tag=backend threshold=65 max_postings=5
Tool #1: fetch_job_postings      INFO tools.fetch: Fetched 20 postings for tag='backend'
Tool #2: load_profile
Tool #3: filter_new_postings     INFO tools.store: 8 of 8 postings are new
Tool #4-8: score_posting
  Scored 'Software Engineer GO'                      -> 25
  Scored 'Senior Software Engineer Case Execution'   -> 72
  Scored 'Staff Software Development Engineer SDM'   -> 15
  Scored 'Senior Backend Engineer Build AI Agents'   -> 35
  Scored 'DESARROLLADOR FULL STACK'                  -> 55
Tool #9: draft_proposal
Tool #10: send_notification      INFO tools.notify: Telegram is not configured; printing to console.

========================================================================
Senior Software Engineer Case Execution — Pivotal Health

Score: 72/100

Rationale: Strong Python backend fit with data-heavy workflow work aligning well
with the profile, but the senior title introduces friction worth a mild penalty.

Remote OK Link: https://remoteOK.com/remote-jobs/remote-senior-software-engineer-
case-execution-pivotal-health-1136795

Draft Proposal:
You need a Senior Software Engineer for your Case Execution team to handle
eligibility, batching, and claim orchestration workflows. I'm a Computer
Engineering student graduating 2027, so I don't meet the senior-level requirement
you've specified. However, I have relevant backend experience: I've built async
workflows with FastAPI and SQLAlchemy, worked with PostgreSQL on complex schema
design and concurrency control, and implemented real-time systems using
WebSockets. My rate is $15-25/hour, and I'm available part-time remote from
UTC+2. Given the senior designation in your title, are you open to considering a
junior contributor for any aspects of this work?

Source: Remote OK
========================================================================

INFO bidwatch.scheduler: Agent summary: 8 new postings, 5 scored, 1 notification sent.
INFO bidwatch.scheduler: Approx. token usage — calls=6 input=7227 output=412
INFO bidwatch.scheduler: Cycle end — 52.2s elapsed
```

Note what the draft does *not* do: it states plainly that the freelancer does not meet
the senior requirement, rather than claiming experience the profile doesn't contain.
Running the same command again immediately afterwards reports `0 of 20 postings are
new`, makes zero model calls, and sends nothing.

### Configuration

All in [`config.py`](config.py), each overridable by an environment variable.

| Setting | Env var | Default | Meaning |
|---|---|---|---|
| `MODEL_ID` | `BIDWATCH_MODEL_ID` | `zai.glm-5` | Bedrock model. One line to swap. |
| `REGION` | `AWS_REGION` | `eu-north-1` | Bedrock region. |
| `JOB_SOURCE_URL` | `JOB_SOURCE_URL` | `https://remoteok.com/api` | Job feed endpoint. |
| `TAG` | `BIDWATCH_TAG` | `backend` | Remote OK tag to monitor. |
| `SCORE_THRESHOLD` | `SCORE_THRESHOLD` | `65` | Notify only above this score. |
| `MAX_POSTINGS_PER_RUN` | `MAX_POSTINGS_PER_RUN` | `5` | Hard cap on postings scored per run. |
| `RUN_INTERVAL_MINUTES` | `RUN_INTERVAL_MINUTES` | `30` | Scheduled interval (floored at 15). |
| `MAX_DESCRIPTION_CHARS` | — | `2000` | Truncation before the model sees a description. |

---

## Data source and attribution

Job data comes from **[Remote OK](https://remoteok.com)**, via their free public JSON
API. Every notification carries a `Source: Remote OK` line and a direct, non-redirected
link to the listing on Remote OK, as their API terms require. The Remote OK logo is
not used anywhere in this project.

**The feed is roughly 24 hours behind the Remote OK website.** This is a property of
the free API, not a bug in BidWatch. Treat BidWatch as a daily scout, not a real-time
alert system. Other honest limits of the source: it returns about the 100 most recent
listings per request, salary is published on only ~30–40% of them (BidWatch treats a
missing salary as neutral, never as a negative), and employment type is not exposed —
listings are effectively full-time roles, so BidWatch scores them as fit candidates
against your profile rather than filtering by contract type.

BidWatch polls no more often than every 15 minutes and identifies itself with a
descriptive `User-Agent`.

---

## Design decisions

**Draft, never send.** The agent has no tool capable of reaching a client — the only
outbound tool is `send_notification`, which talks to you. This is a structural
guarantee, not a promise in a prompt: even a badly-behaved model cannot submit a
proposal, because no such capability exists in the tool set. The system prompt states
the rule too, but the tool boundary is what actually enforces it. Sending is where
reputation and money are at stake, and that judgement stays human.

**Dedupe by posting id, in a local store.** Notification fatigue kills this category
of tool. Remote OK ids are stable, so recording them in SQLite gives an exact
guarantee — one notification per posting, ever — rather than a fuzzy
title-similarity heuristic that would eventually either spam or silently drop a job.
The store is created on demand and gitignored; delete `bidwatch.db` to reset.

**The profile is a plain Markdown file you edit.** Not a wizard, not a database, not
a settings UI. Your skills and deal-breakers change often and are best expressed in
prose the model can read directly ("Will not bid on: projects under $50"). It's read
fresh on every call, so an edit takes effect on the next cycle with no restart, and
it's diffable in git alongside the code that uses it.

**All source knowledge lives in one file.** Every Remote OK peculiarity — the
attribution object in position zero, HTML descriptions, sparse salaries — is confined
to `tools/fetch.py`, which emits a normalized posting dict. Adding Upwork or
Freelancer means writing one new fetcher against that shape; no other file changes.

**Two execution paths.** `--mode agent` (default) lets the model drive the Strands
tool loop — that's the agentic behaviour the project is about. `--mode pipeline` runs
the same six tools deterministically from Python, which is what `--dry-run` uses and
what you want when a run must be exactly predictable or maximally cheap. Both share
the same tool implementations, so they cannot drift apart.

**Spend the model budget on plausible jobs.** The feed is newest-first and mixes
software roles with retail, hospitality and logistics listings that happen to carry a
matching tag. Scoring the newest five would routinely burn the whole per-run budget on
jobs that could never fit, so candidates are ordered by keyword overlap with the
profile's strong skills before the cap applies. Nothing is discarded — only reordered
— and the ranking is plain string matching, so it costs nothing.

**Scoring is calibrated to the source, not to an ideal.** Every Remote OK listing is a
permanent, salaried, full-time role, and the API does not expose contract type. An
early version penalised "full-time" and "senior", which made BidWatch silent by
construction — it rejected every posting the board actually carries. Those signals are
now a mild penalty at most; the score is dominated by stack overlap, and deal-breakers
(on-site, page-builder work, a language the freelancer doesn't speak) remain fatal.
The freelancer decides whether a senior title is worth their bid; BidWatch judges the
work.

**The outbound message is repaired, not trusted.** When the model composes a
notification itself it occasionally retypes the job URL or drops the attribution line.
Both are obligations under the Remote OK API terms, so `send_notification` rewrites any
Remote OK link from the posting BidWatch actually fetched and appends the attribution if
it is missing, before anything is sent.

**Conservative scoring by default.** A missed marginal job costs a freelancer far
less than a wasted bid or, worse, a proposal claiming experience they don't have. So
unparseable model output scores 0 rather than guessing, deal-breaker matches are
forced below 20, and the drafting prompt requires stating plainly what is *not*
known when a posting asks for it.

**Model choice.** `zai.glm-5` on Bedrock in `eu-north-1`. Anthropic Claude models are
not available on this AWS account due to an AWS Marketplace geographic restriction,
so the model id lives in `config.py` and can be swapped in a single line wherever
Claude is available.

**Cost control is built in, not bolted on.** `MAX_POSTINGS_PER_RUN` caps model calls
per cycle at 5, descriptions are truncated to 2000 characters before they reach the
model, `--dry-run` exercises the entire non-LLM path for free, and approximate token
usage is logged after every run.

---

## Future work

- **More sources.** Upwork and Freelancer RSS/API adapters behind the existing
  fetcher interface; a merged, cross-source dedupe key.
- **Live deployment.** Lambda on an EventBridge schedule, with the dedupe store moved
  to DynamoDB so it survives a stateless runtime. The code is already structured for
  it: one cycle is a single function call.
- **Feedback loop.** Let the freelancer reply "bid" or "skip" to a Telegram
  notification and feed those decisions back as few-shot examples, so scoring learns
  the user's real preferences instead of only what they wrote down.
- **Proposal history.** Keep sent drafts and outcomes to spot which openings actually
  win work.
- **Richer filters.** Hourly-rate parsing from free-text descriptions, timezone
  overlap scoring, and per-tag thresholds.
- **A web dashboard** for reviewing the queue when Telegram isn't the right surface.

---

## License

MIT — see [LICENSE](LICENSE).

Job data from [Remote OK](https://remoteok.com).
