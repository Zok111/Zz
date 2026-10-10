import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from app.telegram_auth import AuthError, validate_init_data

TOKEN = "123456:TEST-token"


def sign(fields: dict, token: str = TOKEN) -> str:
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


def fields(age: int = 0) -> dict:
    return {"auth_date": str(int(time.time()) - age), "query_id": "AAE",
            "user": json.dumps({"id": 777, "first_name": "Ann"}), "signature": "abc"}


def test_valid():
    assert validate_init_data(sign(fields()), TOKEN)["id"] == 777


def test_tampered():
    data = sign(fields()).replace("777", "778")
    with pytest.raises(AuthError):
        validate_init_data(data, TOKEN)


def test_wrong_token():
    with pytest.raises(AuthError):
        validate_init_data(sign(fields(), "999:other"), TOKEN)


def test_expired():
    with pytest.raises(AuthError):
        validate_init_data(sign(fields(age=3 * 86400)), TOKEN)
