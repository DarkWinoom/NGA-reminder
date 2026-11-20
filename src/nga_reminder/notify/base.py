"""The notification interface."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Notifier(Protocol):
    """Something that can show a message to the user."""

    def notify(self, title: str, body: str, *, url: str = "") -> bool:
        """Show a message.

        Args:
            title: short headline.
            body: the message itself.
            url: optional link opened when the notification is clicked.

        Returns:
            ``True`` if the message was handed to the OS successfully.
        """
        ...
