import base64
from datetime import datetime, time as dt_time, timezone
import json
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
import requests
import streamlit as st
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
REDIRECT_URI = "https://multi-platform-publisher.streamlit.app/"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
OAUTH_QUERY_KEYS = ("code", "state", "error", "scope", "authuser", "prompt")


def _clear_oauth_query_params():
    # Sirf OAuth temporary params hatayein; 'connected' aur 'yt_token' ko chhua bhi na jaye
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
    }


def _dict_to_creds(data: dict) -> Credentials | None:
    if not data or not data.get("token"):
        return None
    return Credentials(
        token=data["token"],
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri") or TOKEN_URI,
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes") or SCOPES,
    )


def _encode_creds_param(creds_dict: dict) -> str:
    raw = json.dumps(creds_dict, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_creds_param(encoded: str) -> dict | None:
    if not encoded:
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _save_yt_creds(creds: Credentials):
    creds_dict = _creds_to_dict(creds)
    st.session_state["yt_credentials"] = creds
    st.session_state["yt_creds_dict"] = creds_dict
    try:
        st.query_params["yt_token"] = _encode_creds_param(creds_dict)
    except Exception:
        pass


def restore_yt_creds_from_session() -> Credentials | None:
    creds = st.session_state.get("yt_credentials")
    if isinstance(creds, Credentials) and creds.token:
        if "yt_token" not in st.query_params:
            try:
                st.query_params["yt_token"] = _encode_creds_param(_creds_to_dict(creds))
            except Exception:
                pass
        return creds

    creds_dict = st.session_state.get("yt_creds_dict")
    if not creds_dict:
        qp_token = st.query_params.get("yt_token", "")
        creds_dict = _decode_creds_param(qp_token)
        if creds_dict:
            st.session_state["yt_creds_dict"] = creds_dict

    restored = _dict_to_creds(creds_dict) if creds_dict else None
    if restored:
        st.session_state["yt_credentials"] = restored
    return restored


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

    return Credentials(
        token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )


def get_authenticated_youtube_service():
    client_id, client_secret = get_yt_creds()
    creds = restore_yt_creds_from_session()

    if creds and creds.valid:
        _save_yt_creds(creds)
        return build("youtube", "v3", credentials=creds)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_yt_creds(creds)
            return build("youtube", "v3", credentials=creds)
        except Exception:
            st.session_state.pop("yt_credentials", None)
            st.session_state.pop("yt_creds_dict", None)
            if "yt_token" in st.query_params:
                del st.query_params["yt_token"]

    auth_url = get_auth_url(client_id)
    oauth_state = st.query_params.get("state")
    oauth_error = st.query_params.get("error")
    auth_code = st.query_params.get("code")
    used_codes = st.session_state.setdefault("yt_used_codes", set())

    if oauth_state and oauth_state != "youtube":
        st.markdown(f"### 👉 [Click here to Connect YouTube]({auth_url})")
        st.stop()

    if oauth_error:
        _clear_oauth_query_params()
        st.error(f"YouTube login error: {oauth_error}")
        st.markdown(f"### 👉 [Click here to Connect YouTube]({auth_url})")
        st.stop()

    if auth_code:
        if auth_code in used_codes:
            _clear_oauth_query_params()
            st.rerun()

        used_codes.add(auth_code)
        try:
            new_creds = exchange_code_for_creds(auth_code, client_id, client_secret)
            _save_yt_creds(new_creds)
            _clear_oauth_query_params()
            st.rerun()
        except Exception as e:
            _clear_oauth_query_params()
            st.error(f"YouTube token error: {e}")
            st.markdown(f"### 👉 [Click here to Connect YouTube]({auth_url})")
            st.stop()

    st.markdown(f"### 👉 [Click here to Connect YouTube]({auth_url})")
    st.stop()


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
