"""Measure the model half of the analysis: how stable is it, and is it right?

The deterministic half — trimming, alternatives, the vocabulary, the
arithmetic — is pinned by ``tests/test_calibration.py`` and runs in CI for
free. This is the half that cannot: extraction and classification are one model
call, they cost money, and they do not return the same thing twice.

    python deploy/calibrate.py                 # every fixture, three runs each
    python deploy/calibrate.py --runs 5
    python deploy/calibrate.py --posting enterprise-bank
    python deploy/calibrate.py --resume /path/to/real-cv.pdf

Why it exists: the same real advert, scored three times against the same
resume, produced 12, 26 and 27 must-haves. That swing moves the headline
further than any single fix in this codebase, and until it is measured, every
prompt change is a guess with a number attached.

Each run costs one model call per posting. Nothing here writes to a workspace
or touches a user's data.
"""

import argparse
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scoring  # noqa: E402
import utils  # noqa: E402
from ai.resume_parser import analyze_jd  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
POSTINGS = FIXTURES / "postings"

# What must be true of a correct analysis, per fixture. These are the failures
# that were found against real adverts, written down so a change that
# reintroduces one is loud rather than silent.
EXPECTATIONS = {
    "enterprise-bank": {
        # The employer's own programme cannot be a gap: nobody outside has it.
        "not_scored_contains": ["LEDGER"],
        # Competency wording is not a skill and no resume is written in it.
        "never_a_gap": [
            "business acumen",
            "strategic thinking",
            "risk and controls",
            "change and transformation",
            "digital and technology",
        ],
        # A choice the resume satisfies must not be reported as missing.
        "never_a_gap_substring": ["Java", "Django", "LlamaIndex", "Semantic Kernel"],
        # And a real gap must still be found.
        "must_be_a_gap_substring": ["Kubernetes"],
    },
    "startup-backend": {
        "not_scored_contains": [],
        "never_a_gap": [],
        "never_a_gap_substring": [],
        "must_be_a_gap_substring": ["Kubernetes"],
    },
    "vague-generalist": {
        "not_scored_contains": [],
        "never_a_gap": [],
        "never_a_gap_substring": [],
        "must_be_a_gap_substring": [],
        # Four sentences of culture and no requirements. The failure this
        # catches: the model read the requirements off the resume, marked all
        # twenty-two demonstrated and returned 100%.
        "must_be_thin": True,
    },
}


def load_resume(path: str | None) -> str:
    """The resume text to score against. A PDF is read; a .txt is used as is."""
    if not path:
        return (FIXTURES / "synthetic-cv.txt").read_text(encoding="utf-8")

    source = Path(path)

    if source.suffix.lower() == ".pdf":
        with open(source, "rb") as handle:
            return utils.extract_pdf_text(handle)

    return source.read_text(encoding="utf-8")


def check(name: str, result: dict) -> list[str]:
    """Return the expectations this run broke, if any."""
    rules = EXPECTATIONS.get(name)
    if not rules:
        return []

    gaps = result.get("missing_skills") or []
    gaps_joined = " | ".join(gaps).lower()
    not_scored = " | ".join(
        entry["skill"] for entry in (result.get("coverage", {}).get("not_scored") or [])
    )

    broken = []

    for needle in rules["not_scored_contains"]:
        if needle.lower() not in not_scored.lower():
            broken.append(f"{needle!r} was not set aside as unscoreable")

    for phrase in rules["never_a_gap"]:
        if phrase.lower() in gaps_joined:
            broken.append(f"competency wording {phrase!r} was listed as a gap")

    for phrase in rules["never_a_gap_substring"]:
        if phrase.lower() in gaps_joined:
            broken.append(f"{phrase!r} was listed as a gap despite the stated choice")

    for phrase in rules["must_be_a_gap_substring"]:
        if phrase.lower() not in gaps_joined:
            broken.append(f"real gap {phrase!r} was not reported")

    if rules.get("must_be_thin") and not result.get("coverage", {}).get("thin"):
        broken.append(
            "a posting stating no requirements was presented as scoreable "
            f"({result.get('match_percentage')}%)"
        )

    return broken


def spread(values: list[float]) -> str:
    if not values:
        return "—"
    if len(values) == 1:
        return f"{values[0]:g}"

    return (
        f"{min(values):g}–{max(values):g}"
        f"  (mean {statistics.mean(values):.1f}, "
        f"sd {statistics.pstdev(values):.1f})"
    )


def run_fixture(name: str, resume: str, runs: int) -> dict:
    posting_text = (POSTINGS / f"{name}.txt").read_text(encoding="utf-8")

    scores, requirement_counts, must_have_counts, gap_counts = [], [], [], []
    failures: list[str] = []

    for attempt in range(runs):
        result = analyze_jd(posting_text, resume, resume)

        if "error" in result:
            print(f"  run {attempt + 1}: FAILED — {result.get('message', result)}")
            continue

        coverage = result["coverage"]
        scores.append(result["match_percentage"])
        requirement_counts.append(len(result["requirements"]))
        must_have_counts.append(coverage["required_total"])
        gap_counts.append(len(result["missing_skills"]))
        failures.extend(f"run {attempt + 1}: {problem}" for problem in check(name, result))

    return {
        "scores": scores,
        "requirements": requirement_counts,
        "must_haves": must_have_counts,
        "gaps": gap_counts,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3, help="Repeats per posting.")
    parser.add_argument("--posting", help="One fixture name instead of all of them.")
    parser.add_argument("--resume", help="A .pdf or .txt to score with.")
    args = parser.parse_args()

    resume = load_resume(args.resume)
    names = [args.posting] if args.posting else sorted(p.stem for p in POSTINGS.glob("*.txt"))

    print(f"\nCalibration · {args.runs} run(s) per posting · "
          f"resume {'supplied' if args.resume else 'fixture'}\n")

    broken_total = 0

    for name in names:
        print(f"{name}")
        outcome = run_fixture(name, resume, args.runs)

        print(f"  score         {spread(outcome['scores'])}")
        print(f"  requirements  {spread(outcome['requirements'])}")
        print(f"  must-haves    {spread(outcome['must_haves'])}")
        print(f"  gaps listed   {spread(outcome['gaps'])}")

        if outcome["failures"]:
            broken_total += len(outcome["failures"])
            print("  EXPECTATIONS BROKEN:")
            for problem in outcome["failures"]:
                print(f"    - {problem}")
        else:
            print("  expectations  all held")
        print()

    if broken_total:
        print(f"{broken_total} expectation(s) broken.\n")
        return 1

    print("Every expectation held.\n")
    print(
        "Read the spread, not just the mean. A must-have count that moves by\n"
        "more than a couple between identical runs means the headline is mostly\n"
        "reporting how the model felt about splitting bullets that day."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
