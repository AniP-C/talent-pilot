"""Deterministic scoring of a resume against a job description.

The model classifies; this module scores.

Asking a language model for a 0-100 match percentage with no rubric produces a
number that reflects its disposition rather than the evidence — in practice
almost everything came back in the high eighties, which makes the figure carry
no information. Worse, it could contradict the model's own summary: an analysis
that listed a dozen absent requirements still returned 90%. A score nobody can
reproduce or explain is not a measurement.

So ``analyze_jd`` now asks only for the judgement a model is genuinely good at
— is this requirement demonstrated, partially demonstrated, or absent — and the
arithmetic happens here, where it is reproducible for the same input,
explainable line by line, and testable without a network call. Same division of
labour as everywhere else in this codebase: judgement from the model,
consequences from Python.

Two scores are reported, because "would a recruiter think I can do this job?"
and "will an automated filter surface me at all?" are different questions:

* **Requirement coverage** — weighted over what the job actually asks for,
  distinguishing must-haves from nice-to-haves.
* **Keyword coverage** — whether the literal terms appear at all. No AI. Plenty
  of real applicant tracking systems and recruiter searches are string
  matching, so a resume can read well to a person and still never reach one.
"""

import re

import posting
from config import logger

# A must-have carries four times the weight of a nice-to-have. Missing
# "experience with FinTech, preferred" is not the same failure as missing the
# programming language the job is written in, and scoring them alike was one of
# the ways the old number misled.
REQUIRED_WEIGHT = 0.8
PREFERRED_WEIGHT = 0.2

# Adjacent evidence — a different cloud provider, the technique without the
# named tool — is worth something and is not worth full marks.
PARTIAL_CREDIT = 0.5

_STATUS_CREDIT = {"demonstrated": 1.0, "partial": PARTIAL_CREDIT, "absent": 0.0}

VALID_IMPORTANCE = ("required", "preferred")
VALID_STATUS = tuple(_STATUS_CREDIT)

# What kind of thing a requirement is.
#
# A job description does not only ask for skills. It also names the employer's
# own programmes and systems, and nobody outside that employer can evidence
# one. A real Barclays advert opened with "you will be required to have an
# experience in SOLD Simplification" — an internal multi-year initiative. It
# was extracted as a must-have, sat alongside four others, and removed sixteen
# points from the score of a candidate who could not have had it and could not
# do anything about it. The gap list then told them to go and get it.
#
# Classifying is better than filtering by keyword: there is no vocabulary of
# every company's internal jargon, but "is this transferable between employers"
# is a judgement a model makes reliably.
SKILL = "skill"                        # transferable: a tool, platform or technique
DOMAIN = "domain"                      # industry experience — payments, healthcare
EMPLOYER_INTERNAL = "employer_internal"  # this employer's own systems and programmes
META = "meta"                          # behavioural or process: "stakeholder management"

VALID_KINDS = (SKILL, DOMAIN, EMPLOYER_INTERNAL, META)

# Domain experience stays in: it is a real thing a candidate either has or does
# not, and hiding it would flatter the score.
#
# Meta was in, on the reasoning that vague is not the same as impossible. One
# real advert settled it. Barclays closes every posting with "you may be
# assessed on the key critical skills relevant for success in role, such as
# risk and controls, change and transformation, business acumen strategic
# thinking and digital and technology" — five requirements, on every job they
# advertise, and the model marked all five absent, along with "secure coding
# practices" and "effective unit testing practices". Seven of the twelve
# must-haves were phrases no resume contains, so the score was measuring how
# little a CV reads like an HR competency framework.
#
# A category that comes back absent for everybody is not a measurement, it is a
# constant. These are reported instead as what the employer says it will also
# assess, which is what they are actually useful for.
SCOREABLE_KINDS = (SKILL, DOMAIN)

# Below this many scoreable requirements, a percentage is arithmetic over
# almost no evidence and should not be presented as a measurement.
MIN_MEANINGFUL_REQUIREMENTS = 4

# And below this many recognised technical terms, the *posting* has not said
# enough to be scored against, whatever the model returned for it.
#
# This guard is deterministic on purpose, because the failure it catches is the
# model's. Handed four sentences of culture — "you'll work across the stack,
# wear many hats" — it read the requirements off the *resume* instead, marked
# all twenty-two demonstrated, and returned 100%. Telling somebody they are a
# perfect match for a posting that asked for nothing is the most misleading
# output this tool can produce, and a rule in the prompt only mostly stops it.
MIN_TERMS_IN_POSTING = 3


