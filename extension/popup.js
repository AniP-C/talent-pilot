// extension/popup.js

const API_URL = "http://localhost:8000";
let currentJobData = null;

// ==============================================================================
// 1. TARGET PROFILE MANAGER (The Persona Switcher)
// ==============================================================================
async function loadProfiles() {
    try {
        const res = await fetch(`${API_URL}/profiles`);
        const data = await res.json();
        const dropdown = document.getElementById('profile-dropdown');
        
        if (data.profiles && data.profiles.length > 0) {
            dropdown.style.display = 'block';
            
            // Populate the dropdown options
            data.profiles.forEach(profile => {
                let opt = document.createElement('option');
                opt.value = profile;
                opt.innerText = "🎯 " + profile.replace('.json', '').replace(/_/g, ' ').toUpperCase();
                dropdown.appendChild(opt);
            });
            
            // Restore previous choice or set default
            chrome.storage.local.get(['activeProfile'], function(result) {
                if (result.activeProfile) {
                    dropdown.value = result.activeProfile;
                } else {
                    chrome.storage.local.set({ activeProfile: dropdown.value });
                }
            });

            // Save the selection globally whenever the user changes it
            dropdown.addEventListener('change', (e) => {
                chrome.storage.local.set({ activeProfile: e.target.value });
            });
        }
    } catch (err) {
        console.error("Could not fetch profiles from backend. Is FastAPI running?", err);
    }
}

// Call immediately when popup opens
loadProfiles();

// ==============================================================================
// 2. INITIALIZATION & CACHE CHECK
// ==============================================================================
chrome.tabs.query({active: true, currentWindow: true}, function(tabs) {
    const currentUrl = tabs[0].url;
    
    // Check if we have cached analysis data for this specific URL
    chrome.storage.local.get([currentUrl], function(cachedData) {
        
        if (cachedData[currentUrl]) {
            // 🟢 CACHE HIT: Restore the popup state instantly
            const cache = cachedData[currentUrl];
            currentJobData = cache.jobData;
            
            document.getElementById('job-info').innerHTML = `<b>${currentJobData.company}</b><br>${currentJobData.role}`;
            document.getElementById('status-area').innerHTML = cache.htmlContent;
            
            document.getElementById('btn-analyze').style.display = 'block';
            document.getElementById('btn-save').style.display = cache.showSaveButton ? 'block' : 'none';
            return;
        }
        
        // 🔴 CACHE MISS: Run a fresh scan of the active tab
        chrome.tabs.sendMessage(tabs[0].id, {action: "extract_job"}, async function(response) {
            
            if (chrome.runtime.lastError) {
                document.getElementById('job-info').innerHTML = "<span class='warning'>⚠️ Extension not loaded on this page. Try refreshing the page!</span>";
                return;
            }

            if (response && response.company !== "Unknown Company") {
                currentJobData = response;
                document.getElementById('job-info').innerHTML = `<b>${response.company}</b><br>${response.role}`;
                checkIfAlreadyApplied(response.company, response.role);
            } else {
                document.getElementById('job-info').innerText = "No job detected on this page.";
            }
        });
    });
});

// ==============================================================================
// 3. CHECK IF JOB ALREADY EXISTS IN DB
// ==============================================================================
async function checkIfAlreadyApplied(company, role) {
    try {
        const res = await fetch(`${API_URL}/check-job`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ company, role })
        });
        const data = await res.json();
        
        if (data.exists) {
            document.getElementById('status-area').innerHTML = `<span class="warning">⚠️ Already saved! (Status: ${data.status})</span>`;
        } else {
            document.getElementById('btn-analyze').style.display = 'block';
            document.getElementById('btn-save').style.display = 'block';
        }
    } catch (err) {
        document.getElementById('status-area').innerText = "⚠️ Cannot connect to backend. Is FastAPI running?";
    }
}

// ==============================================================================
// 4. ANALYZE MATCH BUTTON (Triggers AI)
// ==============================================================================
document.getElementById('btn-analyze').addEventListener('click', async () => {
    document.getElementById('status-area').innerText = "🤖 Gemini is analyzing...";
    
    try {
        // Grab the selected profile from the dropdown to send to the backend
        const dropdown = document.getElementById('profile-dropdown');
        if (dropdown) currentJobData.profile = dropdown.value;

        const res = await fetch(`${API_URL}/analyze-job`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(currentJobData)
        });
        const aiResult = await res.json();
        
        if (aiResult.error === "RATE_LIMIT") {
            document.getElementById('status-area').innerHTML = `<span class='warning'>⏱️ ${aiResult.message}</span>`;
            return; 
        } else if (aiResult.error) {
            document.getElementById('status-area').innerHTML = `<span class='warning'>❌ Analysis Failed. Check logs.</span>`;
            return;
        }
        
        const resultHtml = `
            <b>Match Score: ${aiResult.match_percentage}%</b><br>
            <i style="color:#555;">${aiResult.summary}</i><br><br>
            <b>Missing Skills:</b> ${aiResult.missing_skills.length > 0 ? aiResult.missing_skills.join(", ") : "None!"}
        `;
        
        document.getElementById('status-area').innerHTML = resultHtml;
        
        // 💾 SAVE TO LOCAL CACHE so it doesn't disappear if user clicks away
        chrome.tabs.query({active: true, currentWindow: true}, function(tabs) {
            const currentUrl = tabs[0].url;
            chrome.storage.local.set({
                [currentUrl]: {
                    jobData: currentJobData,
                    htmlContent: resultHtml,
                    showSaveButton: document.getElementById('btn-save').style.display === 'block'
                }
            });
        });

    } catch (err) {
        document.getElementById('status-area').innerHTML = "<span class='warning'>⚠️ Connection to API dropped.</span>";
    }
});

// ==============================================================================
// 5. SAVE JOB BUTTON (Triggers Database Insert)
// ==============================================================================
document.getElementById('btn-save').addEventListener('click', async () => {
    document.getElementById('status-area').innerText = "Saving to Database...";
    
    try {
        // Include the profile so the DB knows which resume was used
        const dropdown = document.getElementById('profile-dropdown');
        if (dropdown) currentJobData.profile = dropdown.value;

        await fetch(`${API_URL}/save-job`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(currentJobData)
        });
        
        document.getElementById('status-area').innerHTML = "<span class='success-text'>✅ Saved to Dashboard!</span>";
        document.getElementById('btn-save').style.display = 'none';
        
        // 🧹 CLEAR THE CACHE
        chrome.tabs.query({active: true, currentWindow: true}, function(tabs) {
            chrome.storage.local.remove([tabs[0].url]);
        });
        
    } catch (err) {
        document.getElementById('status-area').innerHTML = "<span class='warning'>⚠️ Save failed. Is FastAPI running?</span>";
    }
});

// ==============================================================================
// 6. OPEN DASHBOARD SHORTCUT
// ==============================================================================
document.getElementById('open-dashboard').addEventListener('click', () => {
    chrome.tabs.create({ url: "http://localhost:8501" });
});