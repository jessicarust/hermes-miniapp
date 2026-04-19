#!/usr/bin/env python3
"""
hermes-miniapp installer
Patches a Hermes installation to add the Telegram Mini App.

Usage:
    python3 install.py
    python3 install.py --dry-run    # show what would be done, no changes
    python3 install.py --uninstall  # remove all changes
"""

import argparse
import shutil
import stat
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent.resolve()
SRC_HTML = HERE / "src" / "webapp" / "index.html"
SRC_API_PATCH = HERE / "src" / "patches" / "api_server.py"
SRC_TG_PATCH = HERE / "src" / "patches" / "telegram.py"
SRC_HOOK = HERE / "hooks" / "post-merge"

HERMES_HOME = Path.home() / ".hermes"
HERMES_AGENT = HERMES_HOME / "hermes-agent"
MINIAPP_STATE = HERMES_HOME / "miniapp"

API_SERVER = HERMES_AGENT / "gateway" / "platforms" / "api_server.py"
TELEGRAM_PY = HERMES_AGENT / "gateway" / "platforms" / "telegram.py"
WEBAPP_DIR  = HERMES_AGENT / "gateway" / "platforms" / "webapp"
GIT_HOOKS   = HERMES_AGENT / ".git" / "hooks"

INSTALL_MARKER = "_handle_webapp_index"  # unique string only present after install


# ─── Helpers ─────────────────────────────────────────────────────────────────

def die(msg: str) -> None:
    print(f"\n✗  {msg}", file=sys.stderr)
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"  ✓  {msg}")


def info(msg: str) -> None:
    print(f"  →  {msg}")


def is_installed() -> bool:
    return API_SERVER.exists() and INSTALL_MARKER in API_SERVER.read_text(encoding="utf-8")


def insert_after(text: str, anchor: str, insertion: str) -> str:
    """Insert *insertion* on the line immediately after the first occurrence of *anchor*."""
    idx = text.find(anchor)
    if idx == -1:
        raise ValueError(f"Anchor not found: {anchor!r}")
    end_of_line = text.find("\n", idx)
    if end_of_line == -1:
        end_of_line = len(text)
    return text[: end_of_line + 1] + insertion + text[end_of_line + 1 :]


def replace_block(text: str, anchor: str, old_block: str, new_block: str) -> str:
    """Replace *old_block* starting at *anchor* with *new_block*."""
    idx = text.find(anchor)
    if idx == -1:
        raise ValueError(f"Anchor not found: {anchor!r}")
    end = text.find(old_block, idx)
    if end == -1:
        raise ValueError(f"Block not found after anchor: {old_block!r}")
    return text[:end] + new_block + text[end + len(old_block):]


# ─── api_server.py patches ────────────────────────────────────────────────────

