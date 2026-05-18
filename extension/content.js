// extension/content.js

// --- PASSIVE MODE: Scan for form fields to help user ---
function scanAndSuggest() {
    const elements = document.querySelectorAll('label, h3, span'); 
    
    elements.forEach(el => {
        if (el.classList.contains('ai-copilot-suggestion')) return;
        if (el.hasAttribute('data-ai-scanned')) return;
        if (el.closest('[data-ai-scanned]')) return;

        const text = el.innerText;
        if (!text || text.includes('💡 Suggestion')) return;

        // Note: Assumes AUTOFILL_RULES is defined globally in rules.js
        if (typeof AUTOFILL_RULES !== 'undefined') {
            for (let i = 0; i < AUTOFILL_RULES.length; i++) {
                const rule = AUTOFILL_RULES[i];
                if (rule.pattern.test(text)) {
                    const suggestion = document.createElement('div');
                    suggestion.innerHTML = `💡 <b>Suggestion:</b> ${rule.suggestion}`;
                    
                    suggestion.style.all = 'initial';
                    suggestion.style.display = 'block';
                    suggestion.style.fontFamily = 'Arial, sans-serif';
                    suggestion.style.color = '#0056b3';
                    suggestion.style.backgroundColor = '#e8f4fd';
                    suggestion.style.border = '1px solid #b8daff';
                    suggestion.style.padding = '4px 8px';
                    suggestion.style.marginTop = '4px';
                    suggestion.style.marginBottom = '8px';
                    suggestion.style.borderRadius = '6px';
                    suggestion.style.fontSize = '12px';
                    suggestion.style.width = 'max-content'; 
                    suggestion.style.boxShadow = '0 2px 4px rgba(0,0,0,0.05)';
                    suggestion.className = 'ai-copilot-suggestion'; 
                    
                    el.insertAdjacentElement('afterend', suggestion);
                    el.setAttribute('data-ai-scanned', 'true');
                    break; 
                }
            }
        }
    });
}

// --- ACTIVE MODE: Inject AI Generation Buttons ---
function injectAIGenerateButtons() {
    const textareas = document.querySelectorAll('textarea');
    
    textareas.forEach(textarea => {
        if (textarea.hasAttribute('data-ai-button-added')) return;
        
        let questionText = "Unknown Question";
        const label = textarea.previousElementSibling || textarea.parentElement.querySelector('label');
        if (label) questionText = label.innerText;

        const btn = document.createElement('button');
        btn.innerHTML = '✨ Generate AI Answer';
        btn.style.all = 'initial'; 
        btn.style.display = 'block';
        btn.style.marginTop = '5px';
        btn.style.padding = '5px 10px';
        btn.style.backgroundColor = '#6200ee';
        btn.style.color = 'white';
        btn.style.border = 'none';
        btn.style.borderRadius = '4px';
        btn.style.cursor = 'pointer';
        btn.style.fontFamily = 'Arial';
        btn.style.fontSize = '12px';
        
        // Button Click Event with Smart Caching & Persona Routing
        btn.addEventListener('click', async (e) => {
            e.preventDefault();
            btn.innerHTML = '⏳ Checking...';
            
            const jobContext = extractJobData(); 
            const currentUrl = window.location.href;
            
            // Unique cache key for THIS question on THIS page
            const cacheKey = `ans_${currentUrl}_${questionText.substring(0, 50)}`;
            
            // 1. Check Chrome Local Storage for Cache AND the Active Persona Profile
            chrome.storage.local.get([cacheKey, 'activeProfile'], async function(cache) {
                if (cache[cacheKey]) {
                    // 🟢 CACHE HIT
                    console.log(`🟢 [CACHE HIT] Loaded saved answer for: "${questionText}"`);
                    textarea.value = cache[cacheKey];
                    btn.innerHTML = '✅ Restored from Cache';
                    btn.style.backgroundColor = '#28a745';
                    return; 
                }

                // 🔴 CACHE MISS
                console.log(`🚀 [API CALL] Generating fresh Gemini response for: "${questionText}" using Profile: ${cache.activeProfile || "Default"}`);
                btn.innerHTML = '🤖 Generating via AI...';
                
                try {
                    const response = await fetch("http://localhost:8000/generate-answer", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            question: questionText,
                            company: jobContext.company,
                            role: jobContext.role,
                            jd_text: jobContext.jd_text,
                            profile: cache.activeProfile || null // 🎯 Tell backend which persona to use
                        })
                    });
                    
                    const data = await response.json();
                    
                    if (data.suggested_answer) {
                        textarea.value = data.suggested_answer;
                        btn.innerHTML = '✅ Generated (Review before submitting)';
                        btn.style.backgroundColor = '#28a745';
                        
                        // 💾 SAVE TO CACHE
                        chrome.storage.local.set({ [cacheKey]: data.suggested_answer });
                        
                    } else if (data.error) {
                        btn.innerHTML = `❌ ${data.message || "API Error"}`;
                        btn.style.backgroundColor = '#d9534f';
                        console.error("BACKEND ERROR DATA:", JSON.stringify(data)); 
                    }
                
                } catch (err) {
                    btn.innerHTML = '❌ Server Error (Is FastAPI running?)';
                    btn.style.backgroundColor = '#d9534f';
                    console.error("FETCH ERROR:", err);
                }
            });
        });

        textarea.insertAdjacentElement('afterend', btn);
        textarea.setAttribute('data-ai-button-added', 'true');
    });
}

