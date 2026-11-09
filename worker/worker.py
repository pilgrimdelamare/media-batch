"""
worker.py — gira su GitHub Actions.
Prende i job dalla coda Drive -> separa voce/base -> controlla il testo
-> video -> YouTube -> archivio Drive. Un job fallito finisce in failed/ col motivo.
"""
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from core.common import ROOT, SUB, parse_lrc, words  # noqa: E402
from core.drive import Drive  # noqa: E402
import validate  # noqa: E402
import youtube  # noqa: E402
from render import KaraokeRenderer  # noqa: E402

log = logging.getLogger("worker")
WORK = Path(os.environ.get("WORK_DIR", "/tmp/mb"))
ASSETS = HERE.parent / "assets"
MAX_UPLOADS = int(os.environ.get("MAX_UPLOADS", "2"))
BUDGET = float(os.environ.get("TIME_BUDGET_MIN", "300")) * 60
STALE_H = 6
MAX_ATTEMPTS = 3
DRY = os.environ.get("DRY_RUN") == "1"
T0 = time.time()


def sh(cmd, timeout=3600):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} rc={r.returncode}: {r.stderr[-800:]}")
    return r


def lyrics_hash(lines):
    return hashlib.sha1(" ".join(w for l in lines for w in words(l["text"])).encode()).hexdigest()[:16]


class Queue:
    def __init__(self):
        self.d = Drive.from_env()
        self.root = self.d.path(ROOT)
        self.f = {s: self.d.folder(s, self.root) for s in SUB}
        st = self.d.list(self.root, name="state.json")
        self.state_id = st[0]["id"] if st else None
        self.state = self.d.get_json(self.state_id) if st else {"hashes": {}, "uploaded": {}}

    def save_state(self):
        self.state_id = self.d.put_json(self.state, self.root, "state.json", self.state_id)

    def files(self, folder):
        return self.d.list(self.f[folder])

    def recover_stale(self):
        now = time.time()
        for f in self.files("processing"):
            if not f["name"].endswith(".json"):
                continue
            job = self.d.get_json(f["id"])
            if now - job.get("claimed_at", 0) < STALE_H * 3600:
                continue
            job["attempts"] = job.get("attempts", 0) + 1
            dest = "pending" if job["attempts"] < MAX_ATTEMPTS else "failed"
            job.setdefault("reason", "interrotto")
            self.d.put_json(job, None, None, f["id"])
            self._move_job(job, f["id"], "processing", dest)
            log.info("job bloccato %s -> %s", job["id"], dest)

    def _audio_file(self, job, folder):
        hits = [x for x in self.files(folder) if x["name"] == job["audio_name"]]
        return hits[0] if hits else None

    def _move_job(self, job, json_id, src, dest):
        a = self._audio_file(job, src)
        if a:
            if dest in ("done", "failed"):
                self.d.delete(a["id"])            # l'audio sorgente non serve più
            else:
                self.d.move(a["id"], self.f[dest], self.f[src])
        self.d.move(json_id, self.f[dest], self.f[src])

    def claim(self):
        for f in self.files("pending"):            # ordinati per data di creazione
            if not f["name"].endswith(".json"):
                continue
            job = self.d.get_json(f["id"])
            if not self._audio_file(job, "pending"):
                continue                            # upload ancora in corso
            job["claimed_at"] = time.time()
            self.d.put_json(job, None, None, f["id"])
            self._move_job(job, f["id"], "pending", "processing")
            return job, f["id"]
        return None, None

    def finish(self, job, json_id, dest, **extra):
        job.update(extra, finished_at=time.time())
        self.d.put_json(job, None, None, json_id)
        self._move_job(job, json_id, "processing", dest)


