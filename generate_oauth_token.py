#!/usr/bin/env python3
"""
Generate OAuth 2.1 User Access Token for Kick Bot
Docs: https://docs.kick.com/getting-started/generating-tokens-oauth2-flow
"""
import base64
import hashlib
import logging
import os
import secrets
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import psycopg2
import requests

from utils.secret_settings import encrypt_secret

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Credentials must come from the environment. Hardcoded fallbacks were
# removed because they leaked production secrets into source control —
# rotate any value that ever lived here in your Kick developer portal
# and Railway/host. Set these in your shell or `.env` before running:
#   $env:KICK_CLIENT_ID = "..."
#   $env:KICK_CLIENT_SECRET = "..."
#   $env:DATABASE_URL = "postgresql://..."
CLIENT_ID = os.getenv("KICK_CLIENT_ID")
CLIENT_SECRET = os.getenv("KICK_CLIENT_SECRET")
REDIRECT_URI = os.getenv("KICK_OAUTH_REDIRECT_URI", "http://localhost:8888/callback")
DATABASE_URL = os.getenv("DATABASE_URL", "")

if not CLIENT_ID or not CLIENT_SECRET:
    raise SystemExit(
        "❌ KICK_CLIENT_ID and KICK_CLIENT_SECRET must be set in the environment.\n"
        "   PowerShell: $env:KICK_CLIENT_ID = '...'; $env:KICK_CLIENT_SECRET = '...'"
    )

# Global state for callback
callback_data = {"code": None, "state": None, "error": None, "done": False}

# Scopes required for the bot.
# These are the ONLY scopes Kick currently exposes via OAuth 2.1 — anything
# else (kicks:read, webhook:subscribe, moderator:execute, channel_points_*,
# channel:rewards:read) returns `invalid_scope`. Webhook subscriptions are
# covered by `events:subscribe`; moderation actions by `moderation:ban`.
# Source: https://docs.kick.com
SCOPES = " ".join(
    [
        "user:read",
        "channel:read",
        "channel:write",
        "chat:write",
        "events:subscribe",
        "moderation:ban",
    ]
)


