# BidWatch — Change Request: Rich Notifications + Bid-and-Apply Flow

This extends the existing, working BidWatch project. Do not rebuild it — modify it. Keep the current model (`zai.glm-5`, region `eu-north-1`), the RemoteOK source, and the existing tool architecture.

Execute all changes below. Test them for real before declaring completion. Where something is genuinely ambiguous and not specified here, make the sensible professional choice and note it in the README.

---

## 1. Notification format — critical info only

Each qualifying job produces one message in this exact shape:

```
[85/100] Senior Python Developer
Acme Corp — Remote (US timezones)
About: B2B SaaS company building logistics software.
Salary: $90k–$120k
Why it fits: Strong FastAPI + PostgreSQL match; budget above target rate.

Source: Remote OK
```

Rules:
- **Score first**, in brackets, so it is scannable at a glance.
- **About**: one short line derived from the posting description — what the company does. Omit the whole line if the description yields nothing useful. Never invent it.
- **Salary**: omit the entire line when absent. Never print "N/A" or "Not specified".
- **Why it fits**: the one-line rationale from scoring.
- **Source: Remote OK** attribution on every message (required by their terms).
- Do not include the full job description or the proposal draft in this message — keep it short and scannable.

**Inline buttons on every job message:**
- **`Bid`** — starts the apply flow (section 3)
- **`Open`** — a Telegram URL button linking directly to the Remote OK listing (direct link, no redirect — required by their terms)
- **`Skip`** — marks the posting dismissed so it never resurfaces

---

## 2. Multiple jobs per run

Currently only one or two jobs come through. Change to:

- Raise `MAX_POSTINGS_PER_RUN` default to **15**.
- Score **every** new posting up to that cap.
- Send a notification for **every** posting scoring at or above `SCORE_THRESHOLD` — not just the top one.
- Order messages **highest score first**.
- After the batch, send one closing summary line: `Scanned 40 new postings — 6 above threshold.`
- If nothing qualifies, send nothing at all (no summary, no "no jobs found" message).

---

## 3. The Bid flow (core new feature)

When the user taps **`Bid`** on a job, run this sequence:

### Step 1 — Gather requirements
Fetch the job's application page. Determine:
- The **application method**, classified as one of:
  - `email` — an application email address is discoverable in the posting or page
  - `known_ats` — Greenhouse, Lever, Ashby, or Workable, which expose structured application endpoints
  - `manual` — anything else (custom forms, unknown providers, login-gated pages)
- The **fields required**: name, email, phone, location, cover letter, LinkedIn, GitHub/portfolio, résumé, plus any custom screening questions found on the page.

Post a brief status message while working (e.g. `Reading the application page…`) so the user isn't left waiting on silence.

### Step 2 — Fill
Populate every field from `applicant.md` (new file, section 4). Generate the cover letter tailored to this specific posting, following the voice rules in `profile.md`. Answer any custom screening questions using `applicant.md`; if a question cannot be answered from that file, mark it `⚠️ NEEDS YOUR INPUT` rather than inventing an answer.

### Step 3 — Show the draft
Reply in chat with the complete filled application:

```
Application draft — Senior Python Developer @ Acme Corp
Method: Email (jobs@acme.com)

Name: Hamed Youssef Ezzu
Email: hamed.y.ezzu@gmail.com
Phone: +218 91 006 2163
Location: Tripoli, Libya (UTC+2, remote)
LinkedIn: linkedin.com/in/hamed-ezzu
Résumé: attached (My_Resume.pdf)

── Cover letter ──
<generated letter>
```

Buttons: **`Confirm & Submit`** · **`Edit letter`** · **`Cancel`**

### Step 4 — Edit loop
`Edit letter` prompts the user to reply with instructions in plain language ("make it shorter", "mention the restaurant POS project", "less formal"). Regenerate the letter accordingly and re-show the full draft with the same three buttons. Loop until confirmed or cancelled. Preserve conversation state per job so two concurrent bids don't collide.

### Step 5 — Submit, only on explicit confirmation
- **`email`** → send via SMTP using the `.env` credentials, cover letter as the body, résumé attached, subject line referencing the job title. Report the genuine result (sent / failed, with the error).
- **`known_ats`** → attempt submission through the provider's application endpoint. If it fails, requires auth, or the schema doesn't match, fall back to the `manual` path and say so plainly.
- **`manual`** → reply with the apply URL, the finalized cover letter in a copy-friendly code block, and each field/value listed for pasting. Mark as `applied_manual`.

