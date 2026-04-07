import html
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import smtplib
import ssl
import time
import urllib.request
from urllib.error import HTTPError
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import streamlit.components.v1 as components
from streamlit_js_eval import get_geolocation, streamlit_js_eval
from db import execute

try:
  from twilio.rest import Client
except Exception:  # pragma: no cover
  Client = None


EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PHONE_PATTERN = re.compile(r"^\+?[1-9]\d{7,14}$")
_EMAIL_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sos-email")
_CALL_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sos-call")
_SMS_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sos-sms")
_IP_GEO_CACHE = None
_IP_GEO_CACHE_AT = 0.0
_IP_GEO_RETRY_AFTER = 0.0
DEFAULT_SOS_MESSAGE = (
  "Emergency alert from OneTapSOS. I need immediate help. "
  "Please contact me now and inform emergency services."
)


def setup_logging():
  """Configure file-based application logging."""
  log_path = os.path.join(os.path.dirname(__file__), "sos.log")
  root_logger = logging.getLogger()
  root_logger.setLevel(logging.INFO)

  for handler in root_logger.handlers:
    if isinstance(handler, RotatingFileHandler) and getattr(handler, "baseFilename", None) == log_path:
      return

  file_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
  file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
  root_logger.addHandler(file_handler)


def validate_email(email):
    """Return True if email has valid structure."""
    return EMAIL_PATTERN.match(str(email).strip()) is not None


def now_str():
    """Return current UTC timestamp as ISO string."""
    return datetime.utcnow().isoformat(timespec="seconds")


def build_map_link(lat, lon):
    """Generate a Google Maps link from coordinates."""
    return f"https://maps.google.com/?q={lat},{lon}"


def inject_keyboard_listener():
    """Inject JS that triggers SOS flag when S key is pressed three times quickly."""
    streamlit_js_eval(
        js_expressions="""
        (function () {
          if (window.__oneTapSOSKeysBound) {
            return "bound";
          }
          window.__oneTapSOSKeysBound = true;
          window.__sosTapCount = 0;
          window.__sosLastTap = 0;
          document.addEventListener('keydown', function (event) {
            if (event.key.toLowerCase() !== 's') {
              return;
            }
            const now = Date.now();
            if (now - window.__sosLastTap > 2000) {
              window.__sosTapCount = 0;
            }
            window.__sosTapCount += 1;
            window.__sosLastTap = now;
            if (window.__sosTapCount >= 3) {
              localStorage.setItem('onetapsos_trigger', String(Date.now()));
              window.__sosTapCount = 0;
            }
          });
          return "ok";
        })();
        """,
        key="bind_sos_shortcut",
    )


def keyboard_triggered():
    """Check and clear keyboard SOS trigger from localStorage."""
    value = streamlit_js_eval(
        js_expressions="(function(){const v=localStorage.getItem('onetapsos_trigger'); localStorage.removeItem('onetapsos_trigger'); return v;})();",
        key="read_sos_shortcut",
    )
    return bool(value)


