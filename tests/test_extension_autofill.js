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

function build(html, { rules = RULES, signedOut = false, storage = {} } = {}) {
  const dom = new JSDOM(
    `<!doctype html><html><head><title>AI Engineer - Nexus Labs</title></head>` +
    `<body>${html}</body></html>`,
    { url: "https://nexuslabs.workable.com/j/ABC", runScripts: "outside-only" }
  );

  const w = dom.window;
  const sent = [];
  const listeners = [];

  // Mutable so a test can change what the API would return and then announce
  // it, the way signing in or saving an answer does in the real extension.
  const state = { rules, signedOut };

  w.chrome = {
    runtime: {
      onMessage: { addListener: (fn) => listeners.push(fn) },
      sendMessage: async (msg) => {
        sent.push(msg);
        if (msg.type === "GET_AUTOFILL") {
          return state.signedOut
            ? { ok: false, error: "Please sign in.", needsAuth: true }
            : { ok: true, data: { rules: state.rules } };
        }
        if (msg.type === "GENERATE_ANSWER") {
          return { ok: true, data: { suggested_answer: "A drafted answer." } };
        }
        return { ok: true, data: {} };
      },
    },
    // A real store, not a stub returning {}: the offer to track a submitted
    // application is written here and read back by whichever page loads next,
    // so an amnesiac stub would test nothing.
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
    },
  };

  Object.defineProperty(w.HTMLElement.prototype, "innerText", {
    get() { return this.textContent; },
    set(v) { this.textContent = v; },
    configurable: true,
  });

  // What background.js does when the answer bank changes underneath an open
  // page. Also how the popup asks the page what it extracted.
  const fire = (message) => {
    let reply;
    listeners.forEach((fn) => fn(message, {}, (value) => { reply = value; }));
    return reply;
  };

  // Injecting the same frame twice is normal: the manifest registration and
  // the dynamic one for granted sites overlap, and the popup tops up stale tabs.
  const reinject = () => w.eval(code);

  w.eval(code);
  return { w, dom, sent, state, fire, reinject, storage };
}

// jsdom does not run form submission, so the event is dispatched directly —
// which is also what a form calling preventDefault and posting over XHR does.
function submitForm(w, selector = "form") {
  w.document
    .querySelector(selector)
    .dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
}