// Run scanner and injector cleanly on a single loop
setInterval(() => {
    scanAndSuggest();
    injectAIGenerateButtons();
}, 3000);

// --- DOMAIN ROUTER: Extract context from the page ---
function extractJobData() {
    const domain = window.location.hostname;
    const url = window.location.href;
    
    let company = "Unknown Company";
    let role = "Unknown Role";
    let jd_text = "";

    try {
        // 1. LINKEDIN
        if (domain.includes("linkedin.com")) {
            company = document.querySelector('.job-details-jobs-unified-top-card__company-name')?.innerText || 
                      document.querySelector('.pr2.t-14')?.innerText;
            role = document.querySelector('.job-details-jobs-unified-top-card__job-title')?.innerText || 
                   document.querySelector('h1')?.innerText;
            
            const jdElement = document.querySelector('.jobs-description__content, #job-details');
            jd_text = jdElement ? jdElement.innerText.substring(0, 4000) : "";
        } 
        // 2. GREENHOUSE
        else if (domain.includes("greenhouse.io") || domain.includes("boards.greenhouse.io")) {
            role = document.querySelector('.app-title h1, h1')?.innerText; 
            company = document.querySelector('.company-name, .logo-container')?.innerText?.replace('at ', '');
            if (!company || company.trim() === "") company = document.title.split(' - ')[0]; 
            jd_text = document.querySelector('#content, #main, .accessible-wrapper')?.innerText;
        }
        // 3. LEVER
        else if (domain.includes("jobs.lever.co")) {
            role = document.querySelector('.posting-headline h2')?.innerText;
            company = document.title.split('-')[0]; 
            jd_text = document.querySelector('.posting-details')?.innerText || document.querySelector('.section-wrapper')?.innerText;
        }
        // 4. WELLFOUND (Heuristic Sweep)
        else if (domain.includes("wellfound.com") || domain.includes("angel.co")) {
            role = document.querySelector('h2')?.innerText || document.querySelector('h1')?.innerText;
            company = document.querySelector('h1')?.innerText;
            if (!company || company === role) {
                company = document.title.split(' at ')[1] || document.title.split(' | ')[0];
            }
            const mainContent = document.querySelector('main') || document.body;
            const textElements = mainContent.querySelectorAll('p, ul > li');
            let combinedText = "";
            textElements.forEach(el => {
                if (el.innerText.length > 30) combinedText += el.innerText + "\n";
            });
            jd_text = combinedText.substring(0, 4000);
        }
        // 5. GENERIC FALLBACK
        else {
            role = document.querySelector('h1')?.innerText; 
            company = document.title.split(/[-|]/)[0]; 
            
            const allDivs = document.querySelectorAll('div, section, article');
            let largestText = "";
            allDivs.forEach(div => {
                const text = div.innerText;
                if (text && text.length > largestText.length && text.length < 10000) {
                    largestText = text;
                }
            });
            jd_text = largestText.substring(0, 4000);
        }
    } catch (error) {
        console.error("Extraction error:", error);
    }

    return {
        company: company ? company.trim() : "Unknown Company",
        role: role ? role.trim() : "Unknown Role",
        jd_text: jd_text ? jd_text.trim() : "JD not found",
        link: url
    };
}

// Listen for the popup asking for data
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === "extract_job") {
        const jobData = extractJobData();
        sendResponse(jobData);
    }
});