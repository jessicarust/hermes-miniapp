    # ------------------------------------------------------------------
    # Telegram Mini App (webapp)
    # ------------------------------------------------------------------

    def _get_webapp_bot_token(self) -> str:
        """Bot token used to validate Telegram WebApp initData."""
        return (
            (self.config.extra or {}).get("webapp_bot_token", "")
            or os.getenv("WEBAPP_BOT_TOKEN", "")
            or os.getenv("TELEGRAM_BOT_TOKEN", "")
        )

    def _get_webapp_secret(self) -> str:
        """Secret used to sign webapp session tokens."""
        return self._api_key or self._get_webapp_bot_token() or "hermes-webapp-dev"

    def _validate_telegram_init_data(self, init_data: str) -> Optional[Dict[str, Any]]:
        """Validate Telegram WebApp initData string via HMAC-SHA256."""
        from urllib.parse import parse_qsl, unquote
        import json as _json

        bot_token = self._get_webapp_bot_token()
        if not bot_token or not init_data:
            return None

        params = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = params.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed_hash, received_hash):
            return None

        user_data: Dict[str, Any] = {}
        user_json = params.get("user", "")
        if user_json:
            try:
                user_data = _json.loads(unquote(user_json))
            except (ValueError, KeyError):
                pass

        return {
            "user": user_data,
            "chat_instance": params.get("chat_instance", ""),
            "auth_date": params.get("auth_date", ""),
        }

    def _make_webapp_token(self, user_id: str) -> str:
        """Create a signed bearer token for a webapp session."""
        secret = self._get_webapp_secret()
        payload = f"hermes-webapp:{user_id}"
        sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return f"wa.{user_id}.{sig[:48]}"

    def _verify_webapp_token(self, token: str) -> Optional[str]:
        """Verify a webapp token; returns user_id or None."""
        if not token.startswith("wa."):
            return None
        parts = token.split(".", 2)
        if len(parts) != 3:
            return None
        _, user_id, _ = parts
        expected = self._make_webapp_token(user_id)
        if hmac.compare_digest(token, expected):
            return user_id
        return None

    def _check_webapp_auth(self, request: "web.Request") -> Optional[str]:
        """Return user_id from a valid webapp Bearer token, or None."""
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        return self._verify_webapp_token(auth[7:].strip())

    async def _handle_webapp_index(self, request: "web.Request") -> "web.Response":
        """GET /webapp — serve the Telegram Mini App HTML."""
        import pathlib
        html_path = pathlib.Path(__file__).parent / "webapp" / "index.html"
        if not html_path.exists():
            return web.Response(status=404, text="Webapp not found")

        html = html_path.read_text(encoding="utf-8")

        public_url = (self.config.extra or {}).get("public_url", "").rstrip("/")
        if not public_url:
            scheme = "https" if request.secure else "http"
            host = request.headers.get("X-Forwarded-Host") or request.host
            public_url = f"{scheme}://{host}"

        html = html.replace("window.__HERMES_BASE_URL__ || ''", f"'{public_url}'")
        return web.Response(body=html.encode("utf-8"), content_type="text/html", charset="utf-8")

    async def _handle_webapp_auth(self, request: "web.Request") -> "web.Response":
        """POST /v1/webapp/auth — validate Telegram initData, issue session token."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        init_data: str = body.get("init_data", "")
        bot_token = self._get_webapp_bot_token()

        user_id: str = ""
        if init_data and bot_token:
            parsed = self._validate_telegram_init_data(init_data)
            if parsed is None:
                return web.json_response(
                    {"error": "Invalid Telegram initData — authentication failed"}, status=401)
            user_id = str(parsed["user"].get("id", ""))
        elif not bot_token:
            peer = request.headers.get("X-Forwarded-For") or request.remote or "dev"
            user_id = "dev_" + hashlib.sha256(peer.encode()).hexdigest()[:12]
            logger.warning("[%s] webapp auth: no TELEGRAM_BOT_TOKEN, using dev session %s", self.name, user_id)
        else:
            return web.json_response({"error": "init_data required"}, status=400)

        if not user_id:
            return web.json_response({"error": "Could not determine user identity"}, status=400)

        token = self._make_webapp_token(user_id)
        session_id = f"telegram_webapp_{user_id}"

        history: List[Dict[str, str]] = []
        try:
            db = self._ensure_session_db()
            if db is not None:
                history = db.get_messages_as_conversation(session_id) or []
        except Exception as e:
            logger.debug("[%s] webapp: could not load history: %s", self.name, e)

        return web.json_response({"token": token, "session_id": session_id, "history": history[-40:]})

    async def _handle_webapp_chat(self, request: "web.Request") -> "web.Response":
        """POST /v1/webapp/chat — streaming SSE chat for the Telegram Mini App."""
        user_id = self._check_webapp_auth(request)
        if user_id is None:
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        message: str = body.get("message", "").strip()
        if not message:
            return web.json_response({"error": "message required"}, status=400)

        session_id: str = body.get("session_id") or f"telegram_webapp_{user_id}"
        if re.search(r'[\r\n\x00]', session_id):
            return web.json_response({"error": "Invalid session_id"}, status=400)

        history: List[Dict[str, str]] = []
        try:
            db = self._ensure_session_db()
            if db is not None:
                history = db.get_messages_as_conversation(session_id) or []
        except Exception:
            pass
        if not history:
            history = [m for m in body.get("history", []) if isinstance(m, dict)]

        import queue as _q
        _stream_q: _q.Queue = _q.Queue()

        def _on_delta(delta):
            if delta is not None:
                _stream_q.put(delta)

        def _on_tool(event_type, name, preview, args, **kwargs):
            if event_type == "tool.started" and name and not name.startswith("_"):
                _stream_q.put(("__webapp_tool__", name))

        model_override = self._webapp_session_models.get(session_id)
        agent_ref = [None]
        agent_task = asyncio.ensure_future(self._run_agent(
            user_message=message,
            conversation_history=history,
            session_id=session_id,
            stream_delta_callback=_on_delta,
            tool_progress_callback=_on_tool,
            agent_ref=agent_ref,
            model_override=model_override,
        ))

        origin = request.headers.get("Origin", "")
        sse_headers: Dict[str, str] = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        cors = self._cors_headers_for_origin(origin) if origin else None
        if cors:
            sse_headers.update(cors)

        response = web.StreamResponse(status=200, headers=sse_headers)
        await response.prepare(request)

        def _sse(data: str) -> bytes:
            return f"data: {data}\n\n".encode()

        try:
            last_activity = time.monotonic()
            loop = asyncio.get_event_loop()
            while True:
                try:
                    item = await loop.run_in_executor(None, lambda: _stream_q.get(timeout=0.5))
                except _q.Empty:
                    if agent_task.done():
                        while True:
                            try:
                                item = _stream_q.get_nowait()
                            except _q.Empty:
                                break
                            if item is None:
                                break
                            if isinstance(item, tuple) and item[0] == "__webapp_tool__":
                                await response.write(_sse(json.dumps({"type": "tool", "name": item[1]})))
                            else:
                                await response.write(_sse(json.dumps({"type": "delta", "text": item})))
                        break
                    if time.monotonic() - last_activity >= CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS:
                        await response.write(b": keepalive\n\n")
                        last_activity = time.monotonic()
                    continue
                if item is None:
                    break
                if isinstance(item, tuple) and item[0] == "__webapp_tool__":
                    await response.write(_sse(json.dumps({"type": "tool", "name": item[1]})))
                else:
                    await response.write(_sse(json.dumps({"type": "delta", "text": item})))
                last_activity = time.monotonic()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            agent = agent_ref[0]
            if agent is not None:
                try:
                    agent.interrupt("webapp client disconnected")
                except Exception:
                    pass
            if not agent_task.done():
                agent_task.cancel()
                try:
                    await agent_task
                except (asyncio.CancelledError, Exception):
                    pass
        except Exception as exc:
            logger.error("[%s] webapp chat error: %s", self.name, exc, exc_info=True)
            try:
                await response.write(_sse(json.dumps({"type": "error", "message": str(exc)})))
            except Exception:
                pass

        try:
            await response.write(b"data: [DONE]\n\n")
        except Exception:
            pass
        return response

    async def _handle_webapp_commands(self, request: "web.Request") -> "web.Response":
        """GET /v1/webapp/commands — return gateway-visible slash commands."""
        user_id = self._check_webapp_auth(request)
        if user_id is None:
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            from hermes_cli.commands import COMMAND_REGISTRY
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

        commands = []
        for cmd in COMMAND_REGISTRY:
            if cmd.cli_only and not cmd.gateway_config_gate:
                continue
            commands.append({
                "name": cmd.name,
                "description": cmd.description,
                "category": cmd.category,
                "args_hint": cmd.args_hint,
                "aliases": list(cmd.aliases),
                "subcommands": list(cmd.subcommands),
            })

        return web.json_response({"commands": commands})

    async def _handle_webapp_models(self, request: "web.Request") -> "web.Response":
        """GET /v1/webapp/models — return OpenRouter model list with live pricing."""
        user_id = self._check_webapp_auth(request)
        if user_id is None:
            return web.json_response({"error": "Unauthorized"}, status=401)

        session_id = request.rel_url.query.get("session_id") or f"telegram_webapp_{user_id}"
        current_model = self._webapp_session_models.get(session_id, "")

        loop = asyncio.get_event_loop()

        def _fetch():
            try:
                from hermes_cli.models import OPENROUTER_MODELS
                import urllib.request as _ureq
                import json as _json

                req = _ureq.Request(
                    "https://openrouter.ai/api/v1/models",
                    headers={"Accept": "application/json"},
                )
                with _ureq.urlopen(req, timeout=6) as resp:
                    live = {m.get("id"): m for m in _json.loads(resp.read()).get("data", []) if isinstance(m, dict)}
            except Exception:
                live = {}

            if not current_model:
                from gateway.run import _resolve_gateway_model
                default_model = _resolve_gateway_model()
            else:
                default_model = current_model

            result = []
            for mid, _tag in OPENROUTER_MODELS:
                info = live.get(mid, {})
                pricing = info.get("pricing", {})

                def _fmt(v):
                    try:
                        f = float(v)
                        return "free" if f == 0 else f"${f * 1_000_000:.2f}"
                    except Exception:
                        return ""

                name = info.get("name") or mid.split("/")[-1].replace("-", " ").title()
                provider = mid.split("/")[0].replace("-", " ").title() if "/" in mid else "Unknown"
                result.append({
                    "id": mid,
                    "name": name,
                    "provider": provider,
                    "input": _fmt(pricing.get("prompt", "")),
                    "output": _fmt(pricing.get("completion", "")),
                    "is_free": _fmt(pricing.get("prompt", "")) == "free",
                    "is_current": mid == (current_model or default_model),
                })
            return result, (current_model or default_model)

        models, active = await loop.run_in_executor(None, _fetch)
        return web.json_response({"models": models, "current": active})

    async def _handle_webapp_set_model(self, request: "web.Request") -> "web.Response":
        """POST /v1/webapp/model — set model for this webapp session."""
        user_id = self._check_webapp_auth(request)
        if user_id is None:
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        model_id: str = body.get("model", "").strip()
        session_id: str = body.get("session_id") or f"telegram_webapp_{user_id}"

        if not model_id:
            self._webapp_session_models.pop(session_id, None)
            return web.json_response({"ok": True, "model": None})

        self._webapp_session_models[session_id] = model_id
        logger.info("[%s] webapp: session %s switched to model %s", self.name, session_id[:30], model_id)
        return web.json_response({"ok": True, "model": model_id})
