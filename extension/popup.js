// extension/popup.js
//
// UI only. Every network call is delegated to background.js, which owns the
// auth token. Scraped page values are rendered with textContent, never
// innerHTML — a job posting is untrusted input and could otherwise inject
// markup into this privileged page.

const el = (id) => document.getElementById(id);
const send = (message) => chrome.runtime.sendMessage(message);

let currentJob = null;
// The analysis showing in the popup, kept so that saving the job can carry it
// with it. The usual order is score the posting, then decide to save it — and
// the server has no row to attach the result to until that second step, so
// without this the scores on screen are thrown away and the next visit pays
// for them again.
let currentAnalysis = null;
let currentTab = null;
let authMode = "signin";
// Populated from /health so the invite field appears only where it applies.
let registrationPolicy = { open: true, invite_required: false };
// Where the dashboard lives, reported by the server it is paired with.
let dashboardUrl = "http://localhost:8501";

// ---------------------------------------------------------------------------
// Rendering helpers
// ---------------------------------------------------------------------------
function setStatus(text, className = "") {
    const area = el("status-area");
    area.textContent = "";

    if (!text) return;

    const span = document.createElement("span");
    if (className) span.className = className;
    span.textContent = text;
    area.appendChild(span);
}

function show(id, visible = true) {
    el(id).classList.toggle("hidden", !visible);
}

// "Remote · Bengaluru · INR 1,800,000–2,400,000/yr", or as much of it as the
// posting declared. Mirrors describeFacts in content.js and format_salary in
// posting.py; all three render the same fields and have to read alike.
const PERIOD_SUFFIX = {
    HOUR: "/hr", DAY: "/day", WEEK: "/wk", MONTH: "/mo", YEAR: "/yr"
};

