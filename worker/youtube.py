"""Upload YouTube (stesso titolo/descrizione/tag della pipeline storica)."""
import json
import logging
import os
import re

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

log = logging.getLogger("youtube")

DESCRIPTION = """{title} - {artist}

🎤 Karaoke Version | Versione Karaoke

🎵 Testo sincronizzato per cantare insieme!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ Disclaimer: Questo video è a scopo karaoke/educativo.
I diritti della canzone appartengono ai rispettivi proprietari.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

#karaoke #{artist_tag} #{title_tag} #karaokesongs
"""


class QuotaExceeded(Exception):
    pass


def client():
    d = json.loads(os.environ["YT_AUTH"])
    creds = Credentials(None, refresh_token=d["refresh_token"], client_id=d["client_id"],
                        client_secret=d["client_secret"], token_uri="https://oauth2.googleapis.com/token")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload(yt, video_path, title, artist, thumb=None):
    from googleapiclient.errors import HttpError
    yt_title = f"{artist} - {title} (Karaoke Version)"
    if len(yt_title) > 100:
        yt_title = yt_title[:97] + "..."
    artist_tag = re.sub(r"[^a-zA-Z0-9]", "", artist.split(",")[0]).lower()
    title_tag = re.sub(r"[^a-zA-Z0-9]", "", title).lower()
    body = {
        "snippet": {
            "title": yt_title,
            "description": DESCRIPTION.format(title=title, artist=artist,
                                              artist_tag=artist_tag, title_tag=title_tag),
            "tags": ["karaoke", title, artist, f"{artist} karaoke", f"{title} karaoke",
                     "karaoke italiano", "sing along", "lyrics", "testo"],
            "categoryId": "10",
            "defaultLanguage": "it",
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    try:
        req = yt.videos().insert(part="snippet,status", body=body,
                                 media_body=MediaFileUpload(video_path, mimetype="video/mp4",
                                                            resumable=True, chunksize=16 << 20))
        resp = None
        while resp is None:
            _, resp = req.next_chunk()
    except HttpError as e:
        if "quotaExceeded" in str(e) or "uploadLimitExceeded" in str(e):
            raise QuotaExceeded(str(e)[:200])
        raise
    vid = resp["id"]
    if thumb:
        try:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(thumb)).execute()
        except Exception as e:
            log.warning("thumbnail: %s", e)
    return vid, yt_title
