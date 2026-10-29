# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exceptions raised by pycul."""

from __future__ import annotations


class CulError(Exception):
    """Base class for all pycul errors."""


class CulConnectionError(CulError):
    """The CUL could not be opened or the connection was lost."""


class CulTimeoutError(CulError):
    """The CUL did not answer a command in time."""


class CulCreditError(CulError):
    """The CUL refused to send because its duty cycle credit is exhausted (LOVF)."""


class CulSendError(CulError):
    """The CUL reported an error while sending (e.g. LENERR, ZERR)."""


class NoAckError(CulError):
    """A radio device did not acknowledge a message."""


class NakError(CulError):
    """A radio device answered with a negative acknowledgement."""


class SendCancelledError(CulError):
    """A queued message was removed from the send queue before it was sent."""


class SendExpiredError(CulError):
    """A queued message could not be sent before its deadline."""
