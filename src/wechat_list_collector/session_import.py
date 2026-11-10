from __future__ import annotations

import json
from pathlib import Path

from .errors import MissingCredentials
from .models import Credentials


def import_werss_license(path: Path) -> Credentials:
    """Read only the token/cookie pair from a local we-mp-rss wx.lic file."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    token_data = payload.get("token_data", {})
    token = str(token_data.get("token", "")).strip()
    cookie = str(token_data.get("cookie", "")).strip()
    if not token or not cookie:
        raise MissingCredentials("wx.lic does not contain token_data.token and token_data.cookie")
    return Credentials(token=token, cookie=cookie)
