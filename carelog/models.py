from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Organization(db.Model):
    """A customer. Every piece of care data belongs to exactly one, and no
    query may cross the boundary — see auth.current_organization_id()."""
    __tablename__ = "organizations"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    timezone = db.Column(db.Text, nullable=False, default="Australia/Sydney")


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=False)
    email = db.Column(db.Text, nullable=False, unique=True)
    name = db.Column(db.Text, nullable=False, default="")
    password_hash = db.Column(db.Text, nullable=False)
    role = db.Column(db.Text, nullable=False, default="read_only")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    # Platform operator (you), not a customer role: can act across
    # organizations for support. Kept separate from `role` on purpose.
    is_superuser = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_login_at = db.Column(db.DateTime)
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)

    organization = db.relationship("Organization")


class AuditLog(db.Model):
    """Who did what, when. Written for security-relevant and data-changing
    actions; never deleted through the UI."""
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    user_email = db.Column(db.Text)      # denormalised: survives user deletion
    action = db.Column(db.Text, nullable=False)
    entity = db.Column(db.Text)
    entity_id = db.Column(db.Text)
    detail = db.Column(db.Text)
    ip_address = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class Facility(db.Model):
    __tablename__ = "facilities"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"))
    name = db.Column(db.Text, nullable=False)
    ancc_target = db.Column(db.Float, nullable=False, default=215.0)
    rn_target = db.Column(db.Float, nullable=False, default=44.0)


class Resident(db.Model):
    __tablename__ = "residents"
    id = db.Column(db.Integer, primary_key=True)
    facility_id = db.Column(db.Integer, db.ForeignKey("facilities.id"), nullable=False)
    resident_id = db.Column(db.Text, nullable=False)
    name = db.Column(db.Text, nullable=False)
    ancc_class = db.Column(db.Text)
    admitted_date = db.Column(db.Date)
    discharged_date = db.Column(db.Date)


class ResidentDay(db.Model):
    """Auditable occupied-bed-day ledger.

    When a facility has ledger rows for a date they are the denominator source
    of truth for that date.  Admission/discharge dates remain a clearly marked
    fallback for older imports.
    """
    __tablename__ = "resident_days"
    __table_args__ = (db.UniqueConstraint(
        "facility_id", "resident_id", "date", name="uq_resident_day_facility_resident_date"
    ),)
    id = db.Column(db.Integer, primary_key=True)
    facility_id = db.Column(db.Integer, db.ForeignKey("facilities.id"), nullable=False)
    resident_id = db.Column(db.Text, nullable=False)
    resident_name = db.Column(db.Text)
    date = db.Column(db.Date, nullable=False)
    occupied = db.Column(db.Boolean, nullable=False, default=True)
    service_type = db.Column(db.Text)       # permanent | respite | transition | other
    leave_type = db.Column(db.Text)         # hospital | social | none
    leave_day_number = db.Column(db.Integer)  # consecutive hospital-leave day
    ancc_class = db.Column(db.Text)
    exclusion_reason = db.Column(db.Text)
    import_receipt_id = db.Column(db.Integer, db.ForeignKey("import_receipts.id"))
    source_row = db.Column(db.Integer)


class CareEpisode(db.Model):
    """Resident-level delivered-care evidence used for reconciliation only.

    Episodes do not feed the statutory staffing-hours numerator: overlapping
    episodes and concurrent care would otherwise double count worked time.
    """
    __tablename__ = "care_episodes"
    id = db.Column(db.Integer, primary_key=True)
    facility_id = db.Column(db.Integer, db.ForeignKey("facilities.id"), nullable=False)
    resident_id = db.Column(db.Text, nullable=False)
    resident_name = db.Column(db.Text)
    date = db.Column(db.Date, nullable=False)
    care_type = db.Column(db.Text)
    care_category = db.Column(db.Text)
    staff_id = db.Column(db.Text)
    staff_name = db.Column(db.Text)
    source_role = db.Column(db.Text)
    start_time = db.Column(db.Time)
    end_time = db.Column(db.Time)
    minutes = db.Column(db.Integer, nullable=False, default=0)
    import_receipt_id = db.Column(db.Integer, db.ForeignKey("import_receipts.id"))
    source_row = db.Column(db.Integer)


class Staff(db.Model):
    __tablename__ = "staff"
    id = db.Column(db.Integer, primary_key=True)
    facility_id = db.Column(db.Integer, db.ForeignKey("facilities.id"), nullable=False)
    staff_id = db.Column(db.Text, nullable=False)
    name = db.Column(db.Text, nullable=False)
    role = db.Column(db.Text, nullable=False)
    # As it appeared in the source file, kept so an eligibility decision can be
    # re-examined against what the provider actually sent.
    source_role = db.Column(db.Text)

    # Eligibility. Minutes count only while this is "approved": an unresolved
    # role must never quietly inflate a regulated number.
    eligibility_status = db.Column(db.Text, nullable=False, default="pending")
    eligibility_reason = db.Column(db.Text)
    eligible_from = db.Column(db.Date)
    eligible_to = db.Column(db.Date)
    approved_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)

    # Evidence of entitlement to work in the counted role.
    employment_type = db.Column(db.Text)        # employee | agency | contractor
    classification = db.Column(db.Text)         # award / EA classification
    registration_number = db.Column(db.Text)    # Ahpra, for RN and EN
    registration_expiry = db.Column(db.Date)


