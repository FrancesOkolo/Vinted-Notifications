import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from telegram_bot_plugin.telegram_bot import LeRobot, _build_notification_markup


def _button_rows(markup):
    return [list(row) for row in markup.inline_keyboard]


def test_vinted_item_alert_offers_whatsapp_copy_and_subscription_actions():
    source_url = (
        "https://www.vinted.co.uk/items/123-blue-coat"
        "?referrer=catalog&tracking=private#details"
    )
    canonical_url = "https://www.vinted.co.uk/items/123"

    rows = _button_rows(
        _build_notification_markup(
            source_url,
            "Open Vinted",
            query_id=42,
            polling_enabled=True,
        )
    )

    assert [button.text for row in rows for button in row] == [
        "Open Vinted",
        "Share on WhatsApp",
        "Copy item link",
        "Unsubscribe from this search",
    ]
    assert rows[0][0].url == source_url

    whatsapp_url = urlsplit(rows[1][0].url)
    assert whatsapp_url.scheme == "https"
    assert whatsapp_url.netloc == "wa.me"
    assert parse_qs(whatsapp_url.query)["text"] == [canonical_url]
    assert rows[1][1].copy_text.text == canonical_url
    assert rows[2][0].callback_data == "unsubscribe:42"


def test_non_item_link_keeps_original_action_without_share_controls():
    rows = _button_rows(
        _build_notification_markup(
            "https://github.com/FrancesOkolo/Vinted-Notifications",
            "Open GitHub",
            query_id=7,
            polling_enabled=True,
        )
    )

    assert [button.text for row in rows for button in row] == [
        "Open GitHub",
        "Unsubscribe from this search",
    ]
    assert all(button.copy_text is None for row in rows for button in row)


def test_send_only_item_alert_keeps_cross_app_share_actions_without_callbacks():
    rows = _button_rows(
        _build_notification_markup(
            "https://www.vinted.co.uk/items/99",
            "Open Vinted",
            query_id=99,
            polling_enabled=False,
        )
    )

    assert [button.text for row in rows for button in row] == [
        "Open Vinted",
        "Share on WhatsApp",
        "Copy item link",
    ]
    assert all(button.callback_data is None for row in rows for button in row)


def test_invalid_item_like_urls_do_not_gain_share_controls():
    invalid_urls = [
        "http://www.vinted.co.uk/items/1",
        "https://vinted.co.uk.evil.example/items/1",
        "https://user@www.vinted.co.uk/items/1",
        "https://www.vinted.co.uk/catalog?search_text=coat",
        "https://www.vinted.co.uk/items/not-a-number",
        "https://www.vinted.co.uk/items/1\nhttps://evil.example",
    ]

    for url in invalid_urls:
        rows = _button_rows(_build_notification_markup(url, "Open"))
        assert [button.text for row in rows for button in row] == ["Open"]


def test_subscription_toggle_preserves_static_share_rows():
    markup = _build_notification_markup(
        "https://www.vinted.co.uk/items/500-lamp",
        "Open Vinted",
        query_id=8,
        polling_enabled=True,
    )
    original_rows = _button_rows(markup)

    class Callback:
        def __init__(self):
            self.message = SimpleNamespace(reply_markup=markup)
            self.edited_markup = None

        async def edit_message_reply_markup(self, updated_markup):
            self.edited_markup = updated_markup

    callback = Callback()
    robot = LeRobot.__new__(LeRobot)
    asyncio.run(
        robot._update_subscription_button(
            callback,
            query_id=8,
            subscribed=False,
        )
    )
    updated_rows = _button_rows(callback.edited_markup)

    assert [button.to_dict() for row in updated_rows[:2] for button in row] == [
        button.to_dict() for row in original_rows[:2] for button in row
    ]
    assert updated_rows[2][0].text == "Resubscribe to this search"
    assert updated_rows[2][0].callback_data == "resubscribe:8"
