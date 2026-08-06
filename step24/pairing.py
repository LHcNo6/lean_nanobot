from __future__ import annotations

import json
import os
import secrets
import string
import threading
import time
from pathlib import Path
from typing import Any

PAIRING_CODE_META_KEY = "_pairing_code"


class PairingStore:
    """Persistent pairing store for DM sender approval.

    Approved senders and pending pairing codes per channel, stored in a
    small JSON file (aligned with nanobot's pairing/store.py).
    """

    _ALPHABET = string.ascii_uppercase + string.digits
    _CODE_LENGTH = 8  # e.g. ABCD-EFGH
    _TTL_DEFAULT_S = 600  # 10 minutes

    def __init__(self, path: Path | str = Path("pairing.json")) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {"approved": {}, "pending": {}}
        except (json.JSONDecodeError, OSError):
            print(f"[pairing] Corrupted pairing store {self.path}, resetting")
            return {"approved": {}, "pending": {}}
        for channel, users in data.get("approved", {}).items():
            data["approved"][channel] = {str(u) for u in users}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "approved": {ch: sorted(users) for ch, users in data.get("approved", {}).items()},
            "pending": dict(data.get("pending", {})),
        }
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, indent=2, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def _gc_pending(self, data: dict[str, Any]) -> None:
        now = time.time()
        pending = data.get("pending", {})
        expired = [code for code, info in pending.items() if info.get("expires_at", 0) < now]
        for code in expired:
            del pending[code]

    def generate_code(self, channel: str, sender_id: str, ttl: int = _TTL_DEFAULT_S) -> str:
        """Create a new pairing code for *sender_id* on *channel* (e.g. ``"ABCD-EFGH"``)."""
        with self._lock:
            data = self._load()
            self._gc_pending(data)
            raw = "".join(secrets.choice(self._ALPHABET) for _ in range(self._CODE_LENGTH))
            code = f"{raw[:4]}-{raw[4:]}"
            data.setdefault("pending", {})[code] = {
                "channel": channel,
                "sender_id": str(sender_id),
                "created_at": time.time(),
                "expires_at": time.time() + ttl,
            }
            self._save(data)
            return code

    def approve_code(self, code: str) -> tuple[str, str] | None:
        """Approve a pending pairing code. Returns ``(channel, sender_id)`` or ``None``."""
        with self._lock:
            data = self._load()
            self._gc_pending(data)
            pending = data.get("pending", {})
            info = pending.pop(code, None)
            if info is None:
                return None
            channel = info["channel"]
            sender_id = str(info["sender_id"])
            data.setdefault("approved", {}).setdefault(channel, set()).add(sender_id)
            self._save(data)
            return channel, sender_id

    def deny_code(self, code: str) -> bool:
        """Reject and discard a pending pairing code. Returns True if it existed."""
        with self._lock:
            data = self._load()
            self._gc_pending(data)
            pending = data.get("pending", {})
            if code in pending:
                del pending[code]
                self._save(data)
                return True
            return False

    def is_approved(self, channel: str, sender_id: str) -> bool:
        """Check whether *sender_id* has been approved on *channel*."""
        with self._lock:
            data = self._load()
            approved = data.get("approved", {})
            return str(sender_id) in approved.get(channel, set())

    def list_pending(self) -> list[dict[str, Any]]:
        """Return all non-expired pending pairing requests."""
        with self._lock:
            data = self._load()
            self._gc_pending(data)
            return [{"code": code, **info} for code, info in data.get("pending", {}).items()]

    def revoke(self, channel: str, sender_id: str) -> bool:
        """Remove an approved sender from *channel*. Returns True if removed."""
        with self._lock:
            data = self._load()
            approved = data.get("approved", {})
            users = approved.get(channel, set())
            sid = str(sender_id)
            if sid in users:
                users.discard(sid)
                if not users:
                    del approved[channel]
                self._save(data)
                return True
            return False

    def revoke_channel(self, channel: str) -> int:
        """Remove all approved sender IDs for *channel*. Returns count removed."""
        with self._lock:
            data = self._load()
            approved = data.get("approved", {})
            users = approved.pop(channel, set())
            if not users:
                return 0
            self._save(data)
            return len(users)

    def clear_channel(self, channel: str) -> dict[str, int]:
        """Remove approved senders and pending requests for *channel*."""
        with self._lock:
            data = self._load()
            approved = data.get("approved", {})
            approved_users = approved.pop(channel, set())
            pending = data.get("pending", {})
            pending_codes = [
                code
                for code, info in pending.items()
                if str(info.get("channel", "")) == channel
            ]
            for code in pending_codes:
                del pending[code]
            if not approved_users and not pending_codes:
                return {"approved": 0, "pending": 0}
            self._save(data)
            return {"approved": len(approved_users), "pending": len(pending_codes)}

    def get_approved(self, channel: str) -> list[str]:
        """Return all approved sender IDs for *channel*."""
        with self._lock:
            data = self._load()
            return sorted(data.get("approved", {}).get(channel, set()))

    @staticmethod
    def format_pairing_reply(code: str) -> str:
        return (
            "Hi there! This assistant only responds to approved users.\n\n"
            f"Your pairing code is: `{code}`\n\n"
            "To get access, ask the owner to approve this request.\n"
            "The owner can also send `/pairing approve {code}`."
        )

    def handle_pairing_command(self, channel: str, subcommand_text: str) -> str:
        """Execute a pairing subcommand and return the reply text."""
        parts = subcommand_text.split()
        sub = parts[0] if parts else "list"
        arg = parts[1] if len(parts) > 1 else None

        if sub == "list":
            pending = self.list_pending()
            if not pending:
                return "No pending pairing requests."
            lines = ["Pending pairing requests:"]
            for item in pending:
                lines.append(f"- `{item['code']}` | {item['channel']} | {item['sender_id']}")
            return "\n".join(lines)
        if sub == "approve":
            if arg is None:
                return "Usage: `/pairing approve <code>`"
            result = self.approve_code(arg)
            if result is None:
                return f"Invalid or expired pairing code: `{arg}`"
            ch, sid = result
            return f"Approved pairing code `{arg}` — {sid} can now access {ch}"
        if sub == "deny":
            if arg is None:
                return "Usage: `/pairing deny <code>`"
            if self.deny_code(arg):
                return f"Denied pairing code `{arg}`"
            return f"Pairing code `{arg}` not found or already expired"
        if sub == "revoke":
            if len(parts) == 2:
                return (
                    f"Revoked {arg} from {channel}"
                    if self.revoke(channel, arg)
                    else f"{arg} was not in the approved list for {channel}"
                )
            if len(parts) == 3:
                return (
                    f"Revoked {parts[2]} from {arg}"
                    if self.revoke(arg, parts[2])
                    else f"{parts[2]} was not in the approved list for {arg}"
                )
            return "Usage: `/pairing revoke <user_id>` or `/pairing revoke <channel> <user_id>`"
        return (
            "Unknown pairing command.\n"
            "Usage: `/pairing [list|approve <code>|deny <code>|revoke <user_id>|revoke <channel> <user_id>]`"
        )
