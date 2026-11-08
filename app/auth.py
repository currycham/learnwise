"""邮箱验证码登录。没有密码；新邮箱第一次登录时就是注册，内测期间要填邀请码。"""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from . import db, mail
from .config import ADMIN_EMAILS, INVITE_CODE, SITE_URL

COOKIE = "lw_session"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def now() -> datetime:
    return datetime.now(timezone.utc)


def ts(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def _hash(email: str, code: str) -> str:
    return hashlib.sha256(f"{email}:{code}".encode()).hexdigest()


def send_code(email: str, invite: str = "") -> None:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "邮箱格式不对")
    user = db.one("SELECT banned FROM users WHERE email = ?", email)
    if user and user["banned"]:
        raise HTTPException(403, "这个账号已被停用")
    if not user and INVITE_CODE and invite.strip() != INVITE_CODE and email not in ADMIN_EMAILS:
        raise HTTPException(403, "内测期间注册需要邀请码")
    last = db.one("SELECT sent_at FROM login_codes WHERE email = ?", email)
    if last and last["sent_at"] > ts(now() - timedelta(seconds=55)):
        raise HTTPException(429, "验证码刚发过，一分钟后再试")
    code = f"{secrets.randbelow(10**6):06d}"
    db.run("INSERT OR REPLACE INTO login_codes (email, code_hash, expires_at, tries, sent_at) VALUES (?,?,?,0,?)",
           email, _hash(email, code), ts(now() + timedelta(minutes=10)), ts(now()))
    mail.send(email, f"LearnWise 登录验证码 {code}",
              f"你的 LearnWise 登录验证码是：{code}\n\n10 分钟内有效。如果不是你本人操作，忽略这封邮件即可。\n\n{SITE_URL}")


def verify(email: str, code: str) -> str:
    """验证通过返回会话 token。"""
    email = email.strip().lower()
    row = db.one("SELECT * FROM login_codes WHERE email = ?", email)
    if not row or row["expires_at"] < ts(now()):
        raise HTTPException(400, "验证码已过期，请重新获取")
    if row["tries"] >= 5:
        raise HTTPException(429, "试错太多次，请重新获取验证码")
    if not hmac.compare_digest(row["code_hash"], _hash(email, code.strip())):
        db.run("UPDATE login_codes SET tries = tries + 1 WHERE email = ?", email)
        raise HTTPException(400, "验证码不对")
    db.run("DELETE FROM login_codes WHERE email = ?", email)
    user = db.one("SELECT id FROM users WHERE email = ?", email)
    uid = user["id"] if user else db.run("INSERT INTO users (email, role) VALUES (?, ?)", email,
                                          "admin" if email in ADMIN_EMAILS else "user")
    token = secrets.token_urlsafe(32)
    db.run("INSERT INTO sessions (token, user_id) VALUES (?, ?)", token, uid)
    return token


def current_user(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    u = db.one("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ? AND u.banned = 0", token)
    if u:
        if not u["last_seen_at"] or u["last_seen_at"] < ts(now() - timedelta(minutes=10)):
            db.run("UPDATE users SET last_seen_at = ? WHERE id = ?", ts(now()), u["id"])
        u["is_admin"] = u["role"] == "admin" or u["email"] in ADMIN_EMAILS
    return u


def require_user(request: Request) -> dict:
    u = current_user(request)
    if not u:
        raise HTTPException(401, "请先登录")
    return u


def require_admin(request: Request) -> dict:
    u = require_user(request)
    if not u["is_admin"]:
        raise HTTPException(403, "只有管理员能看")
    return u


def logout(request: Request) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        db.run("DELETE FROM sessions WHERE token = ?", token)
