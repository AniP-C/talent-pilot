// extension/content.js
//
// Runs in the page to (a) read job details and (b) offer AI drafting on
// free-text answer boxes. It never talks to the API directly and never holds
// the auth token — all network work is delegated to background.js.

(() => {
    "use strict";

    // -----------------------------------------------------------------------
    // Passive mode: annotate known questions with a saved suggestion
    // -----------------------------------------------------------------------
    function scanAndSuggest() {
        if (typeof AUTOFILL_RULES === "undefined") return;

        document.querySelectorAll("label, h3, span").forEach((el) => {
            if (el.dataset.aiScanned || el.classList.contains("ai-copilot-suggestion")) return;
            if (el.closest("[data-ai-scanned]")) return;

            const text = el.innerText;
            if (!text || text.length > 300) return;

            const rule = AUTOFILL_RULES.find((candidate) => candidate.pattern.test(text));
            if (!rule) return;

            el.insertAdjacentElement("afterend", buildSuggestion(rule.suggestion));
            el.dataset.aiScanned = "true";
        });
    }

    function buildSuggestion(text) {
        const box = document.createElement("div");
        box.className = "ai-copilot-suggestion";

        const label = document.createElement("b");
        label.textContent = "💡 Suggestion: ";

        // textContent, not innerHTML — rule values are user-editable and must
        // never be parsed as markup.
        box.append(label, document.createTextNode(text));

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
            boxShadow: "0 2px 4px rgba(0,0,0,0.05)"
        });

        return box;
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

        applyAnswer(textarea, response.data.suggested_answer);
        chrome.storage.local.set({ [cacheKey]: response.data.suggested_answer });
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
    // Domain router: pull company/role/JD out of the page
    // -----------------------------------------------------------------------
    function extractJobData() {
        const domain = location.hostname;
        let company = "";
        let role = "";
        let jd_text = "";

        try {
            if (domain.includes("linkedin.com")) {
                company = text(".job-details-jobs-unified-top-card__company-name") || text(".pr2.t-14");
                role = text(".job-details-jobs-unified-top-card__job-title") || text("h1");
                jd_text = text(".jobs-description__content, #job-details");
            } else if (domain.includes("greenhouse.io")) {
                role = text(".app-title h1, h1");
                company = text(".company-name, .logo-container").replace(/^at\s+/i, "") ||
                    document.title.split(" - ")[0];
                jd_text = text("#content, #main, .accessible-wrapper");
            } else if (domain.includes("lever.co")) {
                role = text(".posting-headline h2");
                company = document.title.split("-")[0];
                jd_text = text(".posting-details") || text(".section-wrapper");
            } else if (domain.includes("ashbyhq.com")) {
                role = text("h1");
                company = document.title.split(" @ ")[1] || document.title.split(" - ")[0];
                jd_text = text('[class*="descriptionText"]') || text("main");
            } else if (domain.includes("workable.com")) {
                role = text("h1");
                company = text('[data-ui="company-name"]') || document.title.split(" - ")[0];
                jd_text = text("main") || text('[data-ui="job-description"]');
            } else if (domain.includes("wellfound.com") || domain.includes("angel.co")) {
                role = text("h2") || text("h1");
                company = text("h1");
                if (!company || company === role) {
                    company = document.title.split(" at ")[1] || document.title.split(" | ")[0];
                }
                jd_text = collectParagraphs();
            } else {
                role = text("h1");
                company = document.title.split(/[-|]/)[0];
                jd_text = collectParagraphs();
            }
        } catch (err) {
            console.error("[Job Copilot] extraction failed:", err);
        }

        return {
            company: clean(company) || "Unknown Company",
            role: clean(role) || "Unknown Role",
            jd_text: clean(jd_text).slice(0, 8000),
            link: location.href
        };
    }

    function text(selector) {
        return document.querySelector(selector)?.innerText || "";
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

    scheduleScan();
})();
