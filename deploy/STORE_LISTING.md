# Chrome Web Store listing copy

**Paste the text in this file into the Web Store listing. Do not paste from
[README.md](../README.md) or [PROJECT.md](../PROJECT.md).**

Those documents name every supported job board and applicant tracking system,
which is correct and useful as technical documentation. In store metadata the
same list reads as keyword stuffing, and the listing was rejected for exactly
that:

> **Keyword spam** — Having excessive and/or irrelevant keywords in the item's
> description.
> Violating content: *LinkedIn, Greenhouse, Lever, Ashby, Workable and
> Wellfound*

The rule of thumb: the store listing describes **what the extension does for
the user**. It does not enumerate third-party brands. Naming the sites is not
what makes the extension findable, and it is what gets it rejected.

This applies to every field, not just the description — title, summary,
screenshots, and promotional images all count as metadata. Screenshots that
prominently feature another company's branding are the next most likely thing
to be flagged, so prefer captures of the extension's own popup and dashboard.

---

## Title (45 characters max)

```
Talent Pilot — AI Job Copilot
```

## Summary (132 characters max)

```
Save job postings, score them against your resume, autofill applications, and track where every application stands.
```

## Detailed description

```
Talent Pilot turns a job hunt into one loop: read a posting, see how well it
matches your resume, save it, and keep track of what happens next — without
retyping your own details on every application form.

WHAT IT DOES

• Reads the job posting you are looking at and pulls out the company, the
  role, the description, and — where the page states them — the location and
  the salary.

• Shows a match card on the posting itself, with the keyword score already
  worked out: how many of the terms this job uses are in your resume, and which
  are not. Turn the card off in Settings if you would rather it stayed in the
  popup.

• Scores the posting against your resume in full when you ask, showing which of
  your skills match, which are missing, and an honest summary of the gap.

• Saves the application to your tracker in one click — and when you submit an
  application form, offers to track it so the ones you forget to save are not
  lost.

• Answers the questions every application asks — work authorisation, notice
  period, availability, and the rest — from answers you set up once. Nothing
  is ever filled or submitted without your click.

• Drafts replies to long-form questions using your resume and your own
  previously saved answers, so it sounds like you.

• Answers a question you were asked anywhere. Type it into the popup — or
  select it on any page and right-click "Draft an answer for…" — and get a
  reply written from your resume. Useful when the question arrives in an email
  or a message rather than in a form.

• Revises a draft without starting over: make it shorter, make it longer,
  rephrase it, or sharpen it to the role. Say what to change in your own words
  if the buttons do not cover it, and undo puts the previous version back.

• Shows which applications have gone quiet, so you know which ones are worth
  following up rather than guessing.

HOW IT WORKS

The extension pairs with your own Talent Pilot account. Sign in from the popup
and your resume profiles and saved answers are loaded from your account, so no
personal information is stored inside the extension itself and one install
works for anybody who signs in.

It runs on job posting and application pages. For any other site, you can turn
it on from the popup, which grants access to that one site and nothing else.

Asking a question is the exception, and deliberately so: the popup works on any
tab, and the right-click entry works on any page, because neither reads the page
— you hand over the question yourself.

PRIVACY

Your data stays yours. The extension holds no personal details of its own, it
sends nothing anywhere except to your own account, and it fills a field only
when you click to fill it. Text you select and send with the right-click entry
goes to your account only after you click Draft. Full privacy policy:
https://katchjobs.online/privacy
```

## URLs the listing asks for

Both are on the **Store listing** tab, under Additional fields. Neither is
required to publish, and both are worth filling: an item with no support route
is one a reviewer has to take on trust, and a user with a problem and nowhere
to send it uninstalls instead of writing.

| Field | Value |
| ----- | ----- |
| Homepage URL | `https://katchjobs.online` |
| Support URL | `https://katchjobs.online/support` |
| Privacy policy URL (Privacy practices tab) | `https://katchjobs.online/privacy` |

All three are served by the API from `static/`, so they ship with a **server**
deploy and not with the extension package. A URL entered in the listing before
the server is deployed is a 404 to whoever opens it first, and the reviewer is
usually first.

## Single purpose statement

```
Talent Pilot helps a job seeker track their job applications: it reads the job
posting the user is viewing, saves it to the user's own application tracker,
and helps the user complete the application form using answers they have saved
to their own account.
```

## Permission justifications

