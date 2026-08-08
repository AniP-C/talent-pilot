// Drives the REAL extension/content.js against form markup shaped like the
// application forms these questions actually appear in.
//
// The rules come from the API, not a bundled file, so this stubs the message
// channel and asserts the in-page half: that a saved answer is found, that a
// compliance question buried in legal text still matches, that "Use" fills the
// right control, and that nothing is ever filled without a click.
//
// Run directly:         node tests/test_extension_autofill.js
// Or via the suite:     pytest tests/test_extension_autofill.py

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const code = fs.readFileSync(
  path.join(__dirname, "..", "extension", "content.js"), "utf8"
);

// What GET_AUTOFILL returns for a user who has completed the questionnaire.
const RULES = [
  { key: "first_name", patterns: ["first\\s*name", "given\\s*name"], literal: false,
    answer: "Priya", question: "First name", kind: "text" },
  { key: "full_name", patterns: ["full\\s*name", "legal\\s*name", "\\bname\\b"], literal: false,
    answer: "Priya Sharma", question: "Full name", kind: "text" },
  { key: "work_authorized", patterns: ["legally\\s*authoriz", "authoriz(ed|ation)\\s*to\\s*work"],
    literal: false, answer: "Yes", question: "Authorized to work?", kind: "yes_no" },
  { key: "needs_sponsorship", patterns: ["require\\s*(visa\\s*)?sponsor", "\\bsponsorship\\b"],
    literal: false, answer: "No", question: "Need sponsorship?", kind: "yes_no" },
  { key: "criminal_record", patterns: ["convicted", "criminal\\s*(record|history)"],
    literal: false, answer: "No", question: "Convicted of a crime?", kind: "yes_no" },
  { key: "notice_period", patterns: ["notice\\s*period"], literal: false,
    answer: "30 days", question: "Notice period", kind: "text" },
  { key: "gender", patterns: ["\\bgender\\b"], literal: false,
    answer: "I prefer not to say", question: "Gender", kind: "choice" },
  { key: "custom:licence", patterns: ["Do you hold a valid driving licence? (UK)"],
    literal: true, answer: "Yes", question: "Driving licence", kind: "text" },
];

function build(html, { rules = RULES } = {}) {
  const dom = new JSDOM(
    `<!doctype html><html><head><title>AI Engineer - Nexus Labs</title></head>` +
    `<body>${html}</body></html>`,
    { url: "https://nexuslabs.workable.com/j/ABC", runScripts: "outside-only" }
  );

  const w = dom.window;
  const sent = [];

  w.chrome = {
    runtime: {
      onMessage: { addListener: () => {} },
      sendMessage: async (msg) => {
        sent.push(msg);
        if (msg.type === "GET_AUTOFILL") return { ok: true, data: { rules } };
        if (msg.type === "GENERATE_ANSWER") {
          return { ok: true, data: { suggested_answer: "A drafted answer." } };
        }
        return { ok: true, data: {} };
      },
    },
    storage: { local: { get: async () => ({}), set: async () => {} } },
  };

  Object.defineProperty(w.HTMLElement.prototype, "innerText", {
    get() { return this.textContent; },
    set(v) { this.textContent = v; },
    configurable: true,
  });

  w.eval(code);
  return { w, dom, sent };
}

// scheduleScan() debounces by 400ms, so anything shorter observes an empty page.
const settle = () => new Promise((r) => setTimeout(r, 600));

const results = [];
function check(name, condition, detail = "") {
  results.push({ name, ok: !!condition, detail });
  console.log(`${condition ? "PASS" : "FAIL"}  ${name}${condition ? "" : "\n        " + detail}`);
}

function suggestions(w) {
  return Array.from(w.document.querySelectorAll(".ai-copilot-suggestion"));
}