def terms_named(jd_text: str) -> int:
    """How many recognised technical terms a posting actually names.

    Nothing to do with any resume — this measures the posting alone, which is
    what makes it usable as a check on whatever the model came back with.
    """
    jd = _normalise(posting.trim_to_description(jd_text or ""))

    return sum(
        1
        for spellings in SKILL_VOCABULARY.values()
        if any(_mentions(jd, spelling) for spelling in spellings)
    )


# =====================================================================
# REQUIREMENT COVERAGE
# =====================================================================
def score_requirements(requirements: list[dict]) -> dict:
    """Weighted coverage of a job's stated requirements, as a 0-100 integer.

    Returns the score alongside the counts it was derived from, so the number
    can be shown with its working rather than asserted.

    When a job states only must-haves — which is common — the required side
    takes the whole weight instead of capping the achievable score at 80.

    Requirements naming the employer's own systems, and the competency-framework
    phrases every corporate posting closes with, are set aside rather than
    scored — see SCOREABLE_KINDS. They are returned in ``not_scored`` so the UI
    can still show them: "they will also assess X" is useful, and silently
    dropping a stated requirement would be its own kind of dishonesty.
    """
    scoreable = [
        r for r in requirements if r.get("kind", SKILL) in SCOREABLE_KINDS
    ]
    not_scored = [
        r for r in requirements if r.get("kind", SKILL) not in SCOREABLE_KINDS
    ]

    required = [r for r in scoreable if r.get("importance") == "required"]
    preferred = [r for r in scoreable if r.get("importance") == "preferred"]

    required_ratio = _coverage(required)
    preferred_ratio = _coverage(preferred)

    if required and preferred:
        score = REQUIRED_WEIGHT * required_ratio + PREFERRED_WEIGHT * preferred_ratio
    elif required:
        score = required_ratio
    elif preferred:
        score = preferred_ratio
    else:
        # Nothing left to score. Either the model classified nothing, or
        # everything it found was the employer's own machinery — a posting that
        # asks only for things no outsider can have. Reporting 0 would read as a
        # terrible match rather than as an analysis that did not happen, so the
        # caller is told.
        return {
            "score": 0,
            "scored": False,
            "required_total": 0,
            "required_met": 0.0,
            "preferred_total": 0,
            "preferred_met": 0.0,
            "not_scored": not_scored,
            "thin": True,
        }

    return {
        "score": round(score * 100),
        "scored": True,
        "required_total": len(required),
        "required_met": round(required_ratio * len(required), 1),
        "preferred_total": len(preferred),
        "preferred_met": round(preferred_ratio * len(preferred), 1),
        # Stated by the job, deliberately kept out of the arithmetic.
        "not_scored": not_scored,
        # Half a dozen requirements can carry a percentage. Two cannot: one
        # missing requirement out of two is 50%, and it reads as a considered
        # judgement rather than as arithmetic over almost no evidence. Plenty of
        # postings are three lines of culture and a "get in touch", and the
        # honest answer there is that there is not enough to score.
        "thin": len(scoreable) < MIN_MEANINGFUL_REQUIREMENTS,
    }


def _coverage(requirements: list[dict]) -> float:
    """Mean credit across a group, 0.0-1.0. An empty group scores 0."""
    if not requirements:
        return 0.0

    earned = sum(
        _STATUS_CREDIT.get(str(r.get("status", "")).lower(), 0.0) for r in requirements
    )
    return earned / len(requirements)


