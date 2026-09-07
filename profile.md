# Freelancer Career Database

Everything BidWatch knows about you. Two jobs:

1. **Deciding what to bid on** — Rates, Will not bid on, Context, Proposal voice.
2. **Building a tailored résumé** — Summaries, Skills, Projects, Awards, Education,
   Certifications, Languages.

The résumé generator may only *select and reorder* what is written here. It cannot
add, reword or embellish, so every line must be true as written. Bullets carry
`{tags: ...}` markers used to match them to a job; the tags never appear in output.

---

## Identity

Only the résumé headline lives here. Name, phone, email and links come from
`applicant.md`, which is gitignored — this file is committed, and contact
details do not belong in a public repository.

- Headline: Backend Software Engineer (Student)

---

## Summaries

One is chosen per application, whichever matches the job's primary stack.

### Summary: dotnet
Computer Engineering student specializing in backend development with C#/.NET — ASP.NET Core MVC, Entity Framework Core, and SQL Server. Built and deployed a production news publishing platform with role- and policy-based access control, Repository/Unit of Work data access, and a .NET 8 to .NET 10 migration. Bronze medalist at Libya's national collegiate programming contest (LCPC). Bilingual Arabic/English.

### Summary: python
Computer Engineering student specializing in backend development with Python — FastAPI, async SQLAlchemy 2, and PostgreSQL. Built and deployed production-grade systems featuring real-time WebSocket pipelines, concurrency-safe payment logic, JWT auth with RBAC, and Dockerized deployments with automated testing under mypy --strict. Bronze medalist at Libya's national collegiate programming contest (LCPC). Bilingual Arabic/English.

### Summary: node
Computer Engineering student building full-stack applications with JavaScript/TypeScript, React 18, and Node.js, backed by Python (FastAPI) and C#/.NET services. Shipped a bilingual multi-branch platform with real-time WebSocket interfaces and a strictly typed TypeScript frontend, deployed with Docker behind TLS. Bronze medalist at Libya's national collegiate programming contest (LCPC). Bilingual Arabic/English.

### Summary: backend
Computer Engineering student specializing in backend development with Python (FastAPI), Node.js, and C#/.NET. Built and deployed production-grade systems featuring real-time WebSocket pipelines, concurrency-safe payment logic, JWT auth with RBAC, and Dockerized PostgreSQL deployments. Bronze medalist at Libya's national collegiate programming contest (LCPC). Bilingual Arabic/English.

---

## Skills

### Skills: Languages
Python, JavaScript/TypeScript (Node.js), C#, SQL {tags: python, javascript, typescript, node, csharp, dotnet, sql}

### Skills: Python backend
FastAPI, SQLAlchemy 2 (async), Pydantic, pytest, mypy --strict {tags: python, fastapi, sqlalchemy, testing}

### Skills: .NET backend
ASP.NET Core MVC, ASP.NET Core Web API, Entity Framework Core, ASP.NET Core Identity, AutoMapper, .NET 8/10 {tags: csharp, dotnet, aspnet, efcore, identity}

### Skills: APIs and real-time
REST APIs, WebSockets, JWT authentication, role-based access control {tags: api, rest, websockets, realtime, auth, jwt, security}

### Skills: Databases
PostgreSQL, SQL Server; schema design, migrations, transactions, concurrency control {tags: postgres, postgresql, sqlserver, sql, database, migrations, concurrency}

### Skills: DevOps and tools
Docker, Linux, Git, Caddy, Gunicorn, TLS deployment, automated backups, CI-style automated testing {tags: docker, linux, devops, deployment, ci, git}

### Skills: Frontend (working knowledge)
React 18, TypeScript, Tailwind CSS, Chart.js {tags: react, frontend, typescript, javascript, ui}

### Skills: Desktop
WinForms with SQL Server, some WPF {tags: desktop, winforms, wpf, csharp, dotnet}

---

## Projects

### Project: Restaurant Chain Management System
- Role: Full-Stack Developer (Personal Project)
- Stack line: FastAPI · SQLAlchemy 2 (async) · PostgreSQL 16 · WebSockets · Docker · React 18 · TypeScript
- Tags: python, fastapi, sqlalchemy, postgres, websockets, realtime, docker, react, typescript, concurrency, auth, devops, testing, fullstack
- Bullets:
  - Built an end-to-end multi-branch restaurant platform (~16K LOC): point-of-sale, kitchen display, QR-code table ordering, inventory, promotions, and profit/COGS analytics — fully bilingual Arabic/English with RTL support. {tags: python, fastapi, fullstack, product}
  - Engineered a real-time order pipeline over WebSockets with per-branch kitchen, POS, and customer channels plus automatic polling fallback, keeping kitchen boards and order trackers in sync live. {tags: websockets, realtime, python, fastapi, api}
  - Designed a concurrency-safe transaction layer: split payments with idempotency keys, atomic promo-code redemption under race conditions, and immutable order-line snapshots for accurate margin reporting. {tags: concurrency, postgres, database, payments, python}
  - Implemented JWT auth with rotating refresh tokens, role-based access control, POS PIN quick-switch, login throttling with lockout, and a full audit log of sensitive actions. {tags: auth, jwt, security, api, python}
  - Shipped to production with Docker (Caddy TLS, Gunicorn, nightly pg_dump backups, uptime monitoring) and ESC/POS thermal receipt printing with Arabic text shaping and a retrying print queue. {tags: docker, devops, deployment, linux, postgres}
  - Wrote 91 automated tests against real PostgreSQL, with strict typing (mypy --strict, TypeScript) and enforced translation-key parity across both languages. {tags: testing, python, typescript, quality, postgres}

