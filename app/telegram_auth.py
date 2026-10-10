"""Validation of Telegram Mini App initData (https://core.telegram.org/bots/webapps)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE_SECONDS = 24 * 3600


class AuthError(Exception):
    pass


def validate_init_data(init_data: str, bot_token: str, max_age: int = MAX_AGE_SECONDS) -> dict:
    """Return the Telegram user dict if `init_data` is authentic and fresh."""
    if not init_data:
        raise AuthError("missing initData")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received = pairs.pop("hash", None)
    if not received:
        raise AuthError("initData has no hash")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise AuthError("initData signature mismatch")
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError as e:
        raise AuthError("bad auth_date") from e
    if time.time() - auth_date > max_age:
        raise AuthError("initData expired; reopen the Mini App")
    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError as e:
        raise AuthError("bad user field") from e
    if "id" not in user:
        raise AuthError("initData has no user")
    return user
