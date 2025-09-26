# app/main.py
# app/main.py
import os
import sys

# 🔒 ABSOLUTE FIRST: Force HTTP/1.1 for all httpx usage to fix Windows SSL
# This must run before httpx, supabase, or any network lib is imported
try:
    import httpx
    from httpx import HTTPTransport

    # Save original
    _orig_init = httpx.Client.__init__

    def _patched_init(self, *args, **kwargs):
        if "http2" not in kwargs:
            kwargs["http2"] = False
        if "transport" not in kwargs:
            kwargs["transport"] = HTTPTransport(http2=False)
        _orig_init(self, *args, **kwargs)

    httpx.Client.__init__ = _patched_init

    # Optional: patch AsyncClient
    if hasattr(httpx, 'AsyncClient'):
        _orig_async = httpx.AsyncClient.__init__
        def _patched_async(self, *args, **kwargs):
            if "http2" not in kwargs:
                kwargs["http2"] = False
            _orig_async(self, *args, **kwargs)
        httpx.AsyncClient.__init__ = _patched_async

except Exception as e:
    print(f"⚠️ Failed to patch httpx: {e}", file=sys.stderr)

# ✅ NOW safe to import everything else
from flask import Flask, request, send_file
from twilio.twiml.messaging_response import MessagingResponse
from supabase import create_client, Client
import qrcode
from io import BytesIO
from twilio.rest import Client as TwilioClient
import uuid
import requests
import secrets
from flask import render_template_string, render_template
import hashlib
import hmac
from postgrest.exceptions import APIError as PostgrestAPIError
import re
import json
from datetime import datetime
import time
import httpx  # This is now safe — already patched
import ssl
from httpx import Client as HTTPXClient
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import threading
import time
from datetime import datetime, timedelta, timezone
import pytz
from functools import lru_cache
from datetime import datetime, timedelta
import pytz


# Now safe to import
from flask import Flask
from supabase import create_client, Client
from dotenv import load_dotenv
from pathlib import Path

# ====== ROBUST HTTP CLIENT FOR WINDOWS SSL ======
def create_robust_http_client():
    import ssl
    from httpx import HTTPTransport, Client as HTTPXClient
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.options |= ssl.OP_NO_TICKET      # Critical for Windows SSL session reuse bug
    ctx.options |= ssl.OP_NO_COMPRESSION
    transport = HTTPTransport(http2=False, verify=ctx, retries=3)
    return HTTPXClient(transport=transport, timeout=30.0)

# Initialize Flask app
BASE_DIR = Path(__file__).parent
TEMPLATE_DIR = BASE_DIR / "templates"
app = Flask(__name__, template_folder=TEMPLATE_DIR)
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# Validation
assert SUPABASE_URL and SUPABASE_URL.startswith("https://"), "Invalid SUPABASE_URL"
assert SUPABASE_KEY, "Missing SUPABASE_KEY"
assert SUPABASE_SERVICE_ROLE_KEY, "Missing SUPABASE_SERVICE_ROLE_KEY"

# ✅ Create robust HTTP client once
http_client = create_robust_http_client()

# ✅ Initialize Supabase clients with the patched, SSL-safe client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
supabase_service: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

print("✅ Supabase clients ready (HTTP/2 disabled, SSL hardened for Windows)")



# Init Twilio
twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
TWILIO_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
print(">>> TWILIO CREDENTIALS:")
print("SID:", os.getenv("TWILIO_ACCOUNT_SID"))
print("TOKEN:", os.getenv("TWILIO_AUTH_TOKEN"))

BASE_DIR = Path(__file__).resolve().parent.parent  # project root
TEMPLATE_DIR = BASE_DIR / "templates"

def log_bot_reply(phone: str, message: str):
    print(f"\n>>> 📤 BOT REPLY TO {phone}:")
    print("──────────────────────────────────────")
    print(message)
    print("──────────────────────────────────────\n")

def cleanup_expired_sessions():
    supabase.table('user_sessions') \
        .delete() \
        .lt('expires_at', 'now()') \
        .execute()

def get_user_profile(clean_phone):
    # Reconstruct raw WhatsApp ID
    raw_phone = f"whatsapp:{clean_phone}"
    user = supabase.table('users').select('*').eq('whatsapp_id', raw_phone).execute()
    if not user.data:
        raw_phone = f"whatsapp:{clean_phone}"  # reconstruct
        supabase.table('users').insert({
            'whatsapp_id': raw_phone,
            'phone_clean': clean_phone,
            'user_type': 'organic',
            'name': 'Unknown'
        }).execute()
        return {'whatsapp_id': raw_phone, 'phone_clean': clean_phone, 'user_type': 'organic', 'name': 'Unknown'}
    return user.data[0]

def normalize_phone(twilio_phone):
    """Convert 'whatsapp:+234...' → '+234...'"""
    if twilio_phone.startswith("whatsapp:"):
        return twilio_phone[len("whatsapp:"):]
    return twilio_phone

def get_user_organizers(sender):
    """Get all organizers the user is linked to, with active event count"""
    print(f">>> 🔍 Fetching organizers for raw WhatsApp ID: {sender}")
    result = supabase.table('user_organizers') \
        .select('organizer_id, organizers(name, refundable, contact_for_refunds)') \
        .eq('whatsapp_id', sender) \
        .execute()
    print(f">>> 📋 user_organizers result: {result.data}")

    if not result.data:
        return []
    
    orgs = []
    for row in result.data:
        # Count active events for this organizer
        events_count = supabase.table('events') \
            .select('id', count='exact') \
            .eq('organizer_id', row['organizer_id']) \
            .eq('status', 'active') \
            .eq('ticket_sales_open', True) \
            .gte('date', 'now()') \
            .execute()
        orgs.append({
            'id': row['organizer_id'],
            'name': row['organizers']['name'],
            'refundable': row['organizers']['refundable'],
            'contact_for_refunds': row['organizers']['contact_for_refunds'],
            'active_events_count': events_count.count
        })
    return orgs

def get_active_events_for_organizer(org_id):
    """Get events that are: active, sales open, not cancelled, future"""
    events = supabase.table('events') \
        .select('id, name, date, location') \
        .eq('organizer_id', org_id) \
        .eq('status', 'active') \
        .eq('ticket_sales_open', True) \
        .gte('date', 'now()') \
        .execute()
    return events.data or []

def generate_organizer_code(event_name: str, event_date: str) -> str:
    """
    Generate a human-readable, unique organizer code from event name and date.
    Example: "Lagos Jazz Fest 2025" → "JAZZ-FEST25"
    Ensures uniqueness in the 'organizers' table.
    """
    import re
    import secrets

    # Extract year from event_date (YYYY-MM-DD)
    try:
        year = event_date.split('-')[0]
        year_suffix = year[-2:]  # e.g., "25"
    except Exception:
        year_suffix = str(datetime.now().year)[-2:]

    # Clean and split event name
    clean_name = re.sub(r'[^A-Za-z0-9\s]', ' ', event_name)
    words = [w for w in clean_name.split() if w.isalpha() and len(w) >= 2]

    if not words:
        # Fallback if no valid words
        base_acronym = "EVENT"
    elif len(words) == 1:
        base_acronym = words[0].upper()[:6]
    else:
        # Take 2 most distinctive words (skip common ones like "the", "and", etc.)
        skip_words = {"the", "and", "or", "for", "of", "at", "in", "on", "to", "my", "our"}
        meaningful = [w for w in words if w.lower() not in skip_words]
        if len(meaningful) >= 2:
            w1, w2 = meaningful[0], meaningful[1]
        elif len(words) >= 2:
            w1, w2 = words[0], words[1]
        else:
            w1, w2 = words[0], words[0]
        base_acronym = f"{w1.upper()}-{w2.upper()}"

    # Truncate to reasonable length
    if '-' in base_acronym:
        parts = base_acronym.split('-')
        parts[0] = parts[0][:5]
        parts[1] = parts[1][:5]
        base_acronym = '-'.join(parts)
    else:
        base_acronym = base_acronym[:8]

    base_code = f"{base_acronym}{year_suffix}"

    # Ensure uniqueness: try base, then add A, B, C... or location hint
    for attempt in range(10):
        candidate = base_code
        if attempt > 0:
            # Append a letter (A, B, C...)
            candidate = f"{base_acronym}{year_suffix}{chr(65 + attempt - 1)}"

        # Check uniqueness
        existing = supabase.table('organizers').select('code').eq('code', candidate).execute()
        if not existing.data:
            return candidate

    # Final fallback: random suffix
    return f"{base_acronym}{year_suffix}{secrets.token_urlsafe(3).replace('_', '').replace('-', '').upper()[:3]}"

