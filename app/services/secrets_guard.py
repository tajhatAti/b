"""Secrets watchdog.

The owner edits `config.env` directly (that is normal), but the repository is
**public** — anything committed there is visible to the whole internet, forever.
This little guard notices it and says so out loud at boot:

    ⚠️ config.env is tracked by git and contains BOT_TOKEN, STRING_SESSION …

It is deliberately read-only: it never deletes or rewrites anything. The fix
(revoke the token, remove the file from the repo, use hosting env vars) is a
one-minute human decision, so we only inform — at startup and once a day to the
admins on Telegram.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from app import config as cfg
from app.logger import log

#: values that must never end up in a public repository
SECRET_KEYS = ("BOT_TOKEN", "API_HASH", "STRING_SESSION", "WEB_PASS", "WEB_SECRET")
PRIVATE_KEYS = ("API_ID", "ADMIN_IDS", "PAY_BKASH", "PAY_NAGAD", "PAY_ROCKET")


def _git_tracked(repo_dir: Path, name: str) -> bool | None:
    """True/False when we can ask git, None when git (or the repo) is absent."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(repo_dir), "ls-files", "--error-unmatch", name],
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        return True
    # 128 = not a git repo / no such file; anything else = file simply untracked
    return None if b"fatal" in result.stderr.lower() else False


def _git_history_names(repo_dir: Path, name: str, limit: int = 200) -> bool:
    """True when `name` appears in commit history — even after `git rm`.

    A file removed today is still readable in yesterday's commit, so the tokens
    inside it must be revoked (that part is a human decision — we only tell).
    """
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(repo_dir), "log", f"--max-count={limit}",
             "--name-only", "--pretty=format:"],
            capture_output=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return name.encode() in result.stdout


def _env_file_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def audit(repo_dir: Path | None = None, env_file: Path | None = None) -> dict:
    """Look for secrets that are committed / publicly visible.

    Returns {"tracked": bool, "secrets": [...], "private": [...], "path": str}
    """
    repo_dir = Path(repo_dir or Path(cfg.BASE_DIR))
    env_file = Path(env_file or Path(os.getenv("BOT_ENV_FILE", repo_dir / "config.env")))
    tracked = _git_tracked(repo_dir, env_file.name)
    in_history = _git_history_names(repo_dir, env_file.name) if tracked is not None else False
    values = _env_file_values(env_file)
    present = {key for key, value in values.items() if value and value.lower() not in
               ("your_token_here", "change_me")}
    return {
        "tracked": bool(tracked),
        "git_available": tracked is not None,
        "path": str(env_file),
        "secrets": sorted(present & set(SECRET_KEYS)),
        "private": sorted(present & set(PRIVATE_KEYS)),
        "in_environment": sorted(key for key in SECRET_KEYS if os.getenv(key)),
        "in_history": in_history,
    }


def warning_lines(report: dict) -> list[str]:
    """Human readable warnings (empty list = nothing to worry about).

    We only shout when the file is really committed: an untracked `config.env`
    with secrets is the intended setup, so it stays quiet.
    """
    if report.get("secrets") and report.get("in_history") and not report.get("tracked"):
        # The file was removed from the tree — good — but an older commit still
        # holds it, so the leaked token has to be revoked once and for all.
        return [
            f"🚨 {report['path']} আগের কোনো কমিটে গিট ইতিহাসে রয়ে গেছে "
            f"({', '.join(report['secrets'])})।",
            "   ✅ ফাইলটি এখন আর কমিট হয় না — কিন্তু পুরোনো কমিটে মানটা এখনো পড়া যায়।",
            "   ✅ তাই একবার অবশ্যই করুন: BotFather → /mybots → API Token → Revoke,",
            "      নতুন টোকেন config.env-এ বসান; Telegram → Devices → অচেনা সেশন Terminate।",
            "   বিস্তারিত: README.md → 💾 ডেটা, ব্যাকআপ ও নিরাপত্তা",
        ]
    if not report.get("secrets") or not report.get("tracked"):
        return []
    where = "আপনার GitHub রিপোতে (পাবলিক হলে সবাই দেখছে)"
    lines = [
        f"🚨 {report['path']} — এখানে থাকা গোপন তথ্য {where} আছে:",
        "   " + ", ".join(report["secrets"]),
        "   ✅ যা করবেন: BotFather → /mybots → API Token → Revoke, নতুন টোকেন দিন;",
        "   ✅ Telegram → Settings → Devices → অচেনা সেশন Terminate করুন;",
        "   ✅ `git rm --cached config.env` করে ফাইলটি রিপো থেকে বাদ দিন (ফাইলটা ডিস্কে থাকবে);",
        "   ✅ হোস্টিং প্যানেলের Environment Variables-এ টোকেনগুলো রাখুন।",
        "   বিস্তারিত: README.md → 💾 ডেটা, ব্যাকআপ ও নিরাপত্তা",
    ]
    return lines


def log_report() -> dict:
    """Check at startup and put the warning in the log."""
    report = audit()
    lines = warning_lines(report)
    for line in lines:
        log.warning(line)
    if not lines and report.get("private"):
        log.info("config.env holds private values (%s) — it is not committed, all good.",
                 ", ".join(report["private"]))
    return report


async def notify_admins_once_per_day() -> bool:
    """Send the same warning to the admins on Telegram (max once per day)."""
    from app.runtime import bot_online
    from app.services.telegram import safe_call
    from app.storage import db
    from app.utils import local_date_str

    if not bot_online():
        return False
    report = audit()
    lines = warning_lines(report)
    if not lines:
        return False
    today = local_date_str()
    if db.get_meta("secrets_warning_day") == today:
        return False
    db.set_meta("secrets_warning_day", today)

    from app.runtime import bot
    text = "<b>⚠️ নিরাপত্তা সতর্কতা</b>\n\n" + "\n".join(lines)
    for admin_id in cfg.ADMIN_IDS[:3]:
        try:
            await safe_call(bot.send_message, admin_id, text, what="secrets_warning",
                            retries=1, raise_after_retries=False)
        except Exception:
            pass
    return True
