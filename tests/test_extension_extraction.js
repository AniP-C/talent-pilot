// Loads the REAL extension/content.js into jsdom and asks it to extract a job
// from markup shaped like each board's actual output.
//
// Guards the bug that put a job title in the company field: Workable and Ashby
// title their pages "<Role> - <Company>", so the old `.split(" - ")[0]`
// returned the role. A wrong company means later recruiter emails cannot find
// the application and silently open a duplicate.
//
// Run directly:            node tests/test_extension_extraction.js
// Or through the suite:    pytest tests/test_extension_extraction.py

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const SRC = path.join(__dirname, "..", "extension", "content.js");
const code = fs.readFileSync(SRC, "utf8");

const CASES = [
  {
    name: "workable (role-first title, THE BUG)",
    url: "https://nexuslabs.workable.com/j/ABC123",
    title: "AI Engineer - Nexus Labs",
    html: `<h1>AI Engineer</h1><main><p>${"We are looking for an AI engineer. ".repeat(4)}</p></main>`,
    expect: { company: "Nexus Labs", role: "AI Engineer" },
  },
  {
    name: "workable + JSON-LD",
    url: "https://nexuslabs.workable.com/j/ABC123",
    title: "AI Engineer - Nexus Labs",
    html: `<script type="application/ld+json">
      {"@type":"JobPosting","title":"AI Engineer",
       "hiringOrganization":{"@type":"Organization","name":"Nexus Labs"},
       "description":"<p>Build models at scale.</p>"}</script><h1>AI Engineer</h1>`,
    expect: { company: "Nexus Labs", role: "AI Engineer" },
  },
  {
    name: "greenhouse (Job Application for X at Y)",
    url: "https://boards.greenhouse.io/nexuslabs/jobs/1",
    title: "Job Application for AI Engineer at Nexus Labs",
    html: `<h1 class="app-title">AI Engineer</h1><div id="content"><p>${"Details here. ".repeat(5)}</p></div>`,
    expect: { company: "Nexus Labs", role: "AI Engineer" },
  },
  {
    name: "lever (company-first title)",
    url: "https://jobs.lever.co/nexuslabs/xyz",
    title: "Nexus Labs - AI Engineer",
    html: `<div class="posting-headline"><h2>AI Engineer</h2></div><div class="posting-details"><p>${"Role detail. ".repeat(5)}</p></div>`,
    expect: { company: "Nexus Labs", role: "AI Engineer" },
  },
  {
    name: "ashby (Role @ Company)",
    url: "https://jobs.ashbyhq.com/nexuslabs/abc",
    title: "AI Engineer @ Nexus Labs",
    html: `<h1>AI Engineer</h1><main><p>${"Ashby description. ".repeat(5)}</p></main>`,
    expect: { company: "Nexus Labs", role: "AI Engineer" },
  },
  {
    name: "ashby alt (Role - Company)",
    url: "https://jobs.ashbyhq.com/nexuslabs/abc",
    title: "AI Engineer - Nexus Labs",
    html: `<h1>AI Engineer</h1><main><p>${"Ashby description. ".repeat(5)}</p></main>`,
    expect: { company: "Nexus Labs", role: "AI Engineer" },
  },
  {
    name: "generic career page (Role | Company)",
    url: "https://careers.nexuslabs.com/openings/42",
    title: "AI Engineer | Nexus Labs",
    html: `<h1>AI Engineer</h1><main><p>${"Generic page copy. ".repeat(5)}</p></main>`,
    expect: { company: "Nexus Labs", role: "AI Engineer" },
  },
  {
    name: "generic with Careers tail",
    url: "https://nexuslabs.com/jobs/42",
    title: "AI Engineer - Nexus Labs Careers",
    html: `<h1>AI Engineer</h1><main><p>${"Generic page copy. ".repeat(5)}</p></main>`,
    expect: { company: "Nexus Labs", role: "AI Engineer" },
  },
  {
    name: "og:site_name rescue",
    url: "https://apply.someats.io/x",
    title: "AI Engineer",
    html: `<meta property="og:site_name" content="Nexus Labs"><h1>AI Engineer</h1>`,
    expect: { company: "Nexus Labs", role: "AI Engineer" },
  },
  {
    name: "linkedin (Role | Company | LinkedIn)",
    url: "https://www.linkedin.com/jobs/view/123",
    title: "AI Engineer | Nexus Labs | LinkedIn",
    html: `<h1 class="job-details-jobs-unified-top-card__job-title">AI Engineer</h1>
           <div class="job-details-jobs-unified-top-card__company-name">Nexus Labs</div>
           <div class="jobs-description__content"><p>${"JD text. ".repeat(5)}</p></div>`,
    expect: { company: "Nexus Labs", role: "AI Engineer" },
  },
  {
    name: "NO company anywhere -> must return empty, not the role",
    url: "https://apply.someats.io/x",
    title: "AI Engineer",
    html: `<h1>AI Engineer</h1><main><p>${"Nothing identifies the employer. ".repeat(4)}</p></main>`,
    expect: { company: "", role: "AI Engineer" },
  },
  {
    name: "company legitimately contains a role word",
    url: "https://careers.example.com/x",
    title: "AI Engineer - Engineering Solutions Pvt Ltd",
    html: `<h1>AI Engineer</h1><main><p>${"Copy. ".repeat(6)}</p></main>`,
    expect: { company: "Engineering Solutions Pvt Ltd", role: "AI Engineer" },
  },
];

let pass = 0;
const fails = [];

for (const c of CASES) {
  const dom = new JSDOM(`<!doctype html><html><head><title>${c.title}</title></head><body>${c.html}</body></html>`, {
    url: c.url, runScripts: "outside-only",
  });

  const w = dom.window;
  // Minimal extension surface the content script touches at load time.
  let handler = null;
  w.chrome = {
    runtime: { onMessage: { addListener: (fn) => { handler = fn; } }, sendMessage: async () => ({}) },
    storage: { local: { get: async () => ({}), set: async () => {} } },
  };
  // jsdom lacks innerText; the script reads it, so map it to textContent.
  Object.defineProperty(w.HTMLElement.prototype, "innerText", {
    get() { return this.textContent; }, configurable: true,
  });

  w.eval(code);

  let got = null;
  handler({ action: "extract_job" }, {}, (r) => { got = r; });

  const ok = got.company === c.expect.company && got.role === c.expect.role;
  if (ok) pass++;
  else fails.push({ name: c.name, got, expect: c.expect });

  console.log(
    `${ok ? "PASS" : "FAIL"}  ${c.name}\n` +
    `        company=${JSON.stringify(got.company)} (via ${got.company_source})  role=${JSON.stringify(got.role)}` +
    (ok ? "" : `\n        EXPECTED company=${JSON.stringify(c.expect.company)} role=${JSON.stringify(c.expect.role)}`)
  );
}

console.log(`\n${pass}/${CASES.length} passed`);
if (fails.length) process.exitCode = 1;
