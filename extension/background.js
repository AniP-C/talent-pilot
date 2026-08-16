// extension/background.js
//
// Every call to the local API goes through this service worker. Two reasons:
//
//   1. The auth token lives here and is never handed to a content script, so a
//      hostile page has nothing to steal even if it breaks out of its sandbox.
//   2. Requests originate from the extension's own chrome-extension:// origin,
//      which is the only origin the API's CORS policy accepts.

// Where the API lives.
//
// Defaults to the hosted deployment so an installed extension works with no
// configuration. Overridable from the popup's Settings panel, which is what
// developers point at http://localhost:8000 when running the API locally.
const DEFAULT_API_URL = "https://katchjobs.online";

// The API's /health endpoint reports this. Anything else on the address is a
// different application, which is worth saying out loud.
const SERVICE_NAME = "talent-pilot-api";

async function getApiUrl() {
    const { apiUrl } = await chrome.storage.local.get("apiUrl");
    return (apiUrl || DEFAULT_API_URL).replace(/\/+$/, "");
}

function wrongServerHint(apiUrl) {
    return (
        `${apiUrl} is serving a different application. ` +
        "The dashboard and the API are two separate servers — run the API with: " +
        "uvicorn api.server:app --port 8000"
    );
}

// ---------------------------------------------------------------------------
// Token storage
// ---------------------------------------------------------------------------
async function getToken() {
    const { authToken } = await chrome.storage.local.get("authToken");
    return authToken || null;
}

async function setSession(token, email) {
    await chrome.storage.local.set({ authToken: token, userEmail: email });
    // Signing in is an account change. Without this, a sign-in that followed
    // an expired session would serve the previous account's saved answers
    // until the cache happened to age out.
    autofillCache = { rules: false, data: null, at: 0 };
    // Scores are per-resume, so one account's must never be shown to the next.
    keywordCache.clear();
    // Job pages opened before signing in are holding an empty rule set.
    await broadcastAutofillChanged();
}

async function clearSession() {
    await chrome.storage.local.remove(["authToken", "userEmail"]);
    // One account's saved answers must never be served to the next person to
    // sign in on this browser.
    autofillCache = { rules: false, data: null, at: 0 };
    keywordCache.clear();
    // Signing out has to take the suggestions off open pages too, or the
    // previous account's answers stay one click from being filled in.
    await broadcastAutofillChanged();
}

// ---------------------------------------------------------------------------
// Keeping the copilot enabled on sites the user has granted
// ---------------------------------------------------------------------------
// Granting a host permission does not run a content script; it only makes one
// injectable. The popup used to follow the grant with chrome.scripting
// .executeScript, which lasts exactly as long as that one page — so the next
// navigation had no script, the popup reported "Copilot is not active on this
// page", and the user was asked to enable the same site again. Every page.
// Forever.
//
// A dynamic registration is the durable form of the same thing: Chrome injects
// it into every matching page from now on, and remembers across restarts.
const DYNAMIC_SCRIPT_ID = "talent-pilot-granted-sites";

// Origins used for API traffic rather than job hunting. The dashboard is
// served from one of them, and injecting the form scanner into our own UI
// would decorate its textareas with "Generate AI Answer" buttons.
async function nonJobOrigins() {
    const origins = new Set(["https://katchjobs.online/*", "http://localhost:8000/*"]);

    try {
        origins.add(`${new URL(await getApiUrl()).origin}/*`);
    } catch {
        // A malformed stored address is handled where it is set; here it just
        // means one fewer origin to exclude.
    }

    return origins;
}

async function syncContentScripts() {
    const { origins = [] } = await chrome.permissions.getAll();
    const skip = await nonJobOrigins();
    const matches = origins.filter((origin) => !skip.has(origin));

    // Re-registering an existing id throws, so clear first. Unregistering
    // something that was never registered throws too, and harmlessly.
    try {
        await chrome.scripting.unregisterContentScripts({ ids: [DYNAMIC_SCRIPT_ID] });
    } catch {
        // Nothing was registered yet.
    }

    if (!matches.length) return;

    try {
        await chrome.scripting.registerContentScripts([
            {
                id: DYNAMIC_SCRIPT_ID,
                matches,
                js: ["content.js"],
                runAt: "document_idle",
                // Application forms on Greenhouse, Lever and Workday are
                // routinely embedded in an iframe on the employer's own careers
                // page. Injecting only the top frame is why suggestions never
                // appeared on exactly the pages with the most questions to
                // answer. content.js is idempotent per frame, so the overlap
                // with the static registration above costs nothing.
                allFrames: true,
                persistAcrossSessions: true
            }
        ]);
    } catch (err) {
        console.error("[Job Copilot] could not register content scripts:", err);
    }
}