// Grouped by hand rather than with toLocaleString, which follows the browser's
// locale: the same salary would read "1,800,000" in one browser and "18,00,000"
// in another, and neither would match the dashboard, which has no locale to
// follow. One number, one spelling, everywhere.
function groupDigits(value) {
    return String(Math.round(value)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

// Mirrors ui.NOT_STATED. Both halves are always rendered, with NA where the
// posting said nothing — a missing line cannot be told apart from a broken one.
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

function renderJobInfo(job) {
    const box = el("job-info");
    box.textContent = "";

    const company = document.createElement("b");
    company.textContent = job.company || "Company not detected";
    if (!job.company) company.className = "warning";

    const role = document.createElement("div");
    role.className = "muted";
    role.textContent = job.role || "Role not detected";

    box.append(company, role);

    const line = document.createElement("div");
    line.className = "job-facts";
    line.textContent = describeFacts(job);
    box.append(line);
}

// How many chips to show before collapsing the rest behind a count. Nineteen
// comma-separated terms in a 330px popup is a wall nobody reads; eight is
// about a glance.
const CHIP_PREVIEW = 8;

function scoreBand(value) {
    if (value >= 75) return "score-good";
    if (value >= 50) return "score-mid";
    return "score-low";
}

function buildScoreCard(value, label, sub) {
    const card = document.createElement("div");
    card.className = "score-card";

    // A dial, and the same dial the in-page card draws. The shape is read
    // before the digits are, and the two surfaces have to agree on what a
    // given number looks like or they read as two different measurements.
    const ring = document.createElement("div");
    ring.className = `score-ring ${scoreBand(value)}`;
    ring.style.setProperty("--pct", String(Math.max(0, Math.min(100, value))));

    const number = document.createElement("div");
    number.className = "score-value";
    number.textContent = `${value}%`;
    ring.append(number);

    const name = document.createElement("div");
    name.className = "score-label";
    name.textContent = label;

    card.append(ring, name);

    // Each fact on its own line. Joined with a separator they wrapped
    // mid-phrase at this width, which reads as a mistake rather than a detail.
    [].concat(sub || []).filter(Boolean).forEach((line) => {
        const detail = document.createElement("div");
        detail.className = "score-sub";
        detail.textContent = line;
        card.append(detail);
    });

    return card;
}

// One line saying what the number means, so the score does not have to be
// interpreted from scratch every time.
function buildVerdict(fit, keywordScore) {
    const verdict = document.createElement("div");

    let band;
    let text;

    if (fit >= 75) {
        band = "verdict-good";
        text = "✅ Strong fit — apply";
    } else if (fit >= 50) {
        band = "verdict-mid";
        text = "🟡 Close fit — worth tailoring";
    } else {
        band = "verdict-low";
        text = "🔴 Weak fit — a stretch on the must-haves";
    }

    verdict.className = `verdict ${band}`;
    verdict.textContent = text;

    // A good candidate filtered out before a person ever sees them is a
    // different problem from a weak candidate, so it gets its own line rather
    // than being appended into the same sentence.
    if (fit >= 50 && keywordScore !== null && keywordScore < 40) {
        const note = document.createElement("span");
        note.className = "verdict-note";
        note.textContent = "A keyword filter may screen you out first.";
        verdict.append(note);
    }

    return verdict;
}

// A wrapping row of chips, with anything past the preview hidden behind a
// "+N more" that reveals the rest in place.
function buildChips(items, className) {
    const row = document.createElement("div");
    row.className = "chips";

    const visible = items.slice(0, CHIP_PREVIEW);
    const overflow = items.slice(CHIP_PREVIEW);

    // Scraped and model-produced values, so text nodes only — never innerHTML.
    visible.forEach((item) => {
        const chip = document.createElement("span");
        chip.className = `chip ${className}`;
        chip.textContent = item;
        row.append(chip);
    });

    if (overflow.length) {
        const more = document.createElement("span");
        more.className = "chip chip-more";
        more.textContent = `+${overflow.length} more`;

        more.addEventListener("click", () => {
            more.remove();
            overflow.forEach((item) => {
                const chip = document.createElement("span");
                chip.className = `chip ${className}`;
                chip.textContent = item;
                row.append(chip);
            });
        });

        row.append(more);
    }

    return row;
}

// Phrases like "Security and Compliance best practices" do not fit in a pill.
// A list gives them room and stays readable at this width.
function buildGapList(items) {
    const list = document.createElement("ul");
    list.className = "gap-list";

    items.forEach((item) => {
        const row = document.createElement("li");
        row.textContent = item;
        list.append(row);
    });

    return list;
}

function buildSection(title, hint, body) {
    const fragment = document.createDocumentFragment();

    const heading = document.createElement("div");
    heading.className = "section-title";
    heading.textContent = title;

    const explanation = document.createElement("p");
    explanation.className = "section-hint";
    explanation.textContent = hint;

    fragment.append(heading, explanation, body);
    return fragment;
}

function renderAnalysis(result) {
    const area = el("status-area");
    area.textContent = "";

    const coverage = result.coverage || {};
    const keywords = result.keyword_coverage || {};

    // Two numbers, side by side, because they answer different questions and
    // a candidate can be strong on one and screened out by the other.
    const scores = document.createElement("div");
    scores.className = "scores";

    scores.append(
        buildScoreCard(result.match_percentage, "Recruiter fit", [
            coverage.required_total
                ? `${coverage.required_met}/${coverage.required_total} must-haves`
                : "",
            coverage.preferred_total
                ? `${coverage.preferred_met}/${coverage.preferred_total} preferred`
                : "",
        ])
    );

    if (keywords.scored) {
        scores.append(
            buildScoreCard(keywords.score, "Keyword match", [
                `${keywords.matched.length}/${keywords.total} terms found`,
            ])
        );
    }

    area.append(scores);
    area.append(buildVerdict(result.match_percentage, keywords.scored ? keywords.score : null));

    // The two kinds of absence are deliberately separated. They overlap, and
    // without the distinction it reads as the same gap listed twice.
    if (result.missing_skills?.length) {
        area.append(
            buildSection(
                `⚠️ Gaps a recruiter would probe (${result.missing_skills.length})`,
                "Requirements your resume does not evidence.",
                buildGapList(result.missing_skills)
            )
        );
    }

    if (coverage.thin) {
        const caution = document.createElement("p");
        caution.className = "section-hint";
        caution.textContent =
            "This posting does not state enough to score against — the percentage is not meaningful here.";
        area.append(caution);
    }

    const notScored = coverage.not_scored || [];
    const internal = notScored.filter((entry) => entry.kind === "employer_internal");
    const behavioural = notScored.filter((entry) => entry.kind === "meta");

    if (internal.length) {
        area.append(
            buildSection(
                `ℹ️ Internal to this employer (${internal.length})`,
                "Nothing a resume can evidence from outside. Not counted either way.",
                buildGapList(internal.map((entry) => entry.skill))
            )
        );
    }

    if (behavioural.length) {
        area.append(
            buildSection(
                `🗣️ They will also assess (${behavioural.length})`,
                "Competency wording, not skills. Not scored — worth reading before an interview.",
                buildGapList(behavioural.map((entry) => entry.skill))
            )
        );
    }

    if (keywords.missing?.length) {
        area.append(
            buildSection(
                `🔍 Words a filter looks for (${keywords.missing.length})`,
                "Literal terms the posting uses. Add only the ones you can genuinely claim.",
                buildChips(keywords.missing, "chip-term")
            )
        );
    }

    // Last, and clamped. The summary is the most useful thing to read second
    // and the worst thing to have to scroll past first.
    if (result.summary) {
        const summary = document.createElement("div");
        summary.className = "summary-box clamped";
        summary.textContent = result.summary;

        const toggle = document.createElement("span");
        toggle.className = "summary-toggle";
        toggle.textContent = "Read the full summary";
        toggle.addEventListener("click", () => {
            const collapsed = summary.classList.toggle("clamped");
            toggle.textContent = collapsed ? "Read the full summary" : "Show less";
        });

        area.append(summary, toggle);
    }
}

// ---------------------------------------------------------------------------
// Authentication
// ---------------------------------------------------------------------------
function setAuthMode(mode) {
    authMode = mode;
    const registering = mode === "register";

    el("tab-signin").classList.toggle("active", !registering);
    el("tab-register").classList.toggle("active", registering);
    el("auth-submit").textContent = registering ? "Create account" : "Sign in";
    el("auth-password").autocomplete = registering ? "new-password" : "current-password";
    el("auth-error").textContent = "";

    show("invite-row", registering && registrationPolicy.invite_required);

    if (registering && !registrationPolicy.open) {
        el("auth-error").textContent = "Registration is closed on this server.";
        el("auth-submit").disabled = true;
    } else {
        el("auth-submit").disabled = false;
    }
}

el("tab-signin").addEventListener("click", () => setAuthMode("signin"));
el("tab-register").addEventListener("click", () => setAuthMode("register"));

el("auth-form").addEventListener("submit", async (event) => {
    event.preventDefault();

    const submit = el("auth-submit");
    submit.disabled = true;
    el("auth-error").textContent = "";

    const response = await send({
        type: authMode === "signin" ? "SIGN_IN" : "REGISTER",
        email: el("auth-email").value.trim(),
        password: el("auth-password").value,
        signup_code: el("auth-invite").value.trim()
    });

    submit.disabled = false;

    if (!response.ok) {
        el("auth-error").textContent = response.error;
        return;
    }

    el("auth-password").value = "";
    await initialize();
});

el("sign-out").addEventListener("click", async () => {
    await send({ type: "SIGN_OUT" });
    await initialize();
});

// ---------------------------------------------------------------------------
// Profiles
// ---------------------------------------------------------------------------
async function loadProfiles() {
    const response = await send({ type: "LIST_PROFILES" });
    const dropdown = el("profile-dropdown");
    dropdown.textContent = "";

    if (!response.ok || !response.data.profiles.length) {
        show("profile-dropdown", false);
        // With no resume on file the AI features cannot run, so lead the user
        // straight to the uploader rather than leaving it collapsed.
        el("upload-panel").open = true;
        return;
    }

    el("upload-panel").open = false;

    response.data.profiles.forEach((profile) => {
        const option = document.createElement("option");
        option.value = profile.filename;
        option.textContent = `🎯 ${profile.label}`;
        dropdown.appendChild(option);
    });

    const { activeProfile } = await chrome.storage.local.get("activeProfile");
    if (activeProfile && response.data.profiles.some((p) => p.filename === activeProfile)) {
        dropdown.value = activeProfile;
    } else {
        chrome.storage.local.set({ activeProfile: dropdown.value });
    }

    show("profile-dropdown", true);
}

el("profile-dropdown").addEventListener("change", (event) => {
    chrome.storage.local.set({ activeProfile: event.target.value });
});

// ---------------------------------------------------------------------------
// Settings (API address)
// ---------------------------------------------------------------------------
function setSettingsStatus(text, className = "") {
    const area = el("settings-status");
    area.textContent = "";

    if (!text) return;

    const span = document.createElement("span");
    if (className) span.className = className;
    span.textContent = text;
    area.appendChild(span);
}

async function loadSettings() {
    const response = await send({ type: "GET_SETTINGS" });
    if (response.ok) {
        el("settings-api-url").value = response.data.apiUrl;
    }

    // Opt-out, so an unset value means on.
    const { inPageCard } = await chrome.storage.local.get("inPageCard");
    el("setting-in-page-card").checked = inPageCard !== false;
}

// Written straight to storage rather than routed through the service worker:
// open job pages watch this key with chrome.storage.onChanged and show or hide
// the card without needing a reload.
el("setting-in-page-card").addEventListener("change", (event) => {
    chrome.storage.local.set({ inPageCard: event.target.checked });
});

el("settings-form").addEventListener("submit", (event) => {
    event.preventDefault();

    const raw = el("settings-api-url").value.trim();

    let parsed;
    try {
        parsed = new URL(raw);
    } catch (err) {
        setSettingsStatus("That is not a valid URL.", "warning");
        return;
    }

    if (!["http:", "https:"].includes(parsed.protocol)) {
        setSettingsStatus("The address must start with http:// or https://", "warning");
        return;
    }

    const submit = el("settings-submit");
    submit.disabled = true;
    setSettingsStatus("Requesting access…");

    // Called synchronously inside the click handler, before any await: the
    // user gesture is consumed by the first await, and a service worker
    // cannot call this at all. Requesting an already-granted origin is a
    // no-op that resolves true without prompting.
    chrome.permissions.request({ origins: [`${parsed.origin}/*`] }, async (granted) => {
        if (!granted) {
            submit.disabled = false;
            setSettingsStatus(`Access to ${parsed.origin} was declined.`, "warning");
            return;
        }

        const response = await send({ type: "SET_API_URL", apiUrl: parsed.origin });
        submit.disabled = false;

        if (!response.ok) {
            setSettingsStatus(response.error, "warning");
            return;
        }

        setSettingsStatus(`✅ Now using ${response.data.apiUrl}. Please sign in.`, "success-text");
        // Changing servers clears the session, so return to the signed-out state.
        await initialize();
    });
});

// ---------------------------------------------------------------------------
// Resume upload
// ---------------------------------------------------------------------------
const MAX_RESUME_BYTES = 5 * 1024 * 1024;

function setUploadStatus(text, className = "") {
    const area = el("upload-status");
    area.textContent = "";

    if (!text) return;

    const span = document.createElement("span");
    if (className) span.className = className;
    span.textContent = text;
    area.appendChild(span);
}

function readAsBase64(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        // readAsDataURL yields "data:<type>;base64,<payload>" — we want the payload.
        reader.onload = () => resolve(reader.result.split(",")[1]);
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
    });
}

