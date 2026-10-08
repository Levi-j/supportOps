import re
from typing import Any

MASK = "***"
API_KEY_PREFIX = "bk_"
API_KEY_VISIBLE_LENGTH = 12

_URL_CREDENTIALS = re.compile(
    r"(?P<head>\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:)(?P<password>[^\s/]*)@"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_SCHEME_TOKEN = re.compile(r"(?i)\b(?P<scheme>bearer|basic)\s+(?P<token>[A-Za-z0-9._~+/=*-]{8,})")
_AUTHORIZATION = re.compile(
    r"(?i)\b(?P<head>authorization\s*[:=]\s*)(?!(?:bearer|basic)\s)(?P<token>[^\s,;\"']+)"
)
_API_KEY = re.compile(r"\bbk_[A-Za-z0-9_]{10,}")
SECRET_NAME_WORDS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
)

_JSON_SECRET = re.compile(
    r"(?i)(?P<head>\"[A-Za-z0-9_.-]*(?:password|passwd|secret|token|api_key|apikey"
    r"|authorization|cookie)[A-Za-z0-9_.-]*\"\s*:\s*\")(?P<value>[^\"]*)(?P<tail>\")"
)
_KEY_VALUE_SECRET = re.compile(
    r"(?i)\b(?P<head>[A-Za-z0-9_]*(?:password|passwd|secret|token|api_key|apikey|cookie)"
    r"[A-Za-z0-9_]*'?\s*[=:]\s*'?)(?P<value>[^\s,;&\"']+)"
)
_EMAIL = re.compile(
    r"\b(?P<first>[A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@"
    r"(?P<domain>[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b"
)


def mask_api_key(value: str) -> str:
    if value.startswith(API_KEY_PREFIX) and len(value) > API_KEY_VISIBLE_LENGTH:
        return value[:API_KEY_VISIBLE_LENGTH] + MASK
    return MASK


def redact_dsn(dsn: str) -> str:
    scheme, separator, rest = dsn.partition("://")
    userinfo, at, location = rest.rpartition("@")
    if not separator or not at or ":" not in userinfo:
        return dsn
    user = userinfo.split(":", 1)[0]
    return f"{scheme}://{user}:{MASK}@{location}"


def redact_text(text: str) -> str:
    text = _URL_CREDENTIALS.sub(lambda m: f"{m['head']}{MASK}@", text)
    text = _JWT.sub(MASK, text)
    text = _SCHEME_TOKEN.sub(lambda m: f"{m['scheme']} {mask_api_key(m['token'])}", text)
    text = _AUTHORIZATION.sub(lambda m: f"{m['head']}{mask_api_key(m['token'])}", text)
    text = _API_KEY.sub(lambda m: mask_api_key(m[0]), text)
    text = _JSON_SECRET.sub(lambda m: f"{m['head']}{MASK}{m['tail']}", text)
    text = _KEY_VALUE_SECRET.sub(lambda m: f"{m['head']}{MASK}", text)
    return _EMAIL.sub(lambda m: f"{m['first']}{MASK}@{m['domain']}", text)


def is_secret_name(name: str) -> bool:
    lowered = name.lower().replace("-", "_")
    return any(word in lowered for word in SECRET_NAME_WORDS)


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: MASK if isinstance(item, str) and is_secret_name(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    return value
