// extension/content.js
//
// Runs in the page to (a) read job details and (b) offer AI drafting on
// free-text answer boxes. It never talks to the API directly and never holds
// the auth token — all network work is delegated to background.js.

(() => {
    "use strict";

    // -----------------------------------------------------------------------
    // The signed-in user's saved answers
    //
    // These used to live in a bundled rules.js holding one person's real name,
    // phone number and email — which meant the extension could only ever be
    // used by whoever built it. They now come from the API, scoped to whoever
    // is signed in, so one published build serves everybody.
    // -----------------------------------------------------------------------
    let autofillRules = [];
    let rulesLoaded = false;

    async function loadAutofillRules() {
        try {
            const response = await chrome.runtime.sendMessage({ type: "GET_AUTOFILL" });
            autofillRules = response?.ok ? response.data.rules || [] : [];
        } catch {
            // Not signed in, or the service worker is asleep. Suggestions are
            // an enhancement; the page must keep working without them.
            autofillRules = [];
        }
        rulesLoaded = true;
    }

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

    // -----------------------------------------------------------------------
    // Passive mode: annotate known questions with the user's saved answer
    // -----------------------------------------------------------------------
    function scanAndSuggest() {
        if (!autofillRules.length) return;

        document.querySelectorAll("label, legend, h3, h4, p, span").forEach((el) => {
            if (el.dataset.aiScanned || el.classList.contains("ai-copilot-suggestion")) return;
            if (el.closest("[data-ai-scanned]")) return;

            const text = el.innerText;
            // Compliance questions are genuinely long — a paragraph of legal
            // text ending in "have you ever been convicted?" is the norm — so
            // the cap is generous rather than tight.
            if (!text || text.length > 600) return;

            const rule = findAnswer(text);
            if (!rule) return;

            el.insertAdjacentElement("afterend", buildSuggestion(rule));
            el.dataset.aiScanned = "true";
        });
    }

    function buildSuggestion(rule) {
        const box = document.createElement("div");
        box.className = "ai-copilot-suggestion";

        const label = document.createElement("b");
        label.textContent = "💡 ";

        // textContent, not innerHTML — answers are user-supplied and must
        // never be parsed as markup.
        box.append(label, document.createTextNode(rule.answer));

        // Clicking fills the nearest field rather than making the user retype
        // an answer the extension is already showing them.
        const apply = document.createElement("button");
        apply.type = "button";
        apply.textContent = "Use";
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
            const filled = fillNearestField(box, rule.answer);
            apply.textContent = filled ? "Filled" : "No field found";
        });

        box.appendChild(apply);

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

    const FIELD_SELECTOR =
        "input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, select";

    // Fills the field the suggestion belongs to. Never runs on its own — only
    // from a click, because silently populating a form the user is about to
    // submit is not something to do on their behalf.
    function fillNearestField(anchor, value) {
        // Search must start ABOVE the suggestion box: closest() includes the
        // element itself, and the box is a <div>, so starting at it returns
        // the box — which contains no form field but its own button.
        //
        // Walking outwards a few levels handles the nesting real forms use,
        // where the label and its input are cousins rather than siblings.
        let scope = anchor.parentElement;
        let field = null;

        for (let depth = 0; scope && depth < 4; depth += 1) {
            field = scope.querySelector(FIELD_SELECTOR);
            if (field) break;
            scope = scope.parentElement;
        }

        if (!field || !scope) return false;

        if (field.tagName === "SELECT") {
            const option = Array.from(field.options).find(
                (candidate) =>
                    candidate.text.trim().toLowerCase() === value.trim().toLowerCase() ||
                    candidate.value.trim().toLowerCase() === value.trim().toLowerCase()
            );
            if (!option) return false;
            field.value = option.value;
        } else if (field.type === "radio" || field.type === "checkbox") {
            const radios = scope.querySelectorAll(`input[type="${field.type}"]`);
            const wanted = Array.from(radios).find((candidate) => {
                const label = candidate.labels?.[0]?.innerText || candidate.value || "";
                return label.trim().toLowerCase() === value.trim().toLowerCase();
            });
            if (!wanted) return false;
            wanted.checked = true;
            wanted.dispatchEvent(new Event("change", { bubbles: true }));
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
    function injectAIGenerateButtons() {
        document.querySelectorAll("textarea").forEach((textarea) => {
            if (textarea.dataset.aiButtonAdded) return;

            const button = buildGenerateButton(textarea);
            textarea.insertAdjacentElement("afterend", button);
            textarea.dataset.aiButtonAdded = "true";
        });
    }

    function questionFor(textarea) {
        const label =
            textarea.labels?.[0] ||
            textarea.previousElementSibling ||
            textarea.parentElement?.querySelector("label");

        return (label?.innerText || textarea.getAttribute("aria-label") || "").trim() ||
            "Tell us about yourself";
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

        const job = extractJobData();
        const response = await chrome.runtime.sendMessage({
            type: "GENERATE_ANSWER",
            payload: {
                question,
                company: job.company,
                role: job.role,
                jd_text: job.jd_text
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
    // Wiring
    // -----------------------------------------------------------------------
    chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
        if (request.action === "extract_job") {
            sendResponse(extractJobData());
        }
        return false;
    });

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
    loadAutofillRules().then(scheduleScan);
})();
