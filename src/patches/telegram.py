# ── Patch A: add to __init__, after `self._approval_state: Dict[int, str] = {}` ──

        # Telegram Mini App URL (set via TELEGRAM_WEBAPP_URL or extra.webapp_url)
        self._webapp_url: str = (
            self.config.extra.get("webapp_url", "")
            or os.getenv("TELEGRAM_WEBAPP_URL", "")
        ).rstrip("/")


# ── Patch B: add to connect(), before `self._mark_connected()` ──

            # Set up the Mini App menu button if webapp_url is configured.
            if self._webapp_url:
                try:
                    from telegram import MenuButtonWebApp, WebAppInfo
                    await self._bot.set_chat_menu_button(
                        menu_button=MenuButtonWebApp(
                            text="Open App",
                            web_app=WebAppInfo(url=f"{self._webapp_url}/webapp"),
                        )
                    )
                    logger.info("[%s] Telegram Mini App menu button set → %s/webapp", self.name, self._webapp_url)
                except Exception as e:
                    logger.warning("[%s] Could not set Mini App menu button: %s", self.name, e)


# ── Patch C: replace body of _handle_command() and add _handle_app_command() ──

    async def _handle_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming command messages."""
        if not update.message or not update.message.text:
            return
        if not self._should_process_message(update.message, is_command=True):
            return

        cmd = update.message.text.split()[0].lstrip("/").split("@")[0].lower()
        if cmd == "app":
            await self._handle_app_command(update)
            return

        event = self._build_message_event(update.message, MessageType.COMMAND)
        await self.handle_message(event)

    async def _handle_app_command(self, update: "Update") -> None:
        """Send an inline button to open the Telegram Mini App."""
        if not self._webapp_url:
            await update.message.reply_text(
                "The Hermes Mini App is not configured. "
                "Set TELEGRAM_WEBAPP_URL in your environment or "
                "`platforms.telegram.extra.webapp_url` in config.yaml."
            )
            return
        try:
            from telegram import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
            await update.message.reply_text(
                "Open the Hermes chat interface:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "⚡ Open Hermes",
                        web_app=WebAppInfo(url=f"{self._webapp_url}/webapp"),
                    )
                ]]),
            )
        except Exception as e:
            logger.warning("[%s] /app command failed: %s", self.name, e)