**Hard rules for this flow:**
- Never submit without an explicit `Confirm & Submit` tap. No timeouts that auto-confirm, no implicit approval.
- Never claim a submission happened when it didn't. Report the actual path taken every time.
- The cover letter must never claim skills or experience absent from `profile.md`.
- Rate-limit submissions: `MAX_SUBMISSIONS_PER_HOUR` in config, default 5.
- Log every submission attempt with its outcome.

---

## 4. New file: `applicant.md`

The user's fixed application data, read fresh on every bid so it can be edited without restarting. Gitignore it and ship an `applicant.example.md` alongside.

Contents to create:

```markdown
# Applicant Details

## Identity
- Full name: Hamed Youssef Ezzu
- Email: hamed.y.ezzu@gmail.com
- Phone: +218 91 006 2163
- Location: Tripoli, Libya
- Timezone: UTC+2

## Links
- LinkedIn: https://www.linkedin.com/in/hamed-ezzu
- GitHub: TODO — add GitHub profile URL
- Portfolio: TODO — add once deployed

## Résumé
- File path: ./My_Resume.pdf

## Standard answers
- Work authorization: Available as an independent contractor, remote only
- Availability: Part-time now; flexible hours. Overlaps European and Gulf working hours.
- Notice period: Immediate
- Salary expectation: $15–25/hour or fixed-price equivalent; negotiable for a first project
- Remote preference: Remote only
- Willing to relocate: No
- Languages: Arabic (native), English (professional working proficiency)
- Years of experience: Computer Engineering student (graduating 2027) with production-deployed
  personal projects; no formal employment history
```

Note the two `TODO` values — leave them as-is and flag them in the final report; the user will fill them in.

---

## 5. Store changes

Extend the posting store with:
- `status`: one of `new | notified | bidding | applied_email | applied_ats | applied_manual | skipped`
- `notified_at`, `applied_at` timestamps
- The submitted cover letter text, retained as a record of what was sent

Only postings with status `new` may be notified. Nothing already notified, applied to, or skipped may resurface.

---

## 6. `bot.py` — Telegram listener

Buttons require a process listening for taps. Implement long-polling that handles:
- `callback_query` events (all button taps)
- Plain text replies (the edit-letter loop)

Answer every callback promptly via `answerCallbackQuery` so buttons don't hang. Edit the original message after an action resolves (e.g. `✅ Applied by email` / `⏭ Skipped`) so the chat stays clean. Run alongside the scheduler — document how to run both.

---

## 7. Console fallback must keep working

With no Telegram credentials configured, everything must still run:
- Print the same rich notification format (buttons omitted).
- Simulate the bid flow with terminal prompts (`[b]id / [s]kip / [o]pen`, then `[c]onfirm / [e]dit / [x]cancel`).
- `--once` and `--dry-run` remain fully testable offline.

---

## 8. Environment

Already configured and verified working in `.env`:
```
SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, FROM_EMAIL
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
```
Update `.env.example` to document all of these. Confirm `.env`, `applicant.md`, and the résumé PDF are gitignored.

---

## 9. README updates

Document honestly:
- The new notification format and what each line means
- The three application paths, stating clearly that **fully automated submission covers email and supported ATS providers only**; everything else is a manual handoff with a prepared letter
- That no application is ever sent without explicit confirmation
- The `applicant.md` / `profile.md` split and what belongs in each
- How to run the scheduler and the bot listener together

---

## 10. Definition of done

- `python scheduler.py --dry-run` prints multiple correctly formatted jobs from fixtures, zero model calls.
- `python scheduler.py --once` against the live feed notifies **several** jobs, highest score first, with the summary line.
- Tapping `Bid` produces a filled draft with a tailored letter; `Edit letter` regenerates it; `Confirm` submits and reports the real outcome.
- A real test email sends successfully via SMTP (test against the user's own address first — do not send test mail to real employers).
- Re-running produces no duplicates.
- `pytest` passes.
- Everything committed; no secrets, résumé, or `applicant.md` in git.

Report at the end: what changed, real output from a live test run, the two `TODO` values still needed, and any decisions made that weren't specified here.