def patch_api_server(dry_run: bool) -> None:
    text = API_SERVER.read_text(encoding="utf-8")
    original = text

    # A: Add _webapp_session_models instance variable
    ivar = (
        "\n        # Webapp per-session model overrides: session_id -> model_id\n"
        "        self._webapp_session_models: Dict[str, str] = {}\n"
    )
    anchor_ivar = "        # Active run streams: run_id -> asyncio.Queue of SSE event dicts"
    text = insert_after(text, anchor_ivar, ivar)
    info("Added _webapp_session_models instance variable")

    # B: Add model_override param to _create_agent signature
    old_sig_create = "        tool_complete_callback=None,\n    ) -> Any:"
    new_sig_create = "        tool_complete_callback=None,\n        model_override: Optional[str] = None,\n    ) -> Any:"
    if old_sig_create in text:
        text = text.replace(old_sig_create, new_sig_create, 1)
        info("Added model_override to _create_agent signature")

    # C: Use model_override in _create_agent body
    old_model_resolve = "        model = _resolve_gateway_model()"
    new_model_resolve = "        model = model_override or _resolve_gateway_model()"
    if old_model_resolve in text and new_model_resolve not in text:
        text = text.replace(old_model_resolve, new_model_resolve, 1)
        info("Wired model_override into _create_agent body")

    # D: Add model_override param to _run_agent signature
    old_sig_run = "        agent_ref: Optional[list] = None,\n    ) -> tuple:"
    new_sig_run = "        agent_ref: Optional[list] = None,\n        model_override: Optional[str] = None,\n    ) -> tuple:"
    if old_sig_run in text:
        text = text.replace(old_sig_run, new_sig_run, 1)
        info("Added model_override to _run_agent signature")

    # E: Thread model_override through _run_agent → _create_agent call
    old_create_call = (
        "            agent = self._create_agent(\n"
        "                ephemeral_system_prompt=ephemeral_system_prompt,\n"
        "                session_id=session_id,\n"
        "                stream_delta_callback=stream_delta_callback,\n"
        "                tool_progress_callback=tool_progress_callback,\n"
        "                tool_start_callback=tool_start_callback,\n"
        "                tool_complete_callback=tool_complete_callback,\n"
        "            )"
    )
    new_create_call = (
        "            agent = self._create_agent(\n"
        "                ephemeral_system_prompt=ephemeral_system_prompt,\n"
        "                session_id=session_id,\n"
        "                stream_delta_callback=stream_delta_callback,\n"
        "                tool_progress_callback=tool_progress_callback,\n"
        "                tool_start_callback=tool_start_callback,\n"
        "                tool_complete_callback=tool_complete_callback,\n"
        "                model_override=model_override,\n"
        "            )"
    )
    if old_create_call in text:
        text = text.replace(old_create_call, new_create_call, 1)
        info("Threaded model_override through _run_agent")

    # F: Insert all webapp handler methods before BasePlatformAdapter interface
    methods_anchor = "    # ------------------------------------------------------------------\n    # BasePlatformAdapter interface"
    methods_block = SRC_API_PATCH.read_text(encoding="utf-8")
    insertion = (
        "\n    # ------------------------------------------------------------------\n"
        + methods_block.rstrip()
        + "\n\n"
    )
    text = text.replace(methods_anchor, insertion + methods_anchor, 1)
    info("Inserted webapp handler methods")

    # G: Register routes
    routes_anchor = '            self._app.router.add_get("/v1/runs/{run_id}/events", self._handle_run_events)'
    routes_addition = (
        "\n"
        "            # Telegram Mini App\n"
        '            self._app.router.add_get("/webapp", self._handle_webapp_index)\n'
        '            self._app.router.add_get("/webapp/", self._handle_webapp_index)\n'
        '            self._app.router.add_post("/v1/webapp/auth", self._handle_webapp_auth)\n'
        '            self._app.router.add_post("/v1/webapp/chat", self._handle_webapp_chat)\n'
        '            self._app.router.add_get("/v1/webapp/models", self._handle_webapp_models)\n'
        '            self._app.router.add_post("/v1/webapp/model", self._handle_webapp_set_model)\n'
        '            self._app.router.add_get("/v1/webapp/commands", self._handle_webapp_commands)'
    )
    text = insert_after(text, routes_anchor, routes_addition)
    info("Registered webapp routes")

    if text == original:
        info("api_server.py — no changes needed")
    elif not dry_run:
        API_SERVER.write_text(text, encoding="utf-8")
        ok("Patched gateway/platforms/api_server.py")
    else:
        info("[dry-run] would patch api_server.py")


# ─── telegram.py patches ──────────────────────────────────────────────────────

