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

# Everything except the unwinnable. Domain experience stays in: it is a real
# thing a candidate either has or does not, and hiding it would flatter the
# score. Meta stays in for the same reason — vague is not the same as
# impossible.
SCOREABLE_KINDS = (SKILL, DOMAIN, META)


# =====================================================================
# REQUIREMENT COVERAGE
# =====================================================================
def score_requirements(requirements: list[dict]) -> dict:
    """Weighted coverage of a job's stated requirements, as a 0-100 integer.

    Returns the score alongside the counts it was derived from, so the number
    can be shown with its working rather than asserted.

    When a job states only must-haves — which is common — the required side
    takes the whole weight instead of capping the achievable score at 80.

    Requirements naming the employer's own systems are set aside rather than
    scored — see EMPLOYER_INTERNAL. They are returned in ``not_scored`` so the
    UI can still show them: "this job also wants X, and no outsider has it" is
    useful context, and silently dropping a stated requirement would be its own
    kind of dishonesty.
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
    jd = _normalise(jd_text)
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
