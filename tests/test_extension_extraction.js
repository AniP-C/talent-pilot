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

  // --- a board is never the employer --------------------------------------
  // Both of these are real rows from a live tracker.
  {
    name: "the Indeed HOME page is not a job at a company called Indeed",
    url: "https://in.indeed.com/?r=us&vjk=c662fa823fc214ad",
    title: "Job Search | Indeed",
    // What the home page actually offers: a site name and a greeting.
    html: `<meta property="og:site_name" content="Indeed">
           <h1>Welcome, Aniruddh</h1>
           <main><p>${"Find jobs near you. ".repeat(5)}</p></main>`,
    // Empty, so the popup asks rather than inventing an employer.
    expect: { company: "" },
  },
  {
    name: "a real Indeed posting still extracts",
    url: "https://in.indeed.com/viewjob?jk=abc123",
    title: "AI Engineer - Nexus Labs - Job in Bengaluru - Indeed.com",
    html: `<meta property="og:site_name" content="Indeed">
           <h1 data-testid="jobsearch-JobInfoHeader-title">AI Engineer</h1>
           <div data-testid="inlineHeader-companyName">Nexus Labs</div>
           <div data-testid="inlineHeader-companyLocation">Bengaluru, Karnataka</div>
           <div id="jobDescriptionText"><p>${"Build models. ".repeat(8)}</p></div>`,
    expect: {
      company: "Nexus Labs", role: "AI Engineer", location: "Bengaluru, Karnataka",
    },
  },
  {
    name: "LinkedIn naming itself is rejected too",
    url: "https://www.linkedin.com/feed/",
    title: "Feed | LinkedIn",
    html: `<meta property="og:site_name" content="LinkedIn"><h1>Welcome back</h1>`,
    expect: { company: "" },
  },

  // --- a company name is a name, not a sentence ---------------------------
  {
    name: "wellfound listing copy is not a company (THE BUG)",
    url: "https://wellfound.com/jobs?job_listing_slug=4555230-backend-engineer",
    title: "Backend Engineer at Talkdoc",
    // The real markup: a loose [class*="company"] container holding the whole
    // promo blurb, appearing ABOVE the precise company link.
    html: `<div class="company-card">
             <h1>Talkdoc Actively Hiring PROMOTED Affordable and Accessible Mental
                 Healthcare from People Who Really Understand Mental Health
                 1-10 Employees</h1>
           </div>
           <a href="/company/talkdoc">Talkdoc</a>
           <h2 class="jobTitle">Backend Engineer</h2>
           <main><p>${"Role detail. ".repeat(8)}</p></main>`,
    // The precise selector wins now, and the blurb would be rejected anyway.
    expect: { company: "Talkdoc", role: "Backend Engineer" },
  },
  {
    name: "twenty words of marketing copy is rejected outright",
    url: "https://careers.example.com/x",
    title: "AI Engineer",
    html: `<meta property="og:site_name" content="Talkdoc Actively Hiring PROMOTED
             Affordable and Accessible Mental Healthcare from People Who Really
             Understand Mental Health 1-10 Employees">
           <h1>AI Engineer</h1><main><p>${"Copy. ".repeat(8)}</p></main>`,
    expect: { company: "" },
  },
  {
    name: "a genuinely long company name still passes",
    url: "https://careers.example.com/x",
    title: "AI Engineer",
    html: `<meta property="og:site_name" content="Saint-Gobain India Private Limited">
           <h1>AI Engineer</h1><main><p>${"Copy. ".repeat(8)}</p></main>`,
    expect: { company: "Saint-Gobain India Private Limited" },
  },

  // --- salary and location, out of the same JSON-LD block -----------------
  // These were being parsed past and discarded. They are worth storing only
  // because they are declared fields: `baseSalary.value.minValue` is a stated
  // number, whereas "competitive package, £70k OTE" in a paragraph is a guess.
  {
    name: "JSON-LD salary range and city",
    url: "https://nexuslabs.workable.com/j/ABC",
    title: "AI Engineer - Nexus Labs",
    html: `<script type="application/ld+json">{
      "@type":"JobPosting","title":"AI Engineer",
      "hiringOrganization":{"name":"Nexus Labs"},
      "baseSalary":{"@type":"MonetaryAmount","currency":"INR",
        "value":{"@type":"QuantitativeValue","minValue":1800000,"maxValue":2400000,
                 "unitText":"YEAR"}},
      "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",
        "addressLocality":"Bengaluru","addressRegion":"Karnataka","addressCountry":"IN"}}
    }</script><h1>AI Engineer</h1>`,
    expect: {
      company: "Nexus Labs", role: "AI Engineer",
      location: "Bengaluru, Karnataka", remote: false,
      salary_min: 1800000, salary_max: 2400000,
      salary_currency: "INR", salary_period: "YEAR",
    },
  },
  {
    name: "JSON-LD single figure (value is a bare number)",
    url: "https://nexuslabs.workable.com/j/ABC",
    title: "AI Engineer - Nexus Labs",
    html: `<script type="application/ld+json">{
      "@type":"JobPosting","title":"AI Engineer",
      "hiringOrganization":{"name":"Nexus Labs"},
      "baseSalary":{"@type":"MonetaryAmount","currency":"USD","value":150000},
      "jobLocation":{"@type":"Place","address":{"addressLocality":"Austin",
        "addressRegion":"TX","addressCountry":"US"}}
    }</script><h1>AI Engineer</h1>`,
    expect: {
      company: "Nexus Labs", salary_min: 150000, salary_max: 150000,
      salary_currency: "USD", location: "Austin, TX",
    },
  },
  {
    name: "JSON-LD remote (TELECOMMUTE) with no place",
    url: "https://nexuslabs.workable.com/j/ABC",
    title: "AI Engineer - Nexus Labs",
    html: `<script type="application/ld+json">{
      "@type":"JobPosting","title":"AI Engineer",
      "hiringOrganization":{"name":"Nexus Labs"},
      "jobLocationType":"TELECOMMUTE",
      "applicantLocationRequirements":{"@type":"Country","name":"India"}
    }</script><h1>AI Engineer</h1>`,
    expect: { company: "Nexus Labs", remote: true, location: "India" },
  },
  {
    name: "JSON-LD several offices",
    url: "https://nexuslabs.workable.com/j/ABC",
    title: "AI Engineer - Nexus Labs",
    html: `<script type="application/ld+json">{
      "@type":"JobPosting","title":"AI Engineer",
      "hiringOrganization":{"name":"Nexus Labs"},
      "jobLocation":[
        {"address":{"addressLocality":"London","addressRegion":"England"}},
        {"address":{"addressLocality":"Berlin"}},
        {"address":{"addressLocality":"Lisbon"}}]
    }</script><h1>AI Engineer</h1>`,
    expect: { company: "Nexus Labs", location: "London, England +2 more" },
  },
  {
    name: "JSON-LD region repeating the city is not printed twice",
    url: "https://nexuslabs.workable.com/j/ABC",
    title: "AI Engineer - Nexus Labs",
    html: `<script type="application/ld+json">{
      "@type":"JobPosting","title":"AI Engineer",
      "hiringOrganization":{"name":"Nexus Labs"},
      "jobLocation":{"address":{"addressLocality":"Singapore",
        "addressRegion":"Singapore","addressCountry":"SG"}}
    }</script><h1>AI Engineer</h1>`,
    expect: { company: "Nexus Labs", location: "Singapore" },
  },
  {
    name: "JSON-LD country only",
    url: "https://nexuslabs.workable.com/j/ABC",
    title: "AI Engineer - Nexus Labs",
    html: `<script type="application/ld+json">{
      "@type":"JobPosting","title":"AI Engineer",
      "hiringOrganization":{"name":"Nexus Labs"},
      "jobLocation":{"address":{"addressCountry":"Ireland"}}
    }</script><h1>AI Engineer</h1>`,
    expect: { company: "Nexus Labs", location: "Ireland" },
  },
  {
    name: "no JSON-LD: location falls back to the board's selector",
    url: "https://nexuslabs.workable.com/j/ABC",
    title: "AI Engineer - Nexus Labs",
    html: `<h1>AI Engineer</h1><div data-ui="job-location">Pune, Maharashtra</div>
           <main><p>${"Copy. ".repeat(8)}</p></main>`,
    expect: { company: "Nexus Labs", location: "Pune, Maharashtra" },
  },
  {
    name: "no JSON-LD: a salary is NOT scraped out of the page",
    url: "https://nexuslabs.workable.com/j/ABC",
    title: "AI Engineer - Nexus Labs",
    html: `<h1>AI Engineer</h1>
           <main><p>${"Copy. ".repeat(8)} Competitive package, £70,000 OTE.</p></main>`,
    // Prose is not a declared field. A wrong salary is a number somebody makes
    // a decision on, so nothing beats a guess here.
    expect: { company: "Nexus Labs", salary_min: null, salary_max: null },
  },
  {
    name: '"Remote" in the location slot becomes the flag, not the place',
    url: "https://nexuslabs.workable.com/j/ABC",
    title: "AI Engineer - Nexus Labs",
    html: `<h1>AI Engineer</h1><div data-ui="job-location">Remote</div>
           <main><p>${"Copy. ".repeat(8)}</p></main>`,
    expect: { company: "Nexus Labs", remote: true },
  },
  {
    name: "a posting declaring neither reports neither",
    url: "https://nexuslabs.workable.com/j/ABC",
    title: "AI Engineer - Nexus Labs",
    html: `<h1>AI Engineer</h1><main><p>${"Copy. ".repeat(8)}</p></main>`,
    expect: {
      company: "Nexus Labs", location: "", remote: false,
      salary_min: null, salary_currency: "", salary_period: "",
    },
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

  // Every key the case declares is checked, so a salary case does not have to
  // restate the company assertions and a company case does not have to know
  // that salary fields exist.
  const wrong = Object.keys(c.expect).filter((key) => got[key] !== c.expect[key]);
  const ok = wrong.length === 0;

  if (ok) pass++;
  else fails.push({ name: c.name, got, expect: c.expect });

  const summary = Object.keys(c.expect)
    .map((key) => `${key}=${JSON.stringify(got[key])}`)
    .join("  ");

  console.log(
    `${ok ? "PASS" : "FAIL"}  ${c.name}\n        ${summary}` +
    (ok ? "" : `\n        WRONG: ${wrong.map(
      (key) => `${key} expected ${JSON.stringify(c.expect[key])}`
    ).join(", ")}`)
  );
}

console.log(`\n${pass}/${CASES.length} passed`);
if (fails.length) process.exitCode = 1;
