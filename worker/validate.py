"""
Controllo testo prima dell'upload, usando la traccia voce separata da Demucs.

1. Whisper trascrive la voce con i tempi di ogni parola.
2. CONTENUTO: le parole sentite devono comparire nel testo LRC
   (un testo sbagliato condivide pochissime parole con quello cantato).
3. SINCRONIA: per ogni riga LRC si misura lo scarto tra l'orario previsto
   delle sue parole e quando Whisper le sente davvero. Lo scarto mediano
   viene corretto; se le righe sono incoerenti tra loro il brano è scartato.
"""
import logging
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.common import words  # noqa: E402

log = logging.getLogger("validate")

MIN_HEARD = 25          # parole minime sentite da Whisper
MIN_PRECISION = 0.45    # quota di parole sentite presenti nel testo
MIN_RECALL = 0.30       # quota del vocabolario del testo effettivamente sentita
MIN_LINES_MATCHED = 0.40
MIN_LINES_COHERENT = 0.60
COHERENT_TOL = 1.5      # secondi
MAX_SHIFT = 6.0         # oltre questo scarto non correggiamo: testo di un'altra versione
WINDOW = 10.0

_model = None


def transcribe(vocals_path: str):
    global _model
    from faster_whisper import WhisperModel
    import subprocess
    import numpy as np
    if _model is None:
        _model = WhisperModel("small", device="cpu", compute_type="int8")
    pcm = subprocess.run(["ffmpeg", "-v", "quiet", "-i", vocals_path, "-ac", "1", "-ar", "16000",
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    audio = np.frombuffer(pcm, dtype=np.float32)
    segs, info = _model.transcribe(audio, word_timestamps=True, vad_filter=True,
                                   beam_size=1, condition_on_previous_text=False)
    out = []
    for s in segs:
        for w in s.words or []:
            for tok in words(w.word):
                out.append((tok, float(w.start)))
    return out, info.language


def check(lines: list[dict], heard: list[tuple[str, float]]) -> dict:
    """lines: [{'time','text'}] dal LRC. Ritorna dict con ok, motivo, offset."""
    lyric_vocab = {w for l in lines for w in words(l["text"]) if len(w) >= 3}
    heard_c = [(w, t) for w, t in heard if len(w) >= 3]
    res = {"ok": False, "heard": len(heard_c), "offset": 0.0}

    if len(heard_c) < MIN_HEARD:
        res["reason"] = f"voce quasi assente ({len(heard_c)} parole sentite)"
        return res

    precision = sum(w in lyric_vocab for w, _ in heard_c) / len(heard_c)
    heard_vocab = {w for w, _ in heard_c}
    recall = len(lyric_vocab & heard_vocab) / max(1, len(lyric_vocab))
    res.update(precision=round(precision, 2), recall=round(recall, 2))
    if precision < MIN_PRECISION or recall < MIN_RECALL:
        res["reason"] = f"testo non corrisponde (precisione {precision:.2f}, copertura {recall:.2f})"
        return res

    # posizioni per parola sentita
    pos = {}
    for w, t in heard_c:
        pos.setdefault(w, []).append(t)

    deltas = []
    for i, l in enumerate(lines):
        ws = [w for w in words(l["text"])]
        if not ws:
            continue
        nxt = lines[i + 1]["time"] if i + 1 < len(lines) else l["time"] + 4
        dur = max(0.8, min(nxt - l["time"], 6.0))
        d_line = []
        for k, w in enumerate(ws):
            if len(w) < 3 or w not in pos:
                continue
            est = l["time"] + dur * k / len(ws)
            near = min(pos[w], key=lambda t: abs(t - est))
            if abs(near - est) <= WINDOW:
                d_line.append(near - est)
        if d_line:
            deltas.append(statistics.median(d_line))

    matched = len(deltas) / max(1, len(lines))
    res["lines_matched"] = round(matched, 2)
    if matched < MIN_LINES_MATCHED:
        res["reason"] = f"poche righe ritrovate nella voce ({matched:.0%})"
        return res

    off = statistics.median(deltas)
    coherent = sum(abs(d - off) <= COHERENT_TOL for d in deltas) / len(deltas)
    res.update(offset=round(off, 2), coherent=round(coherent, 2))
    if abs(off) > MAX_SHIFT:
        res["reason"] = f"testo sfasato di {off:+.1f}s (versione diversa?)"
        return res
    if coherent < MIN_LINES_COHERENT:
        res["reason"] = f"sincronia incoerente ({coherent:.0%} righe a tempo)"
        return res
    res["ok"] = True
    return res


def apply_offset(lines, off):
    if abs(off) < 0.25:
        return lines
    return [{**l, "time": max(0.0, l["time"] + off)} for l in lines]


if __name__ == "__main__":
    # uso manuale: python validate.py vocals.wav testo.lrc
    import json
    from core.common import parse_lrc
    logging.basicConfig(level=logging.INFO)
    heard, lang = transcribe(sys.argv[1])
    r = check(parse_lrc(Path(sys.argv[2]).read_text(encoding="utf-8")), heard)
    print(lang, json.dumps(r, ensure_ascii=False))
