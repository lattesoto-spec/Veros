"""Carelog — care minutes compliance for Australian aged care.

Layout:
    carelog/app.py        Flask application factory, routes, CLI commands
    carelog/models.py     SQLAlchemy models (organizations, users, care data)
    carelog/auth.py       Authentication, roles, the tenant boundary
    carelog/storage.py    Object storage for retained audit evidence
    carelog/domain/       Pure calculation and output building — no Flask
    carelog/ingestion/    Universal import: read, fingerprint, learn, map
    carelog/integrations/ Outbound connections to roster platforms

`app.py` at the repository root re-exports the application object, which is
the entrypoint Vercel discovers and the target of `gunicorn app:app`.
"""

def create_app(*args, **kwargs):
    """Lazy application factory so domain modules and tests do not create a
    database-bound Flask app merely by importing the package."""
    from .app import create_app as factory

    return factory(*args, **kwargs)


def init_db(*args, **kwargs):
    from .app import init_db as initialize

    return initialize(*args, **kwargs)

__all__ = ["create_app", "init_db"]
