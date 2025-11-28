import os
import threading
from datetime import datetime

import streamlit as st
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn
from yt_dlp import YoutubeDL

# ===========================================================
# 1. CONFIG – UPDATE THESE PATHS
# ===========================================================
# Main account (Account 1) – always used first
COOKIE_FILE_MAIN = "/tmp/www.instagram.com_cookies_main.txt"

# Second account (Account 2) – only used as fallback on specific errors
COOKIE_FILE_ALT = "/tmp/www.instagram.com_cookies_alt.txt"

def write_cookie_from_secret(secret_key: str, path: str) -> bool:
    """
    Read cookie text from st.secrets[secret_key] and write it to 'path'.
    Returns True if written, False if secret missing/empty.
    """
    value = st.secrets.get(secret_key, "")
    if not value or not str(value).strip():
        # If empty, make sure no stale file is left
        if os.path.exists(path):
            os.remove(path)
        return False

    Path(path).write_text(value, encoding="utf-8")
    return True

# ----- Create cookie files from secrets -----
main_ok = write_cookie_from_secret("INSTAGRAM_COOKIE_MAIN", COOKIE_FILE_MAIN)
alt_ok = write_cookie_from_secret("INSTAGRAM_COOKIE_ALT", COOKIE_FILE_ALT)

if not main_ok:
    # Stop early if main cookie is missing
    raise RuntimeError(
        "INSTAGRAM_COOKIE_MAIN is not set or empty in Streamlit secrets. "
        "Set it in the app's Secrets section."
    )

# ----- Build cookie pool (main first, alt optional) -----
cookie_pool = [COOKIE_FILE_MAIN]

if alt_ok:
    cookie_pool.append(COOKIE_FILE_ALT)
    print("[INFO] Alt Instagram cookie enabled from secrets.")
else:
    print("[INFO] No alt cookie in secrets. Running with main cookie only.")
    

# ===========================================================
# 2. Helper: Decide if we should retry with Account 2
# ===========================================================
def should_retry_with_alt_cookie(error_text: str) -> bool:
    """
    Only retry with Account 2 for errors that look like:
      - HTTP 400 issues
      - API access / empty responses
      - login / private / restricted problems
    """
    error_text = (error_text or "").lower()

    patterns = [
        "http error 400",
        "instagram api is not granting access",
        "instagram sent an empty media response",
        "login session is not accepted",
        "post is private/restricted",
    ]

    return any(p in error_text for p in patterns)


# ===========================================================
# 3. Helper: Map raw yt-dlp errors to user-friendly messages
# ===========================================================
def simplify_error_message(raw: str) -> str:
    raw_lower = (raw or "").lower()

    if "no video formats found" in raw_lower:
        return "No downloadable video found (might be an image, carousel, or removed)."

    if "instagram sent an empty media response" in raw_lower or \
       "instagram api is not granting access" in raw_lower or \
       "http error 400" in raw_lower or \
       "login session is not accepted" in raw_lower:
        return "Post is private/restricted or the login session is not accepted."

    if "unable to extract data" in raw_lower:
        return "Unable to extract data for this URL."

    if "functionality for this site has been marked as broken" in raw_lower:
        return "yt-dlp Instagram extractor is currently broken for this URL."

    return "Unknown error – see server logs."


