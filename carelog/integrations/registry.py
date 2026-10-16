"""Implemented outbound file connections. Vendor exports use Universal Import."""
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
    Platform("url_fetch", "Published export URL", "generic",
             "A URL that serves a CSV/Excel export",
             "available",
             "Point CareMin at a published export URL; it is fetched and imported "
             "through Universal Import on demand. No scheduled polling is configured."),

]

BY_KEY = {p.key: p for p in PLATFORMS}