def generate_pkce_pair():
    """Generate code_verifier and code_challenge for PKCE"""
    # Generate random code_verifier (43-128 characters)
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("utf-8").rstrip("=")

    # Generate code_challenge (SHA256 hash of verifier)
    challenge = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    code_challenge = base64.urlsafe_b64encode(challenge).decode("utf-8").rstrip("=")

    return code_verifier, code_challenge


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler for OAuth callback"""

    def log_message(self, format, *args):
        """Suppress default logging"""
        pass

    def do_GET(self):
        """Handle GET request to /callback"""
        parsed = urlparse(self.path)

        if parsed.path == "/callback":
            params = parse_qs(parsed.query)

            if "error" in params:
                callback_data["error"] = params["error"][0]
                callback_data["done"] = True
                self.send_response(400)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(
                    f"""
                    <html>
                    <body style="font-family: Arial; padding: 50px; text-align: center;">
                        <h1 style="color: red;">❌ Authorization Failed</h1>
                        <p>Error: {params['error'][0]}</p>
                        <p>You can close this window.</p>
                    </body>
                    </html>
                """.encode()
                )
            elif "code" in params:
                callback_data["code"] = params["code"][0]
                callback_data["state"] = params.get("state", [None])[0]
                callback_data["done"] = True
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(
                    """
                    <html>
                    <body style="font-family: Arial; padding: 50px; text-align: center;">
                        <h1 style="color: green;">✅ Authorization Successful!</h1>
                        <p>Processing tokens...</p>
                        <p><em>You can close this window.</em></p>
                    </body>
                    </html>
                """.encode()
                )
            else:
                self.send_response(400)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Invalid callback")
        else:
            self.send_response(404)
            self.end_headers()


def start_callback_server():
    """Start HTTP server for OAuth callback"""
    server = HTTPServer(("localhost", 8888), OAuthCallbackHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    return server


def step1_authorize():
    """Step 1: Open authorization URL in browser"""
    code_verifier, code_challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(16)

    # Build authorization URL
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }

    auth_url = f"https://id.kick.com/oauth/authorize?{urlencode(params)}"

    print("=" * 80)
    print("STEP 1: Authorize the Bot Application")
    print("=" * 80)
    print(f"\n1. Log in to Kick as MAIKELELE (the streamer account)")
    print(f"2. Authorize the BOT APPLICATION to act on maikelele's behalf")
    print(f"3. Opening authorization URL in browser...\n")
    print(f"URL: {auth_url}\n")
    print(f"3. After authorizing, you'll be redirected to:")
    print(f"   {REDIRECT_URI}?code=<CODE>&state=<STATE>")
    print(f"\n4. Copy the 'code' parameter from the URL\n")

    # Save for step 2
    with open("oauth_verifier.txt", "w") as f:
        f.write(code_verifier)

    # Open browser
    webbrowser.open(auth_url)

    return code_verifier


def step2_exchange_code(code, code_verifier, discord_server_id=None):
    """Step 2: Exchange authorization code for access token"""
    print("\n" + "=" * 80)
    print("STEP 2: Exchange Code for Access Token")
    print("=" * 80)

    # Exchange code for token
    data = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": code_verifier,
        "code": code,
    }

    print(f"\nExchanging authorization code for token...")
    response = requests.post(
        "https://id.kick.com/oauth/token", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )

    if response.status_code == 200:
        token_data = response.json()
        print(f"\n✅ SUCCESS! Token generated:\n")
        # Token VALUES are deliberately not printed. This script runs in terminals,
        # CI logs and screen shares; echoing a live credential is how it ends up
        # somewhere it can't be un-leaked. The tokens are persisted (encrypted)
        # below — nothing here needs the operator to copy them by hand.
        logger.info(f"Token Type:    {token_data['token_type']}")
        logger.info(f"Expires In:    {token_data['expires_in']} seconds")
        logger.info(f"Access Token:  [received, not shown]")
        logger.info(f"Refresh Token: {'[received, not shown]' if token_data.get('refresh_token') else '[none]'}")
        logger.info(f"Scopes:        {token_data.get('scope', 'N/A')}")

        # Save to database if discord_server_id provided
        if discord_server_id:
            save_to_database(discord_server_id, token_data)
        else:
            print("\n⚠️  No discord_server_id provided - tokens not saved to database")
            print("Run script again and provide discord_server_id to save tokens")

        print("\n" + "=" * 80)
        print("TOKENS GENERATED SUCCESSFULLY")
        print("=" * 80)

        return token_data
    else:
        print(f"\n❌ ERROR: {response.status_code}")
        print(response.text)
        return None


def save_to_database(discord_server_id, token_data, kick_user_id=0, kick_username=None):
    """Save OAuth tokens to the CANONICAL kick_oauth_tokens table.

    This used to write the legacy `bot_settings` rows ('kick_oauth_token' /
    'kick_refresh_token'). An operational script must not be able to recreate the
    duplicate plaintext credentials the runtime no longer reads — otherwise one
    manual run silently reintroduces the disclosure path.
    """
    if not DATABASE_URL:
        logger.warning("DATABASE_URL not set — skipping DB save.")
        logger.warning("Re-run with DATABASE_URL set, or use the dashboard OAuth flow.")
        return
    try:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(token_data.get("expires_in") or 3600))

        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO kick_oauth_tokens (
                user_id, discord_server_id, kick_username,
                access_token, refresh_token, scopes, expires_at, needs_reauth
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE)
            ON CONFLICT (user_id, discord_server_id) DO UPDATE SET
                kick_username = COALESCE(EXCLUDED.kick_username, kick_oauth_tokens.kick_username),
                access_token  = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token,
                scopes        = EXCLUDED.scopes,
                expires_at    = EXCLUDED.expires_at,
                needs_reauth  = FALSE,
                updated_at    = CURRENT_TIMESTAMP
            """,
            (
                int(kick_user_id or 0),
                int(discord_server_id),
                kick_username,
                encrypt_secret(token_data["access_token"]),
                encrypt_secret(token_data.get("refresh_token") or ""),
                token_data.get("scope", ""),
                expires_at,
            ),
        )
        conn.commit()
        cur.close()
        conn.close()

        logger.info(f"Canonical Kick token saved for discord_server_id={discord_server_id}")
        logger.info(f"Access token expires in {token_data.get('expires_in')} seconds")
    except Exception as e:
        logger.warning(f"Failed to save to database: {e}")
        logger.warning("Use the dashboard OAuth flow instead.")


