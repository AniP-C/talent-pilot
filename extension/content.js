// extension/content.js
//
// Runs in the page to (a) read job details and (b) offer AI drafting on
// free-text answer boxes. It never talks to the API directly and never holds
// the auth token — all network work is delegated to background.js.

(() => {
    "use strict";

    // The same frame can be injected twice — once by the static registration
    // in the manifest and once by the dynamic one covering granted sites, or
    // by the popup topping up a tab that was already open. Content scripts
    // from one extension share an isolated world per frame, so this flag is
    // visible to every copy and the later ones simply stand down.
    if (window.__talentPilotLoaded) return;
    window.__talentPilotLoaded = true;

    // -----------------------------------------------------------------------
    // The signed-in user's saved answers
    //
    // These used to live in a bundled rules.js holding one person's real name,
    // phone number and email — which meant the extension could only ever be
    // used by whoever built it. They now come from the API, scoped to whoever
    // is signed in, so one published build serves everybody.
    // -----------------------------------------------------------------------
    let autofillRules = [];

    // Why there are no suggestions, so the popup can say so instead of leaving
    // the user to guess. "unreachable" covers a sleeping service worker or a
    // down API and is the only state worth retrying.
    let ruleState = "loading"; // loading | ready | signed-out | unreachable

    async function loadAutofillRules() {
        try {
            const response = await chrome.runtime.sendMessage({ type: "GET_AUTOFILL" });

            if (response?.ok) {
                autofillRules = response.data.rules || [];
                ruleState = "ready";
            } else {
                autofillRules = [];
                ruleState = response?.needsAuth ? "signed-out" : "unreachable";
            }
        } catch {
            // The service worker was asleep, or the extension was reloaded out
            // from under this page. Suggestions are an enhancement; the page
            // must keep working without them.
            autofillRules = [];
            ruleState = "unreachable";
        }

        return ruleState;
    }

    // The first load races the service worker waking up, and a job page opened
    // before signing in gets nothing at all. Both used to be permanent for the
    // life of the tab: rules were fetched exactly once, so a page that came up
    // empty stayed empty however long the user waited or however many times
    // they filled the questionnaire.
    const RETRY_DELAYS_MS = [1500, 5000, 15000];

    async function loadWithRetries() {
        for (const delay of RETRY_DELAYS_MS) {
            if ((await loadAutofillRules()) !== "unreachable") return;
            await new Promise((resolve) => setTimeout(resolve, delay));
        }
        await loadAutofillRules();
    }

    // Coming back to the tab is the moment a sign-in or a newly saved answer
    // in another tab is most likely to have happened.
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState !== "visible") return;
        if (ruleState === "ready" && autofillRules.length) return;
        loadAutofillRules().then(scheduleScan);
    });

    // Patterns arrive as strings so they can cross the message boundary.
    // Catalogue entries are curated regexes; a user's own question is matched
    // literally, so typing "(" into it cannot produce a broken pattern.
    function ruleMatches(rule, text) {
        return rule.patterns.some((pattern) => {
            if (rule.literal) {
                return text.toLowerCase().includes(pattern.toLowerCase());
            }
            try {
                return new RegExp(pattern, "i").test(text);
            } catch {
                return false;
            }
        });
    }

    // Order is significant and set by the server: "first name" is tested
    // before "name", or every name field fills with the full name.
    function findAnswer(text) {
        return autofillRules.find((rule) => ruleMatches(rule, text)) || null;
    }

    const FIELD_SELECTOR =
        "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=file]), " +
        "textarea, select";

    // -----------------------------------------------------------------------
    // Passive mode: offer the user's saved answer beside a matching field
    //
    // The scan walks FORM FIELDS and works out what each one is asking, rather
    // than scanning every text node for something question-shaped. Scanning
    // text was both noisy and wrong: a job description containing the words
    // "your name" grew a suggestion box mid-paragraph, and the answer then had
    // to be matched back to a field by guessing at the nearest one.
    // -----------------------------------------------------------------------

    // What a field is asking, gathered from every place forms put that text.
    function questionTextFor(field) {
        const parts = [
            field.labels?.[0]?.innerText,
            field.getAttribute("aria-label"),
            field.getAttribute("placeholder"),
            field.getAttribute("name"),
        ];

        const describedBy = field.getAttribute("aria-labelledby");
        if (describedBy) {
            describedBy.split(/\s+/).forEach((id) => {
                parts.push(document.getElementById(id)?.innerText);
            });
        }

        // Compliance questions are a paragraph of legal text wrapping a
        // fieldset, with the actual question at the end.
        const legend = field.closest("fieldset")?.querySelector("legend");
        if (legend) parts.push(legend.innerText);

        // Last resort: the immediate container's own text, minus any nested
        // field values, for forms that use neither <label> nor aria.
        if (!parts.some(Boolean)) {
            parts.push(field.parentElement?.innerText);
        }

        return parts
            .filter(Boolean)
            .map((part) => part.trim())
            .filter((part) => part.length && part.length <= 600)
            .join(" \n ");
    }

    function scanAndSuggest() {
        if (!autofillRules.length) return;

        const handledRadioGroups = new Set();

        document.querySelectorAll(FIELD_SELECTOR).forEach((field) => {
            if (field.dataset.aiSuggested) return;
            // Our own controls must never be treated as form fields to fill.
            if (field.closest(".ai-copilot-suggestion")) return;

            // A radio group is one question, not one per option.
            if (field.type === "radio" && field.name) {
                if (handledRadioGroups.has(field.name)) return;
                handledRadioGroups.add(field.name);
            }

            const question = questionTextFor(field);
            if (!question) return;

            const rule = findAnswer(question);
            if (!rule) return;

            const anchor = field.closest("fieldset") || field;
            anchor.insertAdjacentElement("afterend", buildSuggestion(rule, field));
            field.dataset.aiSuggested = "true";
        });
    }

    // The suggestion deliberately does NOT render the answer.
    //
    // A content script shares the DOM with the page, so any text put here is
    // readable by page scripts. Rendering answers on sight would hand every
    // page the user's phone number, email, and their disability, ethnicity and
    // veteran-status responses — without the user doing anything, and to any
    // site the copilot is enabled on. Only the catalogue's question label is
    // shown; the value moves from the extension into the field on click, and
    // is then exactly as exposed as anything else the user typed.
    function buildSuggestion(rule, field) {
        const box = document.createElement("div");
        box.className = "ai-copilot-suggestion";

        const label = document.createElement("span");
        label.textContent = `💡 Saved answer for “${rule.question}”`;

        const apply = document.createElement("button");
        apply.type = "button";
        apply.textContent = "Fill";
        Object.assign(apply.style, {
            all: "initial",
            marginLeft: "8px",
            padding: "1px 7px",
            backgroundColor: "#0056b3",
            color: "#fff",
            borderRadius: "4px",
            fontFamily: "Arial, sans-serif",
            fontSize: "11px",
            cursor: "pointer"
        });

        apply.addEventListener("click", (event) => {
            event.preventDefault();
            event.stopPropagation();
            apply.textContent = fillField(field, rule.answer) ? "Filled" : "Could not fill";
        });

        box.append(label, apply);

        Object.assign(box.style, {
            all: "initial",
            display: "block",
            fontFamily: "Arial, sans-serif",
            color: "#0056b3",
            backgroundColor: "#e8f4fd",
            border: "1px solid #b8daff",
            padding: "4px 8px",
            margin: "4px 0 8px",
            borderRadius: "6px",
            fontSize: "12px",
            width: "max-content",
            maxWidth: "100%",
            boxShadow: "0 2px 4px rgba(0,0,0,0.05)"
        });

        return box;
    }

    // Fills one specific field — the one the suggestion was built for, so
    // there is no guessing at which control an answer belongs to. Only ever
    // called from a click: silently populating a form the user is about to
    // submit is not something to do on their behalf.
    function fillField(field, value) {
        const wanted = value.trim().toLowerCase();

        if (field.tagName === "SELECT") {
            const option = Array.from(field.options).find(
                (candidate) =>
                    candidate.text.trim().toLowerCase() === wanted ||
                    candidate.value.trim().toLowerCase() === wanted
            );
            if (!option) return false;
            field.value = option.value;
        } else if (field.type === "radio" || field.type === "checkbox") {
            // Filtered in JS rather than built into a selector: a name
            // containing a quote or bracket would need escaping, and CSS.escape
            // is not available everywhere this runs.
            const group = field.name
                ? Array.from(
                      document.querySelectorAll(`input[type="${field.type}"]`)
                  ).filter((candidate) => candidate.name === field.name)
                : [field];

            const target = group.find((candidate) => {
                const text =
                    candidate.labels?.[0]?.innerText ||
                    candidate.getAttribute("aria-label") ||
                    candidate.value ||
                    "";
                return text.trim().toLowerCase() === wanted;
            });

            if (!target) return false;

            target.checked = true;
            target.dispatchEvent(new Event("change", { bubbles: true }));
            return true;
        } else {
            field.value = value;
        }

        // React-based forms track state internally and ignore a raw .value
        // assignment unless these follow it.
        field.dispatchEvent(new Event("input", { bubbles: true }));
        field.dispatchEvent(new Event("change", { bubbles: true }));
        return true;
    }

    // -----------------------------------------------------------------------
    // Active mode: an AI drafting button under each free-text answer box
    // -----------------------------------------------------------------------
    // A textarea worth offering to draft: one inside a form, or one carrying a
    // question of its own. Every textarea on the page was too broad — signed
    // out it put a button the user could only get an error from under LinkedIn's
    // message composer and under the comment box on any enabled site.
    function isApplicationQuestion(textarea) {
        if (textarea.closest("form")) return true;
        return Boolean(
            textarea.labels?.length ||
            textarea.getAttribute("aria-label") ||
            textarea.getAttribute("aria-labelledby") ||
            // Long-form questions are routinely wrapped in a fieldset whose
            // legend is the question, with no label element anywhere.
            textarea.closest("fieldset")?.querySelector("legend")
        );
    }

    function injectAIGenerateButtons() {
        // Drafting needs a signed-in session and a resume behind it. Offering
        // the button without one produces a button whose only outcome is an
        // error message.
        if (ruleState !== "ready") return;

        document.querySelectorAll("textarea").forEach((textarea) => {
            if (textarea.dataset.aiButtonAdded) return;
            if (!isApplicationQuestion(textarea)) return;

            const button = buildGenerateButton(textarea);
            textarea.insertAdjacentElement("afterend", button);
            textarea.dataset.aiButtonAdded = "true";
        });
    }

    // The question a textarea is asking.
    //
    // This used to check only labels[0], the previous sibling and aria-label,
    // and fall back to the literal string "Tell us about yourself" — so on any
    // form that puts its question in a <legend>, an aria-labelledby, or a plain
    // <div> above the box, the model was genuinely asked to write about
    // nothing. Sources are tried strongest first and the first real one wins;
    // unlike the matcher's version this returns one question rather than every
    // scrap of text, because it is going into a prompt.
    function questionFor(textarea) {
        const describedBy = (textarea.getAttribute("aria-labelledby") || "")
            .split(/\s+/)
            .filter(Boolean)
            .map((id) => document.getElementById(id)?.innerText);

        const candidates = [
            textarea.labels?.[0]?.innerText,
            textarea.getAttribute("aria-label"),
            ...describedBy,
            textarea.closest("fieldset")?.querySelector("legend")?.innerText,
            textarea.previousElementSibling?.innerText,
            textarea.parentElement?.querySelector("label")?.innerText,
            textarea.getAttribute("placeholder"),
            // Last resort: the container's own text, which on label-less forms
            // is the question with the box's own (empty) value beside it.
            textarea.parentElement?.innerText,
        ];

        const found = candidates
            .map((part) => clean(part))
            .find((part) => part.length > 2 && part.length <= 600);

        return found || "";
    }

    // The posting and the form are frequently different pages, and an embedded
    // form is an iframe that can see none of the posting around it. Asking the
    // top frame is the difference between the model knowing which company and
    // role it is writing for and guessing.
    async function jobContext() {
        const here = extractJobData();

        if (window.top === window) return here;

        try {
            const response = await chrome.runtime.sendMessage({ type: "GET_TOP_FRAME_JOB" });
            const outer = response?.ok ? response.data : null;

            if (outer && (outer.company || outer.jd_text)) {
                // Prefer whichever source actually has a description; the outer
                // page usually does and the form iframe usually does not.
                return {
                    company: outer.company || here.company,
                    role: outer.role || here.role,
                    jd_text: outer.jd_text.length >= here.jd_text.length
                        ? outer.jd_text
                        : here.jd_text,
                };
            }
        } catch {
            // The top frame has no content script, which is normal on a site
            // enabled only for this origin. Fall through to what we can see.
        }

        return here;
    }

    function buildGenerateButton(textarea) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "✨ Generate AI Answer";

        Object.assign(button.style, {
            all: "initial",
            display: "block",
            marginTop: "5px",
            padding: "5px 10px",
            backgroundColor: "#6200ee",
            color: "white",
            border: "none",
            borderRadius: "4px",
            cursor: "pointer",
            fontFamily: "Arial, sans-serif",
            fontSize: "12px"
        });

        button.addEventListener("click", (event) => {
            event.preventDefault();
            handleGenerate(textarea, button);
        });

        return button;
    }

    async function handleGenerate(textarea, button) {
        const question = questionFor(textarea);

        // Better to say the question could not be read than to send the model
        // a placeholder and return a paragraph about nothing in particular.
        if (!question) {
            finish(button, "❌ Could not read the question for this box", "#d9534f");
            return;
        }

        const cacheKey = `ans_${location.href}_${question.slice(0, 50)}`;

        button.disabled = true;
        button.textContent = "⏳ Checking…";

        // A previously saved answer costs nothing and is what the user already
        // decided to say. Checked before both the local cache and the model.
        const saved = findAnswer(question);
        if (saved) {
            applyAnswer(textarea, saved.answer);
            finish(button, "✅ Your saved answer", "#28a745");
            return;
        }

        const cached = await chrome.storage.local.get(cacheKey);
        if (cached[cacheKey]) {
            applyAnswer(textarea, cached[cacheKey]);
            finish(button, "✅ Restored from cache", "#28a745");
            return;
        }

        button.textContent = "🤖 Generating…";

        const job = await jobContext();

        // Which resume to answer as. The popup records the chosen profile and
        // the content script never read it, so someone keeping one resume per
        // target role was drafted from whichever the server picked by default.
        const { activeProfile } = await chrome.storage.local.get("activeProfile");

        const response = await chrome.runtime.sendMessage({
            type: "GENERATE_ANSWER",
            payload: {
                question,
                company: job.company,
                role: job.role,
                jd_text: job.jd_text,
                profile: activeProfile || null
            }
        });

        if (!response?.ok) {
            finish(button, `❌ ${response?.error || "Request failed"}`, "#d9534f");
            return;
        }

        const answer = response.data.suggested_answer;
        applyAnswer(textarea, answer);
        chrome.storage.local.set({ [cacheKey]: answer });

        // Short answers join the bank so this question never costs a model
        // call again. Long-form essays are left out deliberately: they are
        // tailored per application, and replaying one verbatim reads worse
        // than redrafting it.
        if (answer && answer.length <= 2000) {
            chrome.runtime
                .sendMessage({ type: "SAVE_CUSTOM_ANSWER", question, answer })
                .then(() => loadAutofillRules())
                .catch(() => {});
            finish(button, "✅ Generated and saved — review before submitting", "#28a745");
            return;
        }

        finish(button, "✅ Generated — review before submitting", "#28a745");
    }

    function applyAnswer(textarea, value) {
        textarea.value = value;
        // React-based forms track state internally and ignore a raw .value
        // assignment unless an input event follows it.
        textarea.dispatchEvent(new Event("input", { bubbles: true }));
    }

    function finish(button, label, color) {
        button.textContent = label;
        button.style.backgroundColor = color;
        button.disabled = false;
    }

    // -----------------------------------------------------------------------
    // Job extraction
    //
    // Company is the field that matters most and the one that used to be
    // wrong. The tracker's identity is (company, role), so a company holding
    // a job title means later recruiter emails cannot find the application
    // they belong to and open a duplicate instead.
    //
    // It used to be derived by splitting document.title, which silently
    // assumed every board writes "Company - Role". Workable, Ashby and most
    // company career pages write "Role - Company", so the split returned the
    // role. Sources are now tried strongest first:
    //
    //   1. JSON-LD JobPosting  - a declared field, not a guess
    //   2. og:site_name        - the site naming itself
    //   3. per-board selectors - real elements, no string surgery
    //   4. title patterns      - last resort, board-specific ordering
    //
    // Anything that comes back looking like a job title is discarded rather
    // than stored.
    // -----------------------------------------------------------------------

    // Mirrors _ROLE_WORDS in job_fields.py. The server rejects these too; this
    // copy exists so the popup can warn before a request is ever sent.
    const ROLE_WORDS = /\b(engineer|engineering|developer|designer|analyst|scientist|manager|director|architect|consultant|specialist|administrator|intern|internship|trainee|associate|lead|head|officer|executive|programmer|researcher|technician|recruiter|coordinator|strategist|apprentice|graduate|fresher|devops|sre)\b/i;

    const ROLE_QUALIFIERS = /^(ai|ml|senior|sr|junior|jr|staff|principal|lead|chief|mid|intermediate|i|ii|iii|full|stack|fullstack|front|frontend|back|backend|end|web|mobile|android|ios|cloud|data|big|deep|machine|learning|generative|genai|mlops|platform|product|project|software|systems?|solutions?|security|quality|qa|test|support|analytics|python|java|javascript|react|node|golang|go|rust|of|and|the|a|an|&|-)$/i;

    // Excludes software/systems/solutions/services/consulting on purpose:
    // they read as corporate suffixes in "Acme Software" but as role
    // qualifiers in "Software Engineer II". Keep in step with
    // _COMPANY_MARKERS in job_fields.py.
    const COMPANY_MARKERS = /\b(inc|llc|ltd|limited|plc|gmbh|corp|corporation|co|company|pvt|private|technologies|labs|group|holdings|ventures|partners|industries|sa|ag|bv|nv|ab|oy|srl|spa|pty)\b\.?/i;

    const PLACEHOLDERS = new Set([
        "", "-", "n/a", "na", "none", "null", "undefined", "unknown",
        "unknown company", "unknown role", "company", "role", "job", "jobs",
        "position", "career", "careers", "apply", "application",
        "job application", "hiring", "we are hiring", "home"
    ]);

    // True when a string reads as a job title rather than an employer.
    // Conservative on purpose: rejecting a real company is worse than letting
    // an odd one through, so a corporate suffix or any word that is neither a
    // role word nor a qualifier keeps the string.
    function looksLikeRole(value) {
        const cleaned = clean(value);
        if (!cleaned) return false;
        if (COMPANY_MARKERS.test(cleaned)) return false;
        if (!ROLE_WORDS.test(cleaned)) return false;

        return cleaned
            .split(/[\s/,|·–—-]+/)
            .filter(Boolean)
            .every((word) => ROLE_WORDS.test(word) || ROLE_QUALIFIERS.test(word));
    }

    function isPlaceholder(value) {
        return PLACEHOLDERS.has(clean(value).toLowerCase());
    }

    // Accept a candidate company only if it is informative and is not the role.
    function acceptCompany(candidate, role) {
        const cleaned = clean(candidate);
        if (!cleaned || cleaned.length > 200) return "";
        if (isPlaceholder(cleaned)) return "";
        if (role && cleaned.toLowerCase() === clean(role).toLowerCase()) return "";
        if (looksLikeRole(cleaned)) return "";
        return cleaned;
    }

    // --- Source 1: schema.org JobPosting ------------------------------------
    // Greenhouse, Lever, Ashby, Workable and most ATS-hosted career pages all
    // emit this. hiringOrganization.name is declared data rather than a
    // guess, which makes it immune to markup and title-format churn.
    function fromJsonLd() {
        const found = { company: "", role: "", jd_text: "" };

        for (const node of document.querySelectorAll('script[type="application/ld+json"]')) {
            let parsed;
            try {
                parsed = JSON.parse(node.textContent);
            } catch {
                continue; // a malformed block must not abort the others
            }

            // Blocks may be a single object, an array, or an @graph wrapper.
            const candidates = []
                .concat(parsed)
                .flatMap((entry) => (entry && entry["@graph"]) || entry)
                .filter(Boolean);

            for (const entry of candidates) {
                if (entry["@type"] !== "JobPosting") continue;

                const org = entry.hiringOrganization;
                found.company = clean(typeof org === "string" ? org : org?.name || "");
                found.role = clean(entry.title || "");

                if (entry.description) {
                    // description is HTML; render it to text without ever
                    // attaching it to the live document.
                    const holder = document.createElement("div");
                    holder.innerHTML = entry.description;
                    found.jd_text = clean(holder.textContent || "");
                }

                if (found.company || found.role) return found;
            }
        }

        return found;
    }

    // --- Source 2: Open Graph ------------------------------------------------
    function fromMeta() {
        return clean(
            document.querySelector('meta[property="og:site_name"]')?.content || ""
        );
    }

    // --- Source 3+4: per-board adapters -------------------------------------
    // Declarative so a board changing its markup is a one-line edit here
    // rather than a new branch in a growing if/else chain.
    //
    // `titleCompany` receives document.title and returns the company part.
    // Each is written against that board's actual title format instead of
    // assuming a shared one.
    const ADAPTERS = [
        {
            match: /linkedin\.com$/,
            company: ".job-details-jobs-unified-top-card__company-name, .topcard__org-name-link, .pr2.t-14",
            role: ".job-details-jobs-unified-top-card__job-title, .topcard__title, h1",
            jd: ".jobs-description__content, #job-details, .description__text",
            // "Role | Company | LinkedIn"
            titleCompany: (t) => t.split("|")[1] || ""
        },
        {
            match: /greenhouse\.io$/,
            company: '.company-name, [class*="companyName"], header img[alt]',
            role: ".app-title, .job__title h1, h1",
            jd: "#content, .job__description, #main, .accessible-wrapper",
            // "Job Application for <Role> at <Company>"
            titleCompany: (t) => t.split(/\bat\s+/i).slice(1).join(" at ") || ""
        },
        {
            match: /lever\.co$/,
            company: '.main-header-logo img[alt], [class*="companyName"]',
            role: ".posting-headline h2, h2",
            jd: ".posting-details, .section-wrapper, main",
            // "Company - Role"  (company first on Lever)
            titleCompany: (t) => t.split(" - ")[0] || ""
        },
        {
            match: /ashbyhq\.com$/,
            company: '[class*="companyName"], header img[alt]',
            role: 'h1, [class*="jobTitle"]',
            jd: '[class*="descriptionText"], main',
            // "Role @ Company", and "Role - Company" on some boards: the
            // company is the LAST segment, never the first.
            titleCompany: (t) => t.split(/\s+[@|]\s+|\s+-\s+/).slice(1).pop() || ""
        },
        {
            match: /workable\.com$/,
            company: '[data-ui="company-name"], [class*="companyName"], header img[alt]',
            role: '[data-ui="job-title"], h1',
            jd: '[data-ui="job-description"], main',
            // "Role - Company"  (company LAST, the original bug)
            titleCompany: (t) => t.split(" - ").slice(1).join(" - ") || ""
        },
        {
            match: /(wellfound\.com|angel\.co)$/,
            company: '[class*="company"] h1, a[href^="/company/"]',
            role: 'h2, [class*="jobTitle"]',
            jd: null,
            // "Role at Company"
            titleCompany: (t) => t.split(/\s+at\s+/i).slice(1).join(" at ") || ""
        }
    ];

    // Fallback for any other origin the user enables on demand. Company career
    // pages overwhelmingly write "<Role> - <Company>" or "<Role> | <Company>",
    // so the LAST segment is the better guess — the opposite of the old code.
    const GENERIC_ADAPTER = {
        company: 'meta[property="og:site_name"], [class*="company-name"]',
        role: "h1",
        jd: null,
        titleCompany: (t) => {
            const parts = t.split(/\s+[|–—]\s+|\s+-\s+/).map(clean).filter(Boolean);
            if (parts.length < 2) return "";
            // Trim boilerplate tails like "Careers" or "Jobs".
            const tail = parts[parts.length - 1];
            const stripped = clean(tail.replace(/\b(careers?|jobs?|hiring|job board)\b/gi, ""));
            return stripped || tail;
        }
    };

    function adapterFor(hostname) {
        return ADAPTERS.find((entry) => entry.match.test(hostname)) || GENERIC_ADAPTER;
    }

    function extractJobData() {
        const adapter = adapterFor(location.hostname);
        let company = "";
        let role = "";
        let jd_text = "";
        let companySource = "none";

        try {
            const structured = fromJsonLd();

            role = structured.role || text(adapter.role) || text("h1");
            jd_text = structured.jd_text || (adapter.jd ? text(adapter.jd) : "") || collectParagraphs();

            // Strongest source first; each candidate must survive acceptCompany.
            const candidates = [
                ["json-ld", structured.company],
                ["selector", textOrAttr(adapter.company)],
                ["og:site_name", fromMeta()],
                ["title", adapter.titleCompany(document.title || "")]
            ];

            for (const [source, value] of candidates) {
                const accepted = acceptCompany(value, role);
                if (accepted) {
                    company = accepted;
                    companySource = source;
                    break;
                }
            }
        } catch (err) {
            console.error("[Job Copilot] extraction failed:", err);
        }

        role = clean(role);

        // Reporting no company beats reporting a wrong one: the popup can ask
        // the user to type it, whereas a silently wrong value becomes a
        // corrupt row that only surfaces weeks later as a duplicate.
        return {
            company: company || "",
            role: role && !isPlaceholder(role) ? role : "",
            jd_text: clean(jd_text).slice(0, 8000),
            link: location.href,
            company_source: companySource
        };
    }

    function text(selector) {
        if (!selector) return "";
        return document.querySelector(selector)?.innerText || "";
    }

    // Logos carry the company in alt text where no text node exists.
    function textOrAttr(selector) {
        if (!selector) return "";
        const el = document.querySelector(selector);
        if (!el) return "";
        return clean(el.innerText || el.getAttribute("alt") || el.content || "");
    }

    function collectParagraphs() {
        const root = document.querySelector("main") || document.body;
        return Array.from(root.querySelectorAll("p, ul > li"))
            .map((el) => el.innerText)
            .filter((value) => value && value.length > 30)
            .join("\n");
    }

    function clean(value) {
        return (value || "").replace(/\s+/g, " ").trim();
    }

    // -----------------------------------------------------------------------
    // Catching an application as it is submitted
    //
    // Saving was a manual click in the popup, so anything submitted without
    // remembering to click it never reached the tracker at all — the largest
    // gap between "applied" and "tracked", and the one the user cannot see.
    //
    // The intent is stashed rather than acted on. Submitting usually navigates,
    // which would destroy a bar rendered on the spot, so the offer is written
    // to storage and picked up by whichever page loads next. Forms that submit
    // over XHR stay put and see the bar immediately.
    //
    // Nothing is ever saved without a click. The extension does not get to
    // decide the user applied to something.
    // -----------------------------------------------------------------------
    const PENDING_SAVE_KEY = "pendingSave";
    const PENDING_SAVE_TTL_MS = 10 * 60 * 1000;

    // A form worth offering to track. A single-input form is a search box; an
    // application has a resume upload, a free-text question, or simply a lot
    // of fields.
    function looksLikeApplication(form) {
        if (!form) return false;
        if (form.querySelector('input[type="file"], textarea')) return true;
        return form.querySelectorAll(FIELD_SELECTOR).length >= 4;
    }

    document.addEventListener(
        "submit",
        (event) => {
            if (ruleState !== "ready") return;
            if (!looksLikeApplication(event.target)) return;

            // Deliberately not awaited: this handler runs during submit and the
            // page may be seconds from unloading.
            jobContext()
                .then(async (job) => {
                    // Neither a company nor a role means this is not a job
                    // posting. Returning here rather than falling through also
                    // stops an unrelated form raising an offer left over from
                    // an earlier submission on another tab.
                    if (!job.company && !job.role) return;

                    await chrome.storage.local.set({
                        [PENDING_SAVE_KEY]: {
                            company: job.company,
                            role: job.role,
                            jd_text: job.jd_text,
                            link: location.href,
                            at: Date.now()
                        }
                    });

                    // Forms that post over XHR stay on the page and see this
                    // straight away; ones that navigate pick it up on arrival.
                    await offerPendingSave();
                })
                .catch(() => {
                    // A failed offer must never interfere with the submission
                    // the user actually came here to make.
                });
        },
        true // capture: forms that preventDefault still reach this first
    );

    async function offerPendingSave() {
        if (ruleState !== "ready") return;
        if (document.querySelector(".ai-copilot-save-bar")) return;

        const stored = await chrome.storage.local.get(PENDING_SAVE_KEY);
        const pending = stored[PENDING_SAVE_KEY];

        if (!pending) return;

        // An offer from a browsing session hours ago is noise, not a prompt.
        if (Date.now() - (pending.at || 0) > PENDING_SAVE_TTL_MS) {
            await chrome.storage.local.remove(PENDING_SAVE_KEY);
            return;
        }

        // Already tracked — saying so beats offering to save it twice, and the
        // database would reject the duplicate anyway.
        const check = await chrome.runtime.sendMessage({
            type: "CHECK_JOB",
            company: pending.company,
            role: pending.role
        });

        if (check?.ok && check.data.exists) {
            await chrome.storage.local.remove(PENDING_SAVE_KEY);
            return;
        }

        document.body.appendChild(buildSaveBar(pending));
    }

    function buildSaveBar(pending) {
        const bar = document.createElement("div");
        bar.className = "ai-copilot-save-bar";

        const label = document.createElement("span");
        // Page-derived values only, and set as text: a job posting is
        // untrusted input and this element lives in the page's own DOM.
        label.textContent = `Track your application to ${
            pending.company || "this company"
        }${pending.role ? ` — ${pending.role}` : ""}?`;

        const save = document.createElement("button");
        save.type = "button";
        save.textContent = "Save to tracker";
        styleBarButton(save, "#1e7e34");

        const dismiss = document.createElement("button");
        dismiss.type = "button";
        dismiss.textContent = "Not now";
        styleBarButton(dismiss, "transparent");
        dismiss.style.color = "#d6d9de";

        save.addEventListener("click", async () => {
            save.disabled = true;
            save.textContent = "Saving…";

            const response = await chrome.runtime.sendMessage({
                type: "SAVE_JOB",
                job: {
                    company: pending.company,
                    role: pending.role || "Unknown Role",
                    jd_text: pending.jd_text,
                    link: pending.link,
                    profile: (await chrome.storage.local.get("activeProfile")).activeProfile || null
                }
            });

            await chrome.storage.local.remove(PENDING_SAVE_KEY);

            if (!response?.ok) {
                save.textContent = "Could not save";
                label.textContent = response?.error || "Saving failed.";
                return;
            }

            label.textContent = "✅ Saved to your tracker.";
            save.remove();
            setTimeout(() => bar.remove(), 2500);
        });

        dismiss.addEventListener("click", async () => {
            await chrome.storage.local.remove(PENDING_SAVE_KEY);
            bar.remove();
        });

        bar.append(label, save, dismiss);

        Object.assign(bar.style, {
            all: "initial",
            position: "fixed",
            bottom: "16px",
            right: "16px",
            zIndex: "2147483647",
            display: "flex",
            alignItems: "center",
            gap: "10px",
            maxWidth: "min(420px, calc(100vw - 32px))",
            padding: "10px 14px",
            backgroundColor: "#1f2328",
            color: "#fff",
            borderRadius: "8px",
            fontFamily: "Arial, sans-serif",
            fontSize: "13px",
            lineHeight: "1.4",
            boxShadow: "0 4px 16px rgba(0,0,0,0.3)"
        });

        return bar;
    }

    function styleBarButton(button, background) {
        Object.assign(button.style, {
            all: "initial",
            flexShrink: "0",
            padding: "5px 10px",
            backgroundColor: background,
            color: "#fff",
            borderRadius: "5px",
            fontFamily: "Arial, sans-serif",
            fontSize: "12px",
            fontWeight: "bold",
            cursor: "pointer"
        });
    }

    // -----------------------------------------------------------------------
    // Wiring
    // -----------------------------------------------------------------------
    chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
        // Answers changed elsewhere — a sign-in, a sign-out, or an answer saved
        // from another tab. Re-reading here is what stops the user having to
        // reload every open application form.
        if (request.type === "AUTOFILL_CHANGED") {
            loadAutofillRules().then(() => {
                // A sign-out has to remove what is already on the page; leaving
                // the boxes up would keep the previous account's answers one
                // click from being filled in.
                if (!autofillRules.length) removeSuggestions();
                scheduleScan();
            });
            return false;
        }

        // Only the top frame answers this. With all_frames injection every
        // iframe on the page runs this script too, and whichever replied first
        // would win — so an ad frame could out-race the real posting.
        if (request.action === "extract_job" && window.top === window) {
            sendResponse(extractJobData());
        }

        // What the popup needs to explain an empty page: whether the script is
        // here at all, and if so why it is not suggesting anything.
        if (request.action === "copilot_state" && window.top === window) {
            sendResponse({ ruleState, ruleCount: autofillRules.length });
        }

        return false;
    });

    function removeSuggestions() {
        document.querySelectorAll(".ai-copilot-suggestion").forEach((box) => box.remove());
        document
            .querySelectorAll("[data-ai-suggested]")
            .forEach((field) => delete field.dataset.aiSuggested);
    }

    // A MutationObserver reacts to job boards rendering asynchronously without
    // the constant re-scanning the old 3-second interval did on every tab.
    let scheduled = false;
    function scheduleScan() {
        if (scheduled) return;
        scheduled = true;
        setTimeout(() => {
            scheduled = false;
            scanAndSuggest();
            injectAIGenerateButtons();
        }, 400);
    }

    new MutationObserver(scheduleScan).observe(document.body, {
        childList: true,
        subtree: true
    });

    // Answers have to arrive before the first scan, or a form rendered
    // immediately gets no suggestions until something else mutates the page.
    loadAutofillRules().then(() => {
        scheduleScan();

        // A submission usually navigates to a confirmation page, so the offer
        // to track it is picked up here rather than where it was made. Top
        // frame only: an embedded form and its parent would otherwise both
        // raise a bar for the same application.
        if (window.top === window) offerPendingSave();

        // Keep trying in the background when the first attempt could not reach
        // the service worker, rescanning after each attempt that changes
        // anything. The old code gave up here and the tab stayed dead.
        if (ruleState === "unreachable") loadWithRetries().then(scheduleScan);
    });
})();
