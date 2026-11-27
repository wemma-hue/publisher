"""Facebook / Instagram posting through Buffer (no Meta developer account needed).

How it works:
  1. The video is uploaded to Cloudinary to get a public URL (Buffer cannot take file uploads).
  2. A post is created in Buffer through its GraphQL API, for the Facebook / Instagram
     channel you connected inside Buffer with your normal Facebook login.

Secrets (Streamlit -> Manage app -> Settings -> Secrets):
  BUFFER_API_KEY        optional. If missing, the app shows a password box instead.
  CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET
  BUFFER_FACEBOOK_TYPE  optional, "post" (default) or "reel"
"""
import hashlib
import json
import time

import requests
import streamlit as st

BUFFER_URL = "https://api.buffer.com"


# ---------------------------------------------------------------- Buffer key
def buffer_key_from_secrets() -> str:
    try:
        return str(st.secrets.get("BUFFER_API_KEY", "")).strip()
    except Exception:
        return ""


def get_buffer_key() -> str:
    return buffer_key_from_secrets() or str(st.session_state.get("buffer_key_input", "")).strip()


# ---------------------------------------------------------------- GraphQL
def _gql(api_key, query, variables=None):
    resp = requests.post(
        BUFFER_URL,
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=60,
    )
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"Buffer ne unexpected response diya (HTTP {resp.status_code}).")
    if resp.status_code >= 400 or data.get("errors"):
        msg = "; ".join(e.get("message", "") for e in data.get("errors", [])) or str(data)
        raise RuntimeError(f"Buffer error: {msg}")
    return data.get("data") or {}


def list_buffer_channels(api_key):
    """Returns [{'id','name','service'}, ...]. Call rarely: free plan has a monthly request limit."""
    orgs = _gql(api_key, "query { account { organizations { id name } } }")["account"]["organizations"]
    channels = []
    for org in orgs:
        q = "query { channels(input: { organizationId: %s }) { id name service } }" % json.dumps(org["id"])
        channels.extend(_gql(api_key, q).get("channels") or [])
    return channels


_CREATE_POST = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id dueAt } }
    ... on MutationError { message }
  }
}
"""


def create_buffer_post(api_key, channel, text, video_url, publish_mode="Public", schedule_iso_utc=None):
    service = (channel.get("service") or "").lower()
    mode = publish_mode.strip().lower()

    base = {"text": text or "", "channelId": channel["id"], "schedulingType": "automatic"}

    if mode == "schedule":
        if not schedule_iso_utc:
            raise ValueError("Schedule mode ke liye date aur time zaroori hai.")
        base["mode"] = "customScheduled"
        base["dueAt"] = schedule_iso_utc.replace("Z", ".000Z") if schedule_iso_utc.endswith("Z") and "." not in schedule_iso_utc else schedule_iso_utc
    elif mode == "private":
        base["mode"] = "addToQueue"
        base["saveToDraft"] = True  # saved as a draft in Buffer, not published
    else:
        base["mode"] = "shareNow"

    if service == "instagram":
        base["metadata"] = {"instagram": {"type": "reel", "shouldShareToFeed": True}}
    elif service == "facebook":
        try:
            fb_type = str(st.secrets.get("BUFFER_FACEBOOK_TYPE", "post")).strip() or "post"
        except Exception:
            fb_type = "post"
        base["metadata"] = {"facebook": {"type": fb_type}}

    # Buffer's docs show two shapes for video assets. Try the current one first.
    shapes = [
        [{"video": {"url": video_url}}],
        {"videos": [{"url": video_url}]},
    ]
    first_error = None
    for assets in shapes:
        try:
            data = _gql(api_key, _CREATE_POST, {"input": {**base, "assets": assets}})
        except RuntimeError as e:
            low = str(e).lower()
            if first_error is None:
                first_error = e
            if any(w in low for w in ("asset", "video", "variable", "invalid value")):
                continue
            raise
        result = data.get("createPost") or {}
        if result.get("post"):
            return result["post"]
        raise RuntimeError(result.get("message") or str(result))
    raise first_error


# ---------------------------------------------------------------- Cloudinary
def _cloudinary_creds():
    cloud = str(st.secrets.get("CLOUDINARY_CLOUD_NAME", "")).strip()
    key = str(st.secrets.get("CLOUDINARY_API_KEY", "")).strip()
    secret = str(st.secrets.get("CLOUDINARY_API_SECRET", "")).strip()
    if not (cloud and key and secret):
        raise RuntimeError(
            "Secrets mein CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY aur "
            "CLOUDINARY_API_SECRET set karein."
        )
    return cloud, key, secret


def _cloudinary_sign(params, secret):
    to_sign = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.sha1((to_sign + secret).encode("utf-8")).hexdigest()


def upload_video_public(video_path):
    """Upload to Cloudinary and return a public https URL.
    The file is NOT deleted afterwards: Buffer downloads it when the post goes out
    (for scheduled posts that can be days later). Clean old videos from the
    Cloudinary Media Library now and then."""
    cloud, key, secret = _cloudinary_creds()
    timestamp = int(time.time())
    signature = _cloudinary_sign({"timestamp": timestamp}, secret)
    with open(video_path, "rb") as f:
        resp = requests.post(
            f"https://api.cloudinary.com/v1_1/{cloud}/video/upload",
            data={"api_key": key, "timestamp": timestamp, "signature": signature},
            files={"file": f},
            timeout=600,
        )
    data = resp.json()
    if resp.status_code >= 400 or "secure_url" not in data:
        raise RuntimeError("Cloudinary upload failed: " + str(data.get("error", {}).get("message") or data))
    return data["secure_url"]
