# Build Prompt: BidWatch — AI Remote Job Scout Agent

Build a complete, working AI agent called **BidWatch** using the **Strands Agents SDK** (Python) on **Amazon Bedrock**. Execute this fully: write all code, wire it together, test it against the real job source, and leave it runnable. Do not stop partway to ask about anything covered below — where a genuine ambiguity exists that is not addressed here, make the most sensible professional choice and note it briefly in the README.

This project is a submission to the AWS "Agents for Humans" hackathon (Professional Agents track), deadline 14 September 2026. It must therefore be finished, public, documented, and demoable — not a prototype.

---

## 1. What BidWatch does

job seekers lose hours every week to a repetitive loop: refresh job boards, scan new postings, mentally discard the ones that don't fit (wrong stack, budget too low, deal-breakers), then write a proposal from scratch for the few that do.

BidWatch runs autonomously in the background and does the scanning and filtering. On each run it:

1. Fetches the latest job postings from a public job feed.
2. Discards postings it has already seen on previous runs.
3. Loads the user's profile (skills, rates, deal-breakers, voice).
4. Scores each new posting 0–100 for fit, with a one-line rationale.
5. For postings above a threshold, drafts a tailored proposal in the user's voice.
6. Sends exactly one notification per qualifying posting, containing the posting summary, score, rationale, and the draft.

If nothing qualifies, it sends nothing and ends the run quietly. The user's only job is the decision a machine should not make for them: **bid or skip**.

**Product principle, which must be enforced in code and prompt:** BidWatch drafts, the human sends. The agent must never submit or send a proposal to a client.

---

## 2. Current state of the repository

The repo already exists and works. Do not rebuild it from scratch; extend it.

```
bidwatch/
├── .gitignore          # venv/, __pycache__/, *.pyc, .env, .aws/, seen.json, *.db, aws/, awscliv2.zip
├── LICENSE             # MIT
├── README.md           # stub
├── config.py           # MODEL_ID, REGION, JOB_SOURCE_URL, SCORE_THRESHOLD
├── profile.md          # empty/stub — needs filling
├── requirements.txt
├── test_agent.py       # WORKING hello-world Strands agent with one custom tool
└── tools/
    ├── __init__.py
    ├── fetch.py        # empty
    └── store.py        # empty
```

**Confirmed working already:**
- Python 3.12 in a venv at `./venv`
- `strands-agents` installed
- AWS credentials configured via `aws configure` (IAM user `hamed_ezzu`, `AmazonBedrockFullAccess` attached)
- Bedrock model **`zai.glm-5`** in region **`eu-north-1`** responds and **successfully calls tools** (verified — the model invoked a `get_current_time()` tool and used the result)

**Important constraint:** Anthropic Claude models are **not available** on this AWS account (blocked by an AWS Marketplace geographic restriction). Do not switch the model to Claude. Use `zai.glm-5`. Keep the model ID in `config.py` so it can be swapped in one line.

---

## 3. Job source

Use **RemoteOK's free public JSON API**: `https://remoteok.com/api`

Facts about this source that the implementation must respect:

- Free, no authentication, no API key.
- Supports tag filtering, e.g. `https://remoteok.com/api?tag=python` or `?tags=dev,python`.
- Returns roughly the 100 most recent listings per request.
- **The first element of the returned array is a legal/attribution notice object, not a job.** It must be skipped.
- The feed is delayed ~24 hours behind the website. This is expected; do not treat it as an error, and describe the source accurately in the README (do not claim real-time).
- Salary data is present on only ~30–40% of listings. Handle missing salary gracefully.
- Employment type is always effectively "full-time"; the API does not expose type.

**Terms of use — these are legal obligations and must be honored in the code and docs:**
- Mention Remote OK as the source wherever job data is displayed (notifications and README).
- Link back to the job listing URL on Remote OK with a **direct** link (no redirects).
- Do not use the Remote OK logo. Using the name is fine.

Set a descriptive `User-Agent` header on requests. Be polite: do not poll more often than every ~15 minutes.

**Source abstraction requirement:** all source-specific logic must live in `tools/fetch.py` behind a stable interface, so a second source could be added without touching any other file. State this in the README.

---

## 4. Architecture

A single Strands agent with six tools. Strands runs the reasoning loop: the model decides which tool to call, the tool executes, the result returns to the model, repeat.

```
[ Scheduler ] every N minutes
      |
      v
[ BidWatch Agent — Strands loop, model: zai.glm-5 ]
      |-- fetch_job_postings()   --> RemoteOK public JSON API
      |-- filter_new_postings()  --> local store (dedupe)
      |-- load_profile()         --> profile.md
      |-- score_posting()        --> model reasoning: 0-100 + rationale
      |-- draft_proposal()       --> model reasoning: tailored draft
      |-- send_notification()    --> Telegram (or console fallback)
      v
[ One message per qualifying posting: summary + score + rationale + draft ]
```

