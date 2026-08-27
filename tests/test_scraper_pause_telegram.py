import asyncio
from types import SimpleNamespace

import telegram_bot_plugin.telegram_bot as plugin


class ReplyMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


def _robot_and_update(admin=True):
    robot = plugin.LeRobot.__new__(plugin.LeRobot)
    message = ReplyMessage()
    update = SimpleNamespace(
        message=message,
        effective_chat=SimpleNamespace(id=111),
    )

    async def require_admin(_update):
        return admin

    robot.require_admin = require_admin
    return robot, update, message


def test_pause_scraper_command_accepts_only_bounded_presets(monkeypatch):
    robot, update, message = _robot_and_update()
    calls = []

    def pause_scraper(**kwargs):
        calls.append(kwargs)
        return {
            "active": True,
            "available": True,
            "remaining": kwargs["duration_seconds"],
        }

    monkeypatch.setattr(plugin.core, "pause_scraper", pause_scraper)
    asyncio.run(
        robot.pause_scraper(
            update,
            SimpleNamespace(args=["6h"]),
        )
    )
    assert calls == [{"duration_seconds": 6 * 60 * 60, "reason": "telegram_6h"}]
    assert "6 hours" in message.replies[0]

    message.replies.clear()
    asyncio.run(
        robot.pause_scraper(
            update,
            SimpleNamespace(args=["999h"]),
        )
    )
    assert len(calls) == 1
    assert message.replies == [
        "Usage: /pause_scraper 1h, /pause_scraper 6h, or /pause_scraper 24h"
    ]


def test_phone_block_and_resume_commands_are_persistent_core_controls(monkeypatch):
    robot, update, message = _robot_and_update()
    pause_calls = []
    resume_calls = []

    def pause_scraper(**kwargs):
        pause_calls.append(kwargs)
        return {"active": True, "available": True, "remaining": None}

    monkeypatch.setattr(plugin.core, "pause_scraper", pause_scraper)
    monkeypatch.setattr(
        plugin.core,
        "resume_scraper",
        lambda: resume_calls.append(True) or True,
    )

    asyncio.run(robot.vinted_blocked(update, SimpleNamespace(args=[])))
    assert pause_calls == [{"duration_seconds": None, "reason": "phone_blocked"}]
    assert "until you manually resume" in message.replies[-1]

    asyncio.run(robot.resume_scraper(update, SimpleNamespace(args=[])))
    assert resume_calls == [True]
    assert "request limit" in message.replies[-1]


def test_scraper_status_reports_pause_cooldown_and_effective_floor(monkeypatch):
    robot, update, message = _robot_and_update()
    monkeypatch.setattr(
        plugin.core,
        "get_scraper_pause",
        lambda: {
            "active": True,
            "available": True,
            "remaining": 3600,
            "reason": "phone_blocked",
        },
    )
    monkeypatch.setattr(
        plugin.core,
        "get_scraper_cooldown",
        lambda: {"active": False, "level": 0},
    )
    monkeypatch.setattr(plugin.db, "get_parameter", lambda key: "15")

    asyncio.run(robot.scraper_status(update, SimpleNamespace(args=[])))
    reply = message.replies[0]
    assert "phone blocked" in reply
    assert "HTTP protection: ready" in reply
    assert "Minimum Vinted request gap: 60 seconds" in reply


def test_scraper_controls_are_admin_only(monkeypatch):
    robot, update, message = _robot_and_update(admin=False)
    calls = []
    monkeypatch.setattr(
        plugin.core,
        "pause_scraper",
        lambda **kwargs: calls.append(kwargs),
    )

    asyncio.run(robot.vinted_blocked(update, SimpleNamespace(args=[])))
    assert calls == []
    assert message.replies == []


def test_scraper_safety_commands_are_published_in_telegram_menu():
    robot = plugin.LeRobot.__new__(plugin.LeRobot)
    commands = []

    class Bot:
        async def set_my_commands(self, values):
            commands.extend(values)

    robot.bot = Bot()
    asyncio.run(robot.set_commands(None))
    names = {name for name, _description in commands}
    assert {
        "pause_scraper",
        "vinted_blocked",
        "resume_scraper",
        "scraper_status",
    } <= names
