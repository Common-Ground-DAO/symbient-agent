"""Common Ground Bot API v1 platform adapter.

The adapter deliberately uses only the bot principal's narrow API surface:
``whoami``, ``messagesById``, ``createMessage``, and the authenticated
``cliMessageEvent`` Socket.IO stream. It does not create a human session or
gain access to the rest of the Common Ground API.

Inbound channel traffic is ignored unless it contains a structured mention of
this bot or directly replies to one of this bot's messages. Hermes' gateway
allowlist is then applied to the sender before an agent turn starts.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
import logging
import os
from typing import Any, Dict, Iterable, Optional
import uuid

import httpx

try:
    import socketio
except ImportError:  # pragma: no cover - exercised by requirements checks
    socketio = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://cg.mogged.eu"
SOCKET_PATH = "api/ws"
PROTOCOL_VERSION = "1"
MAX_MESSAGE_LENGTH = 8_000
MAX_SEEN_MESSAGES = 2_000


class CommonGroundApiError(RuntimeError):
    """A typed, sanitized Common Ground Bot API failure."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _csv(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    values = value if not isinstance(value, str) else value.split(",")
    return [str(item).strip() for item in values if str(item).strip()]


def _extract_text(body: Any) -> str:
    if not isinstance(body, dict) or body.get("version") != PROTOCOL_VERSION:
        return ""
    content = body.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("value"), str):
            parts.append(part["value"])
        elif kind == "newline":
            parts.append("\n")
    return "".join(parts).strip()


def _mentions_user(body: Any, user_id: str) -> bool:
    if not isinstance(body, dict) or not isinstance(body.get("content"), list):
        return False
    return any(
        isinstance(part, dict)
        and part.get("type") == "mention"
        and part.get("userId") == user_id
        for part in body["content"]
    )


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc)


