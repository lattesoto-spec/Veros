from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class AppSetting(db.Model):
    """Simple key/value app configuration (API keys, model overrides)."""
    __tablename__ = "app_settings"
    key = db.Column(db.Text, primary_key=True)
    value = db.Column(db.Text)


def get_setting(key: str, default=None):
    row = db.session.get(AppSetting, key)
    return row.value if row and row.value else default


def set_setting(key: str, value):
    row = db.session.get(AppSetting, key)
    if value:
        if row:
            row.value = value
        else:
            db.session.add(AppSetting(key=key, value=value))
    elif row:
        db.session.delete(row)


class Facility(db.Model):
    __tablename__ = "facilities"
    id = db.Column(db.Integer, primary_key=True)
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


class Staff(db.Model):
    __tablename__ = "staff"
    id = db.Column(db.Integer, primary_key=True)
    facility_id = db.Column(db.Integer, db.ForeignKey("facilities.id"), nullable=False)
    staff_id = db.Column(db.Text, nullable=False)
    name = db.Column(db.Text, nullable=False)
    role = db.Column(db.Text, nullable=False)


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
    # Audit lineage: which import produced this row, and where it came from.
    import_receipt_id = db.Column(db.Integer, db.ForeignKey("import_receipts.id"))
    source_row = db.Column(db.Integer)


class FormatMapping(db.Model):
    """A learned file format: fingerprint of the structure -> mapping spec.

    Once a format is learned, every future upload with the same structure is
    parsed by the stored spec with no AI call.
    """
    __tablename__ = "format_mappings"
    id = db.Column(db.Integer, primary_key=True)
    fingerprint = db.Column(db.Text, nullable=False, unique=True)
    spec_json = db.Column(db.Text, nullable=False)
    kinds = db.Column(db.Text, nullable=False, default="")
    source_filename = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class ImportReceipt(db.Model):
    __tablename__ = "import_receipts"
    id = db.Column(db.Integer, primary_key=True)
    facility_id = db.Column(db.Integer, db.ForeignKey("facilities.id"), nullable=False)
    imported_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    residents_imported = db.Column(db.Integer, nullable=False, default=0)
    residents_skipped = db.Column(db.Integer, nullable=False, default=0)
    shifts_imported = db.Column(db.Integer, nullable=False, default=0)
    shifts_skipped = db.Column(db.Integer, nullable=False, default=0)
    first_shift_date = db.Column(db.Date)
    last_shift_date = db.Column(db.Date)
    # Audit trail
    source_path = db.Column(db.Text)      # retained copy of the uploaded file(s)
    imported_by = db.Column(db.Text)      # placeholder until user accounts exist
    mapping_ids = db.Column(db.Text)      # FormatMapping ids used, comma-separated
    calc_version = db.Column(db.Text)
    summary_json = db.Column(db.Text)     # per-file outcome snapshot for the summary page


class IntegrationConfig(db.Model):
    """A configured data-source connection (Phase 2).

    `platform` is a key from integrations.registry. The only connector that can
    sync today is the generic scheduled-URL fetch; vendor APIs activate once
    partner credentials exist.
    """
    __tablename__ = "integration_configs"
    id = db.Column(db.Integer, primary_key=True)
    platform = db.Column(db.Text, nullable=False, unique=True)
    config_json = db.Column(db.Text, nullable=False, default="{}")
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    last_sync_at = db.Column(db.DateTime)
    last_result = db.Column(db.Text)
