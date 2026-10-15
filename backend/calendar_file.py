"""Calendar reminders (.ics, RFC 5545) for check-ins and spray windows.

One small VEVENT with a reminder alarm, which Apple Calendar, Google Calendar and Outlook all
import. Built in code from fields the server validates, so a reminder can never carry a link
to another site.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator


class Reminder(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    start: datetime  # an absolute time (with a UTC offset or Z)
    end: datetime
    details: str = Field("", max_length=1500)
    path: str = Field("", max_length=200)  # a Leaf page such as /c/<id>; the host is added here
    alarm_minutes: int = Field(60, ge=0, le=1440)

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("times need a UTC offset")
        return v.astimezone(UTC)

    @field_validator("path")
    @classmethod
    def _local(cls, v: str) -> str:
        if v and (not v.startswith("/") or v.startswith("//") or "\\" in v):
            raise ValueError("path must be a Leaf page")
        return v


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """Lines longer than 75 octets continue on the next line after a space (RFC 5545 3.1)."""
    raw = line.encode()
    if len(raw) <= 75:
        return line
    parts, chunk = [], b""
    for ch in line:
        b = ch.encode()
        if len(chunk) + len(b) > (75 if not parts else 74):
            parts.append(chunk.decode())
            chunk = b""
        chunk += b
    parts.append(chunk.decode())
    return "\r\n ".join(parts)


def _stamp(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def ics(rem: Reminder, base_url: str, now: datetime | None = None) -> str:
    end = rem.end if rem.end > rem.start else rem.start
    url = base_url.rstrip("/") + rem.path if rem.path else ""
    details = rem.details + (f"\n\n{url}" if url else "")
    uid = hashlib.sha256(f"{rem.title}|{_stamp(rem.start)}|{rem.path}".encode()).hexdigest()[:24]
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Leaf plant doctor//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}@leaf",
        f"DTSTAMP:{_stamp(now or datetime.now(UTC))}",
        f"DTSTART:{_stamp(rem.start)}",
        f"DTEND:{_stamp(end)}",
        f"SUMMARY:{_escape(rem.title)}",
        f"DESCRIPTION:{_escape(details)}",
    ]
    if url:
        lines.append(f"URL:{url}")
    if rem.alarm_minutes:
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_escape(rem.title)}",
            f"TRIGGER:-PT{rem.alarm_minutes}M",
            "END:VALARM",
        ]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
