"""Who counts toward care minutes, and on what basis.

Only registered nurses, enrolled nurses and personal care workers / assistants
in nursing contribute to the care-minutes targets. Allied health, lifestyle,
diversional therapy, care management, catering, cleaning and administration do
not — and cannot be made to count by marking a roster row "direct care".

Two rules follow, and both are deliberately conservative:

1. A role only counts if it maps to an eligible bucket by a *specific* term.
   Broad words like "assistant" or "support" appear in "allied health
   assistant" and "social support worker" as readily as in "care assistant",
   so a match on those alone is not evidence of eligibility.
2. Anything unresolved is excluded from the calculation and raised as an
   exception for a human to approve. In a regulated number, an unknown row
   must never quietly inflate the result.
"""

import re

# Reporting buckets. The mapping engine normalises to PCW; the government
# statement calls the same bucket PCA.
ELIGIBLE_BUCKETS = ("RN", "EN", "PCA")

BUCKETS = {
    "RN": "RN", "EN": "EN", "EEN": "EN",
    "PCW": "PCA", "PCA": "PCA", "AIN": "PCA",
}

# Approved automatically: unambiguous in aged care.
DEFINITIVE = {
    "RN": ("registered nurse", "clinical nurse", "nurse practitioner",
           "rn", "cns", "cnc", "cnm", "nurse unit manager"),
    "EN": ("enrolled nurse", "endorsed enrolled nurse", "en", "een"),
    "PCA": ("personal care worker", "personal care assistant", "personal carer",
            "assistant in nursing", "care service employee", "pcw", "pca", "ain",
            "care worker", "careworker"),
}

# Never eligible, whatever a "direct care" flag claims.
INELIGIBLE = (
    "allied health", "physio", "occupational therap", "podiatr", "dietit",
    "speech", "exercise physiolog", "lifestyle", "diversional", "recreation",
    "social support", "social work", "pastoral", "chaplain",
    "care manager", "care management", "facility manager", "clinical manager",
    "administration", "admin", "reception", "roster", "payroll",
    "clean", "laundry", "catering", "chef", "cook", "kitchen", "hospitality",
    "maintenance", "garden", "driver", "volunteer", "student", "trainee",
)

# Statuses on Staff.eligibility_status
APPROVED, PENDING, EXCLUDED = "approved", "pending", "excluded"


def bucket_for(role: str) -> str:
    """Normalised role -> reporting bucket, or "OTHER"."""
    return BUCKETS.get((role or "").strip().upper(), "OTHER")


def classify(raw_role: str) -> tuple[str, str, str]:
    """Assess a raw role title.

    Returns (bucket, status, reason). `bucket` is "OTHER" when the title does
    not resolve to an eligible category. Callers must not count anything whose
    status is not APPROVED.
    """
    text = (raw_role or "").strip().lower()
    if not text:
        return "OTHER", PENDING, "no role recorded"

    for term in INELIGIBLE:
        if term in text:
            return "OTHER", EXCLUDED, f"not a direct-care role ({term})"

    for bucket, terms in DEFINITIVE.items():
        tokens = set(re.findall(r"[a-z]+", text))
        for term in terms:
            if (" " in term and term in text) or (" " not in term and term in tokens):
                return bucket, APPROVED, f"matched {term!r}"

    # Broad words suggest care work but are shared with ineligible titles, so
    # they need a human decision rather than an assumption.
    if any(w in text for w in ("assistant", "aide", "carer", "support", "care")):
        return "OTHER", PENDING, "ambiguous title — needs approval before it counts"

    return "OTHER", PENDING, "unrecognised role"


def counts_toward_care(staff, on_day=None) -> bool:
    """Whether this staff member's minutes may be included."""
    if staff is None:
        return False
    if getattr(staff, "eligibility_status", PENDING) != APPROVED:
        return False
    if bucket_for(staff.role) not in ELIGIBLE_BUCKETS:
        return False
    if on_day is not None:
        start = getattr(staff, "eligible_from", None)
        end = getattr(staff, "eligible_to", None)
        if start and on_day < start:
            return False
        if end and on_day > end:
            return False
    return True


def registration_problem(staff) -> str | None:
    """Nurses must hold a current registration for their minutes to be safe to
    report. Returns a description when that cannot be evidenced."""
    bucket = bucket_for(staff.role)
    if bucket not in ("RN", "EN"):
        return None
    if not (staff.registration_number or "").strip():
        return f"{bucket} has no Ahpra registration number recorded"
    return None