class CommonGroundAdapter(BasePlatformAdapter):
    """Bridge Common Ground channel mentions/replies into Hermes sessions."""

    supports_code_blocks = True
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("commonground"))
        extra = config.extra or {}
        self._base_url = str(
            os.getenv("COMMONGROUND_URL")
            or extra.get("url")
            or DEFAULT_URL
        ).rstrip("/")
        self._token = str(
            os.getenv("COMMONGROUND_BOT_TOKEN")
            or config.token
            or extra.get("token")
            or ""
        ).strip()
        self._community_id = str(
            os.getenv("COMMONGROUND_COMMUNITY_ID")
            or extra.get("community_id")
            or ""
        ).strip()
        self._channel_ids = set(
            _csv(os.getenv("COMMONGROUND_CHANNEL_IDS") or extra.get("channel_ids"))
        )

        self._http: httpx.AsyncClient | None = None
        self._socket: Any = None
        self._bot_user_id = ""
        self._device_id = ""
        self._lock_key: str | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()

    @property
    def name(self) -> str:
        return "Common Ground"

    async def _api_post(self, path: str, payload: dict[str, Any]) -> Any:
        if self._http is None:
            raise CommonGroundApiError("HTTP client is not initialized")
        try:
            response = await self._http.post(path, json=payload)
        except httpx.TimeoutException as exc:
            raise CommonGroundApiError("Common Ground request timed out") from exc
        except httpx.HTTPError as exc:
            raise CommonGroundApiError("Common Ground request failed") from exc

        try:
            envelope = response.json()
        except ValueError as exc:
            raise CommonGroundApiError(
                f"Common Ground returned invalid JSON ({response.status_code})",
                response.status_code,
            ) from exc
        if not response.is_success:
            raise CommonGroundApiError(
                f"Common Ground rejected the request ({response.status_code})",
                response.status_code,
            )
        if not isinstance(envelope, dict) or envelope.get("status") != "OK":
            raise CommonGroundApiError("Common Ground returned an API error", response.status_code)
        return envelope.get("data")

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if socketio is None:
            self._set_fatal_error(
                "dependency_missing",
                "python-socketio is not installed; install the messaging extra",
                retryable=False,
            )
            return False
        if not self._token or not self._community_id or not self._channel_ids:
            self._set_fatal_error(
                "config_missing",
                "Common Ground token, community ID, and channel IDs are required",
                retryable=False,
            )
            return False

        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "authorization": f"Bearer {self._token}",
                "content-type": "application/json",
            },
            timeout=20.0,
        )
        try:
            identity = await self._api_post("/api/bot/v1/whoami", {})
            if not isinstance(identity, dict) or identity.get("protocolVersion") != PROTOCOL_VERSION:
                raise CommonGroundApiError("Unsupported Common Ground bot protocol")
            self._bot_user_id = str(identity.get("userId") or "")
            self._device_id = str(identity.get("deviceId") or "")
            if not self._bot_user_id or not self._device_id:
                raise CommonGroundApiError("Common Ground returned an incomplete bot identity")

            try:
                from gateway.status import acquire_scoped_lock

                self._lock_key = self._device_id
                if not acquire_scoped_lock("commonground", self._lock_key):
                    raise CommonGroundApiError("Common Ground bot identity is already in use")
            except ImportError:  # pragma: no cover - isolated unit tests
                self._lock_key = None

            self._socket = socketio.AsyncClient(
                reconnection=True,
                reconnection_delay=1,
                reconnection_delay_max=15,
                logger=False,
                engineio_logger=False,
            )
            self._socket.on("connect", self._on_connect)
            self._socket.on("disconnect", self._on_disconnect)
            self._socket.on("connect_error", self._on_connect_error)
            self._socket.on("cliMessageEvent", self._on_message_event)
            await self._socket.connect(
                self._base_url,
                socketio_path=SOCKET_PATH,
                auth={"token": self._token, "protocolVersion": PROTOCOL_VERSION},
                wait_timeout=20,
            )
            self._mark_connected()
            logger.info(
                "[Common Ground] authenticated as bot user %s for %d channel(s)",
                self._bot_user_id,
                len(self._channel_ids),
            )
            return True
        except Exception as exc:
            await self._cleanup_transport()
            retryable = not isinstance(exc, CommonGroundApiError) or (
                exc.status_code is None or exc.status_code >= 500
            )
            self._set_fatal_error("connect_failed", str(exc), retryable=retryable)
            logger.error("[Common Ground] connection failed: %s", exc)
            return False

    async def disconnect(self) -> None:
        self._mark_disconnected()
        await self._cleanup_transport()
        self._seen.clear()

    async def _cleanup_transport(self) -> None:
        if self._socket is not None:
            try:
                if getattr(self._socket, "connected", False):
                    await self._socket.disconnect()
            except Exception:
                logger.debug("[Common Ground] Socket.IO disconnect failed", exc_info=True)
            self._socket = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._lock_key:
            try:
                from gateway.status import release_scoped_lock

                release_scoped_lock("commonground", self._lock_key)
            except Exception:
                logger.debug("[Common Ground] scoped lock release failed", exc_info=True)
            self._lock_key = None

    async def _on_connect(self) -> None:
        if not self.is_connected:
            self._mark_connected()
        logger.info("[Common Ground] event stream connected")

    async def _on_disconnect(self, reason: Any = None) -> None:
        logger.warning("[Common Ground] event stream disconnected: %s", reason or "unknown")

    async def _on_connect_error(self, error: Any) -> None:
        logger.warning("[Common Ground] event stream connection error: %s", error)

    def _remember(self, message_id: str) -> bool:
        if message_id in self._seen:
            return False
        self._seen[message_id] = None
        while len(self._seen) > MAX_SEEN_MESSAGES:
            self._seen.popitem(last=False)
        return True

    async def _message_by_id(self, channel_id: str, message_id: str) -> dict[str, Any] | None:
        data = await self._api_post(
            "/api/bot/v1/messages/messagesById",
            {
                "access": {
                    "communityId": self._community_id,
                    "channelId": channel_id,
                },
                "messageIds": [message_id],
            },
        )
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return None

    async def _on_message_event(self, event: Any) -> None:
        if not isinstance(event, dict) or event.get("action") != "new":
            return
        message = event.get("data")
        if not isinstance(message, dict):
            return

        message_id = str(message.get("id") or "")
        channel_id = str(message.get("channelId") or "")
        creator_id = str(message.get("creatorId") or "")
        if (
            not message_id
            or not creator_id
            or channel_id not in self._channel_ids
            or creator_id == self._bot_user_id
            or message.get("creatorIsBot") is True
            or not self._remember(message_id)
        ):
            return

        mentioned = _mentions_user(message.get("body"), self._bot_user_id)
        parent_id = message.get("parentMessageId")
        parent: dict[str, Any] | None = None
        reply_to_bot = False
        if isinstance(parent_id, str) and parent_id:
            try:
                parent = await self._message_by_id(channel_id, parent_id)
                reply_to_bot = parent is not None and parent.get("creatorId") == self._bot_user_id
            except CommonGroundApiError as exc:
                logger.warning("[Common Ground] could not resolve reply target: %s", exc)
        if not mentioned and not reply_to_bot:
            return

        text = _extract_text(message.get("body"))
        if not text:
            text = "The user mentioned you without additional text. Respond naturally."
        source = self.build_source(
            chat_id=channel_id,
            chat_name=channel_id,
            chat_type="group",
            user_id=creator_id,
            user_name=creator_id,
            scope_id=self._community_id,
            message_id=message_id,
        )
        normalized = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=message_id,
            reply_to_message_id=str(parent_id) if parent_id else None,
            reply_to_text=_extract_text(parent.get("body")) if parent else None,
            reply_to_author_id=str(parent.get("creatorId")) if parent else None,
            reply_to_is_own_message=reply_to_bot,
            raw_message=message,
            timestamp=_parse_timestamp(message.get("createdAt")),
            metadata={
                "commonground_mentioned": mentioned,
                "commonground_reply_to_bot": reply_to_bot,
            },
        )
        await self.handle_message(normalized)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if chat_id not in self._channel_ids:
            return SendResult(success=False, error="Channel is not allowlisted")
        body = content[:MAX_MESSAGE_LENGTH].strip()
        if not body:
            return SendResult(success=False, error="Message is empty")
        try:
            data = await self._api_post(
                "/api/bot/v1/messages/createMessage",
                {
                    "id": str(uuid.uuid4()),
                    "access": {
                        "communityId": self._community_id,
                        "channelId": chat_id,
                    },
                    "body": {
                        "version": PROTOCOL_VERSION,
                        "content": [{"type": "text", "value": body}],
                    },
                    "parentMessageId": reply_to,
                    "attachments": [],
                },
            )
            message_id = str(data.get("id") or "") if isinstance(data, dict) else ""
            return SendResult(success=True, message_id=message_id or None, raw_response=data)
        except CommonGroundApiError as exc:
            retryable = exc.status_code is None or exc.status_code >= 500
            return SendResult(success=False, error=str(exc), retryable=retryable)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Common Ground Bot API v1 has no typing-indicator endpoint."""

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "group", "scope_id": self._community_id}


def check_requirements() -> bool:
    return socketio is not None and bool(os.getenv("COMMONGROUND_BOT_TOKEN", "").strip())


def validate_config(config: PlatformConfig) -> bool:
    extra = config.extra or {}
    token = os.getenv("COMMONGROUND_BOT_TOKEN") or config.token or extra.get("token")
    community_id = os.getenv("COMMONGROUND_COMMUNITY_ID") or extra.get("community_id")
    channels = os.getenv("COMMONGROUND_CHANNEL_IDS") or extra.get("channel_ids")
    return bool(token and community_id and _csv(channels))


def is_connected(config: PlatformConfig) -> bool:
    return validate_config(config)


def _env_enablement() -> dict[str, Any] | None:
    token = os.getenv("COMMONGROUND_BOT_TOKEN", "").strip()
    community_id = os.getenv("COMMONGROUND_COMMUNITY_ID", "").strip()
    channel_ids = _csv(os.getenv("COMMONGROUND_CHANNEL_IDS"))
    if not token or not community_id or not channel_ids:
        return None
    return {
        "url": os.getenv("COMMONGROUND_URL", DEFAULT_URL).strip() or DEFAULT_URL,
        "community_id": community_id,
        "channel_ids": channel_ids,
    }


def register(ctx) -> None:
    ctx.register_platform(
        name="commonground",
        label="Common Ground",
        adapter_factory=lambda cfg: CommonGroundAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=[
            "COMMONGROUND_BOT_TOKEN",
            "COMMONGROUND_COMMUNITY_ID",
            "COMMONGROUND_CHANNEL_IDS",
            "COMMONGROUND_ALLOWED_USERS",
        ],
        install_hint="Install Hermes with the messaging extra",
        env_enablement_fn=_env_enablement,
        allowed_users_env="COMMONGROUND_ALLOWED_USERS",
        allow_all_env="COMMONGROUND_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="🌐",
        pii_safe=True,
        allow_update_command=False,
        platform_hint=(
            "You are speaking as the Common Ground platform agent in a shared "
            "community channel. You were invoked by an explicit @mention or a "
            "direct reply. Use concise Markdown, distinguish proposals from "
            "approved decisions, and never claim that code, governance, or "
            "deployment work happened unless a trusted handoff reports it."
        ),
    )
