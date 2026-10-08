"""Static safety net: no undefined names, no entry point that only breaks live.

The deployed bot once crashed with

    File "bot.py", line 35, in on_startup
        scheduler.start_all()
    NameError: name 'scheduler' is not defined

…which no import-only test could ever see (the broken line runs only at startup).
`tools/static_check.py` walks the symtable of every module and reports names that
are used but never defined, imported or built in.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from static_check import check_paths, undefined_names  # noqa: E402

TARGETS = ["app", "web", "tools", "bot.py", "run.py"]


def test_checker_actually_catches_a_bot_py_crash():
    broken = (
        "from app.services import secrets_guard\n"
        "async def on_startup(client):\n"
        "    me = await client.get_me()\n"
        "    scheduler.start_all()\n"
    )
    names = [name for name, _line in undefined_names(broken, "bot.py")]
    assert names == ["scheduler"]


def test_checker_ignores_locals_imports_and_builtins():
    healthy = (
        "import os\n"
        "from app import settings\n"
        "def work(items):\n"
        "    total = 0\n"
        "    for item in items:\n"
        "        total += len(str(item))\n"
        "    return total, os.getpid(), settings\n"
    )
    assert undefined_names(healthy, "healthy.py") == []


def test_no_undefined_names_anywhere_in_the_project(capsys):
    bad = check_paths(TARGETS)
    output = capsys.readouterr().out
    assert bad == 0, f"undefined names found:\n{output}"