### Tool specifications

**`fetch_job_postings(tag: str = "dev", limit: int = 20) -> list[dict]`**
Fetches from RemoteOK. Skips the legal-notice first element. Normalizes each posting into a consistent dict:
`{id, title, company, description, tags, url, salary_min, salary_max, location, posted_at}`
Strip HTML from descriptions (RemoteOK returns HTML). Truncate very long descriptions to a sensible length (e.g. 2000 chars) before they reach the model, to control token cost. Handle network errors and non-200 responses without crashing — return an empty list and log the problem.

**`filter_new_postings(postings: list[dict]) -> list[dict]`**
Persists seen posting IDs in a local store (SQLite in `bidwatch.db`, or `seen.json` — your choice; SQLite is cleaner and both are gitignored). Returns only postings whose IDs are not already stored, then records the new IDs. Running twice in a row with the same feed must return an empty list the second time. Make the store creation idempotent (create table/file if absent).

**`load_profile() -> str`**
Reads `profile.md` from disk and returns its contents. Read fresh on every call so the user can edit the profile without restarting. If the file is missing, return a clear error string rather than raising.

**`score_posting(posting_json: str, profile: str) -> str`**
Evaluates one posting against the profile. Returns a compact JSON string: `{"score": 0-100, "rationale": "one sentence"}`. Scoring should weigh: skill overlap with the profile's strong skills, budget/salary against the profile's target rate, and explicit deal-breakers (a deal-breaker match should force a low score). Parse the model's response defensively — if it returns malformed JSON, extract what you can or default to a low score rather than crashing the run.

**`draft_proposal(posting_json: str, profile: str) -> str`**
Writes a proposal draft under 150 words, in the voice described in the profile. Requirements: open by referencing the client's actual problem; be specific, not generic; **never claim skills or experience not present in the profile**. Return plain text.

**`send_notification(message: str) -> str`**
Sends one message. Primary channel: Telegram Bot API (`https://api.telegram.org/bot<TOKEN>/sendMessage`). If `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` are absent from the environment, fall back to printing a nicely formatted message to the console and log that Telegram is not configured — **the project must run end-to-end with no Telegram credentials at all**, so it can be demoed and tested offline. Message must include: job title, company, the RemoteOK direct link, score, rationale, the draft, and a "Source: Remote OK" attribution line.

### Agent and system prompt

Create `agent.py` defining the Strands agent with all six tools and a system prompt containing at minimum these rules:

```
You are BidWatch, an assistant that monitors remote job
postings on behalf of one job seekers.

Each run:
1. Fetch the latest postings.
2. Keep only postings not seen before.
3. Load the job seeker's profile.
4. Score each new posting 0-100 for fit, with one line of reasoning.
5. For any posting scoring above the threshold, draft a short proposal
   (under 150 words) in the job seeker's voice.
6. Send exactly one notification per qualifying posting.

Hard rules:
- NEVER claim skills or experience not present in the profile.
- NEVER submit or send a proposal to a client. You draft only.
- If nothing qualifies, send nothing and end the run quietly.
- Be conservative: a missed marginal job costs less than a wasted bid
  or an inaccurate claim.
- Always include the Remote OK source attribution and the direct job
  link in every notification.
```

### Scheduler

`scheduler.py`: runs the agent on an interval (default 30 minutes, configurable in `config.py`), with `--once` to run a single cycle for testing/demo. Use APScheduler or a simple loop. Log the start, end, count of new postings, and count of notifications per cycle.

---

## 5. Cost control (important)

The model is billed per token against limited AWS credits. Implement all of the following:

- `MAX_POSTINGS_PER_RUN` in `config.py`, default **5**. Never score more than this in one run.
- Truncate posting descriptions before sending them to the model.
- A `--dry-run` flag that exercises the full pipeline using a bundled fixture file of sample postings and **no** model calls, so the non-LLM path can be tested for free.
- Log approximate token usage per run if the SDK exposes it.

---

## 6. Deliverables

1. **Working code**, organized as:
```
bidwatch/
├── LICENSE                 # MIT (already present — keep)
├── README.md               # rewrite fully (see below)
├── requirements.txt        # regenerate with pip freeze
├── .env.example            # documents TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
├── config.py               # MODEL_ID, REGION, JOB_SOURCE_URL, SCORE_THRESHOLD,
│                           # MAX_POSTINGS_PER_RUN, RUN_INTERVAL_MINUTES, TAG
├── profile.md              # filled in with the real profile (section 8)
├── agent.py                # Strands agent + system prompt
├── scheduler.py            # periodic runner; --once and --dry-run flags
├── tools/
│   ├── __init__.py
│   ├── fetch.py
│   ├── store.py
│   ├── profile.py
│   ├── scoring.py
│   ├── drafting.py
│   └── notify.py
├── fixtures/
│   └── sample_postings.json   # for --dry-run
├── tests/
│   └── test_tools.py       # basic tests for the non-LLM tools
└── docs/
    └── architecture.md     # text/mermaid diagram of the agent loop
```

