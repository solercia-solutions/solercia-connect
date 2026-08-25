from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from solercia_connect.secrets import SecretValue


def to_primitive(value: Any) -> Any:
    if isinstance(value, SecretValue):
        return "<redacted>"
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value):
        return {field.name: to_primitive(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): to_primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [to_primitive(item) for item in value]
    return value


def to_json(value: Any, *, pretty: bool = True) -> str:
    return json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        indent=2 if pretty else None,
        sort_keys=True,
    )