el("upload-form").addEventListener("submit", async (event) => {
    event.preventDefault();

    const name = el("upload-name").value.trim();
    const file = el("upload-file").files[0];
    const submit = el("upload-submit");

    if (!name) {
        setUploadStatus("Please name this profile.", "warning");
        return;
    }
    if (!file) {
        setUploadStatus("Please choose a PDF file.", "warning");
        return;
    }
    // Checked here as well as server-side so an oversized file fails instantly
    // instead of after a slow upload.
    if (file.size > MAX_RESUME_BYTES) {
        setUploadStatus("That file is larger than 5 MB.", "warning");
        return;
    }

    submit.disabled = true;
    setUploadStatus("📄 Reading the PDF…");

    try {
        const dataBase64 = await readAsBase64(file);

        setUploadStatus("🤖 Gemini is structuring your resume… this takes a few seconds.");

        const response = await send({
            type: "UPLOAD_PROFILE",
            name,
            filename: file.name,
            dataBase64
        });

        if (!response.ok) {
            setUploadStatus(response.error, "warning");
            if (response.needsAuth) await initialize();
            return;
        }

        setUploadStatus(`✅ Created "${response.data.label}".`, "success-text");
        el("upload-form").reset();
        await loadProfiles();

        // Select the profile that was just created.
        el("profile-dropdown").value = response.data.filename;
        chrome.storage.local.set({ activeProfile: response.data.filename });
    } catch (err) {
        setUploadStatus(`Could not read that file: ${err.message}`, "warning");
    } finally {
        submit.disabled = false;
    }
});

