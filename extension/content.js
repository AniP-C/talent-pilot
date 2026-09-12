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
                //
                // The posting's facts come from the outer frame wholesale rather
                // than field by field: an embedded application form declares no
                // JSON-LD, so a location or salary seen in here is either the
                // outer page's or nothing at all.
                return {
                    company: outer.company || here.company,
                    role: outer.role || here.role,
                    jd_text: outer.jd_text.length >= here.jd_text.length
                        ? outer.jd_text
                        : here.jd_text,
                    ...postingFieldsOf(outer.location ? outer : here),
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

    // What makes two askings of a question the same asking.
    //
    // This used to key on location.href, which meant the same question on the
    // Greenhouse posting and on the Lever form for one job were two separate
    // cache entries and two separate model calls — and any tracking parameter
    // on the URL missed the cache outright. The answer depends on the question
    // and on who is being applied to, so those are what it is filed under.
    function answerCacheKey(question, company) {
        const normalized = question
            .toLowerCase()
            .replace(/\s+/g, " ")
            .replace(/[^a-z0-9 ]/g, "")
            .trim()
            .slice(0, 120);

        return `ans_${(company || "unknown").toLowerCase()}_${normalized}`;
    }

    async function handleGenerate(textarea, button) {
        const question = questionFor(textarea);

        // Better to say the question could not be read than to send the model
        // a placeholder and return a paragraph about nothing in particular.
        if (!question) {
            finish(button, "❌ Could not read the question for this box", "#d9534f");
            return;
        }

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

        const job = await jobContext();
        const cacheKey = answerCacheKey(question, job.company);

        const cached = await chrome.storage.local.get(cacheKey);
        if (cached[cacheKey]) {
            applyAnswer(textarea, cached[cacheKey]);
            finish(button, "✅ Restored from cache", "#28a745");
            return;
        }

        button.textContent = "🤖 Generating…";

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

    // A job board is never the employer.
    //
    // og:site_name is the board naming *itself*, which is how the Indeed home
    // page — not a posting at all — was saved as an application at a company
    // called "Indeed". Mirrors ATS_DOMAINS and _OPAQUE_HOSTS in job_fields.py,
    // which rejects the same names server-side.
    //
    // Ambiguous single words a real employer might use — Shine, Dice, Monster,
    // Seek — are deliberately left out. Rejecting a genuine company is the worse
    // error, and the cost of letting a board name through here is one row the
    // user can correct, while the cost of rejecting one is an application that
    // cannot be saved at all.
    const JOB_BOARDS = new Set([
        "linkedin", "indeed", "naukri", "glassdoor", "ziprecruiter",
        "wellfound", "angellist", "instahyre", "cutshort", "hirist",
        "simplyhired", "careerbuilder", "internshala", "timesjobs",
        "greenhouse", "lever", "workday", "smartrecruiters", "icims", "taleo",
        "bamboohr", "ashby", "workable", "jobvite", "breezy", "recruitee",
        "teamtailor", "successfactors", "hackerrank"
    ]);

    // A company name is a name, not a sentence. Wellfound renders its listing
    // blurb inside a container the company selector matched, so the employer
    // came back as 20 words of marketing copy — "Talkdoc Actively Hiring
    // PROMOTED Affordable and Accessible Mental Healthcare from People Who…".
    // Real employers are short: "Saint-Gobain India Private Limited" is four.
    const MAX_COMPANY_WORDS = 8;

    function isJobBoard(value) {
        const key = clean(value)
            .toLowerCase()
            .replace(/\.(com|in|io|net|org|hr|co(\.[a-z]{2})?)\b/g, "")
            .replace(/\b(jobs?|careers?|india|inc|ltd|limited)\b/g, "")
            .replace(/[^a-z]/g, "");

        return JOB_BOARDS.has(key);
    }

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
    //
    // Every rejection here ends the same way: the popup asks the user to type
    // the employer. That is the point — an unanswered question is recoverable,
    // and a wrong company is a corrupt row that only surfaces weeks later as a
    // duplicate the emails cannot match.
    function acceptCompany(candidate, role) {
        const cleaned = clean(candidate);
        if (!cleaned || cleaned.length > 200) return "";
        if (isPlaceholder(cleaned)) return "";
        if (isJobBoard(cleaned)) return "";
        if (cleaned.split(/\s+/).length > MAX_COMPANY_WORDS) return "";
        if (role && cleaned.toLowerCase() === clean(role).toLowerCase()) return "";
        if (looksLikeRole(cleaned)) return "";
        return cleaned;
    }

    // --- Source 1: schema.org JobPosting ------------------------------------
    // Greenhouse, Lever, Ashby, Workable and most ATS-hosted career pages all
    // emit this. hiringOrganization.name is declared data rather than a
    // guess, which makes it immune to markup and title-format churn.
    //
    // The same block declares the salary and the location, which were being
    // parsed past and discarded. Reading them here rather than out of the
    // description prose is the entire reason they are trustworthy enough to
    // store: `baseSalary.value.minValue` is a stated number, whereas "competitive
    // package, £70k OTE" in a paragraph is a guess waiting to be wrong.
    function fromJsonLd() {
        const found = {
            company: "",
            role: "",
            jd_text: "",
            location: "",
            remote: false,
            salary_min: null,
            salary_max: null,
            salary_currency: "",
            salary_period: ""
        };

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

                Object.assign(found, salaryFromPosting(entry), locationFromPosting(entry));

                if (found.company || found.role) return found;
            }
        }

        return found;
    }

    // baseSalary is a MonetaryAmount whose `value` is either a QuantitativeValue
    // with min/max, or a bare number for a single figure. estimatedSalary is the
    // same shape and is what boards emit when the employer stated no range —
    // taken only as a fallback, because an estimate and a stated salary are not
    // the same claim.
    function salaryFromPosting(entry) {
        const amount = entry.baseSalary || entry.estimatedSalary;
        if (!amount) return {};

        const money = [].concat(amount)[0];
        if (!money) return {};

        const value = money.value ?? money;
        const scalar = typeof value === "object" ? value : { value };

        return {
            salary_min: firstNumber(scalar.minValue, scalar.value),
            salary_max: firstNumber(scalar.maxValue, scalar.value),
            // salaryCurrency is the older top-level spelling and still common.
            salary_currency: clean(
                money.currency || entry.salaryCurrency || scalar.currency || ""
            ).slice(0, 20),
            salary_period: clean(
                scalar.unitText || money.unitText || scalar.unitCode || ""
            ).slice(0, 20)
        };
    }

    // Anything that is not a usable number is left for the server to drop, but
    // there is no point sending a string that plainly is not one.
    function firstNumber(...values) {
        for (const value of values) {
            const number = typeof value === "string"
                ? Number(value.replace(/[,\s]/g, ""))
                : value;
            if (typeof number === "number" && isFinite(number) && number > 0) {
                return number;
            }
        }
        return null;
    }

    // jobLocation is a Place, or an array of them for a role open in several
    // offices. The country is included only when nothing more specific exists:
    // "Bengaluru, Karnataka" is a location, "Bengaluru, Karnataka, IN" is a
    // postal address.
    function locationFromPosting(entry) {
        const places = [].concat(entry.jobLocation || []).filter(Boolean);
        const named = places.map(placeToText).filter(Boolean);

        // TELECOMMUTE is the declared form. It can arrive as an array when a
        // role is remote *and* tied to an office.
        const remote = []
            .concat(entry.jobLocationType || [])
            .some((type) => /telecommute/i.test(String(type)));

        let location = named[0] || "";

        // A role open in five offices is worth recording as such; listing all
        // five is not, at the width this is displayed.
        if (named.length > 1) location += ` +${named.length - 1} more`;

        // For a fully remote role the only stated place is often the region a
        // candidate must be able to work in.
        if (!location && remote) {
            location = [].concat(entry.applicantLocationRequirements || [])
                .map((req) => clean(typeof req === "string" ? req : req?.name || ""))
                .filter(Boolean)[0] || "";
        }

        return { location: location.slice(0, 200), remote };
    }

    function placeToText(place) {
        if (typeof place === "string") return clean(place);

        const address = place.address;
        if (typeof address === "string") return clean(address);
        if (!address) return clean(place.name || "");

        const locality = clean(address.addressLocality || "");
        const region = clean(address.addressRegion || "");
        const country = clean(
            typeof address.addressCountry === "string"
                ? address.addressCountry
                : address.addressCountry?.name || ""
        );

        // Region is dropped when it merely repeats the city, which city-states
        // and single-city regions do constantly.
        const parts = [locality, region === locality ? "" : region].filter(Boolean);

        return parts.length ? parts.join(", ") : country;
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
    //
    // `location` is a fallback only: the JSON-LD block is preferred wherever it
    // exists, and these selectors cover the boards that render a location but
    // declare nothing — LinkedIn above all, which emits no JobPosting block.
    const ADAPTERS = [
        {
            match: /linkedin\.com$/,
            company: ".job-details-jobs-unified-top-card__company-name, .topcard__org-name-link, .pr2.t-14",
            role: ".job-details-jobs-unified-top-card__job-title, .topcard__title, h1",
            jd: ".jobs-description__content, #job-details, .description__text",
            location: ".job-details-jobs-unified-top-card__primary-description-container .tvm__text, .topcard__flavor--bullet",
            // "Role | Company | LinkedIn"
            titleCompany: (t) => t.split("|")[1] || ""
        },
        {
            match: /greenhouse\.io$/,
            company: '.company-name, [class*="companyName"], header img[alt]',
            role: ".app-title, .job__title h1, h1",
            jd: "#content, .job__description, #main, .accessible-wrapper",
            location: '.location, [class*="location"]',
            // "Job Application for <Role> at <Company>"
            titleCompany: (t) => t.split(/\bat\s+/i).slice(1).join(" at ") || ""
        },
        {
            match: /lever\.co$/,
            company: '.main-header-logo img[alt], [class*="companyName"]',
            role: ".posting-headline h2, h2",
            jd: ".posting-details, .section-wrapper, main",
            location: '.posting-categories .location, .sort-by-location',
            // "Company - Role"  (company first on Lever)
            titleCompany: (t) => t.split(" - ")[0] || ""
        },
        {
            match: /ashbyhq\.com$/,
            company: '[class*="companyName"], header img[alt]',
            role: 'h1, [class*="jobTitle"]',
            jd: '[class*="descriptionText"], main',
            location: '[class*="location"]',
            // "Role @ Company", and "Role - Company" on some boards: the
            // company is the LAST segment, never the first.
            titleCompany: (t) => t.split(/\s+[@|]\s+|\s+-\s+/).slice(1).pop() || ""
        },
        {
            match: /workable\.com$/,
            company: '[data-ui="company-name"], [class*="companyName"], header img[alt]',
            role: '[data-ui="job-title"], h1',
            jd: '[data-ui="job-description"], main',
            location: '[data-ui="job-location"], [class*="location"]',
            // "Role - Company"  (company LAST, the original bug)
            titleCompany: (t) => t.split(" - ").slice(1).join(" - ") || ""
        },
        {
            match: /(wellfound\.com|angel\.co)$/,
            // Precise first, and it now actually wins — see eachSelector. The
            // company link is the only element on a Wellfound listing that is
            // certain to be the employer and nothing else; `[class*="company"]`
            // matches the card wrapping the whole promo blurb.
            company: 'a[href^="/company/"], [class*="companyName"], [data-test*="company"] a',
            role: '[class*="jobTitle"], h1, h2',
            jd: '[class*="jobDescription"], [class*="description"]',
            location: '[class*="location"]',
            // "Role at Company"
            titleCompany: (t) => t.split(/\s+at\s+/i).slice(1).join(" at ") || ""
        },
        {
            // Indeed emits no JSON-LD JobPosting on most locales, and its search
            // results page renders a posting in a side panel — so the "page" is
            // not a posting at all. With none of these selectors present the
            // company falls through to og:site_name, which says "Indeed" and is
            // now rejected as a job board, leaving the popup to ask.
            match: /indeed\.(com|co\.[a-z]{2}|[a-z]{2})$/,
            company: '[data-testid="inlineHeader-companyName"], [data-company-name="true"], .jobsearch-CompanyInfoContainer a',
            role: '[data-testid="jobsearch-JobInfoHeader-title"], .jobsearch-JobInfoHeader-title, h1',
            jd: "#jobDescriptionText, .jobsearch-JobComponent-description",
            location: '[data-testid="inlineHeader-companyLocation"], [data-testid="job-location"], .jobsearch-JobInfoHeader-subtitle div:last-child',
            // "Role - Company - Job in City - Indeed.com"
            titleCompany: (t) => t.split(" - ")[1] || ""
        }
    ];

    // Fallback for any other origin the user enables on demand. Company career
    // pages overwhelmingly write "<Role> - <Company>" or "<Role> | <Company>",
    // so the LAST segment is the better guess — the opposite of the old code.
    const GENERIC_ADAPTER = {
        company: 'meta[property="og:site_name"], [class*="company-name"]',
        role: "h1",
        jd: null,
        location: null,
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
        let facts = EMPTY_FACTS;

        try {
            const structured = fromJsonLd();

            role = structured.role || text(adapter.role) || text("h1");
            jd_text = structured.jd_text || (adapter.jd ? text(adapter.jd) : "") || collectParagraphs();
            facts = postingFacts(structured, adapter);

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
            company_source: companySource,
            ...facts
        };
    }

    const EMPTY_FACTS = {
        location: "",
        remote: false,
        salary_min: null,
        salary_max: null,
        salary_currency: "",
        salary_period: ""
    };

    // Just the posting's facts out of a larger job object. Every save path sends
    // the same set, so a job saved from the card, the popup, or the offer after
    // a submission carries identical fields — one of them quietly sending fewer
    // would show up much later as rows that inexplicably have no salary.
    function postingFieldsOf(job) {
        const fields = {};
        Object.keys(EMPTY_FACTS).forEach((key) => {
            fields[key] = job?.[key] ?? EMPTY_FACTS[key];
        });
        return fields;
    }

    // Mirrors _REMOTE_WORDS in posting.py. The server splits place from
    // arrangement the same way, so the two can never end up disagreeing.
    const REMOTE_WORDS = /\b(remote|telecommute|work\s*from\s*home|wfh|distributed)\b/i;

    // The posting's own facts about itself: where the job is and what it pays.
    //
    // Only the JSON-LD block is trusted for the salary. A board that renders a
    // range in the page without declaring it is a board whose markup will move,
    // and a wrong salary is a number somebody makes a decision on — so an empty
    // field is the better failure. The location falls back to an adapter
    // selector, because getting a city wrong costs nothing like as much.
    function postingFacts(structured, adapter) {
        const facts = {
            location: clean(structured.location || ""),
            remote: Boolean(structured.remote),
            salary_min: structured.salary_min ?? null,
            salary_max: structured.salary_max ?? null,
            salary_currency: structured.salary_currency || "",
            salary_period: structured.salary_period || ""
        };

        if (!facts.location && adapter.location) {
            facts.location = clean(text(adapter.location));
        }

        if (REMOTE_WORDS.test(facts.location)) facts.remote = true;

        facts.location = facts.location.slice(0, 200);
        return facts;
    }

    // Selectors are tried in the order they are written, one at a time.
    //
    // Not the same thing as handing the whole list to querySelector, which
    // returns the first match in DOCUMENT order regardless of which selector
    // found it. Wellfound's adapter lists `a[href^="/company/"]` — exactly the
    // employer — after a loose `[class*="company"]`, and the loose one appears
    // higher in the page, so the precise selector never won and the company came
    // back as a paragraph of listing copy.
    //
    // Splitting on commas is safe for the selectors here; none use :is() or an
    // attribute value containing one.
    function eachSelector(list) {
        return String(list || "")
            .split(",")
            .map((part) => part.trim())
            .filter(Boolean);
    }

    function text(selectors) {
        for (const selector of eachSelector(selectors)) {
            const value = document.querySelector(selector)?.innerText;
            if (value && value.trim()) return value;
        }
        return "";
    }

    // Logos carry the company in alt text where no text node exists.
    function textOrAttr(selectors) {
        for (const selector of eachSelector(selectors)) {
            const el = document.querySelector(selector);
            if (!el) continue;

            const value = clean(el.innerText || el.getAttribute("alt") || el.content || "");
            if (value) return value;
        }
        return "";
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
                            ...postingFieldsOf(job),
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
                    ...postingFieldsOf(pending),
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
    // The match card, in the page
    //
    // The score used to exist only behind a click in the popup, which is one
    // click too many for the decision it informs: whether this posting is worth
    // reading at all. A number you have to ask for is a number you ask for
    // after you have already spent the attention.
    //
    // So the card scans on arrival — but only the free half. Keyword coverage
    // is pure string matching over a curated vocabulary, with no model call
    // behind it (see scoring.py), so showing it unprompted costs nothing. The
    // requirement-by-requirement analysis is a paid call and stays behind a
    // button. Same rule as the inbox filter: the cheap pass first, and the
    // expensive one only once it has earned the call.
    //
    // Rendered into a CLOSED shadow root. Two reasons, and the second is the
    // one that matters: a job board's stylesheet cannot reach in and wreck the
    // layout, and page scripts cannot read back out. What is on this card —
    // which skills the user lacks, how poorly they match — is the user's
    // business and not the employer's.
    // -----------------------------------------------------------------------
    const CARD_HOST_TAG = "talent-pilot-match";

    // Below this a page is a search-results list or a stub, not a posting worth
    // scoring. Scoring a fragment produces a confident number about nothing.
    const MIN_JD_CHARS = 400;

    // Re-extracting on every mutation of a job board's DOM is wasted work; the
    // posting does not change that often even when the markup does.
    const CARD_RESCAN_MS = 1500;

    // Terms are the whole point of the card, so more of them are shown here
    // than in the 330px popup — but a wall of forty is still nobody's idea of
    // information.
    const CARD_CHIP_PREVIEW = 12;

    let cardEnabled = true;
    let cardDismissed = false;
    let cardState = { signature: "", keywords: null, analysis: null, collapsed: false };
    let lastCardScanAt = 0;

    // A closed shadow root cannot be read back off the element, so the handle
    // has to be kept somewhere. A WeakMap rather than a property on the node:
    // the page shares the DOM with us and can read any property we set there,
    // which would hand back the very thing "closed" exists to withhold.
    const cardRoots = new WeakMap();

    // The card is opt-out rather than opt-in: the whole point is that it is
    // there before you ask. `chrome.storage.onChanged` is optional-called
    // because a test harness has no reason to stub an event it does not use.
    chrome.storage.local
        .get("inPageCard")
        .then((stored) => {
            cardEnabled = stored.inPageCard !== false;
            if (!cardEnabled) removeCard();
        })
        .catch(() => {});

    chrome.storage.onChanged?.addListener?.((changes, area) => {
        if (area !== "local") return;

        if (changes.inPageCard) {
            cardEnabled = changes.inPageCard.newValue !== false;
            if (cardEnabled) {
                // Turning it back on should not need a reload, and should not
                // wait out the rescan throttle either.
                cardDismissed = false;
                cardState.signature = "";
                lastCardScanAt = 0;
                scheduleScan();
            } else {
                removeCard();
            }
        }

        // A different resume is a different score, so the card is stale the
        // moment the popup's dropdown changes.
        if (changes.activeProfile) {
            cardState = { signature: "", keywords: null, analysis: null, collapsed: false };
            lastCardScanAt = 0;
            scheduleScan();
        }
    });

    function cardHost() {
        return document.querySelector(CARD_HOST_TAG);
    }

    function removeCard() {
        cardHost()?.remove();
    }

    // Anchors to try on a site with no adapter of its own — a company careers
    // page the user enabled on demand. These are the selectors the major ATS
    // templates share, which is most of what such a page is built from.
    const CARD_FALLBACK_ANCHORS = [
        '[data-ui="job-description"]',
        ".jobs-description__content",
        "#job-details",
        ".job__description",
        '[class*="descriptionText"]',
        ".posting-details",
    ];

    // Where the card goes. Above the job description it reads as part of the
    // posting, which is where the reader already is. A fixed corner is the
    // fallback for pages whose description cannot be located — a card nobody
    // can find is the same as no card, but a card wedged into the wrong place
    // is worse than one in the corner, so the guesses stay conservative.
    function insertCard(host) {
        const adapter = adapterFor(location.hostname);
        const selectors = [adapter.jd, ...CARD_FALLBACK_ANCHORS].filter(Boolean);

        for (const selector of selectors) {
            const anchor = document.querySelector(selector);
            if (anchor?.parentElement) {
                anchor.insertAdjacentElement("beforebegin", host);
                return "inline";
            }
        }

        document.body.appendChild(host);
        return "floating";
    }

    async function maybeRenderCard() {
        if (!cardEnabled || cardDismissed) return;
        // One card per page, not one per frame: an embedded application form
        // would otherwise raise a second copy inside the first.
        if (window.top !== window) return;
        // Scanning needs a signed-in session and a resume behind it. A card
        // whose only possible content is an error message is worse than none.
        if (ruleState !== "ready") return;

        // Throttled whether or not a card is up. extractJobData() parses the
        // JSON-LD blocks and walks the paragraphs, and a job board mutates its
        // own DOM continuously — so without this the work happens on every
        // debounce tick on exactly the busiest pages.
        const now = Date.now();
        if (now - lastCardScanAt < CARD_RESCAN_MS) return;
        lastCardScanAt = now;

        const present = cardHost();

        const job = extractJobData();

        if (!job.jd_text || job.jd_text.length < MIN_JD_CHARS) return;
        if (!job.company && !job.role) return;

        const signature = `${job.company}|${job.role}|${job.jd_text.length}`;

        // Same posting, card already up: nothing to do. This is what stops the
        // MutationObserver — which our own insertion triggers — from looping.
        if (present && signature === cardState.signature) return;

        // A single-page navigation to a different posting. The previous
        // posting's numbers must not survive into it.
        if (signature !== cardState.signature) {
            cardState = { signature, keywords: null, analysis: null, collapsed: false };
        }

        renderCard(job, { scanning: true });

        const { activeProfile } = await chrome.storage.local.get("activeProfile");
        const response = await chrome.runtime.sendMessage({
            type: "KEYWORD_SCAN",
            jd_text: job.jd_text,
            profile: activeProfile || null
        });

        // The page may have navigated on while the scan was in flight.
        if (cardState.signature !== signature) return;

        cardState.keywords = response?.ok ? response.data : null;
        renderCard(job, { error: response?.ok ? "" : response?.error || "" });
    }

    function renderCard(job, { scanning = false, error = "" } = {}) {
        let host = cardHost();
        let root;

        if (host) {
            root = cardRoots.get(host);
        } else {
            host = document.createElement(CARD_HOST_TAG);
            // Closed: the page cannot reach the contents through
            // element.shadowRoot. See the section comment.
            root = host.attachShadow({ mode: "closed" });
            cardRoots.set(host, root);

            const placement = insertCard(host);
            root.append(buildCardStyles(placement));
        }

        if (!root) return;

        root.querySelector(".tp-card")?.remove();
        root.append(buildCard(job, { scanning, error }));
    }

    function buildCardStyles(placement) {
        const style = document.createElement("style");

        // `all: initial` on the host, so nothing the page declares inherits in.
        style.textContent = `
            :host {
                all: initial;
                display: block;
                ${placement === "floating"
                    ? `position: fixed; right: 16px; bottom: 16px;
                       width: min(370px, calc(100vw - 32px));
                       z-index: 2147483646;`
                    : "margin: 12px 0 16px;"}

                /* The same palette as the popup, which cannot be shared as a
                   stylesheet: this card lives in a shadow root with
                   \`all: initial\`, deliberately cut off from everything. The
                   two are kept in step by hand — a colour changed in
                   popup.html belongs here too.

                   \`all\` does not reset custom properties, so these survive
                   the reset above. */
                --surface: #ffffff;
                --surface-alt: #eef0f4;
                --track: #e6e9ef;
                --border: #dfe3ea;
                --hairline: #edf0f4;
                --text: #101828;
                --text-soft: #344054;
                --muted: #667085;
                --brand: #2563eb;
                --brand-hover: #1d4ed8;
                --on-brand: #ffffff;
                --good-bg: #ecfdf3;
                --good-fg: #15803d;
                --mid-bg: #fffaeb;
                --mid-fg: #b45309;
                --low-bg: #fef3f2;
                --low-fg: #b42318;
                --term-bg: #eff4ff;
                --term-border: #c7d7fe;
                --term-fg: #3538cd;
                --shadow: 0 4px 16px rgba(16, 24, 40, 0.10), 0 1px 3px rgba(16, 24, 40, 0.06);
            }

            /* Not a near-black panel. This card sits on somebody else's page,
               and a darker rectangle than anything around it reads as a
               foreign object rather than part of the posting. */
            @media (prefers-color-scheme: dark) {
                :host {
                    --surface: #21252d;
                    --surface-alt: #2a2f39;
                    --track: #333945;
                    --border: #343b46;
                    --hairline: #2b313b;
                    --text: #e8ecf1;
                    --text-soft: #cdd5df;
                    --muted: #98a2b3;
                    --brand: #7aa5f7;
                    --brand-hover: #9cbcfa;
                    --on-brand: #0b1220;
                    --good-bg: #10291b;
                    --good-fg: #75e0a7;
                    --mid-bg: #2e2410;
                    --mid-fg: #fcd34d;
                    --low-bg: #35201f;
                    --low-fg: #fda29b;
                    --term-bg: #1c2740;
                    --term-border: #2f4272;
                    --term-fg: #a4bcfd;
                    --shadow: 0 4px 20px rgba(0, 0, 0, 0.45);
                }
            }

            * { box-sizing: border-box; }
            .tp-card {
                font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
                font-size: 13px;
                line-height: 1.5;
                color: var(--text);
                background: var(--surface);
                border: 1px solid var(--border);
                /* The band colour, set from JS. A top edge rather than a left
                   one: in the in-page placement the card is full width, and a
                   4px left rule on a wide card reads as a quote block. */
                border-top: 3px solid var(--tp-accent, var(--brand));
                border-radius: 12px;
                box-shadow: var(--shadow);
                overflow: hidden;
            }
            .tp-head {
                display: flex;
                align-items: center;
                gap: 12px;
                padding: 12px 12px 10px;
            }
            .tp-ring {
                position: relative;
                flex: 0 0 auto;
                width: 56px;
                height: 56px;
                border-radius: 50%;
                background: conic-gradient(
                    var(--tp-accent, var(--brand)) calc(var(--tp-pct, 0) * 1%),
                    var(--track) 0
                );
                display: grid;
                place-items: center;
            }
            .tp-ring::after {
                content: "";
                position: absolute;
                inset: 6px;
                border-radius: 50%;
                background: var(--surface);
            }
            .tp-ring span {
                position: relative;
                /* Above the ::after that punches out the middle of the dial.
                   Both are positioned, so without this the generated content
                   paints last and the number sits behind the hole — which is
                   where it had been since the dial was introduced. */
                z-index: 1;
                font-size: 15px;
                font-weight: 700;
                letter-spacing: -0.02em;
                color: var(--tp-accent, var(--brand));
            }
            .tp-title { flex: 1 1 auto; min-width: 0; }
            .tp-title b { font-size: 13px; font-weight: 650; letter-spacing: -0.01em; }
            .tp-sub { color: var(--muted); font-size: 12px; }
            .tp-facts { font-weight: 600; color: var(--text-soft); font-size: 12px; }
            .tp-actions { display: flex; gap: 2px; flex: 0 0 auto; }
            .tp-icon {
                all: unset;
                cursor: pointer;
                color: var(--muted);
                font-size: 15px;
                line-height: 1;
                padding: 5px 7px;
                border-radius: 7px;
            }
            .tp-icon:hover { background: var(--surface-alt); color: var(--text); }
            .tp-body { padding: 0 12px 12px; }
            .tp-row { display: flex; gap: 8px; margin-bottom: 12px; }
            .tp-btn {
                all: unset;
                flex: 1 1 auto;
                text-align: center;
                cursor: pointer;
                padding: 8px 10px;
                border-radius: 8px;
                font-size: 12px;
                font-weight: 600;
                font-family: inherit;
            }
            .tp-btn-primary { background: var(--brand); color: var(--on-brand); }
            .tp-btn-primary:hover { background: var(--brand-hover); }
            .tp-btn-quiet {
                background: var(--surface-alt);
                color: var(--text-soft);
                box-shadow: inset 0 0 0 1px var(--border);
            }
            .tp-btn-quiet:hover { background: var(--track); }
            .tp-btn[disabled] { opacity: 0.55; cursor: default; }
            .tp-section { font-size: 12px; font-weight: 700; margin: 12px 0 2px; }
            .tp-hint { color: var(--muted); font-size: 11px; margin: 0 0 7px; }
            .tp-chips { display: flex; flex-wrap: wrap; gap: 5px; }
            .tp-chip {
                font-size: 11px;
                line-height: 1.6;
                padding: 2px 9px;
                border-radius: 999px;
                border: 1px solid var(--term-border);
                background: var(--term-bg);
                color: var(--term-fg);
                font-weight: 500;
            }
            .tp-chip-have {
                border-color: var(--good-bg);
                background: var(--good-bg);
                color: var(--good-fg);
            }
            .tp-chip-more {
                background: transparent;
                border-style: dashed;
                border-color: var(--border);
                color: var(--muted);
                cursor: pointer;
                font-weight: 600;
            }
            .tp-chip-more:hover { color: var(--brand); border-color: var(--brand); }
            .tp-gaps { margin: 0; padding-left: 18px; }
            .tp-gaps li { font-size: 12px; color: var(--text-soft); margin-bottom: 3px; }
            .tp-verdict {
                font-size: 12px;
                font-weight: 600;
                padding: 8px 11px;
                border-radius: 9px;
                margin-bottom: 10px;
                line-height: 1.45;
            }
            .tp-good { background: var(--good-bg); color: var(--good-fg); }
            .tp-mid  { background: var(--mid-bg); color: var(--mid-fg); }
            .tp-low  { background: var(--low-bg); color: var(--low-fg); }
            .tp-note { font-size: 11px; color: var(--muted); }
            .tp-warn { font-size: 11px; color: var(--low-fg); }
            .tp-summary { font-size: 12px; color: var(--text-soft); margin-top: 10px; line-height: 1.55; }

            ${placement === "floating" ? `
                /* The floating card is pinned to the viewport, so unlike the
                   in-page placement it cannot rely on the posting's own
                   scrollbar. A full AI match — verdict, competencies, the
                   reasoning paragraph and two chip lists — is taller than a
                   laptop screen, and everything past the bottom edge used to
                   be reachable only by zooming the whole page out. Cap the
                   card at the viewport instead and let the body scroll under
                   a head that stays put, so the score and the buttons are
                   always on screen. */
                .tp-card {
                    display: flex;
                    flex-direction: column;
                    max-height: calc(100vh - 32px);
                }
                .tp-head { flex: 0 0 auto; }
                .tp-body {
                    overflow-y: auto;
                    /* Hitting the end of the list must not hand the scroll on
                       to the posting underneath. */
                    overscroll-behavior: contain;
                    scrollbar-width: thin;
                    scrollbar-color: var(--track) transparent;
                }
                .tp-body::-webkit-scrollbar { width: 8px; }
                .tp-body::-webkit-scrollbar-track { background: transparent; }
                .tp-body::-webkit-scrollbar-thumb {
                    background: var(--track);
                    border-radius: 999px;
                }
            ` : ""}
        `;

        return style;
    }

    // "Remote · Bengaluru · INR 1,800,000–2,400,000/yr", or as much of it as the
    // posting declared. Mirrors format_salary / format_location in posting.py —
    // the dashboard renders the stored columns and this renders what was just
    // scraped, so the two must agree on how a salary reads.
    const PERIOD_SUFFIX = {
        HOUR: "/hr", DAY: "/day", WEEK: "/wk", MONTH: "/mo", YEAR: "/yr"
    };

    // Grouped by hand rather than with toLocaleString, which follows the
    // browser's locale: the same salary would read "1,800,000" in one browser
    // and "18,00,000" in another, and neither would match the dashboard, which
    // has no locale to follow. One number, one spelling, everywhere.
    function groupDigits(value) {
        return String(Math.round(value)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    }

    // Mirrors ui.NOT_STATED. Both halves are always rendered, with NA where the
    // posting said nothing: a missing line cannot be told apart from a broken
    // one, and "this posting does not state a salary" is itself worth knowing
    // before applying. The emoji labels are what make a bare NA mean something —
    // it answers a question the reader can see.
    const NOT_STATED = "NA";

    function describeFacts(job) {
        const where = [job.remote ? "Remote" : "", job.location].filter(Boolean).join(" · ");

        const low = job.salary_min;
        const high = job.salary_max ?? job.salary_min;
        let pay = "";

        if (typeof low === "number" && low > 0) {
            const currency = (job.salary_currency || "").trim();
            const period = PERIOD_SUFFIX[(job.salary_period || "").toUpperCase()] || "";
            const range = low === high
                ? groupDigits(low)
                : `${groupDigits(low)}–${groupDigits(high)}`;
            pay = `${currency ? currency + " " : ""}${range}${period}`;
        }

        return `📍 ${where || NOT_STATED}  ·  💰 ${pay || NOT_STATED}`;
    }

    // Bands are shared with the popup on purpose: one number must not mean
    // "strong" in one surface and "weak" in the other.
    function bandFor(value) {
        if (value >= 75) return { klass: "tp-good", colour: "#1e7e34" };
        if (value >= 50) return { klass: "tp-mid", colour: "#b45309" };
        return { klass: "tp-low", colour: "#c9302c" };
    }

    function buildCard(job, { scanning, error }) {
        const card = document.createElement("div");
        card.className = "tp-card";

        const keywords = cardState.keywords;
        const analysis = cardState.analysis;

        // The headline number is the AI fit once it exists, and the free
        // keyword score until then — in that order, because the fit is the
        // better answer and the keyword score is the one available instantly.
        const headline = analysis
            ? analysis.match_percentage
            : keywords?.scored
            ? keywords.score
            : null;

        const band = bandFor(headline ?? 0);
        card.style.setProperty("--tp-accent", headline === null ? "#6b7280" : band.colour);

        card.append(buildCardHead(job, { headline, scanning, analysis, keywords }));

        if (!cardState.collapsed) {
            card.append(buildCardBody(job, { scanning, error, analysis, keywords }));
        }

        return card;
    }

    function buildCardHead(job, { headline, scanning, analysis, keywords }) {
        const head = document.createElement("div");
        head.className = "tp-head";

        const ring = document.createElement("div");
        ring.className = "tp-ring";
        ring.style.setProperty("--tp-pct", String(headline ?? 0));

        const value = document.createElement("span");
        value.textContent = headline === null ? "–" : `${headline}%`;
        ring.append(value);

        const title = document.createElement("div");
        title.className = "tp-title";

        const heading = document.createElement("b");
        heading.textContent = analysis ? "Resume match" : "Keyword match";

        const sub = document.createElement("div");
        sub.className = "tp-sub";

        if (scanning && !keywords) {
            sub.textContent = "Checking your resume…";
        } else if (analysis) {
            const coverage = analysis.coverage || {};
            sub.textContent = coverage.required_total
                ? `${coverage.required_met} of ${coverage.required_total} must-haves met`
                : "Scored against this posting's requirements";
        } else if (keywords?.scored) {
            sub.textContent =
                `${keywords.matched.length} of ${keywords.total} keywords are in your resume`;
        } else {
            sub.textContent = "No known skill terms in this posting";
        }

        title.append(heading, sub);

        // What the posting says about itself, on its own line. This is the fact
        // a reader wants before any score — and until now it was being parsed
        // out of the JSON-LD block and thrown away.
        const line = document.createElement("div");
        line.className = "tp-sub tp-facts";
        line.textContent = describeFacts(job);
        title.append(line);

        const actions = document.createElement("div");
        actions.className = "tp-actions";

        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "tp-icon";
        toggle.textContent = cardState.collapsed ? "▾" : "▴";
        toggle.title = cardState.collapsed ? "Expand" : "Collapse";
        toggle.addEventListener("click", () => {
            cardState.collapsed = !cardState.collapsed;
            renderCard(job);
        });

        const close = document.createElement("button");
        close.type = "button";
        close.className = "tp-icon";
        close.textContent = "✕";
        close.title = "Hide on this page";
        close.addEventListener("click", () => {
            // This page only. Turning the card off everywhere is a setting, and
            // a close button that silently disables a feature is a trap.
            cardDismissed = true;
            removeCard();
        });

        actions.append(toggle, close);
        head.append(ring, title, actions);

        return head;
    }

    function buildCardBody(job, { scanning, error, analysis, keywords }) {
        const body = document.createElement("div");
        body.className = "tp-body";

        const row = document.createElement("div");
        row.className = "tp-row";

        const analyse = document.createElement("button");
        analyse.type = "button";
        analyse.className = "tp-btn tp-btn-primary";
        analyse.textContent = analysis ? "↻ Re-run AI match" : "🧠 Full AI match";
        analyse.disabled = scanning;
        analyse.addEventListener("click", () => runCardAnalysis(job, analyse));

        const save = document.createElement("button");
        save.type = "button";
        save.className = "tp-btn tp-btn-quiet";
        save.textContent = "💾 Save";
        save.addEventListener("click", () => saveFromCard(job, save));

        row.append(analyse, save);
        body.append(row);

        // Reported above the results and below the buttons, so a quota failure
        // or a sleeping service worker leaves the retry one click away. An
        // error that replaces the whole card is an error you cannot act on.
        if (error) {
            const warning = document.createElement("div");
            warning.className = "tp-warn";
            warning.textContent = error;
            body.append(warning);
        }

        if (analysis) {
            const verdict = document.createElement("div");
            const band = bandFor(analysis.match_percentage);
            verdict.className = `tp-verdict ${band.klass}`;
            verdict.textContent =
                analysis.match_percentage >= 75
                    ? "✅ Strong fit — apply"
                    : analysis.match_percentage >= 50
                    ? "🟡 Close fit — worth tailoring"
                    : "🔴 Weak fit — a stretch on the must-haves";
            body.append(verdict);

            // A percentage over three requirements is arithmetic, not a
            // measurement. Saying so beats presenting 33% as a judgement.
            if (analysis.coverage?.thin) {
                const caution = document.createElement("div");
                caution.className = "tp-hint";
                caution.textContent =
                    "This posting does not state enough to score against — the percentage is not meaningful here.";
                body.append(caution);
            }

            if (analysis.missing_skills?.length) {
                body.append(
                    cardSection(
                        `⚠️ Gaps a recruiter would probe (${analysis.missing_skills.length})`,
                        "Requirements your resume does not evidence.",
                        cardGapList(analysis.missing_skills)
                    )
                );
            }

            // Stated by the posting, kept out of the score and out of the
            // gap list above: an employer's own internal programme is not
            // something a resume can evidence or an applicant can go and
            // acquire. Naming it is still useful; calling it a gap was not.
            const notScored = analysis.coverage?.not_scored || [];
            const internal = notScored.filter((e) => e.kind === "employer_internal");
            const behavioural = notScored.filter((e) => e.kind === "meta");

            if (internal.length) {
                body.append(
                    cardSection(
                        `ℹ️ Internal to this employer (${internal.length})`,
                        "Nothing a resume can evidence from outside. Not counted either way.",
                        cardGapList(internal.map((entry) => entry.skill))
                    )
                );
            }

            // Competency wording — "business acumen", "risk and controls" —
            // which every posting this employer writes carries verbatim and no
            // resume is phrased in. Scoring it measured how little a CV reads
            // like an HR framework.
            if (behavioural.length) {
                body.append(
                    cardSection(
                        `🗣️ They will also assess (${behavioural.length})`,
                        "Competency wording, not skills. Not scored — worth reading before an interview.",
                        cardGapList(behavioural.map((entry) => entry.skill))
                    )
                );
            }

            if (analysis.summary) {
                const summary = document.createElement("div");
                summary.className = "tp-summary";
                summary.textContent = analysis.summary;
                body.append(summary);
            }
        }

        // The keyword lists stay visible after the AI run: they answer a
        // different question — whether a filter surfaces you at all — and the
        // literal terms are the actionable half.
        if (keywords?.missing?.length) {
            body.append(
                cardSection(
                    `🔍 Terms this posting uses that you don't (${keywords.missing.length})`,
                    "A filter matches text, not meaning. Add only what you can genuinely claim.",
                    cardChips(keywords.missing, "")
                )
            );
        }

        if (keywords?.matched?.length) {
            body.append(
                cardSection(
                    `✅ Terms you already have (${keywords.matched.length})`,
                    "",
                    cardChips(keywords.matched, "tp-chip-have")
                )
            );
        }

        return body;
    }

    function cardSection(title, hint, content) {
        const fragment = document.createDocumentFragment();

        const heading = document.createElement("div");
        heading.className = "tp-section";
        heading.textContent = title;
        fragment.append(heading);

        if (hint) {
            const explanation = document.createElement("p");
            explanation.className = "tp-hint";
            explanation.textContent = hint;
            fragment.append(explanation);
        }

        fragment.append(content);
        return fragment;
    }

    function cardChips(items, extraClass) {
        const wrap = document.createElement("div");
        wrap.className = "tp-chips";

        const visible = items.slice(0, CARD_CHIP_PREVIEW);
        const overflow = items.slice(CARD_CHIP_PREVIEW);

        const addChip = (item) => {
            const chip = document.createElement("span");
            chip.className = `tp-chip ${extraClass}`.trim();
            // Model- and page-derived text, so a text node and never markup.
            chip.textContent = item;
            wrap.append(chip);
        };

        visible.forEach(addChip);

        if (overflow.length) {
            const more = document.createElement("span");
            more.className = "tp-chip tp-chip-more";
            more.textContent = `+${overflow.length} more`;
            more.addEventListener("click", () => {
                more.remove();
                overflow.forEach(addChip);
            });
            wrap.append(more);
        }

        return wrap;
    }

    function cardGapList(items) {
        const list = document.createElement("ul");
        list.className = "tp-gaps";

        items.forEach((item) => {
            const row = document.createElement("li");
            row.textContent = item;
            list.append(row);
        });

        return list;
    }

    async function runCardAnalysis(job, button) {
        button.disabled = true;
        button.textContent = "🤖 Analysing…";

        const { activeProfile } = await chrome.storage.local.get("activeProfile");
        const response = await chrome.runtime.sendMessage({
            type: "ANALYZE_JOB",
            job: {
                company: job.company || "Unknown Company",
                role: job.role || "Unknown Role",
                jd_text: job.jd_text,
                link: location.href,
                profile: activeProfile || null
            }
        });

        if (!response?.ok) {
            renderCard(job, { error: response?.error || "The analysis could not be run." });
            return;
        }

        cardState.analysis = response.data;
        renderCard(job);
    }

    async function saveFromCard(job, button) {
        button.disabled = true;
        button.textContent = "Saving…";

        // Asked rather than assumed, and by the same rule the popup uses: a
        // wrong company splits one application into two rows that never
        // reconcile, so an unnamed employer is a reason to stop.
        if (!job.company) {
            button.textContent = "Company not detected — use the popup";
            return;
        }

        const check = await chrome.runtime.sendMessage({
            type: "CHECK_JOB",
            company: job.company,
            role: job.role || "Unknown Role"
        });

        if (check?.ok && check.data.exists) {
            button.textContent = `Already tracked · ${check.data.status}`;
            return;
        }

        const { activeProfile } = await chrome.storage.local.get("activeProfile");
        const response = await chrome.runtime.sendMessage({
            type: "SAVE_JOB",
            job: {
                company: job.company,
                role: job.role || "Unknown Role",
                jd_text: job.jd_text,
                link: location.href,
                ...postingFieldsOf(job),
                profile: activeProfile || null
            }
        });

        button.textContent = response?.ok ? "✅ Saved" : "Could not save";
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
                // click from being filled in, and the card would go on showing
                // the previous account's score.
                if (ruleState !== "ready") {
                    removeSuggestions();
                    removeCard();
                    cardState = {
                        signature: "",
                        keywords: null,
                        analysis: null,
                        collapsed: false,
                    };
                } else if (!autofillRules.length) {
                    removeSuggestions();
                }
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
            // Deliberately not awaited. The card is an enhancement; a failure
            // inside it must not stop the form suggestions above from running.
            maybeRenderCard().catch(() => {});
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