2. **README.md** — this is scored by judges, so write it properly. It must contain: the problem, what BidWatch does, an architecture diagram (mermaid is fine), the tool table, setup instructions from a clean clone, how to run (`--once`, `--dry-run`, scheduled), configuration reference, the Remote OK attribution and 24-hour-delay disclosure, a "Design decisions" section (why draft-not-send; why dedupe by ID; why the profile is a plain editable file), and a "Future work" section (additional sources, live deployment, etc.).

3. **`docs/architecture.md`** — the diagram, since the hackathon requires an architecture diagram as a submission artifact.

4. **Tests** for the four non-LLM tools: fetch (against a fixture, not the network), dedupe (twice-run returns nothing the second time), profile loading, notification formatting/fallback.

5. **Run it for real before you finish.** Execute `python scheduler.py --once` against the live RemoteOK feed with real model calls, confirm the pipeline completes end to end, and paste the output in your final summary. If it fails, fix it and re-run. Do not declare completion on untested code.

---

## 7. Code quality expectations

- Type hints on all tool functions; clear docstrings (Strands uses docstrings to tell the model what each tool does, so write them for the model as much as for humans).
- No secrets in code; read from environment/`.env` and gitignore it.
- Graceful failure everywhere: a network hiccup, a malformed posting, or a bad model response must not crash the run.
- Structured logging (module `logging`, not bare prints) so runs are traceable.
- Small, focused commits with clear messages as you go.

---

## 8. Information the user must supply

The following are needed to complete the build. Where the user has not provided them, **use the placeholder shown, mark it clearly with a `TODO`, and continue** — do not block on them.

| Item | Where it goes | Where to get it |
|---|---|---|
| GitHub username | git remote URL | github.com — the name in the profile URL |
| Telegram bot token | `.env` as `TELEGRAM_BOT_TOKEN` | Open Telegram, message **@BotFather**, send `/newbot`, follow prompts, copy the token |
| Telegram chat ID | `.env` as `TELEGRAM_CHAT_ID` | Message the new bot once, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `message.chat.id` |
| Profile contents | `profile.md` | Supplied below |

**`profile.md` contents — use exactly this as the starting profile:**

```markdown
# Remote Job Profile

## Strong skills
- Python: FastAPI, SQLAlchemy (async), pytest
- C# / .NET: ASP.NET Core (MVC, Web API), Entity Framework Core, .NET 8/10
- Databases: PostgreSQL, SQL Server, schema design, migrations, concurrency control
- JavaScript/TypeScript: Node.js, Express, React 18
- DevOps: Docker, Linux, Git, deployment with TLS and automated backups
- Real-time systems: WebSockets
- Desktop: WinForms (with SQL Server), some WPF

## Willing, but not expert
- MongoDB
- Blazor
- AI/LLM integration (agent tooling, API integration)

## Will not bid on
- WordPress, Wix, Squarespace, or page-builder site jobs
- Pure graphic design or heavy visual-design work
- Projects under $50
- Anything requiring on-site presence
- Roles requiring a language other than English or Arabic

## Rates
- Target: $15-25/hour, or fixed-price equivalent
- Open to lower on small first projects that build reputation

## Context
- Computer Engineering student, University of Tripoli (Libya), graduating 2027
- Available part-time, remote only
- Fluent in Arabic and English
- Timezone UTC+2 — overlaps European and Gulf working hours

## Proposal voice
- Direct and specific. No filler, no flattery, no "I am excited to apply".
- First sentence must reference the client's actual problem in their words.
- Mention one concrete, relevant thing actually built before.
- Never claim experience not listed above. If a required skill is missing,
  say plainly what is known and what is not.
- End with one clarifying question about the project.
- Under 150 words.
```

---

## 9. Definition of done

- `python scheduler.py --dry-run` completes with zero model calls and prints formatted notifications for the fixture postings.
- `python scheduler.py --once` completes against the live feed, scores real postings, and produces at least one notification (console or Telegram) including a direct Remote OK link and attribution.
- Running `--once` twice in a row produces no duplicate notifications.
- `pytest` passes.
- README, architecture doc, and `.env.example` are complete.
- Everything is committed. Nothing secret, no venv, no large binaries in git.

Report at the end: what was built, the real output of the test run, anything left as a `TODO`, and any decision you made that was not specified here.
