"""
Conflict Monitor — Telegram One-Time Authentication
====================================================
Uses Telethon StringSession so NO file is written to the Docker volume
(avoids Windows Docker Desktop bind-mount permission errors).

After auth, the session string is printed AND automatically written into
your .env file as TELEGRAM_SESSION=...  The backend reads this on startup
and connects without any further action.

Usage (from project root):
    docker compose run --rm -it backend python app/auth.py
"""

import asyncio
import os
import re
import sys

# ── Load credentials ──────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

api_id_str  = os.environ.get("TELEGRAM_API_ID",   "").strip()
api_hash    = os.environ.get("TELEGRAM_API_HASH",  "").strip()
phone       = os.environ.get("TELEGRAM_PHONE",     "").strip()

print()
print("=" * 60)
print("  Conflict Monitor — Telegram Authentication")
print("=" * 60)
print()

if not api_id_str or not api_hash:
    print("ERROR: TELEGRAM_API_ID and/or TELEGRAM_API_HASH are missing.")
    print("       Check your .env file and try again.")
    sys.exit(1)

api_id = int(api_id_str)

print(f"  API ID   : {api_id}")
print(f"  API Hash : {api_hash[:8]}...")
print(f"  Phone    : {phone or '(will prompt)'}")
print()
print("  Using StringSession — no volume file writes needed.")
print()
print("Telegram will send you an SMS or app notification with a code.")
print("Enter it when prompted below.")
print()

# ── Auth ──────────────────────────────────────────────────────────────────────
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession


def _save_session_to_env(session_string: str):
    """Write TELEGRAM_SESSION into the .env file (create or update the line)."""
    # The .env is mounted from the host at /app/.env (via env_file in compose)
    # but the actual file on disk relative to auth.py is ../../.env
    # Try a few candidate paths.
    candidates = [
        "/app/.env",
        os.path.join(os.path.dirname(__file__), "..", "..", ".env"),
        ".env",
    ]
    env_path = None
    for c in candidates:
        p = os.path.abspath(c)
        if os.path.isfile(p):
            env_path = p
            break

    if env_path is None:
        print("  (Could not locate .env file to write automatically.)")
        return False

    with open(env_path, "r") as f:
        content = f.read()

    # Replace existing TELEGRAM_SESSION line or append it
    new_line = f"TELEGRAM_SESSION={session_string}"
    if re.search(r"^TELEGRAM_SESSION=", content, re.MULTILINE):
        content = re.sub(r"^TELEGRAM_SESSION=.*$", new_line, content, flags=re.MULTILINE)
    else:
        content = content.rstrip("\n") + "\n" + new_line + "\n"

    with open(env_path, "w") as f:
        f.write(content)

    print(f"  .env updated: {env_path}")
    return True


async def main():
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()

    # ── Phone / code flow ─────────────────────────────────────────────────────
    if phone:
        await client.send_code_request(phone)
        entered_phone = phone
    else:
        entered_phone = input("Enter your phone number (e.g. +13145551234): ").strip()
        await client.send_code_request(entered_phone)

    code = input("Enter the Telegram verification code: ").strip()

    try:
        await client.sign_in(entered_phone, code)
    except SessionPasswordNeededError:
        password = input("2FA is enabled — enter your Telegram password: ").strip()
        await client.sign_in(password=password)

    me = await client.get_me()
    session_string = client.session.save()
    await client.disconnect()

    # ── Output ────────────────────────────────────────────────────────────────
    print()
    print(f"SUCCESS!  Authenticated as: {me.first_name} {me.last_name or ''} (@{me.username or 'no username'})")
    print()
    print("Session string (already saved to .env if possible):")
    print()
    print(f"TELEGRAM_SESSION={session_string}")
    print()

    saved = _save_session_to_env(session_string)
    if not saved:
        print("ACTION REQUIRED: copy the line above into your .env file manually,")
        print("then restart the backend:  docker compose restart backend")
    else:
        print("Session written to .env automatically.")
        print("The backend will pick it up on the next restart.")

    print()


asyncio.run(main())