def patch_telegram(dry_run: bool) -> None:
    text = TELEGRAM_PY.read_text(encoding="utf-8")
    original = text

    # A: _webapp_url in __init__
    ivar_anchor = "        self._approval_state: Dict[int, str] = {}"
    ivar_insertion = (
        "\n"
        "        # Telegram Mini App URL (set via TELEGRAM_WEBAPP_URL or extra.webapp_url)\n"
        "        self._webapp_url: str = (\n"
        "            self.config.extra.get(\"webapp_url\", \"\")\n"
        "            or os.getenv(\"TELEGRAM_WEBAPP_URL\", \"\")\n"
        "        ).rstrip(\"/\")\n"
    )
    if ivar_anchor in text and "_webapp_url" not in text:
        text = insert_after(text, ivar_anchor, ivar_insertion)
        info("Added _webapp_url to TelegramAdapter.__init__")

    # B: menu button before _mark_connected()
    # Find the connect()-specific occurrence (after set_my_commands block)
    menu_anchor = "            self._mark_connected()\n            mode = \"webhook\" if self._webhook_mode else \"polling\""
    menu_insertion = (
        "            # Set up the Mini App menu button if webapp_url is configured.\n"
        "            if self._webapp_url:\n"
        "                try:\n"
        "                    from telegram import MenuButtonWebApp, WebAppInfo\n"
        "                    await self._bot.set_chat_menu_button(\n"
        "                        menu_button=MenuButtonWebApp(\n"
        "                            text=\"Open App\",\n"
        "                            web_app=WebAppInfo(url=f\"{self._webapp_url}/webapp\"),\n"
        "                        )\n"
        "                    )\n"
        "                    logger.info(\"[%s] Telegram Mini App menu button set → %s/webapp\", self.name, self._webapp_url)\n"
        "                except Exception as e:\n"
        "                    logger.warning(\"[%s] Could not set Mini App menu button: %s\", self.name, e)\n"
        "\n"
    )
    if menu_anchor in text and "MenuButtonWebApp" not in text:
        text = text.replace(menu_anchor, menu_insertion + menu_anchor, 1)
        info("Added menu button setup to connect()")

    # C: /app command dispatch + _handle_app_command method
    old_handle_command = (
        "    async def _handle_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:\n"
        "        \"\"\"Handle incoming command messages.\"\"\"\n"
        "        if not update.message or not update.message.text:\n"
        "            return\n"
        "        if not self._should_process_message(update.message, is_command=True):\n"
        "            return\n"
        "\n"
        "        event = self._build_message_event(update.message, MessageType.COMMAND)\n"
        "        await self.handle_message(event)"
    )
    new_handle_command = (
        "    async def _handle_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:\n"
        "        \"\"\"Handle incoming command messages.\"\"\"\n"
        "        if not update.message or not update.message.text:\n"
        "            return\n"
        "        if not self._should_process_message(update.message, is_command=True):\n"
        "            return\n"
        "\n"
        "        cmd = update.message.text.split()[0].lstrip(\"/\").split(\"@\")[0].lower()\n"
        "        if cmd == \"app\":\n"
        "            await self._handle_app_command(update)\n"
        "            return\n"
        "\n"
        "        event = self._build_message_event(update.message, MessageType.COMMAND)\n"
        "        await self.handle_message(event)\n"
        "\n"
        "    async def _handle_app_command(self, update: \"Update\") -> None:\n"
        "        \"\"\"Send an inline button to open the Telegram Mini App.\"\"\"\n"
        "        if not self._webapp_url:\n"
        "            await update.message.reply_text(\n"
        "                \"The Hermes Mini App is not configured. \"\n"
        "                \"Set TELEGRAM_WEBAPP_URL in your environment or \"\n"
        "                \"`platforms.telegram.extra.webapp_url` in config.yaml.\"\n"
        "            )\n"
        "            return\n"
        "        try:\n"
        "            from telegram import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo\n"
        "            await update.message.reply_text(\n"
        "                \"Open the Hermes chat interface:\",\n"
        "                reply_markup=InlineKeyboardMarkup([[\n"
        "                    InlineKeyboardButton(\n"
        "                        \"⚡ Open Hermes\",\n"
        "                        web_app=WebAppInfo(url=f\"{self._webapp_url}/webapp\"),\n"
        "                    )\n"
        "                ]]),\n"
        "            )\n"
        "        except Exception as e:\n"
        "            logger.warning(\"[%s] /app command failed: %s\", self.name, e)"
    )
    if old_handle_command in text:
        text = text.replace(old_handle_command, new_handle_command, 1)
        info("Added /app command intercept and _handle_app_command method")

    if text == original:
        info("telegram.py — no changes needed")
    elif not dry_run:
        TELEGRAM_PY.write_text(text, encoding="utf-8")
        ok("Patched gateway/platforms/telegram.py")
    else:
        info("[dry-run] would patch telegram.py")


# ─── Install ──────────────────────────────────────────────────────────────────

