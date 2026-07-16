from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import RetryAfter
import db
import core
import asyncio
from logger import get_logger

# Get logger for this module
logger = get_logger(__name__)


async def hello(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Register unknown accounts as pending and show their access status.
    """
    try:
        chat_id = str(update.effective_chat.id)
        display_name = (
            update.effective_user.full_name
            if update.effective_user
            else None
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
            logger.error(
                f"Error sending error message: {str(reply_error)}"
            )


class LeRobot:
    def __init__(self, queue):
        from telegram import Bot
        from telegram.ext import ApplicationBuilder, CommandHandler

        try:

            self.bot = Bot(db.get_parameter("telegram_token"))
            self.app = (
                ApplicationBuilder().token(db.get_parameter("telegram_token")).build()
            )

            # Create the item queue to send to telegram
            self.new_items_queue = queue

            # Handler verify if bot is running
            self.app.add_handler(CommandHandler("hello", hello))
            # Keyword handlers
            self.app.add_handler(CommandHandler("add_query", self.add_query))
            self.app.add_handler(CommandHandler("remove_query", self.remove_query))
            self.app.add_handler(CommandHandler("queries", self.queries))
            self.app.add_handler(CommandHandler("my_id", self.my_id))
            self.app.add_handler(
                CommandHandler("approve_user", self.approve_user)
            )
            self.app.add_handler(
                CommandHandler("revoke_user", self.revoke_user)
            )
            self.app.add_handler(CommandHandler("users", self.users))
            # Allowlist handlers
            self.app.add_handler(
                CommandHandler("clear_allowlist", self.clear_allowlist)
            )
            self.app.add_handler(CommandHandler("add_country", self.add_country))
            self.app.add_handler(CommandHandler("remove_country", self.remove_country))
            self.app.add_handler(CommandHandler("allowlist", self.allowlist))

            # TODO : Help command

            # TODO : Manage removals after current items have been processed.

            job_queue = self.app.job_queue
            # Set the commands
            job_queue.run_once(self.set_commands, when=1)
            # Every day we check for a new version
            job_queue.run_repeating(self.check_version, interval=86400, first=1)
            # Every second we check for new posts to send to telegram
            job_queue.run_once(self.check_telegram_queue, when=1)

            self.app.run_polling()
        except Exception as e:
            logger.error(f"Error initializing bot: {str(e)}", exc_info=True)

    async def require_approved(self, update: Update) -> bool:
        chat_id = str(update.effective_chat.id)
        display_name = (
            update.effective_user.full_name
            if update.effective_user
            else None
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
        await update.message.reply_text(
            f"Your Telegram chat ID is {chat_id}."
        )

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
            await update.message.reply_text(
                f"Approved Telegram account {chat_id}."
            )
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
            await update.message.reply_text(
                "The account could not be approved."
            )

    async def revoke_user(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self.require_admin(update):
            return

        if not context.args:
            await update.message.reply_text(
                "Usage: /revoke_user CHAT_ID"
            )
            return

        chat_id = context.args[0]
        if db.revoke_telegram_user(chat_id):
            await update.message.reply_text(
                f"Revoked Telegram account {chat_id}."
            )
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
            lines.append(
                f"{chat_id} | {name or 'Unnamed'} | {status} | {role}"
            )

        await update.message.reply_text(
            "Telegram users:\n" + "\n".join(lines)
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
                query_list = core.get_formatted_query_list(
                    chat_id=chat_id
                )
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
            await update.message.reply_text(
                "An error occurred while adding the query."
            )

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
                query_list = core.get_formatted_query_list(
                    chat_id=chat_id
                )
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
            query_list = core.get_formatted_query_list(
                chat_id=chat_id
            )
            await update.message.reply_text(
                f"Your current queries:\n{query_list}"
            )
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
    ):
        if chat_ids is None:
            configured_chat_id = db.get_parameter("telegram_chat_id")
            chat_ids = [configured_chat_id] if configured_chat_id else []

        if isinstance(chat_ids, (str, int)):
            chat_ids = [chat_ids]

        buttons = [[InlineKeyboardButton(text=text, url=url)]]
        if buy_url and buy_text:
            buttons.append(
                [InlineKeyboardButton(text=buy_text, url=buy_url)]
            )
        markup = InlineKeyboardMarkup(buttons)

        for chat_id in {
            str(value).strip()
            for value in chat_ids
            if value is not None and str(value).strip()
        }:
            if not db.is_telegram_user_approved(chat_id):
                logger.info(
                    "Skipping alert for unapproved Telegram chat %s.",
                    chat_id,
                )
                continue

            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=content,
                    parse_mode="HTML",
                    read_timeout=40,
                    write_timeout=40,
                    reply_markup=markup,
                )
            except RetryAfter as error:
                retry_after = error.retry_after
                logger.error(
                    "Telegram flood control for chat %s. Retrying in %s seconds.",
                    chat_id,
                    retry_after + 2,
                )
                await asyncio.sleep(retry_after + 2)
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=content,
                    parse_mode="HTML",
                    read_timeout=40,
                    write_timeout=40,
                    reply_markup=markup,
                )
            except Exception as error:
                logger.error(
                    "Error sending new post to chat %s: %s",
                    chat_id,
                    str(error),
                    exc_info=True,
                )

    async def check_version(
        self,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        try:
            should_update, current_version, latest_version, url = (
                core.check_version()
            )

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

    async def check_telegram_queue(
        self,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        try:
            while True:
                if self.new_items_queue.empty():
                    await asyncio.sleep(0.1)
                    continue

                item = self.new_items_queue.get()

                if len(item) == 6:
                    (
                        content,
                        url,
                        text,
                        buy_url,
                        buy_text,
                        chat_ids,
                    ) = item
                else:
                    content, url, text, buy_url, buy_text = item
                    chat_ids = None

                await self.send_new_post(
                    content,
                    url,
                    text,
                    buy_url,
                    buy_text,
                    chat_ids=chat_ids,
                )
        except Exception as error:
            logger.error(
                f"Error checking Telegram queue: {str(error)}",
                exc_info=True,
            )

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
                    ("clear_allowlist", "Admin: clear country allowlist"),
                    ("add_country", "Admin: add an allowed country"),
                    ("remove_country", "Admin: remove an allowed country"),
                    ("allowlist", "Admin: list allowed countries"),
                ]
            )
            logger.info("Bot commands set successfully")
        except Exception as e:
            logger.error(f"Error setting bot commands: {str(e)}", exc_info=True)