function saveBar(w) {
  return w.document.querySelector(".ai-copilot-save-bar");
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
    check("a labelled field offers its saved answer",
      boxes.length === 1 && boxes[0].textContent.includes("First name"),
      `got ${boxes.length} box(es): ${boxes.map(b => b.textContent).join(" | ")}`);
  }

  // ---- specificity: "First Name" must not fill with the full name -----
  {
    const { w } = build(`<div><label for="n">First Name</label><input id="n"></div>`);
    await settle();
    // The box names the catalogue question it matched, so specificity is
    // visible without the value ever entering the DOM.
    const text = suggestions(w)[0]?.textContent || "";
    check("first name beats full name",
      text.includes("First name") && !text.includes("Full name"), `got: ${text}`);
  }

  // ---- authorisation and sponsorship stay distinct --------------------
  {
    const { w } = build(`
      <div><label for="a">Are you legally authorized to work in the US?</label><input id="a"></div>
      <div><label for="b">Will you now or in the future require visa sponsorship?</label><input id="b"></div>
    `);
    await settle();
    const texts = suggestions(w).map(b => b.textContent);
    check("authorisation and sponsorship match different questions",
      texts.length === 2 &&
        texts[0].includes("Authorized to work") &&
        texts[1].includes("Need sponsorship"),
      `got: ${JSON.stringify(texts)}`);
    // Filling both proves they carry opposite values.
    suggestions(w).forEach(b => b.querySelector("button").click());
    check("authorisation and sponsorship fill opposite values",
      w.document.querySelector("#a").value === "Yes" &&
        w.document.querySelector("#b").value === "No",
      `a=${w.document.querySelector("#a").value} b=${w.document.querySelector("#b").value}`);
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
      suggestions(w).some(b => b.textContent.includes("Convicted of a crime")),
      `got ${suggestions(w).length} suggestion(s)`);
  }

  // ---- a user's own question matches literally ------------------------
  {
    const { w } = build(
      `<div><label for="d">Do you hold a valid driving licence? (UK)</label><input id="d"></div>`
    );
    await settle();
    check("a custom question with regex characters matches literally",
      suggestions(w).some(b => b.textContent.includes("Driving licence")),
      `got: ${suggestions(w).map(b => b.textContent).join(" | ")}`);
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
    check("clicking Fill fills the text input",
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
    check("clicking Fill selects the matching option",
      w.document.querySelector("#g").value === "x",
      `value was ${JSON.stringify(w.document.querySelector("#g").value)}`);
  }

  // ---- clicking Fill checks the matching radio -------------------------
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
      check("clicking Fill checks the matching radio",
        w.document.querySelector("#y").checked === true,
        `Yes checked=${w.document.querySelector("#y").checked}`);
    }
  }

  // ---- the answer must never reach the page DOM ------------------------
  // A content script shares the DOM with the page. Rendering answers on sight
  // would hand any page the user's phone, email, and their disability,
  // ethnicity and veteran-status responses with no interaction at all — and a
  // hostile page could harvest them by planting labels like "Phone number".
  {
    const { w } = build(`
      <div><label for="a">First Name</label><input id="a"></div>
      <div><label for="b">Gender</label><input id="b"></div>
      <div><label for="c">Have you been convicted of a crime?</label><input id="c"></div>
    `);
    await settle();

    const pageText = w.document.body.innerText;
    const leaked = ["Priya", "I prefer not to say"]
      .filter(v => pageText.includes(v));

    check("no answer value is rendered into the page",
      leaked.length === 0, `leaked: ${JSON.stringify(leaked)}`);

    check("suggestions are still offered for all three",
      suggestions(w).length === 3, `got ${suggestions(w).length}`);

    // The value appears only after a click, in the field itself.
    suggestions(w)[0].querySelector("button").click();
    check("the value appears only once the user clicks",
      w.document.querySelector("#a").value === "Priya");
  }

  // ---- prose must not grow suggestion boxes ----------------------------
  // Scanning text nodes put a box mid-paragraph whenever a job description
  // happened to contain words like "your name".
  {
    const { w } = build(`
      <main>
        <p>We are hiring an AI Engineer. Please include your name and a note on
        your notice period, and tell us about your gender-diverse team work.</p>
        <span>Send your email address to the hiring team.</span>
      </main>`);
    await settle();
    check("a job description grows no suggestion boxes",
      suggestions(w).length === 0,
      `got ${suggestions(w).length}: ${suggestions(w).map(b => b.textContent).join(" | ")}`);
  }

  // ---- a radio group is one question, not one per option ----------------
  {
    const { w } = build(`
      <fieldset>
        <legend>Are you legally authorized to work in the US?</legend>
        <input type="radio" name="auth" id="y" value="Yes"><label for="y">Yes</label>
        <input type="radio" name="auth" id="n2" value="No"><label for="n2">No</label>
        <input type="radio" name="auth" id="n3" value="Decline"><label for="n3">Decline</label>
      </fieldset>`);
    await settle();
    check("a radio group gets exactly one suggestion",
      suggestions(w).length === 1, `got ${suggestions(w).length}`);
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

  // ---- signing in after the page loaded still produces suggestions ------
  // The rules used to be fetched exactly once. Opening a job page and then
  // signing in left that tab permanently empty, which is indistinguishable
  // from the feature being broken.
  {
    const { w, state, fire } = build(
      `<div><label for="n">First Name</label><input id="n"></div>`,
      { signedOut: true }
    );
    await settle();
    check("signed out, a job page shows no suggestions",
      suggestions(w).length === 0, `got ${suggestions(w).length}`);

    state.signedOut = false;
    fire({ type: "AUTOFILL_CHANGED" });
    await settle();

    check("signing in fills an already-open page without a reload",
      suggestions(w).length === 1,
      `got ${suggestions(w).length} after AUTOFILL_CHANGED`);
  }

  // ---- signing out takes the answers back off the page ------------------
  {
    const { w, state, fire } = build(
      `<div><label for="n">First Name</label><input id="n"></div>`
    );
    await settle();
    check("signed in, the suggestion is present", suggestions(w).length === 1);

    state.signedOut = true;
    fire({ type: "AUTOFILL_CHANGED" });
    await settle();

    check("signing out removes suggestions already on the page",
      suggestions(w).length === 0, `got ${suggestions(w).length}`);
  }

  // ---- a newly saved answer reaches other open tabs ---------------------
  {
    const { w, state, fire } = build(
      `<div><label for="q">Do you hold a valid driving licence? (UK)</label><input id="q"></div>`,
      { rules: [] }
    );
    await settle();
    check("an empty bank suggests nothing", suggestions(w).length === 0);

    state.rules = RULES;
    fire({ type: "AUTOFILL_CHANGED" });
    await settle();

    check("an answer saved elsewhere appears without a reload",
      suggestions(w).some(b => b.textContent.includes("Driving licence")),
      `got: ${suggestions(w).map(b => b.textContent).join(" | ")}`);
  }

  // ---- double injection must not double the suggestions -----------------
  // The manifest registration and the dynamic one for granted sites overlap,
  // and the popup injects again into tabs that predate the grant.
  {
    const { w, reinject } = build(
      `<div><label for="n">First Name</label><input id="n"></div>`
    );
    await settle();
    reinject();
    await settle();
    check("injecting the same frame twice yields one suggestion",
      suggestions(w).length === 1, `got ${suggestions(w).length}`);
  }

  // ---- only the top frame answers the popup's extraction request --------
  // With all_frames injection every iframe runs this script, and whichever
  // replied first would win — an ad frame could out-race the real posting.
  {
    const { fire } = build(`<h1>AI Engineer</h1>`);
    await settle();
    const reply = fire({ action: "extract_job" });
    check("the top frame answers the extraction request",
      reply && reply.role === "AI Engineer" && reply.company === "Nexus Labs",
      `got ${JSON.stringify(reply)}`);
  }

  // ---- drafting buttons stay off pages that cannot use them -------------
  {
    const { w } = build(
      `<form><label for="t">Describe a time you led a project</label>` +
      `<textarea id="t"></textarea></form>`,
      { signedOut: true }
    );
    await settle();
    const buttons = Array.from(w.document.querySelectorAll("button"))
      .filter(b => b.textContent.includes("Generate AI Answer"));
    check("signed out, no drafting button is offered",
      buttons.length === 0, `got ${buttons.length}`);
  }

  // A bare textarea is a comment box or a message composer, not an
  // application question.
  {
    const { w } = build(`<textarea id="t"></textarea>`);
    await settle();
    const buttons = Array.from(w.document.querySelectorAll("button"))
      .filter(b => b.textContent.includes("Generate AI Answer"));
    check("an unlabelled textarea outside a form gets no drafting button",
      buttons.length === 0, `got ${buttons.length}`);
  }

  // ---- submitting an application offers to track it ---------------------
  // Saving was a manual click in the popup, so anything submitted without
  // remembering to click it never reached the tracker at all.
  const APPLICATION_FORM = `
    <h1>AI Engineer</h1>
    <form>
      <label for="e">Email</label><input id="e">
      <label for="r">Resume</label><input id="r" type="file">
      <label for="w">Why do you want this role?</label><textarea id="w"></textarea>
      <button type="submit">Submit application</button>
    </form>`;

  {
    const { w } = build(APPLICATION_FORM);
    await settle();
    check("no save bar before submitting", !saveBar(w));

    submitForm(w);
    await settle();

    const bar = saveBar(w);
    check("submitting an application offers to track it", !!bar,
      "no save bar appeared");
    check("the offer names the employer",
      bar && bar.textContent.includes("Nexus Labs"),
      `bar said: ${bar && bar.textContent}`);
  }

  // ---- but nothing is saved without a click -----------------------------
  {
    const { w, sent } = build(APPLICATION_FORM);
    await settle();
    submitForm(w);
    await settle();

    check("submitting alone never saves the job",
      !sent.some(m => m.type === "SAVE_JOB"),
      `calls=${JSON.stringify(sent.map(m => m.type))}`);

    saveBar(w).querySelector("button").click();
    await settle();

    check("clicking Save sends the job to the tracker",
      sent.some(m => m.type === "SAVE_JOB"),
      `calls=${JSON.stringify(sent.map(m => m.type))}`);
  }

  // ---- a search box is not an application -------------------------------
  {
    const { w } = build(`<form><input id="q" placeholder="Search jobs"></form>`);
    await settle();
    submitForm(w);
    await settle();
    check("a one-field search form raises no offer", !saveBar(w));
  }

  // ---- the offer survives the page navigating away ----------------------
  // Submitting usually navigates to a confirmation page, which would destroy
  // a bar rendered on the spot.
  {
    const shared = {};
    const first = build(APPLICATION_FORM, { storage: shared });
    await settle();
    submitForm(first.w);
    await settle();
    check("the pending offer is written to storage",
      !!shared.pendingSave && shared.pendingSave.company === "Nexus Labs",
      `stored: ${JSON.stringify(shared.pendingSave)}`);

    // The confirmation page: a new document, same extension storage.
    const next = build(`<h1>Thanks for applying</h1>`, { storage: shared });
    await settle();
    check("the next page picks the offer back up", !!saveBar(next.w),
      "no save bar on the page after submitting");
  }

  // ---- a stale offer is dropped rather than shown -----------------------
  {
    const shared = {
      pendingSave: {
        company: "Nexus Labs", role: "AI Engineer", jd_text: "", link: "x",
        at: Date.now() - 60 * 60 * 1000,
      },
    };
    const { w } = build(`<h1>Some other page</h1>`, { storage: shared });
    await settle();
    check("an hour-old offer is not raised", !saveBar(w));
    check("and it is cleared from storage", !shared.pendingSave);
  }

  // ---- signed out, submitting offers nothing ----------------------------
  {
    const { w } = build(APPLICATION_FORM, { signedOut: true });
    await settle();
    submitForm(w);
    await settle();
    check("signed out, submitting raises no offer", !saveBar(w));
  }

  // ---- the drafting prompt gets the real question -----------------------
  // questionFor checked only labels[0], the previous sibling and aria-label,
  // and fell back to the literal string "Tell us about yourself" — so on a
  // form using a legend the model was asked to write about nothing.
  {
    const { w, sent } = build(`
      <fieldset>
        <legend>Describe a system you designed end to end</legend>
        <textarea id="t"></textarea>
      </fieldset>`);
    await settle();
    Array.from(w.document.querySelectorAll("button"))
      .find(b => b.textContent.includes("Generate AI Answer")).click();
    await settle();

    const call = sent.find(m => m.type === "GENERATE_ANSWER");
    check("a question in a <legend> reaches the model",
      call && call.payload.question.includes("system you designed"),
      `question was ${JSON.stringify(call && call.payload.question)}`);
  }

  {
    const { w, sent } = build(`
      <div id="q">What is your proudest project?</div>
      <textarea aria-labelledby="q" id="t"></textarea>`);
    await settle();
    Array.from(w.document.querySelectorAll("button"))
      .find(b => b.textContent.includes("Generate AI Answer")).click();
    await settle();

    const call = sent.find(m => m.type === "GENERATE_ANSWER");
    check("an aria-labelledby question reaches the model",
      call && call.payload.question.includes("proudest project"),
      `question was ${JSON.stringify(call && call.payload.question)}`);
  }

  // ---- the drafting call carries the page's job context -----------------
  {
    const { w, sent } = build(`
      <h1>AI Engineer</h1>
      <form>
        <label for="t">Describe a time you led a project</label>
        <textarea id="t"></textarea>
      </form>`);
    await settle();
    Array.from(w.document.querySelectorAll("button"))
      .find(b => b.textContent.includes("Generate AI Answer")).click();
    await settle();

    const call = sent.find(m => m.type === "GENERATE_ANSWER");
    check("the drafting call names the company and role",
      call && call.payload.company === "Nexus Labs" && call.payload.role === "AI Engineer",
      `payload=${JSON.stringify(call && call.payload)}`);
  }

  const failed = results.filter(r => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
  if (failed.length) process.exitCode = 1;
})();
