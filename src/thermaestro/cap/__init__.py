"""The capability interface: one seam between the core and every plugin.

A plugin describes its devices as a tree of nodes with points to observe and levers to
act with, plus series of intervals (prices, grid rules, forecasts). The same messages
travel in process as objects and out of process as JSON lines over a local socket, so
any plugin can move out of process unchanged.
"""

from .carrier import BadLine, Closed, Endpoint, pair
from .client import CapError, Link, Subscription, UnexpectedReply
from .defaults import Assumed, assume
from .messages import MESSAGES, PROTOCOL, VERSION, Message
from .plugin import Plugin, Send, serve

__all__ = [
    "MESSAGES",
    "PROTOCOL",
    "VERSION",
    "Assumed",
    "BadLine",
    "CapError",
    "Closed",
    "Endpoint",
    "Link",
    "Message",
    "Plugin",
    "Send",
    "Subscription",
    "UnexpectedReply",
    "assume",
    "pair",
    "serve",
]