def show_events_for_organizer(org_id, msg):
    events = get_active_events_for_organizer(org_id)
    if not events:
        msg.body("📭 This organizer has no upcoming events with open ticket sales.")
        return

    reply = "🎉 *UPCOMING EVENTS*\n\n"
    for ev in events:
        tickets = supabase.table('ticket_types') \
            .select('name, price, available_quantity') \
            .eq('event_id', ev['id']) \
            .gt('available_quantity', 0) \
            .execute()
        if not tickets.data:
            continue
        reply += f"🎪 *{ev['name']}*\n📅 {ev['date']} | 📍 {ev['location']}\n"
        for t in tickets.data:
            reply += f"🎟️ {t['name']}: ₦{t['price']:,} ({t['available_quantity']} left)\n"
        reply += "\n"
    if reply == "🎉 *UPCOMING EVENTS*\n\n":
        reply = "📭 All events are sold out!"
    reply += "\n👉 Reply with:\n*TicketType Quantity*\nExample: `VIP 2`"
    msg.body(reply)

def handle_attend_command(raw_phone, org_code, msg):
    # Normalize to clean phone for user profile
    clean_phone = normalize_phone(raw_phone)

    org = supabase.table('organizers').select('*').eq('code', org_code).single().execute()
    if not org.data:
        msg.body("❌ Invalid organizer code. Please check and try again.")
        return

    # Check if user exists by clean_phone
    user = supabase.table('users').select('user_type').eq('phone_clean', clean_phone).execute()
    if not user.data:
        # Insert new user with BOTH raw and clean phone
        supabase.table('users').insert({
            'whatsapp_id': raw_phone,           # whatsapp:+234...
            'phone_clean': clean_phone,   # +234...
            'user_type': 'invited',
            'name': 'Unknown'
        }).execute()
    elif user.data[0]['user_type'] == 'organic':
        # Optionally upgrade to 'invited' — or leave as is
        # We'll leave it unchanged per your earlier logic
        pass

    # Link to organizer using RAW phone (because user_organizers.whatsapp_id = raw)
    supabase.table('user_organizers').upsert(
        {'whatsapp_id': raw_phone, 'organizer_id': org.data['id']},
        on_conflict='whatsapp_id,organizer_id'
    ).execute()

    welcome = org.data.get('welcome_message', 'Welcome! How can I help?')
    msg.body(f"🎉 *{org.data['name']}*\n{welcome}\nType 'events' to see available tickets.")
    if org.data.get('logo_url'):
        msg.media(org.data['logo_url'])

def send_ticket(whatsapp_id):
    tx = supabase.table('transactions') \
        .select('event_id, ticket_type_id') \
        .eq('whatsapp_id', whatsapp_id) \
        .eq('status', 'paid') \
        .order('created_at', desc=True) \
        .limit(1) \
        .execute()

    if not tx.data:
        print("❌ No paid transaction found for", whatsapp_id)
        return

    event_id = tx.data[0]['event_id']
    ticket_type_id = tx.data[0]['ticket_type_id']
    ticket_code = f"TKT-{uuid.uuid4().hex[:8].upper()}"
    
    supabase.table('tickets').insert({
        'whatsapp_id': whatsapp_id,
        'ticket_code': ticket_code,
        'event_id': event_id,
        'ticket_type_id': ticket_type_id,
        'quantity': 1,
        'status': 'issued'
    }).execute()

    supabase_service.table('user_carts').delete().eq('whatsapp_id', whatsapp_id).execute()
    print(f">>> 🧹 Cart deleted for {whatsapp_id}")

    qr_link = f"https://your-ngrok-url/verify/{ticket_code}"
    qr = qrcode.QRCode(box_size=5, border=2)
    qr.add_data(qr_link)
    qr.make(fit=True)
    img = qr.make_image(fill='black', back_color='white')
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    file_name = f"tickets/{ticket_code}.png"
    
    try:
        supabase_service.storage.from_("ticket-qr").upload(file_name, buf.getvalue())
        qr_url = supabase_service.storage.from_("ticket-qr").get_public_url(file_name)
        twilio_client.messages.create(
            from_=TWILIO_NUMBER,
            body="🎉 PAYMENT CONFIRMED!\nHere's your e-ticket. Show this QR at the event gate:",
            media_url=[qr_url],
            to=whatsapp_id
        )
        print(f"✅ Real ticket {ticket_code} sent to {whatsapp_id}")
    except Exception as e:
        print(f"❌ Failed to send real ticket: {str(e)}")

def cleanup_expired_carts():
    """Delete carts that have expired (including locked ones)"""
    supabase_service.table('user_carts') \
        .delete() \
        .lt('expires_at', 'now()') \
        .execute()


# Cache health for 60 seconds to avoid hammering APIs
@lru_cache(maxsize=2)
def is_flutterwave_healthy():
    try:
        resp = http_client.get(
            "https://api.flutterwave.com/v3/ping",
            headers={"Authorization": f"Bearer {os.getenv('FLW_SECRET_KEY')}"},
            timeout=5
        )
        return resp.status_code == 200 and resp.json().get("status") == "success"
    except Exception as e:
        print(f"⚠️ Flutterwave health check failed: {e}")
        return False

@lru_cache(maxsize=2)
def is_paystack_healthy():
    try:
        resp = http_client.get(
            "https://api.paystack.co/dedicated_account",
            headers={"Authorization": f"Bearer {os.getenv('PAYSTACK_SECRET_KEY')}"},
            timeout=5
        )
        # Paystack returns 401 on auth failure, but 200+ on healthy
        return resp.status_code < 500
    except Exception as e:
        print(f"⚠️ Paystack health check failed: {e}")
        return False

def clear_health_cache():
    is_flutterwave_healthy.cache_clear()
    is_paystack_healthy.cache_clear()


