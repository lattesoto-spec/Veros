import click

from carelog import auth
from carelog.database import init_db
from carelog.models import (
    AuditLog,
    Facility,
    FormatMapping,
    ImportJob,
    ImportReceipt,
    IntegrationConfig,
    Organization,
    User,
    db,
)


def register_cli(app):
    @app.cli.command("reconcile-evidence-owners")
    @click.option("--apply", is_flag=True, help="Commit audited ownership inherited from an explicitly owned facility")
    def reconcile_evidence_owners_command(apply):
        """Dry-run legacy ownership repair; unassigned/conflicting homes stay quarantined."""
        from carelog.services.ownership import reconcile_evidence_owners
        click.echo({"applied": apply, **reconcile_evidence_owners(apply=apply)})

    @app.cli.command("init-db")
    def init_db_command():
        """Create any missing tables and columns."""
        added = init_db()
        print(f"Schema ready on {db.engine.url.render_as_string(hide_password=True)}")
        if added:
            print("Added columns: " + ", ".join(added))

    @app.cli.command("bootstrap-org")
    @click.option("--name", required=True, help="Organization (company) name")
    @click.option("--admin-email", required=True)
    @click.option(
        "--password", required=True, help="Temporary password; changed at first sign-in"
    )
    @click.option("--admin-name", default="", help="Administrator's full name")
    @click.option(
        "--superuser",
        is_flag=True,
        help="Platform operator: can support any organization",
    )
    @click.option(
        "--adopt-existing",
        is_flag=True,
        help="Assign pre-existing single-tenant data to this organization",
    )
    def bootstrap_org(
        name, admin_email, password, admin_name, superuser, adopt_existing
    ):
        """Create an organization and its first administrator."""
        init_db()
        email = admin_email.strip().lower()
        if User.query.filter(db.func.lower(User.email) == email).first():
            raise SystemExit(f"A user with email {email} already exists.")
        if len(password) < 12:
            raise SystemExit("Password must be at least 12 characters.")

        org = Organization(name=name.strip())
        db.session.add(org)
        db.session.flush()

        auth.create_user(
            organization_id=org.id,
            email=email,
            name=admin_name,
            role="administrator",
            password=password,
            is_superuser=superuser,
            must_change_password=True,
        )

        adopted = {}
        if adopt_existing:
            # A database that predates multi-tenancy has exactly one customer
            # in it; claim those rows so nothing disappears after the upgrade.
            for model in (
                Facility,
                FormatMapping,
                ImportReceipt,
                ImportJob,
                IntegrationConfig,
            ):
                n = model.query.filter(model.organization_id.is_(None)).update(
                    {"organization_id": org.id}, synchronize_session=False
                )
                adopted[model.__tablename__] = n
        db.session.commit()

        print(f"Organization {org.name!r} created (id {org.id}).")
        print(
            f"Administrator {email} created. Password change required at first sign-in."
        )
        if adopted:
            print(
                "Adopted existing rows: "
                + ", ".join(f"{k}={v}" for k, v in adopted.items() if v)
            )

    @app.cli.command("promote-superuser")
    @click.argument("email")
    def promote_superuser(email):
        """Make an existing account a platform owner."""
        user = User.query.filter(
            db.func.lower(User.email) == email.strip().lower()
        ).first()
        if user is None:
            raise SystemExit(f"No user with email {email}.")
        user.is_superuser = True
        db.session.commit()
        print(
            f"{user.email} is now a platform owner (can create and enter client organisations)."
        )

    @app.cli.command("reset-mfa")
    @click.argument("email")
    def reset_mfa_command(email):
        """Reset MFA when a platform owner has lost every recovery method."""
        user = User.query.filter(
            db.func.lower(User.email) == email.strip().lower()
        ).first()
        if user is None:
            raise SystemExit(f"No user with email {email}.")
        if not user.is_superuser:
            raise SystemExit(
                "The CLI recovery command is restricted to platform owners; "
                "customer accounts must be reset by their organization administrator."
            )
        auth.reset_mfa(user)
        db.session.add(
            AuditLog(
                organization_id=user.organization_id,
                user_id=user.id,
                user_email=user.email,
                action="mfa_reset_by_cli",
                entity="user",
                entity_id=str(user.id),
                detail="Platform-owner emergency recovery",
            )
        )
        db.session.commit()
        print(
            f"Two-step verification reset for {user.email}; next sign-in requires enrollment."
        )

    @app.cli.command("seed-demo")
    @click.option("--name", default="Demo", help="Organisation name")
    @click.option("--admin-email", default=app.config["DEMO_EMAIL"])
    @click.option(
        "--password", required=True, help="Demo administrator password (12+ chars)"
    )
    @click.option("--days", default=60, help="Days of shift history to generate")
    def seed_demo(name, admin_email, password, days):
        """Create a self-contained demo organisation with sample care data.

        Deliberately a separate tenant: demo data must never sit alongside a
        real client's records.
        """
        from carelog.demo import build_demo_organization

        init_db()
        email = admin_email.strip().lower()
        if User.query.filter(db.func.lower(User.email) == email).first():
            raise SystemExit(f"A user with email {email} already exists.")
        if len(password) < 12:
            raise SystemExit("Password must be at least 12 characters.")
        summary = build_demo_organization(
            name=name, admin_email=email, password=password, days=days
        )
        print(f"Demo organisation {summary['organization']!r} created.")
        print(f"  administrator: {email} (must change password at first sign-in)")
        print(f"  facility:      {summary['facility']}")
        print(
            f"  data:          {summary['residents']} residents, {summary['staff']} staff, "
            f"{summary['shifts']} shifts over {days} days"
        )
