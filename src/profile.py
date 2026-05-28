"""
Helpers for parsing MikroTik hotspot profile names.

Profile names follow the convention:
    N_{display_name}|D_{duration}|P_{price_in_idr}

Example:
    N_Paket-Hemat|D_30-hari|P_30000
      → name="Paket-Hemat", duration="30-hari", price="30000"

Any profile name that does not match this format (including names that
start with "$", which MikroTik uses for system-reserved profiles) is
silently ignored by the callers.
"""

from typing import TypedDict


class ParsedProfile(TypedDict):
    name: str
    duration: str
    price: str


def parse_profile_name(name: str) -> ParsedProfile | None:
    """
    Parse a profile name into its components.

    Args:
        name: The raw profile name string from MikroTik.

    Returns:
        A ``ParsedProfile`` dict with keys ``name``, ``duration``, and
        ``price``, or ``None`` if the name does not match the expected
        format or any required segment is missing.
    """
    if not name:
        return None

    parts = name.split("|")
    result: dict[str, str] = {}

    for part in parts:
        if part.startswith("N_"):
            result["name"] = part[2:]
        elif part.startswith("D_"):
            result["duration"] = part[2:]
        elif part.startswith("P_"):
            result["price"] = part[2:]

    # All three segments are required
    if not all(k in result for k in ("name", "duration", "price")):
        return None

    # Price must be a valid non-negative integer
    if not result["price"].isdigit():
        return None

    return ParsedProfile(
        name=result["name"],
        duration=result["duration"],
        price=result["price"],
    )