def normalise_requirements(requirements) -> list[dict]:
    """Drop anything the model returned that is not a usable requirement.

    A malformed entry must not silently score as absent and drag the result
    down; it is not evidence of anything.
    """
    if not isinstance(requirements, list):
        return []

    cleaned = []
    seen = set()

    for entry in requirements:
        if not isinstance(entry, dict):
            continue

        skill = str(entry.get("skill", "")).strip()
        if not skill:
            continue

        # The model is told not to list one skill twice under different names,
        # but an exact repeat is cheap to catch here and would otherwise weight
        # that requirement twice.
        key = skill.casefold()
        if key in seen:
            continue
        seen.add(key)

        importance = str(entry.get("importance", "")).strip().lower()
        status = str(entry.get("status", "")).strip().lower()
        kind = str(entry.get("kind", "")).strip().lower()

        cleaned.append(
            {
                "skill": skill,
                # An unrecognised importance is treated as a must-have: assuming
                # something is optional is the assumption that inflates a score.
                "importance": importance if importance in VALID_IMPORTANCE else "required",
                "status": status if status in VALID_STATUS else "absent",
                # An unrecognised kind is scored, for the same reason: the
                # default must never be the one that quietly removes a
                # requirement from the denominator. Excluding something takes a
                # deliberate classification.
                "kind": kind if kind in VALID_KINDS else SKILL,
                "evidence": str(entry.get("evidence", "")).strip(),
            }
        )

    return cleaned


