// Drives the REAL extension/content.js and asserts the in-page match card.
//
// The card is the one part of the extension that draws into somebody else's
// page unprompted, so the assertions here are as much about restraint as about
// rendering: it appears only on a real posting, only when signed in, only when
// the user has not turned it off, exactly once per page, and never in the
// page's own DOM where a page script could read the score back.
//
// Run directly:         node tests/test_extension_card.js
// Or via the suite:     pytest tests/test_extension_card.py

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const code = fs.readFileSync(
  path.join(__dirname, "..", "extension", "content.js"), "utf8"
);

const CARD_TAG = "TALENT-PILOT-MATCH";

// Long enough to clear the card's MIN_JD_CHARS floor, and shaped like a real
// posting so the extractor has something to find.
const DESCRIPTION = `
  We are looking for an AI Engineer to build retrieval-augmented generation
  systems on top of large language models. You will design and ship Python
  services, own the vector search layer, and work with the product team on
  prompt engineering. Requirements: strong Python, experience with LangChain or
  a comparable framework, a vector database such as Pinecone or pgvector, and
  production experience on AWS. Preferred: Kubernetes, observability tooling,
  and prior work on LLM evaluation. You will join a small team and own the
  service end to end, from the API contract through to the dashboards that
  prove it works.
`.trim();

const KEYWORD_SCAN = {
  score: 40,
  scored: true,
  matched: ["Python", "LLM"],
  missing: ["RAG", "LangChain", "Pinecone"],
  total: 5,
};

const ANALYSIS = {
  match_percentage: 62,
  matched_skills: ["Python", "LLM"],
  missing_skills: ["Kubernetes", "LLM evaluation"],
  summary: "Strong on the Python and LLM side, thin on platform work.",
  coverage: { required_total: 4, required_met: 2.5, preferred_total: 3, preferred_met: 1 },
  keyword_coverage: KEYWORD_SCAN,
};

function build(html, { signedOut = false, storage = {}, scan = KEYWORD_SCAN } = {}) {
  const dom = new JSDOM(
    `<!doctype html><html><head><title>AI Engineer - Nexus Labs</title></head>` +
    `<body>${html}</body></html>`,
    { url: "https://nexuslabs.workable.com/j/ABC", runScripts: "outside-only" }
  );

  const w = dom.window;
  const sent = [];
  const state = { signedOut, scan };

  // The card renders into a CLOSED shadow root, so host.shadowRoot is null by
  // design and the test cannot reach it the way the page cannot. Wrapping
  // attachShadow keeps a reference for the assertions without weakening what
  // is being asserted — mode is checked below and stays "closed".
  const shadows = [];
  const attach = w.Element.prototype.attachShadow;
  w.Element.prototype.attachShadow = function (init) {
    const root = attach.call(this, init);
    shadows.push({ host: this, root, mode: init && init.mode });
    return root;
  };

  w.chrome = {
    runtime: {
      onMessage: { addListener: () => {} },
      sendMessage: async (msg) => {
        sent.push(msg);
        if (msg.type === "GET_AUTOFILL") {
          return state.signedOut
            ? { ok: false, error: "Please sign in.", needsAuth: true }
            : { ok: true, data: { rules: [] } };
        }
        if (msg.type === "KEYWORD_SCAN") {
          return state.scan
            ? { ok: true, data: state.scan }
            : { ok: false, error: "Scan unavailable." };
        }
        if (msg.type === "ANALYZE_JOB") return { ok: true, data: ANALYSIS };
        if (msg.type === "CHECK_JOB") return { ok: true, data: { exists: false } };
        return { ok: true, data: {} };
      },
    },
    storage: {
      local: {
        get: async (key) => {
          const names = typeof key === "string" ? [key] : Object.keys(key || storage);
          const out = {};
          names.forEach((name) => {
            if (name in storage) out[name] = storage[name];
          });
          return out;
        },
        set: async (values) => Object.assign(storage, values),
        remove: async (key) => {
          [].concat(key).forEach((name) => delete storage[name]);
        },
      },
      // Deliberately no onChanged: content.js optional-calls it, and a
      // harness should not have to stub an event to avoid a crash.
    },
  };

  Object.defineProperty(w.HTMLElement.prototype, "innerText", {
    get() { return this.textContent; },
    set(v) { this.textContent = v; },
    configurable: true,
  });

  const reinject = () => w.eval(code);

  w.eval(code);
  return { w, sent, state, shadows, reinject, storage };
}