// ---------------------------------------------------------------------------
// Page scanning
// ---------------------------------------------------------------------------
async function activeTab() {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    return tab;
}

// Origin match pattern for a tab, or "" for pages that cannot host a content
// script at all (chrome://, the Web Store, a file:// URL).
function originPatternFor(url) {
    try {
        const parsed = new URL(url);
        if (!["http:", "https:"].includes(parsed.protocol)) return "";
        return `${parsed.origin}/*`;
    } catch {
        return "";
    }
}

async function scanActivePage() {
    const tab = await activeTab();
    // Cached so the "enable on this site" click handler can read it without
    // an await, which would consume the user gesture permissions.request needs.
    currentTab = tab;

    let job;
    try {
        job = await chrome.tabs.sendMessage(tab.id, { action: "extract_job" });
    } catch (err) {
        // No content script in this tab. That does not mean the site is not
        // enabled: a tab open from before the permission was granted has no
        // script in it, and asking the user to "enable" a site they already
        // enabled — on every page, forever — was the old behaviour.
        job = await injectAndRetry(tab);

        if (!job) return;
    }

    // A page with neither a company nor a role is not a job posting.
    if (!job || (!job.company && !job.role)) {
        el("job-info").textContent = "No job posting detected on this page.";
        show("company-prompt", false);
        return;
    }

    currentJob = job;
    renderJobInfo(job);

    // The extractor returns an empty company rather than a guess when the page
    // does not name the employer. Ask instead of inventing one — a wrong
    // company means every later email about this job opens a duplicate row.
    const needsCompany = !job.company;
    show("company-prompt", needsCompany);

    // Analysis only reads the job description, so it works without a company.
    show("btn-analyze", true);

    if (needsCompany) {
        el("company-input").value = "";
        el("company-input").focus();
        show("btn-save", true);
        return;
    }

    const check = await send({ type: "CHECK_JOB", company: job.company, role: job.role });

    if (check.ok && check.data.exists) {
        setStatus(`Already tracked — status: ${check.data.status}`, "warning");
        return;
    }

    show("btn-save", true);
}

