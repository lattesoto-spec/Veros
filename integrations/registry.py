"""Integration registry (Phase 2).

Every platform a provider might already use, with its integration status.
Vendor APIs require partner/API credentials issued by each vendor — until a
customer supplies theirs, those connectors stay "credentials required" and
their exports flow in through Universal Import instead (which already
understands their CSV/Excel exports without templates).

The one connector that works out of the box is `url_fetch`: many systems can
publish or email a scheduled export to a URL/SFTP-backed link; CareMin pulls
it and pushes it through the exact same ingestion pipeline as a manual upload.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Platform:
    key: str
    name: str
    category: str        # rostering | care | payroll | accounting | generic
    auth: str            # what real API access needs
    api_status: str      # "available" | "credentials_required"
    note: str


PLATFORMS = [
    Platform("url_fetch", "Scheduled file fetch (any system)", "generic",
             "A URL that serves a CSV/Excel export",
             "available",
             "Point CareMin at a published export URL; it is fetched and imported "
             "through Universal Import on demand."),
    Platform("humanforce", "Humanforce", "rostering",
             "Humanforce Cloud API key (customer-issued)",
             "credentials_required",
             "Roster/timesheet exports import today via Universal Import."),
    Platform("alayacare", "AlayaCare", "care",
             "AlayaCare REST API credentials (per-tenant)",
             "credentials_required",
             "Visit/shift exports import today via Universal Import."),
    Platform("shiftcare", "ShiftCare", "rostering",
             "ShiftCare API token",
             "credentials_required",
             "Shift exports import today via Universal Import."),
    Platform("ecase", "eCase", "care",
             "eCase integration credentials (Health Metrics)",
             "credentials_required",
             "Resident/occupancy exports import today via Universal Import."),
    Platform("autumncare", "AutumnCare", "care",
             "AutumnCare integration agreement",
             "credentials_required",
             "Resident exports import today via Universal Import."),
    Platform("telstra_health", "Telstra Health (Clinical Manager)", "care",
             "Telstra Health integration agreement",
             "credentials_required",
             "Exports import today via Universal Import."),
    Platform("epic", "Epic", "care",
             "Epic App Orchard / FHIR credentials — rarely used in AU aged care",
             "credentials_required",
             "Evaluate per customer; FHIR shift/staffing resources vary."),
    Platform("employment_hero", "Employment Hero", "payroll",
             "Employment Hero OAuth app",
             "credentials_required",
             "Payroll/timesheet exports import today via Universal Import."),
    Platform("deputy", "Deputy", "rostering",
             "Deputy OAuth 2.0 / permanent token",
             "credentials_required",
             "Timesheet exports import today via Universal Import."),
    Platform("myob", "MYOB", "accounting",
             "MYOB developer app + company file access",
             "credentials_required",
             "Payroll exports import today via Universal Import."),
    Platform("xero", "Xero", "accounting",
             "Xero OAuth 2.0 app (payroll scope)",
             "credentials_required",
             "Payroll exports import today via Universal Import."),
    Platform("keypay", "KeyPay (Employment Hero Payroll)", "payroll",
             "KeyPay API key",
             "credentials_required",
             "Timesheet/payroll exports import today via Universal Import."),
]

BY_KEY = {p.key: p for p in PLATFORMS}
