# BidWatch — Architecture

BidWatch is a single [Strands Agents](https://strandsagents.com) agent running on
Amazon Bedrock, with six tools. Strands owns the reasoning loop: the model decides
which tool to call, the tool executes locally, the result returns to the model, and
the loop repeats until the run is complete.

## Agent loop

```mermaid
flowchart TD
    S([Scheduler — every N minutes, default 30]) --> A

    subgraph A[BidWatch Agent · Strands loop · Bedrock zai.glm-5]
        direction TB
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

## Run sequence

```mermaid
sequenceDiagram
    participant Sch as Scheduler
    participant Ag as BidWatch Agent
    participant ROK as Remote OK API
    participant DB as bidwatch.db
    participant M as Bedrock model
    participant U as Freelancer

    Sch->>Ag: run one cycle
    Ag->>ROK: fetch_job_postings(tag, limit)
    ROK-->>Ag: ~100 recent listings (first element = legal notice, skipped)
    Ag->>DB: filter_new_postings(postings)
    DB-->>Ag: only unseen ids (and records them)
    Ag->>Ag: load_profile() — read profile.md fresh
    Ag->>Ag: rank candidates by profile keyword overlap (free, no model call)
    loop at most MAX_POSTINGS_PER_RUN new postings
        Ag->>M: score_posting(posting, profile)
        M-->>Ag: {"score": 0-100, "rationale": "..."}
        alt score > SCORE_THRESHOLD
            Ag->>M: draft_proposal(posting, profile)
            M-->>Ag: draft under 150 words
            Ag->>U: send_notification(summary + score + rationale + draft + Remote OK link)
        else score <= threshold
            Ag->>Ag: skip quietly, no notification
        end
    end
    Note over Ag,U: If nothing qualifies, nothing is sent.<br/>BidWatch drafts; the human sends.
```

## Components

| Layer | File | Responsibility |
|---|---|---|
| Entry point | `scheduler.py` | `--once`, `--dry-run`, or interval loop; per-cycle logging |
| Agent | `agent.py` | Strands agent, system prompt, tool registry, deterministic pipeline path |
| Source adapter | `tools/fetch.py` | All Remote OK specifics; emits normalized postings |
| Dedupe | `tools/store.py` | SQLite store of seen posting ids |
| Profile | `tools/profile.py` | Fresh read of `profile.md` on every call |
| Scoring | `tools/scoring.py` | 0–100 fit score + rationale, defensive JSON parsing |
| Drafting | `tools/drafting.py` | Proposal under 150 words in the freelancer's voice |
| Delivery | `tools/notify.py` | Telegram, with console fallback |
| Model access | `tools/llm.py` | Shared Bedrock client + token-usage accounting |

## Two execution paths

The same six steps can run either way:

- **`--mode agent`** (default for `--once`): the model drives the Strands tool loop
  and decides the order of calls. This is the agentic path.
- **`--mode pipeline`**: the same tools called deterministically from Python. Used by
  `--dry-run` (zero model calls) and useful when a run must be exactly predictable
  or maximally cheap.

## Source abstraction

Every Remote OK detail — the endpoint, the legal-notice first element, HTML in
descriptions, the sparse salary fields — is confined to `tools/fetch.py`. The rest of
the system only consumes the normalized posting dict:

```
{id, title, company, description, tags, url, salary_min, salary_max, location, posted_at}
```

Adding a second job source means adding a fetcher that emits this shape. No other
file changes.

## Cost control

| Control | Where | Default |
|---|---|---|
| Postings scored per run | `MAX_POSTINGS_PER_RUN` | 5 |
| Description truncation before the model sees it | `MAX_DESCRIPTION_CHARS` | 2000 chars |
| Zero-model test path | `--dry-run` against `fixtures/sample_postings.json` | — |
| Token accounting per run | `tools/llm.py` → logged by the scheduler | always on |
| Poll floor (politeness to the source) | `scheduler.py` | 15 minutes |
| Postings referred to by id, not re-serialized through the model | `tools/fetch.py` cache | always on |
| Candidates ranked before the cap, so the budget goes to plausible jobs | `agent.prioritize` | always on |

Data source: **Remote OK** (https://remoteok.com).
