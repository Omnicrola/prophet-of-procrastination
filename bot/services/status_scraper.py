"""
HTML status page scraper for Dominions 6 game servers.

Two formats are supported:

1. Illwinter hosted games (primary, confirmed working):
   URL pattern: http://ulm.illwinter.com/dom6/server/{GAME_NAME}.html
   Detected by: <td class="blackbolddata"> header row containing
                "GAMENAME, turn N (time left: Xh and Ym)"
   Nation rows: class "whitedata" / "lightgreydata", two cells —
                nation name | "Turn played" or "-"

2. Self-hosted / --statuspage format (generic fallback):
   The --statuspage flag writes an HTML file served by nginx/Apache.
   Structure varies, so the fallback uses multiple heuristic strategies.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup, Tag

from bot.models import GameState, NationStatus

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=10)

# ── Illwinter format ────────────────────────────────────────────────────────
# Header cell text: "KevSunday, turn 26 (time left: 22 hours and 49 minutes)"
_IW_HEADER_RE = re.compile(
    r"^(.+?),\s*turn\s+(\d+)\s*\(time left:\s*(.+?)\)\s*$",
    re.IGNORECASE,
)
# Time string inside the header: "22 hours and 49 minutes", "1 hour", "30 minutes", etc.
_IW_TIME_RE = re.compile(
    r"(?:(\d+)\s*hours?)?\s*(?:and\s*)?(?:(\d+)\s*minutes?)?",
    re.IGNORECASE,
)
_IW_DATA_CLASSES = {"whitedata", "lightgreydata", "blackdata", "greydata"}

# ── Generic format ──────────────────────────────────────────────────────────
_TURN_RE = re.compile(r"[Tt]urn[\s:]*(\d+)", re.IGNORECASE)

_TIME_PATTERNS = [
    re.compile(
        r"(?:(\d+)\s*d(?:ays?)?)?\s*(?:(\d+)\s*h(?:ours?)?)?\s*(?:(\d+)\s*m(?:in(?:utes?)?)?)?\s*(?:left|remain|until)",
        re.IGNORECASE,
    ),
    re.compile(r"(\d+):(\d{2})(?::(\d{2}))?"),
    re.compile(r"(?:hours?\s+left|hours?\s+remaining|left)[:\s]*(\d+)", re.IGNORECASE),
    re.compile(r"(\d+)\s*hours?\s*(?:left|remaining|until)", re.IGNORECASE),
]

_SUBMITTED_POSITIVE = re.compile(r"\b(submitted|played|done|yes|y|ok|true)\b", re.IGNORECASE)
_SUBMITTED_NEGATIVE = re.compile(r"\b(waiting|not\s+submitted|pending|no|n|false)\b", re.IGNORECASE)
_GREEN_RE = re.compile(r"(#0+[a-fA-F5-9]|green)", re.IGNORECASE)
_RED_RE = re.compile(r"(#[a-fA-F5-9][0-9a-fA-F]*00|red)", re.IGNORECASE)
_HUMAN_TYPES = re.compile(r"\b(human|player)\b", re.IGNORECASE)
_NON_HUMAN_TYPES = re.compile(r"\b(ai|computer|closed|empty|defeated|dead|gone)\b", re.IGNORECASE)


# ── Public API ───────────────────────────────────────────────────────────────

async def fetch_status(url: str, session: aiohttp.ClientSession) -> Optional[GameState]:
    """Fetch and parse a Dominions 6 HTML status page."""
    try:
        async with session.get(url, timeout=_TIMEOUT, allow_redirects=True) as resp:
            if resp.status != 200:
                logger.warning("Status page %s returned HTTP %d", url, resp.status)
                return None
            html = await resp.text(errors="replace")
    except aiohttp.ClientError as exc:
        logger.warning("HTTP error fetching %s: %s", url, exc)
        return None
    except Exception as exc:
        logger.warning("Unexpected error fetching %s: %s", url, exc)
        return None

    return _parse_html(html, url)


def illwinter_status_url(game_name: str) -> str:
    """Return the canonical Illwinter status page URL for a game name."""
    return f"http://ulm.illwinter.com/dom6/server/{game_name}.html"


# ── Parsing ──────────────────────────────────────────────────────────────────

def _parse_html(html: str, source: str = "") -> Optional[GameState]:
    soup = BeautifulSoup(html, "lxml")

    # Try the known Illwinter format first
    state = _parse_illwinter(soup)
    if state is not None:
        return state

    # Fall back to generic heuristic parser
    return _parse_generic(soup, source)


# ── Illwinter format parser ──────────────────────────────────────────────────

def _parse_illwinter(soup: BeautifulSoup) -> Optional[GameState]:
    """
    Parse the Illwinter-hosted game status page.

    Confirmed format (ulm.illwinter.com/dom6/server/GAMENAME.html):
      <td class="blackbolddata" colspan="2">
          GAMENAME, turn N (time left: Xh and Ym)
      </td>
      <tr>
          <td class="whitedata">Nation Name, Epithet</td>
          <td class="whitedata">Turn played</td>   <!-- or "-" if not submitted -->
      </tr>
      ... alternating whitedata / lightgreydata rows ...
    """
    header = soup.find("td", class_="blackbolddata")
    if header is None:
        return None

    header_text = header.get_text(strip=True)
    m = _IW_HEADER_RE.match(header_text)
    if not m:
        # Header exists but format is unexpected — still try to parse what we can
        logger.debug("Illwinter header found but regex did not match: %r", header_text)
        return None

    game_name = m.group(1).strip()
    turn_number = int(m.group(2))
    time_str_raw = m.group(3).strip()
    time_label, time_seconds = _parse_illwinter_time(time_str_raw)

    nations: list[NationStatus] = []
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        cell_classes = set(cells[0].get("class") or [])
        if not (cell_classes & _IW_DATA_CLASSES):
            continue

        nation_name = cells[0].get_text(strip=True)
        status_text = cells[1].get_text(strip=True)

        if not nation_name:
            continue

        submitted = status_text.lower() == "turn played"
        nations.append(NationStatus(name=nation_name, submitted=submitted, is_human=True))

    return GameState(
        game_name=game_name,
        turn_number=turn_number,
        time_remaining=time_label,
        time_remaining_seconds=time_seconds,
        nations=nations,
    )


def _parse_illwinter_time(raw: str) -> tuple[Optional[str], Optional[int]]:
    """Parse "22 hours and 49 minutes", "1 hour", "30 minutes", etc."""
    m = _IW_TIME_RE.search(raw)
    if not m or not any(m.groups()):
        return raw or None, None

    hours = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    total_seconds = hours * 3600 + mins * 60

    parts = []
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    label = " ".join(parts) if parts else raw

    return label, total_seconds if total_seconds > 0 else None


# ── Generic heuristic parser (self-hosted / --statuspage) ───────────────────

def _parse_generic(soup: BeautifulSoup, source: str = "") -> Optional[GameState]:
    game_name = _generic_game_name(soup)
    turn_number = _generic_turn_number(soup)
    time_remaining, time_seconds = _generic_time(soup)
    nations = _generic_nations(soup)

    if turn_number is None:
        logger.warning("Could not parse turn number from %s", source)
        return None

    return GameState(
        game_name=game_name or "Unknown Game",
        turn_number=turn_number,
        time_remaining=time_remaining,
        time_remaining_seconds=time_seconds,
        nations=nations,
    )


def _generic_game_name(soup: BeautifulSoup) -> Optional[str]:
    for tag in ("h1", "h2", "h3"):
        el = soup.find(tag)
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)
    title = soup.find("title")
    if title:
        text = re.sub(r"\s*[-–]\s*(status|page|dom\d*).*$", "", title.get_text(strip=True), flags=re.IGNORECASE).strip()
        # Strip "Dom6 - " prefix used by Illwinter titles
        text = re.sub(r"^dom\d+\s*[-–]\s*", "", text, flags=re.IGNORECASE).strip()
        if text:
            return text
    return None


def _generic_turn_number(soup: BeautifulSoup) -> Optional[int]:
    for text in soup.stripped_strings:
        m = _TURN_RE.search(text)
        if m:
            return int(m.group(1))
    return None


def _generic_time(soup: BeautifulSoup) -> tuple[Optional[str], Optional[int]]:
    full_text = soup.get_text(separator=" ")

    m = _TIME_PATTERNS[0].search(full_text)
    if m and any(m.groups()):
        days = int(m.group(1) or 0)
        hours = int(m.group(2) or 0)
        mins = int(m.group(3) or 0)
        total = (days * 86400) + (hours * 3600) + (mins * 60)
        if total > 0:
            parts = []
            if days:
                parts.append(f"{days}d")
            if hours:
                parts.append(f"{hours}h")
            if mins:
                parts.append(f"{mins}m")
            return " ".join(parts) or "0m", total

    m = _TIME_PATTERNS[1].search(full_text)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        total = h * 3600 + mi * 60 + s
        if total > 0:
            return f"{h}h {mi}m" if h else f"{mi}m {s}s", total

    for pat in _TIME_PATTERNS[2:]:
        m = pat.search(full_text)
        if m:
            h = int(m.group(1))
            if h > 0:
                return f"{h}h", h * 3600

    return None, None


def _generic_nations(soup: BeautifulSoup) -> list[NationStatus]:
    nations: list[NationStatus] = []
    tables = soup.find_all("table")
    if not tables:
        return nations

    best_table = max(tables, key=lambda t: len(t.find_all("tr")), default=None)
    if best_table is None:
        return nations

    for row in best_table.find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue

        nation_name = _generic_nation_name(cells)
        if not nation_name:
            continue

        player_type = _generic_player_type(cells)
        is_human = player_type is None or bool(_HUMAN_TYPES.search(player_type))
        if player_type and _NON_HUMAN_TYPES.search(player_type):
            is_human = False

        submitted = _generic_submitted(cells, row)
        nations.append(NationStatus(name=nation_name, submitted=submitted, is_human=is_human))

    return nations


def _generic_nation_name(cells: list[Tag]) -> Optional[str]:
    for cell in cells[:3]:
        text = cell.get_text(strip=True)
        if not text or len(text) <= 1 or text.isdigit():
            continue
        if _SUBMITTED_POSITIVE.match(text) or _SUBMITTED_NEGATIVE.match(text):
            continue
        if re.match(r"^(human|ai|closed|defeated|empty|player|turn|era|epithet)$", text, re.IGNORECASE):
            continue
        return text
    return None


def _generic_player_type(cells: list[Tag]) -> Optional[str]:
    for cell in cells:
        text = cell.get_text(strip=True)
        if _HUMAN_TYPES.search(text) or _NON_HUMAN_TYPES.search(text):
            return text
    return None


def _generic_submitted(cells: list[Tag], row: Tag) -> bool:
    for cell in reversed(cells[-3:]):
        text = cell.get_text(strip=True)
        bg = _cell_bgcolor(cell)
        if _SUBMITTED_POSITIVE.search(text):
            return True
        if _SUBMITTED_NEGATIVE.search(text):
            return False
        if bg:
            if _GREEN_RE.search(bg):
                return True
            if _RED_RE.search(bg):
                return False
    bg = _cell_bgcolor(row)
    if bg:
        if _GREEN_RE.search(bg):
            return True
        if _RED_RE.search(bg):
            return False
    return False


def _cell_bgcolor(tag: Tag) -> Optional[str]:
    bg = tag.get("bgcolor") or ""
    if bg:
        return str(bg)
    style = tag.get("style", "")
    if style:
        m = re.search(r"background(?:-color)?\s*:\s*([^;]+)", str(style), re.IGNORECASE)
        if m:
            return m.group(1).strip()
    cls = " ".join(tag.get("class", []))
    return cls or None
