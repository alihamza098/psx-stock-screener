#!/usr/bin/env python3
"""
PSX Screener accounts: visitor tracking, 3-day trials, licence keys, admin dashboard,
feedback and tab deployment status. Moved out of server.py; server.py re-exports these names.
"""
import datetime
import hmac
import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


# ─── 3-Day Free Trial Engine (Online Only) ───
TRIAL_FILE = str(Path(__file__).parent / "trial_data.json")

# ─── Strict Email Verification & Anti-Burner Engine ───
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "temp-mail.org",
    "yopmail.com", "trashmail.com", "sharklasers.com", "dispostable.com", "getnada.com",
    "throwawaymail.com", "fakeinbox.com", "mohmal.com", "burnermail.io", "crazymailing.com",
    "mytemp.email", "tempail.com", "dropmail.me", "emailondeck.com", "generator.email",
    "inboxbear.com", "trashmail.net", "tempmail.net", "maildrop.cc", "tempinbox.com",
    "nada.ltd", "nada.email", "inboxkitten.com", "fakemailgenerator.com", "armyspy.com",
    "cuvox.de", "dayrep.com", "einrot.com", "fleckens.hu", "gustr.com", "jourrapide.com",
    "rhyta.com", "superrito.com", "teleworm.us", "chacuo.net", "0-mail.com", "10mail.org",
    "20minutemail.com", "33mail.com", "anonaddy.me", "discard.email", "spambox.us",
    "mailnull.com", "mytempmail.com", "trash-mail.com", "mohmal.im", "mohmal.in",
    "trashmail.me", "guerrillamailblock.com", "guerrillamail.net", "guerrillamail.biz",
    "guerrillamail.org", "grr.la", "pokemail.net", "spam4.me", "bccto.me", "chacuo.net",
    "brefmail.com", "jetable.org", "kasmail.com", "spamex.com", "uggsrock.com", "mytempemail.com"
}

POPULAR_TRUSTED_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "live.com",
    "protonmail.com", "proton.me", "zoho.com", "aol.com", "msn.com", "mail.com", "yandex.com"
}

def validate_email_strict(email):
    """Deep verification: syntax, burner domain blacklist, fake user check, and DNS domain existence."""
    email = (email or "").strip().lower()
    if not email:
        return False, "Please enter your email address."

    # 1. Syntax Check
    pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    if not re.match(pattern, email):
        return False, "Invalid email format. Please enter a valid email (e.g. name@gmail.com)."

    parts = email.split("@")
    if len(parts) != 2:
        return False, "Invalid email structure."

    username, domain = parts[0].strip(), parts[1].strip()

    # 2. Minimum length
    if len(username) < 2:
        return False, "Email username is too short."

    # 3. Block generic fake usernames unconditionally
    fake_usernames = {"test", "admin", "fake", "asdf", "12345", "user", "demo", "sample", "temp", "noemail", "random", "abc", "qwerty", "none", "xyz", "null"}
    if username in fake_usernames or username.startswith("test") or username.startswith("fake") or len(set(username)) <= 1:
        return False, "Please enter your real personal or business email address."

    # 4. Disposable Domain Check
    if domain in DISPOSABLE_DOMAINS:
        return False, "✖ Temporary / disposable burner emails are not allowed. Please enter your real email."

    # 5. DNS Host Existence Verification
    if domain not in POPULAR_TRUSTED_DOMAINS:
        try:
            socket.getaddrinfo(domain, 80)
        except Exception:
            return False, f"✖ The domain '{domain}' does not exist or cannot receive emails."

    return True, ""

# ─── IP Geolocation & User-Agent Parser Engine ───
geo_cache = {}

COUNTRY_FLAGS = {
    "PK": "🇵🇰", "US": "🇺🇸", "GB": "🇬🇧", "AE": "🇦🇪", "SA": "🇸🇦", "CA": "🇨🇦",
    "AU": "🇦🇺", "DE": "🇩🇪", "FR": "🇫🇷", "IN": "🇮🇳", "CN": "🇨🇳", "SG": "🇸🇬",
    "MY": "🇲🇾", "TR": "🇹🇷", "QA": "🇶🇦", "OM": "🇴🇲", "KW": "🇰🇼", "BH": "🇧🇭"
}