chrome.runtime.onInstalled.addListener(syncContentScripts);
chrome.runtime.onStartup.addListener(syncContentScripts);
chrome.permissions.onAdded.addListener(syncContentScripts);
chrome.permissions.onRemoved.addListener(syncContentScripts);

// ---------------------------------------------------------------------------
// Telling open pages their answers changed
// ---------------------------------------------------------------------------
// A content script reads the answer bank once, when it loads. Without this,
// signing in — or saving an answer — after a job page was already open leaves
// that page with an empty rule set and no suggestions until it is reloaded,
// which looks exactly like the feature being broken.
async function broadcastAutofillChanged() {
    const tabs = await chrome.tabs.query({});

    await Promise.all(
        tabs.map((tab) =>
            chrome.tabs
                .sendMessage(tab.id, { type: "AUTOFILL_CHANGED" })
                .catch(() => {
                    // No content script in that tab, which is the normal case.
                })
        )
    );
}

// ---------------------------------------------------------------------------
// Autofill answer cache
// ---------------------------------------------------------------------------
// A content script asks for the user's answers on every page load, and they
// change rarely. Without this, opening ten job tabs is ten identical requests.
const AUTOFILL_TTL_MS = 5 * 60 * 1000;
let autofillCache = { rules: false, data: null, at: 0 };

// ---------------------------------------------------------------------------
// Keyword scan cache
// ---------------------------------------------------------------------------
// The in-page card scans on arrival rather than on a click, so the same posting
// is asked about every time a job board re-renders it — which on LinkedIn is
// several times per navigation. Keyed by description, since that is what the
// answer actually depends on.
const KEYWORD_TTL_MS = 10 * 60 * 1000;
const KEYWORD_CACHE_MAX = 30;
const keywordCache = new Map();

// ---------------------------------------------------------------------------
// HTTP
// ---------------------------------------------------------------------------
async function apiRequest(path, { method = "GET", body = null, auth = true } = {}) {
    const isFormData = body instanceof FormData;

    // FormData must set its own Content-Type so the multipart boundary is
    // included; setting it by hand produces an unparseable request.
    const headers = isFormData ? {} : { "Content-Type": "application/json" };

    if (auth) {
        const token = await getToken();
        if (!token) return { ok: false, error: "Please sign in from the extension popup.", needsAuth: true };
        headers["Authorization"] = `Bearer ${token}`;
    }

    let requestBody;
    if (body === null) {
        requestBody = undefined;
    } else if (isFormData) {
        requestBody = body;
    } else {
        requestBody = JSON.stringify(body);
    }

    const apiUrl = await getApiUrl();

    let response;
    try {
        response = await fetch(`${apiUrl}${path}`, {
            method,
            headers,
            body: requestBody
        });
    } catch (err) {
        console.error("[Job Copilot] network error calling", path, err);
        return {
            ok: false,
            error: `Cannot reach ${apiUrl}. Check the API address in Settings, ` +
                "or start the API with: uvicorn api.server:app --port 8000"
        };
    }

    // An expired or revoked token should drop the local session rather than
    // leaving the popup in a broken half-signed-in state.
    if (response.status === 401) {
        await clearSession();
        return { ok: false, error: "Your session expired. Please sign in again.", needsAuth: true };
    }

    if (response.status === 204) return { ok: true, data: {} };

    const contentType = response.headers.get("content-type") || "";

    // A non-JSON reply means the port is answering, but not with our API —
    // typically the Streamlit dashboard started on 8000 by mistake. Saying so
    // beats reporting a bare status code the user cannot act on.
    if (!contentType.includes("application/json")) {
        const preview = (await response.text()).slice(0, 120);
        console.error(
            "[Job Copilot] non-JSON reply from", path,
            "| status", response.status, "| content-type", contentType, "|", preview
        );
        return { ok: false, error: wrongServerHint(apiUrl), wrongServer: true };
    }

    let payload;
    try {
        payload = await response.json();
    } catch (err) {
        return { ok: false, error: `Malformed response from the API (HTTP ${response.status}).` };
    }

    if (!response.ok) {
        console.warn("[Job Copilot]", path, "failed:", response.status, payload.detail);
        return {
            ok: false,
            error: payload.detail || `Request failed (HTTP ${response.status}).`,
            status: response.status
        };
    }

    return { ok: true, data: payload };
}

