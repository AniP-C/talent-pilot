// extension/popup.js
//
// UI only. Every network call is delegated to background.js, which owns the
// auth token. Scraped page values are rendered with textContent, never
// innerHTML — a job posting is untrusted input and could otherwise inject
// markup into this privileged page.

const el = (id) => document.getElementById(id);
const send = (message) => chrome.runtime.sendMessage(message);

let currentJob = null;
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
}

function renderAnalysis(result) {
    const area = el("status-area");
    area.textContent = "";

    const score = document.createElement("div");
    score.className = "score";
    score.textContent = `Recruiter fit: ${result.match_percentage}%`;

    area.append(score);

    // How that figure was reached. The number used to be the model's opinion;
    // it is now computed from the requirement classifications, and saying so
    // is the difference between a score and an assertion.
    const coverage = result.coverage;
    if (coverage?.required_total) {
        const working = document.createElement("div");
        working.className = "muted";
        working.textContent =
            `${coverage.required_met} of ${coverage.required_total} must-haves` +
            (coverage.preferred_total
                ? `, ${coverage.preferred_met} of ${coverage.preferred_total} preferred`
                : "");
        area.append(working);
    }

    // A filter matches text, not meaning, so a strong candidate can still be
    // screened out. Reported separately because it is a different problem with
    // a different fix.
    const keywords = result.keyword_coverage;
    if (keywords?.scored) {
        const ats = document.createElement("div");
        ats.className = "score";
        ats.textContent = `Keyword coverage: ${keywords.score}%`;
        area.append(ats);
    }

    const summary = document.createElement("p");
    summary.className = "muted";
    summary.textContent = result.summary;
    area.append(summary);

    const missingLabel = document.createElement("b");
    missingLabel.textContent = "Missing: ";

    const missing = document.createElement("div");
    missing.append(
        missingLabel,
        document.createTextNode(
            result.missing_skills.length ? result.missing_skills.join(", ") : "None"
        )
    );
    area.append(missing);

    if (keywords?.missing?.length) {
        const termsLabel = document.createElement("b");
        termsLabel.textContent = "Terms not on your resume: ";

        const terms = document.createElement("div");
        terms.className = "muted";
        terms.append(termsLabel, document.createTextNode(keywords.missing.join(", ")));
        area.append(terms);
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
}

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

    renderAnalysis(response.data);
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
