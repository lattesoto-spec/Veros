"""Import review snapshots; care and mappings remain uncommitted until approval."""

import hashlib
import json

from sqlalchemy import select

from carelog.models import (
    CareEpisode, Facility, FacilityTarget, PayCategoryRule, Resident, ResidentDay, Shift, Staff, db,
)


def state_fingerprint(facility_id):
    digest = hashlib.sha256()
    for model in (Staff, Resident, Shift, ResidentDay, CareEpisode, FacilityTarget):
        rows = db.session.execute(select(*model.__table__.columns).where(
            model.facility_id == facility_id).order_by(model.id)).all()
        digest.update(model.__tablename__.encode())
        for row in rows:
            digest.update(json.dumps(list(row), default=str, ensure_ascii=True).encode())
    organization_id = db.session.get(Facility, facility_id).organization_id
    rules = PayCategoryRule.query.filter_by(organization_id=organization_id)
    digest.update(json.dumps(sorted((r.category, r.treatment) for r in rules)).encode())
    return digest.hexdigest()


def public_review(review, include_samples=False):
    """Allowlisted review summary. Sample values need record-level permission."""
    samples = review.get("samples", {}) if include_samples else {}
    files = []
    for outcome in review.get("outcomes", []):
        columns = []
        for target in outcome.get("spec", {}).get("targets", []):
            shown = samples.get(outcome["filename"], {}).get(
                f"{target.get('sheet')}/{target.get('kind')}", {})
            for field, rule in target.get("fields", {}).items():
                columns.append({"sheet": target.get("sheet"), "kind": target.get("kind"),
                                "field": field, "source": rule.get("column") or ", ".join(rule.get("columns", []))
                                or f"Fixed value: {rule.get('value', '')}", "format": rule.get("format"),
                                "sample": ", ".join(shown.get(field, [])) or None})
        files.append({"filename": outcome["filename"], "columns": columns,
                      "results": outcome["results"], "warnings": outcome.get("warnings", []),
                      "row_errors": outcome.get("row_errors", [])})
    impact = dict(review.get("impact", {}))
    if not include_samples:
        for private in ("period_sample", "census_changes", "void_matches", "holds"):
            impact.pop(private, None)
    return {"files": files, "impact": impact, "effect": review.get("effect", []),
            "basis": "Replaces only supplied staff/date/evidence groups and resident/date episode groups. Other dates and staff are preserved. Replaced shifts are retained and return if this import is later deleted. Resident census rows update by resident and date."}
