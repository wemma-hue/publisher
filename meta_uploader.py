import time
from urllib.parse import urlencode
import requests
import streamlit as st

GRAPH_VERSION = "v21.0"
META_REDIRECT_URI = "https://multi-platform-publisher.streamlit.app/"
META_AUTH_URI = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth"
META_TOKEN_URI = f"https://graph.facebook.com/{GRAPH_VERSION}/oauth/access_token"

META_SCOPES = [
    "public_profile",
]

META_OAUTH_QUERY_KEYS = (
    "code",
    "state",
    "error",
    "error_code",
    "error_description",
    "error_reason",
)


def _clear_meta_oauth_params():
    for key in META_OAUTH_QUERY_KEYS:
        if key in st.query_params:
            del st.query_params[key]


def get_meta_app_creds():
    app_id = str(st.secrets.get("META_APP_ID", "")).strip()
    app_secret = str(st.secrets.get("META_APP_SECRET", "")).strip()
    return app_id, app_secret


def get_meta_auth_url(app_id, platform_hint="facebook"):
    params = {
        "client_id": app_id,
        "redirect_uri": META_REDIRECT_URI,
        "state": f"meta_oauth:{platform_hint}",
        "scope": ",".join(META_SCOPES),
    }
    if platform_hint == "instagram":
        params["auth_type"] = "reauthenticate"
    return f"{META_AUTH_URI}?{urlencode(params)}"


def exchange_meta_code(code, app_id, app_secret):
    resp = requests.get(
        META_TOKEN_URI,
        params={
            "client_id": app_id,
            "client_secret": app_secret,
            "redirect_uri": META_REDIRECT_URI,
            "code": code,
        },
        timeout=30,
    )
    data = resp.json()
    if resp.status_code != 200 or "access_token" not in data:
        raise RuntimeError(data.get("error", {}).get("message") or str(data))
    return data["access_token"]


def fetch_user_pages_and_ig(user_access_token):
    resp = requests.get(
        f"https://graph.facebook.com/{GRAPH_VERSION}/me/accounts",
        params={
            "fields": "id,name,access_token,instagram_business_account{id,username}",
            "access_token": user_access_token,
        },
        timeout=30,
    )
    data = resp.json()
    if resp.status_code >= 400:
        raise RuntimeError(data.get("error", {}).get("message") or str(data))
    return data.get("data", [])


def process_meta_oauth_callback():
    state = str(st.query_params.get("state", ""))
    if not state.startswith("meta_oauth"):
        return None

    platform_hint = state.split(":", 1)[1] if ":" in state else "facebook"
    oauth_error = st.query_params.get("error_description") or st.query_params.get("error")
    auth_code = st.query_params.get("code")
    used_codes = st.session_state.setdefault("meta_used_codes", set())

    if oauth_error:
        _clear_meta_oauth_params()
        st.session_state["meta_error"] = f"Meta login error: {oauth_error}"
        return platform_hint

    if auth_code:
        if auth_code in used_codes:
            _clear_meta_oauth_params()
            return platform_hint

        used_codes.add(auth_code)
        app_id, app_secret = get_meta_app_creds()
        if not app_id or not app_secret:
            _clear_meta_oauth_params()
            st.session_state["meta_error"] = "Secrets mein META_APP_ID aur META_APP_SECRET daalein."
            return platform_hint

        try:
            user_token = exchange_meta_code(auth_code, app_id, app_secret)
            pages = fetch_user_pages_and_ig(user_token)
            st.session_state["meta_user_token"] = user_token
            st.session_state["meta_pages"] = pages
            st.session_state["meta_connected"] = True
            st.session_state["meta_error"] = None
            if platform_hint == "facebook":
                st.session_state["cb_fb"] = True
            elif platform_hint == "instagram":
                st.session_state["cb_ig"] = True
        except Exception as e:
            st.session_state["meta_error"] = f"Meta connection error: {e}"
        finally:
            _clear_meta_oauth_params()

        return platform_hint

    _clear_meta_oauth_params()
    return platform_hint


def get_authenticated_meta_session():
    return st.session_state.get("meta_pages") or []


def upload_to_facebook(
    video_path,
    title,
    description,
    page_id,
    page_token,
    publish_mode="Public",
    schedule_unix_ts=None,
):
    if not page_id or not page_token:
        raise RuntimeError("Facebook Page connection nahi mila.")

    mode = publish_mode.strip().lower()
    payload = {
        "access_token": page_token,
        "title": title,
        "description": description,
    }

    if mode == "schedule":
        if not schedule_unix_ts:
            raise ValueError("Schedule mode ke liye date aur time zaroori hai.")
        payload["published"] = "false"
        payload["scheduled_publish_time"] = int(schedule_unix_ts)
    elif mode == "private":
        payload["published"] = "false"
    else:
        payload["published"] = "true"

    url = f"https://graph-video.facebook.com/{GRAPH_VERSION}/{page_id}/videos"
    with open(video_path, "rb") as f:
        resp = requests.post(url, data=payload, files={"source": f}, timeout=120)

    data = resp.json()
    if resp.status_code >= 400 or "id" not in data:
        raise RuntimeError(data.get("error", {}).get("message", str(data)))
    return data


def upload_to_instagram(
    video_path,
    caption,
    ig_user_id,
    page_token,
    publish_mode="Public",
    schedule_unix_ts=None,
):
    video_url = str(st.secrets.get("IG_VIDEO_URL", "")).strip()

    if not ig_user_id or not page_token:
        raise RuntimeError("Is Facebook Page se linked Instagram Business/Creator account nahi mila.")
    if not video_url:
        raise RuntimeError(
            "Instagram Graph API video publish ke liye public video_url mangti hai. "
            "Secrets mein IG_VIDEO_URL set karein."
        )

    mode = publish_mode.strip().lower()
    if mode == "schedule":
        raise RuntimeError(
            "Instagram Content Publishing API direct future scheduling ko directly support nahi karti; "
            "isse Public ya Private mode mein publish karein."
        )

    create_url = f"https://graph.facebook.com/{GRAPH_VERSION}/{ig_user_id}/media"
    create_resp = requests.post(
        create_url,
        data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "access_token": page_token,
        },
        timeout=60,
    ).json()

    creation_id = create_resp.get("id")
    if not creation_id:
        raise RuntimeError(create_resp.get("error", {}).get("message", str(create_resp)))

    status_url = f"https://graph.facebook.com/{GRAPH_VERSION}/{creation_id}"
    for _ in range(20):
        status_resp = requests.get(
            status_url,
            params={"fields": "status_code", "access_token": page_token},
            timeout=30,
        ).json()
        code = status_resp.get("status_code")
        if code == "FINISHED":
            break
        if code == "ERROR":
            raise RuntimeError("Instagram ne video processing mein error diya.")
        time.sleep(3)

    if mode == "private":
        return {"id": creation_id, "status": "container_created_not_published"}

    publish_url = f"https://graph.facebook.com/{GRAPH_VERSION}/{ig_user_id}/media_publish"
    pub_resp = requests.post(
        publish_url,
        data={
            "creation_id": creation_id,
            "access_token": page_token,
        },
        timeout=60,
    ).json()

    if "id" not in pub_resp:
        raise RuntimeError(pub_resp.get("error", {}).get("message", str(pub_resp)))
    return pub_resp
