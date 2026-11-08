from carelog.domain.clock import facility_today
import os

from flask import (
    session,
)

from carelog.api.responses import form_data
from carelog.auth import (
    current_organization_id,
)
from carelog.domain import eligibility as el
from carelog.domain.care_minutes import (
    quarter_bounds,
)
from carelog.domain.compliance import (
    CALC_VERSION,
)
from carelog.models import (
    CareEpisode,
    Facility,
    ImportJob,
    ImportReceipt,
    Resident,
    ResidentDay,
    Shift,
    Staff,
)
from carelog.storage import LocalStorage, StorageError, build_storage

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def org_facilities():
    """Every facility belonging to the signed-in user's organization."""
    return (
        Facility.query.filter_by(organization_id=current_organization_id())
        .order_by(Facility.name)
        .all()
    )


def _clock_label(minute: int) -> str:
    if minute >= 1440:
        return "24:00"
    return f"{minute // 60:02d}:{minute % 60:02d}"


def workspace_context(facility: Facility | None) -> dict:
    """Small, factual context record displayed above facility pages."""
    if facility is None:
        return {}
    latest = (
        Shift.query.filter_by(facility_id=facility.id, evidence_type="worked")
        .filter(Shift.date <= facility_today(facility))
        .order_by(Shift.date.desc())
        .first()
    )
    latest_day = latest.date if latest else None
    receipt = (
        ImportReceipt.query.filter_by(facility_id=facility.id)
        .order_by(ImportReceipt.imported_at.desc())
        .first()
    )
    if latest_day:
        period_start, period_end = quarter_bounds(latest_day)
        from carelog.domain.compliance import assessment_end
        period_end = assessment_end(facility.id, latest_day)
        period = (
            f"{period_start.strftime('%d %b')} to {period_end.strftime('%d %b %Y')}"
        )
    else:
        period = "Not available"
    return {
        "period": period,
        "latest_day": latest_day,
        "last_import": receipt.imported_at if receipt else None,
        "open_issues": Staff.query.filter_by(
            facility_id=facility.id, eligibility_status=el.PENDING
        ).count(),
        "calc_version": CALC_VERSION,
    }


def current_facility():
    """The facility the user is currently working in.

    Organizations can run several homes, so the active one is held in the
    session. The id is re-checked against the organization on every request —
    a session value is user-supplied data and must never be trusted to select
    a row on its own.
    """
    facilities = org_facilities()
    if not facilities:
        return None
    chosen = session.get("facility_id")
    for f in facilities:
        if f.id == chosen:
            return f
    return facilities[0]


def set_active_facility(facility_id: int) -> Facility | None:
    facility = Facility.query.filter_by(
        id=facility_id, organization_id=current_organization_id()
    ).first()
    if facility:
        session["facility_id"] = facility.id
    return facility


def _storage():
    from flask import current_app

    return build_storage(current_app.config["UPLOADS_DIR"])


def _storage_for(receipt):
    """Receipts written before object storage recorded an absolute directory
    on the machine's disk. Serve those from the filesystem so the existing
    audit trail keeps working after the migration."""
    if receipt.source_path and os.path.isabs(receipt.source_path):
        return LocalStorage(os.path.dirname(receipt.source_path))
    return _storage()


def _confirmed(req) -> bool:
    """Destructive actions require the word typed out, not just a click."""
    return (form_data().get("confirm") or "").strip().lower() == "delete"


def _delete_facility_data(facility_ids):
    """Remove care data for facilities, in an order the foreign keys allow.

    Shifts reference both staff and receipts, and import jobs reference
    receipts — so receipts cannot go first. Postgres enforces this even though
    SQLite historically did not.
    """
    Shift.query.filter(Shift.facility_id.in_(facility_ids)).delete(
        synchronize_session=False
    )
    ResidentDay.query.filter(ResidentDay.facility_id.in_(facility_ids)).delete(
        synchronize_session=False
    )
    CareEpisode.query.filter(CareEpisode.facility_id.in_(facility_ids)).delete(
        synchronize_session=False
    )
    ImportJob.query.filter(ImportJob.facility_id.in_(facility_ids)).delete(
        synchronize_session=False
    )
    ImportReceipt.query.filter(ImportReceipt.facility_id.in_(facility_ids)).delete(
        synchronize_session=False
    )
    from carelog.models import FacilityTarget, QuarterClosure

    FacilityTarget.query.filter(FacilityTarget.facility_id.in_(facility_ids)).delete(
        synchronize_session=False
    )
    QuarterClosure.query.filter(QuarterClosure.facility_id.in_(facility_ids)).delete(
        synchronize_session=False
    )
    for model in (Staff, Resident):
        model.query.filter(model.facility_id.in_(facility_ids)).delete(
            synchronize_session=False
        )


def _purge_evidence(receipts, facility_ids=None) -> int:
    """Purge receipt and failed/cancelled job sources before deleting their records.

    Fail visibly if storage cannot confirm deletion; keep the database history
    available so the operator can investigate and retry.
    """
    removed, handled = 0, set()
    for receipt in receipts:
        if not receipt.source_path:
            continue
        store = _storage_for(receipt)
        prefix = (
            os.path.basename(receipt.source_path.rstrip("/"))
            if os.path.isabs(receipt.source_path)
            else receipt.source_path
        )
        removed += store.delete([item.key for item in store.list(prefix)])
        handled.add(receipt.source_path)
    jobs = (
        ImportJob.query.filter(ImportJob.facility_id.in_(facility_ids)).all()
        if facility_ids is not None
        else (
            ImportJob.query.filter(
                ImportJob.receipt_id.in_([r.id for r in receipts])
            ).all()
            if receipts
            else []
        )
    )
    store = _storage()
    if facility_ids is not None:
        from carelog.models import QuarterClosure

        keys = [
            closure.pdf_key
            for closure in QuarterClosure.query.filter(
                QuarterClosure.facility_id.in_(facility_ids)
            )
        ]
        if keys:
            removed += store.delete(keys)
    for job in jobs:
        if job.storage_prefix and job.storage_prefix not in handled:
            removed += store.delete(
                [item.key for item in store.list(job.storage_prefix)]
            )
            handled.add(job.storage_prefix)
    return removed


def _receipt_files(receipt):
    """Evidence files retained for a receipt, whichever storage holds them."""
    if not receipt.source_path:
        return []
    prefix = (
        os.path.basename(receipt.source_path.rstrip("/"))
        if os.path.isabs(receipt.source_path)
        else receipt.source_path
    )
    try:
        return _storage_for(receipt).list(prefix)
    except StorageError:
        return []


def _latest_receipt(facility):
    if not facility:
        return None
    return (
        ImportReceipt.query.filter_by(facility_id=facility.id)
        .order_by(ImportReceipt.imported_at.desc())
        .first()
    )
