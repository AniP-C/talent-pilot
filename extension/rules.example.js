// Copy this file to extension/rules.js and replace the placeholder values.
// extension/rules.js is ignored by git because it may contain personal data.

const AUTOFILL_RULES = [
    {
        pattern: /authorized to work|eligibility to work|legally authorized/i,
        suggestion: "Yes"
    },
    {
        pattern: /require visa|need sponsorship|future sponsorship|employment sponsorship/i,
        suggestion: "No"
    },
    {
        pattern: /full name|legal name/i,
        suggestion: "Your Name"
    },
    {
        pattern: /email address|email/i,
        suggestion: "you@example.com"
    },
    {
        pattern: /phone|mobile|contact|cell/i,
        suggestion: "+10000000000"
    },
    {
        pattern: /current location|current city|location/i,
        suggestion: "Your City, Country"
    },
    {
        pattern: /linkedin profile|linkedin url/i,
        suggestion: "https://www.linkedin.com/in/your-profile/"
    },
    {
        pattern: /github profile|github url/i,
        suggestion: "https://github.com/your-username"
    },
    {
        pattern: /years of experience|total experience/i,
        suggestion: "Your experience"
    },
    {
        pattern: /technical skills|skills/i,
        suggestion: "Python, SQL, FastAPI"
    },
    {
        pattern: /cover letter/i,
        suggestion: "Available upon request"
    }
];
