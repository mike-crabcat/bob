"""Persona scripts for the rehearsal (§4.3): fake parties replying across
channels, including the historically-lost wrong-channel case."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class Persona:
    name: str            # "alice" — also the slug and roster key
    phone_digits: str    # "61400000101"

    @property
    def dm_session(self) -> str:
        return f"agent:main:whatsapp:dm:{self.phone_digits}"

    @property
    def jid(self) -> str:
        return f"{self.phone_digits}@s.whatsapp.net"


@dataclass
class Reply:
    trigger: str                 # regex over Bob's outbound message text
    text: str                    # what the persona says
    channel: str = "dm"          # where they say it ("dm" | "group")


# Baseline availability replies. The channel each persona uses is decided by
# the perturbation — the "wrong channel" cases are replies that land in the
# group instead of the DM Bob wrote to.
AVAILABILITY_TRIGGER = r"(availability|can you (do|make)|lunch on|which day)"
CONFIRM_TEXT = "I'm in for Thursday!"
CANCEL_TRIGGER = r"(booked|confirmed the booking|table is booked)"
CANCEL_TEXT = "can't make it anymore sorry, something came up"


def default_personas(count: int = 8) -> list[Persona]:
    names = ["alice", "bruno", "carol", "dan", "eve", "frank", "gina", "hank"]
    return [Persona(n, f"61400000{i:03d}") for i, n in enumerate(names[:count], 101)]


class FakeBridge:
    """Captures Bob's outbound WhatsApp sends (DM outreach rides this
    directly; group sends arrive here via the harness's whatsapp_send
    executor). The driver reacts to what Bob actually sent."""

    def __init__(self) -> None:
        self.connected = True
        self.outbox: list[tuple[str, str]] = []   # (chat_id, text)
        self.on_outbound: Callable[[str, str], Awaitable[None]] | None = None

    async def send_message(self, chat_id: str, text: str,
                           reply_to: str | None = None) -> str:
        self.outbox.append((chat_id, text))
        if self.on_outbound is not None:
            await self.on_outbound(chat_id, text)
        return f"req-{len(self.outbox)}"

    async def send_media(self, chat_id: str, file_path: str,
                         caption: str = "") -> str:
        self.outbox.append((chat_id, f"[media {file_path}] {caption}"))
        return f"req-{len(self.outbox)}"


class PersonaDriver:
    """Watches Bob's outbound messages and schedules persona replies in the
    scripted channel (dm = the historically-safe path; group = the
    wrong-channel case the benchmark is about)."""

    def __init__(self, personas: list[Persona], group_key: str,
                 channel_plan: dict[str, str]):
        self.personas = {p.name: p for p in personas}
        self.group_key = group_key
        self.channel_plan = channel_plan      # persona name → "dm" | "group"
        self.pending: list[tuple[int, Persona, str, str]] = []  # due, who, chan, text
        self.delivered: list[tuple[str, str, str]] = []         # who, chan, text
        self.cancel_armed = False

    def bind(self, bridge: FakeBridge) -> None:
        async def _on(chat_id: str, text: str) -> None:
            await self.on_outbound(chat_id, text)
        bridge.on_outbound = _on

    async def on_outbound(self, chat_id: str, text: str, tick: int = 0) -> None:
        # Group announcements reach everyone; the armed canceller replies there.
        if chat_id.endswith("@g.us"):
            if self.cancel_armed and re.search(CANCEL_TRIGGER, text, re.I):
                carol = self.personas.get("carol")
                if carol is not None:
                    self.pending.append((tick + 1, carol, "group", CANCEL_TEXT))
            return
        for persona in self.personas.values():
            if chat_id != persona.jid:
                continue
            if re.search(AVAILABILITY_TRIGGER, text, re.I):
                channel = self.channel_plan.get(persona.name, "dm")
                reply = CONFIRM_TEXT
                if persona.name == "eve":
                    reply = "I'm in for Thursday! (I'm vegetarian btw)"
                self.pending.append((tick + 1, persona, channel, reply))

    def arm_cancellation(self) -> None:
        self.cancel_armed = True

    async def deliver_due(self, tick: int, inject) -> list[tuple[str, str, str]]:
        due = [p for p in self.pending if p[0] <= tick]
        self.pending = [p for p in self.pending if p[0] > tick]
        for _, persona, channel, text in due:
            await inject(persona, channel, text)
            self.delivered.append((persona.name, channel, text))
        return self.delivered
