import json
import logging
import streamlit as st
from dotenv import load_dotenv
import os
import re
import time
import html

from auth import (
    clear_persistent_session,
    create_persistent_session,
    ensure_startup_user,
    get_user_by_session_token,
    login_user,
    signup_user,
)
import db
from db import execute, fetch_all, fetch_one, init_db
from utils import (
    fetch_location,
    now_str,
    queue_sos_call_delivery,
    queue_sos_email_delivery,
    setup_logging,
    validate_email,
)
from streamlit_js_eval import streamlit_js_eval


# Section: Session state
def _init_state():
    default_message = os.getenv(
        "SOS_DEFAULT_MESSAGE",
        "Emergency alert from OneTapSOS. I need immediate help. Please contact me now and inform emergency services.",
    )
    defaults = {
        "logged_in": False,
        "user": None,
        "restore_probe_done": False,
        "skip_auto_login_once": False,
        "active_view": "home",  # home | contacts | settings | history
        "show_add_contact": False,
        "edit_contact_id": None,
        "custom_message": default_message,
        "status": "SAFE",  # SAFE | SENDING ALERT | EMERGENCY ACTIVE
        "location_text": "Location Pending",
        "location_updated": "-",
        "map_link": "",
        "last_location_payload": None,
        "pending_sos_future": None,
        "pending_call_future": None,
        "queued_channels": [],
        "pending_sos_alert_id": None,
        "sos_notice": "",
        "sos_notice_kind": "",
        "show_fake_call": False,
        "live_tracking_enabled": False,
        "live_tracking_last_sent_at": 0.0,
        "safe_timer_deadline": 0.0,
        "safe_timer_triggered": False,
        "sos_trigger_requested": False,
        "last_sos_trigger_at": 0.0,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _request_sos_trigger():
    """Set a one-shot flag so SOS dispatch runs reliably on the next rerun."""
    if st.session_state.get("status") == "SENDING ALERT":
        st.session_state.sos_notice = "SOS is already being sent."
        st.session_state.sos_notice_kind = "info"
        return

    now_ts = time.time()
    cooldown_seconds = 2.0
    last_ts = float(st.session_state.get("last_sos_trigger_at") or 0.0)
    if (now_ts - last_ts) < cooldown_seconds:
        st.session_state.sos_notice = "SOS already triggered. Please wait a moment."
        st.session_state.sos_notice_kind = "info"
        return

    st.session_state.last_sos_trigger_at = now_ts
    st.session_state.sos_trigger_requested = True


# Section: Global app theme
def _apply_styles():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;700;800&display=swap');

        html, body, [class*="css"] {
            font-family: 'Outfit', sans-serif;
            background: linear-gradient(135deg, #020617 0%, #0f172a 100%) !important;
            color: #e5e7eb !important;
        }

        [data-testid="stAppViewContainer"] {
            background: linear-gradient(135deg, #020617 0%, #0f172a 100%) !important;
        }

        [data-testid="stSidebar"] {
            display: none !important;
        }

        [data-testid="stHeader"] {
            background: transparent;
        }

        .block-container {
            max-width: 860px;
            padding-top: 0.8rem;
            padding-bottom: 4.25rem;
        }

        .page-shell {
            position: relative;
            min-height: 100vh;
        }

        .page-shell::before {
            content: "";
            position: fixed;
            inset: 0;
            pointer-events: none;
            background:
                radial-gradient(circle at 50% 8%, rgba(239, 68, 68, 0.16), transparent 24%),
                radial-gradient(circle at 18% 18%, rgba(59, 130, 246, 0.10), transparent 20%),
                radial-gradient(circle at 82% 12%, rgba(34, 197, 94, 0.08), transparent 18%);
            opacity: 0.9;
        }

        .card {
            background: linear-gradient(180deg, rgba(17, 24, 39, 0.92), rgba(15, 23, 42, 0.92));
            border: 1px solid rgba(148, 163, 184, 0.18);
            border-radius: 18px;
            padding: 1rem;
            box-shadow:
                0 18px 34px rgba(2, 6, 23, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.03);
            backdrop-filter: blur(10px);
        }

        .header-row {
            display: grid;
            grid-template-columns: 44px 1fr 44px;
            align-items: center;
            gap: 0.8rem;
            margin-top: 0.3rem;
            margin-bottom: 0.35rem;
        }

        .header-title {
            text-align: center;
            color: #f8fafc;
            font-weight: 800;
            font-size: 1.4rem;
            letter-spacing: 0.3px;
        }

        .chip {
            width: 44px;
            height: 44px;
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            background: #111827;
            border: 1px solid rgba(148, 163, 184, 0.25);
        }

        .title {
            text-align: center;
            color: #f8fafc;
            font-weight: 800;
            font-size: 1.65rem;
            letter-spacing: 0.4px;
        }

        .hero-note {
            text-align: center;
            color: #94a3b8;
            font-size: 0.92rem;
            margin-top: 0.2rem;
            margin-bottom: 0.7rem;
        }

        .hero-strip {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.8rem;
            margin-bottom: 0.65rem;
        }

        .hero-pill {
            display: inline-flex;
            align-items: center;
            gap: 0.45rem;
            padding: 0.45rem 0.75rem;
            border-radius: 999px;
            background: rgba(15, 23, 42, 0.72);
            color: #cbd5e1;
            border: 1px solid rgba(148, 163, 184, 0.18);
            font-size: 0.8rem;
            font-weight: 700;
        }

        .hero-mini {
            color: #94a3b8;
            font-size: 0.8rem;
            letter-spacing: 0.35px;
            text-transform: uppercase;
        }

        .greet {
            color: #cbd5e1;
            margin: 0.2rem 0 0.7rem;
            font-size: 0.98rem;
        }

        .status-badge {
            display: inline-block;
            border-radius: 999px;
            padding: 0.35rem 0.85rem;
            font-size: 0.8rem;
            font-weight: 800;
            border: 1px solid transparent;
        }

        .safe {
            color: #22c55e;
            background: rgba(34, 197, 94, 0.14);
            border-color: rgba(34, 197, 94, 0.45);
            box-shadow: 0 0 12px rgba(34, 197, 94, 0.24);
        }

        .sending {
            color: #facc15;
            background: rgba(250, 204, 21, 0.14);
            border-color: rgba(250, 204, 21, 0.45);
            box-shadow: 0 0 12px rgba(250, 204, 21, 0.24);
        }

        .active {
            color: #ef4444;
            background: rgba(239, 68, 68, 0.16);
            border-color: rgba(239, 68, 68, 0.58);
            box-shadow: 0 0 16px rgba(239, 68, 68, 0.34);
        }

        .loc-title {
            color: #f8fafc;
            font-weight: 700;
            font-size: 1rem;
            margin-bottom: 0.15rem;
        }

        .loc-meta {
            color: #94a3b8;
            font-size: 0.84rem;
            margin-bottom: 0.3rem;
        }

        .map-link {
            color: #93c5fd;
            text-decoration: none;
            font-weight: 700;
        }

        .map-link:hover {
            text-decoration: underline;
        }

        .quick-title {
            color: #94a3b8;
            text-transform: uppercase;
            letter-spacing: 0.45px;
            font-size: 0.75rem;
            margin-bottom: 0.5rem;
        }

        .quick-card {
            display: flex;
            gap: 0.75rem;
            align-items: center;
            background: #111827;
            border: 1px solid rgba(148, 163, 184, 0.22);
            border-radius: 14px;
            padding: 0.7rem 0.85rem;
            margin-bottom: 0.5rem;
        }

        .quick-row {
            display: flex;
            gap: 0.75rem;
            overflow-x: auto;
            padding-bottom: 0.2rem;
            margin-bottom: 0.65rem;
        }

        .quick-pill {
            min-width: 52px;
            text-align: center;
        }

        .quick-pill-name {
            font-size: 0.78rem;
            color: #e2e8f0;
            margin-top: 0.25rem;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .avatar {
            width: 42px;
            height: 42px;
            border-radius: 50%;
            background: radial-gradient(circle at 30% 30%, #374151, #1f2937);
            border: 1px solid rgba(148, 163, 184, 0.3);
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .timeline {
            border-left: 2px solid rgba(239, 68, 68, 0.4);
            margin-left: 0.4rem;
            padding-left: 0.95rem;
            margin-bottom: 0.75rem;
        }

        .timeline-time {
            color: #94a3b8;
            font-size: 0.78rem;
        }

        .timeline-status {
            color: #f8fafc;
            font-weight: 700;
            margin-top: 0.2rem;
            font-size: 0.9rem;
        }

        .top-nav-title {
            color: #94a3b8;
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-top: 0.35rem;
            margin-bottom: 0.35rem;
        }

        .stTextInput input,
        .stTextArea textarea {
            background: #0b1220 !important;
            color: #e5e7eb !important;
            border: 1px solid rgba(148, 163, 184, 0.28) !important;
            border-radius: 12px !important;
        }

        .stButton button {
            border-radius: 14px !important;
            min-height: 40px !important;
            font-weight: 800 !important;
            border: 1px solid rgba(148, 163, 184, 0.24) !important;
            background: linear-gradient(180deg, rgba(17, 24, 39, 0.98), rgba(15, 23, 42, 0.95)) !important;
            color: #f8fafc !important;
            transition: transform 0.12s ease, box-shadow 0.12s ease !important;
        }

        .stButton button:hover {
            transform: translateY(-1px);
            box-shadow: 0 8px 16px rgba(2, 6, 23, 0.45);
        }

        .sos-main {
            text-align: center;
            margin: 1.45rem 0 1rem;
        }

        .st-key-sos_trigger button,
        .sos-main button[aria-label="SOS"] {
            width: 300px !important;
            height: 300px !important;
            border-radius: 50% !important;
            background: radial-gradient(circle at 30% 30%, #ff5a5a, #ef4444 62%, #b91c1c) !important;
            color: #ffffff !important;
            font-size: 1.9rem !important;
            font-weight: 800 !important;
            border: none !important;
            box-shadow: 0 0 38px rgba(239, 68, 68, 0.74) !important;
            cursor: pointer;
            margin: 0 auto !important;
            display: block !important;
        }

        .location-panel {
            margin-top: 0.95rem;
            padding: 0.95rem 1rem;
            border-radius: 18px;
            background: linear-gradient(180deg, rgba(17, 24, 39, 0.86), rgba(15, 23, 42, 0.86));
            border: 1px solid rgba(148, 163, 184, 0.18);
            box-shadow: 0 16px 34px rgba(2, 6, 23, 0.35);
        }

        .location-status-row {
            display: flex;
            justify-content: space-between;
            gap: 1rem;
            align-items: flex-start;
        }

        .location-status-label {
            color: #f8fafc;
            font-size: 1rem;
            font-weight: 800;
            margin-bottom: 0.15rem;
        }

        .location-status-sub {
            color: #94a3b8;
            font-size: 0.83rem;
        }

        .location-badge {
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            padding: 0.35rem 0.7rem;
            border-radius: 999px;
            background: rgba(239, 68, 68, 0.12);
            border: 1px solid rgba(239, 68, 68, 0.24);
            color: #fecaca;
            font-size: 0.76rem;
            font-weight: 800;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# Section: Authentication UI
COOKIE_NAME = "sos_session_token"
PHONE_PATTERN = re.compile(r"^\+?[1-9]\d{7,14}$")


def _cookie_set_js(token, max_age_seconds):
    return f"""
    (function(){{
        const token = {json.dumps(token)};
        const age = {int(max_age_seconds)};
        document.cookie = '{COOKIE_NAME}=' + encodeURIComponent(token) + '; Path=/; Max-Age=' + age + '; SameSite=Lax';
        localStorage.setItem('{COOKIE_NAME}', token);
        return 'ok';
    }})();
    """


def _cookie_read_js():
    return f"""
    (function(){{
        const cookieMatch = document.cookie.match(new RegExp('(?:^|; )' + {json.dumps(COOKIE_NAME)} + '=([^;]*)'));
        const cookieToken = cookieMatch ? decodeURIComponent(cookieMatch[1]) : '';
        const storageToken = localStorage.getItem({json.dumps(COOKIE_NAME)}) || '';
        const token = cookieToken || storageToken;
        if (token && !cookieToken) {{
            document.cookie = '{COOKIE_NAME}=' + encodeURIComponent(token) + '; Path=/; Max-Age=' + (60 * 60 * 24 * 30) + '; SameSite=Lax';
        }}
        return token;
    }})();
    """


def _cookie_clear_js():
    return f"""
    (function(){{
        document.cookie = '{COOKIE_NAME}=; Path=/; Max-Age=0; SameSite=Lax';
        localStorage.removeItem('{COOKIE_NAME}');
        return 'ok';
    }})();
    """


def _clear_browser_refresh_timer():
    """Clear stale client-side refresh timer from older app sessions."""
    try:
        streamlit_js_eval(
            js_expressions="""
            (function(){
                if (window.__onetapsos_refresh_timer) {
                    clearTimeout(window.__onetapsos_refresh_timer);
                    window.__onetapsos_refresh_timer = null;
                }
                return 'ok';
            })();
            """,
            key="clear_refresh_timer_once",
            want_output=False,
        )
    except Exception:
        pass


def set_login_cookie(user):
    token = create_persistent_session(user["id"])
    st.session_state.logged_in = True
    st.session_state.user = user
    try:
        streamlit_js_eval(
            js_expressions=_cookie_set_js(token, 60 * 60 * 24 * 30),
            key="set_login_token",
            want_output=False,
        )
    except Exception:
        logging.debug("Could not persist login token to localStorage.")


def get_login_cookie():
    try:
        raw_value = streamlit_js_eval(
            js_expressions=_cookie_read_js(),
            key="get_login_cookie",
            want_output=True
        )
    except Exception:
        return None

    token = (raw_value or "").strip()
    if not token:
        return None
    return {"token": token}


def clear_login_cookie():
    token_data = get_login_cookie() or {}
    token = token_data.get("token")
    if token:
        clear_persistent_session(token=token)
    elif st.session_state.get("user") and st.session_state["user"].get("id"):
        clear_persistent_session(user_id=st.session_state["user"]["id"])

    try:
        streamlit_js_eval(
            js_expressions=_cookie_clear_js(),
            key="clear_login_token",
            want_output=False,
        )
    except Exception:
        logging.debug("Could not clear login token from localStorage.")


def _auto_login_emergency_account():
    """Emergency fallback: auto-login with configured default account."""
    if os.getenv("AUTO_LOGIN_ON_START", "1").strip() != "1":
        return False
    if st.session_state.get("skip_auto_login_once"):
        return False

    identity = os.getenv("DEFAULT_LOGIN_EMAIL", "keerthanaveldanda05@gmail.com")
    password = os.getenv("DEFAULT_LOGIN_PASSWORD", "123456789")
    profile = login_user(identity, password)
    if not profile:
        return False

    st.session_state.logged_in = True
    st.session_state.user = profile
    st.session_state.restore_probe_done = True
    set_login_cookie(profile)
    logging.info("Emergency auto-login used.")
    return True


def _load_user_profile_by_id(user_id):
    row = fetch_one(
        "SELECT id, username, email, phone FROM users WHERE id=?",
        (user_id,),
    )
    return dict(row) if row else None


def _render_auth():
    st.markdown("<div style='height:5vh;'></div>", unsafe_allow_html=True)
    _, mid, _ = st.columns([1, 2.05, 1])
    with mid:
        st.markdown(
            """
            <div class='card' style='text-align:center;'>
                <div style='font-size:2.3rem;'>🚨</div>
                <div style='font-size:2rem; color:#f8fafc; font-weight:800;'>OneTapSOS</div>
                <div style='font-size:0.9rem; color:#94a3b8;'>One tap to safety. Instant emergency response.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        mode = st.radio(
            "Authentication",
            ["Login", "Signup"],
            horizontal=True,
            label_visibility="collapsed",
            key="auth_mode",
        )

        if mode == "Login":
            with st.form("login_form"):
                default_identity = os.getenv("DEFAULT_LOGIN_EMAIL", "keerthanaveldanda05@gmail.com")
                default_password = os.getenv("DEFAULT_LOGIN_PASSWORD", "123456789")
                identity = st.text_input(
                    "Username or Email",
                    value=default_identity,
                    placeholder="Enter username or email",
                )
                password = st.text_input(
                    "Password",
                    type="password",
                    value=default_password,
                    placeholder="Enter password",
                )
                submitted = st.form_submit_button("Login", use_container_width=True, type="primary")
            if submitted:
                profile = login_user(identity, password)
                if not profile:
                    st.error("Invalid credentials")
                else:
                    st.session_state.logged_in = True
                    st.session_state.user = profile
                    st.session_state.restore_probe_done = False
                    set_login_cookie(profile)
                    st.rerun()
        else:
            with st.form("signup_form"):
                username = st.text_input("Username", placeholder="Choose a username")
                email = st.text_input("Email", placeholder="Enter your email")
                phone = st.text_input("Phone", placeholder="+91XXXXXXXXXX")
                password = st.text_input("Password", type="password", placeholder="Create password")
                submitted = st.form_submit_button("Create Account", use_container_width=True, type="primary")
            if submitted:
                ok, message = signup_user(username, email, phone, password)
                if ok:
                    st.success(message)
                else:
                    st.error(message)


# Section: Data helpers
def _get_contacts(user_id):
    rows = fetch_all(
        "SELECT id, name, email, phone FROM contacts WHERE user_id=? ORDER BY id DESC",
        (user_id,),
    )
    return [dict(r) for r in rows]


def _normalize_contact(name, email, phone):
    return name.strip(), email.strip().lower(), _normalize_phone_for_storage(phone)


def _normalize_phone_for_storage(phone):
    raw = str(phone or "").strip()
    if not raw:
        return ""

    if raw.startswith("+"):
        digits = re.sub(r"\D", "", raw[1:])
        candidate = f"+{digits}" if digits else ""
    else:
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 10:
            cc = os.getenv("DEFAULT_COUNTRY_CODE", "+91")
            cc_digits = re.sub(r"\D", "", cc)
            candidate = f"+{cc_digits}{digits}" if cc_digits else f"+{digits}"
        elif 11 <= len(digits) <= 15:
            candidate = f"+{digits}"
        else:
            candidate = raw

    return candidate if PHONE_PATTERN.match(candidate) else ""


def _is_duplicate_contact(user_id, email, phone, ignore_contact_id=None):
    params = [user_id, email]
    query = "SELECT id FROM contacts WHERE user_id=? AND lower(email)=lower(?)"
    if ignore_contact_id is not None:
        query += " AND id<>?"
        params.append(ignore_contact_id)
    email_dup = fetch_one(query, tuple(params))
    if email_dup:
        return True

    if not phone:
        return False

    params = [user_id, phone]
    query = "SELECT id FROM contacts WHERE user_id=? AND phone=?"
    if ignore_contact_id is not None:
        query += " AND id<>?"
        params.append(ignore_contact_id)
    phone_dup = fetch_one(query, tuple(params))
    return bool(phone_dup)


def _validate_contact(name, email, phone):
    if not name:
        return False, "Contact name is required."
    if not validate_email(email):
        return False, "Enter a valid contact email."
    if phone and not PHONE_PATTERN.match(phone):
        return False, "Enter a valid contact phone. Example: +91XXXXXXXXXX or 10-digit local number."
    return True, ""


def _normalize_existing_contact_phones(user_id):
    rows = fetch_all("SELECT id, phone FROM contacts WHERE user_id=?", (user_id,))
    for row in rows:
        phone = str(row["phone"] or "").strip()
        normalized = _normalize_phone_for_storage(phone)
        if normalized and normalized != phone:
            execute("UPDATE contacts SET phone=? WHERE id=? AND user_id=?", (normalized, row["id"], user_id))


def _save_contact(user_id, name, email, phone):
    name, email, phone = _normalize_contact(name, email, phone)
    ok, msg = _validate_contact(name, email, phone)
    if not ok:
        return False, msg
    if _is_duplicate_contact(user_id, email, phone):
        return False, "Contact with same email or phone already exists."

    execute(
        """
        INSERT INTO contacts (user_id, name, email, phone, relationship, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, name, email, phone, "", now_str()),
    )
    return True, "Contact saved"


def _update_contact(user_id, contact_id, name, email, phone):
    name, email, phone = _normalize_contact(name, email, phone)
    ok, msg = _validate_contact(name, email, phone)
    if not ok:
        return False, msg
    if _is_duplicate_contact(user_id, email, phone, ignore_contact_id=contact_id):
        return False, "Another contact with same email or phone already exists."

    execute(
        """
        UPDATE contacts
        SET name=?, email=?, phone=?
        WHERE id=? AND user_id=?
        """,
        (name, email, phone, contact_id, user_id),
    )
    return True, "Contact updated"


def _delete_contact(user_id, contact_id):
    execute("DELETE FROM contacts WHERE id=? AND user_id=?", (contact_id, user_id))


# Section: Header and status
def _render_header(username):
    st.markdown("<div class='header-title'>OneTapSOS</div>", unsafe_allow_html=True)
    st.markdown(f"<div class='greet'>Hi, {username} 👋</div>", unsafe_allow_html=True)


def _render_status_badge():
    status = st.session_state.status
    if status == "SAFE":
        cls = "safe"
    elif status == "SENDING ALERT":
        cls = "sending"
    else:
        cls = "active"
    st.markdown(f"<span class='status-badge {cls}'>{status}</span>", unsafe_allow_html=True)


def _smtp_status():
    required = ["SMTP_USER", "SMTP_PASSWORD"]
    missing = [key for key in required if not os.getenv(key)]
    return len(missing) == 0, missing


def _render_smtp_banner():
    configured, missing = _smtp_status()
    if configured:
        st.success("Email alerts are configured.")
    else:
        st.warning("Email alerts are not fully configured. Missing: " + ", ".join(missing))


def _create_alert_record(user, payload, custom_message, email_status, recipients_count):
    return execute(
        """
        INSERT INTO alerts (user_id, latitude, longitude, map_link, custom_message, email_status, recipients_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user["id"],
            payload["lat"],
            payload["lon"],
            payload["map_link"],
            custom_message,
            email_status,
            recipients_count,
            now_str(),
        ),
    )


def _render_sos_notice():
    kind = st.session_state.get("sos_notice_kind", "")
    message = st.session_state.get("sos_notice", "")
    if not message:
        return

    if kind == "success":
        st.success(message)
    elif kind == "warning":
        st.warning(message)
    else:
        st.info(message)

    st.session_state.sos_notice = ""
    st.session_state.sos_notice_kind = ""


def _location_label(payload):
    source = payload.get("source", "browser") if isinstance(payload, dict) else "browser"
    accuracy = payload.get("accuracy") if isinstance(payload, dict) else None

    if source == "ip":
        return "Location Approximate (IP)"
    if source == "default":
        return "Location Approximate (Default)"

    try:
        if accuracy is not None and float(accuracy) > 300:
            return "Location Approximate (Low GPS Accuracy)"
    except Exception:
        pass
    return "Location Active"


def _sync_pending_sos_job():
    email_future = st.session_state.get("pending_sos_future")
    call_future = st.session_state.get("pending_call_future")
    if not email_future and not call_future:
        return

    if (email_future and not email_future.done()) or (call_future and not call_future.done()):
        st.session_state.status = "SENDING ALERT"
        st.session_state.sos_notice = "SOS alert is being sent by email and call in the background."
        st.session_state.sos_notice_kind = "info"
        return

    email_result = {"success": True, "partial": False, "sent_count": 0, "failed_recipients": []}
    call_result = {"success": True, "called_count": 0, "failed_recipients": []}

    try:
        if email_future:
            email_result = email_future.result()
        if call_future:
            call_result = call_future.result()
    except Exception as exc:
        if st.session_state.get("pending_sos_alert_id"):
            execute(
                "UPDATE alerts SET email_status=? WHERE id=?",
                ("failed", st.session_state.pending_sos_alert_id),
            )
        st.session_state.sos_notice = f"SOS delivery failed: {exc}"
        st.session_state.sos_notice_kind = "warning"
        st.session_state.status = "SAFE"
        st.session_state.pending_sos_future = None
        st.session_state.pending_call_future = None
        st.session_state.pending_sos_alert_id = None
        return

    st.session_state.pending_sos_future = None
    st.session_state.pending_call_future = None
    st.session_state.pending_sos_alert_id = None
    st.session_state.status = "SAFE"

    queued_channels = st.session_state.get("queued_channels", [])
    email_ok = ("email" not in queued_channels) or email_result.get("success") or email_result.get("partial")
    call_ok = ("call" not in queued_channels) or call_result.get("success")

    if email_ok and call_ok:
        parts = []
        if "email" in queued_channels:
            parts.append(f"email to {email_result.get('sent_count', 0)}")
        if "call" in queued_channels:
            parts.append(f"calls to {call_result.get('called_count', 0)}")
        if email_result.get("partial"):
            failures = []
            for item in email_result.get("failed_recipients", []):
                if isinstance(item, str):
                    failures.append(item)
                else:
                    failures.append(f"{item.get('email', 'unknown')}: {'; '.join(item.get('errors', []))}")
            failure_text = "; ".join(failures) if failures else "some email recipients failed"
            st.session_state.sos_notice = "SOS delivered with partial email success: " + failure_text
            st.session_state.sos_notice_kind = "warning"
        else:
            st.session_state.sos_notice = "SOS delivered: " + ", ".join(parts) + " contact(s)."
            st.session_state.sos_notice_kind = "success"
    else:
        failures = []
        for item in email_result.get("failed_recipients", []):
            if isinstance(item, str):
                failures.append(item)
            else:
                failures.append(f"{item.get('email', 'unknown')}: {'; '.join(item.get('errors', []))}")
        for item in call_result.get("failed_recipients", []):
            if isinstance(item, str):
                failures.append(item)
            else:
                failures.append(f"{item.get('phone', 'unknown')}: {item.get('error', 'call failed')}")
        if not failures:
            failures = ["SOS delivery failed."]
        st.session_state.sos_notice = " ".join(failures)
        st.session_state.sos_notice_kind = "warning"

    st.session_state.queued_channels = []


# Section: SOS action
def _send_sos(user, contacts, silent=False, message_override=None, force_live_location=False):
    logging.info("SOS dispatch requested for user=%s with %s contacts", user.get("id"), len(contacts or []))
    st.session_state.status = "SENDING ALERT"
    default_message = os.getenv(
        "SOS_DEFAULT_MESSAGE",
        "Emergency alert from OneTapSOS. I need immediate help. Please contact me now and inform emergency services.",
    )
    custom_message = (message_override or st.session_state.custom_message).strip() or default_message

    fallback_lat = float(os.getenv("DEFAULT_LAT", "17.33594"))
    fallback_lon = float(os.getenv("DEFAULT_LON", "78.53031"))
    fallback_payload = {
        "lat": fallback_lat,
        "lon": fallback_lon,
        "map_link": f"https://maps.google.com/?q={fallback_lat},{fallback_lon}",
    }

    payload = None
    if force_live_location:
        result = fetch_location(skip_browser_request=False)
        if result:
            ok_loc, live_payload = result
            if ok_loc and isinstance(live_payload, dict):
                payload = dict(live_payload)
                st.session_state.location_text = _location_label(payload)
                st.session_state.last_location_payload = payload

    if payload is None:
        cached_payload = st.session_state.get("last_location_payload")
        if (
            isinstance(cached_payload, dict)
            and cached_payload.get("lat") is not None
            and cached_payload.get("lon") is not None
            and cached_payload.get("map_link")
        ):
            payload = dict(cached_payload)
            st.session_state.location_text = _location_label(payload)
        else:
            payload = {
                **fallback_payload,
                "source": "default",
                "accuracy": None,
            }
            st.session_state.location_text = "Location Active (Fallback)"
            if not silent:
                st.info("Using last known/default location for instant SOS dispatch.")

    st.session_state.location_updated = "just now"
    st.session_state.map_link = payload["map_link"]
    st.session_state.last_location_payload = payload

    payload = {
        **payload,
        "message": custom_message,
        "timestamp": now_str(),
    }

    valid_email_contacts = [contact for contact in contacts if validate_email(contact.get("email", ""))]
    valid_phone_contacts = [
        contact
        for contact in contacts
        if PHONE_PATTERN.match(str(contact.get("phone", "")).strip())
    ]

    smtp_configured, smtp_missing = _smtp_status()
    twilio_configured = bool(
        os.getenv("TWILIO_ACCOUNT_SID", "").strip()
        and os.getenv("TWILIO_AUTH_TOKEN", "").strip()
        and (os.getenv("TWILIO_FROM_NUMBER", "").strip() or os.getenv("TWILIO_PHONE_NUMBER", "").strip())
    )

    can_email = smtp_configured and bool(valid_email_contacts)
    can_call = twilio_configured and bool(valid_phone_contacts)

    if not can_email and not can_call:
        _create_alert_record(user, payload, custom_message, "failed", 0)
        st.session_state.status = "SAFE"
        if not silent:
            reasons = []
            if not valid_email_contacts and not valid_phone_contacts:
                reasons.append("No valid contact email/phone configured")
            if not smtp_configured:
                reasons.append("Email config missing: " + ", ".join(smtp_missing))
            if not twilio_configured:
                reasons.append("Twilio config missing for calls")
            st.session_state.sos_notice = " | ".join(reasons) if reasons else "No valid alert channel available"
            st.session_state.sos_notice_kind = "warning"
        return

    recipient_count = max(len(valid_email_contacts), len(valid_phone_contacts))
    alert_id = _create_alert_record(user, payload, custom_message, "queued", recipient_count)
    payload["alert_id"] = alert_id

    # Create tracking session for live location sharing
    try:
        tracking_token = db.create_tracking_session(alert_id, user, payload)
        payload["tracking_token"] = tracking_token
        
        # Build tracking URL based on environment
        app_url = os.getenv("STREAMLIT_APP_URL", "").strip()
        if not app_url:
            # Fallback to localhost if not in environment
            app_url = "http://localhost:8501"
        
        tracking_url = f"{app_url}?token={tracking_token}"
        payload["tracking_url"] = tracking_url
        logging.info("Created tracking session with token=%s for alert_id=%s", tracking_token, alert_id)
    except Exception as e:
        logging.warning("Failed to create tracking session: %s", str(e))
        payload["tracking_token"] = None
        payload["tracking_url"] = None

    st.session_state.pending_sos_future = None
    st.session_state.pending_call_future = None
    st.session_state.queued_channels = []

    if can_email:
        logging.info("Queueing SOS email from button flow for %s recipients", len(valid_email_contacts))
        st.session_state.pending_sos_future = queue_sos_email_delivery(user, valid_email_contacts, payload)
        st.session_state.queued_channels.append("email")
    if can_call:
        logging.info("Queueing SOS call from button flow for %s recipients", len(valid_phone_contacts))
        st.session_state.pending_call_future = queue_sos_call_delivery(user, valid_phone_contacts, payload)
        st.session_state.queued_channels.append("call")

    st.session_state.pending_sos_alert_id = alert_id
    st.session_state.status = "SENDING ALERT"
    if not silent:
        channel_text = "+".join(st.session_state.queued_channels)
        call_delay = os.getenv("CALL_DELAY_SECONDS", "0")
        try:
            delay_value = max(0, int(call_delay))
        except Exception:
            delay_value = 0
        delay_text = "Calls start immediately." if delay_value == 0 else f"Calls start after {delay_value} seconds."
        st.session_state.sos_notice = (
            f"SOS alert queued via {channel_text} for {recipient_count} contact(s). "
            f"{delay_text}"
        )
        st.session_state.sos_notice_kind = "info"

    return {
        "alert_id": alert_id,
        "tracking_token": payload.get("tracking_token"),
        "location": {
            "lat": payload.get("lat"),
            "lon": payload.get("lon"),
            "accuracy": payload.get("accuracy"),
            "source": payload.get("source"),
            "timestamp": payload.get("timestamp"),
        },
    }


def _enable_mode_refresh(interval_seconds):
    if os.getenv("SOS_AUTO_REFRESH", "0") != "1":
        return

    try:
        streamlit_js_eval(
            js_expressions=f"""
            (function(){{
                if (window.__onetapsos_refresh_timer) {{
                    clearTimeout(window.__onetapsos_refresh_timer);
                }}
                window.__onetapsos_refresh_timer = setTimeout(function() {{
                    window.location.reload();
                }}, {int(interval_seconds * 1000)});
                return 'ok';
            }})();
            """,
            key=f"mode_refresh_{int(time.time() * 1000)}",
            want_output=False,
        )
    except Exception:
        pass


def _process_live_tracking(user, contacts):
    if not st.session_state.live_tracking_enabled:
        return

    _enable_mode_refresh(5)
    now_ts = time.time()
    if (
        (st.session_state.pending_sos_future and not st.session_state.pending_sos_future.done())
        or (st.session_state.pending_call_future and not st.session_state.pending_call_future.done())
    ):
        return

    if (now_ts - st.session_state.live_tracking_last_sent_at) >= 30:
        result = _send_sos(
            user,
            contacts,
            silent=True,
            message_override="Live tracking update: location heartbeat from safety app.",
            force_live_location=True,
        )
        
        # Update tracking session with fresh location
        if result and isinstance(result, dict):
            tracking_token = result.get("tracking_token")
            location_payload = result.get("location")
            if tracking_token and location_payload:
                try:
                    db.update_tracking_location(
                        tracking_token,
                        location_payload.get("lat"),
                        location_payload.get("lon"),
                        location_payload.get("accuracy"),
                        location_payload.get("source"),
                        location_payload.get("timestamp")
                    )
                    logging.info("Updated tracking session %s with fresh location", tracking_token)
                except Exception as e:
                    logging.warning("Failed to update tracking location: %s", str(e))
        
        st.session_state.live_tracking_last_sent_at = now_ts


def _process_safe_timer(user, contacts):
    deadline = float(st.session_state.safe_timer_deadline or 0.0)
    if deadline <= 0:
        return

    _enable_mode_refresh(2)
    if time.time() < deadline:
        return
    if st.session_state.safe_timer_triggered:
        return

    st.session_state.safe_timer_triggered = True
    st.session_state.safe_timer_deadline = 0.0
    _send_sos(
        user,
        contacts,
        silent=True,
        message_override="Safe timer expired. Auto SOS triggered.",
    )
    st.session_state.sos_notice = "Safe timer expired. SOS sent automatically."
    st.session_state.sos_notice_kind = "warning"


def _render_modes_panel(user, contacts):
    st.markdown("<div class='card'>", unsafe_allow_html=True)
    st.markdown("<div class='quick-title'>Emergency Modes</div>", unsafe_allow_html=True)

    if os.getenv("SOS_AUTO_REFRESH", "0") != "1" and (
        st.session_state.live_tracking_enabled or st.session_state.safe_timer_deadline
    ):
        st.info("Auto-refresh is disabled for UI stability. Use Refresh below while timer/tracking is active.")
        if st.button("Refresh Mode Status", use_container_width=True, key="manual_mode_refresh_btn"):
            st.rerun()

    m1, m2 = st.columns(2)
    with m1:
        if st.button("Silent SOS", use_container_width=True, key="silent_sos_btn"):
            _send_sos(
                user,
                contacts,
                silent=True,
                message_override="Silent SOS triggered. Immediate assistance required.",
            )
            st.session_state.sos_notice = "Silent SOS sent."
            st.session_state.sos_notice_kind = "success"
            st.rerun()
    with m2:
        if st.button("Fake Call", use_container_width=True, key="fake_call_btn"):
            st.session_state.show_fake_call = True

    t1, t2 = st.columns(2)
    with t1:
        live_label = "Stop Live Tracking" if st.session_state.live_tracking_enabled else "Start Live Tracking"
        if st.button(live_label, use_container_width=True, key="live_tracking_btn"):
            st.session_state.live_tracking_enabled = not st.session_state.live_tracking_enabled
            if st.session_state.live_tracking_enabled:
                st.session_state.live_tracking_last_sent_at = 0.0
                st.session_state.sos_notice = "Live tracking enabled. Sends location every 30 seconds."
            else:
                st.session_state.sos_notice = "Live tracking disabled."
            st.session_state.sos_notice_kind = "info"
            st.rerun()

    with t2:
        minutes = st.number_input("Safe Timer (minutes)", min_value=1, max_value=120, value=5, step=1, key="safe_timer_minutes")
        if st.button("Start Safe Timer", use_container_width=True, key="safe_timer_start"):
            st.session_state.safe_timer_deadline = time.time() + (int(minutes) * 60)
            st.session_state.safe_timer_triggered = False
            st.session_state.sos_notice = f"Safe timer started for {int(minutes)} minute(s)."
            st.session_state.sos_notice_kind = "info"
            st.rerun()

    if st.session_state.safe_timer_deadline:
        remaining = max(0, int(st.session_state.safe_timer_deadline - time.time()))
        mm, ss = divmod(remaining, 60)
        st.caption(f"Safe timer remaining: {mm:02d}:{ss:02d}")
        if st.button("Cancel Safe Timer", use_container_width=True, key="safe_timer_cancel"):
            st.session_state.safe_timer_deadline = 0.0
            st.session_state.safe_timer_triggered = False
            st.session_state.sos_notice = "Safe timer canceled."
            st.session_state.sos_notice_kind = "info"
            st.rerun()

    if st.session_state.show_fake_call:
        st.markdown(
            """
            <div class='quick-card' style='border-color:rgba(34,197,94,0.55);'>
                <div class='avatar'>📞</div>
                <div>
                    <div style='font-weight:800;color:#f8fafc;'>Incoming Call</div>
                    <div style='font-size:0.82rem;color:#94a3b8;'>Safety Hotline</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        f1, f2 = st.columns(2)
        with f1:
            st.button("Accept", use_container_width=True, key="fake_call_accept")
        with f2:
            if st.button("Dismiss", use_container_width=True, key="fake_call_dismiss"):
                st.session_state.show_fake_call = False
                st.rerun()

    st.markdown("</div>", unsafe_allow_html=True)


# Section: Home page
def _home_page(user):
    contacts = _get_contacts(user["id"])

    if st.session_state.get("sos_trigger_requested"):
        st.session_state.sos_trigger_requested = False
        _send_sos(user, contacts)
        st.rerun()

    hero_left, hero_mid, hero_right = st.columns([1, 2, 1])
    with hero_mid:
        st.markdown("<div class='title'>Safety</div>", unsafe_allow_html=True)
        st.markdown("<div class='hero-note'>One tap sends email and voice call alerts.</div>", unsafe_allow_html=True)
    with hero_right:
        if st.button("Add Contact", use_container_width=True, key="home_add_contact_btn"):
            st.session_state.active_view = "contacts"
            st.rerun()

    # Probe location once on home load so status doesn't remain stale.
    if st.session_state.location_text in {"Location Pending", "Location Inactive"}:
        ok_loc, payload = fetch_location()
        if ok_loc:
            st.session_state.location_text = _location_label(payload)
            st.session_state.location_updated = "just now"
            st.session_state.map_link = payload.get("map_link", "")
            st.session_state.last_location_payload = payload
        else:
            st.session_state.location_text = "Location Unavailable"

    # Big centered SOS button
    st.markdown("<div style='height:0.6rem;'></div>", unsafe_allow_html=True)
    st.markdown("<div class='sos-main'>", unsafe_allow_html=True)
    c1, c2, c3 = st.columns([1, 1.8, 1])
    with c2:
        st.button(
            "SOS",
            key="sos_trigger",
            on_click=_request_sos_trigger,
            disabled=st.session_state.status == "SENDING ALERT",
        )
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div class='location-panel'>", unsafe_allow_html=True)
    st.markdown(
        f"""
        <div class='location-status-row'>
            <div>
                <div class='location-status-label'>{st.session_state.location_text}</div>
                <div class='location-status-sub'>Updated {st.session_state.location_updated}</div>
            </div>
            <div class='location-badge'>Live safety location</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.session_state.map_link:
        st.markdown(
            f"<div style='margin-top:0.5rem;'><a class='map-link' href='{st.session_state.map_link}' target='_blank'>View on Map</a></div>",
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)

# Section: Contacts page
def _contacts_page(user):
    st.subheader("➕")

    c1, c2 = st.columns([3, 1])
    with c2:
        if st.button("➕ Add Contact", use_container_width=True, type="primary"):
            st.session_state.show_add_contact = not st.session_state.show_add_contact

    if st.session_state.show_add_contact:
        with st.form("contact_form"):
            name = st.text_input("Name")
            email = st.text_input("Email")
            phone = st.text_input("Phone", placeholder="+911234567890")
            submitted = st.form_submit_button("Save Contact", use_container_width=True, type="primary")
        if submitted:
            ok, msg = _save_contact(user["id"], name, email, phone)
            if ok:
                st.success(msg)
                st.session_state.show_add_contact = False
                st.rerun()
            else:
                st.error(msg)

    contacts = _get_contacts(user["id"])
    if not contacts:
        st.info("No contacts found")
        return

    for contact in contacts:
        st.markdown(
            f"""
            <div class='quick-card'>
                <div class='avatar'>👤</div>
                <div>
                    <div style='font-weight:700; color:#f8fafc;'>{contact['name']}</div>
                    <div style='font-size:0.82rem; color:#94a3b8;'>{contact['email']}</div>
                    <div style='font-size:0.78rem; color:#64748b;'>{contact.get('phone', '') or 'No phone'}</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        e1, e2, _ = st.columns([1, 1, 2])
        with e1:
            if st.button("Edit", key=f"edit_contact_{contact['id']}"):
                st.session_state.edit_contact_id = contact["id"]
                st.rerun()
        with e2:
            if st.button("Delete", key=f"delete_contact_{contact['id']}"):
                _delete_contact(user["id"], contact["id"])
                if st.session_state.edit_contact_id == contact["id"]:
                    st.session_state.edit_contact_id = None
                st.success("Contact deleted")
                st.rerun()

        if st.session_state.edit_contact_id == contact["id"]:
            with st.form(f"edit_contact_form_{contact['id']}"):
                edit_name = st.text_input("Edit Name", value=contact["name"])
                edit_email = st.text_input("Edit Email", value=contact["email"])
                edit_phone = st.text_input("Edit Phone", value=contact.get("phone", ""))
                c1, c2 = st.columns(2)
                with c1:
                    save_edit = st.form_submit_button("Save Changes", use_container_width=True)
                with c2:
                    cancel_edit = st.form_submit_button("Cancel", use_container_width=True)

            if save_edit:
                ok, msg = _update_contact(user["id"], contact["id"], edit_name, edit_email, edit_phone)
                if ok:
                    st.session_state.edit_contact_id = None
                    st.success(msg)
                    st.rerun()
                st.error(msg)
            if cancel_edit:
                st.session_state.edit_contact_id = None
                st.rerun()


# Section: History page
def _history_page(user):
    st.subheader("📜 History")

    rows = fetch_all(
        """
        SELECT created_at, email_status, map_link
        FROM alerts
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 20
        """,
        (user["id"],),
    )

    if not rows:
        st.info("No previous SOS alerts")
        return

    for row in rows:
        if row["email_status"] == "success":
            icon = "✅"
            text = "Alert sent"
        elif row["email_status"] == "partial":
            icon = "⚠️"
            text = "Alert partially sent"
        elif row["email_status"] == "queued":
            icon = "⏳"
            text = "Alert queued"
        else:
            icon = "❌"
            text = "Alert failed"
        st.markdown(
            f"""
            <div class='timeline'>
                <div class='timeline-time'>{row['created_at']}</div>
                <div class='timeline-status'>{icon} {text}</div>
                <div style='font-size:0.8rem; margin-top:0.25rem;'>
                    <a class='map-link' href='{row['map_link']}' target='_blank'>Location link</a>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


# Section: Settings page
def _settings_page():
    st.subheader("⚙️")
    st.markdown("<div class='card'>", unsafe_allow_html=True)

    if st.button("📜 History", use_container_width=True, key="settings_history_btn"):
        st.session_state.active_view = "history"
        st.rerun()

    if st.button("Logout", use_container_width=True, key="settings_logout_btn"):
        clear_login_cookie()
        st.session_state.logged_in = False
        st.session_state.user = None
        st.session_state.restore_probe_done = False
        st.session_state.skip_auto_login_once = True
        st.session_state.active_view = "home"
        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


def _tracking_page(token):
    """Public page for viewing live emergency location tracking."""

    try:
        session = db.get_tracking_session(token)
        if not session:
            st.error("❌ Tracking session not found or has expired.")
            return
        
        # Extract session data based on database structure
        if isinstance(session, (list, tuple)):
            # SQL query result
            (session_id, alert_id, tracking_token, lat, lon, accuracy, source, username, phone, msg, last_updated, created_at) = session[:12]
        else:
            # Dictionary result
            lat = session.get("latitude")
            lon = session.get("longitude")
            username = session.get("username")
            phone = session.get("phone")
            msg = session.get("custom_message")
            accuracy = session.get("accuracy")
            source = session.get("source")
            last_updated = session.get("last_updated")
        
        st.markdown("---")
        st.title("🚨 Emergency Location Tracking")
        
        # Display person info
        st.markdown(f"""
        <div style='background:#fee2e2;border-left:4px solid #dc2626;padding:12px;border-radius:4px;margin:16px 0;'>
            <b>Emergency Alert Active</b><br/>
            Person in Emergency: <b>{html.escape(str(username or 'Unknown'))}</b><br/>
            Contact: <b>{html.escape(str(phone or 'N/A'))}</b>
        </div>
        """, unsafe_allow_html=True)
        
        # Display location map
        if lat is not None and lon is not None:
            st.markdown("### 📍 Current Location")
            
            # Create map using folium
            import folium
            from streamlit_folium import st_folium
            
            center = [lat, lon]
            m = folium.Map(location=center, zoom_start=15, tiles="OpenStreetMap")
            
            folium.Marker(
                location=center,
                popup=f"{username or 'Emergency'}<br>Phone: {phone or 'N/A'}",
                icon=folium.Icon(color="red", icon="exclamation"),
            ).add_to(m)
            
            st_folium(m, width=700, height=500)
            
            col1, col2 = st.columns(2)
            with col1:
                st.metric("📍 Latitude", f"{lat:.6f}")
            with col2:
                st.metric("📍 Longitude", f"{lon:.6f}")
            
            if accuracy:
                st.caption(f"Accuracy: ±{accuracy}m | Source: {source or 'unknown'}")
            
            if last_updated:
                try:
                    from datetime import datetime
                    last_ts = datetime.fromisoformat(str(last_updated).replace('Z', '+00:00'))
                    st.caption(f"Last updated: {last_ts.strftime('%Y-%m-%d %H:%M:%S UTC')}")
                except Exception:
                    st.caption(f"Last updated: {last_updated}")
        else:
            st.warning("⚠️ Location not yet available. Please wait for updates...")
        
        # Auto-refresh instruction
        st.info("📱 This page auto-refreshes every 3 seconds. If the person in emergency moves, their location will update automatically.")
        
        # JavaScript for auto-refresh
        js_code = """
        <script>
        setTimeout(() => {
            location.reload();
        }, 3000);
        </script>
        """
        st.markdown(js_code, unsafe_allow_html=True)
        
    except Exception as e:
        logging.error("Error loading tracking session: %s", str(e))
        st.error(f"❌ Error: {str(e)}")


# Section: Top navigation
def _top_nav():
    st.markdown("<div class='top-nav-title'>Navigation</div>", unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)

    with c1:
        if st.button("🏠", use_container_width=True, type="primary" if st.session_state.active_view == "home" else "secondary", key="nav_home"):
            st.session_state.active_view = "home"
            st.rerun()
    with c2:
        if st.button("➕", use_container_width=True, type="primary" if st.session_state.active_view == "contacts" else "secondary", key="nav_contacts"):
            st.session_state.active_view = "contacts"
            st.rerun()
    with c3:
        if st.button("⚙️", use_container_width=True, type="primary" if st.session_state.active_view == "settings" else "secondary", key="nav_settings"):
            st.session_state.active_view = "settings"
            st.rerun()


# Section: Main app entry
@st.cache_resource
def _bootstrap_app():
    """Run one-time startup work for the app process."""
    load_dotenv(override=True)
    setup_logging()
    init_db()
    ensure_startup_user(
        email=os.getenv("DEFAULT_LOGIN_EMAIL", "keerthanaveldanda05@gmail.com"),
        password=os.getenv("DEFAULT_LOGIN_PASSWORD", "123456789"),
        username=os.getenv("DEFAULT_LOGIN_USERNAME", "keerthana"),
        phone=os.getenv("DEFAULT_LOGIN_PHONE", "+911234567890"),
    )


def run_app():
    st.set_page_config(page_title="OneTapSOS", page_icon="🚨", layout="centered")
    _bootstrap_app()
    
    # Check if this is a tracking link
    query_params = st.query_params
    if query_params.get("token"):
        tracking_token = query_params.get("token")
        _tracking_page(tracking_token)
        return

    _init_state()
    _apply_styles()
    _clear_browser_refresh_timer()

    _sync_pending_sos_job()

    # If not logged in, probe local storage token once to avoid UI flicker.
    if not st.session_state.logged_in and not st.session_state.restore_probe_done:
        try:
            cookie_data = get_login_cookie()
            token = cookie_data.get("token") if cookie_data else ""
            if token:
                profile = get_user_by_session_token(token)
                if profile:
                    st.session_state.logged_in = True
                    st.session_state.user = profile
                    st.session_state.restore_probe_done = False
                    logging.info("Restored login from persistent session token.")
                else:
                    clear_login_cookie()
        except Exception as exc:
            logging.debug(f"Could not restore from localStorage: {exc}")
        finally:
            st.session_state.restore_probe_done = True

    if not st.session_state.logged_in:
        _auto_login_emergency_account()

    # If still not logged in, show auth screen
    if not st.session_state.logged_in:
        _render_auth()
        return

    st.session_state.skip_auto_login_once = False

    user = st.session_state.user
    if st.session_state.get("normalized_phones_for_user") != user["id"]:
        _normalize_existing_contact_phones(user["id"])
        st.session_state.normalized_phones_for_user = user["id"]
    contacts = _get_contacts(user["id"])
    _process_live_tracking(user, contacts)
    _process_safe_timer(user, contacts)

    panic_home = os.getenv("SOS_PANIC_UI", "1") == "1" and st.session_state.active_view == "home"
    if not panic_home:
        _render_header(user["username"])
        _top_nav()
        _render_smtp_banner()
    _render_sos_notice()

    view = st.session_state.active_view
    if view == "home":
        _home_page(user)
    elif view == "contacts":
        _contacts_page(user)
    elif view == "settings":
        _settings_page()
    else:
        _history_page(user)

