import re
import unicodedata

ROOT = "media-batch"          # cartella radice su Drive
SUB = ("pending", "processing", "done", "failed", "out_video", "out_vocals")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\s*[\(\[](feat|ft|with|prod)[^\)\]]*[\)\]]", "", s)
    s = re.sub(r"\b(feat|ft)\.?\s.*$", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def song_key(artist: str, title: str) -> str:
    first = re.split(r",|&| x | e | and ", artist or "", maxsplit=1)[0]
    return f"{norm(first)}|{norm(title)}"


def words(text: str) -> list[str]:
    return norm(text).split()


def parse_lrc(lrc: str) -> list[dict]:
    pat = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
    out = []
    for raw in (lrc or "").splitlines():
        stamps = pat.findall(raw)
        text = pat.sub("", raw).strip()
        if not stamps or not text:
            continue
        for m, s in stamps:
            out.append({"time": int(m) * 60 + float(s), "text": text})
    return sorted(out, key=lambda x: x["time"])