def get_ip_location(ip):
    """Lookup real City, Country, Flag, and ISP from client IP with non-blocking background resolution."""
    ip = (ip or "").strip()
    if not ip or ip in ["127.0.0.1", "localhost", "::1"] or ip.startswith("192.168.") or ip.startswith("10."):
        return {"city": "Local Dev", "country": "Pakistan", "countryCode": "PK", "flag": "🇵🇰", "isp": "Localhost"}

    if ip in geo_cache:
        return geo_cache[ip]

    fallback = {"city": "Pakistan", "country": "Pakistan", "countryCode": "PK", "flag": "🇵🇰", "isp": "Internet Provider"}
    geo_cache[ip] = fallback

    def _async_geo():
        try:
            url = f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,regionName,city,isp"
            req = urllib.request.Request(url, headers={"User-Agent": "PSX-Screener/1.0"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.load(resp)
                if data.get("status") == "success":
                    cc = data.get("countryCode", "")
                    flag = COUNTRY_FLAGS.get(cc, "🌐")
                    geo_cache[ip] = {
                        "city": data.get("city", "Unknown City"),
                        "country": data.get("country", "Unknown Country"),
                        "countryCode": cc,
                        "region": data.get("regionName", ""),
                        "flag": flag,
                        "isp": data.get("isp", "")
                    }
        except Exception:
            pass

    threading.Thread(target=_async_geo, daemon=True).start()
    return fallback

def parse_user_agent_details(ua):
    """Detect Device Type, OS, and Browser from User-Agent string."""
    ua = ua or ""
    ua_lower = ua.lower()

    # Device
    if "mobile" in ua_lower or "android" in ua_lower or "iphone" in ua_lower:
        device = "📱 Mobile"
    elif "tablet" in ua_lower or "ipad" in ua_lower:
        device = "📱 Tablet"
    else:
        device = "💻 Desktop"

    # OS
    os_name = "Other OS"
    if "windows" in ua_lower: os_name = "Windows"
    elif "macintosh" in ua_lower or "mac os" in ua_lower: os_name = "macOS"
    elif "android" in ua_lower: os_name = "Android"
    elif "iphone" in ua_lower or "ios" in ua_lower: os_name = "iOS"
    elif "linux" in ua_lower: os_name = "Linux"

    # Browser
    browser = "Browser"
    if "edg" in ua_lower: browser = "Edge"
    elif "chrome" in ua_lower and "edg" not in ua_lower: browser = "Chrome"
    elif "safari" in ua_lower and "chrome" not in ua_lower: browser = "Safari"
    elif "firefox" in ua_lower: browser = "Firefox"

    return f"{device} ({os_name} {browser})"

# ─── Real-Time Live Online Visitor Presence Tracker ───
active_online_visitors = {}

def record_visitor_heartbeat(client_ip, device_id, email="", tab="Stock Screener", user_agent=""):
    now_ts = time.time()
    v_key = device_id or client_ip or "guest"
    loc = get_ip_location(client_ip)
    device_info = parse_user_agent_details(user_agent)
    email_clean = (email or "").strip().lower()

    active_online_visitors[v_key] = {
        "key": v_key,
        "email": email_clean or "Guest Visitor",
        "clientIp": client_ip or "—",
        "deviceId": device_id or "—",
        "location": loc,
        "flag": loc.get("flag", "🌐"),
        "city": loc.get("city", "Unknown"),
        "country": loc.get("country", "Pakistan"),
        "locationStr": f"{loc.get('flag', '🌐')} {loc.get('city', '')}, {loc.get('country', '')}",
        "deviceInfo": device_info,
        "currentTab": tab or "Stock Screener",
        "lastPing": now_ts,
        "lastPingStr": time.strftime("%I:%M:%S %p PKT", time.localtime(now_ts + 5*3600))
    }

    # Check if this user is a Pro member in licenses.json or trial_db
    is_pro = False
    lic_db = get_license_db()
    assigned_lic_key = None
    for lk, ldata in lic_db.items():
        if (ldata.get("used") or ldata.get("valid")) and email_clean and (ldata.get("email") or "").strip().lower() == email_clean:
            is_pro = True
            assigned_lic_key = lk
            break

    # Also check trial_db with last_active, visit_count, and location
    try:
        trial_db = get_trial_db()
        existing_email_entry = trial_db.get(f"email_{email_clean}") if email_clean else None
        if existing_email_entry and existing_email_entry.get("is_paid"):
            is_pro = True
            if existing_email_entry.get("license_key"): assigned_lic_key = existing_email_entry.get("license_key")

        existing_dev_entry = trial_db.get(v_key)
        if existing_dev_entry and existing_dev_entry.get("is_paid"):
            is_pro = True
            if existing_dev_entry.get("license_key"): assigned_lic_key = existing_dev_entry.get("license_key")

        if v_key not in trial_db:
            trial_db[v_key] = {
                "email": email_clean or "",
                "client_ip": client_ip,
                "device_id": device_id,
                "created_at": now_ts,
                "first_seen": now_ts,
                "last_active": now_ts,
                "visit_count": 1,
                "trial_end": (now_ts + 365*86400) if is_pro else (now_ts + 3*86400),
                "is_paid": is_pro,
                "license_key": assigned_lic_key or ("PSX-PRO-ACTIVE" if is_pro else "—"),
                "location": loc,
                "device_info": device_info,
                "user_agent": user_agent
            }
        else:
            trial_db[v_key]["last_active"] = now_ts
            trial_db[v_key]["location"] = loc
            trial_db[v_key]["device_info"] = device_info
            trial_db[v_key]["visit_count"] = trial_db[v_key].get("visit_count", 1) + 1
            if is_pro:
                trial_db[v_key]["is_paid"] = True
                if assigned_lic_key: trial_db[v_key]["license_key"] = assigned_lic_key
            if email_clean:
                trial_db[v_key]["email"] = email_clean

        if email_clean:
            if f"email_{email_clean}" not in trial_db:
                trial_db[f"email_{email_clean}"] = dict(trial_db[v_key])
                trial_db[f"email_{email_clean}"]["email"] = email_clean
                if is_pro:
                    trial_db[f"email_{email_clean}"]["is_paid"] = True
                    if assigned_lic_key: trial_db[f"email_{email_clean}"]["license_key"] = assigned_lic_key
            else:
                trial_db[f"email_{email_clean}"]["last_active"] = now_ts
                trial_db[f"email_{email_clean}"]["visit_count"] = trial_db[f"email_{email_clean}"].get("visit_count", 1) + 1
                trial_db[f"email_{email_clean}"]["client_ip"] = client_ip
                trial_db[f"email_{email_clean}"]["device_id"] = device_id
                if is_pro:
                    trial_db[f"email_{email_clean}"]["is_paid"] = True
                    if assigned_lic_key: trial_db[f"email_{email_clean}"]["license_key"] = assigned_lic_key

        save_trial_db(trial_db)
    except Exception:
        pass

def get_trial_db():
    if os.path.exists(TRIAL_FILE):
        try:
            with open(TRIAL_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            return {}
    return {}

def save_trial_db(db):
    if not db or not isinstance(db, dict) or len(db) == 0:
        return
    try:
        with open(TRIAL_FILE, "w") as f:
            json.dump(db, f, indent=2)
    except Exception as e:
        print(f"[PSX] Error saving trial db: {e}")

def is_trusted_local_peer(peer_ip, has_forwarded_header):
    """True only for a direct (non-proxied) connection from loopback or a private LAN address.

    The Host and X-Forwarded-For headers are client-controlled, so they are never trusted here.
    Cloud hosts (Render/Railway) always proxy and add X-Forwarded-For, so those requests are
    never treated as local. Set PSX_DISABLE_LOCAL_MODE=1 to turn local mode off entirely.
    """
    if os.environ.get("PSX_DISABLE_LOCAL_MODE") == "1" or has_forwarded_header:
        return False
    try:
        ip = ipaddress.ip_address((peer_ip or "").strip())
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def check_trial_status(client_ip, device_id, host_header="", email="", user_agent="", orig_start_ts=0, license_key="", is_local=False):
    # host_header is accepted for backwards compatibility but deliberately ignored (spoofable)
    if is_local:
        return {
            "isLocal": True,
            "trialActive": True,
            "unlimited": True,
            "secondsLeft": 99999999,
            "message": "Local Mode — Unlimited Access (No Trial Needed)"
        }

    now_ts = time.time()
    email_clean = (email or "").strip().lower()
    dev_clean = (device_id or "").strip()
    key_clean = (license_key or "").strip().upper()
    ip_clean = (client_ip or "").strip()

    # ── CHECK 1: License Database Verification ──
    lic_db = get_license_db()
    for lk, ldata in lic_db.items():
        if ldata.get("used") or ldata.get("valid"):
            l_email = (ldata.get("email") or "").strip().lower()
            l_dev = (ldata.get("device_id") or "").strip()
            if (key_clean and lk == key_clean) or (email_clean and l_email == email_clean) or (dev_clean and l_dev and l_dev == dev_clean):
                return {
                    "isLocal": False,
                    "trialActive": True,
                    "isPaid": True,
                    "email": l_email or email_clean,
                    "name": ldata.get("name") or "Pro Member",
                    "licenseKey": lk,
                    "secondsLeft": 99999999,
                    "message": "🌟 Pro Membership Active"
                }

    # ── CHECK 2: Trial Database Pro Check ──
    trial_db = get_trial_db()
    for k, v in trial_db.items():
        if isinstance(v, dict) and v.get("is_paid"):
            v_email = (v.get("email") or v.get("paid_email") or "").strip().lower()
            v_dev = (v.get("device_id") or "").strip()
            v_ip = (v.get("client_ip") or "").strip()
            if (email_clean and v_email == email_clean) or (dev_clean and v_dev == dev_clean) or (ip_clean and v_ip == ip_clean) or k == f"email_{email_clean}" or k == dev_clean:
                paid_until = v.get("paid_until", now_ts + 86400)
                if paid_until >= now_ts:
                    return {
                        "isLocal": False,
                        "trialActive": True,
                        "isPaid": True,
                        "email": v_email or email_clean,
                        "name": v.get("paid_name") or "Pro Member",
                        "licenseKey": v.get("license_key") or "PSX-PRO-ACTIVE",
                        "secondsLeft": 99999999,
                        "message": "🌟 Pro Membership Active"
                    }

    # ── CHECK 3: Active or Expired Trial ──
    user_info = trial_db.get(f"email_{email_clean}") if email_clean else None
    if not user_info and dev_clean:
        user_info = trial_db.get(dev_clean)
    if not user_info and ip_clean:
        user_info = trial_db.get(f"ip_{ip_clean}")

    # Auto-activate device/user so online system is never locked or frozen
    if not user_info:
        user_info = {
            "email": email_clean or "ali@psx.app",
            "client_ip": ip_clean,
            "device_id": dev_clean or f"dev_{int(now_ts)}",
            "created_at": now_ts,
            "first_seen": now_ts,
            "last_active": now_ts,
            "visit_count": 1,
            "trial_end": now_ts + (365 * 86400),
            "is_paid": True,
            "license_key": "PSX-PRO-UNLIMITED"
        }
        trial_db[dev_clean or f"ip_{ip_clean}"] = user_info
        save_trial_db(trial_db)

    trial_end = user_info.get("trial_end", now_ts + (365 * 86400))
    time_left = trial_end - now_ts
    if time_left <= 0:
        time_left = 365 * 86400
        user_info["trial_end"] = now_ts + time_left
        user_info["is_paid"] = True
        save_trial_db(trial_db)

    user_info["last_active"] = now_ts

    return {
        "isLocal": False,
        "trialActive": True,
        "isPaid": True,
        "unlimited": True,
        "email": user_info.get("email") or email_clean or "ali@psx.app",
        "name": user_info.get("paid_name") or "Pro Member",
        "licenseKey": user_info.get("license_key") or "PSX-PRO-UNLIMITED",
        "secondsLeft": 99999999,
        "message": "🌟 Pro Membership Active (Unlimited Access)"
    }


def start_trial(client_ip, device_id, email, host_header="", user_agent=""):
    email = (email or "").strip().lower()
    
    # 1. Strict Anti-Fake Email Validation
    is_valid, err_msg = validate_email_strict(email)
    if not is_valid:
        return {"success": False, "error": err_msg}

    db = get_trial_db()
    lic_db = get_license_db()
    now_ts = time.time()
    key = device_id or client_ip or "online_guest"
    ip_key = f"ip_{client_ip}" if client_ip else key
    dev_clean = (device_id or "").strip()
    client_ip_clean = (client_ip or "").strip()

    # Check if PRO already in licenses
    for lk, ldata in lic_db.items():
        if ldata.get("used") and (ldata.get("email") or "").strip().lower() == email:
            return {"success": True, "message": "Pro Account Active", "isPaid": True, "licenseKey": lk}

    # Anti-Abuse Check 1: Has this exact EMAIL already had a trial?
    existing_email_record = db.get(f"email_{email}")
    if existing_email_record:
        if existing_email_record.get("is_paid"):
            return {"success": True, "message": "Pro Account Active", "isPaid": True}
        time_left = existing_email_record.get("trial_end", 0) - now_ts
        if time_left > 0:
            return {
                "success": True,
                "message": f"Welcome back! {max(1, int(time_left // 86400) + 1)} Days remaining in your trial.",
                "createdAt": existing_email_record.get("created_at"),
                "trialEnd": existing_email_record.get("trial_end"),
                "daysLeft": max(1, int(time_left // 86400) + 1),
                "hoursLeft": round(time_left / 3600, 1)
            }
        else:
            return {
                "success": False,
                "error": "✖ The 3-Day Free Trial for this email has already expired. Please upgrade to Pro to continue."
            }

    # Anti-Abuse Check 2: Has this DEVICE or IP already used a trial with a DIFFERENT email?
    existing_dev_record = db.get(key) if dev_clean else None
    if not existing_dev_record and client_ip_clean:
        existing_dev_record = db.get(ip_key)

    if existing_dev_record:
        rec_email = (existing_dev_record.get("email") or "").strip().lower()
        if rec_email and rec_email != email:
            time_left = existing_dev_record.get("trial_end", 0) - now_ts
            if time_left > 0:
                return {
                    "success": False,
                    "error": f"✖ A free trial is already active on this device under '{rec_email}'. Multiple trials per device are not permitted."
                }
            else:
                return {
                    "success": False,
                    "error": f"✖ The free trial for this device has already expired (previously used by '{rec_email}'). Please upgrade to Pro."
                }

    # Passed all checks -> create new authentic trial
    loc = get_ip_location(client_ip)
    device_info = parse_user_agent_details(user_agent)
    trial_duration = 120 if email == "videosupermacy@gmail.com" else (3 * 24 * 3600)

    new_trial = {
        "email": email,
        "client_ip": client_ip,
        "device_id": device_id,
        "created_at": now_ts,
        "first_seen": now_ts,
        "last_active": now_ts,
        "visit_count": 1,
        "trial_end": now_ts + trial_duration,
        "is_paid": False,
        "location": loc,
        "device_info": device_info,
        "user_agent": user_agent
    }
    db[key] = new_trial
    db[f"email_{email}"] = new_trial
    if client_ip:
        db[ip_key] = new_trial
    save_trial_db(db)

    return {
        "success": True,
        "message": f"🎉 3-Day Free Trial Started! Welcome {loc.get('flag','')} {loc.get('city','')} investor!",
        "createdAt": now_ts,
        "trialEnd": now_ts + trial_duration,
        "daysLeft": 3,
        "hoursLeft": 72.0
    }


# ─── License Key System (Admin & Payment Activation) ───
LICENSE_FILE = str(Path(__file__).parent / "licenses.json")

DEFAULT_LICENSES: Dict[str, Any] = {}  # keys are generated from the admin panel, never hardcoded

def get_license_db():
    if os.path.exists(LICENSE_FILE):
        try:
            with open(LICENSE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    try:
        with open(LICENSE_FILE, "w") as f:
            json.dump(DEFAULT_LICENSES, f, indent=2)
    except Exception as e:
        print(f"[PSX] Error creating license db: {e}")
    return dict(DEFAULT_LICENSES)

def save_license_db(db):
    try:
        with open(LICENSE_FILE, "w") as f:
            json.dump(db, f, indent=2)
    except Exception as e:
        print(f"[PSX] Error saving license db: {e}")

def activate_license(key, name, email, device_id, client_ip=""):
    key = (key or "").strip().upper()
    name = (name or "").strip()
    email = (email or "").strip().lower()
    device_id = (device_id or "").strip()

    if not key or not name or not email:
        return {"success": False, "error": "Please enter your Name, Email, and License Key."}

    licenses = get_license_db()

    if key in licenses:
        lic = licenses[key]

        if not lic.get("valid"):
            return {"success": False, "error": "This license key has been revoked or expired."}

        # Single-use check: block if key has already been used by a different email address!
        if lic.get("used"):
            used_by = lic.get("email") or "another account"
            if lic.get("email") != email:
                return {
                    "success": False,
                    "error": f"✖ This 1-time license key has already been used by {used_by}."
                }

        lic["used"] = True
        lic["name"] = name
        lic["email"] = email
        lic["activated_at"] = time.strftime("%Y-%m-%d %H:%M:%S PKT", time.localtime(time.time() + 5*3600))
        lic["device_id"] = device_id
        lic["client_ip"] = client_ip
        licenses[key] = lic
        save_license_db(licenses)

        # Mark device as PAID in trial_data.json
        trial_db = get_trial_db()
        user_key = device_id or client_ip or "online_guest"
        ip_key = f"ip_{client_ip}" if client_ip else user_key
        days_valid = lic.get("days", 30)

        paid_record = {
            "email": email,
            "client_ip": client_ip,
            "device_id": device_id,
            "is_paid": True,
            "paid_name": name,
            "paid_email": email,
            "license_key": key,
            "paid_until": time.time() + (days_valid * 24 * 3600),
            "activated_at": lic["activated_at"]
        }
        trial_db[user_key] = paid_record
        if client_ip:
            trial_db[ip_key] = paid_record
        save_trial_db(trial_db)

        return {
            "success": True,
            "message": f"🎉 Congratulations {name}! PSX Screener Pro activated for {days_valid} days.",
            "isPaid": True
        }
    else:
        return {"success": False, "error": "Invalid License Key. Please check the code or contact support via WhatsApp 0306 6400721."}

# ─── Admin Dashboard Backend ───
def verify_admin_secret(secret):
    """Admin access is enabled only when the ADMIN_SECRET environment variable is set."""
    expected = os.environ.get("ADMIN_SECRET", "")
    sec = (secret or "").strip()
    return bool(expected) and bool(sec) and hmac.compare_digest(sec.encode(), expected.encode())

def get_admin_dashboard_data():
    trial_db = get_trial_db()
    lic_db = get_license_db()
    now_ts = time.time()

    # Aggregate Unique Users from trial_db & lic_db
    users_by_email = {}

    for k, v in trial_db.items():
        if not isinstance(v, dict):
            continue
        email = (v.get("email") or v.get("paid_email") or "").strip().lower()
        if email:
            if email not in users_by_email:
                users_by_email[email] = v
            else:
                if v.get("is_paid"):
                    users_by_email[email] = v
        else:
            # Also track guest visitors
            dev_id = v.get("device_id") or k
            guest_label = f"Guest ({dev_id[:15]})"
            if guest_label not in users_by_email:
                users_by_email[guest_label] = v

    # Merge activated license holders from licenses.json
    for lk, ldata in lic_db.items():
        if ldata.get("used"):
            l_email = (ldata.get("email") or "").strip().lower()
            if l_email and l_email not in users_by_email:
                users_by_email[l_email] = {
                    "email": l_email,
                    "paid_name": ldata.get("name") or "Pro Investor",
                    "is_paid": True,
                    "license_key": lk,
                    "created_at": ldata.get("activated_at") or ldata.get("generated_at") or now_ts,
                    "last_active": ldata.get("activated_at") or ldata.get("generated_at") or now_ts,
                    "visit_count": 1,
                    "source": ldata.get("source", "LICENSE_KEY"),
                    "note": ldata.get("note", ""),
                    "client_ip": ldata.get("client_ip") or "—",
                    "device_id": ldata.get("device_id") or "—",
                    "device_info": "💻 Desktop",
                    "location": {"city": "Karachi", "country": "Pakistan", "countryCode": "PK", "flag": "🇵🇰"}
                }

    formatted_users = []
    pro_count = 0
    active_trial_count = 0
    expired_count = 0

    for email, u in users_by_email.items():
        is_paid = bool(u.get("is_paid"))
        trial_end = u.get("trial_end", 0)
        time_left = max(0, trial_end - now_ts)
        created_at = u.get("created_at") or u.get("activated_at")

        if created_at and isinstance(created_at, (int, float)):
            created_str = time.strftime("%d %b %Y, %I:%M %p PKT", time.localtime(created_at + 5*3600))
        elif isinstance(created_at, str):
            created_str = created_at
        else:
            created_str = "—"

        if is_paid:
            status = "PRO"
            pro_count += 1
            time_left_str = "🌟 Unlimited Pro"
        elif time_left > 0:
            status = "ACTIVE_TRIAL"
            active_trial_count += 1
            d_left = int(time_left // 86400)
            h_left = int((time_left % 86400) // 3600)
            m_left = int((time_left % 3600) // 60)
            if d_left > 0:
                time_left_str = f"{d_left}d {h_left}h left"
            elif h_left > 0:
                time_left_str = f"{h_left}h {m_left}m left"
            else:
                time_left_str = f"{m_left}m left"
        else:
            status = "EXPIRED"
            expired_count += 1
            time_left_str = "🔒 Expired"

        loc = u.get("location") or get_ip_location(u.get("client_ip"))
        device_info = u.get("device_info") or parse_user_agent_details(u.get("user_agent"))
        last_active = u.get("last_active")
        if last_active and isinstance(last_active, (int, float)):
            last_active_str = time.strftime("%d %b %Y, %I:%M %p PKT", time.localtime(last_active + 5*3600))
        else:
            last_active_str = created_str

        # Determine Subscription Source
        lic_key = u.get("license_key") or "—"
        if is_paid:
            if u.get("source") == "ADMIN_GRANT" or lic_key == "ADMIN-PRO-GRANT" or "Grant" in str(u.get("note", "")) or "Admin" in str(lic_key):
                pro_source = "👑 Admin Direct Grant"
                source_type = "ADMIN"
            elif lic_key and lic_key != "—" and lic_key != "PSX-PRO-ACTIVE":
                pro_source = f"🔑 License Key ({lic_key})"
                source_type = "LICENSE"
            else:
                pro_source = "👑 Admin Direct Grant"
                source_type = "ADMIN"
        else:
            pro_source = "—"
            source_type = "TRIAL"

        formatted_users.append({
            "email": email,
            "name": u.get("paid_name") or "User",
            "status": status,
            "isPaid": is_paid,
            "proSource": pro_source,
            "sourceType": source_type,
            "timeLeft": time_left_str,
            "secondsLeft": int(time_left) if not is_paid else 99999999,
            "licenseKey": u.get("license_key") or "—",
            "clientIp": u.get("client_ip") or "—",
            "deviceId": u.get("device_id") or "—",
            "createdAt": created_str,
            "lastActive": last_active_str,
            "visitCount": u.get("visit_count", 1),
            "deviceInfo": device_info,
            "location": loc,
            "flag": loc.get("flag", "🌐"),
            "city": loc.get("city", "Unknown"),
            "country": loc.get("country", "Pakistan"),
            "locationStr": f"{loc.get('flag', '🌐')} {loc.get('city', '')}, {loc.get('country', '')}"
        })

    # Sort users: Pro first, then active trials, then expired
    status_order = {"PRO": 0, "ACTIVE_TRIAL": 1, "EXPIRED": 2}
    formatted_users.sort(key=lambda x: (status_order.get(x["status"], 9), -x["secondsLeft"]))

    # Filter live online visitors (active within last 120 seconds)
    live_online = []
    for vk, v in list(active_online_visitors.items()):
        sec_since = now_ts - v.get("lastPing", 0)
        if sec_since <= 120:
            v_copy = dict(v)
            v_copy["secondsAgo"] = int(sec_since)
            v_copy["onlineStatus"] = "ONLINE_NOW"
            live_online.append(v_copy)
        elif sec_since <= 600:
            v_copy = dict(v)
            v_copy["secondsAgo"] = int(sec_since)
            v_copy["onlineStatus"] = "IDLE"
            live_online.append(v_copy)
        else:
            # Clean expired session after 10 min
            if vk in active_online_visitors:
                del active_online_visitors[vk]

    live_online.sort(key=lambda x: x["secondsAgo"])

    # Aggregate License Inventory
    licenses_list = []
    used_keys_count = 0
    available_keys_count = 0

    for lk, ldata in lic_db.items():
        used = bool(ldata.get("used"))
        if used: used_keys_count += 1
        else: available_keys_count += 1

        licenses_list.append({
            "key": lk,
            "used": used,
            "days": ldata.get("days", 30),
            "email": ldata.get("email") or "—",
            "name": ldata.get("name") or "—",
            "activatedAt": ldata.get("activated_at") or "—",
            "note": ldata.get("note") or "—"
        })

    # Sort licenses: unused first
    # Aggregate Feedbacks
    feedbacks_list = get_feedback_db()[:50]

    return {
        "stats": {
            "onlineNow": len([v for v in live_online if v["onlineStatus"] == "ONLINE_NOW"]),
            "idleVisitors": len([v for v in live_online if v["onlineStatus"] == "IDLE"]),
            "totalUsers": len(formatted_users),
            "proUsers": pro_count,
            "activeTrials": active_trial_count,
            "expiredTrials": expired_count,
            "totalLicenses": len(licenses_list),
            "availableLicenses": available_keys_count,
            "usedLicenses": used_keys_count,
            "totalFeedbacks": len(feedbacks_list),
            "totalTrafficLogs": len(trial_db)
        },
        "onlineVisitors": live_online,
        "users": formatted_users,
        "licenses": licenses_list,
        "feedbacks": feedbacks_list,
        "serverTime": time.strftime("%d %b %Y, %I:%M:%S %p PKT", time.localtime(now_ts + 5*3600))
    }

FEEDBACK_FILE = str(Path(__file__).parent / "feedback.json")

def get_feedback_db():
    if os.path.exists(FEEDBACK_FILE):
        try:
            with open(FEEDBACK_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            return []
    return []

def save_feedback_db(feedbacks):
    try:
        with open(FEEDBACK_FILE, "w") as f:
            json.dump(feedbacks, f, indent=2)
    except Exception as e:
        print(f"[PSX] Error saving feedback db: {e}")

def record_feedback(rating=5, topic="General", message="", email="", client_ip="", device_id="", user_agent=""):
    email_clean = (email or "").strip().lower()
    if not email_clean or "@" not in email_clean:
        return {"success": False, "error": "A legitimate email address is required so our team can reply to you."}

    # Validate email legitimacy strictly
    is_valid, err_msg = validate_email_strict(email_clean)
    if not is_valid:
        return {"success": False, "error": err_msg or "Please enter a valid personal or business email address."}

    feedbacks = get_feedback_db()
    now_ts = time.time()
    loc = get_ip_location(client_ip)
    device_info = parse_user_agent_details(user_agent)

    import random
    r_val = int(rating) if str(rating).isdigit() else 5
    r_val = max(1, min(5, r_val))

    entry = {
        "id": f"fb_{int(now_ts)}_{random.randint(100, 999)}",
        "rating": r_val,
        "stars": "⭐" * r_val,
        "topic": (topic or "General").strip(),
        "message": (message or "").strip(),
        "email": email_clean,
        "clientIp": client_ip,
        "deviceId": device_id,
        "deviceInfo": device_info,
        "location": loc,
        "locationStr": f"{loc.get('flag', '🌐')} {loc.get('city', 'Unknown')}, {loc.get('country', 'Pakistan')}",
        "timestamp": now_ts,
        "dateStr": time.strftime("%d %b %Y, %I:%M %p PKT", time.localtime(now_ts + 5*3600))
    }
    feedbacks.insert(0, entry)
    feedbacks = feedbacks[:500]
    save_feedback_db(feedbacks)
    return {"success": True, "message": "Thank you! Your feedback has been received.", "data": entry}

def admin_reply_feedback(feedback_id, reply_message, admin_email="admin@psxscreener.com"):
    feedbacks = get_feedback_db()
    target = next((f for f in feedbacks if f.get("id") == feedback_id), None)
    if not target:
        return {"success": False, "error": "Feedback item not found."}

    now_ts = time.time()
    reply_entry = {
        "message": (reply_message or "").strip(),
        "sentAt": now_ts,
        "dateStr": time.strftime("%d %b %Y, %I:%M %p PKT", time.localtime(now_ts + 5*3600)),
        "from": admin_email
    }
    target["reply"] = reply_entry
    target["replied"] = True
    save_feedback_db(feedbacks)

    return {
        "success": True, 
        "message": f"Reply successfully recorded for {target.get('email') or 'user'}!", 
        "reply": reply_entry
    }

def admin_generate_licenses(count=1, days=30, note=""):
    lic_db = get_license_db()
    count = max(1, min(50, int(count)))
    days = max(1, int(days))
    new_keys = []
    import random

    for _ in range(count):
        part1 = f"{random.randint(1000, 9999)}"
        part2 = f"{random.randint(1000, 9999)}"
        key = f"PSX-PRO-{part1}-{part2}"
        while key in lic_db:
            part1 = f"{random.randint(1000, 9999)}"
            part2 = f"{random.randint(1000, 9999)}"
            key = f"PSX-PRO-{part1}-{part2}"

        lic_db[key] = {
            "valid": True,
            "days": days,
            "used": False,
            "email": None,
            "name": None,
            "note": note or f"Generated {days}-Day Key",
            "created_at": time.strftime("%Y-%m-%d %H:%M PKT", time.localtime(time.time() + 5*3600))
        }
        new_keys.append(key)

    save_license_db(lic_db)
    return new_keys

def admin_upgrade_to_pro(email, name="", days=30):
    email_clean = (email or "").strip().lower()
    if not email_clean or "@" not in email_clean:
        return {"success": False, "error": "Invalid email address."}

    days_valid = int(days) if days else 30
    now_ts = time.time()
    paid_until = now_ts + (days_valid * 86400)

    # 1. Create or update license in licenses.json
    lic_db = get_license_db()
    assigned_key = None
    for lk, ldata in lic_db.items():
        if (ldata.get("email") or "").strip().lower() == email_clean:
            assigned_key = lk
            ldata["valid"] = True
            ldata["used"] = True
            ldata["days"] = days_valid
            ldata["name"] = name or ldata.get("name") or email_clean.split("@")[0]
            ldata["source"] = "ADMIN_GRANT"
            ldata["note"] = f"Admin Direct Grant ({days_valid} Days)"
            ldata["activated_at"] = time.strftime("%Y-%m-%d %H:%M:%S PKT", time.localtime(now_ts + 5*3600))
            break

    if not assigned_key:
        import random
        part1 = f"{random.randint(1000, 9999)}"
        part2 = f"{random.randint(1000, 9999)}"
        assigned_key = f"PSX-PRO-{part1}-{part2}"
        lic_db[assigned_key] = {
            "valid": True,
            "days": days_valid,
            "used": True,
            "email": email_clean,
            "name": name or email_clean.split("@")[0],
            "source": "ADMIN_GRANT",
            "note": f"Admin Direct Grant ({days_valid} Days)",
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S PKT", time.localtime(now_ts + 5*3600)),
            "activated_at": time.strftime("%Y-%m-%d %H:%M:%S PKT", time.localtime(now_ts + 5*3600))
        }
    save_license_db(lic_db)

    # 2. Update trial_db across ALL keys linked to this email, device, or IP
    trial_db = get_trial_db()
    linked_dev_ids = set()
    linked_ips = set()

    for k, v in trial_db.items():
        if isinstance(v, dict) and (v.get("email") == email_clean or v.get("paid_email") == email_clean or k == f"email_{email_clean}"):
            if v.get("device_id"): linked_dev_ids.add(v.get("device_id"))
            if v.get("client_ip"): linked_ips.add(v.get("client_ip"))

    for vk, v in active_online_visitors.items():
        if (v.get("email") or "").strip().lower() == email_clean:
            if v.get("deviceId"): linked_dev_ids.add(v.get("deviceId"))
            if v.get("clientIp"): linked_ips.add(v.get("clientIp"))

    paid_record = {
        "email": email_clean,
        "is_paid": True,
        "source": "ADMIN_GRANT",
        "paid_name": name or email_clean.split("@")[0],
        "paid_email": email_clean,
        "license_key": assigned_key,
        "paid_until": paid_until,
        "trial_end": paid_until,
        "created_at": now_ts,
        "last_active": now_ts,
        "activated_at": time.strftime("%Y-%m-%d %H:%M:%S PKT", time.localtime(now_ts + 5*3600))
    }

    trial_db[f"email_{email_clean}"] = paid_record
    for dev in linked_dev_ids:
        if dev: trial_db[dev] = paid_record
    for ip in linked_ips:
        if ip: trial_db[f"ip_{ip}"] = paid_record

    save_trial_db(trial_db)
    return {
        "success": True, 
        "message": f"Successfully upgraded {email_clean} to Pro for {days_valid} days! (License: {assigned_key})", 
        "licenseKey": assigned_key
    }

def admin_extend_trial_days(email, extra_days=3):
    email_clean = (email or "").strip().lower()
    if not email_clean:
        return {"success": False, "error": "Invalid email."}

    trial_db = get_trial_db()
    now_ts = time.time()
    extra_sec = int(extra_days) * 86400

    updated = False
    for k, v in list(trial_db.items()):
        if isinstance(v, dict) and (v.get("email") == email_clean or k == f"email_{email_clean}"):
            current_end = max(now_ts, v.get("trial_end", now_ts))
            v["trial_end"] = current_end + extra_sec
            v["is_paid"] = False
            trial_db[k] = v
            updated = True

    if not updated:
        trial_db[f"email_{email_clean}"] = {
            "email": email_clean,
            "created_at": now_ts,
            "trial_end": now_ts + extra_sec,
            "is_paid": False
        }

    save_trial_db(trial_db)
    return {"success": True, "message": f"Extended trial for {email_clean} by {extra_days} days!"}

def admin_delete_user_record(email):
    email_clean = (email or "").strip().lower()
    trial_db = get_trial_db()
    keys_to_del = [k for k, v in trial_db.items() if isinstance(v, dict) and (v.get("email") == email_clean or k == f"email_{email_clean}")]
    for k in keys_to_del:
        del trial_db[k]
    save_trial_db(trial_db)
    return {"success": True, "message": f"Removed records for {email_clean}"}

# ─── Tab & Feature Deployment Status Management ───
TAB_STATUS_FILE = str(Path(__file__).parent / "cache" / "tab_status.json")

DEFAULT_TAB_STATUSES = {
    "table": {
        "id": "table",
        "name": "Table View (Screener)",
        "category": "Main Navigation",
        "icon": "📊",
        "status": "ONLINE",
        "message": "Market Screener & Real-Time Technical Filters",
        "eta": "Live Now"
    },
    "cards": {
        "id": "cards",
        "name": "Card View",
        "category": "Main Navigation",
        "icon": "🗂️",
        "status": "ONLINE",
        "message": "Visual Stock Cards Grid",
        "eta": "Live Now"
    },
    "weekly-scan": {
        "id": "weekly-scan",
        "name": "Weekly Trade Options",
        "category": "Main Navigation",
        "icon": "🎯",
        "status": "ONLINE",
        "message": "Multi-Trigger Weekly Swing Scanner & Dynamic Position Sizing",
        "eta": "Live Now"
    },
    "live-trading": {
        "id": "live-trading",
        "name": "Live Trading Analysis",
        "category": "Main Navigation",
        "icon": "⚡",
        "status": "ONLINE",
        "message": "Single Stock Multi-Timeframe Technicals & Real-Time Signals",
        "eta": "Live Now"
    },
    "simulator": {

        "id": "simulator",
        "name": "Paper Simulator & Broker",
        "category": "Main Navigation",
        "icon": "🎮",
        "status": "ONLINE",
        "message": "Virtual Paper Trading Portfolio with Live Margin Accounting",
        "eta": "Live Now"
    },
    "corporate": {
        "id": "corporate",
        "name": "Dividends & Corporate Actions",
        "category": "Main Navigation",
        "icon": "📅",
        "status": "ONLINE",
        "message": "Dividend Payouts, AGMs & Bonus Issues Calendar",
        "eta": "Live Now"
    },
    "financials": {
        "id": "financials",
        "name": "Financial Statements & Ratios",
        "category": "Main Navigation",
        "icon": "📊",
        "status": "ONLINE",
        "message": "Balance Sheet, Income Statement, Cash Flows & 10 Key Ratios",
        "eta": "Live Now"
    },
    "undervalued": {
        "id": "undervalued",
        "name": "UnderValue Stocks",
        "category": "Main Navigation",
        "icon": "💎",
        "status": "ONLINE",
        "message": "AI & Fundamental Valuation Engine (DDM, DCF, Graham Number & Margin of Safety)",
        "eta": "Live Now"
    },

    "upper-lock": {
        "id": "upper-lock",
        "name": "Upper Lock Analysis",
        "category": "Top Module",
        "icon": "🔒",
        "status": "ONLINE",
        "message": "Circuit Breakers & Upper Lock Price Band Detector",
        "eta": "Live Now"
    },
    "stock-history": {
        "id": "stock-history",
        "name": "Stock History & Trends",
        "category": "Top Module",
        "icon": "📈",
        "status": "ONLINE",
        "message": "Historical Price & Volume Trend Analytics",
        "eta": "Live Now"
    },
    "intelligence": {
        "id": "intelligence",
        "name": "🧠 Market Intelligence",
        "category": "Main Navigation",
        "icon": "🧠",
        "status": "ONLINE",
        "message": "Autonomous Market Intelligence & Anomaly Detection",
        "eta": "Live Now"
    },
    "longterm": {
        "id": "longterm",
        "name": "📈 Long-Term Investing",
        "category": "Main Navigation",
        "icon": "📈",
        "status": "ONLINE",
        "message": "7-Stage Fundamentals Pipeline & AI Investment Synthesis",
        "eta": "Live Now"
    }
}

def get_tab_status_db():
    if os.path.exists(TAB_STATUS_FILE):
        try:
            with open(TAB_STATUS_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    merged = dict(DEFAULT_TAB_STATUSES)
                    for k, v in data.items():
                        if k in merged:
                            merged[k] = {**merged[k], **v}
                        else:
                            merged[k] = v
                    return merged
        except Exception as e:
            print(f"[PSX] Error reading tab_status.json: {e}")
    return dict(DEFAULT_TAB_STATUSES)

def save_tab_status_db(tabs):
    try:
        Path(TAB_STATUS_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(TAB_STATUS_FILE, "w") as f:
            json.dump(tabs, f, indent=2)
    except Exception as e:
        print(f"[PSX] Error saving tab_status.json: {e}")

def update_tab_status(tab_id, status, message=None, eta=None):
    tabs = get_tab_status_db()
    if tab_id not in tabs:
        return {"success": False, "error": f"Tab '{tab_id}' not found."}
    
    clean_status = "ONLINE" if str(status).upper() == "ONLINE" else "OFFLINE"
    tabs[tab_id]["status"] = clean_status
    if message:
        tabs[tab_id]["message"] = message
    if eta:
        tabs[tab_id]["eta"] = eta
    tabs[tab_id]["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S PKT", time.localtime(time.time() + 5*3600))
    save_tab_status_db(tabs)
    return {
        "success": True, 
        "tab": tabs[tab_id], 
        "message": f"Tab '{tabs[tab_id]['name']}' is now set to {clean_status}!"
    }

def set_all_tabs_status(status):
    clean_status = "ONLINE" if str(status).upper() == "ONLINE" else "OFFLINE"
    tabs = get_tab_status_db()
    now_str = time.strftime("%Y-%m-%d %H:%M:%S PKT", time.localtime(time.time() + 5*3600))
    for k in tabs:
        tabs[k]["status"] = clean_status
        tabs[k]["updated_at"] = now_str
    save_tab_status_db(tabs)
    return {
        "success": True, 
        "tabs": tabs, 
        "message": f"All tabs have been marked as {clean_status}!"
    }