def install(dry_run: bool) -> None:
    print()
    print("━" * 50)
    print("  hermes-miniapp installer")
    print("━" * 50)
    print()

    # 1. Locate Hermes
    if not HERMES_AGENT.exists():
        die(
            f"Hermes not found at {HERMES_AGENT}\n"
            "  Install Hermes first: https://hermesagent.ai"
        )
    ok(f"Found Hermes at {HERMES_AGENT}")

    # 2. Idempotency check
    if is_installed():
        print()
        print("  ℹ️  hermes-miniapp is already installed.")
        print()
        _print_config_instructions()
        return

    # 3. Copy index.html
    if not dry_run:
        WEBAPP_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SRC_HTML, WEBAPP_DIR / "index.html")
        ok("Copied src/webapp/index.html → gateway/platforms/webapp/index.html")
    else:
        info(f"[dry-run] would copy index.html → {WEBAPP_DIR}/index.html")

    # 4 & 5. Patch Python files
    patch_api_server(dry_run)
    patch_telegram(dry_run)

    # 6. Install git hook
    if not dry_run and GIT_HOOKS.exists():
        hook_dest = GIT_HOOKS / "post-merge"
        shutil.copy2(SRC_HOOK, hook_dest)
        hook_dest.chmod(hook_dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        ok("Installed .git/hooks/post-merge (survives hermes auto-updates)")
    elif dry_run:
        info("[dry-run] would install post-merge hook")

    # 7. Save state copies for hook recovery
    if not dry_run:
        MINIAPP_STATE.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SRC_HTML, MINIAPP_STATE / "webapp-index.html")
        # Generate patch for hook to use
        try:
            result = subprocess.run(
                ["git", "diff", "gateway/platforms/api_server.py", "gateway/platforms/telegram.py"],
                cwd=HERMES_AGENT, capture_output=True, text=True,
            )
            if result.returncode == 0 and result.stdout:
                (MINIAPP_STATE / "webapp.patch").write_text(result.stdout, encoding="utf-8")
                ok("Saved patch state to ~/.hermes/miniapp/")
        except Exception:
            pass

    print()
    _print_config_instructions()


def _print_config_instructions() -> None:
    print("━" * 50)
    print("  ✓  hermes-miniapp installed!")
    print("━" * 50)
    print()
    print("  Add to ~/.hermes/.env:")
    print()
    print("    TELEGRAM_BOT_TOKEN=<your bot token>")
    print("    TELEGRAM_WEBAPP_URL=https://your-public-url.com")
    print("    API_SERVER_CORS_ORIGINS=*")
    print()
    print("  Add to ~/.hermes/config.yaml (under platforms:):")
    print()
    print("    telegram:")
    print("      enabled: true")
    print("      extra:")
    print("        webapp_url: https://your-public-url.com")
    print("    api_server:")
    print("      enabled: true")
    print("      extra:")
    print("        host: 0.0.0.0")
    print("        public_url: https://your-public-url.com")
    print()
    print("  Then:")
    print()
    print("    hermes gateway run")
    print()
    print("  Open: https://your-public-url.com/webapp")
    print("  Or send /app to your bot in Telegram.")
    print()


# ─── Uninstall ────────────────────────────────────────────────────────────────

