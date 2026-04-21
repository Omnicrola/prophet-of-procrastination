"""
Dominions 5/6 direct TCP status query.

Protocol reference:
  http://www.cs.helsinki.fi/u/aitakang/dom3_serverformat_notes
  https://github.com/djmcgill/dominions-5-status (src/server/)

The binary protocol allows querying a Dominions server directly without
needing the HTTP status page. It is used as a secondary/fallback data source.

Packet structure (client -> server):
  [2 bytes LE] total payload length
  [1 byte]     message type  (0x05 = status request)
  [N bytes]    game name, null-padded to fill the packet

Response structure (server -> client):
  [2 bytes LE] total payload length
  [1 byte]     message type  (0x08 = status response)
  [36 bytes]   game name, null-terminated
  [2 bytes LE] turn number
  [4 bytes LE] time remaining (in hours — verify against your server version)
  [1 byte]     number of nations
  [per nation] 1 byte nation ID + 1 byte status flags

Nation status flags:
  0x01  submitted (player has sent their turn orders)
  0x02  is a human player
  0x04  is AI-controlled
  0x08  slot is closed/empty
  0x10  player is defeated

NOTE: This is a best-effort implementation derived from community documentation.
The exact byte layout (especially time units and response type byte) may differ
between Dom3, Dom5, and Dom6. If TCP queries consistently return None, verify
the protocol by capturing a session with Wireshark and comparing against the
Helsinki notes. The HTML status page scraper is the primary data source.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Optional

from bot.models import GameState, NationStatus

logger = logging.getLogger(__name__)

_MSG_STATUS_REQ = 0x05
_MSG_STATUS_RESP = 0x08

_FLAG_SUBMITTED = 0x01
_FLAG_HUMAN = 0x02
_FLAG_AI = 0x04
_FLAG_CLOSED = 0x08
_FLAG_DEFEATED = 0x10

_NAME_FIELD_LEN = 36


async def query_server(
    host: str,
    port: int,
    game_name: str,
    timeout: float = 5.0,
) -> Optional[GameState]:
    """
    Query a Dominions game server via the TCP status protocol.
    Returns None on any failure — callers should fall back to the HTML scraper.
    """
    try:
        return await asyncio.wait_for(
            _do_query(host, port, game_name),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.debug("TCP timeout querying %s:%d for %s", host, port, game_name)
    except ConnectionRefusedError:
        logger.debug("TCP connection refused: %s:%d", host, port)
    except OSError as exc:
        logger.debug("TCP OS error querying %s:%d: %s", host, port, exc)
    except Exception as exc:
        logger.debug("TCP unexpected error querying %s:%d: %s", host, port, exc)
    return None


async def _do_query(host: str, port: int, game_name: str) -> Optional[GameState]:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        packet = _build_request(game_name)
        writer.write(packet)
        await writer.drain()

        # Read the 2-byte length header first
        header = await reader.readexactly(2)
        pkt_len = struct.unpack_from("<H", header)[0]
        if pkt_len < 1 or pkt_len > 4096:
            logger.debug("TCP response length out of range: %d", pkt_len)
            return None

        body = await reader.readexactly(pkt_len)
        return _parse_response(header + body, game_name)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def _build_request(game_name: str) -> bytes:
    name_bytes = game_name.encode("ascii", errors="ignore")[:_NAME_FIELD_LEN]
    name_bytes = name_bytes.ljust(_NAME_FIELD_LEN, b"\x00")
    payload = bytes([_MSG_STATUS_REQ]) + name_bytes
    return struct.pack("<H", len(payload)) + payload


def _parse_response(data: bytes, fallback_name: str) -> Optional[GameState]:
    try:
        offset = 0

        if len(data) < 3:
            return None

        _pkt_len = struct.unpack_from("<H", data, offset)[0]
        offset += 2

        msg_type = data[offset]
        offset += 1

        if msg_type != _MSG_STATUS_RESP:
            logger.debug("Unexpected TCP response type: 0x%02x", msg_type)
            # Try to parse anyway — some server versions use different type bytes

        # Game name (null-terminated, up to _NAME_FIELD_LEN bytes)
        name_end = data.find(b"\x00", offset)
        if name_end == -1 or name_end > offset + _NAME_FIELD_LEN:
            name_end = offset + _NAME_FIELD_LEN
        game_name = data[offset:name_end].decode("ascii", errors="ignore").strip()
        if not game_name:
            game_name = fallback_name
        offset += _NAME_FIELD_LEN

        if len(data) < offset + 6:
            logger.debug("TCP response too short for turn/time fields")
            return None

        turn_number = struct.unpack_from("<H", data, offset)[0]
        offset += 2

        # Time value — the Helsinki notes describe this as "time left" but do not
        # specify the unit clearly for Dom5/6. Hours is the most common interpretation
        # in community tools. If your server reports wrong times, check this.
        time_hours = struct.unpack_from("<I", data, offset)[0]
        offset += 4

        time_seconds = time_hours * 3600
        if time_hours > 0:
            days = time_hours // 24
            hrs = time_hours % 24
            parts = []
            if days:
                parts.append(f"{days}d")
            if hrs:
                parts.append(f"{hrs}h")
            time_str = " ".join(parts) or f"{time_hours}h"
        else:
            time_str = None
            time_seconds = None

        nations: list[NationStatus] = []

        if offset < len(data):
            nation_count = data[offset]
            offset += 1

            for _ in range(nation_count):
                if offset + 2 > len(data):
                    break
                nation_id = data[offset]
                flags = data[offset + 1]
                offset += 2

                is_closed = bool(flags & _FLAG_CLOSED)
                is_defeated = bool(flags & _FLAG_DEFEATED)
                is_human = bool(flags & _FLAG_HUMAN) and not is_closed and not is_defeated
                is_submitted = bool(flags & _FLAG_SUBMITTED)

                if is_closed:
                    continue

                # TCP protocol provides nation IDs, not names.
                # The HTML scraper is preferred when nation names are needed.
                nation_name = f"Nation #{nation_id}"
                nations.append(
                    NationStatus(name=nation_name, submitted=is_submitted, is_human=is_human)
                )

        if turn_number == 0:
            logger.debug("TCP response has turn_number=0, discarding")
            return None

        return GameState(
            game_name=game_name,
            turn_number=turn_number,
            time_remaining=time_str,
            time_remaining_seconds=time_seconds,
            nations=nations,
        )

    except struct.error as exc:
        logger.debug("TCP response parse error (struct): %s", exc)
    except Exception as exc:
        logger.debug("TCP response parse error: %s", exc)
    return None
