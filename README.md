# OneTapSOS - Smart Emergency Alert System

Production-ready Streamlit app for emergency alerts with secure authentication, contact management, live geolocation, and tri-channel alert dispatch.

## Features

- Signup, login, logout with bcrypt password hashing
- SQLite database for local development and optional PostgreSQL for cloud persistence
- Add, edit, delete emergency contacts
- One-click centered SOS button
- Browser-based GPS capture with Google Maps link
- Parallel SOS dispatch: email + voice call
- Emergency message customization
- Dark and light theme toggle
- Panic mode for minimal-click UI
- Keyboard SOS shortcut (press S key 3 times quickly)
- 5-second false-alarm cancel window
- Browser siren tone when SOS is armed
- Logging to sos.log and delivery status reporting
- **Live Tracking Dashboard**: Recipients receive tracking links in emails that show real-time emergency person location as they move (updates every 3 seconds)

## Project Structure

- app.py : Entry point
- main.py : Streamlit pages and app flow
- auth.py : Signup/login and bcrypt utilities
- db.py : SQLite schema and DB helpers
- utils.py : Geolocation, SMTP, validation, logging, JS helpers
- sos.db : Created automatically on first run
- sos.log : Runtime logs

## Requirements

Python 3.9+ recommended.

Install dependencies:

```bash
pip install -r requirements.txt
```

## Environment Variables

Set SMTP and Twilio credentials before sending alerts:

```powershell
$env:SMTP_HOST="smtp.gmail.com"
$env:SMTP_PORT="587"
$env:SMTP_USER="your_email@gmail.com"
$env:SMTP_PASSWORD="your_app_password"
$env:SMTP_FROM_NAME="OneTapSOS"
$env:TWILIO_ACCOUNT_SID="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
$env:TWILIO_AUTH_TOKEN="your_twilio_auth_token"
$env:TWILIO_FROM_NUMBER="+1xxxxxxxxxx"
$env:COOKIE_SECRET="replace-with-a-long-random-secret"
$env:DATABASE_URL="postgresql://USER:PASSWORD@HOST:PORT/DBNAME"
```

For Gmail, use an App Password with 2FA enabled.

Database behavior:

- If `DATABASE_URL` is set to a PostgreSQL URL, the app uses PostgreSQL.
- If `DATABASE_URL` is not set, the app uses local `sos.db` SQLite.
- For Streamlit Cloud deployment, use PostgreSQL (`DATABASE_URL`) so data persists across restarts/redeploys.

Suggested hosted PostgreSQL providers: Neon, Supabase, Render PostgreSQL.

## Run

```bash
streamlit run app.py
```

Open the URL shown in terminal (usually http://localhost:8501).

## Usage

1. Signup with username, email, phone, and password.
2. Login and add emergency contacts.
3. Optionally customize emergency message.
4. Press SOS (or press S key 3 times quickly).
5. SOS dispatch runs in parallel on all configured channels: email and call.
6. Check delivery status and alert history.

## Notes

- Power/volume hardware button taps are not accessible from browser security sandbox. A keyboard triple-press shortcut is provided as the web-safe equivalent.
- Keep SMTP credentials in environment variables only.
