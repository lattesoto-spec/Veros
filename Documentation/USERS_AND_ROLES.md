# Users, organizations and roles

## The tenant model

An **Organization** is a customer. Every facility, resident, shift, import
receipt, learned format and integration belongs to exactly one, and a **User**
belongs to exactly one organization. There is no UI path that crosses that
boundary.

Two mechanisms enforce it, and both matter:

1. **Scoped reads.** Care data is only reached through `current_facility()`,
   which filters by `auth.current_organization_id()`. Nothing calls
   `Facility.query.first()` any more — that was the single-tenant assumption.
2. **Ownership checks on ids from URLs.** `auth.owned_or_404()` guards anything
   fetched by an id in the path (import jobs, receipts, evidence files). Without
   it, scoped reads alone would still let someone type another customer's
   receipt id into the address bar.

Login is enforced by a `before_request` hook rather than per-route decorators,
so a newly added route is protected by default. Only the login page, the
stylesheet and the internal worker endpoint are exempt.

## Roles

| Role | Can do |
| --- | --- |
| **Administrator** | Full provider workspace, including managing users |
| **Facility Manager** | Imports, targets, integrations, evidence — everything except user management |
| **Clinical Manager** | Dashboard, compliance, scenarios, exports, audit trail (read-only on data) |
| **Compliance Officer** | Imports, exports, audit trail and evidence downloads — no integrations |
| **Auditor** | Read-only: figures, audit trail, evidence downloads, exports |
| **Read Only** | Dashboard and compliance position only |

Permissions are declared in `carelog/auth.py`; routes are gated with
`@require("permission")` and the navigation hides what a role cannot open.

`is_superuser` is deliberately separate from these roles. It marks you, the
platform operator, and grants access across organizations for support — it is
not something a customer administrator can grant.

## The platform owner console

A superuser signs into a separate platform-administration experience. They do
not have a provider dashboard, compliance screen or facility of their own.
From **Platform → Accounts** they can:

- see every client organisation with its administrators, user count,
  facilities, residents, shifts and last import;
- create a client organisation together with its first administrator in one
  step (they set their own password at first sign-in, so you never hold it);
- **enter** an organisation for support. The whole application then renders
  that client's data, with a banner on every page saying whose data you are
  looking at, until you leave.
- open an account overview covering its users, facilities, imports,
  connections and recent activity without entering the provider workspace;
- review the cross-account activity log and platform system status.

Entering and leaving are written to the audit log as `platform_acting_as` and
`platform_stopped_acting`, and organisation creation as `platform_org_created`.
Every platform route returns **404** to a non-superuser rather than 403 — a
customer administrator has no business learning the console exists.

To promote an existing account:

```bash
flask --app app promote-superuser you@example.com
```

## The demo environment

The demo is a normal tenant with generated data, never a mode inside a real
customer's organisation:

```bash
flask --app app seed-demo --password 'a-long-demo-password'
```

That creates a `Demo` organisation, its own administrator
(`demo@caremin.app`), one facility and roughly 60 days of residents, staff and
shifts, deliberately rostered a little under target so the dashboard shows
amber rather than a flat green. The generator is seeded with a fixed value, so
the demo looks the same every time it is rebuilt, and it writes rows directly
rather than through the import pipeline — no Anthropic call, no cost, works
offline.

## Creating the first account

```bash
DATABASE_URL='postgres://…' python -m flask --app app bootstrap-org \
  --name "Sunrise Aged Care" \
  --admin-email you@example.com \
  --password 'a-long-temporary-password' \
  --superuser --adopt-existing
```

`--adopt-existing` claims rows that predate multi-tenancy (they carry a NULL
organization) for the new organization. Use it exactly once, when upgrading a
database that was previously single-tenant, otherwise the existing data is
invisible after the upgrade.

Administrators add further users from **Administration → Users**. New accounts
get a temporary password and must change it at first sign-in.

## Multiple facilities

An organization can run any number of homes. The active one is held in the
session and re-checked against the organization on every request — a session
value is user-supplied and must never select a row on its own. Switch between
them from the sidebar; manage them under **Setup → Facilities**, where each
facility carries its own care-minute and RN targets.

Imports name their target facility explicitly, so an upload cannot silently
land in whichever home happened to be selected. Deleting a facility removes its
shifts, staff, residents and receipts with it.

## Security posture today

- Passwords hashed with Werkzeug's scrypt default; minimum 12 characters.
- Session cookies are HttpOnly, SameSite=Lax, Secure in any deployed
  environment, and expire after 12 hours.
- Failed sign-ins are recorded with the IP address, and the response is
  identical for an unknown email, a wrong password and a deactivated account,
  so the form cannot be used to discover valid addresses.
- Sign-ins, role changes, deactivations, password resets, imports, facility
  edits and data deletion are written to an append-only `audit_logs` table.
  The cross-account activity view is restricted to platform owners; provider
  users receive the import evidence audit trail relevant to their work.
- The Anthropic API key is read from the environment only. It used to be
  storable from the Settings page, which put a platform credential in tenant
  data where any administrator could read or replace it.
- Diagnostics (`/debug/*`) require a superuser session or the `DEBUG_TOKEN`
  header, and return 404 otherwise rather than advertising their existence.
- `/clear` deletes only the caller's own organization. It previously emptied
  every table for every tenant.

## Not built yet

These are on your roadmap and are **not** implemented. Listing them so nothing
is assumed to be working:

- **Multi-factor authentication, SSO, API keys** (Phase 12). Password auth only.
- **Password reset by email.** An administrator sets a temporary password;
  there is no self-service "forgot password" flow because there is no mail
  transport yet.
- **Rate limiting / lockout** on repeated failed sign-ins. Attempts are logged
  but not throttled.
- **Notifications of any kind** (Phase 8) — no email, SMS, Teams, Slack or
  digests. There is no mail transport configured at all.
- **Approval workflow.** Phase 5 asks for "modified values" and "who approved";
  imported figures are currently immutable, so there is nothing to approve. The
  evidence chain that *does* exist is: source file, upload date, imported by
  (now a real user), parser version, calculation version and original row
  number, all reachable from the audit trail.
- **Settings depth** (Phase 11): resident categories, award rules, shift types,
  care classifications, public holidays and parser management are not
  configurable. Time zone is stored on the organization but not yet applied to
  display — all timestamps shown are UTC.
- **Encryption at rest beyond what the providers give you**, and backup policy.
  Neon and Vercel Blob encrypt at rest; there is no application-level
  encryption and no tested restore procedure.
