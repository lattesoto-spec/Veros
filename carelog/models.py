from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# Retained shift rows that have left every evidence stream. Calculations select
# worked/rostered/unverified explicitly, so a retired row can never be counted.
SUPERSEDED = "superseded"
VOIDED = "voided"
RETIRED_EVIDENCE = (SUPERSEDED, VOIDED)


class Organization(db.Model):
    """A customer. Every piece of care data belongs to exactly one, and no
    query may cross the boundary — see auth.current_organization_id()."""

    __tablename__ = "organizations"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    timezone = db.Column(db.Text, nullable=False, default="Australia/Sydney")
    # External AI-assisted format mapping is off until an administrator opts in.
    ai_mapping_enabled = db.Column(db.Boolean, nullable=False, default=False)


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(
        db.Integer, db.ForeignKey("organizations.id"), nullable=False
    )
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
    # A session is valid only while its stamped version matches this value.
    # Credential, MFA and account-state changes increment it to revoke every
    # signed cookie immediately without needing server-side Flask sessions.
    auth_version = db.Column(db.Integer, nullable=False, default=1)
    # Advances at sign-out. Kept apart from auth_version, which also binds
    # trusted devices that a normal sign-out should not revoke.
    session_epoch = db.Column(db.Integer, nullable=False, default=0)
    mfa_enabled = db.Column(db.Boolean, nullable=False, default=False)
    mfa_secret_encrypted = db.Column(db.Text)
    mfa_enrolled_at = db.Column(db.DateTime)
    # Reject a TOTP counter which has already been accepted. A valid code must
    # never be reusable inside the normal clock-skew window.
    mfa_last_totp_counter = db.Column(db.BigInteger)

    organization = db.relationship("Organization")


class MfaRecoveryCode(db.Model):
    """A single-use MFA recovery code. The plaintext is displayed once."""

    __tablename__ = "mfa_recovery_codes"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    code_prefix = db.Column(db.Text, nullable=False, index=True)
    code_hash = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    used_at = db.Column(db.DateTime)


class TrustedDevice(db.Model):
    """Server-side half of a revocable 30-day trusted-device token."""

    __tablename__ = "trusted_devices"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    selector = db.Column(db.Text, nullable=False, unique=True, index=True)
    token_hash = db.Column(db.Text, nullable=False)
    auth_version = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_used_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    revoked_at = db.Column(db.DateTime)


class AuthRateLimit(db.Model):
    """Database-backed failed-login buckets suitable for serverless workers."""

    __tablename__ = "auth_rate_limits"
    key_hash = db.Column(db.String(64), primary_key=True)
    failures = db.Column(db.Integer, nullable=False, default=0)
    window_started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    blocked_until = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class AuditLog(db.Model):
    """Who did what, when. Written for security-relevant and data-changing
    actions; never deleted through the UI."""

    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    user_email = db.Column(db.Text)  # denormalised: survives user deletion
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

    @property
    def target_history(self):
        return (
            FacilityTarget.query.filter_by(facility_id=self.id)
            .order_by(FacilityTarget.quarter_start.desc())
            .all()
        )


class FacilityTarget(db.Model):
    """Approved target evidence for one quarter; corrections remain audited."""

    __tablename__ = "facility_targets"
    __table_args__ = (
        db.UniqueConstraint(
            "facility_id", "quarter_start", name="uq_facility_target_quarter"
        ),
    )
    id = db.Column(db.Integer, primary_key=True)
    facility_id = db.Column(db.Integer, db.ForeignKey("facilities.id"), nullable=False)
    quarter_start = db.Column(db.Date, nullable=False)
    ancc_target = db.Column(db.Float, nullable=False)
    rn_target = db.Column(db.Float, nullable=False)
    source = db.Column(db.Text, nullable=False)
    approved_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


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
    __table_args__ = (
        db.UniqueConstraint(
            "facility_id",
            "resident_id",
            "date",
            name="uq_resident_day_facility_resident_date",
        ),
        db.Index("ix_resident_day_facility_date", "facility_id", "date"),
    )
    id = db.Column(db.Integer, primary_key=True)
    facility_id = db.Column(db.Integer, db.ForeignKey("facilities.id"), nullable=False)
    resident_id = db.Column(db.Text, nullable=False)
    resident_name = db.Column(db.Text)
    date = db.Column(db.Date, nullable=False)
    occupied = db.Column(db.Boolean, nullable=False, default=True)
    service_type = db.Column(db.Text)  # permanent | respite | transition | other
    leave_type = db.Column(db.Text)  # hospital | social | none
    leave_day_number = db.Column(db.Integer)  # consecutive hospital-leave day
    ancc_class = db.Column(db.Text)
    exclusion_reason = db.Column(db.Text)
    import_receipt_id = db.Column(db.Integer, db.ForeignKey("import_receipts.id"))
    source_row = db.Column(db.Integer)
    source_file = db.Column(db.Text)
    source_sheet = db.Column(db.Text)
    source_page = db.Column(db.Integer)
    # Earlier versions of this row, newest last, restored if an import is deleted.
    revisions_json = db.Column(db.Text)


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
    source_file = db.Column(db.Text)
    source_sheet = db.Column(db.Text)
    source_page = db.Column(db.Integer)


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

    # Every decision has a reviewer; only an approval has an approval stamp.
    reviewed_at = db.Column(db.DateTime)
    reviewed_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    identity_aliases_json = db.Column(db.Text)

    @property
    def identity_aliases(self):
        import json
        return json.loads(self.identity_aliases_json or "[]")

    # Evidence of entitlement to work in the counted role.
    employment_type = db.Column(db.Text)  # employee | agency | contractor
    classification = db.Column(db.Text)  # award / EA classification
    registration_number = db.Column(db.Text)  # Ahpra, for RN and EN
    registration_expiry = db.Column(db.Date)
    # Renewal periods not evidenced by a credential export: [[from, to], ...].
    # A later expiry never rewrites a lapse; a reviewer confirms continuity.
    credential_gaps_json = db.Column(db.Text)

    @property
    def credential_gaps(self):
        import json

        return [
            {"from": start, "to": end}
            for start, end in json.loads(self.credential_gaps_json or "[]")
        ]


