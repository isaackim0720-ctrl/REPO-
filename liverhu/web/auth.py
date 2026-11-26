"""Shared lab password (stored only as a salted PBKDF2 hash in <data>/auth.json)."""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
from pathlib import Path

_ITER = 200_000


def _file(data: Path) -> Path:
    return Path(data) / "auth.json"


def has_password(data: Path) -> bool:
    return _file(data).exists()


def _hash(password: str, salt: bytes) -> str:
    return base64.b64encode(hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITER)).decode()


def set_password(data: Path, password: str) -> None:
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    _file(data).write_text(json.dumps({"salt": base64.b64encode(salt).decode(), "hash": _hash(password, salt),
                                       "iterations": _ITER}))


def check_password(data: Path, password: str) -> bool:
    try:
        rec = json.loads(_file(data).read_text())
    except (OSError, ValueError):
        return False
    salt = base64.b64decode(rec["salt"])
    return hmac.compare_digest(_hash(password, salt), rec["hash"])


def set_password_interactive(data: Path) -> None:
    Path(data).mkdir(parents=True, exist_ok=True)
    while True:
        p1 = getpass.getpass("New website password (min 8 characters): ")
        p2 = getpass.getpass("Type it again: ")
        if p1 != p2:
            print("Passwords did not match, try again.")
            continue
        try:
            set_password(data, p1)
        except ValueError as e:
            print(e)
            continue
        print("Password saved.")
        return


def first_time_password(data: Path) -> None:
    env = os.environ.get("LIVERHU_PASSWORD")
    if env:
        set_password(data, env)
        print("Website password taken from LIVERHU_PASSWORD.")
    elif sys.stdin and sys.stdin.isatty():
        print("First start: choose the password lab members will use to open the website.")
        set_password_interactive(data)
    else:
        pw = secrets.token_urlsafe(9)
        set_password(data, pw)
        print(f"Generated website password: {pw}   (change with: python -m liverhu set-password)")