def _request_geo_with_component():
    """Request geolocation in browser and cache result/error in localStorage."""
    ts = int(datetime.utcnow().timestamp() * 1000)

    # Preferred path: execute in the main page context.
    try:
        streamlit_js_eval(
            js_expressions="""
            (function(){
              try {
                if (!navigator.geolocation) {
                  localStorage.setItem('onetapsos_geo_error', 'Geolocation not supported in this browser.');
                  return 'unsupported';
                }
                navigator.geolocation.getCurrentPosition(
                  function(position){
                    const payload = {
                      lat: position.coords.latitude,
                      lon: position.coords.longitude,
                      accuracy: position.coords.accuracy,
                      ts: Date.now()
                    };
                    localStorage.setItem('onetapsos_geo_cache', JSON.stringify(payload));
                    localStorage.removeItem('onetapsos_geo_error');
                  },
                  function(error){
                    let msg = 'Unable to fetch location.';
                    if (error && error.code === 1) msg = 'Location permission denied in browser settings.';
                    if (error && error.code === 2) msg = 'Location unavailable. Check GPS/network.';
                    if (error && error.code === 3) msg = 'Location request timed out.';
                    localStorage.setItem('onetapsos_geo_error', msg);
                  },
                  { enableHighAccuracy: true, timeout: 12000, maximumAge: 0 }
                );
                return 'requested';
              } catch (e) {
                localStorage.setItem('onetapsos_geo_error', 'Location access failed.');
                return 'error';
              }
            })();
            """,
            key=f"request_geo_main_{ts}",
            want_output=False,
        )
        return
    except Exception as exc:
        logging.exception("Main-context geo request failed: %s", exc)

    # Fallback path: iframe component script.
    components.html(
        """
        <script>
        (function () {
          try {
            if (!navigator.geolocation) {
              localStorage.setItem('onetapsos_geo_error', 'Geolocation not supported in this browser.');
              return;
            }
            navigator.geolocation.getCurrentPosition(
              function (position) {
                const payload = {
                  lat: position.coords.latitude,
                  lon: position.coords.longitude,
                  accuracy: position.coords.accuracy,
                  ts: Date.now()
                };
                localStorage.setItem('onetapsos_geo_cache', JSON.stringify(payload));
                localStorage.removeItem('onetapsos_geo_error');
              },
              function (error) {
                let msg = 'Unable to fetch location.';
                if (error && error.code === 1) msg = 'Location permission denied in browser settings.';
                if (error && error.code === 2) msg = 'Location unavailable. Check GPS/network.';
                if (error && error.code === 3) msg = 'Location request timed out.';
                localStorage.setItem('onetapsos_geo_error', msg);
              },
              { enableHighAccuracy: true, timeout: 12000, maximumAge: 0 }
            );
          } catch (e) {
            localStorage.setItem('onetapsos_geo_error', 'Location access failed.');
          }
        })();
        </script>
        """,
        height=0,
    )


def _fetch_ip_location():
  """Fallback approximate location using public IP geolocation service."""
  global _IP_GEO_CACHE, _IP_GEO_CACHE_AT, _IP_GEO_RETRY_AFTER

  now = time.time()
  if _IP_GEO_CACHE and (now - _IP_GEO_CACHE_AT) < 3600:
    return _IP_GEO_CACHE

  if now < _IP_GEO_RETRY_AFTER:
    return None

  try:
    req = urllib.request.Request(
      "https://ipapi.co/json/",
      headers={"User-Agent": "OneTapSOS/1.0"},
    )
    with urllib.request.urlopen(req, timeout=6) as resp:
      payload = json.loads(resp.read().decode("utf-8"))

    lat = payload.get("latitude")
    lon = payload.get("longitude")
    if lat is None or lon is None:
      return None

    _IP_GEO_CACHE = {
      "lat": float(lat),
      "lon": float(lon),
      "map_link": build_map_link(lat, lon),
      "source": "ip",
      "accuracy": None,
    }
    _IP_GEO_CACHE_AT = now
    return _IP_GEO_CACHE
  except HTTPError as exc:
    if exc.code == 429:
      _IP_GEO_RETRY_AFTER = now + 3600
      logging.warning("IP geolocation fallback rate-limited; using local fallback coordinates instead.")
      return None
    logging.warning("IP geolocation fallback failed with HTTP %s", exc.code)
    return None
  except Exception as exc:
    logging.warning("IP geolocation fallback failed: %s", exc)
    return None