function host(w) {
  return w.document.querySelector("talent-pilot-match");
}

function shadowFor(w, shadows) {
  const element = host(w);
  if (!element) return null;
  const entry = shadows.find((candidate) => candidate.host === element);
  return entry ? entry.root : null;
}

// The card's own text, not the shadow root's — the root also holds the
// stylesheet, and "border-radius: 50%" contains the substring "0%".
function cardText(w, shadows) {
  const root = shadowFor(w, shadows);
  const card = root && root.querySelector(".tp-card");
  return card ? card.textContent : "";
}

function cardButton(w, shadows, label) {
  const root = shadowFor(w, shadows);
  if (!root) return null;
  return Array.from(root.querySelectorAll("button")).find((button) =>
    button.textContent.includes(label)
  );
}

// scheduleScan() debounces by 400ms and the scan itself is a round trip, so
// anything shorter observes a card mid-flight.
const settle = () => new Promise((r) => setTimeout(r, 800));

const results = [];
function check(name, condition, detail = "") {
  results.push({ name, ok: !!condition, detail });
  console.log(`${condition ? "PASS" : "FAIL"}  ${name}${condition ? "" : "\n        " + detail}`);
}

const POSTING = `
  <h1>AI Engineer</h1>
  <div data-ui="job-description">${DESCRIPTION}</div>
`;

// The same posting, declaring what it pays and where it is — which is the fact
// a reader wants before any score.
const POSTING_WITH_FACTS = `
  <script type="application/ld+json">{
    "@type":"JobPosting","title":"AI Engineer",
    "hiringOrganization":{"name":"Nexus Labs"},
    "baseSalary":{"@type":"MonetaryAmount","currency":"INR",
      "value":{"minValue":1800000,"maxValue":2400000,"unitText":"YEAR"}},
    "jobLocationType":"TELECOMMUTE",
    "jobLocation":{"address":{"addressLocality":"Bengaluru","addressRegion":"Karnataka"}},
    "description":"${DESCRIPTION.replace(/\s+/g, " ")}"
  }</script>
  <h1>AI Engineer</h1>
  <div data-ui="job-description">${DESCRIPTION}</div>
`;

