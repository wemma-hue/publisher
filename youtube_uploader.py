from datetime import datetime, time as dt_time, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
import requests
import streamlit as st
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

import token_store

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
REDIRECT_URI = "https://multipublisher.streamlit.app/"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
OAUTH_QUERY_KEYS = ("code", "state", "error", "scope", "authuser", "prompt")
STORE_KEY = "youtube"


def _clear_oauth_query_params():
    for key in OAUTH_QUERY_KEYS:
        if key in st.query_params:
            del st.query_params[key]


def _creds_to_dict(creds: Credentials) -> dict:
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri or TOKEN_URI,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or SCOPES),
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }


def _dict_to_creds(data: dict) -> Credentials | None:
    if not data or not data.get("token"):
        return None
    expiry = None
    if data.get("expiry"):
        try:
            expiry = datetime.fromisoformat(data["expiry"])
        except ValueError:
            expiry = None
    return Credentials(
        token=data["token"],
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri") or TOKEN_URI,
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes") or SCOPES,
        expiry=expiry,
    )


def _save_yt_creds(creds: Credentials):
    st.session_state["yt_credentials"] = creds
    token_store.save(STORE_KEY, _creds_to_dict(creds))


def restore_yt_creds_from_session() -> Credentials | None:
    """Session first, then the server-side store (survives redirects)."""
    creds = st.session_state.get("yt_credentials")
    if isinstance(creds, Credentials) and creds.token:
        return creds
    restored = _dict_to_creds(token_store.load(STORE_KEY))
    if restored:
        st.session_state["yt_credentials"] = restored
    return restored


def disconnect_youtube():
    st.session_state["yt_credentials"] = None
    st.session_state["yt_service"] = None
    token_store.remove(STORE_KEY)


def to_iso_utc(schedule_date, schedule_time, tz_name="Asia/Kolkata"):
    if not isinstance(schedule_time, dt_time):
        schedule_time = dt_time(12, 0)
    local_dt = datetime.combine(schedule_date, schedule_time).replace(tzinfo=ZoneInfo(tz_name))
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_yt_creds():
    client_id = str(st.secrets.get("YOUTUBE_CLIENT_ID", st.secrets.get("CLIENT_ID", ""))).strip()
    client_secret = str(st.secrets.get("YOUTUBE_CLIENT_SECRET", st.secrets.get("CLIENT_SECRET", ""))).strip()
    if not client_id or not client_secret:
        st.error("Secrets mein YOUTUBE_CLIENT_ID ya YOUTUBE_CLIENT_SECRET missing hai.")
        st.stop()
    return client_id, client_secret


def get_auth_url(client_id):
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": "youtube",
    }
    return f"{AUTH_URI}?{urlencode(params)}"


def exchange_code_for_creds(auth_code, client_id, client_secret):
    clean_code = str(auth_code).strip()
    if not clean_code:
        raise RuntimeError("Empty auth code received.")

    resp = requests.post(
        TOKEN_URI,
        data={
            "code": clean_code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    data = resp.json()
    if resp.status_code != 200 or "access_token" not in data:
        raise RuntimeError(data.get("error_description") or data.get("error") or str(data))

    # google-auth expects a naive UTC datetime for expiry
    expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
        seconds=int(data.get("expires_in", 3600))
    )
    return Credentials(
        token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
        expiry=expiry,
    )


def process_youtube_oauth_callback():
    """Call once at the top of app.py, before any widgets are drawn.
    Handles the return from Google. Never calls st.stop()."""
    if st.query_params.get("state") != "youtube":
        return

    oauth_error = st.query_params.get("error")
    auth_code = st.query_params.get("code")

    if oauth_error:
        _clear_oauth_query_params()
        st.session_state["yt_error"] = f"YouTube login error: {oauth_error}"
        return

    if auth_code:
        used_codes = st.session_state.setdefault("yt_used_codes", set())
        if auth_code in used_codes:
            _clear_oauth_query_params()
            return
        used_codes.add(auth_code)
        client_id, client_secret = get_yt_creds()
        try:
            _save_yt_creds(exchange_code_for_creds(auth_code, client_id, client_secret))
            st.session_state["yt_error"] = None
            st.session_state["cb_yt"] = True
        except Exception as e:
            st.session_state["yt_error"] = f"YouTube token error: {e}"
        finally:
            _clear_oauth_query_params()
        return

    _clear_oauth_query_params()


def get_authenticated_youtube_service():
    """Returns a YouTube service, or None if not connected. Never stops the page."""
    creds = restore_yt_creds_from_session()
    if creds is None:
        return None

    if not creds.valid:
        if creds.refresh_token:
            try:
                creds.refresh(Request())
                _save_yt_creds(creds)
            except Exception:
                disconnect_youtube()
                return None
        else:
            disconnect_youtube()
            return None

    return build("youtube", "v3", credentials=creds)


def upload_to_youtube(
    youtube,
    video_path,
    title,
    description,
    publish_mode="Public",
    schedule_iso_utc=None,
):
    mode = publish_mode.strip().lower()

    if mode == "schedule":
        if not schedule_iso_utc:
            raise ValueError("Schedule mode ke liye date aur time zaroori hai.")
        status_body = {
            "privacyStatus": "private",
            "publishAt": schedule_iso_utc,
        }
    elif mode == "private":
        status_body = {"privacyStatus": "private"}
    else:
        status_body = {"privacyStatus": "public"}

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "22",
        },
        "status": status_body,
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    return response