def fetch_location(skip_browser_request=False):
  """Fetch location and return (ok, payload_or_error).

  When skip_browser_request=True, the function avoids JS request/poll loops and
  falls back to IP/default coordinates quickly.
  """
  def _read_cached_geo(suffix):
    cached_value = streamlit_js_eval(
      js_expressions="""
      (function(){
        try {
          return localStorage.getItem('onetapsos_geo_cache');
        } catch (e) {
          return null;
        }
      })();
      """,
      key=f"read_geo_cache_{suffix}",
    )
    if not cached_value:
      return None
    parsed = json.loads(cached_value)
    lat = parsed.get("lat")
    lon = parsed.get("lon")
    accuracy = parsed.get("accuracy")
    if lat is None or lon is None:
      return None
    return {
      "lat": lat,
      "lon": lon,
      "map_link": build_map_link(lat, lon),
      "source": "browser",
      "accuracy": accuracy,
    }

  try:
    geo = get_geolocation()
    logging.info(f"Step 1 - Browser geolocation result: {geo}")
    if geo and "coords" in geo:
      coords = geo["coords"]
      lat = coords.get("latitude")
      lon = coords.get("longitude")
      accuracy = coords.get("accuracy")
      if lat is not None and lon is not None:
        logging.info(f"✅ SUCCESS: Got browser geolocation - lat: {lat}, lon: {lon}")
        return True, {
          "lat": lat,
          "lon": lon,
          "map_link": build_map_link(lat, lon),
          "source": "browser",
          "accuracy": accuracy,
        }
  except Exception as exc:
    logging.exception("Primary geolocation error: %s", exc)

  try:
    cached_geo = _read_cached_geo("initial")
    logging.info(f"Step 2 - Cached geolocation: {cached_geo}")
    if cached_geo:
      logging.info(f"✅ SUCCESS: Got cached geolocation - lat: {cached_geo['lat']}, lon: {cached_geo['lon']}")
      return True, cached_geo
  except Exception as exc:
    logging.exception("Cached geolocation read error: %s", exc)

  if skip_browser_request:
    ip_geo = _fetch_ip_location()
    logging.info(f"Step 2b - Fast-path IP geolocation result: {ip_geo}")
    if ip_geo:
      return True, ip_geo

    fallback_lat = float(os.getenv("DEFAULT_LAT", "17.3850"))
    fallback_lon = float(os.getenv("DEFAULT_LON", "78.4867"))
    logging.info(f"⚠️  FAST FALLBACK: Using default coordinates - lat: {fallback_lat}, lon: {fallback_lon}")
    return True, {
      "lat": fallback_lat,
      "lon": fallback_lon,
      "map_link": build_map_link(fallback_lat, fallback_lon),
      "source": "default",
      "accuracy": None,
    }

  if not skip_browser_request:
    _request_geo_with_component()

    # Give browser geolocation a short window to resolve before fallback.
    for attempt in range(1, 4):
      try:
        time.sleep(0.6)
        polled_geo = _read_cached_geo(f"poll_{attempt}_{int(datetime.utcnow().timestamp() * 1000)}")
        if polled_geo:
          logging.info(
            "✅ SUCCESS: Got browser geolocation after request (attempt %s) - lat: %s, lon: %s",
            attempt,
            polled_geo["lat"],
            polled_geo["lon"],
          )
          return True, polled_geo
      except Exception as exc:
        logging.debug("Geolocation polling attempt %s failed: %s", attempt, exc)

  try:
    error_msg = streamlit_js_eval(
      js_expressions="""
      (function(){
        try {
          return localStorage.getItem('onetapsos_geo_error');
        } catch (e) {
          return null;
        }
      })();
      """,
      key=f"read_geo_error_{int(datetime.utcnow().timestamp() * 1000)}",
    )
    logging.info(f"Step 3 - Geolocation error from browser: {error_msg}")
    if error_msg:
      ip_geo = _fetch_ip_location()
      logging.info(f"Step 4 - IP geolocation result: {ip_geo}")
      if ip_geo:
        logging.info(f"✅ SUCCESS: Got IP geolocation - {ip_geo}")
        return True, ip_geo

      fallback_lat = float(os.getenv("DEFAULT_LAT", "17.3850"))
      fallback_lon = float(os.getenv("DEFAULT_LON", "78.4867"))
      logging.info(f"⚠️  FALLBACK: Using default coordinates - lat: {fallback_lat}, lon: {fallback_lon}")
      return True, {
        "lat": fallback_lat,
        "lon": fallback_lon,
        "map_link": build_map_link(fallback_lat, fallback_lon),
        "source": "default",
        "accuracy": None,
      }
  except Exception as exc:
    logging.exception("Geo error-read failure: %s", exc)

  ip_geo = _fetch_ip_location()
  logging.info(f"Step 5 - Final IP geolocation attempt: {ip_geo}")
  if ip_geo:
    logging.info(f"✅ SUCCESS: Got IP geolocation (final) - {ip_geo}")
    return True, ip_geo

  fallback_lat = float(os.getenv("DEFAULT_LAT", "17.3850"))
  fallback_lon = float(os.getenv("DEFAULT_LON", "78.4867"))
  logging.info(f"⚠️  FALLBACK: Using default coordinates (final) - lat: {fallback_lat}, lon: {fallback_lon}")
  return True, {
    "lat": fallback_lat,
    "lon": fallback_lon,
    "map_link": build_map_link(fallback_lat, fallback_lon),
    "source": "default",
    "accuracy": None,
  }


