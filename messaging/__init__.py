"""Platform-agnostic messaging layer (Codex-oriented exports).

This module historically exported ClaudeMessageHandler; we now prefer
CodexMessageHandler while preserving a backwards-compatible alias so
existing imports keep working.
"""

from .event_parser import parse_cli_event
from .handler import CodexMessageHandler
from .models import IncomingMessage
from .platforms.base import CLISession, MessagingPlatform, SessionManagerInterface
from .session import SessionStore
from .trees.data import MessageNode, MessageState, MessageTree
from .trees.queue_manager import TreeQueueManager

# Backwards compatibility: preserve the historical name
ClaudeMessageHandler = CodexMessageHandler

__all__ = [
    "CLISession",
    "ClaudeMessageHandler",  # legacy alias
    "CodexMessageHandler",
    "IncomingMessage",
    "MessageNode",
    "MessageState",
    "MessageTree",
    "MessagingPlatform",
    "SessionManagerInterface",
    "SessionStore",
    "TreeQueueManager",
    "parse_cli_event",
]