def separate(src, out):
    wav = out / "in.wav"
    sh(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-ac", "2", "-ar", "44100", str(wav)])
    sh([sys.executable, "-m", "demucs", "-n", "htdemucs", "--two-stems", "vocals",
        "-d", "cpu", "-j", "2", "-o", str(out / "sep"), "--filename", "{stem}.{ext}", str(wav)])
    voc = next((out / "sep").rglob("vocals.wav"))
    inst = next((out / "sep").rglob("no_vocals.wav"))
    return str(voc), str(inst)


def process(q: Queue, yt, job):
    w = WORK / job["id"]
    shutil.rmtree(w, ignore_errors=True)
    w.mkdir(parents=True)
    try:
        artist, title = job["artist"], job["title"]
        if job["key"] in q.state["uploaded"]:
            return "failed", {"reason": "già caricato"}

        lines = parse_lrc(job["lrc"])
        h = lyrics_hash(lines)
        other = q.state["hashes"].get(h)
        if other and other != job["key"]:
            return "failed", {"reason": f"testo identico a quello di {other}"}

        a = q._audio_file(job, "processing")
        src = q.d.download(a["id"], str(w / job["audio_name"]))

        log.info("  separazione voce/base…")
        voc, inst = separate(src, w)

        log.info("  controllo testo (Whisper)…")
        heard, lang = validate.transcribe(voc)
        chk = validate.check(lines, heard)
        chk["lang"] = lang
        log.info("  esito: %s", json.dumps(chk, ensure_ascii=False))
        if not chk["ok"]:
            return "failed", {"reason": chk["reason"], "check": chk}
        lines = validate.apply_offset(lines, chk["offset"])

        log.info("  rendering…")
        r = KaraokeRenderer(output_dir=str(w / "video"), background_image=str(ASSETS / "background.jpg"),
                            font_size=120, logo_path=str(ASSETS / "logo.png"))
        mp4 = r.render(title=title, artist=artist, audio_path=inst,
                       lyrics_data={"type": "synced", "content": lines}, song_id=job["id"])
        if not mp4:
            return "failed", {"reason": "rendering fallito", "check": chk}
        thumb = str(w / "thumb.jpg")
        sh(["ffmpeg", "-v", "error", "-y", "-ss", "0.1", "-i", mp4, "-vf",
            "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
            "-frames:v", "1", "-q:v", "2", thumb])
        voc_mp3 = str(Path(mp4).with_suffix(".mp3"))
        sh(["ffmpeg", "-v", "error", "-y", "-i", voc, "-b:a", "192k", voc_mp3])

        if DRY:
            log.info("  DRY_RUN: niente upload")
            vid, yt_title = None, None
        else:
            log.info("  upload YouTube…")
            vid, yt_title = youtube.upload(yt, mp4, title, artist, thumb)
            q.state["uploaded"][job["key"]] = vid
            q.state["hashes"][h] = job["key"]
            q.save_state()
            log.info("  https://youtu.be/%s", vid)

        q.d.upload(mp4, q.f["out_video"])
        q.d.upload(voc_mp3, q.f["out_vocals"])
        return "done", {"youtube_id": vid, "youtube_title": yt_title, "check": chk}
    finally:
        shutil.rmtree(w, ignore_errors=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    q = Queue()
    q.recover_stale()
    yt = None if DRY else youtube.client()
    uploaded = 0
    while uploaded < MAX_UPLOADS and time.time() - T0 < BUDGET - 40 * 60:
        job, jid = q.claim()
        if not job:
            log.info("coda vuota")
            break
        log.info("▶ %s - %s (#%s %s)", job["artist"], job["title"], job.get("chart_pos"), job.get("country"))
        try:
            dest, extra = process(q, yt, job)
        except youtube.QuotaExceeded as e:
            log.warning("quota YouTube esaurita, rimetto in coda: %s", e)
            job["claimed_at"] = 0
            q.d.put_json(job, None, None, jid)
            q._move_job(job, jid, "processing", "pending")
            break
        except Exception as e:
            log.exception("errore")
            job["attempts"] = job.get("attempts", 0) + 1
            dest = "pending" if job["attempts"] < MAX_ATTEMPTS else "failed"
            extra = {"reason": str(e)[:500]}
            if dest == "pending":
                job.update(extra, claimed_at=0)
                q.d.put_json(job, None, None, jid)
                q._move_job(job, jid, "processing", "pending")
                continue
        q.finish(job, jid, dest, **extra)
        log.info("  -> %s %s", dest, extra.get("reason", ""))
        if dest == "done":
            uploaded += 1
    log.info("fine: %d caricati", uploaded)


if __name__ == "__main__":
    main()