@app.route("/webhook", methods=['POST'])
def whatsapp_webhook():
    try: 
        incoming_msg = request.values.get('Body', '').strip()
        sender = request.values.get('From', '')
        print(f"\n>>> 📥 INCOMING MESSAGE: '{incoming_msg}' FROM: {sender}")
        resp = MessagingResponse()
        msg = resp.message()

        # Normalize phone
        whatsapp_id = sender

        raw_phone = request.values.get('From', '')
        clean_phone = normalize_phone(raw_phone)
        print(f">>> 📱 raw_phone: {raw_phone} | clean_phone: {clean_phone}")

        # Get user profile
        user = get_user_profile(clean_phone)
        user_orgs = get_user_organizers(raw_phone)
        print(f">>> 👤 User: {user.get('name', 'Unknown')} | Orgs count: {len(user_orgs)}")
        print(f">>> 🏢 Organizers: {[org['name'] for org in user_orgs]}")

        # ================================
        # REFUND REQUEST DETECTION
        # ================================
        refund_keywords = ['refund', 'return ticket', 'cancel my ticket', 'get money back', 'reimburse']
        if any(kw in incoming_msg.lower() for kw in refund_keywords):
            tx = supabase.table('transactions') \
                .select('organizer_id') \
                .eq('whatsapp_id', whatsapp_id) \
                .eq('status', 'paid') \
                .order('created_at', desc=True) \
                .limit(1) \
                .execute()
            if not tx.data:
                msg.body("❌ You haven’t purchased any tickets yet.")
            else:
                org = supabase.table('organizers') \
                    .select('name, refundable, contact_for_refunds') \
                    .eq('id', tx.data[0]['organizer_id']) \
                    .single() \
                    .execute()
                if org.data['refundable']:
                    contact = org.data.get('contact_for_refunds') or "the organizer directly"
                    msg.body(f"🎟️ Refunds for *{org.data['name']}* are handled manually.\n\nPlease contact them at: {contact}")
                else:
                    msg.body("🚫 Sorry, this event does not offer refunds.")
            return str(resp)

        # ================================
        # TICKET RESEND REQUEST
        # ================================
        if any(kw in incoming_msg.lower() for kw in ['my ticket', 'resend', 'send ticket', 'qr code']):
            send_ticket(whatsapp_id)
            return str(resp)

        # ================================
        # HANDLE "attend ORG-CODE"
        # ================================
        if incoming_msg.lower().startswith("attend "):
            org_code = incoming_msg.split(" ", 1)[1].strip().upper()
            handle_attend_command(whatsapp_id, org_code, msg)
            return str(resp)

        # ================================
        # NEW ORGANIC USER — SMART WELCOME + ONBOARDING
        # ================================
        onboard_triggers = ["i'm an organizer", "create event", "new event", "host event", "sell tickets"]

        if not user_orgs:
            # Check if user is in the middle of organizer onboarding
            session = supabase.table('user_sessions').select('*').eq('whatsapp_id', raw_phone).execute()
            
            if session.data:
                # Handle onboarding steps
                sess = session.data[0]
                step = sess['step']
                data = sess['data'] or {}

                incoming_lower = incoming_msg.lower().strip()

                # === HANDLE NAVIGATION COMMANDS FIRST (EARLY EXIT) ===
                if incoming_lower in ["back", "edit", "go back", "previous"]:
                    print(f">>> 🔄 USER ACTION: '{incoming_msg}' from {raw_phone} (current step: {step})")
                    if sess.get('previous_step'):
                        # Restore previous step
                        supabase.table('user_sessions').update({
                            'step': sess['previous_step'],
                            'previous_step': None  # optional: don't allow infinite back
                        }).eq('whatsapp_id', raw_phone).execute()
                        # Send prompt for previous step
                        if sess['previous_step'] == 'org_name':
                            reply_text = "What’s your *organizer name*?\n(e.g., Lagos Jazz Fest)"
                            msg.body(reply_text)
                            print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                            print("──────────────────────────────────────")
                            print(reply_text)
                            print("──────────────────────────────────────\n")
                            return str(resp)
                        elif sess['previous_step'] == 'event_name':
                            reply_text = "What’s your *event name*?\n(e.g., Summer Night Jazz)"
                            msg.body(reply_text)
                            print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                            print("──────────────────────────────────────")
                            print(reply_text)
                            print("──────────────────────────────────────\n")
                            return str(resp)
                        elif sess['previous_step'] == 'date':
                            reply_text = "When is the event? Please send the date in *YYYY-MM-DD* format."
                            msg.body(reply_text)
                            print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                            print("──────────────────────────────────────")
                            print(reply_text)
                            print("──────────────────────────────────────\n")
                            return str(resp)
                        elif sess['previous_step'] == 'location':
                            reply_text = "Where is the event happening?\n(e.g., Eko Hotel, Lagos)"
                            msg.body(reply_text)
                            print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                            print("──────────────────────────────────────")
                            print(reply_text)
                            print("──────────────────────────────────────\n")
                            return str(resp)
                        elif sess['previous_step'] == 'refundable':
                            reply_text = "Do you allow *refunds*? Reply:\n1. Yes\n2. No"
                            msg.body(reply_text)
                            print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                            print("──────────────────────────────────────")
                            print(reply_text)
                            print("──────────────────────────────────────\n")
                            return str(resp)
                        elif sess['previous_step'] == 'welcome_message':
                            reply_text = "Optional: Send a *welcome message* for your attendees (max 200 chars), or type `skip`."
                            msg.body(reply_text)
                            print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                            print("──────────────────────────────────────")
                            print(reply_text)
                            print("──────────────────────────────────────\n")
                            return str(resp)
                        else:
                            reply_text = "Let’s start over. What’s your *organizer name*?"
                            msg.body(reply_text)
                            print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                            print("──────────────────────────────────────")
                            print(reply_text)
                            print("──────────────────────────────────────\n")
                            supabase.table('user_sessions').update({'step': 'org_name'}).eq('whatsapp_id', raw_phone).execute()
                            return str(resp)
                    else:
                        # No previous step — restart from org_name
                        reply_text = "Let’s start over. What’s your *organizer name*?"
                        msg.body(reply_text)
                        print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                        print("──────────────────────────────────────")
                        print(reply_text)
                        print("──────────────────────────────────────\n")
                        supabase.table('user_sessions').update({
                            'step': 'org_name',
                            'previous_step': None,
                            'data': {}
                        }).eq('whatsapp_id', raw_phone).execute()
                        return str(resp)

                elif incoming_lower == "cancel":
                    print(f">>> 🚫 CANCEL REQUEST from {raw_phone} (current step: {step})")
                    supabase.table('user_sessions').delete().eq('whatsapp_id', raw_phone).execute()
                    reply_text = "✅ Onboarding cancelled. Reply `I'm an organizer` anytime to restart."
                    msg.body(reply_text)
                    print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                    print("──────────────────────────────────────")
                    print(reply_text)
                    print("──────────────────────────────────────\n")
                    return str(resp)

                # === ONLY NOW HANDLE STEP-SPECIFIC LOGIC ===
                if step == 'org_name':
                    data['org_name'] = incoming_msg.strip()
                    supabase.table('user_sessions').update({
                        'step': 'event_name',
                        'previous_step': 'org_name',
                        'data': data
                    }).eq('whatsapp_id', raw_phone).execute()
                    reply_text = "What’s your *event name*?\n(e.g., Summer Night Jazz)"
                    msg.body(reply_text)
                    print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                    print("──────────────────────────────────────")
                    print(reply_text)
                    print("──────────────────────────────────────\n")
                    return str(resp)

                elif step == 'event_name':
                    data['event_name'] = incoming_msg.strip()
                    supabase.table('user_sessions').update({
                        'step': 'date',
                        'previous_step': 'event_name',
                        'data': data
                    }).eq('whatsapp_id', raw_phone).execute()
                    reply_text = "When is the event? Please send the date in *YYYY-MM-DD* format."
                    msg.body(reply_text)
                    print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                    print("──────────────────────────────────────")
                    print(reply_text)
                    print("──────────────────────────────────────\n")
                    return str(resp)

                elif step == 'date':
                    if not re.match(r'^\d{4}-\d{2}-\d{2}$', incoming_msg.strip()):
                        reply_text = "❌ Invalid date format. Please use YYYY-MM-DD (e.g., 2025-08-15)"
                        msg.body(reply_text)
                        print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                        print("──────────────────────────────────────")
                        print(reply_text)
                        print("──────────────────────────────────────\n")
                        return str(resp)
                    try:
                        event_date = datetime.strptime(incoming_msg.strip(), "%Y-%m-%d").replace(tzinfo=pytz.utc)
                        if event_date < datetime.now(pytz.utc):
                            reply_text = "❌ Event date must be in the future. Try again."
                            msg.body(reply_text)
                            print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                            print("──────────────────────────────────────")
                            print(reply_text)
                            print("──────────────────────────────────────\n")
                            return str(resp)
                    except Exception:
                        reply_text = "❌ Invalid date. Please use YYYY-MM-DD."
                        msg.body(reply_text)
                        print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                        print("──────────────────────────────────────")
                        print(reply_text)
                        print("──────────────────────────────────────\n")
                        return str(resp)
                    data['date'] = incoming_msg.strip()
                    supabase.table('user_sessions').update({
                        'step': 'location',
                        'previous_step': 'date',
                        'data': data
                    }).eq('whatsapp_id', raw_phone).execute()
                    reply_text = "Where is the event happening?\n(e.g., Eko Hotel, Lagos)"
                    msg.body(reply_text)
                    print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                    print("──────────────────────────────────────")
                    print(reply_text)
                    print("──────────────────────────────────────\n")
                    return str(resp)

                elif step == 'location':
                    data['location'] = incoming_msg.strip()
                    supabase.table('user_sessions').update({
                        'step': 'refundable',
                        'previous_step': 'location',
                        'data': data
                    }).eq('whatsapp_id', raw_phone).execute()
                    reply_text = "Do you allow *refunds*? Reply:\n1. Yes\n2. No"
                    msg.body(reply_text)
                    print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                    print("──────────────────────────────────────")
                    print(reply_text)
                    print("──────────────────────────────────────\n")
                    return str(resp)

                elif step == 'refundable':
                    if incoming_msg.strip() in ["1", "Yes", "yes"]:
                        data['refundable'] = True
                    elif incoming_msg.strip() in ["2", "No", "no"]:
                        data['refundable'] = False
                    else:
                        reply_text = "Please reply 1 for Yes or 2 for No."
                        msg.body(reply_text)
                        print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                        print("──────────────────────────────────────")
                        print(reply_text)
                        print("──────────────────────────────────────\n")
                        return str(resp)
                    supabase.table('user_sessions').update({
                        'step': 'welcome_message',
                        'previous_step': 'refundable',
                        'data': data
                    }).eq('whatsapp_id', raw_phone).execute()
                    reply_text = "Optional: Send a *welcome message* for your attendees (max 200 chars), or type `skip`."
                    msg.body(reply_text)
                    print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                    print("──────────────────────────────────────")
                    print(reply_text)
                    print("──────────────────────────────────────\n")
                    return str(resp)

                elif step == 'welcome_message':
                    if incoming_msg.lower().strip() != "skip":
                        data['welcome_message'] = incoming_msg.strip()[:200]
                    else:
                        data['welcome_message'] = ""

                    # ✅ FINALIZE ORGANIZER CREATION
                    try:
                        code = generate_organizer_code(data['event_name'], data['date'])
                        org_res = supabase_service.table('organizers').insert({
                            'name': data['org_name'],
                            'code': code,
                            'welcome_message': data['welcome_message'],
                            'refundable': data['refundable'],
                            'contact_for_refunds': raw_phone
                        }).execute()
                        organizer_id = org_res.data[0]['id']
                        supabase_service.table('events').insert({
                            'organizer_id': organizer_id,
                            'name': data['event_name'],
                            'date': data['date'],
                            'location': data['location'],
                            'status': 'active',
                            'ticket_sales_open': False
                        }).execute()
                        supabase.table('user_organizers').upsert(
                            {'whatsapp_id': raw_phone, 'organizer_id': organizer_id},
                            on_conflict='whatsapp_id,organizer_id'
                        ).execute()
                        supabase.table('user_sessions').delete().eq('whatsapp_id', raw_phone).execute()

                        # === GENERATE QR CODE ===
                        twilio_wa_number = TWILIO_NUMBER.replace('whatsapp:', '').replace('+', '')
                        invite_link = f"https://wa.me/{twilio_wa_number}?text=attend%20{code}"
                        qr = qrcode.QRCode(box_size=8, border=2)
                        qr.add_data(invite_link)
                        qr.make(fit=True)
                        img = qr.make_image(fill='black', back_color='white')
                        buf = BytesIO()
                        img.save(buf, format="PNG")
                        buf.seek(0)

                        # Upload to Supabase Storage
                        qr_file_name = f"invite_qr/{code}.png"
                        try:
                            supabase_service.storage.from_("ticket-qr").upload(
                                qr_file_name,
                                buf.getvalue(),
                                file_options={"content-type": "image/png"}
                            )
                            qr_url = supabase_service.storage.from_("ticket-qr").get_public_url(qr_file_name)
                        except Exception as e:
                            print(f"⚠️ QR upload failed: {e}")
                            qr_url = None

                        # === SEND MESSAGE + QR ===
                        reply_text = (
                            f"🎉 *Your event is ready!* ✅\n"
                            f"Your organizer code: *{code}*\n\n"
                            f"📲 *Share this link with attendees:*\n"
                            f"{invite_link}\n\n"
                            f"✅ They’ll be able to buy tickets instantly!\n"
                            f"🛠️ Next: Go to your dashboard to add ticket types and open sales!"
                        )
                        msg.body(reply_text)
                        if qr_url:
                            msg.media(qr_url)  # Attach QR image

                        # Log to console
                        print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                        print("──────────────────────────────────────")
                        print(reply_text)
                        if qr_url:
                            print(f"🖼️ QR URL: {qr_url}")
                        print("──────────────────────────────────────\n")

                        return str(resp)

                    except Exception as e:
                        print(f"❌ Onboarding save error: {e}")
                        supabase.table('user_sessions').delete().eq('whatsapp_id', raw_phone).execute()
                        reply_text = "❌ Sorry, we couldn’t create your event. Please try again."
                        msg.body(reply_text)
                        print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                        print("──────────────────────────────────────")
                        print(reply_text)
                        print("──────────────────────────────────────\n")
                        return str(resp)

            # Not in onboarding → show initial choice
            if any(trigger in incoming_msg.lower() for trigger in onboard_triggers):
                supabase.table('user_sessions').upsert({
                    'whatsapp_id': raw_phone,
                    'step': 'org_name',
                    'data': {},
                    'expires_at': (datetime.now(pytz.utc) + timedelta(minutes=30)).isoformat()
                }).execute()
                reply_text = "Great! Let’s set up your event 🎪\n\nWhat’s your *organizer name*?\n(e.g., Lagos Jazz Fest)"
                msg.body(reply_text)
                print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
                print("──────────────────────────────────────")
                print(reply_text)
                print("──────────────────────────────────────\n")
                return str(resp)

            # Define the new welcome message
            welcome_text = (
                "👋 Welcome to *TicketBot*!\n\n"
                "Are you here to:\n"
                "🎟️ *Buy tickets*? → Type: `attend ORG-CODE`\n"
                "🎪 *Create & sell tickets*? → Reply: `I'm an organizer`"
            )
            msg.body(welcome_text)
            # ✅ LOG OUTGOING MESSAGE TO CONSOLE
            print(f"\n>>> 📤 OUTGOING MESSAGE to {raw_phone}:\n{welcome_text}\n")
            return str(resp)

        # ================================
        # MULTIPLE ORGANIZERS → ASK TO CHOOSE
        # ================================
        if len(user_orgs) > 1 and not any(ev['active_events_count'] > 0 for ev in user_orgs):
            msg.body("📭 None of your organizers have upcoming events right now.")
            return str(resp)

        if len(user_orgs) > 1:
            # Check if message is a ticket selection (e.g., "VIP 2")
            ticket_selection_pattern = r'^[A-Za-z]+\s+\d+$'
            if re.match(ticket_selection_pattern, incoming_msg):
                # Try to process with last session? Or ask for organizer first.
                # For safety: require organizer context.
                reply = "You’re linked to multiple organizers. Please specify which event you’re buying for:\n"
                for i, org in enumerate(user_orgs, 1):
                    status = f" ({org['active_events_count']} active)" if org['active_events_count'] > 0 else " (no active events)"
                    reply += f"{i}. {org['name']}{status}\n"
                reply += "\nOr type an event code: attend ORG-CODE"
                msg.body(reply)
                return str(resp)

        # ================================
        # SINGLE ORGANIZER CONTEXT
        # ================================
        # If only one org, or user typed "events", proceed
        selected_org = None
        if len(user_orgs) == 1:
            selected_org = user_orgs[0]
        elif incoming_msg.lower().strip() == "events":
            # If multiple, this shouldn't happen—but fallback to first with events
            for org in user_orgs:
                if org['active_events_count'] > 0:
                    selected_org = org
                    break
            if not selected_org:
                selected_org = user_orgs[0]

        if not selected_org:
            # Should not happen, but safe guard
            msg.body("❓ Please specify which organizer you’d like to interact with.")
            return str(resp)

        # ================================
        # CANCEL PAYMENT COMMAND
        # ================================
        if incoming_msg.lower().strip() == "cancel":
            deleted = supabase_service.table('user_carts') \
                .delete() \
                .eq('whatsapp_id', whatsapp_id) \
                .eq('locked', 'true') \
                .execute()
            if deleted.data:
                msg.body("✅ Your payment attempt was cancelled. You can now start a new purchase.")
            else:
                msg.body("ℹ️ No active payment to cancel.")
            return str(resp)

         # ================================
        # TICKET PURCHASE FLOW (UNCHANGED CORE)
        # ================================
        if " " in incoming_msg and not incoming_msg.lower().startswith("attend "):
            print(f">>> 🧪 ENTERED TICKET PURCHASE FLOW with: '{incoming_msg}'")
            parts = incoming_msg.split(" ", 1)
            ticket_type_name = parts[0].strip()
            try:
                quantity = int(parts[1].strip())
                if quantity < 1:
                    raise ValueError
            except (ValueError, IndexError):
                msg.body("❌ Invalid format. Example: VIP 2")
                return str(resp)

            # === Generate UTC timestamps with Supabase-compatible format ===
            now_utc = datetime.now(pytz.utc)
            expires_at_utc = now_utc + timedelta(minutes=20)
            current_time_iso = now_utc.isoformat()
            expires_at_iso = expires_at_utc.isoformat()

            # 🔒 Check for existing locked cart — with context and abort hint
            existing_locked_cart = supabase_service.table('user_carts') \
                .select('event_id, ticket_type_id, quantity') \
                .eq('whatsapp_id', whatsapp_id) \
                .eq('locked', 'true') \
                .execute()

            if existing_locked_cart.data and len(existing_locked_cart.data) > 0:
                cart = existing_locked_cart.data[0]
                
                # Fetch event name
                event = supabase.table('events').select('name').eq('id', cart['event_id']).single().execute()
                event_name = event.data['name'] if event.data else "Unknown Event"
                
                # Fetch ticket type name
                ticket_type = supabase.table('ticket_types').select('name').eq('id', cart['ticket_type_id']).single().execute()
                ticket_name = ticket_type.data['name'] if ticket_type.data else "Unknown Ticket"
                
                quantity = cart['quantity']
                
                msg.body(
                    f"⏳ You’re already buying {quantity}x {ticket_name} for *{event_name}*.\n"
                    "Please complete your previous payment or reply *CANCEL* to abort and start over."
                )
                return str(resp)

            # Check if event is still open
            events = get_active_events_for_organizer(selected_org['id'])
            if not events:
                msg.body("❌ Ticket sales are closed for all events by this organizer.")
                return str(resp)

            # Find ticket type across active events
            ticket = None
            event_found = None
            for ev in events:
                tkt = supabase.table('ticket_types') \
                    .select('id, name, price, event_id, available_quantity') \
                    .eq('name', ticket_type_name) \
                    .eq('event_id', ev['id']) \
                    .gte('available_quantity', quantity) \
                    .single() \
                    .execute()
                if tkt.data:
                    ticket = tkt.data
                    event_found = ev
                    break

            if not ticket:
                msg.body(f"❌ '{ticket_type_name}' not found or sold out. Type 'events' to see options.")
                return str(resp)


            # 💾 Create new LOCKED cart with expiry
            cart_data = {
                'whatsapp_id': whatsapp_id,
                'event_id': ticket['event_id'],
                'ticket_type_id': ticket['id'],
                'quantity': quantity,
                'locked': 'true',
                'expires_at': expires_at_iso
            }

            try:
                cart_insert = supabase_service.table('user_carts').insert(cart_data).execute()
                print(f"✅ Locked cart created for {whatsapp_id} (expires at {expires_at_iso})")
            except Exception as e:
                print(f"❌ FAILED to save locked cart for {whatsapp_id}: {str(e)}")
                msg.body("❌ Sorry, we couldn't reserve your ticket. Please try again.")
                return str(resp)
                
            

            total = ticket['price'] * quantity
                        # 🔍 Check gateway health (clear cache to get fresh status)
            clear_health_cache()
            flw_ok = is_flutterwave_healthy()
            psk_ok = is_paystack_healthy()

            options = []
            if flw_ok:
                options.append("1. Flutterwave")
            if psk_ok:
                options.append("2. Paystack")

            if not options:
                msg.body("🚫 All payment services are temporarily unavailable. Please try again later.")
                # Unlock cart by deleting it
                supabase_service.table('user_carts').delete().eq('whatsapp_id', whatsapp_id).execute()
                return str(resp)

            option_text = "\n".join(options)
            reply = f"✅ {quantity}x {ticket['name']} for '{event_found['name']}'"
            reply += f"💰 Total: ₦{total:,}"
            reply += f"💳 Pay with:\n{option_text}\nReply with the number."
            msg.body(reply)
            print(f">>> 💬 PAYMENT MESSAGE: {reply}")
            return str(resp)

        # ================================
        # PAYMENT METHOD SELECTION (1 or 2)
        # ================================
        elif incoming_msg.strip() in ["1", "2"]:
            print(f">>> 💳 PAYMENT SELECTION: '{incoming_msg}' by {whatsapp_id}")

            # DEBUG: List ALL carts for this user
            print(f">>> 🧪 PAYMENT DEBUG")
            print(f">>> 📞 whatsapp_id (repr): {repr(whatsapp_id)}")
            try:
                probe = httpx.get(
                    f"{SUPABASE_URL}/rest/v1/user_carts",
                    headers={
                        "apikey": SUPABASE_SERVICE_ROLE_KEY,
                        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                    },
                    timeout=10
                )
                print("🔍 Probe to Supabase:", probe.status_code, probe.text[:200])
            except Exception as e:
                print("❌ Probe error:", e)

            all_carts = supabase_service.table('user_carts').select('*').eq('whatsapp_id', whatsapp_id).execute()
            print(f">>> 🔍 ALL carts for {whatsapp_id}: {all_carts.data}")

            try:
                cart_resp = supabase_service.table('user_carts') \
                    .select('event_id, ticket_type_id, quantity') \
                    .eq('whatsapp_id', whatsapp_id) \
                    .order('created_at', desc=True) \
                    .limit(1) \
                    .execute()
                print(f">>> 🛒 Cart query result: {cart_resp.data if cart_resp else 'None'}")
            except PostgrestAPIError as e:
                print(f">>> ❌ Cart query failed: {e}")
                cart_resp = type("EmptyResp", (), {"data": None})

            if not cart_resp.data:
                # 🔓 Unlock cart
                supabase_service.table('user_carts').update({'locked': 'false'}).eq('whatsapp_id', whatsapp_id).execute()
                msg.body("❌ No ticket selected. Please select a ticket first.")
                return str(resp)

            cart_data = cart_resp.data[0]
            event_id = cart_data['event_id']
            ticket_type_id = cart_data['ticket_type_id']
            quantity = cart_data['quantity']
            print(f">>> 🎟️ Cart loaded: event={event_id}, ticket_type={ticket_type_id}, qty={quantity}")

            # 🔐 FETCH TICKET PRICE VIA DIRECT REST (using hardened http_client to avoid SSL bug)
            ticket_type_url = f"{SUPABASE_URL}/rest/v1/ticket_types?id=eq.{ticket_type_id}"
            headers = {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Accept": "application/json"
            }
            try:
                resp_price = http_client.get(ticket_type_url, headers=headers, timeout=10)
                if resp_price.status_code == 200:
                    data = resp_price.json()
                    if data and len(data) == 1:
                        price = data[0]['price']
                        amount = price * quantity
                        print(f">>> 💰 Price: ₦{price}, Total: ₦{amount}")
                    else:
                        raise Exception("Ticket type not found or multiple matches")
                else:
                    raise Exception(f"REST error {resp_price.status_code}: {resp_price.text[:100]}")
            except Exception as e:
                print(f">>> ❌ Failed to fetch ticket price via REST: {e}")
                # 🔓 Unlock cart
                supabase_service.table('user_carts').update({'locked': False}).eq('whatsapp_id', whatsapp_id).execute()
                msg.body("❌ Error fetching ticket details.")
                return str(resp)

            phone = whatsapp_id.replace("whatsapp:", "")
            email = f"user_{phone.lstrip('+')}@example.com"

            # Determine initial method
            initial_method = "flutterwave" if incoming_msg.strip() == "1" else "paystack"
            method = initial_method
            print(f">>> 🏦 Initial payment method: {method}")

            # === PAYMENT LINK GENERATION WITH FALLBACK ===
            payment_url = None
            gateway = None
            tx_ref = None
            reference = None

            # Try primary method
            if method == "flutterwave":
                tx_ref = f"FLW-{secrets.token_hex(8)}"
                result = generate_payment_link_flw(amount, phone, email, tx_ref)
                print(f">>> 🔄 Flutterwave API response: {result}")
                if isinstance(result, dict) and result.get("status") == "success":
                    payment_url = result["data"]["link"]
                    gateway = "flutterwave"
                else:
                    print(">>> ⚠️ Flutterwave failed — attempting Paystack fallback")
                    method = "paystack"

            if method == "paystack" and not payment_url:
                reference = f"PSK-{secrets.token_hex(8)}"
                result = generate_payment_link_paystack(amount, email, reference)
                print(f">>> 🔄 Paystack API response: {result}")
                if isinstance(result, dict) and result.get("status") is True:
                    payment_url = result["data"]["authorization_url"]
                    gateway = "paystack"
                    tx_ref = reference
                else:
                    print(">>> ❌ Both gateways failed")

            # Handle total failure
            if not payment_url:
                # 🔓 Unlock cart
                supabase_service.table('user_carts').update({'locked': False}).eq('whatsapp_id', whatsapp_id).execute()
                msg.body("❌ Payment services are currently unavailable. Please try again later.")
                return str(resp)

            print(f">>> 🔗 PAYMENT LINK GENERATED: {payment_url}")

            # Re-fetch user organizers
            user_orgs_for_payment = get_user_organizers(raw_phone)
            if not user_orgs_for_payment:
                # 🔓 Unlock cart
                supabase_service.table('user_carts').update({'locked': False}).eq('whatsapp_id', whatsapp_id).execute()
                msg.body("❌ Organizer not found. Please start over with 'events'.")
                return str(resp)

            if len(user_orgs_for_payment) == 1:
                payment_org = user_orgs_for_payment[0]
            else:
                payment_org = next((org for org in user_orgs_for_payment if org['active_events_count'] > 0), user_orgs_for_payment[0])

            organizer_id = payment_org['id']

            # Save transaction
            try:
                supabase_service.table('transactions').insert({
                    'whatsapp_id': whatsapp_id,
                    'organizer_id': organizer_id,
                    'event_id': event_id,
                    'ticket_type_id': ticket_type_id,
                    'amount': amount,
                    'payment_gateway': gateway,
                    'payment_ref': tx_ref,
                    'status': 'pending'
                }).execute()
                print(">>> 📥 Transaction saved")
            except Exception as e:
                print(f">>> ❌ Failed to save transaction: {e}")
                # Note: We leave cart locked — user can retry or it will be cleaned up later

            # Send message
            if method != initial_method:
                msg.body(f"💳 We switched to {gateway.upper()} for reliability.\nPay ₦{amount:,}:\n{payment_url}\nAfter payment, wait for your ticket!")
            else:
                msg.body(f"💳 Pay ₦{amount:,} via {gateway.upper()}:\n{payment_url}\nAfter payment, wait for your ticket!")

            return str(resp)

        # ================================
        # DEFAULT: SHOW EVENTS OR HELP
        # ================================
        if incoming_msg.lower().strip() == "events":
            show_events_for_organizer(selected_org['id'], msg)
        else:
            msg.body(f"✅ You're connected to *{selected_org['name']}*.\nType 'events' to see tickets, or 'my ticket' to resend.")

        # DEBUG: Log outgoing message to console
        # DEBUG: Log outgoing message to console — ENHANCED
        # ✅ ALWAYS LOG THE BOT'S REPLY TEXT TO CONSOLE (even if Twilio fails)
                # ✅ ALWAYS LOG THE BOT'S REPLY TEXT TO CONSOLE
        twiml_output = str(resp)
        try:
            start = twiml_output.find("<Body>") + 6
            end = twiml_output.find("</Body>")
            if start >= 6 and end > start:
                body_text = twiml_output[start:end]
                # Unescape common entities
                body_text = (
                    body_text
                    .replace("&#xA;", "\n")
                    .replace("&amp;", "&")
                    .replace("<", "<")
                    .replace(">", ">")
                )
            else:
                body_text = "(no Body found in TwiML)"
        except Exception:
            body_text = "(failed to extract body)"

        print(f"\n>>> 📤 BOT REPLY TO {raw_phone}:")
        print("──────────────────────────────────────")
        print(body_text)
        print("──────────────────────────────────────\n")

        return str(resp)

    except Exception as e:
        # 🔥 CRITICAL ERROR HANDLER (outer try/except)
        print("🔥 CRITICAL ERROR IN WEBHOOK:")
        import traceback
        traceback.print_exc()
        try:
            fallback_msg = "❌ Sorry, something went wrong. We're fixing it! Try again shortly"
            print(f"\n>>> 📤 FALLBACK BOT REPLY (due to error):")
            print("──────────────────────────────────────")
            print(fallback_msg)
            print("──────────────────────────────────────\n")
        except:
            pass
        resp = MessagingResponse()
        resp.message("❌ Sorry, something went wrong. We're fixing it! Try again shortly!")
        return str(resp)

