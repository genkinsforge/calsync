"""Connector interface shared by Google, Microsoft Graph and test fakes.

A connector reads every event instance in a window (recurrences expanded) and
writes calsync's own mirror events. The engine only asks a connector to update
or delete an event it has just seen in the listing carrying the matching
marker, and connectors re-check the marker before writing as a second guard.
"""
from __future__ import annotations

from datetime import datetime

from .model import Event


class MarkerMismatch(Exception):
    """Refused to touch an event that does not carry the expected calsync marker."""


class Connector:
    name: str

    def list_events(self, start: datetime, end: datetime) -> list[Event]:
        raise NotImplementedError

    def create(self, payload: dict, marker: str) -> str:
        raise NotImplementedError

    def update(self, event_id: str, payload: dict, marker: str) -> None:
        raise NotImplementedError

    def delete(self, event_id: str, marker: str) -> None:
        raise NotImplementedError
