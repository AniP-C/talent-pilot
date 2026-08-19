# Analyzer redesign — review of the Barclays run, and what to build next

Written against the code as it stands today ([scoring.py](scoring.py),
[ai/resume_parser.py](ai/resume_parser.py)) and against one real case: the
Barclays *AI Engineer, Pune* posting scored against `Aniruddh_Parashar_AI.pdf`,
which returned **60% recruiter fit, 39% keyword match**.

Everything in Part 1 was reproduced locally before it was written down.
**Items 1 to 4 of Part 5 are now implemented.** With all four in, the same
posting and the same CV score **49% recruiter fit / 36% keyword coverage**,
built from 31 atomic requirements instead of nine bundled ones, with the
employer's own programme set aside rather than counted against the candidate.

---

## Part 1 — What actually went wrong

### 1.1 The granularity is set by the JD's bullet formatting, not by its content

The run reported **3/5 must-haves** and **2.5/4 preferred**. The Barclays
posting has exactly four "to be successful" bullets and four "highly valued"
bullets. Five and four. The model extracted **one requirement per bullet**.

But this is one bullet:

> Designing secure, scalable enterprise applications, using microservices, REST
> APIs, event-driven architecture, relational or NoSQL databases, authentication
> and enterprise integration patterns.

That is six requirements. Collapsed into one, the CV's genuine REST API and
enterprise-application evidence gets averaged against missing event-driven
architecture and lands as a single "partial" — worth 0.5 of one fifth of the
must-have weight.

The prompt says *"list every distinct requirement"* but never says a bullet
routinely contains several, and rule 2 ("alternatives are one requirement")
pushes hard in the direction of collapsing. So the two forces are unbalanced:
one rule fights over-splitting, nothing fights under-splitting.

**Fix:** extract in two passes. Pass one splits the JD into atomic
requirement *candidates* with no judgement at all. Pass two classifies each.
Splitting is a task with a right answer; judging is not, and mixing them lets
the model trade one off against the other.

**Largely resolved as a side effect.** Asking for a `kind` per requirement (see
1.2) forced per-item classification, and the same posting now yields **27
must-haves and 4 preferred** rather than five and four — the enterprise-
applications bullet came back as six separate requirements, as it should have
all along. The headline moved 60% → **49%**, because nine unmet cloud-native
requirements that used to average into one "partial" now each count. That is
the number becoming true, not the candidate getting worse.

The two-pass split is still worth doing: this is the model happening to
enumerate well, not the pipeline guaranteeing it. The calibration set in 4.1 is
what would turn one good run into a property.

### 1.2 A programme name became an unwinnable must-have

`SOLD Simplification` was listed as a gap. It is Barclays-internal
nomenclature — nobody outside the bank can evidence it, and no CV edit can fix
it. It was one of five must-haves, so it removed **16 points** on its own.

That is a whole class of requirement the schema cannot currently express:

| Class | Example from this JD | Scoreable? |
| ----- | -------------------- | ---------- |
| Transferable skill | Kubernetes, RAG, FastAPI | yes |
| Employer-internal | SOLD Simplification, "our SDLC" | **no — exclude** |
| Domain experience | payments, financial services | yes, separately |
| Meta / behavioural | stakeholder management, "risk and controls" | rarely — usually noise |
| Logistics | Pune, hybrid | not a skill; a filter |

Adding a `class` field to `Requirement` and excluding `employer_internal` from
the denominator is perhaps twenty lines across
[ai/resume_parser.py](ai/resume_parser.py) and [scoring.py](scoring.py). It is
the single highest-value change on this page.

**Now fixed.** `Requirement` carries a `kind` — `skill`, `domain`,
`employer_internal` or `meta` — and `scoring.SCOREABLE_KINDS` excludes the
unwinnable one from the denominator. It is still returned, in
`coverage.not_scored`, and shown as context in the dashboard and both extension
surfaces: the posting did say it, and silently discarding a stated requirement
would be its own dishonesty. It no longer appears in `missing_skills`, which is
the list both clients label "gaps a recruiter would probe".

