"""Telegram I/O Gateway (Modul 1).

Isolierter Userbot-Service: wandelt private Telegram-Nachrichten in Events um
und führt Sendeaufträge mit Tipp-Status und Verzögerung aus.
"""

from .config import Config, ConfigError
from .core import GatewayError, TelegramGateway, UnknownUser, UserPaused
from .models import IncomingMessage, SendJob
from .pauselist import PauseList

__all__ = [
    "Config",
    "ConfigError",
    "GatewayError",
    "IncomingMessage",
    "PauseList",
    "SendJob",
    "TelegramGateway",
    "UnknownUser",
    "UserPaused",
]
