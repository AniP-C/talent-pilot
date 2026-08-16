# Talent Pilot — Every Feature, In Detail

What the product does, feature by feature: what each one is for, how to use it,
what it does behind the scenes, and where it stops.

The other documents cover different ground. [README.md](README.md) is the
overview and setup guide, [PROJECT.md](PROJECT.md) explains *why* the system is
built the way it is, [TECHNICAL_OVERVIEW.md](TECHNICAL_OVERVIEW.md) is the
internals reference, and [DEPLOYMENT.md](DEPLOYMENT.md) is for hosting it. This
one is the complete functional description.

**Live at [katchjobs.online](https://katchjobs.online).**

---

## Contents

1. [The shape of the product](#1-the-shape-of-the-product)
2. [Accounts and sign-in](#2-accounts-and-sign-in)
3. [Resume profiles](#3-resume-profiles)
4. [The browser extension](#4-the-browser-extension)
5. [Job detection and extraction](#5-job-detection-and-extraction)
6. [Match analysis and scoring](#6-match-analysis-and-scoring)
7. [The application answer bank](#7-the-application-answer-bank)
8. [In-page form suggestions](#8-in-page-form-suggestions)
9. [AI answer drafting](#9-ai-answer-drafting)
10. [Catching an application as you submit it](#10-catching-an-application-as-you-submit-it)
11. [The tracker dashboard](#11-the-tracker-dashboard)
12. [Follow-ups: what has gone quiet](#12-follow-ups-what-has-gone-quiet)
13. [Stage history](#13-stage-history)
14. [Gmail inbox sync](#14-gmail-inbox-sync)
15. [Recruiter contacts](#15-recruiter-contacts)
16. [Activity log](#16-activity-log)
17. [Settings and operations](#17-settings-and-operations)
18. [Privacy and security](#18-privacy-and-security)
19. [Known limits](#19-known-limits)

---

## 1. The shape of the product

Talent Pilot is a job-application tracker with three parts that share one
account:

| Part | What it is | Where it runs |
| ---- | ---------- | ------------- |
| **Chrome extension** | Reads job pages, fills forms, saves applications | Your browser |
| **API** | Everything the extension asks for | FastAPI, port 8000 |
| **Dashboard** | Where you look at and manage everything | Streamlit, port 8501 |

The loop it is built around:

**Detect → Analyse → Save → Track → Sync**

You are on a job page. The extension reads it, scores it against your resume,
and saves it. Recruiter emails arriving later update the status by themselves.
The dashboard is the single place that knows where everything stands.

Every user's data lives in its own directory and its own SQLite file, keyed by
account id:

```
data/workspaces/<user_id>/
    jobs.db          applications, stage history, processed email ids
    profiles/        parsed resumes, one JSON per profile
    answers/         saved long-form answers
    autofill.json    the application answer bank
    gmail_token.json Gmail credentials, if connected
```

---

## 2. Accounts and sign-in

**What it is.** One account works across the dashboard and the extension.

**How to use it.** Register on the dashboard or from the extension popup. Sign
in on either; they are the same credential.

**Registration control.** A deployment can be locked down three ways:

| Setting | Effect |
| ------- | ------ |
| Open | Anyone can register |
| Invite code | Registration requires a shared code |
| Closed | No new registrations at all |

The extension popup reads the server's policy from `/health` and shows or hides
the invite field to match, so the form always reflects the server it is
actually talking to.

**Single sign-on handoff.** "Dashboard ↗" in the extension popup asks the API
for a one-time code and opens the dashboard already signed in, rather than
presenting a second login form.

**Sessions.** API tokens last 30 days and are stored as SHA-256 digests, so a
copy of the database grants no sessions. The dashboard keeps you signed in
across reloads with a cookie holding a revocable token — not credentials.

**Rate limiting.** Sign-in attempts are limited per email and per IP, and the
limit survives a restart. Failures take constant time, so accounts cannot be
enumerated by timing.

---

## 3. Resume profiles

**What it is.** A resume, uploaded as a PDF, parsed into a structured profile
you can keep several of — typically one per target role.

**How to use it.** Upload from the dashboard's **Profiles** tab or from the
extension popup. Name it something you will recognise ("AI Engineer",
"Backend"). Switch the active profile from the sidebar or the popup dropdown.

**What happens.** Text is extracted from the PDF, then Gemini converts it into
a structured JSON profile — name, contact details, skills, experience with
bullet points, education. That structure is what match analysis and answer
drafting read.

**It seeds your answer bank.** After a successful upload, contact details and
employment facts are copied into the [answer bank](#7-the-application-answer-bank)
so the questionnaire arrives mostly filled in. Existing answers are never
overwritten — correcting a badly parsed phone number is not undone by
re-uploading the same PDF.

**Limits.** 5 MB, PDF only. A scanned image with no text layer fails with a
clear message rather than producing an empty profile.

---

## 4. The browser extension

**What it is.** The part that does the work on the page.

**Where it runs automatically.** LinkedIn, Greenhouse, Lever, Wellfound, Ashby,
Workable, Workday, SmartRecruiters, iCIMS, Jobvite, BambooHR, Breezy,
Recruitee, Teamtailor, JazzHR, Zoho Recruit, Taleo, SuccessFactors, Work at a
Startup, Pinpoint, Naukri and Indeed — including application forms embedded in
an iframe on a company's own careers page.

**Anywhere else.** Open the popup and click **Enable Copilot on this site**.
That grants access to that one origin and nothing else. The grant is permanent:
the site is registered and the extension runs there on every visit afterwards.

**What the popup shows.**

- The company and role detected on the page
- **Analyze match** — score this posting against your resume
- **Save to tracker** — record the application
- A prompt for the company name when the page does not name it
- "Already tracked" when you have saved this one before, with its current status
- A setup nudge when your answer bank is incomplete
- Settings for the API address, so one build works against a local or hosted server

**How it is wired.** Every network call goes through the extension's background
service worker, which owns the auth token. The token never enters page context,
and requests come from the extension's own origin — the only origin the API's
CORS policy accepts. Anything scraped from a page is rendered with
`textContent`, never `innerHTML`.

---

## 5. Job detection and extraction

**What it is.** Working out the company, the role, and the job description from
whatever page you are on.

**How it decides.** Sources are tried strongest first:

| Order | Source | Why it is trusted at that level |
| ----- | ------ | ------------------------------- |
| 1 | JSON-LD `JobPosting` | A declared field, not a guess. Most ATS pages emit it |
| 2 | Per-board selectors | Real elements, no string surgery |
| 3 | `og:site_name` | The site naming itself |
| 4 | Page title patterns | Last resort, and board-specific |

**Why the ordering matters.** The company is the field that matters most,
because an application's identity is `(company, role)`. A company field holding
a job title means later recruiter emails cannot find the application they
belong to, and open a duplicate instead.

Title parsing is per-board because boards disagree: Lever writes
"Company - Role", Workable and Ashby write "Role - Company". A single split
rule returns the wrong half on half the internet.

**It refuses to guess.** Any candidate that reads like a job title rather than
an employer is discarded. When nothing survives, the extension reports no
company and the popup asks you to type it. A wrong company is worse than no
company: it becomes a corrupt row that only surfaces weeks later as a
duplicate.

**Salary and location come from the same block.** The JSON-LD `JobPosting`
declares `baseSalary` and `jobLocation`, and both were being parsed past and
thrown away. They are now captured and shown on the card, in the popup, and as
**Where** and **Pay** columns in the tracker.

The salary is stored as *numbers* — minimum, maximum, currency, period — rather
than as a display string, so it can be filtered and compared later rather than
only read. A posting quoting one figure stores it as a range of width zero, so
every reader handles one shape instead of three.

**Only the declared field is trusted for pay.** A salary is read from the
JSON-LD block and nowhere else. "Competitive package, £70k OTE" in a paragraph
is not a stated number, and a board that renders a range without declaring it is
a board whose markup will move. An empty salary field is corrected by the next
posting; a wrong one is a number you make a decision on. The location is less
dangerous to get wrong, so it does fall back to a per-board selector — which is
what covers LinkedIn, where no JSON-LD block exists at all.

What else it will not accept: an ambiguous currency symbol (`$` is USD, CAD, AUD,
SGD and more, so the amount is kept and the currency is not), an amount that
cannot be a salary (a requisition id, an epoch timestamp), and a period the
amount cannot be quoted in — £120,000 *per hour* keeps the number and drops the
period. "Remote" in the location slot becomes the remote flag rather than the
place, so the two can never disagree.

---

## 6. Match analysis and scoring

**What it is.** How well your resume matches a posting — reported as two
numbers, because there are two different questions.

| Score | Question it answers |
| ----- | ------------------- |
| **Recruiter fit** | Would a person reading this think you can do the job? |
| **Keyword coverage** | Will an automated filter surface you at all? |

**Where you see it.** Three places, and the first one you do not have to ask
for:

| Surface | What it shows | What it costs |
| ------- | ------------- | ------------- |
| **The card in the job page** | Keyword coverage, on arrival | Nothing |
| Same card, **Full AI match** | Recruiter fit, gaps, summary | One model call |
| Extension popup → **Analyze match** | The same, in the popup | One model call |
| Dashboard → **Analyzer** tab | The same, plus the requirement table | One model call |

**The card in the page.** Open a posting on a supported board and a card
appears just above the job description with the keyword score already computed:
"4 of 9 keywords are in your resume", the terms you have, and the terms you do
not. **Full AI match** turns it into the requirement-by-requirement verdict;
**Save** files the job without opening the popup.

It scans on arrival because that half is free — keyword coverage is string
matching with no model behind it, so showing it unprompted costs nothing. The
paid analysis stays behind a button. This is the same rule as the inbox filter:
the cheap pass first, the expensive one only once it has earned the call.

The card renders into a **closed shadow root**. A job board's stylesheet cannot
reach in and wreck it, and — the reason that matters — page scripts cannot read
back out. Which skills you lack and how poorly you match is your business, not
the employer's.

It can be turned off in the popup's **Settings** panel, and closed for one page
with the **✕**. Closing hides it; only the setting turns it off everywhere,
because a close button that silently disables a feature is a trap.

**How recruiter fit is calculated.** The model classifies; Python scores. Every
requirement the posting states is listed and classified:

- **Importance** — `required` (a must-have) or `preferred` (a bonus)
- **Status** — `demonstrated`, `partial` (adjacent or transferable evidence —
  a different cloud provider, the technique without the named tool), or `absent`
- **Evidence** — where in your resume it was found

The score is then computed in [scoring.py](scoring.py): must-haves carry 80% of
the weight and nice-to-haves 20%, with partial evidence earning half credit.
When a posting states only must-haves, they take the full weight.

**Why it works this way.** A language model asked for a percentage with no
rubric returns a number reflecting its disposition rather than the evidence.
In practice almost everything came back in the high eighties — and it could
contradict its own summary, returning 90% for an analysis that listed a dozen
absent requirements. A score nobody can reproduce or explain is not a
measurement. Now the same input always gives the same number, and the dashboard
shows the requirement table it was derived from.

**How keyword coverage is calculated.** No AI at all. A curated vocabulary of
technical terms is checked literally against the posting and your resume. Terms
the posting never mentions are not counted, so the denominator is what this job
actually asks for.

Two mistakes are structurally impossible here. One skill spelled several ways
is a single entry, so "LLM" cannot be a match while "Large Language Models" is
counted as a separate miss. And only listed technical terms are ever
considered, so generic prose — "collaborate", "best practices", "solutions" —
never pads the count.

The output names the terms the posting uses that your resume does not. Those
are the literal strings worth surfacing **provided they are true**.

**Limits.** A term absent from the vocabulary is invisible to the keyword pass.
That is a deliberate trade: a visible, one-line-to-fix gap beats the invisible
noise of extracting keywords from arbitrary prose.

---

## 7. The application answer bank

**What it is.** The two dozen questions every application asks, answered once.

**How to use it.** Dashboard → **Application answers**. The tab header shows
how many are still unanswered. Blank answers are simply never suggested, so
skipping ones that do not apply to you is fine.

**What it covers.**

| Group | Examples |
| ----- | -------- |
| Personal | Name, email, phone, location, LinkedIn, GitHub, portfolio |
| Work authorisation | Right to work, visa sponsorship, citizenship, visa status |
| Employment | Current employer and title, years of experience, notice period, current and expected salary, reason for leaving, start date |
| Education | Highest qualification, university, graduation year, field of study |
| Logistics | Relocation, remote, on-site/hybrid, travel |
| Compliance | Criminal record, background check, drug test, veteran status, disability, gender, ethnicity |
| This employer | Previously worked here, relative employed, referral, how you heard |

**Your own questions.** Anything the catalogue does not cover can be added by
hand, and answers drafted with AI are saved here automatically.

**Why order matters.** Matching walks the catalogue and takes the first hit, so
specific questions are tested before general ones — "first name" before "name",
or every name field fills with your full name.

**One deliberate separation.** "Are you authorised to work?" and "Do you
require sponsorship?" are stored as separate answers with a warning attached.
They are opposites, and filling both with the same value is the classic
mistake.

---

## 8. In-page form suggestions

**What it is.** A small prompt beside a form field that has a saved answer.

**How to use it.** Fill in your answer bank, then open any application form. A
field it recognises grows a **💡 Saved answer for "…"** box with a **Fill**
button.

**What it does not do.** It never fills anything on sight, and it never
submits. The value moves into the field only when you click.

**It never renders your answer into the page.** A content script shares the DOM
with the page, so anything written there is readable by page scripts. Only the
question label is shown — never the value. Otherwise every site you enabled
would be handed your phone number, email, and your disability, ethnicity and
veteran-status answers without you doing anything at all.

**How a field is understood.** The scan walks form fields and works out what
each is asking, from labels, `aria-label`, `aria-labelledby`, `placeholder`,
the field name, and a `<fieldset>` legend — which is where compliance questions
buried in a paragraph of legal text usually live. A radio group is treated as
one question, not one per option.

**What it fills.** Text inputs, textareas, `<select>` dropdowns by matching
option text, and radio or checkbox groups by matching label text. React-based
forms are handled by dispatching the input and change events they listen for.

**Staying current.** Your answers are re-read whenever they change — on
sign-in, sign-out, or after saving an answer in another tab — so a form open in
a tab does not need reloading to pick them up.

---

## 9. AI answer drafting

**What it is.** A **✨ Generate AI Answer** button under long-form answer boxes.

**How to use it.** Click it. What happens depends on what already exists:

1. **A saved answer for this question** — used immediately. No model call, no
   cost, and it is what you already decided to say.
2. **A cached answer for this question on this page** — restored.
3. **Otherwise** — drafted by Gemini, then saved to your answer bank so the same
   question is instant and free next time.

Long-form essays over 2000 characters are deliberately not saved for reuse:
they are tailored per application, and replaying one verbatim reads worse than
redrafting it.

**What the draft is grounded in.** Your resume profile, your previously saved
answers, the company, the role, and the job description. Where the posting is
on a different page from the form — or the form is an embedded iframe with none
of the description in it — the description captured when you saved the job is
used instead.

**Rules the model works under.** Under 200 words, no invented employers, dates
or metrics, respect facts from your previous answers, and connect the resume to
the posting's stated requirements rather than describing you in general terms.

**It always says "review before submitting".** The draft is a starting point.

---

## 10. Catching an application as you submit it

**What it is.** An offer to track an application at the moment you submit it.

**Why it exists.** Saving was a click in the popup, so anything you filled in
without remembering to click was never tracked — the largest gap between
"applied" and "tracked", and the one you cannot see.

**How it behaves.** When you submit a form that looks like an application — one
with a resume upload, a free-text question, or simply several fields — a small
bar appears offering to save it. A one-field search box never triggers it.

**It survives the page navigating away.** Submitting usually loads a
confirmation page, which would destroy a bar drawn on the spot. The offer is
written to extension storage and picked up by whichever page loads next. Forms
that post over XHR stay put and see it immediately.

**It never saves by itself.** The bar offers; you click. The extension does not
get to decide you applied to something. The offer expires after ten minutes,
and it is skipped entirely for a job already in your tracker.

---

## 11. The tracker dashboard

**What it is.** Every application, with search, filters and summary metrics.

**What is stored per application.** Company, role, job description, status,
date applied, link, notes, source, which resume was used, the posting's location
and salary, recruiter contact, and timestamps.

**Statuses.**

| Status | Meaning |
| ------ | ------- |
| 🔵 Applied | Submitted, nothing back yet |
| 🟡 Action Required | They need something from you |
| 🟠 Assessment | A test or take-home |
| 🟣 Interview | Interviewing |
| 🟢 Offer | Offer made |
| 🔴 Rejected | Closed |

**What you can do.** Search by company, role or location, filter by status,
show only remote roles, change a status, add an application by hand, read notes,
view stage history, and delete.

**Where and Pay.** Two columns rendered from what the posting declared about
itself. They are formatted for reading and stored structured, so "which of these
paid over 20 lakh?" stays answerable later — the same reason the score is
computed from classifications rather than asked for as a number.

**Duplicates.** "Already tracked" is a unique index on
`(LOWER(company), LOWER(role))` — enforced by the database rather than by a
check in code, so it is the same rule everywhere and cannot race.

---

## 12. Follow-ups: what has gone quiet

**What it is.** A **🔔 Needs a nudge** panel at the top of the dashboard listing
live applications that have gone silent, longest first.

**Why it exists.** Status alone cannot tell "applied on Tuesday" from "applied
in March and never heard back". The second is the one that needs a decision.

**How silence is measured.** From the most recent real signal — the last email
the employer sent, the last stage change, or failing both the date you applied.
Measuring from the application date alone would report every long-running
process as neglected, and a list that is mostly wrong stops being read.

**What is excluded.** Offers and rejections. Neither is waiting on anybody.

**What is included that you might not expect.** An application with no usable
date at all is surfaced rather than hidden — a job nothing is known about is
exactly the kind that gets forgotten.

**Adjustable.** The quiet threshold defaults to 10 days and can be changed in
the panel. Each entry shows the status, how long it has been silent, the
recruiter to reply to if one is known, and a link to the posting.

---

## 13. Stage history

**What it is.** An append-only record of every status observation, whether or
not it changed anything.

**Why it exists.** The applications table knows only where something stands
*now*, which cannot answer "how long did they sit on my assessment?" or "when
did this go quiet?".

**What is recorded.** The previous and new status, whether it was applied, what
caused it (Manual, Email Sync, Migration), the reason, and when.

**Rejected moves are recorded too.** Inbox sync will not move an application
backwards — Gmail hands back several days of mail at once, so a sync can
legitimately see "application received" and "we would like to interview you" in
the same run. Without a ranking, whichever was processed last would win. The
rejected observation is still written to history with a flag, so a
wrong-looking timeline can be explained rather than guessed at.

**A person always wins.** Editing a status in the dashboard bypasses the
ranking. The rule exists to stop out-of-order email rewinding an application,
not to stop you correcting a mistake.

---

## 14. Gmail inbox sync

**What it is.** Recruiter mail read and turned into status updates.

**How to use it.** Dashboard → connect Gmail → grant consent → run a sync.

**What it can access.** `gmail.readonly` only. The app can read mail and
nothing else — it cannot send, delete, or modify anything.

**How a sync runs.**

1. Recent mail is fetched, oldest first, so an application ends on its most
   recent state.
2. Messages classified on an earlier run are skipped **before any AI call**, so
   a repeat sync costs nothing and cannot duplicate notes.
3. A keyword rule filter runs next, so newsletters never reach a paid call. It
   matches phrases rather than bare words, because "offer" alone also matches
   "limited time offer" — exactly the marketing mail it exists to exclude.
4. What survives is classified by Gemini in a single call into a status, a
   company, a role, the recruiter's name, address and phone, what you have to do
   next, and any deadline the message sets. All of it comes from one request —
   asking for more structure costs nothing extra, and each field removes a guess
   the pipeline would otherwise make on its own.
5. The matching application is updated, or a new one is created.

**When it declines to act.** Every skip is logged with its reason: the category
is not a tracker status, it is not about an application you submitted, it looks
like phishing, no usable company name, or confidence below threshold. "Skipped"
as a bare number is not actionable; knowing *which* tells you what to improve.

**How an email finds its application.** By company and role together. Matching
on company alone was wrong — two applications at one company meant a rejection
for the second silently flipped the first. When the email names no role, the
company is used only if it has exactly one tracked application; otherwise a new
row is created rather than corrupting a good one.

---

## 15. Recruiter contacts

**What it is.** Who to reply to — name, address and direct line — recorded
automatically against each application.

**Why it exists.** Inbox sync always knew far more about a message than the
tracker kept. Only the `From` header was recorded, and only when it looked
replyable, so an ATS relay (`no-reply@greenhouse.io`) left the application with
no contact at all — even when the mail set a `Reply-To` to the recruiter and
signed off with their name and mobile. "Who is handling this?" was a question
the tracker could not answer about its own data.

**Where it looks, strongest signal first.** Headers beat prose, and prose beats
a guess:

| Source | Why it ranks there |
| ------ | ------------------ |
| `Reply-To` | Set by the sending system. On ATS mail this is the employer while `From` is the vendor. |
| `From` | The same, one step weaker — it is often the relay. |
| The signature, as read by the model | A person typed it, but the model is reading attacker-controlled text. |
| The first replyable address in the body | No name attached, but better than nothing. |

The same ordering picks the company: a `Reply-To` domain identifies the employer
on mail that `From` attributes to Greenhouse.

**The model is not trusted.** An email body is written by whoever sent it, so an
address or a number the model reports is accepted **only when the body verbatim
contains it**. A message cannot conjure a contact it does not name, and cannot
conjure one at all if the model was the only source. A body saying "reply to
payments@acme-verify.example to release your offer" gets recorded as a mentioned
address and never as the person to contact.

**What is stored where.** Columns are for "who do I reply to": name, address,
phone, last heard. The note is for "what did this say": the sender, every other
address the message named, what you have to do next, and any deadline. A shared
`careers@` inbox is context worth keeping and not somebody to phone, and the
split is the point.

**Judgement calls.**

- Send-only mailboxes — `no-reply@`, `notifications@`, `mailer@`, and per-message
  bounce addresses at ESP domains — are never stored as contacts. A mailbox that
  cannot receive reads as somebody to reply to, which is worse than nothing.
- Your own address is excluded explicitly. It appears in almost every
  confirmation ("we received your application from you@gmail.com") and would
  otherwise be recorded as the recruiter.
- A phone number needs evidence: a label ("Mobile:", "Direct line") or an
  international prefix. A bare run of digits is a requisition id or a salary
  band as often as a number, and a wrong number in a tracker eventually gets
  dialled.
- A name is kept even when every address was a robot. "Priya in Acme Talent said
  X" is still more than an empty field.
- A later automated message never displaces a human already on the record.

**But automated mail still counts as contact.** An ATS acknowledgement is the
employer making contact, and that is what the follow-up clock measures silence
against.

**Where you see it.** As a `mailto:` and a `tel:` link in both the follow-up
panel and the application editor — the point of capturing them is that replying
is one click rather than one trip back to Gmail.

---

## 16. Activity log

**What it is.** Recent sync decisions and stage changes, in the dashboard.

**Why it exists.** Hosted, log files sit on a VM behind SSH, which in practice
means nobody reads them. Every automated status change is an unattended
decision about your data, so it belongs somewhere you can actually see it.

---

## 17. Settings and operations

**API address.** The extension can be pointed at a local or hosted server.
Changing it requests permission for that origin and clears the session, since a
token from one server is meaningless to another.

**The in-page match card.** On by default, and switched off from the same
Settings panel. Injecting into somebody else's page is the kind of thing that
should always have an off switch that is easy to find. Turning it off takes the
card off pages that are already open, without a reload.

**Backups.** Nightly, 14-day retention. The restore path has actually been
exercised and the databases checked afterwards — an untested backup is not a
backup.

**Hosting.** Designed for a GCE `e2-micro` free tier instance with systemd and
Caddy, which terminates TLS with auto-renewing Let's Encrypt certificates and
routes API paths to 8000 and everything else to the dashboard.

---

## 18. Privacy and security

- Passwords: PBKDF2-HMAC-SHA256, 600,000 iterations, per-user salt
- API tokens stored as SHA-256 digests
- Sign-in failures take constant time, so accounts cannot be enumerated
- Rate limiting per email and per IP, surviving restarts
- CORS restricted to extension origins, so no web page can call the API
- Gmail access is read-only
- Workspaces are keyed by numeric account id, so no user input ever controls
  where files land
- No endpoint accepts an email in the request body; identity comes from the
  token and nothing else
- Your answers are never rendered into a web page — only the question label is
- Nothing is ever filled or submitted on your behalf without a click

Privacy policy: <https://katchjobs.online/privacy>

---

## 19. Known limits

**Extraction is heuristic.** Job boards change their markup and selectors
break. The failure is visible — the extension reports no job detected — but it
needs occasional maintenance.

**The keyword vocabulary is curated.** A technical term not in the list is
invisible to keyword coverage.

**SQLite is the ceiling.** Fine for a handful of users on one box. A real
multi-tenant deployment would want a server-side database.

**It depends on Gemini.** Quota exhaustion or an outage degrades to handled
error messages rather than retries.

**Latency.** The free hosting tier only covers US regions, so from India there
is around 250 ms of round-trip on every page load.

**Gaps we know about.** No password reset without database access, no email
verification, and no audit log beyond the application log.

**Near-duplicates still slip through.** "Sr. AI Engineer" and "Senior AI
Engineer" at one company are two rows, because the uniqueness index is exact.