# =====================================================================
# KEYWORD COVERAGE
# =====================================================================
# A curated vocabulary rather than words pulled out of the job description.
#
# Extracting keywords from arbitrary prose is how a comparison tool ends up
# reporting fifty "keywords" that are really twenty skills plus "collaborate",
# "solutions" and "best practices" — and counting "LLM" as matched while
# counting "Large Language Models" as missing. Both mistakes are structurally
# impossible here: only listed terms are ever considered, and every spelling of
# one skill lives under a single canonical name.
#
# The cost is that a term absent from this table is invisible. That is a
# visible, one-line-to-fix limitation, which is the better failure.
#
# Each entry is  canonical -> every spelling that means it.
SKILL_VOCABULARY: dict[str, list[str]] = {
    # --- languages ---
    "Python": ["python"],
    "JavaScript": ["javascript", "js"],
    "TypeScript": ["typescript"],
    "Java": ["java"],
    "C++": ["c++"],
    "C#": ["c#", ".net"],
    # Bare "Go" is deliberately absent: it matches the English verb, and a false
    # match inflates the very number this module exists to make honest.
    "Go": ["golang", "go lang"],
    "Rust": ["rust"],
    "Ruby": ["ruby"],
    "PHP": ["php"],
    "Scala": ["scala"],
    "Kotlin": ["kotlin"],
    "Swift": ["swift"],
    "SQL": ["sql"],
    "Bash": ["bash", "shell scripting"],

    # --- AI / ML ---
    # Found by unknown_terms() on its first run against a real posting, which
    # is exactly the job that function exists to do.
    "Generative AI": ["generative ai", "genai", "gen ai"],
    "LLM": ["llm", "llms", "large language model", "large language models"],
    "RAG": ["rag", "retrieval augmented generation", "retrieval-augmented generation"],
    "Prompt Engineering": ["prompt engineering", "prompting"],
    "Fine-tuning": ["fine-tuning", "fine tuning", "finetuning"],
    "LLMOps": ["llmops"],
    "MLOps": ["mlops"],
    "OpenAI": ["openai", "gpt-4", "gpt4"],
    "Anthropic Claude": ["claude", "anthropic"],
    "Hugging Face": ["hugging face", "huggingface"],
    "LangChain": ["langchain"],
    # Added after a real posting named LangGraph, the candidate's resume named
    # it five times, and neither number could see it: the term was simply not
    # in this table. An absent term is invisible, which is fine as a limitation
    # and expensive when it is the strongest evidence either document contains.
    "LangGraph": ["langgraph"],
    "LlamaIndex": ["llamaindex", "llama index"],
    "Semantic Kernel": ["semantic kernel"],
    "Spring AI": ["spring ai"],
    "LangChain4j": ["langchain4j"],
    "Ollama": ["ollama"],
    "LLM Evaluation": ["llm evaluation", "llm-as-a-judge", "evaluator agent", "eval harness"],
    "Guardrails": ["guardrail", "guardrails"],
    "Responsible AI": ["responsible ai", "ai safety", "ai ethics"],
    "Embeddings": ["embedding", "embeddings"],
    "Vector Database": ["vector database", "vector databases", "vector store", "vector search"],
    "Pinecone": ["pinecone"],
    "Weaviate": ["weaviate"],
    "pgvector": ["pgvector"],
    "FAISS": ["faiss"],
    "Chroma": ["chromadb", "chroma db"],
    "PyTorch": ["pytorch"],
    "TensorFlow": ["tensorflow"],
    "scikit-learn": ["scikit-learn", "sklearn", "scikit learn"],
    "NLP": ["nlp", "natural language processing"],
    "Computer Vision": ["computer vision"],
    "Machine Learning": ["machine learning"],
    "Deep Learning": ["deep learning"],
    "Agents": [
        "ai agent", "ai agents", "agentic",
        # A posting that says "agentic AI workflows" and a resume that says
        # "multi-agent orchestration" are talking about the same thing.
        "multi-agent", "multi agent", "agent orchestration",
    ],
    "MCP": ["model context protocol"],

    # --- cloud ---
    "AWS": ["aws", "amazon web services"],
    "Azure": ["azure"],
    "GCP": ["gcp", "google cloud", "google cloud platform"],
    "Docker": ["docker", "containerisation", "containerization"],
    "Kubernetes": ["kubernetes", "k8s"],
    "Terraform": ["terraform"],
    "Serverless": ["serverless", "lambda function", "aws lambda"],
    "DevSecOps": ["devsecops", "devops"],
    "CI/CD": [
        "ci/cd", "cicd", "ci cd",
        "continuous integration", "continuous delivery", "continuous deployment",
    ],
    "GitHub Actions": ["github actions"],
    "Jenkins": ["jenkins"],

    # --- backend / web ---
    "REST API": ["rest api", "restful", "rest apis"],
    "GraphQL": ["graphql"],
    "gRPC": ["grpc"],
    "Microservices": ["microservice", "microservices"],
    "Event-Driven Architecture": [
        "event-driven", "event driven", "event-driven architecture",
    ],
    "NoSQL": ["nosql", "no-sql"],
    "Node.js": ["node.js", "nodejs"],
    "React": ["react", "react.js", "reactjs"],
    "Next.js": ["next.js", "nextjs"],
    "Vue": ["vue", "vue.js", "vuejs"],
    "Angular": ["angular"],
    "Django": ["django"],
    "Flask": ["flask"],
    "FastAPI": ["fastapi"],
    "Express": ["express.js", "expressjs"],
    "Spring": ["spring boot", "spring framework"],

    # --- data ---
    "PostgreSQL": ["postgresql", "postgres"],
    "MySQL": ["mysql"],
    "MongoDB": ["mongodb", "mongo db"],
    "Redis": ["redis"],
    "Elasticsearch": ["elasticsearch", "elastic search"],
    "Kafka": ["kafka"],
    "Spark": ["apache spark", "pyspark"],
    "Airflow": ["airflow"],
    "Snowflake": ["snowflake"],
    "dbt": ["dbt"],
    "ETL": ["etl", "elt"],
    "Data Pipeline": ["data pipeline", "data pipelines"],
    "Data Warehouse": ["data warehouse", "data warehousing"],

    # --- practice ---
    "Observability": ["observability", "monitoring", "telemetry"],
    "Prometheus": ["prometheus"],
    "Grafana": ["grafana"],
    "Datadog": ["datadog"],
    "Unit Testing": ["unit test", "unit testing", "unit tests"],
    "TDD": ["tdd", "test driven development", "test-driven development"],
    "Agile": ["agile", "scrum"],
    "Git": ["git", "version control"],
    "Code Review": ["code review", "code reviews"],
    "System Design": ["system design", "distributed systems"],
    "Performance Optimisation": [
        "performance optimisation", "performance optimization",
        "performance tuning", "latency optimisation", "latency optimization",
    ],
    "Caching": ["caching", "cache layer"],
    "Security": [
        "security best practice", "security best practices",
        "application security", "data security", "secure coding",
    ],
    "Compliance": ["compliance", "gdpr", "soc 2", "soc2", "hipaa"],
    "Authentication": ["authentication", "oauth", "sso", "jwt"],

    # --- domain ---
    "FinTech": ["fintech", "financial technology"],
    # Distinct from FinTech: a bank's payments platform and a fintech startup
    # are different employers asking for different experience, and a posting
    # frequently names one without the other.
    "Payments": ["payments engineering", "payment systems", "payment processing"],
    "Banking": ["banking", "investment bank", "retail banking"],
    "SaaS": ["saas", "software as a service"],
    "E-commerce": ["e-commerce", "ecommerce"],
    "Healthcare": ["healthtech", "healthcare technology"],
}


