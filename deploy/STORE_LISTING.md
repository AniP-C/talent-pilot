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
  role, and the description.

• Scores the posting against your resume, showing which of your skills match,
  which are missing, and an honest summary of the gap.

• Saves the application to your tracker in one click — and when you submit an
  application form, offers to track it so the ones you forget to save are not
  lost.

• Answers the questions every application asks — work authorisation, notice
  period, availability, and the rest — from answers you set up once. Nothing
  is ever filled or submitted without your click.

• Drafts replies to long-form questions using your resume and your own
  previously saved answers, so it sounds like you.

• Shows which applications have gone quiet, so you know which ones are worth
  following up rather than guessing.

HOW IT WORKS

The extension pairs with your own Talent Pilot account. Sign in from the popup
and your resume profiles and saved answers are loaded from your account, so no
personal information is stored inside the extension itself and one install
works for anybody who signs in.

It runs on job posting and application pages. For any other site, you can turn
it on from the popup, which grants access to that one site and nothing else.

PRIVACY

Your data stays yours. The extension holds no personal details of its own, it
sends nothing anywhere except to your own account, and it fills a field only
when you click to fill it. Full privacy policy:
https://katchjobs.online/privacy
```

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
| `activeTab` | Reads the job posting on the page the user is viewing, only when the user opens the extension popup. |
| `storage` | Stores the user's sign-in token, their chosen resume profile, and a short-lived prompt to save an application they just submitted. |
| `scripting` | Runs the page reader on sites the user has explicitly enabled from the popup. |
| Host permissions | Communicates with the user's own Talent Pilot account server to load their resume profiles and saved answers, and to save applications. |
| `optional_host_permissions` | Requested one site at a time, only when the user clicks "Enable Copilot on this site", so the extension never has access to sites the user has not chosen. |

## Before resubmitting

- [ ] Description contains no list of third-party site or company names
- [ ] Screenshots show the extension's own UI, not another company's branding
- [ ] Privacy policy URL resolves: <https://katchjobs.online/privacy>
- [ ] `manifest.json` description matches the tone here (it is already compliant)