Verified against the live model on this posting: `SOLD Simplification` comes
back classified `employer_internal`, `financial services` as `domain`, and
`stakeholder management` as `meta`. An unrecognised or absent kind defaults to
`skill` — the default must never be the one that quietly removes a requirement
from the denominator.

### 1.3 The two scores disagree about what the job asks for

`analyze_jd` knows that "either Python or Java" is one requirement satisfied by
either. `keyword_coverage` does not — it treats every vocabulary term the JD
mentions as independently required. Same posting, two incompatible models of
the same sentence.

Reproduced, raw CV text against the Barclays JD:

```
39%  11/28 terms
matched: Python, LLM, RAG, Prompt Engineering, LangChain, Embeddings,
         Vector Database, Agents, Docker, REST API, FastAPI
missing: TypeScript, Java, LLMOps, MLOps, LlamaIndex, Kubernetes, CI/CD,
         Microservices, React, Angular, Django, Spring, Observability,
         Performance Optimisation, Security, Compliance, Authentication
```

Four of those seventeen are **not gaps at all**:

| "Missing" | The JD's actual words | Reality |
| --------- | --------------------- | ------- |
| Java | "either Python **or** Java" | satisfied by Python |
| Django | "FastAPI/Django **or** Spring Boot" | satisfied by FastAPI |
| Spring | same bullet | satisfied by FastAPI |
| LlamaIndex | "LangChain, LangGraph, LlamaIndex, Semantic Kernel, Spring AI **or** LangChain4j" | satisfied by LangChain |

And React / Angular / TypeScript are three spellings of **one** gap, counted
three times.

Group the terms the way the JD groups them and the honest number is **11 of 22
requirements — 50%**, not 39%. The tool told the user a filter would probably
screen them out, on the strength of gaps the posting never asked for.

**Now fixed.** `_alternation_groups` in [scoring.py](scoring.py) reads the
separators between terms and merges a run into one requirement when its final
connector offers a choice. `A, B or C` is a menu; `A, B and C` is a shopping
list. Java, Django, LlamaIndex, Semantic Kernel, Spring AI and LangChain4j all
stopped being gaps, and React/Angular/TypeScript became one.

One case it still misses: "Python with FastAPI/Django **or** Java with Spring
Boot" is a choice between two *stacks*, and only the halves adjacent to the
"or" get linked — so Spring Boot is still reported as a gap. Nested alternation
needs the parser, not the separator reader.

### 1.4 The ATS scan reads the wrong document

```python
# ai/resume_parser.py
keywords = scoring.keyword_coverage(jd_text, resume_data)   # resume_data = json.dumps(parsed_profile)
```

The entire purpose of that number is to simulate a literal string filter over
**the PDF that gets submitted**. It runs over Gemini's re-encoding of the PDF
instead — a flat skills list and condensed bullets. Anything the parser
normalised, merged or dropped is invisible to a scan whose only job is to be
literal.

The raw text is never stored: [workspace.py](workspace.py) keeps
`profiles/*.json` and nothing else, and `convert_pdf_to_json` discards its
input. So this cannot be fixed without keeping the extracted text — which is
also the prerequisite for your §10 (Candidate Fit vs CV Coverage) and your §30
`resume_versions` table.

**Now fixed.** `senior_ai_engineer.json` is accompanied by
`senior_ai_engineer.txt` — the words as the PDF contains them. The keyword pass
reads that; the model still reads the parsed profile, so no prompt pays to send
a resume twice. Profiles uploaded before this existed have no text and fall
back to the old behaviour rather than breaking. Deleting a profile deletes the
text with it, or a later profile saved under the same name would inherit
somebody else's words.

This is one file per profile, not the versioned table of §30 — but it is the
piece that table was blocked on.

The ligature artefacts came with it, as predicted: `workﬂows`,
`identiﬁcation`, `eﬀort` and `conﬁdence` all arrive from pypdf carrying
U+FB01/U+FB02. `utils.normalise_pdf_text` now runs inside `extract_pdf_text`,
so the parser, the keyword pass and the stored copy all see the same
characters — soft hyphens, non-breaking spaces and smart quotes included. The
real CV now extracts with zero ligatures left in it.

### 1.5 The strongest evidence in the CV is invisible to the scanner