(async () => {
  // ---- a real posting gets a card ---------------------------------------
  {
    const { w, shadows } = build(POSTING);
    await settle();

    check("a job posting grows a match card", !!host(w), "no card host in the page");
    check("the card reports the keyword score",
      cardText(w, shadows).includes("40%"),
      `card said: ${cardText(w, shadows).slice(0, 200)}`);
    check("the card counts the terms found",
      cardText(w, shadows).includes("2 of 5 keywords"),
      `card said: ${cardText(w, shadows).slice(0, 200)}`);
    check("the missing terms are listed",
      ["RAG", "LangChain", "Pinecone"].every(t => cardText(w, shadows).includes(t)),
      `card said: ${cardText(w, shadows).slice(0, 300)}`);
  }

  // ---- the score never enters the page's own DOM -------------------------
  // A content script shares the DOM with the page. Which skills the user lacks
  // and how poorly they match is the user's business, not the employer's, so
  // the card is a closed shadow root and nothing the page can read.
  {
    const { w, shadows } = build(POSTING);
    await settle();

    check("the shadow root is closed",
      shadows.some(s => s.host === host(w) && s.mode === "closed"),
      `modes: ${JSON.stringify(shadows.map(s => s.mode))}`);
    check("the page cannot reach the card through shadowRoot",
      host(w).shadowRoot === null,
      "host.shadowRoot was readable");
    check("the score is not in the page's text",
      !w.document.body.innerText.includes("40%"),
      "the page's own text carried the score");
  }

  // ---- it sits with the description, not floating over the page ----------
  {
    const { w } = build(POSTING);
    await settle();

    const description = w.document.querySelector('[data-ui="job-description"]');
    check("the card is placed just above the job description",
      description.previousElementSibling === host(w),
      `previous sibling was ${description.previousElementSibling &&
        description.previousElementSibling.tagName}`);
  }

  // ---- a page that is not a posting gets nothing -------------------------
  {
    const { w } = build(`<h1>AI Engineer</h1><p>Apply below.</p>`);
    await settle();
    check("a stub page grows no card", !host(w));
  }

  {
    const { w } = build(`<div data-ui="job-description">Short description.</div>`);
    await settle();
    check("a too-short description grows no card", !host(w));
  }

  // ---- signed out, there is nothing to score against --------------------
  {
    const { w, sent } = build(POSTING, { signedOut: true });
    await settle();
    check("signed out, no card is drawn", !host(w));
    check("signed out, no scan is requested",
      !sent.some(m => m.type === "KEYWORD_SCAN"),
      `calls=${JSON.stringify(sent.map(m => m.type))}`);
  }

  // ---- the setting is honoured ------------------------------------------
  {
    const { w, sent } = build(POSTING, { storage: { inPageCard: false } });
    await settle();
    check("with the card turned off, none is drawn", !host(w));
    check("with the card turned off, no scan is requested",
      !sent.some(m => m.type === "KEYWORD_SCAN"),
      `calls=${JSON.stringify(sent.map(m => m.type))}`);
  }

  // ---- one card, however many times the script is injected --------------
  // The manifest registration and the dynamic one for granted sites overlap,
  // and a job board re-renders its own DOM constantly.
  {
    const { w, reinject } = build(POSTING);
    await settle();
    reinject();
    await settle();
    check("injecting twice yields one card",
      w.document.querySelectorAll("talent-pilot-match").length === 1,
      `got ${w.document.querySelectorAll("talent-pilot-match").length}`);
  }

  // ---- the same posting is scanned once, not once per mutation -----------
  {
    const { w, sent } = build(POSTING);
    await settle();

    // What a job board does continuously: mutate around the posting.
    for (let i = 0; i < 5; i++) {
      w.document.body.appendChild(w.document.createElement("span"));
    }
    await settle();

    const scans = sent.filter(m => m.type === "KEYWORD_SCAN").length;
    check("re-rendering the page does not re-scan the same posting",
      scans === 1, `${scans} scan(s) requested`);
  }

  // ---- closing it takes it off the page and keeps it off -----------------
  {
    const { w, shadows } = build(POSTING);
    await settle();

    cardButton(w, shadows, "✕").click();
    check("closing the card removes it", !host(w));

    w.document.body.appendChild(w.document.createElement("span"));
    await settle();
    check("a closed card does not come back on the next render", !host(w));
  }

  // ---- collapsing keeps the number and drops the detail ------------------
  {
    const { w, shadows } = build(POSTING);
    await settle();

    cardButton(w, shadows, "▴").click();
    const collapsed = cardText(w, shadows);

    check("collapsed, the score is still shown", collapsed.includes("40%"), collapsed);
    check("collapsed, the term lists are gone",
      !collapsed.includes("LangChain"), collapsed);
  }

  // ---- the AI analysis is a click, never automatic -----------------------
  // The keyword score is free. The requirement-by-requirement analysis is a
  // paid model call, so it must never happen just because a page was opened.
  {
    const { w, shadows, sent } = build(POSTING);
    await settle();

    check("opening a posting never runs the paid analysis",
      !sent.some(m => m.type === "ANALYZE_JOB"),
      `calls=${JSON.stringify(sent.map(m => m.type))}`);

    cardButton(w, shadows, "Full AI match").click();
    await settle();

    check("clicking runs the analysis",
      sent.some(m => m.type === "ANALYZE_JOB"),
      `calls=${JSON.stringify(sent.map(m => m.type))}`);

    const text = cardText(w, shadows);
    check("the card switches to the recruiter fit", text.includes("62%"), text.slice(0, 200));
    check("the verdict is shown", text.includes("Close fit"), text.slice(0, 200));
    check("the gaps are listed", text.includes("Kubernetes"), text.slice(0, 400));
    check("the keyword view survives the AI run",
      text.includes("LangChain"), text.slice(0, 400));
  }

  // ---- the analysis call carries the page's job -------------------------
  {
    const { w, shadows, sent } = build(POSTING);
    await settle();
    cardButton(w, shadows, "Full AI match").click();
    await settle();

    const call = sent.find(m => m.type === "ANALYZE_JOB");
    check("the analysis names the company and role",
      call && call.job.company === "Nexus Labs" && call.job.role === "AI Engineer",
      `job=${JSON.stringify(call && call.job)}`);
  }

  // ---- the posting's own facts, on the card -----------------------------
  {
    const { w, shadows } = build(POSTING_WITH_FACTS);
    await settle();

    const text = cardText(w, shadows);
    check("the card names where the job is",
      text.includes("Remote") && text.includes("Bengaluru, Karnataka"),
      text.slice(0, 240));
    check("the card names what it pays",
      text.includes("INR 1,800,000–2,400,000/yr"), text.slice(0, 240));
  }

  {
    const { w, shadows } = build(POSTING);
    await settle();
    // A posting that states neither says so out loud. A missing line cannot be
    // told apart from a broken one, and "this posting does not state a salary"
    // is itself worth knowing before applying.
    const text = cardText(w, shadows);
    check("a posting declaring neither says NA rather than going quiet",
      text.includes("📍 NA") && text.includes("💰 NA"), text.slice(0, 240));
  }

  // ---- and they reach the tracker ---------------------------------------
  {
    const { w, shadows, sent } = build(POSTING_WITH_FACTS);
    await settle();
    cardButton(w, shadows, "Save").click();
    await settle();

    const call = sent.find(m => m.type === "SAVE_JOB");
    check("saving from the card carries the salary and location",
      call && call.job.salary_min === 1800000 && call.job.salary_max === 2400000 &&
        call.job.salary_currency === "INR" && call.job.salary_period === "YEAR" &&
        call.job.location === "Bengaluru, Karnataka" && call.job.remote === true,
      `job=${JSON.stringify(call && call.job)}`);
  }

  // ---- saving from the card ---------------------------------------------
  {
    const { w, shadows, sent } = build(POSTING);
    await settle();

    check("nothing is saved by opening the page",
      !sent.some(m => m.type === "SAVE_JOB"),
      `calls=${JSON.stringify(sent.map(m => m.type))}`);

    cardButton(w, shadows, "Save").click();
    await settle();

    check("clicking Save sends the job to the tracker",
      sent.some(m => m.type === "SAVE_JOB"),
      `calls=${JSON.stringify(sent.map(m => m.type))}`);
    check("Save checks for a duplicate first",
      sent.some(m => m.type === "CHECK_JOB"),
      `calls=${JSON.stringify(sent.map(m => m.type))}`);
  }

  // ---- a failed scan says so rather than showing a made-up number --------
  {
    const { w, shadows } = build(POSTING, { scan: null });
    await settle();

    const text = cardText(w, shadows);
    check("a failed scan reports the error", text.includes("Scan unavailable"), text);
    // A dash, not a zero: "0%" would read as a terrible match rather than as a
    // measurement that did not happen.
    check("and shows a dash rather than a number", text.includes("–") && !/\d%/.test(text),
      text);
  }

  const failed = results.filter(r => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
  if (failed.length) process.exitCode = 1;
})();
