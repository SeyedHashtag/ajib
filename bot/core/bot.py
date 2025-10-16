from __future__ import annotations

"""
AJIB Telegram Bot runner (aiogram-based)

- Loads configuration from .env (installer/CLI manages /opt/ajib/.env)
- Starts aiogram Dispatcher with minimal builtin routers
- Provides skeleton handlers for /start, /help, /ping and admin entry
- Designed to be extended via modular routers in `ajib.bot.handlers.*`

Run directly:
  python -m ajib.bot.core.bot

Or via systemd service managed by the `ajib` CLI.
"""

import asyncio
import contextlib
import signal
from typing import Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, BotCommand

from .config import load_config, ensure_paths, setup_logging, get_logger, Config


__all__ = [
    "main",
    "run",
    "create_bot",
    "create_dispatcher",
    "register_builtin_routers",
    "AdminFilter",
]

log = get_logger(__name__)


class AdminFilter(BaseFilter):
    """Allow messages only from configured admin IDs."""

    def __init__(self, admin_ids: set[int]):
        self._admin_ids = set(admin_ids)

    async def __call__(self, message: Message) -> bool:
        if not message.from_user:
            return False
        return message.from_user.id in self._admin_ids


def register_builtin_routers(dp: Dispatcher, cfg: Config) -> None:
    """
    Register minimal builtin routers.
    Tries to include external modular routers if available:
      - ajib.bot.handlers.customer: router
      - ajib.bot.handlers.admin: router
    """
    # Basic user router
    user_router = Router(name="user")

    @user_router.message(CommandStart())
    async def on_start(message: Message) -> None:
        await message.answer(
            "Welcome to AJIB VPN Bot.\n\n"
            "Use the menu or send /help to see available commands."
        )

    @user_router.message(Command("help"))
    async def on_help(message: Message) -> None:
        await message.answer(
            "Available commands:\n"
            "/start - Show welcome message\n"
            "/help - Show this help\n"
            "/ping - Health check\n"
            "\n"
            "Admins can use /admin to open admin menu."
        )

    @user_router.message(Command("ping"))
    async def on_ping(message: Message) -> None:
        await message.answer("pong")

    # Admin router (skeleton)
    admin_router = Router(name="admin")
    admin_router.message.filter(AdminFilter(cfg.admin_ids))

    @admin_router.message(Command("admin"))
    async def on_admin(message: Message) -> None:
        await message.answer(
            "Admin panel (skeleton).\n\n"
            "- Backup/Restore\n"
            "- Broadcast\n"
            "- Plans (Add/Edit)\n"
            "\n"
            "Use the upcoming admin menu to navigate."
        )

    dp.include_router(user_router)
    dp.include_router(admin_router)

    # Attempt to include optional, more complete routers
    with contextlib.suppress(Exception):
        from ajib.bot.handlers.customer import router as customer_router  # type: ignore

        dp.include_router(customer_router)
        log.info("Registered external router: ajib.bot.handlers.customer")

    with contextlib.suppress(Exception):
        from ajib.bot.handlers.admin import router as admin_ext_router  # type: ignore

        dp.include_router(admin_ext_router)
        log.info("Registered external router: ajib.bot.handlers.admin")


async def _set_bot_commands(bot: Bot) -> None:
    """Set minimal list of bot commands."""
    commands = [
        BotCommand(command="start", description="Start the bot"),
        BotCommand(command="help", description="Show help"),
        BotCommand(command="ping", description="Health check"),
        BotCommand(command="admin", description="Admin menu"),
    ]
    await bot.set_my_commands(commands)


def create_bot(cfg: Config) -> Bot:
    """Create Bot instance with configured parse mode."""
    return Bot(token=cfg.bot_token, parse_mode=ParseMode.HTML)


def create_dispatcher(cfg: Config) -> Dispatcher:
    """Create Dispatcher with in-memory FSM storage and register routers."""
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    register_builtin_routers(dp, cfg)
    return dp


async def run() -> None:
    """
    Boot sequence:
      1) Configure logging
      2) Load config and ensure data paths
      3) Initialize Bot/Dispatcher
      4) Set commands
      5) Start polling (graceful shutdown on SIGINT/SIGTERM)
    """
    # Logging may already be configured earlier; calling again is idempotent
    setup_logging()

    cfg = load_config()
    ensure_paths(cfg)

    log.info("Starting AJIB bot with config: %s", cfg.redacted_dict())

    bot = create_bot(cfg)
    dp = create_dispatcher(cfg)

    # Prepare shutdown signals
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_shutdown() -> None:
        if not stop_event.is_set():
            log.info("Shutdown signal received. Stopping polling...")
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_shutdown)

    await _set_bot_commands(bot)

    # Start polling and wait for stop event
    polling = asyncio.create_task(
        dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    )

    await stop_event.wait()

    # Gracefully cancel polling
    if not polling.done():
        polling.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await polling

    await bot.session.close()
    log.info("AJIB bot stopped.")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