def update_transaction_with_retry(payment_ref, update_data, max_retries=3, backoff=0.5):
    """
    Attempt to update via supabase client first (normal path).
    On httpx.ReadError (TLS/httpcore read errors) retry a few times.
    If still failing, fallback to direct REST PATCH using requests.
    Returns the result-like object (mimics supabase .execute() where possible),
    or raises the last exception.
    """
    last_exc = None

    # 1) Try the normal supabase path with retries
    for attempt in range(1, max_retries + 1):
        try:
            result = supabase.table('transactions') \
                .update(update_data) \
                .eq('payment_ref', payment_ref) \
                .execute()
            return result
        except Exception as e:
            last_exc = e
            # specifically retry on httpx ReadError / network/TLS anomalies
            if isinstance(e, httpx.ReadError) or 'SSLV3_ALERT_BAD_RECORD_MAC' in str(e) or 'httpcore.ReadError' in str(type(e)):
                wait = backoff * (2 ** (attempt - 1))
                print(f"⚠️ transient network/TLS error on attempt {attempt}, retrying in {wait}s: {e}")
                time.sleep(wait)
                continue
            # non-transient: re-raise immediately
            print("❌ non-transient error updating via supabase client:", e)
            raise

    # 2) Fallback: use direct REST (requests) call to Supabase REST endpoint
    try:
        print("➡️ Fallback: using direct REST PATCH to Supabase")
        supabase_url = os.getenv("SUPABASE_URL").rstrip("/")
        rest_table = "transactions"
        # Build filter for payment_ref eq.<value>
        # Note: use URL-encoded filter
        filter_q = f"payment_ref=eq.{requests.utils.quote(payment_ref, safe='')}"
        patch_url = f"{supabase_url}/rest/v1/{rest_table}?{filter_q}"

        # Use the Service Role key for update privileges
        svc_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
        if not svc_key:
            raise RuntimeError("No SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY set for fallback REST update")

        headers = {
            "apikey": svc_key,
            "Authorization": f"Bearer {svc_key}",
            "Content-Type": "application/json",
            # ask PostgREST to return the updated rows like supabase client does
            "Prefer": "return=representation"
        }

        resp = requests.patch(patch_url, json=update_data, headers=headers, timeout=10)
        if resp.status_code in (200, 201, 204):
            try:
                data = resp.json()
            except Exception:
                data = resp.text
            # Build a minimal object to mimic supabase response
            return type("Resp", (), {"status_code": resp.status_code, "data": data})
        else:
            raise RuntimeError(f"Fallback REST update failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print("❌ Fallback update also failed:", str(e))
        # raise the last meaningful exception
        raise last_exc or e

@app.route('/payment-callback', methods=['POST'])
def payment_callback():
    try:
        print("\n>>> 🚨 WEBHOOK RECEIVED — START DEBUG")
        print(">>> RAW PAYLOAD (bytes):", request.data)
        print(">>> RAW PAYLOAD (text):", request.get_data(as_text=True))
        print(">>> CONTENT TYPE:", request.content_type)
        print(">>> HEADERS:", dict(request.headers))
        # Parse payload
        data = request.get_json(silent=True) or request.form.to_dict()
        print(">>> PARSED DATA:", data)
        print(">>> DATA TYPE:", type(data))
        print(">>> KEYS IN DATA:", list(data.keys()) if isinstance(data, dict) else "NOT DICT")
        if not data:
            print("❌ Empty payload")
            return "Empty payload", 400
        payment_ref = None
        status = None
        gateway = None

        # 🔹 PAYSTACK: detect by event == "charge.success" AND data.reference
        if isinstance(data, dict) and data.get("event") == "charge.success" and data.get("data", {}).get("reference"):
            print(">>> 🟢 Processing Paystack webhook")
            payment_ref = data["data"]["reference"]
            gateway = "paystack"
            # Verify signature FIRST
            signature = request.headers.get('X-Paystack-Signature')
            print(">>> PAYSTACK SIGNATURE HEADER:", signature)
            if not verify_paystack_signature(request.data, signature):
                print("❌ Invalid Paystack signature")
                return "Invalid signature", 400
            # Verify with Paystack API using http_client (to avoid timeout/SSL issues)
            verify_url = f"https://api.paystack.co/transaction/verify/{payment_ref}"
            headers = {"Authorization": f"Bearer {os.getenv('PAYSTACK_SECRET_KEY')}"}
            try:
                verify_resp = http_client.get(verify_url, headers=headers).json()
            except Exception as e:
                print(f"❌ Paystack verification failed: {e}")
                verify_resp = {"status": False}
            print(">>> PAYSTACK VERIFY RESPONSE:", verify_resp)
            if verify_resp.get('status') is True and verify_resp['data']['status'] == 'success':
                status = 'successful'
            else:
                status = 'failed'

        # 🔹 FLUTTERWAVE: detect by known event types AND data.id
        elif isinstance(data, dict):
            event = data.get("event") or data.get("event.type")
            payment_data = data.get("data")
            if event in ["charge.completed", "BANK_TRANSFER_TRANSACTION"] and payment_data and payment_data.get("id"):
                print(">>> 🟢 Processing Flutterwave webhook")
                tx_ref = payment_data.get("tx_ref")
                transaction_id = payment_data.get("id")
                flw_ref = payment_data.get("flw_ref")
                amount = payment_data.get("amount")
                print(f">>> FLW WEBHOOK tx_ref={tx_ref}, flw_ref={flw_ref}, amount={amount}, id={transaction_id}")
                if not transaction_id:
                    print("❌ Missing transaction_id in Flutterwave webhook")
                    return "Missing transaction_id", 400
                # Verify with Flutterwave API using http_client
                verify_url = f"https://api.flutterwave.com/v3/transactions/{transaction_id}/verify"
                headers = {"Authorization": f"Bearer {os.getenv('FLW_SECRET_KEY')}"}
                try:
                    verify_resp = http_client.get(verify_url, headers=headers).json()
                except Exception as e:
                    print(f"❌ Flutterwave verification failed: {e}")
                    verify_resp = {"status": "error"}
                print(">>> FLUTTERWAVE VERIFY RESPONSE:", verify_resp)
                if (
                    verify_resp.get("status") == "success"
                    and verify_resp["data"]["status"] == "successful"
                ):
                    status = "successful"
                else:
                    status = "failed"
                payment_ref = tx_ref  # ✅ CRITICAL: set payment_ref for update
                gateway = "flutterwave"

        else:
            print("❌ Unknown payload format")
            return "Unknown payload", 400

        # Update transaction
        print(">>> UPDATING TRANSACTION:", payment_ref, "with status:", status)
        if payment_ref is None:
            print("❌ payment_ref is None — cannot update")
            return "Missing payment reference", 400

        update_data = {
            'status': 'paid' if status in ['successful', 'success'] else 'failed',
            'updated_at': 'now()'
        }
        # Use the resilient updater
        try:
            result = update_transaction_with_retry(payment_ref, update_data)
        except Exception as e:
            print("❌ Final failure updating transaction:", str(e))
            import traceback; traceback.print_exc()
            return "Internal error", 500
        print(">>> UPDATE RESULT:", result)
        if result.data and update_data['status'] == 'paid':
            # Fetch transaction + whatsapp_id
            tx = supabase.table('transactions') \
                .select('whatsapp_id') \
                .eq('payment_ref', payment_ref) \
                .single() \
                .execute()
            if tx.data:
                print(">>> ISSUING TICKET TO:", tx.data['whatsapp_id'])
                send_ticket(tx.data['whatsapp_id'])
                return "Ticket issued", 200
            else:
                print("❌ No transaction found for payment_ref:", payment_ref)
                return "Transaction not found", 404
        else:
            print("❌ Payment not successful or update failed")
            return "Payment not successful", 400
    except Exception as e:
        print("❌ WEBHOOK ERROR:", str(e))
        import traceback
        traceback.print_exc()
        return "Internal error", 500


def verify_paystack_signature(payload_body, signature_header):
    if not signature_header:
        print("❌ No signature header received")
        return False

    secret = os.getenv('PAYSTACK_SECRET_KEY')
    if not secret:
        print("❌ PAYSTACK_SECRET_KEY not set in .env")
        return False

    # Compute expected signature
    expected_signature = hmac.new(
        secret.encode('utf-8'),
        payload_body,
        hashlib.sha512
    ).hexdigest()

    # ✅ LOG BOTH SIGNATURES
    print("\n>>> 🔑 PAYSTACK SIGNATURE DEBUG:")
    print(">>> SECRET KEY USED:", secret[:5] + "..." if secret else "NONE")
    print(">>> COMPUTED SIGNATURE:", expected_signature)
    print(">>> RECEIVED SIGNATURE:", signature_header)
    print(">>> MATCHES:", hmac.compare_digest(expected_signature, signature_header), "\n")

    # Compare securely
    return hmac.compare_digest(expected_signature, signature_header)



def generate_payment_link_flw(amount, phone, email="customer@example.com", tx_ref=None):
    if not tx_ref:
        tx_ref = f"FLW-{secrets.token_hex(8)}"
    
    url = "https://api.flutterwave.com/v3/payments"
    headers = {
        "Authorization": f"Bearer {os.getenv('FLW_SECRET_KEY')}",
        "Content-Type": "application/json"
    }
    data = {
        "tx_ref": tx_ref,
        "amount": int(amount),
        "currency": "NGN",
        "redirect_url": "https://46e92286d210.ngrok-free.app/payment-redirect",
        "payment_options": "card,ussd,mobilemoney,qr",
        "customer": {
            "email": email,
            "phone_number": phone,
            "name": "Customer"
        },
        "customizations": {
            "title": "Event Ticket Payment",
            "description": "Pay for your event ticket"
        }
    }

    # ✅ Use httpx instead of requests
    response = http_client.post(url, json=data, headers=headers)
    return response.json()


def generate_payment_link_paystack(amount, email="customer@example.com", reference=None):
    if not reference:
        reference = f"PSK-{secrets.token_hex(8)}"
    
    url = "https://api.paystack.co/transaction/initialize"
    headers = {
        "Authorization": f"Bearer {os.getenv('PAYSTACK_SECRET_KEY')}",
        "Content-Type": "application/json"
    }
    data = {
        "email": email,
        "amount": int(amount * 100),  # Paystack uses kobo
        "reference": reference,
        "redirect_url": "https://46e92286d210.ngrok-free.app/payment-redirect"
    }
    response = requests.post(url, json=data, headers=headers)
    return response.json()

# -------------------------------
# Gate Validation: Show Ticket
# -------------------------------
@app.route('/verify/<ticket_code>')
def verify_ticket(ticket_code):
    # Fetch ticket + event + ticket type + BUYER NAME
    ticket = supabase.table('tickets') \
        .select('*, event:events(name, date, location), ticket_type:ticket_types(name), buyer:users(name)') \
        .eq('ticket_code', ticket_code) \
        .single() \
        .execute()

    if not ticket.data:
        return render_template('gate/invalid.html', code=ticket_code)

    return render_template('gate/verify.html', ticket=ticket.data)

# -------------------------------
# Gate Validation: Scan Ticket
# -------------------------------
@app.route('/scan/<ticket_code>', methods=['POST'])
def scan_ticket(ticket_code):
    staff_name = request.form.get('staff_name', 'unknown').strip()
    if not staff_name:
        staff_name = 'unknown'

    # Update ticket
    result = supabase.table('tickets') \
        .update({
            'status': 'scanned',
            'scanned_at': 'now()',
            'scanned_by': staff_name
        }) \
        .eq('ticket_code', ticket_code) \
        .execute()

    if result.data:
        return render_template('gate/success.html')
    else:
        return "<h2 style='color:red;text-align:center'>❌ Failed to update. Try again.</h2>", 400


# -------------------------------
# QR Code Generator Endpoint (for testing)
# -------------------------------
@app.route("/generate_qr/<org_code>")
def generate_invite_qr(org_code):
    invite_text = f"attend {org_code}"
    wa_link = f"https://wa.me/14155238886?text={invite_text.replace(' ', '%20')}"

    qr = qrcode.QRCode(box_size=10, border=4)
    qr.add_data(wa_link)
    qr.make(fit=True)
    img = qr.make_image(fill='black', back_color='white')

    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)

    # Upload to Supabase Storage (optional)
    # For now, just return image
    from flask import send_file
    return send_file(buffer, mimetype='image/png', as_attachment=False, download_name=f'{org_code}.png')