# ===========================================================
# 4. Instagram Metadata Function (for a given cookie file)
# ===========================================================
def get_instagram_metadata(url: str, cookie_file: str) -> dict:
    """
    Fetch metadata for a single Instagram URL using the given cookie file.
    Raises whatever yt-dlp raises; caller handles retries / error logging.
    """
    ydl_opts = {
        "skip_download": True,
        "quiet": True,
        "cookiefile": cookie_file,
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    ts = info.get("timestamp")
    pub_date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else None
    pub_time = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else None

    return {
        "plays": info.get("view_count"),
        "comments": info.get("comment_count"),
        "likes": info.get("like_count"),
        "pub_date": pub_date,
        "pub_time": pub_time,
        "insta_id": info.get("uploader") or info.get("uploader_id"),
    }


# ===========================================================
# 6. FastAPI app – this is what Apps Script calls
# ===========================================================
api = FastAPI()


@api.post("/instagram-metadata")
async def instagram_metadata(request: Request):
    """
    Expected JSON body from Apps Script:
      { "link": "https://www.instagram.com/reel/..." }

    Response JSON (what your Apps Script already expects):
      {
        "plays": <int or ''>,
        "comments": <int or ''>,
        "likes": <int or ''>,
        "pub_date": "YYYY-MM-DD" or '',
        "pub_time": "HH:MM:SS" or '',
        "insta_id": <str or ''>,
        "error": <str or ''>
      }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {
                "plays": "",
                "comments": "",
                "likes": "",
                "pub_date": "",
                "pub_time": "",
                "insta_id": "",
                "error": "Invalid JSON body.",
            },
            status_code=400,
        )

    link = (body.get("link") or "").strip()

    if not link:
        return JSONResponse(
            {
                "plays": "",
                "comments": "",
                "likes": "",
                "pub_date": "",
                "pub_time": "",
                "insta_id": "",
                "error": "No link provided.",
            },
            status_code=400,
        )

    # If no cookies at all, fail fast
    if not cookie_pool:
        return JSONResponse(
            {
                "plays": "",
                "comments": "",
                "likes": "",
                "pub_date": "",
                "pub_time": "",
                "insta_id": "",
                "error": "No valid Instagram cookie files found on server.",
            },
            status_code=500,
        )

    last_error_raw = None
    success = False
    meta = None

    # Try main account first; only try alt account if specific types of errors occur
    for cookie_idx, cookie_file in enumerate(cookie_pool):
        try:
            meta = get_instagram_metadata(link, cookie_file)
            success = True
            break  # success, no need to try others
        except Exception as e:
            last_error_raw = str(e)
            # Try alt cookie only if we are on main cookie and error is "retry-worthy"
            if cookie_idx == 0 and len(cookie_pool) > 1 and should_retry_with_alt_cookie(last_error_raw):
                continue  # try next cookie
            else:
                break  # not retry-worthy or already using alt cookie

    if not success:
        friendly = simplify_error_message(last_error_raw)
        return JSONResponse(
            {
                "plays": "",
                "comments": "",
                "likes": "",
                "pub_date": "",
                "pub_time": "",
                "insta_id": "",
                "error": friendly,
            },
            status_code=200,  # we return 200 so Apps Script can still read error field
        )

    # Success: map None -> '' for Apps Script
    resp = {
        "plays": int(meta["plays"]) if meta.get("plays") is not None else "",
        "comments": int(meta["comments"]) if meta.get("comments") is not None else "",
        "likes": int(meta["likes"]) if meta.get("likes") is not None else "",
        "pub_date": meta.get("pub_date") or "",
        "pub_time": meta.get("pub_time") or "",
        "insta_id": meta.get("insta_id") or "",
        "error": "",
    }

    return JSONResponse(resp, status_code=200)


# ===========================================================
# 7. Start FastAPI (uvicorn) in background when Streamlit runs
# ===========================================================
def run_api():
    # host/port MUST match your Apps Script PYTHON_ENDPOINT
    uvicorn.run(api, host="0.0.0.0", port=8000, log_level="info")


if "api_server_started" not in st.session_state:
    st.session_state["api_server_started"] = False

if not st.session_state["api_server_started"]:
    thread = threading.Thread(target=run_api, daemon=True)
    thread.start()
    st.session_state["api_server_started"] = True


# ===========================================================
# 8. Streamlit UI – simple status + manual test
# ===========================================================
st.title("Instagram Metadata Service (Streamlit + FastAPI)")
st.write("This app runs a FastAPI endpoint at `http://localhost:8000/instagram-metadata`.")
st.write("Your Google Apps Script can POST to this URL to fetch Instagram metrics.")

st.subheader("Service status")
st.write(f"Main cookie file: `{COOKIE_FILE_MAIN}` "
         f"({'FOUND' if os.path.exists(COOKIE_FILE_MAIN) else 'NOT FOUND'})")
st.write(f"Alt cookie file: `{COOKIE_FILE_ALT}` "
         f"({'FOUND' if os.path.exists(COOKIE_FILE_ALT) else 'NOT FOUND'})")

st.subheader("Manual test (local only)")
test_url = st.text_input("Instagram Post URL:")

if st.button("Test fetch (direct Python call)"):
    if not test_url.strip():
        st.error("Please enter an Instagram URL.")
    else:
        try:
            # Directly call our helper (bypassing FastAPI) for quick testing.
            last_error_raw = None
            success = False
            meta = None

            for cookie_idx, cookie_file in enumerate(cookie_pool):
                try:
                    meta = get_instagram_metadata(test_url.strip(), cookie_file)
                    success = True
                    break
                except Exception as e:
                    last_error_raw = str(e)
                    if cookie_idx == 0 and len(cookie_pool) > 1 and should_retry_with_alt_cookie(last_error_raw):
                        continue
                    else:
                        break

            if not success:
                friendly = simplify_error_message(last_error_raw)
                st.error(f"Failed: {friendly}")
            else:
                st.success("Success!")
                st.json(
                    {
                        "plays": meta.get("plays"),
                        "comments": meta.get("comments"),
                        "likes": meta.get("likes"),
                        "pub_date": meta.get("pub_date"),
                        "pub_time": meta.get("pub_time"),
                        "insta_id": meta.get("insta_id"),
                    }
                )
        except Exception as e:
            st.error(f"Unexpected error: {e}")

