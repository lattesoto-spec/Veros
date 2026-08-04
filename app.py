"""Entrypoint.

Vercel looks for a Flask instance named `app` here, and the Dockerfile runs
`gunicorn app:app`. The application itself lives in the carelog package.
"""

import os
import pathlib

# Local development reads .env; deployed environments set real variables and
# have no such file, so this is a no-op there.
_env = pathlib.Path(__file__).with_name(".env")
if _env.is_file():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _, _value = _line.partition("=")
        os.environ.setdefault(_key.strip(), _value.strip())

from carelog.app import create_app  # noqa: E402  (after .env is loaded)

app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=8080)
