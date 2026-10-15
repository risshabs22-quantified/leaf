"""Web Push with VAPID (RFC 8291 encryption, RFC 8292 VAPID) through pywebpush.

The private key is a Worker secret (VAPID_PRIVATE_KEY, PEM), never printed or logged; the
public key is served to browsers. A push service answering 404 or 410 means the browser
unsubscribed: the subscription is deleted.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("leaf.push")

PUSH_TTL_S = 12 * 3600  # a push not delivered within 12 hours is no longer useful


@dataclass
class PushResult:
    ok: bool
    gone: bool = False
    status: int | None = None


class WebPush:
    def __init__(self, private_key_pem: str, subject: str):
        from py_vapid import Vapid02

        self.vapid = Vapid02.from_pem(private_key_pem.encode())
        self.claims = {"sub": subject}

    async def send(self, sub: dict[str, Any], payload: dict[str, Any]) -> PushResult:
        from pywebpush import WebPushException, webpush_async

        info = {"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}
        try:
            resp = await webpush_async(
                info,
                json.dumps(payload, ensure_ascii=False),
                vapid_private_key=self.vapid,
                vapid_claims=dict(self.claims),
                ttl=PUSH_TTL_S,
                timeout=15,
            )
        except WebPushException as exc:
            status = getattr(exc.response, "status", None) or getattr(
                exc.response, "status_code", None
            )
            log.warning("push failed: HTTP %s", status)
            return PushResult(ok=False, gone=status in (404, 410), status=status)
        except Exception as exc:  # noqa: BLE001 - a network error is a failed send, not a crash
            log.warning("push failed: %s", type(exc).__name__)
            return PushResult(ok=False)
        status = getattr(resp, "status", None) or getattr(resp, "status_code", None)
        return PushResult(ok=True, status=status)