// Puts the content script into a tab that is missing one, and asks the page
// again. Returns the job data, or null when the site is not enabled yet.
async function injectAndRetry(tab) {
    const origin = originPatternFor(tab.url);

    if (!origin) {
        el("job-info").textContent = "Copilot cannot run on this page.";
        show("btn-enable-site", false);
        return null;
    }

    // Already granted? Then this is just a stale tab. Top it up silently
    // rather than making the user re-authorise a site they already trusted.
    if (!(await chrome.permissions.contains({ origins: [origin] }))) {
        el("job-info").textContent = "Copilot is not enabled on this site yet.";
        show("btn-enable-site", true);
        return null;
    }

    return injectInto(tab.id);
}

// Injects and re-asks. Shared by the stale-tab path and the enable button.
async function injectInto(tabId) {
    try {
        await chrome.scripting.executeScript({
            target: { tabId, allFrames: true },
            files: ["content.js"]
        });
        return await chrome.tabs.sendMessage(tabId, { action: "extract_job" });
    } catch (err) {
        el("job-info").textContent = "Copilot could not run on this page.";
        show("btn-enable-site", false);
        return null;
    }
}

el("btn-enable-site").addEventListener("click", () => {
    if (!currentTab?.url) return;

    const origin = originPatternFor(currentTab.url);
    if (!origin) return;

    const tabId = currentTab.id;

    // Requested synchronously so the click's user gesture is still live.
    chrome.permissions.request({ origins: [origin] }, async (granted) => {
        if (!granted) {
            setStatus(`Access to ${origin} was declined.`, "warning");
            return;
        }

        // Register the site durably before touching this tab. Without it the
        // grant only ever produced a one-page injection, and the next
        // navigation asked to enable the same site all over again.
        await send({ type: "SYNC_SITE_SCRIPTS" });

        show("btn-enable-site", false);
        await scanActivePage();
    });
});

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------
function jobPayload() {
    // A company typed into the prompt wins over whatever was scraped: the
    // prompt only appears when extraction found nothing, and the user is the
    // authority either way.
    const typed = el("company-input").value.trim();

    return {
        company: typed || currentJob.company,
        role: currentJob.role || "Unknown Role",
        jd_text: currentJob.jd_text,
        link: currentJob.link,
        // Declared by the posting rather than typed by anyone. Sent on every
        // save path so a row's origin never determines whether it has a salary.
        location: currentJob.location || "",
        remote: Boolean(currentJob.remote),
        salary_min: currentJob.salary_min ?? null,
        salary_max: currentJob.salary_max ?? null,
        salary_currency: currentJob.salary_currency || "",
        salary_period: currentJob.salary_period || "",
        profile: el("profile-dropdown").value || null
    };
}

