"""Tests for the Common Ground Bot API v1 platform plugin."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_cg = load_plugin_adapter("commonground")

CommonGroundAdapter = _cg.CommonGroundAdapter
CommonGroundApiError = _cg.CommonGroundApiError
_csv = _cg._csv
_extract_text = _cg._extract_text
_mentions_user = _cg._mentions_user
check_requirements = _cg.check_requirements
validate_config = _cg.validate_config
register = _cg.register


def _message(
    *,
    message_id="message-1",
    creator_id="user-1",
    channel_id="channel-1",
    content=None,
    parent_id=None,
    creator_is_bot=False,
):
    return {
        "action": "new",
        "data": {
            "id": message_id,
            "creatorId": creator_id,
            "creatorIsBot": creator_is_bot,
            "channelId": channel_id,
            "body": {
                "version": "1",
                "content": content
                if content is not None
                else [{"type": "text", "value": "hello"}],
            },
            "parentMessageId": parent_id,
            "createdAt": "2026-07-14T12:00:00.000Z",
        },
    }


@pytest.fixture
def adapter(monkeypatch):
    for key in (
        "COMMONGROUND_URL",
        "COMMONGROUND_BOT_TOKEN",
        "COMMONGROUND_COMMUNITY_ID",
        "COMMONGROUND_CHANNEL_IDS",
    ):
        monkeypatch.delenv(key, raising=False)

    from gateway.config import PlatformConfig

    instance = CommonGroundAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "url": "https://cg.example.test",
                "community_id": "community-1",
                "channel_ids": ["channel-1"],
            },
        )
    )
    instance._bot_user_id = "bot-1"
    return instance


class TestHelpers:
    def test_csv_normalizes_strings_and_lists(self):
        assert _csv(" a, b ,,c ") == ["a", "b", "c"]
        assert _csv([" a ", "", "b"]) == ["a", "b"]

    def test_extract_text_ignores_mentions_but_preserves_lines(self):
        body = {
            "version": "1",
            "content": [
                {"type": "mention", "userId": "bot-1", "alias": "Symbient"},
                {"type": "text", "value": " first"},
                {"type": "newline"},
                {"type": "text", "value": "second "},
            ],
        }
        assert _extract_text(body) == "first\nsecond"

    def test_structured_mention_matches_user_id(self):
        body = {
            "version": "1",
            "content": [{"type": "mention", "userId": "bot-1"}],
        }
        assert _mentions_user(body, "bot-1") is True
        assert _mentions_user(body, "bot-2") is False


class TestConfiguration:
    def test_env_overrides_yaml(self, monkeypatch):
        monkeypatch.setenv("COMMONGROUND_URL", "https://env.example")
        monkeypatch.setenv("COMMONGROUND_BOT_TOKEN", "env-token")
        monkeypatch.setenv("COMMONGROUND_COMMUNITY_ID", "env-community")
        monkeypatch.setenv("COMMONGROUND_CHANNEL_IDS", "env-channel-1,env-channel-2")

        from gateway.config import PlatformConfig

        instance = CommonGroundAdapter(
            PlatformConfig(
                enabled=True,
                token="yaml-token",
                extra={
                    "url": "https://yaml.example",
                    "community_id": "yaml-community",
                    "channel_ids": ["yaml-channel"],
                },
            )
        )
        assert instance._base_url == "https://env.example"
        assert instance._token == "env-token"
        assert instance._community_id == "env-community"
        assert instance._channel_ids == {"env-channel-1", "env-channel-2"}

    def test_validate_requires_token_community_and_channel(self, monkeypatch):
        from gateway.config import PlatformConfig

        monkeypatch.delenv("COMMONGROUND_BOT_TOKEN", raising=False)
        config = PlatformConfig(enabled=True, extra={})
        assert validate_config(config) is False

        config = PlatformConfig(
            enabled=True,
            token="token",
            extra={"community_id": "community", "channel_ids": ["channel"]},
        )
        assert validate_config(config) is True

    def test_requirements_need_dependency_and_token(self, monkeypatch):
        monkeypatch.setenv("COMMONGROUND_BOT_TOKEN", "token")
        monkeypatch.setattr(_cg, "socketio", object())
        assert check_requirements() is True
        monkeypatch.delenv("COMMONGROUND_BOT_TOKEN")
        assert check_requirements() is False


class TestInboundRouting:
    @pytest.mark.asyncio
    async def test_structured_mention_dispatches(self, adapter):
        adapter.handle_message = AsyncMock()
        event = _message(
            content=[
                {"type": "mention", "userId": "bot-1", "alias": "Symbient"},
                {"type": "text", "value": " please summarize this"},
            ]
        )

        await adapter._on_message_event(event)

        adapter.handle_message.assert_awaited_once()
        normalized = adapter.handle_message.await_args.args[0]
        assert normalized.text == "please summarize this"
        assert normalized.source.user_id == "user-1"
        assert normalized.source.chat_id == "channel-1"
        assert normalized.source.scope_id == "community-1"
        assert normalized.metadata["commonground_mentioned"] is True

    @pytest.mark.asyncio
    async def test_reply_to_bot_dispatches_with_reply_context(self, adapter):
        adapter.handle_message = AsyncMock()
        adapter._message_by_id = AsyncMock(
            return_value={
                "id": "parent-1",
                "creatorId": "bot-1",
                "body": {"version": "1", "content": [{"type": "text", "value": "Earlier answer"}]},
            }
        )

        await adapter._on_message_event(_message(parent_id="parent-1"))

        normalized = adapter.handle_message.await_args.args[0]
        assert normalized.reply_to_is_own_message is True
        assert normalized.reply_to_text == "Earlier answer"
        assert normalized.metadata["commonground_reply_to_bot"] is True

    @pytest.mark.asyncio
    async def test_unaddressed_channel_message_is_ignored(self, adapter):
        adapter.handle_message = AsyncMock()
        await adapter._on_message_event(_message())
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_wrong_channel_and_bot_messages_are_ignored(self, adapter):
        adapter.handle_message = AsyncMock()
        mention = [{"type": "mention", "userId": "bot-1"}]
        await adapter._on_message_event(_message(channel_id="other", content=mention))
        await adapter._on_message_event(_message(message_id="m2", content=mention, creator_is_bot=True))
        await adapter._on_message_event(_message(message_id="m3", creator_id="bot-1", content=mention))
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_duplicate_event_is_dispatched_once(self, adapter):
        adapter.handle_message = AsyncMock()
        event = _message(content=[{"type": "mention", "userId": "bot-1"}])
        await adapter._on_message_event(event)
        await adapter._on_message_event(event)
        adapter.handle_message.assert_awaited_once()


class TestOutboundApi:
    @pytest.mark.asyncio
    async def test_send_posts_reply_through_bot_api(self, adapter):
        adapter._api_post = AsyncMock(return_value={"id": "reply-1"})

        result = await adapter.send("channel-1", "response", reply_to="message-1")

        assert result.success is True
        assert result.message_id == "reply-1"
        path, payload = adapter._api_post.await_args.args
        assert path == "/api/bot/v1/messages/createMessage"
        assert payload["access"] == {
            "communityId": "community-1",
            "channelId": "channel-1",
        }
        assert payload["parentMessageId"] == "message-1"
        assert payload["body"]["content"] == [{"type": "text", "value": "response"}]

    @pytest.mark.asyncio
    async def test_send_refuses_non_allowlisted_channel(self, adapter):
        adapter._api_post = AsyncMock()
        result = await adapter.send("other-channel", "response")
        assert result.success is False
        adapter._api_post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_marks_server_errors_retryable(self, adapter):
        adapter._api_post = AsyncMock(side_effect=CommonGroundApiError("unavailable", 503))
        result = await adapter.send("channel-1", "response")
        assert result.success is False
        assert result.retryable is True

    @pytest.mark.asyncio
    async def test_api_rejects_non_ok_envelope(self, adapter):
        async def handler(request):
            return httpx.Response(200, json={"status": "ERROR", "error": "secret detail"})

        adapter._http = httpx.AsyncClient(
            base_url="https://cg.example.test",
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(CommonGroundApiError, match="API error") as exc:
                await adapter._api_post("/api/bot/v1/whoami", {})
            assert "secret detail" not in str(exc.value)
        finally:
            await adapter._http.aclose()


def test_register_exposes_platform_security_contract():
    ctx = MagicMock()
    register(ctx)
    kwargs = ctx.register_platform.call_args.kwargs
    assert kwargs["name"] == "commonground"
    assert kwargs["allowed_users_env"] == "COMMONGROUND_ALLOWED_USERS"
    assert kwargs["allow_all_env"] == "COMMONGROUND_ALLOW_ALL_USERS"
    assert kwargs["allow_update_command"] is False
