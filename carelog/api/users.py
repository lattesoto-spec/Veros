from flask import (
    abort,
    flash,
    url_for,
)

from carelog import auth
from carelog.api import bp
from carelog.api.responses import form_data, navigate, resource
from carelog.auth import (
    ROLES,
    current_organization_id,
    current_user,
    require,
)
from carelog.models import (
    Organization,
    User,
    db,
)


@bp.route("/admin/users")
@require("manage_users")
def admin_users():
    org_id = current_organization_id()
    users = (
        User.query.filter_by(organization_id=org_id, is_superuser=False)
        .order_by(User.is_active.desc(), User.email)
        .all()
    )
    return resource(
        "admin_users",
        users=users,
        roles=ROLES,
        organization=db.session.get(Organization, org_id),
    )


@bp.route("/admin/users/create", methods=["POST"])
@require("manage_users")
def admin_create_user():
    email = (form_data().get("email") or "").strip().lower()
    name = (form_data().get("name") or "").strip()
    role = (form_data().get("role") or "read_only").strip()
    password = form_data().get("password") or ""
    confirm = form_data().get("confirm_password") or ""

    if not email or "@" not in email:
        flash("A valid email address is required.")
    elif role not in ROLES:
        flash("Unknown role.")
    elif User.query.filter(db.func.lower(User.email) == email).first():
        flash("A user with that email already exists.")
    elif problem := auth.password_problem(password, confirm):
        flash(problem)
    else:
        user = auth.create_user(
            organization_id=current_organization_id(),
            email=email,
            name=name,
            role=role,
            password=password,
            must_change_password=True,
        )
        db.session.flush()
        auth.record("user_created", "user", user.id, f"{email} as {role}")
        db.session.commit()
        flash(
            f"{email} added as {ROLES[role]['label']}. They must change the password at first sign-in."
        )
    return navigate(url_for("api.admin_users"))


@bp.route("/admin/users/<int:user_id>/update", methods=["POST"])
@require("manage_users")
def admin_update_user(user_id):
    user = User.query.filter_by(
        id=user_id, organization_id=current_organization_id(), is_superuser=False
    ).first()
    if user is None:
        abort(404)

    actor = current_user()
    action = form_data().get("action")
    if action == "set_role":
        role = (form_data().get("role") or "").strip()
        if role not in ROLES:
            flash("Unknown role.")
        elif user.id == actor.id and role != "administrator":
            # Otherwise the last administrator can lock everyone out.
            flash("You cannot remove your own administrator access.")
        else:
            user.role = role
            user.auth_version += 1
            auth.revoke_trusted_devices(user)
            auth.record("user_role_changed", "user", user.id, role)
            flash(f"{user.email} is now {ROLES[role]['label']}.")
    elif action == "deactivate":
        if user.id == actor.id:
            flash("You cannot deactivate your own account.")
        else:
            user.is_active = False
            user.auth_version += 1
            auth.revoke_trusted_devices(user)
            auth.record("user_deactivated", "user", user.id)
            flash(f"{user.email} can no longer sign in.")
    elif action == "reactivate":
        user.is_active = True
        user.auth_version += 1
        auth.record("user_reactivated", "user", user.id)
        flash(f"{user.email} can sign in again.")
    elif action == "reset_password":
        password = form_data().get("password") or ""
        confirm = form_data().get("confirm_password") or ""
        problem = auth.password_problem(password, confirm)
        if problem:
            flash(problem)
        else:
            from werkzeug.security import generate_password_hash

            user.password_hash = generate_password_hash(password)
            user.must_change_password = True
            user.auth_version += 1
            auth.revoke_trusted_devices(user)
            auth.record("password_reset", "user", user.id)
            flash(
                f"Password reset for {user.email}; they must change it at next sign-in."
            )
    elif action == "reset_mfa":
        if user.id == actor.id:
            flash("Use Account security to replace your own authenticator.")
        else:
            auth.reset_mfa(user)
            auth.record("mfa_reset_by_admin", "user", user.id)
            flash(
                f"Two-step verification reset for {user.email}; "
                "they must enroll again at next sign-in."
            )
    db.session.commit()
    return navigate(url_for("api.admin_users"))