`LangGraph` is **not in `SKILL_VOCABULARY`**. The JD names it. The CV names it
five times. It is the candidate's clearest differentiator and neither number can
see it.

Eighteen terms this posting uses have no vocabulary entry at all:

```
langgraph, semantic kernel, spring ai, langchain4j, devsecops, event-driven,
nosql, guardrails, responsible ai, data privacy, payments, resilience,
automated testing, production support, enterprise integration,
model versioning, cost management, stakeholder management
```

The docstring is honest that an absent term is invisible and calls it "a
visible, one-line-to-fix limitation". It is — but nobody is running the fix,
because nothing reports which JD terms fell outside the vocabulary. **Log the
misses.** A weekly list of unmatched capitalised tokens across all analyses is
how the vocabulary maintains itself, and it costs one query. That part is still
not built.

**Now fixed (the vocabulary half).** LangGraph, Semantic Kernel, Spring AI,
LangChain4j, Ollama, LLM Evaluation, Guardrails, Responsible AI, DevSecOps,
Event-Driven Architecture, NoSQL, Payments and Banking were added, and `Agents`
learned "multi-agent" and "agent orchestration".

### What changed, and a warning about doing half of it

Measured on the Barclays case, in the order the changes were made:

| State | Score | Why |
| ----- | ----- | --- |
| Before | 39% (11/28 terms) | four false gaps, one gap counted three times, ten real gaps invisible |
| Vocabulary only | **32%** | the denominator grew faster than the numerator |
| Vocabulary + grouping | 36% (10/28 requirements) | false gaps gone, real gaps visible |

The middle row is the point. **Expanding the vocabulary without grouping the
alternatives actively makes the number worse**, because most of what you add to
a modern AI posting is a framework named as one option among six. The two
changes are not independent improvements that can be shipped separately; the
first one alone is a regression.

The final number is lower than the original, and it should be. The 39% was
made of four gaps that were not real and ten that were real and unseen. What
the user gets now is the same headline order of magnitude built out of true
statements — and, more usefully, a "missing" list they can act on without being
sent to learn Java.

### 1.6 The prose contradicts the number

The summary opens "The candidate is a **strong fit**". The headline says 60%
and "🟡 Close fit — worth tailoring". Both came out of the same run.

The summary is generated by the model in the same call that classifies
requirements, before any arithmetic happens, so it is a second opinion rather
than a description of the result. Either generate the verdict text *from* the
computed numbers, or feed the numbers back for a one-line second call. The
first is cheaper and deterministic.

---

## Part 2 — Where your design is right

Points I would keep exactly as you wrote them:

- **§1 (don't collapse scores too early)** and **§10 (Candidate Fit vs CV
  Coverage)** are the core insight, and §1.4 above is the proof: today's two
  numbers are already trying to be that distinction and are undermined by
  reading the same lossy document.
- **§4/§5 (classify requirements by type and importance)** — the SOLD case is
  exactly what this fixes.
- **§8/§9 (evidence engine and hierarchy)** — the `evidence` field already
  exists on `Requirement`; it is free-text and unverified. Making it a
  *citation* is the natural next step.
- **§15 (missing needs multiple meanings)** — "underrepresented" vs "no
  evidence" is the difference between a CV edit and a career decision. Today
  both render as ❌ Absent.