class Shift(db.Model):
    __tablename__ = "shifts"
    __table_args__ = (
        db.Index(
            "ix_shift_facility_stream_date", "facility_id", "evidence_type", "date"
        ),
    )
    id = db.Column(db.Integer, primary_key=True)
    staff_id = db.Column(db.Integer, db.ForeignKey("staff.id"), nullable=False)
    facility_id = db.Column(db.Integer, db.ForeignKey("facilities.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    is_direct_care = db.Column(db.Boolean, nullable=False, default=True)
    break_minutes = db.Column(db.Integer, nullable=False, default=0)
    duration_minutes = db.Column(db.Integer)
    coverage_known = db.Column(db.Boolean, nullable=False, default=True)
    is_agency = db.Column(db.Boolean, nullable=False, default=False)
    # The evidence basis is explicit. Only "worked" rows feed historical
    # compliance; "rostered" rows are planning evidence and "unverified" rows
    # remain visible but are withheld until their source is confirmed.
    evidence_type = db.Column(db.Text, nullable=False, default="unverified")
    labour_cost = db.Column(db.Float)
    pay_category = db.Column(db.Text)  # distinguishes payroll lines for one day
    # Correction history. Replaced and voided rows stay on record outside the
    # evidence streams, keeping the class they held so they can be restored.
    prior_evidence_type = db.Column(db.Text)
    superseded_by_receipt_id = db.Column(
        db.Integer, db.ForeignKey("import_receipts.id")
    )
    void_reason = db.Column(db.Text)
    voided_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    voided_at = db.Column(db.DateTime)
    # Audit lineage: which import produced this row, and where it came from.
    import_receipt_id = db.Column(db.Integer, db.ForeignKey("import_receipts.id"))
    source_row = db.Column(db.Integer)
    source_file = db.Column(db.Text)
    source_sheet = db.Column(db.Text)
    source_page = db.Column(db.Integer)
    timezone_name = db.Column(db.Text)


class PayCategoryRule(db.Model):
    """A provider's recorded decision on whether a pay category is worked time."""

    __tablename__ = "pay_category_rules"
    __table_args__ = (
        db.UniqueConstraint("organization_id", "category", name="uq_pay_rule_org_category"),
    )
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=False)
    category = db.Column(db.Text, nullable=False)  # normalised
    treatment = db.Column(db.Text, nullable=False)  # worked | excluded
    basis = db.Column(db.Text, nullable=False)
    decided_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    decided_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class QuarterClosure(db.Model):
    """Figures and the statement as issued. Reclosing adds a row; none is altered."""

    __tablename__ = "quarter_closures"
    __table_args__ = (db.Index("ix_quarter_closure_facility", "facility_id", "year", "quarter"),)
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"))
    facility_id = db.Column(db.Integer, db.ForeignKey("facilities.id"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    quarter = db.Column(db.Integer, nullable=False)
    closed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    closed_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    closed_by = db.Column(db.Text)
    basis = db.Column(db.Text, nullable=False)
    calc_version = db.Column(db.Text, nullable=False)
    provisional = db.Column(db.Boolean, nullable=False, default=False)
    figures_json = db.Column(db.Text, nullable=False)
    sources_json = db.Column(db.Text, nullable=False, default="[]")
    pdf_key = db.Column(db.Text, nullable=False)
    pdf_sha256 = db.Column(db.Text, nullable=False)


class FormatMapping(db.Model):
    """A learned file format: fingerprint of the structure -> mapping spec.

    Once a format is learned, every future upload with the same structure is
    parsed by the stored spec with no AI call.
    """

    __tablename__ = "format_mappings"
    # A learned spec names the columns of a customer's roster system, so it is
    # tenant configuration rather than shared knowledge: scoped per org.
    __table_args__ = (
        db.UniqueConstraint(
            "organization_id", "fingerprint", name="uq_format_org_fingerprint"
        ),
    )
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
    original_evidence_type = db.Column(db.Text)
    first_shift_date = db.Column(db.Date)
    last_shift_date = db.Column(db.Date)
    # Audit trail
    source_path = db.Column(db.Text)  # retained copy of the uploaded file(s)
    imported_by = db.Column(db.Text)  # placeholder until user accounts exist
    mapping_ids = db.Column(db.Text)  # FormatMapping ids used, comma-separated
    calc_version = db.Column(db.Text)
    summary_json = db.Column(db.Text)  # per-file outcome snapshot for the summary page

    def _retired_shifts(self, kind):
        return Shift.query.filter_by(
            import_receipt_id=self.id, evidence_type=kind
        ).count()

    @property
    def voided_rows(self):
        return self._retired_shifts(VOIDED)

    @property
    def superseded_rows(self):
        return self._retired_shifts(SUPERSEDED)

    @property
    def active_care_rows(self):
        active_shifts = Shift.query.filter(
            Shift.import_receipt_id == self.id,
            Shift.evidence_type.notin_(RETIRED_EVIDENCE),
        ).count()
        return active_shifts + sum(
            model.query.filter_by(import_receipt_id=self.id).count()
            for model in (ResidentDay, CareEpisode)
        )

    @property
    def record_status(self):
        if any(
            value is None
            for value in (
                self.shifts_imported,
                self.resident_days_imported,
                self.care_episodes_imported,
            )
        ):
            return "Not available"
        original = (
            self.shifts_imported
            + self.resident_days_imported
            + self.care_episodes_imported
        )
        active = self.active_care_rows
        return (
            "Superseded"
            if original and active == 0
            else "Partly superseded"
            if active < original
            else "Current"
        )


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
    status = db.Column(
        db.Text, nullable=False, default="queued"
    )  # queued|running|done|failed
    error = db.Column(db.Text)
    receipt_id = db.Column(db.Integer, db.ForeignKey("import_receipts.id"))
    files_json = db.Column(
        db.Text, nullable=False, default="[]"
    )  # [{filename, stage, detail}]
    review_json = db.Column(db.Text)
    # Reviewer-supplied column mappings, {filename: spec}, re-run deterministically.
    manual_specs_json = db.Column(db.Text)
    # Chosen at confirmation: rows in the date range absent from the file retire.
    replace_period = db.Column(db.Boolean, nullable=False, default=False)
    approved_at = db.Column(db.DateTime)
    reviewed_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    storage_prefix = db.Column(db.Text, nullable=False, default="")
    evidence_type = db.Column(db.Text, nullable=False, default="unverified")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def as_dict(self, include_samples: bool = False) -> dict:
        import json as _json
        from carelog.ingestion.review import public_review

        updated = self.updated_at or self.created_at
        stalled = self.status in {"retaining", "queued", "running"} and (
            updated is None or (datetime.utcnow() - updated).total_seconds() > 900
        )

        return {
            "id": self.id,
            "status": "stalled" if stalled else self.status,
            "error": self.error,
            "receipt_id": self.receipt_id,
            "files": _json.loads(self.files_json or "[]"),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": updated.isoformat() if updated else None,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "review": public_review(_json.loads(self.review_json), include_samples)
            if self.review_json
            else None,
        }


class IntegrationConfig(db.Model):
    """A configured data-source connection (Phase 2).

    `platform` is a key from integrations.registry. The only connector that can
    sync today is the generic scheduled-URL fetch; vendor APIs activate once
    partner credentials exist.
    """

    __tablename__ = "integration_configs"
    __table_args__ = (
        db.UniqueConstraint(
            "organization_id", "platform", name="uq_integration_org_platform"
        ),
    )
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"))
    platform = db.Column(db.Text, nullable=False)
    config_json = db.Column(db.Text, nullable=False, default="{}")
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    last_sync_at = db.Column(db.DateTime)
    last_result = db.Column(db.Text)