def play_siren():
    """Play a short browser siren tone using Web Audio API."""
    streamlit_js_eval(
        js_expressions="""
        (function(){
          try {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const gain = ctx.createGain();
            gain.gain.value = 0.04;
            gain.connect(ctx.destination);
            const osc = ctx.createOscillator();
            osc.type = 'sawtooth';
            osc.connect(gain);
            osc.frequency.setValueAtTime(740, ctx.currentTime);
            osc.frequency.linearRampToValueAtTime(430, ctx.currentTime + 0.4);
            osc.frequency.linearRampToValueAtTime(760, ctx.currentTime + 0.8);
            osc.start();
            osc.stop(ctx.currentTime + 1.2);
          } catch (e) {}
          return 'played';
        })();
        """,
        key=f"siren_{datetime.utcnow().timestamp()}",
    )


def _smtp_settings():
  """Load SMTP settings from environment variables."""
  smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
  try:
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
  except ValueError:
    smtp_port = 587
  smtp_user = os.getenv("SMTP_USER", "").strip()
  smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
  smtp_from_name = os.getenv("SMTP_FROM_NAME", "OneTapSOS").strip() or "OneTapSOS"
  return {
    "host": smtp_host,
    "port": smtp_port,
    "user": smtp_user,
    "password": smtp_password,
    "from_name": smtp_from_name,
  }


def _normalize_contacts(contacts):
  """Return unique contacts with valid email addresses."""
  normalized = []
  seen = set()

  for contact in contacts or []:
    email = str(contact.get("email", "")).strip()
    if not email or not validate_email(email):
      continue

    email_key = email.lower()
    if email_key in seen:
      continue
    seen.add(email_key)

    normalized.append(
      {
        "name": str(contact.get("name", "Contact")).strip() or "Contact",
        "email": email,
        "phone": str(contact.get("phone", "")).strip(),
      }
    )

  return normalized


def _normalize_call_contacts(contacts):
  """Return unique contacts with valid phone numbers."""
  normalized = []
  seen = set()

  for contact in contacts or []:
    phone = _normalize_phone_number(contact.get("phone", ""))
    if not phone:
      continue

    key = phone
    if key in seen:
      continue
    seen.add(key)

    normalized.append(
      {
        "name": str(contact.get("name", "Contact")).strip() or "Contact",
        "phone": phone,
      }
    )

  return normalized


