# Access and accounts

All provider data belongs to an organisation. Administrators can create provider users, change roles, issue a temporary password, revoke access and reset another user's authenticator after verifying identity. Passwords require at least 12 characters and confirmation. A temporary password must be changed before enrollment/workspace access.

Every account uses authenticator-app MFA. Ten hashed single-use recovery codes are issued at enrollment. Trusted devices can skip the second factor for 30 days, but still require a password; users can revoke them from Account security. Password changes, role changes, deactivation and MFA reset invalidate existing sessions/devices as appropriate. Failed sign-ins are throttled in the database.

The permission sets in `carelog/auth.py:ROLES` are authoritative. React hides unavailable controls; every API operation independently enforces permission and tenant ownership. Platform operators have a separate console and must explicitly enter a provider support workspace before modifying provider data. Platform endpoints return 404 to provider users.

Forgot-password and Contact pages give support addresses. No password-reset email transport exists. Never promise that a reset email was sent. Platform credentials, including the mapping provider key, are configured only through the environment.