| Permission | Justification |
| ---------- | ------------- |
| `activeTab` | Reads the job posting on the page the user is viewing, so the extension can show the match score and save the job to the user's tracker. |
| `storage` | Stores the user's sign-in token, their chosen resume profile, whether the match card is switched on, and a short-lived prompt to save an application they just submitted. |
| `scripting` | Runs the page reader on sites the user has explicitly enabled from the popup. |
| `contextMenus` | Adds one right-click entry, shown only when text is selected, that carries the selected question into the extension's own popup so the user can ask for a draft answer to it. Nothing is read from the page and nothing is sent anywhere until the user clicks Draft in the popup. |
| Host permissions | Communicates with the user's own Talent Pilot account server to load their resume profiles and saved answers, and to save applications. |
| `optional_host_permissions` | Requested one site at a time, only when the user clicks "Enable Copilot on this site", so the extension never has access to sites the user has not chosen. |

## When "Submit for review" is greyed out

The button is disabled by a blank **required** field, never by the package —
and the dashboard does not say which one. Adding a permission is the usual
cause: a new permission creates a new empty justification box on the **Privacy
practices** tab, so an item that submitted fine last release stops submitting
this one. 2.6.0 added `contextMenus` and did exactly that.

Everything below gates the button. Work down the list; every one of them has to
be non-empty, and the last three have to be ticked.

**Privacy practices tab**

| Field | What to put |
| ----- | ----------- |
| Single purpose | The statement [above](#single-purpose-statement) |
| `activeTab` | The row in the table above |
| `storage` | The row in the table above |
| `scripting` | The row in the table above |
| `contextMenus` | The row in the table above — **new in 2.6.0** |
| Host permissions | The row in the table above |
| Remote code | **No.** Everything the extension runs is in the package. It calls an API and renders the response; it never loads or evaluates code fetched at runtime. Answering yes here starts a far longer review for something that is not true. |
| Privacy policy URL | `https://katchjobs.online/privacy` |

**The three certifications at the bottom of that tab.** All three must be
ticked, and all three are true of this extension:

- I do not sell or transfer user data to third parties outside of the approved
  use cases
- I do not use or transfer user data for purposes unrelated to my item's single
  purpose
- I do not use or transfer user data to determine creditworthiness or for
  lending purposes

**Store listing tab** — a category, a language, and at least one screenshot
(1280x800 or 640x400). A missing screenshot greys the button out with no
message about screenshots.

**Account tab** — the contact email has to be verified, and the publisher
account needs two-factor authentication before it can publish anything.

---

## Data use disclosures

Fill these in on the dashboard's **Privacy practices** tab. They must match what
the extension actually does — a justification that describes an older version is
the kind of mismatch that gets an approved item taken down later, which is worse
than a rejection now.

**"Website content" — yes, this item collects it.** Since 2.3.0 the extension
reads the job description of a supported job posting **automatically on page
load**, while signed in, and sends it to the user's own Talent Pilot server to be
scored against their resume. Earlier versions only did this on a click, and the
old wording said so.

What keeps that defensible, and what to say:

- It is sent to the **user's own account server**, never to a third party.
- It is **not stored** unless the user goes on to save that job.
- Nothing is read while signed out, and the whole feature has an off switch in
  the extension's Settings panel.
- The automatic scan is plain text matching on that server — no AI, nothing sent
  to Google. A description only reaches Gemini when the user clicks for the full
  analysis.

Do **not** claim the extension only reads a page when the popup is opened. It no
longer does.

**The right-click entry does not widen any of this.** 2.6.0 adds "Draft an
answer for …" to the context menu. Chrome hands the extension the text the user
selected, and only when they choose that entry — it is not a page read, it does
not require access to the site, and it works on pages the extension has no
permission for. The selection is put into the popup's question box and nothing
leaves the browser until the user clicks Draft, at which point it goes to their
own account server exactly as a question typed into the popup does.

## Before resubmitting

- [ ] Description contains no list of third-party site or company names
- [ ] Screenshots show the extension's own UI, not another company's branding
- [ ] Privacy policy URL resolves: <https://katchjobs.online/privacy>
- [ ] `manifest.json` description matches the tone here (it is already compliant)
- [ ] The privacy policy describes the **current** version. 2.3.0 made the page
      read automatic; the policy said "when you click Save" until it was updated
      to match. Re-read it against the diff on every release that changes when
      data leaves the page — the store checks this, and so should you.
- [ ] Data use disclosures match the permission justifications above
- [ ] Every permission in `manifest.json` has a row in the table above. 2.6.0
      added `contextMenus`; a permission present in the package and absent from
      the justifications is the mismatch the store looks for. Diff the two on
      every release rather than trusting that nothing changed.
- [ ] The deployed policy is the updated one — it is served by the API from
      `static/privacy.html`, so it ships with a **server** deploy, not with the
      extension package. Confirm at the URL before submitting.