def uninstall(dry_run: bool) -> None:
    print()
    print("━" * 50)
    print("  hermes-miniapp uninstaller")
    print("━" * 50)
    print()

    if not is_installed():
        print("  ℹ️  hermes-miniapp does not appear to be installed.")
        return

    # Remove webapp directory
    if WEBAPP_DIR.exists():
        if not dry_run:
            shutil.rmtree(WEBAPP_DIR)
            ok("Removed gateway/platforms/webapp/")
        else:
            info(f"[dry-run] would remove {WEBAPP_DIR}")

    # Remove inserted code from api_server.py
    if API_SERVER.exists():
        text = API_SERVER.read_text(encoding="utf-8")
        original_len = len(text)

        # Remove _webapp_session_models
        text = text.replace(
            "\n        # Webapp per-session model overrides: session_id -> model_id\n"
            "        self._webapp_session_models: Dict[str, str] = {}\n",
            "",
        )
        # Remove model_override from _create_agent
        text = text.replace(
            "        model_override: Optional[str] = None,\n    ) -> Any:",
            "    ) -> Any:",
            1,
        )
        text = text.replace(
            "        model = model_override or _resolve_gateway_model()",
            "        model = _resolve_gateway_model()",
            1,
        )
        # Remove model_override from _run_agent
        text = text.replace(
            "        model_override: Optional[str] = None,\n    ) -> tuple:",
            "    ) -> tuple:",
            1,
        )
        text = text.replace(
            "                model_override=model_override,\n            )",
            "            )",
            1,
        )
        # Remove webapp methods block (between the two dashes sections)
        start_marker = "\n    # ------------------------------------------------------------------\n    # Telegram Mini App (webapp)\n"
        end_marker = "\n    # ------------------------------------------------------------------\n    # BasePlatformAdapter interface"
        start = text.find(start_marker)
        end = text.find(end_marker)
        if start != -1 and end != -1 and start < end:
            text = text[:start] + "\n" + text[end:]
        # Remove routes
        routes_block = (
            "\n"
            "            # Telegram Mini App\n"
            '            self._app.router.add_get("/webapp", self._handle_webapp_index)\n'
            '            self._app.router.add_get("/webapp/", self._handle_webapp_index)\n'
            '            self._app.router.add_post("/v1/webapp/auth", self._handle_webapp_auth)\n'
            '            self._app.router.add_post("/v1/webapp/chat", self._handle_webapp_chat)\n'
            '            self._app.router.add_get("/v1/webapp/models", self._handle_webapp_models)\n'
            '            self._app.router.add_post("/v1/webapp/model", self._handle_webapp_set_model)\n'
            '            self._app.router.add_get("/v1/webapp/commands", self._handle_webapp_commands)'
        )
        text = text.replace(routes_block, "", 1)

        if not dry_run:
            API_SERVER.write_text(text, encoding="utf-8")
            ok(f"Reverted api_server.py ({original_len - len(text)} chars removed)")
        else:
            info("[dry-run] would revert api_server.py")

    # Remove inserted code from telegram.py
    if TELEGRAM_PY.exists():
        text = TELEGRAM_PY.read_text(encoding="utf-8")
        original_len = len(text)

        text = text.replace(
            "\n"
            "        # Telegram Mini App URL (set via TELEGRAM_WEBAPP_URL or extra.webapp_url)\n"
            "        self._webapp_url: str = (\n"
            "            self.config.extra.get(\"webapp_url\", \"\")\n"
            "            or os.getenv(\"TELEGRAM_WEBAPP_URL\", \"\")\n"
            "        ).rstrip(\"/\")\n",
            "",
            1,
        )
        # Remove menu button block
        menu_block = (
            "            # Set up the Mini App menu button if webapp_url is configured.\n"
            "            if self._webapp_url:\n"
            "                try:\n"
            "                    from telegram import MenuButtonWebApp, WebAppInfo\n"
            "                    await self._bot.set_chat_menu_button(\n"
            "                        menu_button=MenuButtonWebApp(\n"
            "                            text=\"Open App\",\n"
            "                            web_app=WebAppInfo(url=f\"{self._webapp_url}/webapp\"),\n"
            "                        )\n"
            "                    )\n"
            "                    logger.info(\"[%s] Telegram Mini App menu button set → %s/webapp\", self.name, self._webapp_url)\n"
            "                except Exception as e:\n"
            "                    logger.warning(\"[%s] Could not set Mini App menu button: %s\", self.name, e)\n"
            "\n"
        )
        text = text.replace(menu_block, "", 1)
        # Restore _handle_command and remove _handle_app_command
        # Find and remove /app dispatch lines + _handle_app_command method
        cmd_dispatch = (
            "\n"
            "        cmd = update.message.text.split()[0].lstrip(\"/\").split(\"@\")[0].lower()\n"
            "        if cmd == \"app\":\n"
            "            await self._handle_app_command(update)\n"
            "            return\n"
        )
        text = text.replace(cmd_dispatch, "", 1)
        # Remove _handle_app_command method (up to next method definition)
        app_cmd_start = "\n    async def _handle_app_command(self, update: \"Update\") -> None:\n"
        app_cmd_end = "\n    async def _handle_location_message("
        s = text.find(app_cmd_start)
        e = text.find(app_cmd_end)
        if s != -1 and e != -1 and s < e:
            text = text[:s] + "\n" + text[e:]

        if not dry_run:
            TELEGRAM_PY.write_text(text, encoding="utf-8")
            ok(f"Reverted telegram.py ({original_len - len(text)} chars removed)")
        else:
            info("[dry-run] would revert telegram.py")

    # Remove git hook
    hook = GIT_HOOKS / "post-merge"
    if hook.exists():
        if not dry_run:
            hook.unlink()
            ok("Removed .git/hooks/post-merge")
        else:
            info("[dry-run] would remove post-merge hook")

    # Remove state dir
    if MINIAPP_STATE.exists():
        if not dry_run:
            shutil.rmtree(MINIAPP_STATE)
            ok("Removed ~/.hermes/miniapp/")
        else:
            info("[dry-run] would remove ~/.hermes/miniapp/")

    print()
    ok("Uninstall complete. Restart the gateway to apply changes.")
    print()


# ─── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="hermes-miniapp installer")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change, make no edits")
    parser.add_argument("--uninstall", action="store_true", help="Remove all installed changes")
    args = parser.parse_args()

    if args.uninstall:
        uninstall(args.dry_run)
    else:
        install(args.dry_run)


if __name__ == "__main__":
    main()
