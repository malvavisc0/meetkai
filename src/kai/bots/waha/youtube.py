"""YouTube video enrichment for the WAHA bot.

When an inbound message contains a YouTube URL, fetch the transcript
so the agent can read it in the same turn. Input pre-processing only.

Caption tracks are fetched via YouTube's InnerTube ``player`` endpoint
impersonating the ANDROID client. The WEB-client ``baseUrl`` (scraped
from the watch page) is gated behind YouTube's PoToken enforcement
(``&exp=xpe``) and returns an empty body, so it is intentionally not
used. The ANDROID-client ``baseUrl`` is ungated and returns the real
transcript XML. Only ``httpx`` is required; no API key is needed (the
player endpoint responds without one).
"""

import html as html_module
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_YT_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|embed/|v/|shorts/)|youtu\.be/)"
    r"(?P<video_id>[A-Za-z0-9_-]{11})"
)

# InnerTube player endpoint. No API key is required — the endpoint responds
# without ?key=.
_INNERTUBE_PLAYER_URL = "https://www.youtube.com/youtubei/v1/player"

# InnerTube ANDROID client. This client returns caption track URLs that are
# NOT gated behind the &exp=xpe PoToken flag, so the timedtext endpoint
# returns the real transcript XML (the WEB client's URLs are gated and
# return an empty body).
_INNERTUBE_CONTEXT = {"client": {"clientName": "ANDROID", "clientVersion": "20.10.38"}}

_INNERTUBE_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "com.google.android.youtube/20.10.38 (Linux; U; Android 11)",
}

_MAX_TRANSCRIPT_LINES = 600
_TIMEOUT = 20.0


def extract_youtube_video_id(text: str) -> str | None:
    """Return the 11-char video ID if text contains a YouTube URL."""
    m = _YT_RE.search(text)
    return m.group("video_id") if m else None


def fetch_youtube_transcript(video_id: str, lang: str | None = None) -> dict[str, Any]:
    """Fetch transcript for a YouTube video.

    Returns dict with:
        - video_id
        - transcript_text (clean plain text)
        - transcript (list of {start, duration, text})
        - language
        - url
        - error (if any)

    ``lang`` selects a preferred caption language when several tracks exist.
    """
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            tracks = _fetch_caption_tracks(client, video_id)
            if not tracks:
                return {
                    "video_id": video_id,
                    "error": "No captions or transcript available for this video.",
                }

            track = _select_caption_track(tracks, lang)
            if track is None:
                return {"video_id": video_id, "error": "No suitable caption track found."}

            base_url = track.get("baseUrl")
            if not base_url:
                return {"video_id": video_id, "error": "Caption track has no baseUrl."}

            # fmt=srv3 yields JSON-less XML <text start dur>...</text>; drop it
            # so the default XML format is used. The &exp=xpe flag marks the
            # PoToken-gated branch (WEB client) which returns an empty body —
            # the ANDROID client URLs we use here should never carry it, but
            # guard regardless.
            base_url = base_url.replace("&fmt=srv3", "")
            if "&exp=xpe" in base_url:
                return {
                    "video_id": video_id,
                    "error": "Caption URL is PoToken-gated; cannot fetch without a browser token.",
                }

            xml_content = _fetch_transcript_xml(client, base_url)
            if xml_content is None:
                return {"video_id": video_id, "error": "Failed to fetch transcript XML."}
    except httpx.HTTPError as exc:
        logger.warning("YouTube HTTP error for %s: %s", video_id, exc)
        return {"video_id": video_id, "error": f"HTTP error: {exc}"}
    except Exception as exc:
        logger.warning("YouTube transcript fetch failed for %s: %s", video_id, exc)
        return {"video_id": video_id, "error": str(exc)}

    transcript = _parse_transcript_xml(xml_content)
    if not transcript:
        return {"video_id": video_id, "error": "Transcript was empty or could not be parsed."}

    plain_text = " ".join(item["text"] for item in transcript)
    return {
        "video_id": video_id,
        "language": track.get("languageCode"),
        "transcript": transcript[:_MAX_TRANSCRIPT_LINES],
        "transcript_text": plain_text,
        "url": f"https://www.youtube.com/watch?v={video_id}",
    }


def _fetch_caption_tracks(client: httpx.Client, video_id: str) -> list[dict]:
    """POST to the InnerTube player endpoint (ANDROID client) for caption tracks."""
    resp = client.post(
        _INNERTUBE_PLAYER_URL,
        json={"context": _INNERTUBE_CONTEXT, "videoId": video_id},
        headers=_INNERTUBE_HEADERS,
    )
    resp.raise_for_status()
    data = resp.json()

    playability = (data.get("playabilityStatus") or {}).get("status")
    if playability not in ("OK", None):
        logger.warning("YouTube playability for %s: %s", video_id, playability)
        return []

    return (
        data.get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
    )


def _select_caption_track(tracks: list[dict], preferred_lang: str | None) -> dict | None:
    """Return preferred language track, or first available."""
    if not tracks:
        return None
    if preferred_lang:
        for track in tracks:
            if track.get("languageCode", "").lower().startswith(preferred_lang.lower()):
                return track
    return tracks[0]


def _fetch_transcript_xml(client: httpx.Client, base_url: str) -> str | None:
    """GET the timedtext XML. Returns None on failure."""
    resp = client.get(base_url)
    resp.raise_for_status()
    return resp.text


def _parse_transcript_xml(xml_content: str) -> list[dict]:
    """Parse YouTube timedtext XML."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return []

    transcript: list[dict] = []
    for node in root.findall(".//text"):
        text = html_module.unescape(node.text or "").strip()
        text = text.replace("\xa0", " ")
        if text:
            transcript.append(
                {
                    "start": float(node.get("start", 0.0)),
                    "duration": float(node.get("dur", 0.0)),
                    "text": text,
                }
            )
    return transcript