### Project: Worldwide — News Publishing Platform
- Role: Full-Stack Developer (Personal Project)
- Stack line: ASP.NET Core MVC · C# · Entity Framework Core · SQL Server · JavaScript · Chart.js
- Tags: csharp, dotnet, aspnet, efcore, sqlserver, javascript, fullstack, auth, identity, migrations
- Bullets:
  - Built a full-stack news platform with a public site and role-based admin dashboard for managing articles, sections, users, and site settings. {tags: csharp, dotnet, aspnet, fullstack, product}
  - Implemented authentication with ASP.NET Core Identity using role- and policy-based access control (Admin, Editor, Reporter) to gate administrative features. {tags: auth, identity, security, dotnet, aspnet}
  - Designed the data layer with EF Core over SQL Server using Repository and Unit of Work patterns, code-first migrations, and AutoMapper. {tags: efcore, sqlserver, database, migrations, dotnet, architecture}
  - Developed an analytics dashboard with interactive Chart.js visualizations backed by AJAX/JSON endpoints, plus transactional email via MailKit. {tags: javascript, frontend, api, dotnet}
  - Migrated the application from .NET 8 to .NET 10, resolving EF Core breaking changes, and redesigned the UI into a responsive design system with light/dark theming. {tags: dotnet, efcore, migrations, frontend, maintenance}

### Project: BidWatch — AI Job Scout Agent
- Role: Developer (Personal Project)
- Stack line: Python · Strands Agents · Amazon Bedrock · SQLite · Telegram Bot API
- Tags: python, ai, llm, agent, api, sqlite, automation, integration, testing
- Bullets:
  - Built an autonomous agent that scans job feeds, scores postings against a skill profile, and drafts tailored applications, using the Strands Agents SDK over Amazon Bedrock with six custom tools. {tags: python, ai, llm, agent, automation}
  - Designed a tool-calling loop with defensive parsing of model output, per-run cost caps, and a deterministic no-model test path so the whole pipeline can run offline. {tags: python, ai, llm, testing, architecture}
  - Integrated the Telegram Bot API with inline-keyboard callbacks and a stateful edit-and-confirm conversation flow, plus SMTP submission with attachments. {tags: python, api, integration, telegram, smtp}

<!-- TODO (Hamed): the WinForms + SQL Server point-of-sale project is listed as a
     skill but has no project entry here, so the generator cannot use it. Add it
     as a "### Project:" block with real bullets if you want it on résumés. -->

---

## Awards
- 1st Place — International Finance Corporation (IFC) program · Program hosted by the IFC (World Bank Group), delivered through Tadawul Financial Group.
- 3rd Place — Libyan Collegiate Programming Contest (LCPC) · National qualifier in the ICPC family of contests.

## Education
- B.Sc. Computer Engineering — University of Tripoli, expected 2027
- Relevant coursework: data structures & algorithms, databases, operating systems, computer networks.

## Certifications
- Boot.dev — Backend Developer Career Path (in progress, 2026)
- The Complete Python Bootcamp: From Zero to Hero — Udemy, 2024
- The Web Developer Bootcamp — Udemy, 2025

## Languages
- Arabic (native) · English (Professional)

---

## Rates
- Target: $15-25/hour, or fixed-price equivalent
- Open to lower on small first projects that build reputation

## Will not bid on
- WordPress, Wix, Squarespace, or page-builder site jobs
- Pure graphic design or heavy visual-design work
- Projects under $50
- Anything requiring on-site presence
- Roles requiring a language other than English or Arabic

## Context
- Computer Engineering student, University of Tripoli (Libya), graduating 2027
- Available part-time, remote only
- Fluent in Arabic and English
- Timezone UTC+2 — overlaps European and Gulf working hours

## Willing, but not expert
- MongoDB
- Blazor
- AI/LLM integration (agent tooling, API integration)

## Proposal voice
- Direct and specific. No filler, no flattery, no "I am excited to apply".
- First sentence must reference the client's actual problem in their words.
- Mention one concrete, relevant thing actually built before.
- Never claim experience not listed above. If a required skill is missing,
  say plainly what is known and what is not.
- End with one clarifying question about the project.
- Under 150 words.
