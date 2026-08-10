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


# =====================================================================
# REQUIREMENT COVERAGE
# =====================================================================
def score_requirements(requirements: list[dict]) -> dict:
    """Weighted coverage of a job's stated requirements, as a 0-100 integer.

    Returns the score alongside the counts it was derived from, so the number
    can be shown with its working rather than asserted.

    When a job states only must-haves — which is common — the required side
    takes the whole weight instead of capping the achievable score at 80.
    """
    required = [r for r in requirements if r.get("importance") == "required"]
    preferred = [r for r in requirements if r.get("importance") == "preferred"]

    required_ratio = _coverage(required)
    preferred_ratio = _coverage(preferred)

    if required and preferred:
        score = REQUIRED_WEIGHT * required_ratio + PREFERRED_WEIGHT * preferred_ratio
    elif required:
        score = required_ratio
    elif preferred:
        score = preferred_ratio
    else:
        # Nothing classified. Reporting 0 would read as a terrible match rather
        # than as an analysis that did not happen, so the caller is told.
        return {
            "score": 0,
            "scored": False,
            "required_total": 0,
            "required_met": 0.0,
            "preferred_total": 0,
            "preferred_met": 0.0,
        }

    return {
        "score": round(score * 100),
        "scored": True,
        "required_total": len(required),
        "required_met": round(required_ratio * len(required), 1),
        "preferred_total": len(preferred),
        "preferred_met": round(preferred_ratio * len(preferred), 1),
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

        cleaned.append(
            {
                "skill": skill,
                # An unrecognised importance is treated as a must-have: assuming
                # something is optional is the assumption that inflates a score.
                "importance": importance if importance in VALID_IMPORTANCE else "required",
                "status": status if status in VALID_STATUS else "absent",
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
    "LlamaIndex": ["llamaindex", "llama index"],
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
    "Agents": ["ai agent", "ai agents", "agentic"],
    "MCP": ["model context protocol"],

    # --- cloud ---
    "AWS": ["aws", "amazon web services"],
    "Azure": ["azure"],
    "GCP": ["gcp", "google cloud", "google cloud platform"],
    "Docker": ["docker", "containerisation", "containerization"],
    "Kubernetes": ["kubernetes", "k8s"],
    "Terraform": ["terraform"],
    "Serverless": ["serverless", "lambda function", "aws lambda"],
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


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def keyword_coverage(jd_text: str, resume_text: str) -> dict:
    """Which technical terms the job names that the resume literally contains.

    No AI, no network, and the same answer every time. This is the pass that
    approximates an automated filter, which does not care that Azure experience
    transfers to AWS — it cares whether the string "AWS" is on the page.
    """
    jd = _normalise(jd_text)
    resume = _normalise(resume_text)

    matched: list[str] = []
    missing: list[str] = []

    for canonical, spellings in SKILL_VOCABULARY.items():
        if not any(_mentions(jd, spelling) for spelling in spellings):
            continue  # the job never asks for it

        if any(_mentions(resume, spelling) for spelling in spellings):
            matched.append(canonical)
        else:
            missing.append(canonical)

    total = len(matched) + len(missing)

    return {
        "score": round(len(matched) / total * 100) if total else 0,
        "scored": total > 0,
        "matched": matched,
        "missing": missing,
        "total": total,
    }
