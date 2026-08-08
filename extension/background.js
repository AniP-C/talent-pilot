// extension/background.js
//
// Every call to the local API goes through this service worker. Two reasons:
//
//   1. The auth token lives here and is never handed to a content script, so a
//      hostile page has nothing to steal even if it breaks out of its sandbox.
//   2. Requests originate from the extension's own chrome-extension:// origin,
//      which is the only origin the API's CORS policy accepts.

// Where the API lives. Overridable from the popup so the same extension works
// against a local server or a hosted deployment.
const DEFAULT_API_URL = "http://localhost:8000";

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
}

async function clearSession() {
    await chrome.storage.local.remove(["authToken", "userEmail"]);
    // One account's saved answers must never be served to the next person to
    // sign in on this browser.
    autofillCache = { rules: false, data: null, at: 0 };
}

// ---------------------------------------------------------------------------
// Autofill answer cache
// ---------------------------------------------------------------------------
// A content script asks for the user's answers on every page load, and they
// change rarely. Without this, opening ten job tabs is ten identical requests.
const AUTOFILL_TTL_MS = 5 * 60 * 1000;
let autofillCache = { rules: false, data: null, at: 0 };

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
    SAVE_CUSTOM_ANSWER({ question, answer }) {
        // Any write invalidates the cache, or the new answer would not be
        // suggested until the TTL happened to expire.
        autofillCache = { rules: false, data: null, at: 0 };
        return apiRequest("/autofill/custom", {
            method: "POST",
            body: { question, answer }
        });
    }
};

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    const handler = handlers[message?.type];

    if (!handler) {
        sendResponse({ ok: false, error: `Unknown request: ${message?.type}` });
        return false;
    }

    handler(message)
        .then(sendResponse)
        .catch((err) => sendResponse({ ok: false, error: String(err) }));

    return true; // keep the message channel open for the async response
});
