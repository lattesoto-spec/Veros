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
    "ordinary", "ord", "ordinary hours", "ordinary time", "normal", "normal hours",
    "regular", "base", "base hours", "worked", "worked hours", "overtime", "ot",
    "time and a half", "double time", "penalty", "saturday", "sunday",
    "public holiday worked", "night", "night shift", "afternoon", "afternoon shift",
    "evening", "evening shift", "weekend",
}

NON_WORKED_TERMS = (
    "leave", "lsl", "annual", "sick", "compassionate", "bereavement",
    "parental", "maternity", "paternity", "workers comp", "workcover", "work cover",
    "training", "study", "induction", "allowance", "uniform", "laundry", "meal", "travel",
    "mileage", "on call", "on-call", "absence", "absent", "no show", "no-show",
    "not worked", "phnw", "toil", "backpay", "back pay", "bonus", "reimbursement",
)


def key(category) -> str:
    return " ".join(str(category or "").strip().lower().split())


# Terms that identify a category column by its values. Narrower than the list
# above: "travel" or "meal" could appear in an unrelated column.
_SHORT_CODES = {"al", "sl", "pl", "pcl", "lsl", "wc", "cl", "lwop", "phnw", "rdo"}
_VALUE_SIGNALS = (
    "leave", "lsl", "sick", "workers comp", "workcover", "training", "allowance",
    "absence", "absent", "compassionate", "bereavement", "parental",
)
_MAX_CATEGORIES = 40


def pay_column(headers, rows=(), mapped=()) -> str | None:
    """Find a pay column by name, then by content.

    Header names vary too much to rely on, and missing one would count leave
    as worked. Any low-variety text column holding leave-like values qualifies.
    """
    named = next((h for h in headers if normalize_header(h) in PAY_COLUMNS), None)
    if named:
        return named
    for header in headers:
        if header in mapped:
            continue
        values = {key(row.get(header)) for row in rows} - {""}
        if not values or len(values) > _MAX_CATEGORIES:
            continue
        if values & _SHORT_CODES or any(
            signal in value for value in values for signal in _VALUE_SIGNALS
        ):
            return header
    return None


def candidate_columns(headers, rows, mapped) -> list[dict]:
    """Unmapped low-variety text columns that could hold short pay codes."""
    candidates = []
    for header in headers:
        if header in mapped:
            continue
        values = sorted({str(row.get(header) or "").strip() for row in rows} - {""})
        textual = [v for v in values if any(c.isalpha() for c in v)]
        if textual and len(values) <= _MAX_CATEGORIES:
            candidates.append({"column": header, "values": values[:8]})
    return candidates


def is_non_worked(category) -> bool:
    name = key(category)
    return any(term in name for term in NON_WORKED_TERMS) or name in _SHORT_CODES


def treatment(category, rules: dict) -> str:
    """A recorded decision wins; otherwise only an exact worked name counts."""
    name = key(category)
    if is_non_worked(category):
        return EXCLUDED  # Leave cannot be decided into worked time.
    if name in rules:
        return rules[name]
    if name in WORKED_CATEGORIES:
        return WORKED
    return UNKNOWN


def split_records(records, rules, minutes_of):
    """Return worked records, lines ruled out, and a summary of every category."""
    kept, ruled_out, summary = [], [], {}
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
        elif decision == EXCLUDED:
            ruled_out.append(record)
    categories = sorted(summary.values(), key=lambda item: item["category"].lower())
    return kept, ruled_out, categories