# Where a technical term may begin and end.
#
# `\b` alone is wrong, because it treats "." as a boundary: `\bjs\b` matches
# the tail of "node.js", so a posting asking for Node.js also appeared to ask
# for JavaScript.
#
# But a dot cannot simply be excluded either, or "Python." at the end of a
# sentence stops matching. The distinction is whether the dot joins two parts
# of a name: "node.js" yes, "Python." no. Hence the second lookaround in each
# direction, which rejects a dot only when a word character sits on its far
# side.
_TERM_START = r"(?<![\w+#-])(?<!\w\.)"
_TERM_END = r"(?![\w+#-])(?!\.\w)"


def _mentions(haystack: str, phrase: str) -> bool:
    """True when ``phrase`` appears in ``haystack`` as a term, not a fragment.

    Keeps "Java" out of "JavaScript" and "SQL" out of "PostgreSQL", which are
    the false positives that would inflate the very number this module exists
    to make trustworthy.
    """
    pattern = f"{_TERM_START}{re.escape(phrase)}{_TERM_END}"
    return re.search(pattern, haystack) is not None


def _spans(haystack: str, phrase: str):
    """Every place ``phrase`` occurs as a whole term."""
    pattern = f"{_TERM_START}{re.escape(phrase)}{_TERM_END}"
    return [(match.start(), match.end()) for match in re.finditer(pattern, haystack)]


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


# =====================================================================
# ALTERNATIVES
# =====================================================================
# "Strong full-stack engineering expertise using either Python or Java" is ONE
# requirement, and a candidate who writes Python does not have a Java gap.
#
# The requirement analysis has always known this — it is rule 2 of the prompt in
# ai/resume_parser.py, and it is there because inventing gaps the posting never
# asked for is the single most common way an assessment goes wrong. The keyword
# pass did not know it, so the same posting was read two incompatible ways: one
# Barclays advert produced four "missing" terms (Java, Django, Spring,
# LlamaIndex) that the candidate satisfied through the stated alternative, and
# counted React, Angular and TypeScript as three gaps where the posting offers
# one choice.
#
# Nothing but text separates the members of an alternation, so this reads the
# separators. Everything hangs on the difference between "A, B or C" — pick one
# — and "A, B and C" — bring all three.

# What may sit between two terms without breaking the run: commas, slashes,
# whitespace, and the word "or". Anything else — a noun, a verb, a full stop —
# ends it.
_SEPARATOR_RE = re.compile(r"^[\s,/]*(?:or|and/or)?[\s,/]*$")

# A separator carrying an explicit choice.
_DISJUNCTIVE_RE = re.compile(r"\bor\b|/")

# Beyond this, two terms are not in the same list however the text reads.
_MAX_GAP_CHARS = 40


def _alternation_groups(jd: str, present: dict[str, list[str]]) -> list[list[str]]:
    """Partition the terms a posting mentions into requirements.

    Each returned list is one requirement: several names when the posting
    offers a choice between them, one name otherwise. Order follows
    SKILL_VOCABULARY so the output is stable.
    """
    located: list[tuple[int, int, str]] = []
    for canonical, spellings in present.items():
        for spelling in spellings:
            located.extend(
                (start, end, canonical) for start, end in _spans(jd, spelling)
            )

    located.sort()

    # Terms that belong together, built up as pairs and merged at the end. A
    # term can appear several times in one posting and join a different list
    # each time, which is why this cannot be a single pass.
    links: list[tuple[str, str]] = []
    run: list[tuple[str, str, bool]] = []  # (left, right, gap offered a choice)

    def close_run() -> None:
        # The whole run is a choice only if its LAST separator was one. This is
        # what keeps "microservices, REST APIs, event-driven architecture,
        # authentication and enterprise integration patterns" intact: it is a
        # list of things to bring, not a menu, and its final connector says so.
        if run and run[-1][2]:
            links.extend((left, right) for left, right, _ in run)
        run.clear()

    for (_, prev_end, left), (start, _, right) in zip(located, located[1:]):
        gap = jd[prev_end:start]

        if (
            left == right
            or len(gap) > _MAX_GAP_CHARS
            or not _SEPARATOR_RE.match(gap)
        ):
            close_run()
            continue

        run.append((left, right, bool(_DISJUNCTIVE_RE.search(gap))))

    close_run()

    # Merge into connected components: "Python or Java" and "FastAPI/Django or
    # Java with Spring Boot" name Java in both, and one requirement satisfied
    # by any of those four is the honest reading.
    component: dict[str, str] = {name: name for name in present}

    def root(name: str) -> str:
        while component[name] != name:
            component[name] = component[component[name]]
            name = component[name]
        return name

    for left, right in links:
        left_root, right_root = root(left), root(right)
        if left_root != right_root:
            component[left_root] = right_root

    grouped: dict[str, list[str]] = {}
    for canonical in present:
        grouped.setdefault(root(canonical), []).append(canonical)

    # Sorted by where each group's first member sits in the vocabulary, so the
    # result does not depend on dictionary iteration order changing.
    order = list(SKILL_VOCABULARY)
    return sorted(grouped.values(), key=lambda names: order.index(names[0]))


