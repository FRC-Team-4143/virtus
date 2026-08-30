"""
Slack client helpers — DMs and channel posts.
"""
import logging
from typing import Optional

from slack_sdk.web.async_client import AsyncWebClient

from app.config import settings

log = logging.getLogger(__name__)

_client: Optional[AsyncWebClient] = None


def get_slack_client() -> AsyncWebClient:
    global _client
    if _client is None:
        _client = AsyncWebClient(token=settings.slack_bot_token)
    return _client


async def send_dm(slack_user_id: str, text: str, blocks=None, automated: bool = False) -> Optional[str]:
    """Open a DM with a user and post a message. Returns the message ts or None on failure.
    If automated=True, skips sending when updates_enabled is false."""
    if not slack_user_id:
        return None
    if automated and not settings.updates_enabled:
        return None
    client = get_slack_client()
    try:
        conv = await client.conversations_open(users=slack_user_id)
        channel_id = conv["channel"]["id"]
        result = await client.chat_postMessage(channel=channel_id, text=text, blocks=blocks)
        return result["ts"]
    except Exception:
        log.exception("send_dm failed for %s", slack_user_id)
        return None


async def post_to_channel(channel_id: str, text: str, blocks=None, automated: bool = True) -> Optional[str]:
    """Post a message to a channel. Returns the message ts or None on failure.
    If automated=True, skips sending when updates_enabled is false. The bot must be a
    member of the channel."""
    if not channel_id:
        return None
    if automated and not settings.updates_enabled:
        return None
    client = get_slack_client()
    try:
        result = await client.chat_postMessage(channel=channel_id, text=text, blocks=blocks)
        return result["ts"]
    except Exception:
        log.exception("post_to_channel failed for %s", channel_id)
        return None


async def update_message(channel_id: str, ts: str, text: str, blocks=None) -> bool:
    """Edit a previously posted message in place. Returns True on success."""
    if not channel_id or not ts:
        return False
    client = get_slack_client()
    try:
        await client.chat_update(channel=channel_id, ts=ts, text=text, blocks=blocks)
        return True
    except Exception:
        log.exception("update_message failed for %s/%s", channel_id, ts)
        return False
