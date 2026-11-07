"""Explicit maintenance reconciliation for legacy evidence tenant ownership."""
from carelog.models import AuditLog, Facility, ImportJob, ImportReceipt, db


def reconcile_evidence_owners(apply=False):
    """Inherit only a known facility owner; never guess an unassigned home."""
    counts = {"repairable": 0, "quarantined": 0, "conflicts": 0}
    for model in (ImportJob, ImportReceipt):
        for item in model.query.all():
            facility = db.session.get(Facility, item.facility_id)
            owner = facility.organization_id if facility else None
            if owner is None:
                counts["quarantined"] += 1
            elif item.organization_id is None:
                counts["repairable"] += 1
                if apply:
                    item.organization_id = owner
                    db.session.add(AuditLog(organization_id=owner,
                        action="evidence_owner_reconciled", entity=model.__tablename__,
                        entity_id=str(item.id), detail=f"Inherited verified facility owner from facility {item.facility_id}; previous owner NULL."))
            elif item.organization_id != owner:
                counts["conflicts"] += 1
    if apply:
        db.session.commit()
    return counts
