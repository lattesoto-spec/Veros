"""Entrypoint.

Vercel looks for a Flask instance named `app` here, and the Dockerfile runs
`gunicorn app:app`. The application itself lives in the carelog package.
"""

from carelog.app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=8080)
