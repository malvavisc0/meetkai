import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

logger = logging.getLogger(__name__)


def get_current_datetime(timezone_name: str | None = None) -> dict[str, str]:
    now_utc = datetime.now(UTC)
    local = now_utc.astimezone()
    result: dict[str, str] = {
        "utc": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "local": local.strftime("%Y-%m-%d %H:%M:%S %Z"),
    }
    if timezone_name:
        try:
            tz = ZoneInfo(timezone_name)
            target = now_utc.astimezone(tz)
            result[timezone_name] = target.strftime("%Y-%m-%d %H:%M:%S %Z")
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("unknown timezone requested: %r", timezone_name)
            result["error"] = f"Unknown timezone: {timezone_name}"
    return result


def get_weather(location: str, timeout: float = 15.0) -> str | None:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(
                f"https://wttr.in/{location.strip()}",
                params={"format": "j1"},
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, httpx.RequestError, ValueError) as exc:
        logger.warning("failed to fetch weather for %r: %s", location, exc)
        return None

    try:
        cur = data["current_condition"][0]
        area = data.get("nearest_area", [{}])[0]
        place = area.get("areaName", [{}])[0].get("value", location)
        desc = cur.get("weatherDesc", [{}])[0].get("value", "")
        return (
            f"{place}: {desc}, {cur.get('temp_C')}°C "
            f"(feels like {cur.get('FeelsLikeC')}°C), "
            f"humidity {cur.get('humidity')}%, wind {cur.get('windspeedKmph')} km/h."
        )
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning("unexpected weather payload for %r: %s", location, exc)
        return None