// ---------------------------------------------------------------------------
// Backend identity
// ---------------------------------------------------------------------------
async function checkBackend() {
    const result = await apiRequest("/health", { auth: false });

    if (!result.ok) return result;

    if (result.data.service !== SERVICE_NAME) {
        return {
            ok: false,
            error: wrongServerHint(await getApiUrl()),
            wrongServer: true
        };
    }

    return { ok: true, data: result.data };
}

function base64ToBlob(base64, contentType) {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);

    for (let i = 0; i < binary.length; i++) {
        bytes[i] = binary.charCodeAt(i);
    }

    return new Blob([bytes], { type: contentType });
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------
const handlers = {
    async GET_SETTINGS() {
        return { ok: true, data: { apiUrl: await getApiUrl(), defaultApiUrl: DEFAULT_API_URL } };
    },

    // Stores the address only. Host permission is requested by the popup,
    // because chrome.permissions.request() needs a user gesture and cannot
    // be called from a service worker at all.
    async SET_API_URL({ apiUrl }) {
        let parsed;
        try {
            parsed = new URL(apiUrl);
        } catch (err) {
            return { ok: false, error: "That is not a valid URL." };
        }

        if (!["http:", "https:"].includes(parsed.protocol)) {
            return { ok: false, error: "The address must start with http:// or https://" };
        }

        const origin = `${parsed.origin}/*`;
        if (!(await chrome.permissions.contains({ origins: [origin] }))) {
            return {
                ok: false,
                error: `Permission to reach ${parsed.origin} was not granted.`
            };
        }

        // Pointing at a different server makes the old server's token useless.
        await clearSession();
        await chrome.storage.local.set({ apiUrl: parsed.origin });

        return { ok: true, data: { apiUrl: parsed.origin } };
    },

    async AUTH_STATUS() {
        // Confirm we are talking to the right server before anything else, so
        // a misconfigured address is reported once and clearly.
        const backend = await checkBackend();
        if (!backend.ok) {
            return { ok: true, data: { signedIn: false, backendError: backend.error } };
        }

        // Carry the server's own description of itself back to the popup, so
        // the invite field and dashboard link match the server in use.
        const serverInfo = {
            registration: backend.data.registration || { open: true, invite_required: false },
            dashboardUrl: backend.data.dashboard_url || "http://localhost:8501"
        };

        const token = await getToken();
        if (!token) return { ok: true, data: { signedIn: false, ...serverInfo } };

        const result = await apiRequest("/auth/me");
        if (!result.ok) {
            return { ok: true, data: { signedIn: false, error: result.error, ...serverInfo } };
        }

        return { ok: true, data: { signedIn: true, email: result.data.email, ...serverInfo } };
    },

    async SIGN_IN({ email, password }) {
        const backend = await checkBackend();
        if (!backend.ok) return backend;

        const result = await apiRequest("/auth/login", {
            method: "POST",
            body: { email, password },
            auth: false
        });
        if (result.ok) await setSession(result.data.token, result.data.email);
        return result;
    },

    async REGISTER({ email, password, signup_code }) {
        const backend = await checkBackend();
        if (!backend.ok) return backend;

        const result = await apiRequest("/auth/register", {
            method: "POST",
            body: { email, password, signup_code: signup_code || "" },
            auth: false
        });
        if (result.ok) await setSession(result.data.token, result.data.email);
        return result;
    },

    async SIGN_OUT() {
        await apiRequest("/auth/logout", { method: "POST" });
        await clearSession();
        return { ok: true, data: {} };
    },

    // Returns a URL that opens the dashboard already signed in.
    OPEN_DASHBOARD() {
        return apiRequest("/auth/handoff", { method: "POST" });
    },

    LIST_PROFILES() {
        return apiRequest("/profiles");
    },

    async UPLOAD_PROFILE({ name, filename, dataBase64 }) {
        // The popup sends base64 because a File cannot cross the extension
        // message boundary — messages are JSON-serialized.
        const form = new FormData();
        form.append("name", name);
        form.append("file", base64ToBlob(dataBase64, "application/pdf"), filename);

        return apiRequest("/profiles/upload", { method: "POST", body: form });
    },

    DELETE_PROFILE({ filename }) {
        return apiRequest(`/profiles/${encodeURIComponent(filename)}`, {
            method: "DELETE"
        });
    },

    CHECK_JOB({ company, role }) {
        return apiRequest("/check-job", { method: "POST", body: { company, role } });
    },

    SAVE_JOB({ job }) {
        return apiRequest("/save-job", { method: "POST", body: job });
    },

    ANALYZE_JOB({ job }) {
        return apiRequest("/analyze-job", { method: "POST", body: job });
    },

    // The keyword-only score behind the in-page card. Cached per description
    // because a job board re-renders the same posting several times on the way
    // to settling — a MutationObserver-driven card would otherwise ask for the
    // same number on every one of them.
    async KEYWORD_SCAN({ jd_text, profile }) {
        const key = `${profile || "default"}::${jd_text.length}::${jd_text.slice(0, 200)}`;
        const cached = keywordCache.get(key);

        if (cached && Date.now() - cached.at < KEYWORD_TTL_MS) {
            return { ok: true, data: cached.data };
        }

        const response = await apiRequest("/keyword-scan", {
            method: "POST",
            body: { jd_text, profile }
        });

        if (response.ok) {
            // Bounded, oldest first: a long browsing session opens a lot of
            // postings and none of them are worth remembering forever.
            if (keywordCache.size >= KEYWORD_CACHE_MAX) {
                keywordCache.delete(keywordCache.keys().next().value);
            }
            keywordCache.set(key, { data: response.data, at: Date.now() });
        }

        return response;
    },

    GENERATE_ANSWER({ payload }) {
        return apiRequest("/generate-answer", { method: "POST", body: payload });
    },

    SAVE_ANSWER({ question, answer }) {
        return apiRequest("/save-answer", { method: "POST", body: { question, answer } });
    },

    // The user's saved application-form answers. Cached because a content
    // script asks for these on every page load and they change rarely; without
    // it, opening ten job tabs would be ten identical round trips.
    async GET_AUTOFILL({ force = false } = {}) {
        const now = Date.now();

        if (!force && autofillCache.rules && now - autofillCache.at < AUTOFILL_TTL_MS) {
            return { ok: true, data: autofillCache.data };
        }

        const response = await apiRequest("/autofill");

        if (response.ok) {
            autofillCache = { rules: true, data: response.data, at: now };
        }

        return response;
    },

    // Saves an AI-drafted answer so the same question is instant next time.
    async SAVE_CUSTOM_ANSWER({ question, answer }) {
        // Any write invalidates the cache, or the new answer would not be
        // suggested until the TTL happened to expire.
        autofillCache = { rules: false, data: null, at: 0 };

        const result = await apiRequest("/autofill/custom", {
            method: "POST",
            body: { question, answer }
        });

        // Other tabs are showing the same application form more often than
        // not, so tell them a new answer exists rather than making the user
        // reload to see it.
        if (result.ok) await broadcastAutofillChanged();

        return result;
    },

    // Called by the popup straight after a site is granted, so the durable
    // registration exists without waiting for the permissions event.
    async SYNC_SITE_SCRIPTS() {
        await syncContentScripts();
        return { ok: true, data: {} };
    },

    // An application form embedded in an iframe can see the questions but not
    // the posting around them. Only the top frame can answer that, and only
    // this worker knows the tab id needed to address it.
    async GET_TOP_FRAME_JOB(message, sender) {
        const tabId = sender?.tab?.id;
        if (tabId === undefined) return { ok: false, error: "No originating tab." };

        try {
            const data = await chrome.tabs.sendMessage(
                tabId,
                { action: "extract_job" },
                { frameId: 0 }
            );
            return { ok: true, data };
        } catch (err) {
            return { ok: false, error: String(err) };
        }
    }
};

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    const handler = handlers[message?.type];

    if (!handler) {
        sendResponse({ ok: false, error: `Unknown request: ${message?.type}` });
        return false;
    }

    // `sender` is passed through so a handler can tell which tab and frame
    // asked. Handlers that do not care simply ignore the second argument.
    handler(message, sender)
        .then(sendResponse)
        .catch((err) => sendResponse({ ok: false, error: String(err) }));

    return true; // keep the message channel open for the async response
});