def _normalize_phone_number(raw_phone):
  """Normalize to E.164-like format; auto-add default country code for local 10-digit numbers."""
  raw = str(raw_phone or "").strip()
  if not raw:
    return ""

  # Keep leading + if provided, strip all other non-digits.
  if raw.startswith("+"):
    digits = re.sub(r"\D", "", raw[1:])
    candidate = f"+{digits}" if digits else ""
  else:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
      default_cc = (os.getenv("DEFAULT_COUNTRY_CODE", "+91") or "+91").strip()
      cc_digits = re.sub(r"\D", "", default_cc)
      candidate = f"+{cc_digits}{digits}" if cc_digits else f"+{digits}"
    elif 11 <= len(digits) <= 15:
      candidate = f"+{digits}"
    else:
      candidate = raw

  if PHONE_PATTERN.match(candidate):
    return candidate
  return ""


def _twilio_error_message(exc):
  code = getattr(exc, "code", None)
  base = str(exc)
  if code == 21408:
    return f"{base} | Twilio geo permissions blocked this destination number (error 21408)."
  if code == 21219:
    return (
      f"{base} | Twilio blocked this destination number (error 21219). "
      "On trial accounts, the number must be verified in Twilio Console."
    )
  return base


def _build_sos_message(user, location, recipient_email):
  """Build a structured SOS email for a single recipient."""
  smtp_user = os.getenv("SMTP_USER", "").strip()
  sender_name = str(user.get("username", "Unknown user")).strip() or "Unknown user"
  sender_phone = str(user.get("phone", "")).strip() or "N/A"
  default_message = os.getenv("SOS_DEFAULT_MESSAGE", DEFAULT_SOS_MESSAGE).strip() or DEFAULT_SOS_MESSAGE
  emergency_message = str(location.get("message") or default_message).strip()
  map_link = str(location.get("map_link", "")).strip()
  timestamp = str(location.get("timestamp") or now_str())
  latitude = location.get("lat")
  longitude = location.get("lon")
  from_name = str(user.get("username", "OneTapSOS")).strip() or "OneTapSOS"
  reply_to = str(user.get("email", "")).strip() or smtp_user

  plain_body = (
    "Emergency Alert from OneTapSOS\n\n"
    f"User: {sender_name}\n"
    f"Phone: {sender_phone}\n"
    f"Emergency Message: {emergency_message}\n"
    f"Live Location: {map_link}\n"
    f"Coordinates: {latitude}, {longitude}\n"
    f"Timestamp (UTC): {timestamp}\n"
    f"Recipient: {recipient_email}\n"
  )

  html_body = f"""
  <html>
    <body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;">
    <div style="max-width:640px;margin:0 auto;padding:24px;">
      <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:16px;padding:24px;box-shadow:0 8px 24px rgba(15,23,42,0.08);">
      <div style="font-size:18px;font-weight:700;color:#b91c1c;margin-bottom:8px;">Emergency SOS Alert</div>
      <div style="font-size:14px;color:#374151;margin-bottom:20px;">An SOS trigger was activated in OneTapSOS.</div>
      <table style="width:100%;border-collapse:collapse;font-size:14px;color:#111827;">
        <tr><td style="padding:8px 0;color:#6b7280;width:34%;">User</td><td style="padding:8px 0;font-weight:700;">{html.escape(sender_name)}</td></tr>
        <tr><td style="padding:8px 0;color:#6b7280;">Phone</td><td style="padding:8px 0;font-weight:700;">{html.escape(sender_phone)}</td></tr>
        <tr><td style="padding:8px 0;color:#6b7280;">Message</td><td style="padding:8px 0;font-weight:700;">{html.escape(emergency_message)}</td></tr>
        <tr><td style="padding:8px 0;color:#6b7280;">Location</td><td style="padding:8px 0;"><a href="{html.escape(map_link)}" style="color:#2563eb;font-weight:700;">Open live location</a></td></tr>
        <tr><td style="padding:8px 0;color:#6b7280;">Coordinates</td><td style="padding:8px 0;font-weight:700;">{html.escape(str(latitude))}, {html.escape(str(longitude))}</td></tr>
        <tr><td style="padding:8px 0;color:#6b7280;">Timestamp (UTC)</td><td style="padding:8px 0;font-weight:700;">{html.escape(timestamp)}</td></tr>
      </table>
      <div style="margin-top:20px;padding-top:16px;border-top:1px solid #e5e7eb;font-size:12px;color:#6b7280;">
        Recipient: {html.escape(recipient_email)}
      </div>
      </div>
    </div>
    </body>
  </html>
  """

  message = MIMEMultipart("alternative")
  message["Subject"] = f"URGENT SOS ALERT - {sender_name}"
  message["From"] = f"{from_name} <{smtp_user}>"
  if reply_to:
    message["Reply-To"] = reply_to
  message["To"] = recipient_email
  message.attach(MIMEText(plain_body, "plain", "utf-8"))
  message.attach(MIMEText(html_body, "html", "utf-8"))
  return message


