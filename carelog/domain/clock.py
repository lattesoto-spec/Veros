"""Provider-local calendar dates for evidence and monitoring boundaries."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from carelog.models import Facility, Organization, db


def organization_today(organization_id=None, *, now=None):
    if organization_id is None:
        from flask import has_request_context
        if has_request_context():
            from carelog.auth import current_organization_id
            organization_id = current_organization_id()
    organization = db.session.get(Organization, organization_id) if organization_id else None
    zone = ZoneInfo(organization.timezone if organization else "Australia/Sydney")
    return (now or datetime.now(timezone.utc)).astimezone(zone).date()


def facility_today(facility, *, now=None):
    if isinstance(facility, int):
        facility = db.session.get(Facility, facility)
    return organization_today(getattr(facility, "organization_id", None), now=now)


def facility_zone_name(facility_id):
    """Keep related objects within this session to avoid a query per shift."""
    cached = db.session.info.setdefault("facility_zone_objects", {})
    if facility_id not in cached:
        facility = db.session.get(Facility, facility_id)
        organization = db.session.get(Organization, facility.organization_id) if facility and facility.organization_id else None
        cached[facility_id] = (facility, organization)
    organization = cached[facility_id][1]
    return organization.timezone if organization else "Australia/Sydney"
