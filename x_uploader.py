import streamlit as st


def upload_to_x(video_path, caption, publish_mode="Public", schedule_iso_utc=None):
    token = str(st.secrets.get("X_ACCESS_TOKEN", "")).strip()
    if not token:
        raise RuntimeError(
            "X (Twitter) par automated video upload ke liye X Developer API (Basic/paid tier) aur X_ACCESS_TOKEN chahiye."
        )
    raise RuntimeError("X media upload token mila, par X API v2 chunked media endpoint abhi configure nahi kiya gaya.")