class Shift(db.Model):
    __tablename__ = "shifts"
    id = db.Column(db.Integer, primary_key=True)
    staff_id = db.Column(db.Integer, db.ForeignKey("staff.id"), nullable=False)
    facility_id = db.Column(db.Integer, db.ForeignKey("facilities.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    is_direct_care = db.Column(db.Boolean, nullable=False, default=True)
    break_minutes = db.Column(db.Integer, nullable=False, default=0)
    is_agency = db.Column(db.Boolean, nullable=False, default=False)
    # The evidence basis is explicit. Only "worked" rows feed historical
    # compliance; "rostered" rows are planning evidence and "unverified" rows
    # remain visible but are withheld until their source is confirmed.
    evidence_type = db.Column(db.Text, nullable=False, default="unverified")
    labour_cost = db.Column(db.Float)
    # Audit lineage: which import produced this row, and where it came from.
    import_receipt_id = db.Column(db.Integer, db.ForeignKey("import_receipts.id"))
    source_row = db.Column(db.Integer)


class FormatMapping(db.Model):
    """A learned file format: fingerprint of the structure -> mapping spec.

    Once a format is learned, every future upload with the same structure is
    parsed by the stored spec with no AI call.
    """
    __tablename__ = "format_mappings"
    # A learned spec names the columns of a customer's roster system, so it is
    # tenant configuration rather than shared knowledge: scoped per org.
    __table_args__ = (db.UniqueConstraint("organization_id", "fingerprint",
                                          name="uq_format_org_fingerprint"),)
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"))
    fingerprint = db.Column(db.Text, nullable=False)
    spec_json = db.Column(db.Text, nullable=False)
    kinds = db.Column(db.Text, nullable=False, default="")
    source_filename = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class ImportReceipt(db.Model):
    __tablename__ = "import_receipts"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"))
    facility_id = db.Column(db.Integer, db.ForeignKey("facilities.id"), nullable=False)
    imported_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    imported_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    residents_imported = db.Column(db.Integer, nullable=False, default=0)
    residents_skipped = db.Column(db.Integer, nullable=False, default=0)
    shifts_imported = db.Column(db.Integer, nullable=False, default=0)
    shifts_skipped = db.Column(db.Integer, nullable=False, default=0)
    resident_days_imported = db.Column(db.Integer, nullable=False, default=0)
    care_episodes_imported = db.Column(db.Integer, nullable=False, default=0)
    evidence_type = db.Column(db.Text, nullable=False, default="unverified")
    first_shift_date = db.Column(db.Date)
    last_shift_date = db.Column(db.Date)
    # Audit trail
    source_path = db.Column(db.Text)      # retained copy of the uploaded file(s)
    imported_by = db.Column(db.Text)      # placeholder until user accounts exist
    mapping_ids = db.Column(db.Text)      # FormatMapping ids used, comma-separated
    calc_version = db.Column(db.Text)
    summary_json = db.Column(db.Text)     # per-file outcome snapshot for the summary page


class ImportJob(db.Model):
    """A background import, tracked in the database rather than in memory.

    Serverless hosts give no guarantee that the request which started a job and
    the request which polls its status land on the same instance — and any
    instance can be recycled mid-flight. Keeping job state here means progress
    survives both, and a job that dies leaves an inspectable record instead of
    vanishing.
    """
    __tablename__ = "import_jobs"
    id = db.Column(db.Text, primary_key=True)  # short uuid hex
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"))
    facility_id = db.Column(db.Integer, db.ForeignKey("facilities.id"), nullable=False)
    started_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    status = db.Column(db.Text, nullable=False, default="queued")  # queued|running|done|failed
    error = db.Column(db.Text)
    receipt_id = db.Column(db.Integer, db.ForeignKey("import_receipts.id"))
    files_json = db.Column(db.Text, nullable=False, default="[]")  # [{filename, stage, detail}]
    storage_prefix = db.Column(db.Text, nullable=False, default="")
    evidence_type = db.Column(db.Text, nullable=False, default="unverified")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def as_dict(self) -> dict:
        import json as _json

        return {
            "id": self.id,
            "status": self.status,
            "error": self.error,
            "receipt_id": self.receipt_id,
            "files": _json.loads(self.files_json or "[]"),
        }


class IntegrationConfig(db.Model):
    """A configured data-source connection (Phase 2).

    `platform` is a key from integrations.registry. The only connector that can
    sync today is the generic scheduled-URL fetch; vendor APIs activate once
    partner credentials exist.
    """
    __tablename__ = "integration_configs"
    __table_args__ = (db.UniqueConstraint("organization_id", "platform",
                                          name="uq_integration_org_platform"),)
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"))
    platform = db.Column(db.Text, nullable=False)
    config_json = db.Column(db.Text, nullable=False, default="{}")
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    last_sync_at = db.Column(db.DateTime)
    last_result = db.Column(db.Text)