def refresh_token(refresh_token):
    """Refresh an expired access token"""
    print("\n" + "=" * 80)
    print("REFRESH ACCESS TOKEN")
    print("=" * 80)

    data = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
    }

    print(f"\nRefreshing access token...")
    response = requests.post(
        "https://id.kick.com/oauth/token", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )

    if response.status_code == 200:
        token_data = response.json()
        print(f"\n✅ SUCCESS! Token refreshed:\n")
        print(f"New Access Token expires in: {token_data['expires_in']} seconds")

        print("\n⚠️  IMPORTANT: Update tokens in database manually")
        print("Tokens are NOT saved to files for security reasons")
        return token_data
    else:
        print(f"\n❌ ERROR: {response.status_code}")
        print(response.text)
        return None


if __name__ == "__main__":
    import sys
    import time

    if len(sys.argv) == 1:
        # Automatic flow with callback server
        print("=" * 80)
        print("AUTOMATIC OAUTH 2.1 TOKEN GENERATION")
        print("=" * 80)

        # Ask for discord_server_id
        discord_server_id = input("\nEnter discord_server_id (or press Enter to skip database save): ").strip()
        if discord_server_id:
            try:
                discord_server_id = int(discord_server_id)
            except ValueError:
                print("Invalid discord_server_id, skipping database save")
                discord_server_id = None
        else:
            discord_server_id = None

        # Start callback server
        print("\n🌐 Starting local callback server on http://localhost:8888...")
        server = start_callback_server()

        # Generate PKCE pair
        code_verifier, code_challenge = generate_pkce_pair()
        state = secrets.token_urlsafe(16)

        # Build authorization URL
        params = {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }

        auth_url = f"https://id.kick.com/oauth/authorize?{urlencode(params)}"

        print("\n" + "=" * 80)
        print("STEP 1: Authorize the Bot Application")
        print("=" * 80)
        print(f"\n1. Log in to Kick as MAIKELELE (the streamer account)")
        print(f"2. Authorize the BOT APPLICATION to act on maikelele's behalf")
        print(f"3. Opening authorization URL in browser...\n")
        print(f"URL: {auth_url}\n")
        print(f"⏳ Waiting for authorization...\n")

        # Open browser
        webbrowser.open(auth_url)

        # Wait for callback
        while not callback_data["done"]:
            time.sleep(0.5)

        # Stop server
        server.shutdown()

        # Check for errors
        if callback_data["error"]:
            print(f"\n❌ Authorization failed: {callback_data['error']}")
            sys.exit(1)

        # Exchange code for token
        if callback_data["code"]:
            step2_exchange_code(callback_data["code"], code_verifier, discord_server_id)
        else:
            print("\n❌ No authorization code received")
            sys.exit(1)

    elif len(sys.argv) == 2:
        # Manual flow: Step 2 only
        code = sys.argv[1]

        # Load code_verifier from file
        try:
            with open("oauth_verifier.txt", "r") as f:
                code_verifier = f.read().strip()
        except FileNotFoundError:
            print("❌ ERROR: oauth_verifier.txt not found")
            print("Run the script without arguments to use automatic flow")
            sys.exit(1)

        step2_exchange_code(code, code_verifier)

    elif len(sys.argv) == 3 and sys.argv[1] == "--refresh":
        # Refresh token
        refresh_token_value = sys.argv[2]
        refresh_token(refresh_token_value)

    else:
        print("Usage:")
        print("  Automatic flow:  python generate_oauth_token.py")
        print("  Manual exchange: python generate_oauth_token.py <CODE>")
        print("  Refresh token:   python generate_oauth_token.py --refresh <REFRESH_TOKEN>")
