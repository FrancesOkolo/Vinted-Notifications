from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import RetryAfter, NetworkError, BadRequest, Conflict
import db
import core
import asyncio
import time
import json
from logger import get_logger

# Get logger for this module
logger = get_logger(__name__)

# Delivery retry policy. A notification tool must not drop an alert on a
# transient blip, so failed sends are retried with backoff before giving up.
SEND_MAX_ATTEMPTS = 4
SEND_BACKOFFS = (2, 5, 10)  # seconds to wait before attempts 2, 3, 4
OUTBOX_MAX_ATTEMPTS = 10


def _eligible_outbox_chat_ids(query_id, queued_chat_ids):
    """Filter an alert against current query/subscription state.

    ``None`` means the database state could not be read. Callers must retain
    the durable row in that case rather than mistaking an operational failure
    for an intentional pause or unsubscribe.
    """
    if query_id is None:
        return queued_chat_ids
    state = db.get_query_delivery_state(query_id)
    if state is None:
        return None
    enabled, subscribers = state
    if not enabled:
        return []

    current_subscribers = {str(value) for value in subscribers}
    return [
        chat_id
        for chat_id in (queued_chat_ids or [])
        if str(chat_id) in current_subscribers
    ]


def _defer_outbox_notification(notification_id, attempts):
    """Back off one failed outbox row without creating a tight retry loop."""
    attempts = int(attempts or 0) + 1
    if attempts >= OUTBOX_MAX_ATTEMPTS:
        if not db.delete_notification(notification_id):
            logger.error(
                "Could not remove exhausted notification %s; stopping this "
                "outbox pass to avoid duplicate delivery.",
                notification_id,
            )
            return False
        logger.error(
            "Giving up on notification %s after %s attempts.",
            notification_id,
            attempts,
        )
        return True

    backoff = min(60 * attempts, 600)
    if not db.reschedule_notification(notification_id, attempts, time.time() + backoff):
        logger.error(
            "Could not reschedule notification %s; stopping this outbox pass "
            "to avoid an immediate retry loop.",
            notification_id,
        )
        return False
    logger.warning(
        "Notification %s not delivered (attempt %s); retrying in %ss.",
        notification_id,
        attempts,
        backoff,
    )
    return True


def is_retryable_telegram_error(error):
    """True for transient errors worth retrying.

    telegram.error.NetworkError covers timeouts and "Bad Gateway"-style
    upstream hiccups. Note BadRequest is a *subclass* of NetworkError in
    python-telegram-bot, but it signals a permanent client problem (invalid
    chat id, message too long, malformed HTML) that will never succeed, so it
    is excluded. Forbidden (bot blocked) is not a NetworkError and is likewise
    not retried.
    """
    if isinstance(error, BadRequest):
        return False
    return isinstance(error, NetworkError)