@app.route('/payment-redirect')
def payment_redirect():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial, sans-serif; text-align: center; padding: 50px; background: #f0f8ff; }
            .card { background: white; border-radius: 12px; padding: 30px; box-shadow: 0 4px 20px rgba(0,0,0,0.1); max-width: 500px; margin: 0 auto; }
            h1 { color: #28a745; }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>🎉 Thank You!</h1>
            <p>Your payment was successful.</p>
            <p>Your e-ticket will arrive on WhatsApp within 60 seconds.</p>
            <p>Don’t close this page — we’re preparing your ticket!</p>
        </div>
    </body>
    </html>
    """
    resp = make_response(html)
    resp.headers['ngrok-skip-browser-warning'] = 'true'  # ✅ THIS IS CRITICAL
    return resp


@app.route('/test-flutterwave')
def test_flutterwave():
    test_result = generate_payment_link_flw(100, "+2348012345678", "test@example.com", "TEST-REF")
    
    # Print to console
    print("\n>>> FLUTTERWAVE RESPONSE:")
    print(json.dumps(test_result, indent=2))
    
    # Return formatted JSON to browser
    return test_result

def is_admin_request():
    """Simple admin auth via ?secret=ADMIN_SECRET"""
    return request.args.get('secret') == os.getenv('ADMIN_SECRET')

def is_admin_request():
    """Simple admin auth via ?secret=ADMIN_SECRET"""
    return request.args.get('secret') == os.getenv('ADMIN_SECRET')

@app.route('/admin')
def admin_dashboard():
    if not is_admin_request():
        return "🔒 Access denied. Use ?secret=YOUR_ADMIN_SECRET", 403

    try:
        # --- Counts ---
        users = supabase.table('users').select('whatsapp_id', count='exact').execute().count
        organizers = supabase.table('organizers').select('id', count='exact').execute().count
        events = supabase.table('events').select('id', count='exact').execute().count
        tickets_issued = supabase.table('tickets').select('ticket_code', count='exact').execute().count

        # --- Failed Transactions ---
        failed_tx = supabase_service.table('transactions') \
            .select('whatsapp_id, event_id, organizer_id, amount, payment_gateway, payment_ref, created_at') \
            .eq('status', 'failed') \
            .order('created_at', desc=True) \
            .limit(20) \
            .execute()

        whatsapp_ids = [tx['whatsapp_id'] for tx in failed_tx.data or []]
        event_ids = list({tx['event_id'] for tx in failed_tx.data or [] if tx.get('event_id')})
        org_ids = list({tx['organizer_id'] for tx in failed_tx.data or [] if tx.get('organizer_id')})

        # Batch fetch related data
        users_map = {}
        if whatsapp_ids:
            users_res = supabase.table('users').select('whatsapp_id, name, phone_clean').in_('whatsapp_id', whatsapp_ids).execute()
            users_map = {u['whatsapp_id']: u for u in users_res.data or []}

        events_map = {}
        if event_ids:
            events_res = supabase.table('events').select('id, name').in_('id', event_ids).execute()
            events_map = {e['id']: e['name'] for e in events_res.data or []}

        orgs_map = {}
        if org_ids:
            orgs_res = supabase.table('organizers').select('id, name').in_('id', org_ids).execute()
            orgs_map = {o['id']: o['name'] for o in orgs_res.data or []}

        # Enrich failed transactions
        enriched_failed = []
        for tx in failed_tx.data or []:
            user = users_map.get(tx['whatsapp_id'], {})
            enriched_failed.append({
                "user_name": user.get('name', 'Unknown'),
                "user_phone_clean": user.get('phone_clean', 'N/A'),
                "event_name": events_map.get(tx['event_id'], 'N/A'),
                "organizer_name": orgs_map.get(tx['organizer_id'], 'N/A'),
                "amount": tx.get('amount', 0),
                "payment_gateway": tx.get('payment_gateway', '').upper(),
                "payment_ref": (tx.get('payment_ref', '')[:12] + "...") if len(tx.get('payment_ref', '')) > 12 else tx.get('payment_ref', ''),
                "created_at": tx.get('created_at', '')[:16]
            })

        # --- Recent Tickets ---
        recent_tickets = supabase_service.table('tickets') \
            .select('ticket_code, status, created_at, whatsapp_id, event_id') \
            .order('created_at', desc=True) \
            .limit(10) \
            .execute()

        ticket_phones = [t['whatsapp_id'] for t in recent_tickets.data or []]
        ticket_events = list({t['event_id'] for t in recent_tickets.data or [] if t.get('event_id')})

        ticket_users_map = {}
        if ticket_phones:
            u_res = supabase.table('users').select('whatsapp_id, name').in_('whatsapp_id', ticket_phones).execute()
            ticket_users_map = {u['whatsapp_id']: u.get('name', 'Unknown') for u in u_res.data or []}

        ticket_events_map = {}
        if ticket_events:
            e_res = supabase.table('events').select('id, name').in_('id', ticket_events).execute()
            ticket_events_map = {e['id']: e['name'] for e in e_res.data or []}

        enriched_tickets = []
        for t in recent_tickets.data or []:
            enriched_tickets.append({
                "ticket_code": t['ticket_code'],
                "user_name": ticket_users_map.get(t['whatsapp_id'], 'Unknown'),
                "event_name": ticket_events_map.get(t['event_id'], 'N/A'),
                "status": "Scanned" if t['status'] == 'scanned' else "Issued",
                "created_at": t['created_at'][:16]
            })

        # ✅ RENDER TEMPLATE
        return render_template('admin.html',
            stats={
                "users": users,
                "organizers": organizers,
                "events": events,
                "tickets_issued": tickets_issued
            },
            failed_payments=enriched_failed,
            recent_tickets=enriched_tickets,
            secret=request.args.get('secret')  # optional: for dev
        )

    except Exception as e:
        print("❌ Admin dashboard error:", str(e))
        import traceback
        traceback.print_exc()
        return "Internal server error", 500

def get_organizer_by_code(org_code):
    try:
        org = supabase.table('organizers').select('*').eq('code', org_code).single().execute()
        return org.data
    except Exception as e:
        # If no row found (PGRST116) or any other error, return None
        print(f"⚠️ Organizer not found for code: {org_code} | Error: {e}")
        return None

@app.route('/organizer/<org_code>')
def organizer_dashboard(org_code):
    org = get_organizer_by_code(org_code)
    if not org:
        return "❌ Invalid organizer code", 404

    # Reuse the API logic
    api_data = organizer_api(org_code)
    if isinstance(api_data, tuple) and api_data[1] == 404:
        return api_data[0], 404

    # Render template
    return render_template('organizer.html',
        organizer=api_data['organizer'],
        events=api_data['events'],
        tickets=api_data['tickets'],
        total_revenue=api_data['total_revenue'],
        org_code=org_code
    )

@app.route('/organizer/<org_code>/api')
def organizer_api(org_code):
    org = get_organizer_by_code(org_code)
    if not org:
        return {"error": "Invalid organizer code"}, 404

    # Get events
    events = supabase.table('events') \
        .select('id, name, date, location, status') \
        .eq('organizer_id', org['id']) \
        .execute()

    if not events.data:
        return {
            "organizer": {"name": org['name'], "code": org['code']},
            "events": [],
            "tickets": [],
            "total_revenue": 0
        }

    event_ids = [e['id'] for e in events.data]

    # Get tickets
    tickets = supabase_service.table('tickets') \
        .select('ticket_code, status, created_at, whatsapp_id, event_id, ticket_type_id') \
        .in_('event_id', event_ids) \
        .order('created_at', desc=True) \
        .execute()

    # Get ticket types for price
    ticket_type_ids = list({t['ticket_type_id'] for t in tickets.data or []})
    ticket_types_map = {}
    if ticket_type_ids:
        tt_res = supabase.table('ticket_types').select('id, name, price').in_('id', ticket_type_ids).execute()
        ticket_types_map = {tt['id']: tt for tt in tt_res.data or []}

    # Get user names
    whatsapp_ids = [t['whatsapp_id'] for t in tickets.data or []]
    users_map = {}
    if whatsapp_ids:
        u_res = supabase.table('users').select('whatsapp_id, name').in_('whatsapp_id', whatsapp_ids).execute()
        users_map = {u['whatsapp_id']: u['name'] for u in u_res.data or []}

    # Enrich tickets
    enriched_tickets = []
    total_revenue = 0
    for t in tickets.data or []:
        tt = ticket_types_map.get(t['ticket_type_id'], {})
        price = tt.get('price', 0)
        total_revenue += price
        enriched_tickets.append({
            "ticket_code": t['ticket_code'],
            "buyer_name": users_map.get(t['whatsapp_id'], 'Unknown'),
            "ticket_type": tt.get('name', 'N/A'),
            "price": price,
            "event_id": t['event_id'],
            "status": "Scanned" if t['status'] == 'scanned' else "Issued",
            "created_at": t['created_at'][:16]
        })

    # Map event names
    events_map = {e['id']: e['name'] for e in events.data}

    for t in enriched_tickets:
        t['event_name'] = events_map.get(t['event_id'], 'N/A')

    return {
        "organizer": {
            "name": org['name'],
            "code": org['code'],
            "welcome_message": org.get('welcome_message', ''),
            "logo_url": org.get('logo_url')
        },
        "events": events.data,
        "tickets": enriched_tickets,
        "total_revenue": total_revenue
    }

def verify_flw_payment(tx_ref):
    """Returns 'successful' or 'failed'"""
    try:
        url = f"https://api.flutterwave.com/v3/transactions/{tx_ref}/verify"
        headers = {"Authorization": f"Bearer {os.getenv('FLW_SECRET_KEY')}"}
        resp = http_client.get(url, headers=headers)
        data = resp.json()
        if data.get("status") == "success" and data["data"]["status"] == "successful":
            return "successful"
        return "failed"
    except Exception as e:
        print(f"❌ FLW verify error for {tx_ref}: {e}")
        return "failed"

def verify_paystack_payment(reference):
    """Returns 'successful' or 'failed'"""
    try:
        url = f"https://api.paystack.co/transaction/verify/{reference}"
        headers = {"Authorization": f"Bearer {os.getenv('PAYSTACK_SECRET_KEY')}"}
        resp = http_client.get(url, headers=headers)
        data = resp.json()
        if data.get("status") is True and data["data"]["status"] == "success":
            return "successful"
        return "failed"
    except Exception as e:
        print(f"❌ Paystack verify error for {reference}: {e}")
        return "failed"

@app.route('/admin/reconcile-pending')
def reconcile_pending():
    if not is_admin_request():
        return {"error": "Unauthorized"}, 403

    # Calculate 24 hours ago in UTC
    utc = pytz.UTC
    time_24h_ago = datetime.now(utc) - timedelta(hours=24)
    time_24h_ago_iso = time_24h_ago.isoformat()

    pending_tx = supabase_service.table('transactions') \
        .select('*') \
        .eq('status', 'pending') \
        .gte('created_at', time_24h_ago_iso) \
        .execute()

    reconciled = 0
    for tx in pending_tx.data or []:
        if tx['payment_gateway'] == 'flutterwave':
            verified = verify_flw_payment(tx['payment_ref'])
        else:
            verified = verify_paystack_payment(tx['payment_ref'])
        
        if verified == 'successful':
            supabase_service.table('transactions').update({'status': 'paid'}).eq('payment_ref', tx['payment_ref']).execute()
            send_ticket(tx['whatsapp_id'])
            reconciled += 1

    return {"reconciled": reconciled, "checked": len(pending_tx.data or [])}


def start_reconciliation_scheduler():
    """Run reconciliation and cart cleanup every 10 minutes in background"""
    def run():
        utc = pytz.UTC
        while True:
            try:
                # ====== 1. Reconcile pending payments ======
                print("🕒 Running automatic payment reconciliation...")
                time_24h_ago = datetime.now(utc) - timedelta(hours=24)
                time_24h_ago_iso = time_24h_ago.isoformat()

                pending_tx = supabase_service.table('transactions') \
                    .select('*') \
                    .eq('status', 'pending') \
                    .gte('created_at', time_24h_ago_iso) \
                    .execute()

                reconciled = 0
                for tx in pending_tx.data or []:
                    if tx['payment_gateway'] == 'flutterwave':
                        verified = verify_flw_payment(tx['payment_ref'])
                    else:
                        verified = verify_paystack_payment(tx['payment_ref'])
                    
                    if verified == 'successful':
                        supabase_service.table('transactions').update({'status': 'paid'}).eq('payment_ref', tx['payment_ref']).execute()
                        send_ticket(tx['whatsapp_id'])
                        reconciled += 1

                print(f"✅ Auto-reconciliation done. Checked: {len(pending_tx.data or [])}, Reconciled: {reconciled}")

                # ====== 2. Clean up expired carts (including locked ones) ======
                print("🧹 Cleaning up expired user carts...")
                deleted = supabase_service.table('user_carts') \
                    .delete() \
                    .lt('expires_at', 'now()') \
                    .execute()
                deleted_count = len(deleted.data) if deleted.data else 0
                print(f"✅ Expired carts cleaned up: {deleted_count}")

            except Exception as e:
                print(f"❌ Background scheduler error: {e}")
                import traceback
                traceback.print_exc()
            
            time.sleep(600)  # every 10 minutes

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    print("🔁 Auto-reconciliation + cart cleanup scheduler started (every 10 mins)")

if __name__ == "__main__":
    start_reconciliation_scheduler()
    app.run(debug=False)