def _send_with_retries(smtp_settings, recipient_email, message, max_attempts=3):
  """Send one email with retry handling."""
  tls_context = ssl.create_default_context()
  attempt_errors = []

  for attempt in range(1, max_attempts + 1):
    try:
      with smtplib.SMTP(smtp_settings["host"], smtp_settings["port"], timeout=20) as server:
        server.ehlo()
        server.starttls(context=tls_context)
        server.ehlo()
        server.login(smtp_settings["user"], smtp_settings["password"])
        server.sendmail(smtp_settings["user"], [recipient_email], message.as_string())

      logging.info(
        "SOS email sent successfully to %s on attempt %s at %s",
        recipient_email,
        attempt,
        now_str(),
      )
      return True, []
    except smtplib.SMTPAuthenticationError as exc:
      hint = (
        "Gmail rejected the login. Use a Google App Password with 2-step verification enabled "
        "or verify the SMTP username/password in .env."
      )
      error_text = f"attempt {attempt}: {exc} | {hint}"
      attempt_errors.append(error_text)
      logging.warning("SOS email authentication failed for %s (%s)", recipient_email, error_text)
    except Exception as exc:
      error_text = f"attempt {attempt}: {exc}"
      attempt_errors.append(error_text)
      logging.warning("SOS email failed for %s (%s)", recipient_email, error_text)
      if attempt < max_attempts:
        time.sleep(min(2 ** (attempt - 1), 4))

  logging.error("SOS email permanently failed for %s: %s", recipient_email, attempt_errors[-1])
  return False, attempt_errors


def _update_alert_record(alert_id, status, recipients_count):
  """Persist final alert delivery status for history and monitoring."""
  if not alert_id:
    return

  try:
    execute(
      "UPDATE alerts SET email_status=?, recipients_count=? WHERE id=?",
      (status, recipients_count, alert_id),
    )
  except Exception as exc:
    logging.exception("Failed to update alert %s with status %s: %s", alert_id, status, exc)


def send_sos_email(user, contacts, location):
  """Send SOS emails to all valid contacts and return a delivery summary."""
  smtp_settings = _smtp_settings()
  alert_id = location.get("alert_id")
  timestamp = str(location.get("timestamp") or now_str())
  recipient_list = _normalize_contacts(contacts)

  logging.info(
    "Starting SOS email worker for user=%s, recipients=%s, alert_id=%s",
    user.get("id"),
    len(recipient_list),
    alert_id,
  )

  result = {
    "success": False,
    "partial": False,
    "sent_count": 0,
    "failed_recipients": [],
    "invalid_recipients": [],
    "recipients_count": len(recipient_list),
    "timestamp": timestamp,
  }

  if not smtp_settings["user"] or not smtp_settings["password"]:
    error_message = "SMTP credentials are not configured in environment variables."
    logging.error(error_message)
    result["failed_recipients"].append(error_message)
    _update_alert_record(alert_id, "failed", len(recipient_list))
    return result

  if not recipient_list:
    error_message = "No valid emergency contacts were found."
    logging.warning(error_message)
    result["failed_recipients"].append(error_message)
    _update_alert_record(alert_id, "failed", 0)
    return result

  for contact in contacts or []:
    email = str(contact.get("email", "")).strip()
    if email and not validate_email(email):
      result["invalid_recipients"].append(email)
      logging.warning("Skipped invalid SOS email address: %s", email)

  def _send_to_contact(contact):
    recipient_email = contact["email"]
    message = _build_sos_message(user, location, recipient_email)
    sent, errors = _send_with_retries(smtp_settings, recipient_email, message)
    return recipient_email, sent, errors

  max_workers = max(1, min(4, len(recipient_list)))
  with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="sos-email-contact") as pool:
    futures = [pool.submit(_send_to_contact, contact) for contact in recipient_list]
    for future in futures:
      recipient_email, sent, errors = future.result()
      if sent:
        result["sent_count"] += 1
      else:
        result["failed_recipients"].append(
          {
            "email": recipient_email,
            "errors": errors,
          }
        )

  result["partial"] = result["sent_count"] > 0 and bool(result["failed_recipients"])
  result["success"] = result["sent_count"] > 0 and not result["failed_recipients"]
  if result["success"]:
    alert_status = "success"
  elif result["partial"]:
    alert_status = "partial"
  else:
    alert_status = "failed"
  _update_alert_record(alert_id, alert_status, len(recipient_list))

  return result