def keyword_coverage(jd_text: str, resume_text: str) -> dict:
    """Which technical terms the job names that the resume literally contains.

    No AI, no network, and the same answer every time. This is the pass that
    approximates an automated filter, which does not care that Azure experience
    transfers to AWS — it cares whether the string "AWS" is on the page.

    Counted in requirements rather than in words: where the posting offers a
    choice, the alternatives are one entry satisfied by any of them. A resume
    with Python has no Java gap on a job advertised as "either Python or Java",
    and reporting one both understates the candidate and sends them off to add
    a language the employer never asked them for.
    """
    # The page, minus the careers-page furniture that follows the job. A
    # marketing word cloud of every technology the employer uses anywhere put
    # C++, C# and Kotlin into one candidate's "terms this posting uses that you
    # lack", for a role that asks for none of them.
    jd = _normalise(posting.trim_to_description(jd_text))
    resume = _normalise(resume_text)

    present = {
        canonical: spellings
        for canonical, spellings in SKILL_VOCABULARY.items()
        if any(_mentions(jd, spelling) for spelling in spellings)
    }

    matched: list[str] = []
    missing: list[str] = []

    for group in _alternation_groups(jd, present):
        have = [
            canonical
            for canonical in group
            if any(_mentions(resume, spelling) for spelling in present[canonical])
        ]

        if have:
            # Named by what the resume actually says, not by the whole
            # alternation: "LangChain / LangGraph" is the useful thing to show
            # under "terms you already have".
            matched.append(" / ".join(have))
        else:
            missing.append(" / ".join(group))

    total = len(matched) + len(missing)

    return {
        "score": round(len(matched) / total * 100) if total else 0,
        "scored": total > 0,
        # Every caller reads these as "requirements met" and "requirements
        # not met", which is what they now are. The lists still add up to
        # total, so nothing downstream had to change.
        "matched": matched,
        "missing": missing,
        "total": total,
    }


# =====================================================================
# WHAT THE VOCABULARY HAS NEVER HEARD OF
# =====================================================================
# A term absent from SKILL_VOCABULARY is invisible: the posting can ask for it,
# the resume can be built on it, and neither number moves. That is a defensible
# trade only while somebody notices the misses — and nothing reported them, so
# LangGraph sat missing for as long as it took a person to read a bad result
# and go digging.
#
# This is the noticing. It cannot know what is a technology, so it does not
# guess: it reports capitalised or symbol-bearing tokens the vocabulary does
# not cover, and a human decides which are worth adding.

# Words that are capitalised for grammar rather than because they are products.
_NOT_A_TECHNOLOGY = {
    "the", "this", "that", "these", "those", "you", "your", "we", "our", "us",
    "it", "its", "they", "their", "as", "and", "or", "but", "if", "for", "with",
    "will", "would", "should", "can", "may", "must", "have", "has", "are", "is",
    "be", "been", "role", "roles", "job", "jobs", "team", "teams", "work",
    "working", "experience", "experienced", "skills", "skill", "years", "year",
    "join", "apply", "applying", "please", "about", "purpose", "location",
    "responsibilities", "requirements", "qualifications", "benefits", "salary",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december",
    "engineer", "engineering", "developer", "development", "manager", "senior",
    "junior", "lead", "principal", "staff", "director", "head", "vice",
    "president", "officer", "analyst", "specialist", "consultant", "architect",
    "hybrid", "remote", "onsite", "office", "full", "part", "time", "permanent",
    "contract", "internship", "graduate", "candidate", "candidates", "applicant",
}

# A technology token: capitalised, or carrying the punctuation that marks a
# product name. Two characters minimum, because initials are noise.
_CANDIDATE_TERM_RE = re.compile(r"\b[A-Za-z][\w.+#/-]{1,24}\b")

