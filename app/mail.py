import logging
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr

from .config import SMTP_FROM, SMTP_HOST, SMTP_PASSWORD, SMTP_PORT, SMTP_USER

log = logging.getLogger("learnwise.mail")


def send(to: str, subject: str, text: str) -> None:
    """没配 SMTP 时只打印到日志（本机开发用）。"""
    if not SMTP_HOST:
        log.warning("SMTP 未配置，邮件只打印：to=%s subject=%s\n%s", to, subject, text)
        return
    msg = MIMEText(text, "plain", "utf-8")
    name, addr = parseaddr(SMTP_FROM)
    msg["From"] = formataddr((name or "LearnWise", addr or SMTP_USER))
    msg["To"] = to
    msg["Subject"] = subject
    cls = smtplib.SMTP_SSL if SMTP_PORT == 465 else smtplib.SMTP
    with cls(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        if SMTP_PORT != 465:
            s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.sendmail(addr or SMTP_USER, [to], msg.as_string())