def queue_sos_email_delivery(user, contacts, location):
  """Submit SOS email delivery to the shared background executor."""
  logging.info(
    "Queueing SOS email delivery for user=%s with %s raw contacts (alert_id=%s)",
    user.get("id"),
    len(contacts or []),
    location.get("alert_id"),
  )
  return _EMAIL_EXECUTOR.submit(send_sos_email, user, contacts, location)


def _voice_settings():
  return {
    "account_sid": os.getenv("TWILIO_ACCOUNT_SID", "").strip(),
    "auth_token": os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
    "from_number": (
      os.getenv("TWILIO_FROM_NUMBER")
      or os.getenv("TWILIO_PHONE_NUMBER")
      or ""
    ).strip(),
  }


def send_sos_calls(user, contacts, location):
  """Place SOS calls to all valid contact phone numbers using Twilio."""
  recipient_list = _normalize_call_contacts(contacts)
  result = {
    "success": False,
    "called_count": 0,
    "failed_recipients": [],
    "invalid_recipients": [],
    "recipients_count": len(recipient_list),
    "timestamp": str(location.get("timestamp") or now_str()),
  }

  for contact in contacts or []:
    raw_phone = str(contact.get("phone", "")).strip()
    normalized_phone = _normalize_phone_number(raw_phone)
    if raw_phone and not normalized_phone:
      result["invalid_recipients"].append(raw_phone)

  if not recipient_list:
    result["failed_recipients"].append("No valid phone numbers found for calls.")
    return result

  if Client is None:
    result["failed_recipients"].append("Twilio SDK is unavailable.")
    return result

  voice_cfg = _voice_settings()
  if not voice_cfg["account_sid"] or not voice_cfg["auth_token"] or not voice_cfg["from_number"]:
    result["failed_recipients"].append("Twilio credentials are missing.")
    return result

  emergency_message = str(location.get("message") or DEFAULT_SOS_MESSAGE).strip()
  sender_name = str(user.get("username", "User")).strip() or "User"
  sender_phone = str(user.get("phone", "")).strip() or "not available"
  twiml = (
    "<Response><Say voice='alice'>"
    "Emergency alert from One Tap S O S. "
    f"The person who pressed S O S is {sender_name}. "
    f"Their phone number is {sender_phone}. "
    f"Emergency message is: {emergency_message}. "
    "Please contact them immediately."
    "</Say></Response>"
  )

  client = Client(voice_cfg["account_sid"], voice_cfg["auth_token"])

  for contact in recipient_list:
    phone = contact["phone"]
    try:
      call = client.calls.create(
        twiml=twiml,
        to=phone,
        from_=voice_cfg["from_number"],
      )
      logging.info("SOS call placed to %s with sid %s", phone, call.sid)
      result["called_count"] += 1
    except Exception as exc:
      err = _twilio_error_message(exc)
      logging.warning("SOS call failed for %s: %s", phone, err)
      result["failed_recipients"].append({"phone": phone, "error": err})

  result["success"] = result["called_count"] > 0 and not result["failed_recipients"]
  return result