(async () => {
  // ---- a plain labelled field gets the saved answer -------------------
  {
    const { w } = build(`<div><label for="n">First Name</label><input id="n"></div>`);
    await settle();
    const boxes = suggestions(w);
    check("a labelled field shows the saved answer",
      boxes.length === 1 && boxes[0].textContent.includes("Priya"),
      `got ${boxes.length} box(es): ${boxes.map(b => b.textContent).join(" | ")}`);
  }

  // ---- specificity: "First Name" must not fill with the full name -----
  {
    const { w } = build(`<div><label for="n">First Name</label><input id="n"></div>`);
    await settle();
    const text = suggestions(w)[0]?.textContent || "";
    check("first name beats full name",
      text.includes("Priya") && !text.includes("Priya Sharma"), `got: ${text}`);
  }

  // ---- authorisation and sponsorship stay distinct --------------------
  {
    const { w } = build(`
      <div><label for="a">Are you legally authorized to work in the US?</label><input id="a"></div>
      <div><label for="b">Will you now or in the future require visa sponsorship?</label><input id="b"></div>
    `);
    await settle();
    const texts = suggestions(w).map(b => b.textContent);
    check("authorisation and sponsorship get opposite answers",
      texts.length === 2 && texts[0].includes("Yes") && texts[1].includes("No"),
      `got: ${JSON.stringify(texts)}`);
  }

  // ---- a compliance question buried in legal text ---------------------
  {
    const { w } = build(`
      <div><label for="c">For purposes of this application and in accordance with
      applicable federal and state law, please indicate whether you have ever been
      convicted of a felony or misdemeanour. A conviction will not necessarily
      disqualify you from employment.</label><input id="c"></div>
    `);
    await settle();
    check("a paragraph-long compliance question still matches",
      suggestions(w).some(b => b.textContent.includes("No")),
      `got ${suggestions(w).length} suggestion(s)`);
  }

  // ---- a user's own question matches literally ------------------------
  {
    const { w } = build(
      `<div><label for="d">Do you hold a valid driving licence? (UK)</label><input id="d"></div>`
    );
    await settle();
    check("a custom question with regex characters matches literally",
      suggestions(w).some(b => b.textContent.includes("Yes")));
  }

  // ---- nothing is filled without a click ------------------------------
  {
    const { w } = build(`<div><label for="n">First Name</label><input id="n"></div>`);
    await settle();
    check("the field is NOT filled automatically",
      w.document.querySelector("#n").value === "",
      `value was ${JSON.stringify(w.document.querySelector("#n").value)}`);
  }

  // ---- clicking Use fills a text input --------------------------------
  {
    const { w } = build(`<div><label for="n">First Name</label><input id="n"></div>`);
    await settle();
    suggestions(w)[0].querySelector("button").click();
    check("clicking Use fills the text input",
      w.document.querySelector("#n").value === "Priya",
      `value was ${JSON.stringify(w.document.querySelector("#n").value)}`);
  }

  // ---- clicking Use picks the matching <select> option -----------------
  {
    const { w } = build(`
      <div><label for="g">Gender</label>
        <select id="g">
          <option value="">Select…</option>
          <option value="m">Male</option>
          <option value="x">I prefer not to say</option>
        </select>
      </div>`);
    await settle();
    suggestions(w)[0].querySelector("button").click();
    check("clicking Use selects the matching option",
      w.document.querySelector("#g").value === "x",
      `value was ${JSON.stringify(w.document.querySelector("#g").value)}`);
  }

  // ---- clicking Use checks the matching radio -------------------------
  {
    const { w } = build(`
      <fieldset>
        <legend>Are you legally authorized to work in the US?</legend>
        <input type="radio" name="auth" id="y" value="Yes"><label for="y">Yes</label>
        <input type="radio" name="auth" id="n2" value="No"><label for="n2">No</label>
      </fieldset>`);
    await settle();
    const box = suggestions(w)[0];
    check("a legend is scanned as a question", !!box, "no suggestion rendered");
    if (box) {
      box.querySelector("button").click();
      check("clicking Use checks the matching radio",
        w.document.querySelector("#y").checked === true,
        `Yes checked=${w.document.querySelector("#y").checked}`);
    }
  }

  // ---- an unknown question produces nothing ---------------------------
  {
    const { w } = build(
      `<div><label for="q">What is your favourite colour?</label><input id="q"></div>`
    );
    await settle();
    check("an unknown question shows no suggestion", suggestions(w).length === 0,
      `got ${suggestions(w).length}`);
  }

  // ---- no rules (signed out) must not break the page -------------------
  {
    const { w } = build(`<div><label for="n">First Name</label><input id="n"></div>`,
      { rules: [] });
    await settle();
    check("signed out renders no suggestions and does not throw",
      suggestions(w).length === 0);
  }

  // ---- a saved answer short-circuits the AI call ------------------------
  {
    const { w, sent } = build(`
      <div><label for="t">What is your notice period?</label><textarea id="t"></textarea></div>`);
    await settle();
    const generate = Array.from(w.document.querySelectorAll("button"))
      .find(b => b.textContent.includes("Generate AI Answer"));
    generate.click();
    await settle();
    check("a saved answer is used instead of calling the model",
      w.document.querySelector("#t").value === "30 days" &&
        !sent.some(m => m.type === "GENERATE_ANSWER"),
      `value=${JSON.stringify(w.document.querySelector("#t").value)} ` +
      `calls=${JSON.stringify(sent.map(m => m.type))}`);
  }

  // ---- a genuinely new question is drafted AND saved --------------------
  {
    const { w, sent } = build(`
      <div><label for="t">Describe a time you led a project</label><textarea id="t"></textarea></div>`);
    await settle();
    Array.from(w.document.querySelectorAll("button"))
      .find(b => b.textContent.includes("Generate AI Answer")).click();
    await settle();
    check("a new question is drafted with AI",
      w.document.querySelector("#t").value === "A drafted answer.",
      `value=${JSON.stringify(w.document.querySelector("#t").value)}`);
    check("the drafted answer is saved back to the bank",
      sent.some(m => m.type === "SAVE_CUSTOM_ANSWER"),
      `calls=${JSON.stringify(sent.map(m => m.type))}`);
  }

  const failed = results.filter(r => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
  if (failed.length) process.exitCode = 1;
})();
