"""
fetch.py — gira sul server di casa (IP residenziale).
Classifica -> brani nuovi -> testo LRC sincronizzato + audio -> coda su Drive.
Ogni file locale viene cancellato appena caricato.

  python fetch.py    # un giro (il loop lo fa lo script runit)
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import zlib
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from core.common import ROOT, SUB, song_key, norm, parse_lrc  # noqa: E402
from core.drive import Drive  # noqa: E402

HOME = Path(os.environ.get("MB_HOME", Path.home() / "kp"))
TMP = HOME / "tmp"
SEEN = HOME / "seen.json"
COUNTRIES = os.environ.get("MB_COUNTRIES", "it").split(",")
CHART_N = int(os.environ.get("MB_CHART_N", "100"))
MAX_NEW = int(os.environ.get("MB_MAX_NEW", "12"))       # brani nuovi per giro
MAX_PENDING = int(os.environ.get("MB_MAX_PENDING", "25"))
MAX_TRIES = 5                                            # giri senza LRC prima di arrendersi

log = logging.getLogger("fetch")
UA = {"User-Agent": "Mozilla/5.0"}


# ── stato locale ───────────────────────────────────────────────────────
def load_seen():
    try:
        return json.loads(SEEN.read_text())
    except Exception:
        return {}


def save_seen(seen):
    SEEN.write_text(json.dumps(seen, ensure_ascii=False))


# ── classifica ─────────────────────────────────────────────────────────
def chart(cc):
    url = f"https://rss.applemarketingtools.com/api/v2/{cc}/music/most-played/{CHART_N}/songs.json"
    res = requests.get(url, headers=UA, timeout=30).json()["feed"]["results"]
    return [{"artist": r["artistName"], "title": r["name"], "pos": i + 1, "cc": cc}
            for i, r in enumerate(res)]


# ── YouTube ────────────────────────────────────────────────────────────
def _score(c, title, artist):
    t = (c.get("title") or "").lower()
    ch = (c.get("channel") or c.get("uploader") or "").lower()
    d = c.get("duration") or 0
    if d < 60 or d > 480:
        return None
    a = artist.split(",")[0].strip().lower()
    tl = title.lower()
    s = 0
    s += 30 if "audio" in t else 0
    s += 25 if ("topic" in ch or "provided to youtube" in ch) else 0
    s += 15 if a in ch else 0
    s += 10 if norm(title) in norm(t) else 0
    s += 5 if a in t else 0
    s -= 20 if "official music video" in t else 0
    s -= 15 if "official video" in t else 0
    s -= 25 if "live" in t and "live" not in tl else 0
    s -= 20 if "remix" in t and "remix" not in tl else 0
    s -= 30 if "cover" in t else 0
    s -= 50 if "karaoke" in t or "instrumental" in t or "base" in t.split() else 0
    s -= 40 if "sped up" in t or "slowed" in t or "nightcore" in t else 0
    return s


def find_video(title, artist):
    best = None
    for q in (f"{artist} - {title} (Official Audio)", f"{artist} {title} topic", f"{artist} {title}"):
        r = subprocess.run(["yt-dlp", f"ytsearch6:{q}", "--dump-json", "--flat-playlist",
                            "--no-warnings"], capture_output=True, text=True, timeout=90)
        for line in r.stdout.splitlines():
            try:
                c = json.loads(line)
            except ValueError:
                continue
            s = _score(c, title, artist)
            if s is not None and (best is None or s > best[0]):
                best = (s, c)
        if best and best[0] >= 40:
            break
    return best[1] if best else None


def download(vid, dest_stem):
    r = subprocess.run(["yt-dlp", "-f", "bestaudio", "--no-playlist", "--no-warnings",
                        "--max-filesize", "60M", "-o", f"{dest_stem}.%(ext)s",
                        f"https://www.youtube.com/watch?v={vid}"],
                       capture_output=True, text=True, timeout=600)
    files = list(dest_stem.parent.glob(dest_stem.name + ".*"))
    if r.returncode != 0 or not files:
        log.warning("download fallito: %s", r.stderr[-300:])
        return None
    return files[0]


# ── testi ──────────────────────────────────────────────────────────────
def _lrclib_ok(item, title, artist, dur):
    if not item.get("syncedLyrics"):
        return False
    if dur and item.get("duration") and abs(item["duration"] - dur) > 4:
        return False
    return norm(title)[:12] in norm(item.get("trackName", "")) or \
        norm(item.get("trackName", ""))[:12] in norm(title)


def lyrics(title, artist, dur):
    a = artist.split(",")[0].strip()
    # 1) match esatto per durata
    try:
        p = urllib.parse.urlencode({"track_name": title, "artist_name": a, "duration": int(dur or 0)})
        r = requests.get(f"https://lrclib.net/api/get?{p}", headers=UA, timeout=20)
        if r.ok and _lrclib_ok(r.json(), title, artist, dur):
            return r.json()["syncedLyrics"], "lrclib-get"
    except Exception as e:
        log.debug("lrclib get: %s", e)
    # 2) ricerca, con controllo su titolo e durata
    try:
        p = urllib.parse.urlencode({"track_name": title, "artist_name": a})
        for it in requests.get(f"https://lrclib.net/api/search?{p}", headers=UA, timeout=20).json():
            if _lrclib_ok(it, title, artist, dur):
                return it["syncedLyrics"], "lrclib-search"
    except Exception as e:
        log.debug("lrclib search: %s", e)
    # 3) syncedlyrics (musixmatch/netease) — il controllo Whisper nel worker fa da garanzia
    try:
        import syncedlyrics
        lrc = syncedlyrics.search(f"{a} {title}", synced_only=True,
                                  providers=["Musixmatch", "NetEase", "Lrclib"])
        if lrc and len(parse_lrc(lrc)) >= 8:
            return lrc, "syncedlyrics"
    except Exception as e:
        log.debug("syncedlyrics: %s", e)
    return None, None


# ── giro ───────────────────────────────────────────────────────────────
def run_once():
    TMP.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(TMP, ignore_errors=True)       # pulizia di eventuali avanzi
    TMP.mkdir(parents=True, exist_ok=True)

    drive = Drive.from_env()
    root = drive.path(ROOT)
    pend = drive.folder("pending", root)
    for s in SUB:
        drive.folder(s, root)
    queued = len([f for f in drive.list(pend) if f["name"].endswith(".json")])
    room = max(0, min(MAX_NEW, MAX_PENDING - queued))
    log.info("in coda: %d, posti liberi: %d", queued, room)
    if not room:
        return

    seen = load_seen()
    songs = []
    for cc in COUNTRIES:
        try:
            songs += chart(cc.strip())
        except Exception as e:
            log.warning("classifica %s: %s", cc, e)
    songs.sort(key=lambda s: s["pos"])

    added = 0
    for s in songs:
        if added >= room:
            break
        key = song_key(s["artist"], s["title"])
        st = seen.get(key, {})
        if st.get("state") in ("queued", "skip") or st.get("tries", 0) >= MAX_TRIES:
            continue
        log.info("#%d %s - %s", s["pos"], s["artist"], s["title"])
        try:
            c = find_video(s["title"], s["artist"])
            if not c:
                raise RuntimeError("nessun video adatto")
            lrc, src = lyrics(s["title"], s["artist"], c.get("duration"))
            if not lrc:
                seen[key] = {"state": "nolyrics", "tries": st.get("tries", 0) + 1, "t": time.time()}
                log.info("   niente LRC (tentativo %d)", seen[key]["tries"])
                continue
            jid = f"{int(time.time())}_{zlib.crc32(key.encode()) % 100000:05d}"
            audio = download(c["id"], TMP / jid)
            if not audio:
                raise RuntimeError("download fallito")
            job = {"id": jid, "artist": s["artist"], "title": s["title"], "key": key,
                   "chart_pos": s["pos"], "country": s["cc"], "yt_source": c["id"],
                   "duration": c.get("duration"), "lrc": lrc, "lrc_source": src,
                   "audio_name": audio.name, "queued_at": time.time()}
            drive.upload(str(audio), pend)
            jp = TMP / f"{jid}.json"
            jp.write_text(json.dumps(job, ensure_ascii=False))
            drive.upload(str(jp), pend)            # il .json per ultimo = job completo
            seen[key] = {"state": "queued", "id": jid, "t": time.time()}
            added += 1
            log.info("   in coda (%s, %s)", src, audio.suffix)
        except Exception as e:
            seen[key] = {"state": "error", "tries": st.get("tries", 0) + 1, "err": str(e)[:200]}
            log.warning("   errore: %s", e)
        finally:
            for f in TMP.glob("*"):
                f.unlink(missing_ok=True)
            save_seen(seen)
    log.info("giro finito: %d brani nuovi in coda", added)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%d/%m %H:%M")
    run_once()


if __name__ == "__main__":
    main()
