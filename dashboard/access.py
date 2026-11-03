"""Login and demo-mode limits for the dashboard (used when it is hosted, e.g. on Hugging Face).

Everything is off unless configured, so the local dashboard behaves exactly as before:

  DASHBOARD_PASSWORD     turns on the login page (one shared password; sessions last 12 hours)
  SESSION_SECRET         optional; signs the session cookie (derived from the password if not set)
  DEMO_MODE=true         shows a "Demo" badge and applies the two limits below
  DEMO_EMAIL_DOMAINS     comma list, e.g. "ltm.com"; in demo mode emails may only go to these domains
  DEMO_DAILY_ANALYSES    LTM analyses allowed per day in demo mode (default 30), to cap OpenAI/search spend
"""
import hashlib
import hmac
import os
import time
from datetime import date
from typing import Optional

COOKIE = "gcc_session"
SESSION_SECONDS = 12 * 3600
_failed: dict[str, list[float]] = {}  # ip -> recent failed login times (simple brute-force brake)


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def password() -> str:
    return os.getenv("DASHBOARD_PASSWORD", "")


def login_required() -> bool:
    return bool(password())


def demo_mode() -> bool:
    return _flag("DEMO_MODE")


def email_domains() -> list[str]:
    return [d.strip().lower().lstrip("@") for d in os.getenv("DEMO_EMAIL_DOMAINS", "").split(",") if d.strip()]


def daily_analyses() -> int:
    try:
        return max(0, int(os.getenv("DEMO_DAILY_ANALYSES", "30")))
    except ValueError:
        return 30


# ---------------------------------------------------------------- sessions
def _secret() -> bytes:
    explicit = os.getenv("SESSION_SECRET", "")
    base = explicit or ("gcc-dashboard|" + password())
    return hashlib.sha256(base.encode()).digest()


def make_session(now: Optional[float] = None) -> str:
    expires = int((now or time.time()) + SESSION_SECONDS)
    sig = hmac.new(_secret(), str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def valid_session(value: Optional[str], now: Optional[float] = None) -> bool:
    if not value or "." not in value:
        return False
    expires, sig = value.split(".", 1)
    if not expires.isdigit() or int(expires) < (now or time.time()):
        return False
    good = hmac.new(_secret(), expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good)


def check_password(given: str, ip: str) -> bool:
    """Constant-time compare; after 5 failures in 10 minutes from one address, refuse for a while."""
    now = time.time()
    recent = [t for t in _failed.get(ip, []) if now - t < 600]
    if len(recent) >= 5:
        _failed[ip] = recent
        return False
    ok = bool(password()) and hmac.compare_digest(given.encode(), password().encode())
    if not ok:
        recent.append(now)
        _failed[ip] = recent
        time.sleep(0.5)
    return ok


# ---------------------------------------------------------------- demo limits
def check_recipients(recipients: list[str]) -> Optional[str]:
    """Error text if a recipient is outside the allowed demo domains, else None."""
    domains = email_domains()
    if not demo_mode() or not domains:
        return None
    bad = [r for r in recipients if r.rsplit("@", 1)[-1].lower() not in domains]
    if bad:
        return f"In this demo, reports can only be emailed to {', '.join('@' + d for d in domains)} addresses."
    return None


def analyses_left(conn) -> Optional[int]:
    """Analyses still allowed today in demo mode (None = unlimited)."""
    if not demo_mode():
        return None
    from gcc_agents import db
    used = int(db.get_setting(conn, f"demo_analyses_{date.today().isoformat()}", "0") or 0)
    return max(0, daily_analyses() - used)


def use_analyses(conn, n: int) -> None:
    if not demo_mode() or n <= 0:
        return
    from gcc_agents import db
    key = f"demo_analyses_{date.today().isoformat()}"
    db.set_setting(conn, key, str(int(db.get_setting(conn, key, "0") or 0) + n))


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in - LTM GCC Radar</title>
<style>
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center; background:#0B0B0C
         url("/hero.png") center bottom / cover no-repeat; font:15px/1.5 "Segoe UI",Helvetica,Arial,sans-serif; color:#0B0B0C; }}
  .card {{ background:#fff; border-radius:12px; width:min(380px, calc(100% - 32px)); padding:28px 26px; box-shadow:0 10px 40px rgba(0,0,0,.4); }}
  .logo {{ font-size:30px; font-weight:800; color:#F2553D; letter-spacing:1px; }}
  .eyebrow {{ font-size:11px; letter-spacing:2px; text-transform:uppercase; color:#C2391F; font-weight:700; margin-top:2px; }}
  h1 {{ font-size:19px; margin:10px 0 18px; }}
  label {{ font-size:12px; font-weight:600; color:#5B6470; text-transform:uppercase; letter-spacing:.6px; }}
  input {{ width:100%; box-sizing:border-box; height:40px; margin:6px 0 14px; padding:0 12px; border:1px solid #D9DDE3; border-radius:8px; font:inherit; }}
  input:focus-visible, button:focus-visible {{ outline:2px solid #F2553D; outline-offset:2px; }}
  button {{ width:100%; height:42px; border:0; border-radius:8px; background:#F2553D; color:#fff; font:inherit; font-weight:700; cursor:pointer; }}
  button:hover {{ background:#C2391F; }}
  .err {{ color:#B42318; font-size:13px; margin:-6px 0 12px; }}
</style></head>
<body><form class="card" method="post" action="/login">
  <div class="logo">LTM</div><div class="eyebrow">GCC Origin &middot; Market Radar</div>
  <h1>India GCC Intelligence Dashboard</h1>
  <label for="pw">Password</label>
  <input id="pw" name="password" type="password" autocomplete="current-password" required autofocus>
  {error}
  <button type="submit">Sign in</button>
</form></body></html>"""
