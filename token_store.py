"""Tiny server-side token store.

Why: Streamlit's st.session_state is wiped every time the browser leaves the
app (e.g. to facebook.com for login) and comes back. Tokens saved here live on
the server, so YouTube and Meta logins survive each other's redirects.

IMPORTANT: add `token_store.json` to .gitignore. Never commit it to GitHub.
"""
import json
import os
import threading
from pathlib import Path

_PATH = Path(os.environ.get("TOKEN_STORE_PATH", "token_store.json"))
_LOCK = threading.Lock()


def _read() -> dict:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load(key, default=None):
    with _LOCK:
        return _read().get(key, default)


def save(key, value):
    with _LOCK:
        data = _read()
        data[key] = value
        _PATH.write_text(json.dumps(data), encoding="utf-8")


def remove(key):
    with _LOCK:
        data = _read()
        if key in data:
            del data[key]
            _PATH.write_text(json.dumps(data), encoding="utf-8")
