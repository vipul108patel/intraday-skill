"""
Option-chain context (OI / PCR / Max Pain / VIX) — soft gates.

Angel SmartAPI does not always expose full option-chain OI in this repo.
When data is missing, return neutral context so core price-action signals still trade.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def option_flow_context(
    spot: float,
    side: str,
    cfg: dict,
    client: Any = None,
    as_of=None,
) -> dict[str, Any]:
    """
    Returns supportive / neutral / against for CE or PE setup.
    Never hard-blocks unless cfg.require_option_flow is true AND data exists and conflicts.
    """
    require = bool(cfg.get("require_option_flow", False))
    out: dict[str, Any] = {
        "available": False,
        "supportive": True,  # neutral-pass when no data
        "pcr": None,
        "max_pain": None,
        "india_vix": None,
        "call_oi_bias": None,
        "put_oi_bias": None,
        "why": "Option flow data unavailable — soft skip (price-action primary).",
    }

    if client is None:
        try:
            from src.angelone.client import get_client

            client = get_client()
        except Exception:
            client = None

    # Soft VIX check if client can fetch India VIX LTP
    vix = None
    if client is not None and cfg.get("use_india_vix", True):
        try:
            # Common Angel symbol; may fail — then ignore
            vix = float(client.get_ltp("INDIA VIX", exchange="NSE"))
            out["india_vix"] = vix
            out["available"] = True
            max_vix = float(cfg.get("max_india_vix", 25.0))
            if vix > max_vix:
                out["supportive"] = False
                out["why"] = f"India VIX {vix:.1f} > max {max_vix} — elevated vol, skip."
                if require:
                    return out
                # soft: still allow but warn
                out["supportive"] = True
                out["why"] = f"India VIX {vix:.1f} elevated (soft warn); core signals still OK."
            else:
                out["why"] = f"India VIX {vix:.1f} OK; OI/PCR not wired — soft pass."
        except Exception as e:
            logger.debug("VIX fetch skipped: %s", e)

    # Placeholder for future ATM±5 OI / PCR from option chain API
    # When implemented: set call_oi_bias / put_oi_bias / pcr and supportive flag.
    out["note"] = (
        "OI Change / PCR / Max Pain / IV: not hard-wired yet. "
        "Interpret only with price; never standalone."
    )
    return out
