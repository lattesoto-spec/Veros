"""Which payroll lines are time actually worked.

Payroll exports mix worked hours with leave, training and allowances on the
same sheet. Paid is not worked, so a line counts only when its category is
recognised as worked or a person has said so. Anything else is held back.
"""

from .fingerprint import normalize_header

WORKED = "worked"
EXCLUDED = "excluded"
UNKNOWN = "unknown"
BLANK = "(blank)"

PAY_COLUMNS = {
    "pay_code", "pay_type", "pay_category", "pay_item", "pay_rule", "pay_class",
    "earnings_code", "earnings_type", "earning_code", "earning_type", "wage_type",
    "leave_type", "absence_type", "activity_type", "time_type", "hours_type",
}

# Exact, deliberately short: a near match is a question for a person.
WORKED_CATEGORIES = {
    "ordinary", "ordinary hours", "ordinary time", "normal", "normal hours",
    "regular", "base", "base hours", "worked", "worked hours", "overtime", "ot",
    "time and a half", "double time", "penalty", "saturday", "sunday",
    "public holiday worked", "night", "night shift", "afternoon", "afternoon shift",
    "evening", "evening shift", "weekend",
}

NON_WORKED_TERMS = (
    "leave", "lsl", "annual", "personal", "sick", "carer", "compassionate", "bereavement",
    "parental", "maternity", "paternity", "workers comp", "workcover", "work cover",
    "training", "study", "induction", "allowance", "uniform", "laundry", "meal", "travel",
    "mileage", "on call", "on-call", "absence", "absent", "no show", "no-show",
    "not worked", "phnw", "toil", "backpay", "back pay", "bonus", "reimbursement",
)


def key(category) -> str:
    return " ".join(str(category or "").strip().lower().split())


def pay_column(headers) -> str | None:
    return next((h for h in headers if normalize_header(h) in PAY_COLUMNS), None)


def treatment(category, rules: dict) -> str:
    """A recorded decision wins; otherwise only an exact worked name counts."""
    name = key(category)
    if name in rules:
        return rules[name]
    if name in WORKED_CATEGORIES:
        return WORKED
    if any(term in name for term in NON_WORKED_TERMS):
        return EXCLUDED
    return UNKNOWN


def split_records(records, rules, minutes_of):
    """Return worked records, and a per-category summary of every line seen."""
    kept, summary = [], {}
    for record in records:
        if "pay_category" not in record:
            kept.append(record)
            continue
        label = str(record.get("pay_category") or "").strip() or BLANK
        decision = treatment("" if label == BLANK else label, rules)
        entry = summary.setdefault(
            label, {"category": label, "treatment": decision, "rows": 0, "minutes": 0}
        )
        entry["rows"] += 1
        entry["minutes"] += int(minutes_of(record) or 0)
        if decision == WORKED:
            kept.append(record)
    return kept, sorted(summary.values(), key=lambda item: item["category"].lower())