async def hello(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Register unknown accounts as pending and show their access status.
    """
    try:
        chat_id = str(update.effective_chat.id)
        display_name = (
            update.effective_user.full_name if update.effective_user else None
        )

        db.register_telegram_user(chat_id, display_name)
        user = db.get_telegram_user(chat_id)
        version = db.get_parameter("version")

        if user and user[2] == "approved":
            role = "administrator" if int(user[3]) == 1 else "approved user"
            await update.message.reply_text(
                f"Hello {update.effective_user.first_name}! "
                f"Vinted-Notifications {version} is running.\n"
                f"Your Telegram chat ID is {chat_id}.\n"
                f"Access: {role}."
            )
        else:
            await update.message.reply_text(
                "Your access request has been recorded.\n"
                f"Your Telegram chat ID is {chat_id}.\n"
                "Ask the administrator to approve this ID using:\n"
                f"/approve_user {chat_id}"
            )
    except Exception as error:
        logger.error(
            f"Error in hello command: {str(error)}",
            exc_info=True,
        )
        try:
            await update.message.reply_text(
                "An error occurred. Please try again later."
            )
        except Exception as reply_error:
            logger.error(f"Error sending error message: {str(reply_error)}")


class LeRobot:
    def __init__(self, queue, polling_enabled=True):
        from telegram import Bot

        try:
            self.polling_enabled = polling_enabled
            self.bot = Bot(db.get_parameter("telegram_token"))
            # Create the item queue to send to telegram
            self.new_items_queue = queue
            # Throttle repeated getUpdates-conflict logs (see on_error).
            self._last_conflict_log = 0.0

            if not polling_enabled:
                self.app = None
                logger.info(
                    "Telegram send-only mode enabled: notifications will be "
                    "delivered, but getUpdates command polling is disabled."
                )
                asyncio.run(self.run_send_only())
                return

            from telegram.ext import (
                ApplicationBuilder,
                CallbackQueryHandler,
                CommandHandler,
            )

            self.app = (
                ApplicationBuilder().token(db.get_parameter("telegram_token")).build()
            )

            # Handler verify if bot is running
            self.app.add_handler(CommandHandler("hello", hello))
            # Keyword handlers
            self.app.add_handler(CommandHandler("add_query", self.add_query))
            self.app.add_handler(CommandHandler("remove_query", self.remove_query))
            self.app.add_handler(CommandHandler("queries", self.queries))
            self.app.add_handler(CommandHandler("my_id", self.my_id))
            self.app.add_handler(CommandHandler("approve_user", self.approve_user))
            self.app.add_handler(CommandHandler("revoke_user", self.revoke_user))
            self.app.add_handler(CommandHandler("users", self.users))
            self.app.add_handler(
                CommandHandler("copy_my_queries", self.copy_my_queries)
            )
            # Allowlist handlers
            self.app.add_handler(
                CommandHandler("clear_allowlist", self.clear_allowlist)
            )
            self.app.add_handler(CommandHandler("add_country", self.add_country))
            self.app.add_handler(CommandHandler("remove_country", self.remove_country))
            self.app.add_handler(CommandHandler("allowlist", self.allowlist))
            self.app.add_handler(
                CallbackQueryHandler(
                    self.unsubscribe_query,
                    pattern=r"^unsubscribe:\d+$",
                )
            )
            self.app.add_handler(
                CallbackQueryHandler(
                    self.resubscribe_query,
                    pattern=r"^resubscribe:\d+$",
                )
            )

            # TODO : Help command

            # TODO : Manage removals after current items have been processed.

            # Handle otherwise-unhandled errors (e.g. transient polling
            # NetworkErrors) so they don't dump full tracebacks to the log.
            self.app.add_error_handler(self.on_error)

            job_queue = self.app.job_queue
            # Set the commands
            job_queue.run_once(self.set_commands, when=1)
            # Every day we check for a new version
            job_queue.run_repeating(self.check_version, interval=86400, first=1)
            # Deliver persisted notifications from the outbox. A *repeating*
            # job (rather than one long-lived loop) means a network stall or a
            # cancelled tick can only delay a single pass — the next tick always
            # fires, so delivery can never silently stop for the process's life.
            job_queue.run_repeating(
                self.drain_outbox,
                interval=10,
                first=1,
                job_kwargs={"max_instances": 1, "coalesce": True},
            )

            # drop_pending_updates avoids replaying a backlog of old commands
            # that piled up while the bot was stopped.
            self.app.run_polling(drop_pending_updates=True)
        except Exception as e:
            logger.error(f"Error initializing bot: {str(e)}", exc_info=True)

    async def run_send_only(self):
        """Deliver the outbox without receiving commands via getUpdates.

        drain_outbox is a finite single pass (see its docstring), so loop it
        here to keep delivering; each pass clears the current backlog and then
        sleeps briefly before checking again.
        """
        await self.bot.initialize()
        try:
            while True:
                await self.drain_outbox(None)
                await asyncio.sleep(10)
        finally:
            await self.bot.shutdown()

    async def on_error(self, update, context):
        """Global error handler for the Telegram application.

        Transient network errors are logged as a single warning line; anything
        else is logged as an error with a traceback for investigation.
        """
        error = context.error
        if is_retryable_telegram_error(error):
            logger.warning("Transient Telegram network error: %s", error)
        elif isinstance(error, Conflict):
            # Another instance is polling the same bot token. The polling loop
            # retries fast, so log a concise, actionable message at most once a
            # minute instead of a traceback on every retry.
            now = time.monotonic()
            if now - self._last_conflict_log > 60:
                self._last_conflict_log = now
                logger.error(
                    "Telegram getUpdates conflict: another bot instance is "
                    "polling the same token. Ensure only one instance runs "
                    "(e.g. stop the local copy if the server is live)."
                )
        else:
            logger.error("Unhandled Telegram error: %s", error, exc_info=error)

    async def require_approved(self, update: Update) -> bool:
        chat_id = str(update.effective_chat.id)
        display_name = (
            update.effective_user.full_name if update.effective_user else None
        )
        db.register_telegram_user(chat_id, display_name)

        if db.is_telegram_user_approved(chat_id):
            return True

        await update.message.reply_text(
            "This Telegram account is not approved yet.\n"
            f"Your chat ID is {chat_id}. Send /hello for instructions."
        )
        return False

    async def require_admin(self, update: Update) -> bool:
        if not await self.require_approved(update):
            return False

        chat_id = str(update.effective_chat.id)
        if db.is_telegram_user_admin(chat_id):
            return True

        await update.message.reply_text(
            "This command is restricted to the administrator."
        )
        return False

    async def my_id(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        chat_id = str(update.effective_chat.id)
        await update.message.reply_text(f"Your Telegram chat ID is {chat_id}.")

    async def approve_user(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self.require_admin(update):
            return

        if not context.args:
            await update.message.reply_text(
                "Usage: /approve_user CHAT_ID optional name"
            )
            return

        chat_id = context.args[0]
        display_name = " ".join(context.args[1:]).strip() or None

        if not chat_id.lstrip("-").isdigit():
            await update.message.reply_text("Invalid Telegram chat ID.")
            return

        if db.approve_telegram_user(chat_id, display_name):
            await update.message.reply_text(f"Approved Telegram account {chat_id}.")
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "Your Vinted Notifications access has been approved.\n"
                        "Use /add_query, /queries and /remove_query."
                    ),
                )
            except Exception:
                logger.warning(
                    "User approved, but the approval message could not be "
                    "delivered to chat %s.",
                    chat_id,
                    exc_info=True,
                )
        else:
            await update.message.reply_text("The account could not be approved.")

    async def revoke_user(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self.require_admin(update):
            return

        if not context.args:
            await update.message.reply_text("Usage: /revoke_user CHAT_ID")
            return

        chat_id = context.args[0]
        if db.revoke_telegram_user(chat_id):
            await update.message.reply_text(f"Revoked Telegram account {chat_id}.")
        else:
            await update.message.reply_text(
                "Account not found, already revoked, or is the administrator."
            )

    async def users(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self.require_admin(update):
            return

        users = db.get_telegram_users()
        if not users:
            await update.message.reply_text("No Telegram users registered.")
            return

        lines = []
        for chat_id, name, status, is_admin in users:
            role = "admin" if int(is_admin) == 1 else "user"
            lines.append(f"{chat_id} | {name or 'Unnamed'} | {status} | {role}")

        await update.message.reply_text("Telegram users:\n" + "\n".join(lines))

    async def copy_my_queries(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Copy the administrator's subscriptions to an approved account."""
        if not await self.require_admin(update):
            return

        if not context.args:
            await update.message.reply_text("Usage: /copy_my_queries CHAT_ID")
            return

        target_chat_id = context.args[0]
        if not target_chat_id.lstrip("-").isdigit():
            await update.message.reply_text("Invalid Telegram chat ID.")
            return

        if not db.is_telegram_user_approved(target_chat_id):
            await update.message.reply_text(
                "That Telegram account is not approved yet. " "Use /approve_user first."
            )
            return

        source_chat_id = str(update.effective_chat.id)
        copied = db.copy_query_subscriptions(
            source_chat_id,
            target_chat_id,
        )
        if copied is None:
            await update.message.reply_text(
                "The query subscriptions could not be copied."
            )
            return

        await update.message.reply_text(
            f"Copied {copied} new query subscription(s) to "
            f"{target_chat_id}. Existing subscriptions were kept."
        )

        try:
            await self.bot.send_message(
                chat_id=target_chat_id,
                text=(
                    "Your administrator shared their Vinted searches with "
                    "your account.\n"
                    "Use /queries to review them and /remove_query NUMBER "
                    "to remove any you do not want."
                ),
            )
        except Exception:
            logger.warning(
                "Subscriptions copied, but the target account could not be "
                "notified.",
                exc_info=True,
            )

    ### QUERIES ###

    # Add a query to the db
    async def add_query(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self.require_approved(update):
            return

        try:
            if not context.args:
                await update.message.reply_text(
                    "No query provided.\n"
                    "Usage: /add_query URL\n"
                    "or /add_query Name=URL"
                )
                return

            supplied = " ".join(context.args).strip()
            if "=http" in supplied:
                name, url = supplied.split("=", 1)
                name = name.strip() or None
            else:
                name = None
                url = supplied

            chat_id = str(update.effective_chat.id)
            message, is_new_query = core.process_query(
                url,
                name=name,
                chat_id=chat_id,
            )

            if is_new_query:
                query_list = core.get_formatted_query_list(chat_id=chat_id)
                await update.message.reply_text(
                    f"{message}\nYour current queries:\n{query_list}"
                )
            else:
                await update.message.reply_text(message)

        except (ValueError, TypeError) as error:
            await update.message.reply_text(str(error))
        except Exception as error:
            logger.error(
                f"Error adding query: {str(error)}",
                exc_info=True,
            )
            await update.message.reply_text("An error occurred while adding the query.")

    # Remove a query from the db
    async def remove_query(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self.require_approved(update):
            return

        try:
            if not context.args:
                await update.message.reply_text(
                    "No number provided. Use /queries first."
                )
                return

            requested = context.args[0].lower()
            chat_id = str(update.effective_chat.id)

            if requested == "all":
                message, success = core.process_remove_query(
                    "all",
                    chat_id=chat_id,
                )
            else:
                query_id = db.get_query_id_by_rowid(
                    requested,
                    chat_id=chat_id,
                )
                if query_id is None:
                    await update.message.reply_text(
                        "That query number is not in your list."
                    )
                    return

                message, success = core.process_remove_query(
                    str(query_id),
                    chat_id=chat_id,
                )

            if success:
                query_list = core.get_formatted_query_list(chat_id=chat_id)
                await update.message.reply_text(
                    f"{message}\nYour current queries:\n{query_list}"
                )
            else:
                await update.message.reply_text(message)

        except Exception as error:
            logger.error(
                f"Error removing query: {str(error)}",
                exc_info=True,
            )
            await update.message.reply_text(
                "An error occurred while removing the query."
            )

    # get all queries from the db
    async def queries(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self.require_approved(update):
            return

        try:
            chat_id = str(update.effective_chat.id)
            query_list = core.get_formatted_query_list(chat_id=chat_id)
            await update.message.reply_text(f"Your current queries:\n{query_list}")
        except Exception as error:
            logger.error(
                f"Error retrieving queries: {str(error)}",
                exc_info=True,
            )
            await update.message.reply_text(
                "An error occurred while retrieving your queries."
            )

    ### ALLOWLIST ###

    async def clear_allowlist(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self.require_admin(update):
            return
        try:
            db.clear_allowlist()
            await update.message.reply_text(
                "Allowlist cleared. All countries are allowed."
            )
        except Exception as e:
            logger.error(f"Error clearing allowlist: {str(e)}", exc_info=True)
            try:
                await update.message.reply_text(
                    "An error occurred while clearing the allowlist. Please try again later."
                )
            except Exception as e2:
                logger.error(f"Error sending error message: {str(e2)}")

    async def add_country(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self.require_admin(update):
            return
        try:
            country = context.args
            if not country:
                await update.message.reply_text("No country provided")
                return

            # Process the country using the core function
            message, country_list = core.process_add_country(" ".join(country))

            await update.message.reply_text(
                f"{message} Current allowlist: {country_list}"
            )
        except Exception as e:
            logger.error(f"Error adding country to allowlist: {str(e)}", exc_info=True)
            try:
                await update.message.reply_text(
                    "An error occurred while adding the country to the allowlist. Please try again later."
                )
            except Exception as e2:
                logger.error(f"Error sending error message: {str(e2)}")

    async def remove_country(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self.require_admin(update):
            return
        try:
            country = context.args
            if not country:
                await update.message.reply_text("No country provided")
                return

            # Process the country using the core function
            message, country_list = core.process_remove_country(" ".join(country))

            await update.message.reply_text(
                f"{message} Current allowlist: {country_list}"
            )
        except Exception as e:
            logger.error(
                f"Error removing country from allowlist: {str(e)}", exc_info=True
            )
            try:
                await update.message.reply_text(
                    "An error occurred while removing the country from the allowlist. Please try again later."
                )
            except Exception as e2:
                logger.error(f"Error sending error message: {str(e2)}")

    async def allowlist(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self.require_admin(update):
            return
        try:
            if db.get_allowlist() == 0:
                await update.message.reply_text(
                    "No allowlist set. All countries are allowed."
                )
            else:
                await update.message.reply_text(
                    f"Current allowlist: {db.get_allowlist()}"
                )
        except Exception as e:
            logger.error(f"Error retrieving allowlist: {str(e)}", exc_info=True)
            try:
                await update.message.reply_text(
                    "An error occurred while retrieving the allowlist. Please try again later."
                )
            except Exception as e2:
                logger.error(f"Error sending error message: {str(e2)}")

    ### TELEGRAM SPECIFIC FUNCTIONS ###

    async def send_new_post(
        self,
        content,
        url,
        text,
        buy_url=None,
        buy_text=None,
        chat_ids=None,
        query_id=None,
    ):
        if chat_ids is None:
            configured_chat_id = db.get_parameter("telegram_chat_id")
            chat_ids = [configured_chat_id] if configured_chat_id else []

        if isinstance(chat_ids, (str, int)):
            chat_ids = [chat_ids]

        # Only attach buttons when a link is supplied. Watchdog/status
        # messages pass url=None and send as plain text.
        buttons = []
        if url and text:
            buttons.append([InlineKeyboardButton(text=text, url=url)])
        if buy_url and buy_text:
            buttons.append([InlineKeyboardButton(text=buy_text, url=buy_url)])
        if query_id is not None and self.polling_enabled:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Unsubscribe from this search",
                        callback_data=f"unsubscribe:{int(query_id)}",
                    )
                ]
            )
        markup = InlineKeyboardMarkup(buttons) if buttons else None

        normalised_chat_ids = list(
            dict.fromkeys(
                str(value).strip()
                for value in chat_ids
                if value is not None and str(value).strip()
            )
        )
        approval_states = {}
        for chat_id in normalised_chat_ids:
            approval = db.get_telegram_user_approval_state(chat_id)
            if approval is None:
                logger.error(
                    "Could not verify Telegram approval for chat %s; retaining "
                    "the notification for a later attempt.",
                    chat_id,
                )
                return None
            approval_states[chat_id] = approval

        all_delivered = True
        for chat_id in normalised_chat_ids:
            if not approval_states[chat_id]:
                logger.info(
                    "Skipping alert for unapproved Telegram chat %s.",
                    chat_id,
                )
                continue

            delivered = await self._send_message_with_retries(chat_id, content, markup)
            all_delivered = all_delivered and delivered

        # True when every approved recipient received it (or every supplied
        # recipient was definitively ineligible), False on a Telegram delivery
        # failure, and None when approval could not be read. The outbox retains
        # the recipient unchanged for the latter case.
        return all_delivered

    async def unsubscribe_query(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Unsubscribe only the Telegram account that clicked the button."""
        callback = update.callback_query
        if callback is None:
            return

        try:
            query_id = int((callback.data or "").split(":", 1)[1])
        except (IndexError, TypeError, ValueError):
            await callback.answer(
                "This unsubscribe button is invalid.",
                show_alert=True,
            )
            return

        chat_id = str(update.effective_chat.id)
        if not db.is_telegram_user_approved(chat_id):
            await callback.answer(
                "This Telegram account is not approved.",
                show_alert=True,
            )
            return

        removed = db.remove_query_subscription(query_id, chat_id)
        if removed is None:
            await callback.answer(
                "Could not update this subscription right now. Please try again.",
                show_alert=True,
            )
            return
        await callback.answer(
            (
                "Unsubscribed from this search."
                if removed
                else "You are already unsubscribed from this search."
            ),
            show_alert=True,
        )
        await self._update_subscription_button(
            callback,
            query_id,
            subscribed=False,
        )

    async def resubscribe_query(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Restore only the Telegram account that clicked Resubscribe."""
        callback = update.callback_query
        if callback is None:
            return

        try:
            query_id = int((callback.data or "").split(":", 1)[1])
        except (IndexError, TypeError, ValueError):
            await callback.answer(
                "This resubscribe button is invalid.",
                show_alert=True,
            )
            return

        chat_id = str(update.effective_chat.id)
        if not db.is_telegram_user_approved(chat_id):
            await callback.answer(
                "This Telegram account is not approved.",
                show_alert=True,
            )
            return

        added = db.add_query_subscription(query_id, chat_id)
        if added is None:
            await callback.answer(
                "Could not resubscribe right now. The search may no longer be "
                "available; please try again.",
                show_alert=True,
            )
            return

        await callback.answer(
            (
                "Resubscribed to this search."
                if added
                else "You are already subscribed to this search."
            ),
            show_alert=True,
        )
        await self._update_subscription_button(
            callback,
            query_id,
            subscribed=True,
        )

    async def _update_subscription_button(
        self,
        callback,
        query_id,
        subscribed,
    ) -> None:
        """Turn the notification's subscription action into a toggle."""
        markup = callback.message.reply_markup if callback.message else None
        if not markup:
            return

        action_prefixes = ("unsubscribe:", "resubscribe:")
        action_text = (
            "Unsubscribe from this search"
            if subscribed
            else "Resubscribe to this search"
        )
        action_data = (
            f"unsubscribe:{query_id}" if subscribed else f"resubscribe:{query_id}"
        )
        updated_rows = [
            [
                (
                    InlineKeyboardButton(
                        text=action_text,
                        callback_data=action_data,
                    )
                    if (
                        button.callback_data
                        and button.callback_data.startswith(action_prefixes)
                    )
                    else button
                )
                for button in row
            ]
            for row in markup.inline_keyboard
        ]
        try:
            await callback.edit_message_reply_markup(InlineKeyboardMarkup(updated_rows))
        except BadRequest:
            logger.debug(
                "Subscription changed, but the Telegram message buttons "
                "could not be updated.",
                exc_info=True,
            )

    async def _send_message_with_retries(self, chat_id, content, markup):
        """Send one message, retrying transient failures with backoff.

        Returns True on success, False once the message is given up on. Only
        transient errors are retried; permanent client errors (bad chat id,
        blocked bot, malformed HTML) fail fast without wasting retries.
        """
        attempt = 0
        while True:
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=content,
                    parse_mode="HTML",
                    read_timeout=40,
                    write_timeout=40,
                    reply_markup=markup,
                )
                return True
            except RetryAfter as error:
                wait_seconds = error.retry_after + 2
                if attempt >= SEND_MAX_ATTEMPTS - 1:
                    logger.error(
                        "Gave up delivering to chat %s after flood control "
                        "(%s attempts).",
                        chat_id,
                        attempt + 1,
                    )
                    return False
                logger.warning(
                    "Telegram flood control for chat %s; waiting %ss "
                    "(attempt %s/%s).",
                    chat_id,
                    wait_seconds,
                    attempt + 1,
                    SEND_MAX_ATTEMPTS,
                )
                await asyncio.sleep(wait_seconds)
                attempt += 1
            except Exception as error:
                if not is_retryable_telegram_error(error):
                    logger.error(
                        "Permanent error delivering to chat %s (not retried): %s",
                        chat_id,
                        error,
                        exc_info=True,
                    )
                    return False
                if attempt >= SEND_MAX_ATTEMPTS - 1:
                    logger.error(
                        "Failed to deliver to chat %s after %s attempts: %s",
                        chat_id,
                        attempt + 1,
                        error,
                    )
                    return False
                wait_seconds = SEND_BACKOFFS[min(attempt, len(SEND_BACKOFFS) - 1)]
                logger.warning(
                    "Transient error delivering to chat %s (attempt %s/%s): "
                    "%s; retrying in %ss.",
                    chat_id,
                    attempt + 1,
                    SEND_MAX_ATTEMPTS,
                    error,
                    wait_seconds,
                )
                await asyncio.sleep(wait_seconds)
                attempt += 1

    async def check_version(
        self,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        try:
            should_update, current_version, latest_version, url = core.check_version()

            if not should_update:
                admin_chat_id = db.get_parameter("telegram_chat_id")
                await self.send_new_post(
                    (
                        f"Version {latest_version} is now available. "
                        "Please update the bot."
                    ),
                    url,
                    "Open GitHub",
                    chat_ids=[admin_chat_id],
                )
        except Exception as error:
            logger.error(
                f"Error checking for new version: {str(error)}",
                exc_info=True,
            )

    # A single delivery must never block the queue forever. If the network
    # wedges mid-send, abandon that attempt and let the retry/backoff path
    # requeue it rather than freezing every other pending alert behind it.
    _OUTBOX_SEND_TIMEOUT = 120

    async def drain_outbox(self, context: ContextTypes.DEFAULT_TYPE):
        """Deliver all currently-due outbox notifications, then return.

        Scheduled to run repeatedly (see ``run``), so this is a single finite
        pass rather than an immortal loop. A network stall or a cancelled tick
        can therefore only delay delivery until the next scheduled run — it can
        never silently kill delivery for the life of the process. Each
        recipient is acknowledged in SQLite immediately after a successful
        send. If another recipient fails, only that failed recipient remains for
        the retry, preventing duplicate alerts to people who already received
        the message. Because the outbox lives in the database, undelivered
        notifications survive a restart.
        """
        while True:
            try:
                due = db.get_due_notifications(limit=10)
            except Exception:
                logger.error("Could not read the notification outbox.", exc_info=True)
                return

            if not due:
                return

            for (
                notif_id,
                content,
                url,
                button_text,
                chat_ids_json,
                query_id,
                attempts,
            ) in due:
                if chat_ids_json is None:
                    configured_chat_id = db.get_parameter("telegram_chat_id")
                    if not configured_chat_id:
                        logger.error(
                            "Notification %s has no stored recipients and no "
                            "configured fallback recipient.",
                            notif_id,
                        )
                        if not _defer_outbox_notification(notif_id, attempts):
                            return
                        continue
                    chat_ids = [configured_chat_id]
                else:
                    try:
                        chat_ids = json.loads(chat_ids_json)
                    except (TypeError, ValueError):
                        logger.error(
                            "Notification %s has invalid recipient data; retaining "
                            "it without guessing a replacement recipient.",
                            notif_id,
                        )
                        if not _defer_outbox_notification(notif_id, attempts):
                            return
                        continue

                if not isinstance(chat_ids, (list, str, int)) or isinstance(
                    chat_ids, bool
                ):
                    logger.error(
                        "Notification %s has an unsupported recipient format; "
                        "retaining it without attempting delivery.",
                        notif_id,
                    )
                    if not _defer_outbox_notification(notif_id, attempts):
                        return
                    continue

                if isinstance(chat_ids, (str, int)):
                    chat_ids = [chat_ids]

                # Normalise legacy rows and discard duplicate/empty recipients.
                chat_ids = list(
                    dict.fromkeys(
                        str(value).strip()
                        for value in chat_ids
                        if value is not None and str(value).strip()
                    )
                )

                if query_id is not None:
                    eligible_chat_ids = _eligible_outbox_chat_ids(query_id, chat_ids)
                    if eligible_chat_ids is None:
                        logger.error(
                            "Could not verify recipients for notification %s; "
                            "retaining it for the next outbox pass.",
                            notif_id,
                        )
                        return
                    chat_ids = eligible_chat_ids
                    if not chat_ids:
                        logger.info(
                            "Dropping queued notification %s because query %s "
                            "is paused or has no remaining queued subscribers.",
                            notif_id,
                            query_id,
                        )
                recipients_saved = db.set_notification_recipients(notif_id, chat_ids)
                if recipients_saved is None:
                    logger.error(
                        "Could not persist eligible recipients for notification %s; "
                        "stopping this outbox pass.",
                        notif_id,
                    )
                    return
                if not recipients_saved or not chat_ids:
                    continue

                remaining_count = len(chat_ids)
                for chat_id in chat_ids:
                    # Subscription state can change while a multi-recipient row
                    # is being delivered. Drop a recipient that became ineligible
                    # before their individual send.
                    current_eligibility = (
                        _eligible_outbox_chat_ids(query_id, [chat_id])
                        if query_id is not None
                        else [chat_id]
                    )
                    if current_eligibility is None:
                        logger.error(
                            "Could not recheck recipient %s for notification %s; "
                            "retaining it for the next outbox pass.",
                            chat_id,
                            notif_id,
                        )
                        return
                    if not current_eligibility:
                        remaining_count = db.ack_notification_recipient(
                            notif_id, chat_id
                        )
                        if remaining_count is None:
                            logger.error(
                                "Could not acknowledge removed recipient for "
                                "notification %s; stopping this outbox pass.",
                                notif_id,
                            )
                            return
                        continue

                    try:
                        delivered = await asyncio.wait_for(
                            self.send_new_post(
                                content,
                                url,
                                button_text,
                                chat_ids=[chat_id],
                                query_id=query_id,
                            ),
                            timeout=self._OUTBOX_SEND_TIMEOUT,
                        )
                    except asyncio.CancelledError:
                        raise
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Delivery of notification %s timed out after %ss; "
                            "will retry this recipient.",
                            notif_id,
                            self._OUTBOX_SEND_TIMEOUT,
                        )
                        delivered = False
                    except Exception as error:
                        logger.error(
                            "Unexpected error delivering notification %s: %s",
                            notif_id,
                            error,
                            exc_info=True,
                        )
                        delivered = False

                    if delivered is None:
                        logger.error(
                            "Could not verify recipient approval for notification "
                            "%s; retaining it unchanged for the next outbox pass.",
                            notif_id,
                        )
                        return
                    if delivered:
                        remaining_count = db.ack_notification_recipient(
                            notif_id, chat_id
                        )
                        if remaining_count is None:
                            logger.error(
                                "Telegram accepted notification %s but its "
                                "recipient acknowledgement could not be saved; "
                                "stopping this outbox pass.",
                                notif_id,
                            )
                            return

                if remaining_count <= 0:
                    continue

                if not _defer_outbox_notification(notif_id, attempts):
                    return

    async def set_commands(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            await self.bot.set_my_commands(
                [
                    ("hello", "Show access status"),
                    ("my_id", "Show your Telegram chat ID"),
                    ("add_query", "Add a Vinted search"),
                    ("remove_query", "Remove one of your searches"),
                    ("queries", "List your searches"),
                    ("approve_user", "Admin: approve an account"),
                    ("revoke_user", "Admin: revoke an account"),
                    ("users", "Admin: list bot accounts"),
                    ("copy_my_queries", "Admin: share your searches"),
                    ("clear_allowlist", "Admin: clear country allowlist"),
                    ("add_country", "Admin: add an allowed country"),
                    ("remove_country", "Admin: remove an allowed country"),
                    ("allowlist", "Admin: list allowed countries"),
                ]
            )
            logger.info("Bot commands set successfully")
        except Exception as e:
            logger.error(f"Error setting bot commands: {str(e)}", exc_info=True)
