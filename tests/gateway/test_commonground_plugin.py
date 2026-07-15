"""Tests for the Common Ground Bot API v1 platform plugin."""

import asyncio

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
    community_id="community-1",
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
            "communityId": community_id,
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
    ):
        monkeypatch.delenv(key, raising=False)

    from gateway.config import PlatformConfig

    instance = CommonGroundAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "url": "https://cg.example.test",
            },
        )
    )
    instance._bot_user_id = "bot-1"
    instance._scopes = {
        "channel-1": {
            "communityId": "community-1",
            "communityTitle": "First Community",
            "channelId": "channel-1",
            "channelTitle": "General",
        }
    }
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

        from gateway.config import PlatformConfig

        instance = CommonGroundAdapter(
            PlatformConfig(
                enabled=True,
                token="yaml-token",
                extra={
                    "url": "https://yaml.example",
                    "channel_ids": ["restricted-channel"],
                },
            )
        )
        assert instance._base_url == "https://env.example"
        assert instance._token == "env-token"
        assert instance._allowed_channel_ids == {"restricted-channel"}

    def test_validate_requires_only_token(self, monkeypatch):
        from gateway.config import PlatformConfig

        monkeypatch.delenv("COMMONGROUND_BOT_TOKEN", raising=False)
        config = PlatformConfig(enabled=True, extra={})
        assert validate_config(config) is False

        config = PlatformConfig(
            enabled=True,
            token="token",
            extra={},
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
    async def test_second_community_uses_event_scope(self, adapter):
        adapter._scopes["channel-2"] = {
            "communityId": "community-2",
            "communityTitle": "Second Community",
            "channelId": "channel-2",
            "channelTitle": "General",
        }
        adapter.handle_message = AsyncMock()

        await adapter._on_message_event(
            _message(
                message_id="message-2",
                community_id="community-2",
                channel_id="channel-2",
                content=[{"type": "mention", "userId": "bot-1"}],
            )
        )

        normalized = adapter.handle_message.await_args.args[0]
        assert normalized.source.scope_id == "community-2"
        assert normalized.source.chat_id == "channel-2"

    @pytest.mark.asyncio
    async def test_scope_refresh_replaces_routes(self, adapter):
        adapter._api_post = AsyncMock(
            return_value={
                "items": [
                    {
                        "communityId": "community-2",
                        "communityTitle": "Second Community",
                        "channelId": "channel-2",
                        "channelTitle": "General",
                    }
                ],
                "nextCursor": None,
            }
        )

        await adapter._on_scopes_event({"action": "refresh", "data": {}})

        assert set(adapter._scopes) == {"channel-2"}

    @pytest.mark.asyncio
    async def test_config_channel_restriction_filters_server_scopes(self, adapter):
        adapter._allowed_channel_ids = {"channel-1"}
        adapter._api_post = AsyncMock(
            return_value={
                "items": [
                    {
                        "communityId": "community-2",
                        "communityTitle": "Second Community",
                        "channelId": "channel-2",
                        "channelTitle": "General",
                    }
                ],
                "nextCursor": None,
            }
        )

        await adapter._refresh_scopes()

        assert adapter._scopes == {}

    @pytest.mark.asyncio
    async def test_scope_refresh_follows_pagination(self, adapter):
        adapter._api_post = AsyncMock(
            side_effect=[
                {
                    "items": [
                        {
                            "communityId": "community-1",
                            "communityTitle": "First Community",
                            "channelId": "channel-1",
                            "channelTitle": "General",
                        }
                    ],
                    "nextCursor": "next-page",
                },
                {
                    "items": [
                        {
                            "communityId": "community-2",
                            "communityTitle": "Second Community",
                            "channelId": "channel-2",
                            "channelTitle": "General",
                        }
                    ],
                    "nextCursor": None,
                },
            ]
        )

        await adapter._refresh_scopes()

        assert set(adapter._scopes) == {"channel-1", "channel-2"}
        assert adapter._api_post.await_args_list[1].args[1]["cursor"] == "next-page"

    @pytest.mark.asyncio
    async def test_duplicate_event_is_dispatched_once(self, adapter):
        adapter.handle_message = AsyncMock()
        event = _message(content=[{"type": "mention", "userId": "bot-1"}])
        await adapter._on_message_event(event)
        await adapter._on_message_event(event)
        adapter.handle_message.assert_awaited_once()


class TestConnectionRecovery:
    @pytest.mark.asyncio
    async def test_server_disconnect_queues_gateway_recovery(self, adapter):
        fatal_handler = AsyncMock()
        adapter.set_fatal_error_handler(fatal_handler)
        adapter._mark_connected()

        await adapter._on_disconnect("server disconnect")
        await adapter._disconnect_recovery_task

        assert adapter.is_connected is False
        assert adapter.fatal_error_code == "server_disconnect"
        assert adapter.fatal_error_retryable is True
        fatal_handler.assert_awaited_once_with(adapter)

    @pytest.mark.asyncio
    async def test_transport_disconnect_leaves_reconnect_to_socket_client(self, adapter):
        fatal_handler = AsyncMock()
        adapter.set_fatal_error_handler(fatal_handler)
        adapter._mark_connected()

        await adapter._on_disconnect("transport error")
        await asyncio.sleep(0)

        assert adapter.is_connected is False
        assert adapter.has_fatal_error is False
        fatal_handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_intentional_disconnect_does_not_queue_recovery(self, adapter):
        fatal_handler = AsyncMock()
        adapter.set_fatal_error_handler(fatal_handler)
        adapter._shutting_down = True

        await adapter._on_disconnect("server disconnect")
        await asyncio.sleep(0)

        assert adapter._disconnect_recovery_task is None
        fatal_handler.assert_not_awaited()


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
    async def test_directory_lists_server_granted_scopes(self, adapter):
        assert await adapter.get_channel_directory_entries() == [
            {
                "id": "channel-1",
                "name": "General",
                "type": "group",
                "community": "First Community",
            }
        ]

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
