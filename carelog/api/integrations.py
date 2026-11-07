"""Connector availability. Care evidence enters through reviewed retained-file jobs."""

from flask import request

from carelog.api import bp
from carelog.api.responses import resource
from carelog.auth import require


@bp.route("/integrations", methods=["GET", "POST"])
@require("manage_integrations")
def integrations_view():
    if request.method == "POST":
        return {"error": "URL connections are unavailable. Download the source export and use Imports so evidence is retained and reviewed."}, 409
    return resource("integrations", platforms=[], configs={},
                    notice="URL connections are unavailable in this release. Download your source-system export and use Imports to review and retain the evidence.")