# Requisition numbers and other per-advert reference codes.
_REFERENCE_CODE_RE = re.compile(r"^[a-z]{1,4}[-_]?\d{3,}$")


def unknown_terms(jd_text: str, limit: int = 25) -> list[str]:
    """Terms a posting uses that the vocabulary cannot see, most frequent first.

    Reported rather than acted on. Adding to the vocabulary is a judgement —
    "Titans" and "Volta" are real words in one Barclays page and neither is a
    skill anybody hires for — so this produces a list to review, not a change.
    """
    text = posting.trim_to_description(jd_text or "")
    lowered = _normalise(text)

    covered = {
        spelling
        for spellings in SKILL_VOCABULARY.values()
        for spelling in spellings
    }

    counts: dict[str, int] = {}

    for match in _CANDIDATE_TERM_RE.finditer(text):
        token = match.group(0)
        key = token.lower().strip(".")

        if key in _NOT_A_TECHNOLOGY or key in covered or len(key) < 2:
            continue

        # A requisition number is capitalised, unique to one advert, and never
        # a skill: JR-0000111867.
        if _REFERENCE_CODE_RE.match(key):
            continue

        # "FastAPI/Django" is two terms the vocabulary already knows, joined by
        # the posting's own punctuation — not something nobody has heard of.
        parts = [part for part in re.split(r"[/+]", key) if part]
        if len(parts) > 1 and all(part in covered for part in parts):
            continue

        # Capitalised mid-sentence, or carrying product punctuation. A word at
        # the start of a sentence is capitalised by grammar alone, so it only
        # qualifies on the punctuation test.
        starts_sentence = match.start() == 0 or text[match.start() - 1] in ".!?\n"
        looks_technical = any(ch in token for ch in "+#/.") or (
            token[0].isupper() and not starts_sentence
        )

        if not looks_technical:
            continue

        # Already matched by a multi-word vocabulary entry, e.g. "Semantic" in
        # "Semantic Kernel", which is covered and should not be reported.
        if any(spelling in lowered and key in spelling for spelling in covered):
            continue

        counts[token] = counts.get(token, 0) + 1

    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [term for term, _ in ordered[:limit]]


def drop_alternatives_already_met(
    requirements: list[dict], jd_text: str, resume_text: str
) -> list[dict]:
    """Remove requirements the posting offered as one option among several,
    where the resume has one of the others.

    The keyword pass groups alternatives deterministically. The requirement
    pass asks the model to, and the model mostly does — but "either Python or
    Java" came back as a separate absent "Java" on one run in three, and one
    run in three is enough to tell somebody to go and learn a language the
    employer explicitly did not ask them for.

    Deliberately narrow. A requirement is only dropped when the posting itself
    put it in an alternation, the resume satisfies another member of that same
    alternation, and the entry is marked absent. Anything else is left exactly
    as the model classified it.
    """
    if not requirements or not resume_text:
        return requirements

    jd = _normalise(posting.trim_to_description(jd_text or ""))
    resume = _normalise(resume_text)

    present = {
        canonical: spellings
        for canonical, spellings in SKILL_VOCABULARY.items()
        if any(_mentions(jd, spelling) for spelling in spellings)
    }

    # Every spelling of every term that is one option in a group the resume
    # already satisfies through a different option.
    covered_elsewhere: set[str] = set()

    for group in _alternation_groups(jd, present):
        if len(group) < 2:
            continue

        met = [
            canonical
            for canonical in group
            if any(_mentions(resume, spelling) for spelling in present[canonical])
        ]

        if not met:
            continue

        for canonical in group:
            if canonical in met:
                continue
            covered_elsewhere.add(canonical.casefold())
            covered_elsewhere.update(
                spelling.casefold() for spelling in present[canonical]
            )

    if not covered_elsewhere:
        return requirements

    kept = []

    for entry in requirements:
        skill = entry.get("skill", "").strip().casefold()

        # Only an exact naming of the alternative. "Java" goes; "Java-based
        # event streaming" is its own requirement and stays.
        if entry.get("status") == "absent" and skill in covered_elsewhere:
            logger.debug(
                "Dropped %r: the posting offered it as an alternative the resume meets",
                entry.get("skill"),
            )
            continue

        kept.append(entry)

    return kept
