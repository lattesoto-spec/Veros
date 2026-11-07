"""Local browser-test server. Refuses every database except the dedicated local fixture."""

import os
import sys
from datetime import date, time
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.fernet import Fernet
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from carelog import auth
from carelog.app import create_app
from carelog.database import init_db
from carelog.models import Facility, FacilityTarget, Organization, Shift, Staff, db


def main():
    url = os.environ.get("CAREMIN_E2E_DATABASE_URL", "")
    parsed = make_url(url)
    if (
        parsed.host not in {"localhost", "127.0.0.1"}
        or parsed.database != "caremin_browser_test"
        or not parsed.drivername.startswith("postgresql")
    ):
        raise RuntimeError(
            "Only a local PostgreSQL database named caremin_browser_test is allowed."
        )
    os.environ.pop("VERCEL", None)
    os.environ["AUTO_INIT_DB"] = "0"
    os.environ["IMPORT_WORKER"] = "thread"
    os.environ["MFA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    os.environ["PUBLIC_APP_ORIGINS"] = ""
    with TemporaryDirectory(prefix="caremin-browser-sources-") as uploads:
        app = create_app(
            {
                "SQLALCHEMY_DATABASE_URI": url,
                "SECRET_KEY": "isolated-browser-test-only",
                "UPLOADS_DIR": uploads,
                "TESTING": False,
            }
        )
        with app.app_context():
            db.drop_all()
            init_db()
            org = Organization(name="Synthetic Browser Provider")
            db.session.add(org)
            db.session.flush()
            admin = auth.create_user(
                organization_id=org.id,
                email="browser@example.test",
                name="Browser Test Manager",
                role="administrator",
                password="BrowserTestPassword123!",
                must_change_password=False,
            )
            admin.mfa_enabled = True
            admin.mfa_secret_encrypted = auth._encrypt_secret("JBSWY3DPEHPK3PXP")
            home = Facility(
                organization_id=org.id,
                name="Browser Test Home",
                ancc_target=215,
                rn_target=44,
            )
            db.session.add(home)
            db.session.flush()
            db.session.add(
                FacilityTarget(
                    facility_id=home.id,
                    quarter_start=date(2026, 7, 1),
                    ancc_target=215,
                    rn_target=44,
                    source="Synthetic browser fixture target notice",
                    approved_by_user_id=admin.id,
                )
            )
            member = Staff(
                facility_id=home.id,
                staff_id="BROWSER-PENDING",
                name="Browser Review Worker",
                role="PCW",
                source_role="Support Worker",
                eligibility_status="pending",
                eligibility_reason="Ambiguous source title",
            )
            db.session.add(member)
            db.session.flush()
            db.session.add(
                Shift(
                    facility_id=home.id,
                    staff_id=member.id,
                    date=date(2026, 8, 25),
                    start_time=time(14),
                    end_time=time(15),
                    evidence_type="worked",
                    source_file="synthetic_pending.csv",
                    source_row=2,
                )
            )
            db.session.commit()
        app.run(host="127.0.0.1", port=8088, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
