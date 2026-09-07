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
    L -->|5| N[send_notification]
    L -->|6| SM[send_run_summary]

    F <--> ROK[(Remote OK public JSON API)]
    D <--> DB[(bidwatch.db · seen posting ids)]
    P <--> PM[/profile.md/]
    SC <--> M1{{Bedrock model}}
    N --> Q[[queue, flushed highest score first]]
    SM --> Q
    Q --> TG[Telegram Bot API]
    N --> CO[Console fallback]

    Q --> CO[Console fallback]
    TG --> H([Human: Bid · Open · Skip])
    CO --> H
```

## The bid flow

Scanning and applying are deliberately separate. The scanning agent has no tool that
can reach an employer; submission lives in its own path, entered only by a button tap
and completed only by an explicit confirmation.

```mermaid
flowchart TD
    T[Bid tapped] --> R[gather_requirements: read the listing page]
    R --> C{Application method}
    C -->|email found| E[method = email]
    C -->|Greenhouse / Lever / Ashby / Workable| A[method = known_ats]
    C -->|anything else| M[method = manual]

    E --> F[Fill from applicant.md + generate cover letter]
    A --> F
    M --> F
    F --> RES[Build tailored résumé: select from profile.md, render one-page PDF]
    RES -.->|generation failed| STATIC[Fall back to static My_Resume.pdf]
    F --> QQ2[Unanswerable screening questions marked NEEDS YOUR INPUT]
    QQ2 --> QQ[ ]
    RES --> QQ
    STATIC --> QQ
    QQ --> D[Show the complete draft + View résumé]

    D --> B{User decides}
    B -->|Edit letter| ED[Revise from plain-language instruction] --> D
    B -->|Cancel| X([Nothing sent])
    B -->|Confirm & Submit| S{Submit by method}

    S -->|email| SM[SMTP: letter as body, résumé attached] --> OK1([applied_email])
    S -->|known_ats| AT[POST to the provider endpoint]
    AT -->|success| OK2([applied_ats])
    AT -->|token missing or schema mismatch| MH
    S -->|manual| MH[Hand back apply URL, letter and fields] --> OK3([applied_manual])

    style X fill:#fee,stroke:#c00
```

Nothing crosses from the left of that diagram to a submission without passing through
`Confirm & Submit`. There is no timeout that auto-confirms.

## Posting lifecycle

Every posting has exactly one status in the store, and only `new` may be notified
about, so nothing already handled can resurface:

```mermaid
stateDiagram-v2
    [*] --> new: fetched and unseen
    new --> notified: passed the threshold
    notified --> bidding: Bid tapped
    notified --> skipped: Skip tapped
    bidding --> applied_email: SMTP accepted it
    bidding --> applied_ats: the ATS accepted it
    bidding --> applied_manual: handed back for manual submission
    bidding --> notified: Cancel
    applied_email --> [*]
    applied_ats --> [*]
    applied_manual --> [*]
    skipped --> [*]
```

## Two processes

```
python scheduler.py     finds jobs, scores them, sends notifications
python bot.py           listens for button taps and the edit-letter replies
```

The scheduler can run alone — you will get alerts, but the buttons will not respond
until `bot.py` is listening. With no Telegram credentials, `scheduler.py --once
--interactive` runs the identical flow as terminal prompts.

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
        alt score >= SCORE_THRESHOLD
            Ag->>Ag: queue a notification for this posting
        else below threshold
            Ag->>Ag: skip quietly
        end
    end
    Ag->>U: flush queued notifications, highest score first
    Ag->>U: one closing summary line
    Note over Ag,U: If nothing qualifies, nothing is sent at all.<br/>BidWatch prepares; the human confirms.
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

## Résumé selection

The model never writes résumé text. It returns identifiers — a summary variant name,
skill group names, project names and bullet **indices** — and every line is then copied
verbatim out of `profile.md`. Unknown names and out-of-range indices are discarded, and
an unusable response falls back to deterministic tag matching. Invention is not
forbidden by instruction; it has no path into the document.

```mermaid
flowchart LR
    P[profile.md career database] --> C[Catalogue: names + bullet indices]
    J[Job posting] --> M{{Model}}
    C --> M
    M --> S["{summary, skills, projects, bullet indices}"]
    S --> V[Validate: drop anything not in the database]
    V -->|nothing survives| FB[Deterministic tag-overlap selection]
    V --> R[Render: text copied verbatim from profile.md]
    FB --> R
    R --> PDF[(One-page ATS-friendly PDF)]
```

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
| Submissions per rolling hour | `MAX_SUBMISSIONS_PER_HOUR` | 5 |
| Postings referred to by id, not re-serialized through the model | `tools/fetch.py` cache | always on |
| Candidates ranked before the cap, so the budget goes to plausible jobs | `agent.prioritize` | always on |

Data source: **Remote OK** (https://remoteok.com).