- **§16 (safe-to-add engine)** and **§18 (don't-change list)** — this is the
  most commercially distinctive idea in your document. It is also the one with
  real liability attached, which is why §18 is not optional.
- **§26/§27 (decompose the prompt, use the cheap tool for the cheap question)** —
  the codebase already believes this. `scoring.py` exists precisely because the
  model was asked for arithmetic it could not do honestly, and
  `keyword_coverage` runs with no model at all.
- **§12 (no fake precision)** — already respected; keep it that way.

---

## Part 3 — Where I would push back

### 3.1 Don't hand-maintain a skill ontology

Your §6 is right about the problem and expensive about the solution. A
hand-curated graph of aliases and broader/narrower relations is a permanent
part-time job, and it decays silently — exactly like the 18 missing terms
above.

Do this instead, in order:
1. Keep the deterministic alias table for the **hot set** (the ~200 terms that
   actually appear in the postings your users open). It is fast, free and
   auditable.
2. Mine new candidates automatically from the "JD terms with no vocabulary
   entry" log. Review a list, not a graph.
3. Use embeddings for the tail, and *never* let an embedding hit produce a
   green tick — it produces 🟡 related evidence, as you already said in §7.

The distinction you drew in §6 (FastAPI ≠ microservices) is right and is the
reason the broader/narrower graph is not worth hand-building: you would encode
the edge and then have to encode that it does not transmit.

### 3.2 Don't move to Postgres + pgvector yet

Your §25 is a reasonable end state and a poor next step. The app's data model
is currently *one SQLite database per user workspace*, and that is not
incidental — it is how [workspace.py](workspace.py) guarantees isolation, and
it is 60% of the security story in the README. Moving to a single shared
Postgres means re-earning that guarantee with row-level rules, on a product
with (today) a handful of accounts.

`sqlite-vec` or a plain numpy dot-product over a few hundred cached embeddings
will carry you a very long way. Postgres becomes right when you need
cross-user queries — which, note, is exactly what the new
[usage.py](usage.py) does, and it needed one central SQLite table, not a
migration.

### 3.3 Six proficiency levels will not survive contact with users

`Learning / Academic / Project / Professional / Production / Expert` — nobody
grades themselves consistently across six bands, and the difference between
"Professional" and "Production" is not a question a person can answer about
their own work. You will get self-assessment noise and then score on it.

Three, defined by *evidence* rather than by feeling:
- **Shipped** — used in something that ran for real users
- **Built** — used in a personal or academic project
- **Studied** — read, coursed, certified, not built

Each maps onto something in the CV, so the system can infer it and ask the user
to confirm rather than asking them to introspect.

### 3.4 The "portal profile" will rot unless it is derived

Your §2 has the user maintaining a structured profile alongside their CV. In
practice the CV is the artefact people actually update, because it is the one
they send. A hand-maintained second profile goes stale within two months and
then quietly poisons every score.

Invert it: the profile is **derived** from the CV on upload, and the portal is
where the user *confirms, corrects and adds* — with every field carrying its
provenance (`from_cv`, `confirmed`, `added_by_user`). That is also what makes
§16 work: "React — you confirmed this in your profile, it is not in your CV" is
only possible if the two are separate sources with a known relationship.

### 3.5 "Tailoring effort: 10 minutes" is a guess dressed as data

Your §21 is a good instinct — prioritising by effort is genuinely useful — but
a minutes estimate implies a measurement you cannot make. Rank instead:
**quick tailoring / substantial rewrite / not worth it**, derived from how many
missing requirements are "underrepresented" (a CV edit) versus "no evidence"
(not fixable). That is honest, and it is the same decision support.

---

## Part 4 — What is missing from your 33 points

### 4.1 A calibration set — the thing that makes all the rest safe

You are proposing to change prompts and scoring repeatedly. Right now there is
no way to know whether a change helped, because every judgement is one
non-deterministic call about one document.

Build a fixture set of 15–25 real JD + CV pairs with **hand-written expected
outputs** — for each pair, the requirements you believe should be extracted,
their importance, and the verdict. Run the extractor against it and report
precision/recall on requirement extraction, plus verdict agreement. The Barclays
case is fixture number one: the expected output includes "SOLD Simplification is
excluded, not scored" and "Java is not a gap".

Without this, every future prompt change is a vibe. With it, §26's staged
pipeline is testable stage by stage. This is the highest-leverage item in this
document and it needs no new architecture.

### 4.2 Evidence as citations, not prose

Your §9 hierarchy needs something to hang on. Make evidence a span, not a
sentence:

```json
{
  "requirement": "Agentic AI",
  "status": "demonstrated",
  "evidence": [
    {"source": "cv", "section": "Experience → TCS/Bayer", "offset": [1204, 1338],
     "quote": "multi-agent Agentic AI system using LangGraph"}
  ]
}
```

Offsets into the stored CV text make three things possible that prose cannot:
the UI can highlight the sentence, the system can *verify* the quote exists
(catching fabricated evidence mechanically), and §16's rewrite suggestions know
which sentence to rewrite.

### 4.3 Recency, and the difference between "has it" and "has it now"

Nothing in your model distinguishes a skill used last month from one used in
2021. For an AI role in 2026 that gap matters more than most. You already have
the dates in the CV parse; attach each piece of evidence to the role it came
from, and let the UI say "LangGraph — current role" versus "PyTorch — 2023".
Don't decay the score automatically; show the date and let the human judge.

### 4.4 Version the CV text, and attach outcomes to the version

Your §22/§23 feedback loop is only learnable if "which CV got the interview" is
answerable. That needs `resume_versions(id, user_id, uploaded_at, raw_text,
parsed_json)` and a `resume_version_id` on the application row — not just the
filename that `resume_used` stores today. Cheap to add now, impossible to
backfill later.

### 4.5 A cost ceiling per analysis

Your §26 splits one call into six. Six calls per analysis at today's prices is
still cheap, but the new [usage.py](usage.py) exists because that is no longer
theoretical — one account already accounts for most of the spend. Put a hard
per-analysis call budget in code, and record `model_calls` alongside the usage
event, so the pricing question stays answerable as the pipeline gets deeper.

### 4.6 Say when the tool does not know

Two failure modes deserve first-class output states rather than a low score:
JD text that is mostly benefits-and-culture boilerplate (the Barclays page is
half "This is Barclays Pune"), and a CV that parsed badly. Both currently
produce a confident percentage. "This posting states too few concrete
requirements to score" is a better answer than 34%.

---

## Part 5 — Sequencing

The first four cost hours, not weeks, and fix most of what you saw.

| # | Change | Files | Effort |
| - | ------ | ----- | ------ |
| 1 | ~~Add `LangGraph` and the missing terms to `SKILL_VOCABULARY`~~ **done**; logging JD terms with no entry still to do | [scoring.py](scoring.py) | done / 30 min |
| 2 | ~~Group alternatives in `keyword_coverage`~~ **done** — one requirement per "A or B or C" | [scoring.py](scoring.py) | done |
| 3 | ~~Add `class` to `Requirement`; exclude `employer_internal` from the denominator~~ **done** | [ai/resume_parser.py](ai/resume_parser.py), [scoring.py](scoring.py) | done |
| 4 | ~~Store the extracted CV text; run `keyword_coverage` against it, normalising ligatures~~ **done** — one file per profile, not yet versioned | [workspace.py](workspace.py), [utils.py](utils.py), [ai/resume_parser.py](ai/resume_parser.py) | done |
| 5 | Split extraction from judgement (two prompts), with the calibration set to prove it | new fixtures + [ai/resume_parser.py](ai/resume_parser.py) | 2–3 days |
| 6 | Evidence spans with quote verification | schema + prompt | 2–3 days |
| 7 | Derived profile with provenance, and the safe-to-add engine | new module | 1–2 weeks |

Only after 1–5 does the rest of your §31 Phase 1 become worth building — and by
then you will have the calibration set that tells you whether each piece of it
actually helped.

### Where the number went, across all four changes

| | Recruiter fit | Keyword | Built from |
| - | - | - | - |
| Originally | 60% | 39% | 5 must-haves, 4 preferred; one of the five unwinnable; four false keyword gaps |
| Now | **49%** | **36%** | 27 must-haves, 4 preferred, all real; alternatives grouped; the CV's own text |

Both numbers went **down**, and both are now true. The 60% averaged nine
missing cloud-native requirements into a single "partial" and spent a fifth of
its must-have weight on a Barclays-internal programme. What replaced it is a
list of thirty-one statements, each of which can be checked by eye.

### What the Barclays run should have said

```
Strong technical fit · domain gap · worth tailoring

Recruiter fit      Strong on the AI stack (RAG, agentic, LangGraph, FastAPI)
Real gaps          Frontend (React/Angular/TypeScript) · Kubernetes · CI/CD
Not scored         SOLD Simplification (Barclays-internal)
Preferred, missing MLOps/LLMOps naming · financial services domain
Safe to add        LangGraph, evaluator agents, LLM evaluation — all in your CV,
                   none in the words this posting searches for
```

Not "60%".