def queue_sos_call_delivery(user, contacts, location):
  """Submit SOS call delivery to the shared background executor with optional delay."""
  try:
    delay_seconds = int(os.getenv("CALL_DELAY_SECONDS", "0"))
  except ValueError:
    delay_seconds = 0

  delay_seconds = max(0, delay_seconds)

  def _delayed_call_job():
    if delay_seconds > 0:
      logging.info("Delaying SOS calls by %s seconds after email dispatch.", delay_seconds)
      time.sleep(delay_seconds)
    return send_sos_calls(user, contacts, location)

  return _CALL_EXECUTOR.submit(_delayed_call_job)


def send_sos_sms(user, contacts, location):
  """Send SOS SMS messages to all valid contact phone numbers using Twilio."""
  recipient_list = _normalize_call_contacts(contacts)
  result = {
    "success": False,
    "sent_count": 0,
    "failed_recipients": [],
    "invalid_recipients": [],
    "recipients_count": len(recipient_list),
    "timestamp": str(location.get("timestamp") or now_str()),
  }

  for contact in contacts or []:
    raw_phone = str(contact.get("phone", "")).strip()
    normalized_phone = _normalize_phone_number(raw_phone)
    if raw_phone and not normalized_phone:
      result["invalid_recipients"].append(raw_phone)

  if not recipient_list:
    result["failed_recipients"].append("No valid phone numbers found for SMS.")
    return result

  if Client is None:
    result["failed_recipients"].append("Twilio SDK is unavailable.")
    return result

  sms_cfg = _voice_settings()
  if not sms_cfg["account_sid"] or not sms_cfg["auth_token"] or not sms_cfg["from_number"]:
    result["failed_recipients"].append("Twilio credentials are missing.")
    return result

  sender_name = str(user.get("username", "User")).strip() or "User"
  emergency_message = str(location.get("message") or DEFAULT_SOS_MESSAGE).strip()
  map_link = str(location.get("map_link") or "").strip()
  body = (
    f"EMERGENCY ALERT: {sender_name} needs immediate help. "
    f"Message: {emergency_message} "
    f"Location: {map_link}"
  )

  client = Client(sms_cfg["account_sid"], sms_cfg["auth_token"])

  for contact in recipient_list:
    phone = contact["phone"]
    try:
      msg = client.messages.create(
        body=body,
        to=phone,
        from_=sms_cfg["from_number"],
      )
      logging.info("SOS SMS sent to %s with sid %s", phone, msg.sid)
      result["sent_count"] += 1
    except Exception as exc:
      err = _twilio_error_message(exc)
      logging.warning("SOS SMS failed for %s: %s", phone, err)
      result["failed_recipients"].append({"phone": phone, "error": err})

  result["success"] = result["sent_count"] > 0 and not result["failed_recipients"]
  return result


def queue_sos_sms_delivery(user, contacts, location):
  """Submit SOS SMS delivery to the shared background executor."""
  return _SMS_EXECUTOR.submit(send_sos_sms, user, contacts, location)


def send_alert_emails(sender_name, sender_phone, custom_message, map_link, recipients):
  """Compatibility wrapper for legacy callers."""
  user = {"username": sender_name, "phone": sender_phone}
  location = {"message": custom_message, "map_link": map_link, "timestamp": now_str()}
  result = send_sos_email(user, recipients, location)
  errors = []
  for item in result.get("failed_recipients", []):
    if isinstance(item, str):
      errors.append(item)
    else:
      errors.append(f"{item['email']}: {'; '.join(item['errors'])}")
  for email in result.get("invalid_recipients", []):
    errors.append(f"invalid: {email}")
  return result["success"], errors