el("btn-analyze").addEventListener("click", async () => {
    if (!currentJob) return;

    const button = el("btn-analyze");
    button.disabled = true;
    setStatus("🤖 Gemini is analyzing…");

    const response = await send({ type: "ANALYZE_JOB", job: jobPayload() });
    button.disabled = false;

    if (!response.ok) {
        setStatus(response.error, "warning");
        if (response.needsAuth) await initialize();
        return;
    }

    currentAnalysis = response.data;
    renderAnalysis(response.data);

    // Said plainly rather than left to look like a fast model. Reusing an
    // earlier analysis is the normal case for a posting already visited.
    if (response.data.reused) {
        setStatus("Showing the analysis saved for this posting.", "success");
    }
});

el("btn-save").addEventListener("click", async () => {
    if (!currentJob) return;

    const payload = jobPayload();

    if (!payload.company) {
        setStatus("Please enter the company name before saving.", "warning");
        el("company-input").focus();
        return;
    }

    const button = el("btn-save");
    button.disabled = true;
    setStatus("Saving…");

    if (currentAnalysis && !currentAnalysis.error) {
        payload.analysis = currentAnalysis;
    }

    const response = await send({ type: "SAVE_JOB", job: payload });
    button.disabled = false;

    if (!response.ok) {
        setStatus(response.error, "warning");
        if (response.needsAuth) await initialize();
        return;
    }

    setStatus("✅ Saved to your tracker.", "success-text");
    show("btn-save", false);
    show("company-prompt", false);
});

async function openDashboard() {
    // Ask the API for a one-time code so the dashboard adopts this session
    // instead of presenting a second sign-in form.
    const response = await send({ type: "OPEN_DASHBOARD" });

    chrome.tabs.create({
        url: response.ok ? response.data.url : dashboardUrl
    });
}

el("open-dashboard").addEventListener("click", openDashboard);

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
async function initialize() {
    ["btn-analyze", "btn-save", "btn-enable-site", "profile-dropdown"].forEach((id) =>
        show(id, false)
    );
    setStatus("");
    setUploadStatus("");

    await loadSettings();

    const status = await send({ type: "AUTH_STATUS" });
    const signedIn = status.ok && status.data.signedIn;

    // Adopt whatever the paired server reports about itself.
    if (status.data?.registration) registrationPolicy = status.data.registration;
    if (status.data?.dashboardUrl) dashboardUrl = status.data.dashboardUrl;

    show("auth-view", !signedIn);
    show("main-view", signedIn);

    if (!signedIn) {
        setAuthMode("signin");
        // Report an unreachable or wrong backend up front — signing in cannot
        // work until it is fixed, so say why before the user tries.
        if (status.data?.backendError) {
            el("auth-error").textContent = status.data.backendError;
            // The address is the usual culprit, so open Settings for them.
            el("settings-panel").open = true;
        }
        return;
    }

    el("account-email").textContent = status.data.email;
    await loadProfiles();
    await showSetupNudge();
    await scanActivePage();
}

// Form suggestions only exist once the questionnaire is filled in. Without
// this, a new user sees no suggestions and no explanation why.
async function showSetupNudge() {
    const response = await send({ type: "GET_AUTOFILL" });

    if (!response.ok) return;

    const missing = response.data.completeness?.missing || 0;
    const banner = el("setup-nudge");

    if (!missing) {
        show("setup-nudge", false);
        return;
    }

    banner.textContent = "";

    const text = document.createTextNode(
        `${missing} application answers not set up yet — `
    );
    const link = document.createElement("span");
    link.className = "link";
    link.textContent = "finish setup ↗";
    link.addEventListener("click", openDashboard);

    banner.append(text, link);
    show("setup-nudge", true);
}

initialize();
