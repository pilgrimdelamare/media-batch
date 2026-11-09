"""
modules/video.py - Crea video karaoke con testo sincronizzato

Comportamento:
  - La riga di testo finisce poco dopo la sua battuta (max MAX_LINE_DUR sec)
  - Nei gap strumentali > GAP_THRESHOLD sec: appare logo.png centrato
    con fade-in e fade-out veloci tramite geq sul canale alpha
  - Il logo NON appare durante il titolo intro (primi TITLE_DURATION sec)
  - Il logo è grande (LOGO_H_RATIO = 35% altezza video)
  - Nessun watermark di default
"""
import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

TITLE_DURATION = 4.5    # durata schermata titolo iniziale (secondi)
FADE_DUR       = 0.5    # fade in / fade out del logo (secondi)
GAP_THRESHOLD  = 1.5    # gap minimo per mostrare il logo (secondi)
MAX_LINE_DUR   = 5.0    # durata massima di una riga testo (secondi)
LOGO_H_RATIO   = 0.35   # logo = 35% dell'altezza video
FINAL_LOGO_DUR = 4.0    # secondi di logo garantiti alla fine del video


class KaraokeRenderer:
    def __init__(self, output_dir: str, background_image: str,
                 resolution: str = "1920x1080", fps: int = 30,
                 font_file: str = None,
                 primary_color: str = "&H00FFFFFF",
                 highlight_color: str = "&H0000FFFF",
                 outline_color: str = "&H00000000",
                 font_size: int = 72,
                 margin_v: int = 0,
                 watermark_text: str = "",
                 watermark_enabled: bool = False,
                 logo_path: str = "logo.png"):

        self.output_dir        = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.background_image  = background_image
        self.resolution        = resolution
        self.fps               = fps
        self.font_file         = font_file
        self.primary_color     = primary_color
        self.highlight_color   = highlight_color
        self.outline_color     = outline_color
        self.font_size         = font_size
        self.margin_v          = margin_v
        self.watermark_text    = watermark_text
        self.watermark_enabled = watermark_enabled
        self.logo_path         = logo_path

        w, h = resolution.split("x")
        self.width  = int(w)
        self.height = int(h)

    # ── Entry point ────────────────────────────────────────────────────────

    def render(self, title: str, artist: str, audio_path: str,
               lyrics_data: dict, song_id: int) -> str | None:

        # Nome file leggibile: "Artista - Titolo.mp4"
        import re as _re
        def _safe(s):
            return _re.sub(r'[\\/:*?"<>|]', '_', s).strip()
        output_video = self.output_dir / f"{_safe(artist)} - {_safe(title)}.mp4"
        if output_video.exists():
            logger.info(f"Video già presente: {output_video}")
            return str(output_video)

        duration = self._get_duration(audio_path)
        if duration <= 0:
            logger.error("Impossibile determinare durata audio")
            return None

        # Prepara righe con timing (end_time cappato a MAX_LINE_DUR)
        lines = lyrics_data.get("content", [])
        if lyrics_data["type"] == "plain":
            lines = self._plain_to_timed(lyrics_data["content"], duration)
        if not lines:
            lines = [{"time": 5.0, "text": f"{title} — {artist}"}]
        lines = self._add_end_times(lines, duration)

        # Trova gap strumentali (basati sugli end_time cappati)
        all_gaps  = self._find_gaps(lines, duration)
        # Logo solo nei gap fuori dal titolo
        logo_gaps = [(s, e) for s, e in all_gaps if s >= TITLE_DURATION]

        # Genera ASS (solo il testo; nessun testo nei gap)
        ass_path = self.output_dir / f"{song_id}_karaoke.ass"
        self._write_ass(title, artist, lines, str(ass_path), duration)

        cover_path = self._make_background(song_id)

        success = self._ffmpeg_render(
            audio_path=audio_path,
            ass_path=str(ass_path),
            cover_path=cover_path,
            output_path=str(output_video),
            duration=duration,
            title=title,
            artist=artist,
            logo_gaps=logo_gaps,
        )

        if success:
            logger.info(f"✓ Video karaoke: {output_video}")
            return str(output_video)
        return None

    # ── Timing righe ───────────────────────────────────────────────────────

    def _add_end_times(self, lines: list, total_duration: float) -> list:
        """
        Assegna end_time a ogni riga.
        Regola chiave: la riga NON può durare più di MAX_LINE_DUR secondi
        anche se la riga successiva è molto lontana nel tempo.
        Questo lascia i gap strumentali "liberi" per il logo.
        """
        result = []
        for i, line in enumerate(lines):
            line = dict(line)
            if i < len(lines) - 1:
                # Fine naturale = inizio riga successiva meno 0.2s
                natural_end = lines[i + 1]["time"] - 0.2
                # Cap: se il gap è lungo, non superare MAX_LINE_DUR
                capped_end  = line["time"] + MAX_LINE_DUR
                end = min(natural_end, capped_end)
            else:
                # Ultima riga: stessa logica
                end = min(total_duration - 1.0, line["time"] + MAX_LINE_DUR)

            line["end_time"] = max(line["time"] + 0.8, end)
            line["duration"] = line["end_time"] - line["time"]
            result.append(line)
        return result

    # ── Gap detection ──────────────────────────────────────────────────────

    def _find_gaps(self, lines: list, total_duration: float) -> list[tuple[float, float]]:
        """
        Trova le finestre senza testo più lunghe di GAP_THRESHOLD secondi.
        Queste sono le parti strumentali dove apparirà il logo.
        """
        gaps = []

        if not lines:
            gaps.append((TITLE_DURATION + 0.5, total_duration - 0.5))
            return gaps

        # Gap iniziale (tra fine titolo e prima riga)
        first_start = lines[0]["time"]
        if first_start - TITLE_DURATION > GAP_THRESHOLD:
            gaps.append((TITLE_DURATION + 0.3, first_start - 0.3))

        # Gap tra righe consecutive
        for i in range(len(lines) - 1):
            gap_s = lines[i]["end_time"] + 0.2
            gap_e = lines[i + 1]["time"] - 0.3
            if gap_e - gap_s > GAP_THRESHOLD:
                gaps.append((gap_s, gap_e))

        # Gap finale (dopo l'ultima riga)
        last_end = lines[-1]["end_time"]
        final_gap_start = last_end + 0.2
        final_gap_end   = total_duration - 0.3

        if final_gap_end - final_gap_start > GAP_THRESHOLD:
            # Il gap naturale è già abbastanza lungo
            gaps.append((final_gap_start, final_gap_end))
        else:
            # Forza comunque FINAL_LOGO_DUR secondi di logo alla fine:
            # Anticipa l'inizio del gap riducendo l'end_time dell'ultima riga
            forced_start = max(last_end - 0.5, total_duration - FINAL_LOGO_DUR)
            if forced_start < final_gap_end - FADE_DUR * 2:
                # Accorcia l'end_time dell'ultima riga (aggiornamento diretto)
                if lines:
                    lines[-1]["end_time"] = forced_start
                gaps.append((forced_start + 0.1, final_gap_end))

        return gaps

    # ── Background ─────────────────────────────────────────────────────────

    def _make_background(self, song_id: int) -> str:
        cover_path = self.output_dir / f"{song_id}_cover.png"
        if cover_path.exists():
            return str(cover_path)

        bg = self.background_image
        if bg and Path(bg).exists():
            cmd = [
                "ffmpeg", "-y", "-i", bg,
                "-vf",
                f"scale={self.width}:{self.height}:force_original_aspect_ratio=increase,"
                f"crop={self.width}:{self.height}",
                "-frames:v", "1", str(cover_path)
            ]
        else:
            logger.warning(f"Background non trovato: {bg} — uso sfondo scuro")
            cmd = [
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", f"color=0x1a1a2e:size={self.resolution}:rate=1",
                "-frames:v", "1", str(cover_path)
            ]

        try:
            subprocess.run(cmd, capture_output=True, timeout=30, check=True)
        except Exception as e:
            logger.warning(f"Background fallito: {e}")
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", f"color=black:size={self.resolution}:rate=1",
                "-frames:v", "1", str(cover_path)
            ], capture_output=True, timeout=30)

        return str(cover_path)

    # ── ASS Subtitles ──────────────────────────────────────────────────────

    def _write_ass(self, title: str, artist: str, lines: list,
                   ass_path: str, total_duration: float):

        def ac(c):
            return c.lstrip("&H") if c.startswith("&H") else c

        primary   = ac(self.primary_color)
        highlight = ac(self.highlight_color)
        outline   = ac(self.outline_color)
        font      = "Arial"
        title_size = self.font_size * 2  # titolo = doppio del testo lirica

        ass_header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {self.width}
PlayResY: {self.height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,{font},{self.font_size},&H{primary},&H{highlight},&H{outline},&H80000000,-1,0,0,0,100,100,2,0,1,4,2,5,40,40,0,1
Style: Title,{font},{title_size},&H00FFFFFF,&H{highlight},&H{outline},&H90000000,-1,0,0,0,100,100,1,0,1,5,3,5,40,40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        events = []

        # Schermata titolo: artista piccolo + titolo grande, centrati
        artist_tag = f"{{\\fs{self.font_size}\\b0\\c&H00AAAAAA&}}{artist}"
        title_tag  = f"{{\\fs{title_size}\\b1\\c&H00FFFFFF&}}{title}"
        events.append(
            f"Dialogue: 0,0:00:00.00,{self._t(TITLE_DURATION)},"
            f"Title,,0,0,0,,{artist_tag}\\N{title_tag}"
        )

        # Watermark opzionale
        if self.watermark_enabled and self.watermark_text:
            events.append(
                f"Dialogue: 0,0:00:00.00,{self._t(total_duration)},"
                f"Karaoke,,0,0,0,,{self.watermark_text}"
            )

        # Righe karaoke (le righe finiscono sui loro end_time cappati)
        for line_data in lines:
            start  = line_data["time"]
            end    = line_data["end_time"]
            dur_cs = int((end - start) * 100)
            ass_text = self._karaoke_text(line_data["text"], dur_cs)
            events.append(
                f"Dialogue: 0,{self._t(start)},{self._t(end + 0.1)},"
                f"Karaoke,,0,0,0,,{ass_text}"
            )

        ass_content = ass_header + "\n".join(events) + "\n"
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)

    def _karaoke_text(self, text: str, total_cs: int) -> str:
        """Testo ASS con tag \\kf per fill parola per parola."""
        words = text.split()
        if not words:
            return text
        total_chars = sum(len(w) for w in words) or 1
        parts, remaining = [], total_cs
        for i, word in enumerate(words):
            if i == len(words) - 1:
                cs = remaining
            else:
                cs = max(10, int(total_cs * len(word) / total_chars))
                remaining -= cs
            parts.append(f"{{\\kf{cs}}}{word}")
        return " ".join(parts)

    # ── FFmpeg render ──────────────────────────────────────────────────────

    def _ffmpeg_render(self, audio_path: str, ass_path: str, cover_path: str,
                       output_path: str, duration: float,
                       title: str, artist: str,
                       logo_gaps: list[tuple[float, float]] = None) -> bool:
        """
        Assembla il video finale.

        Logo con fade in/out via filtro 'fade' nativo FFmpeg (SIMD-accelerated).
        Per ogni gap (S, E):
          1. Prende la stream del logo (immagine PNG in loop)
          2. Shift dei PTS a S con setpts=PTS+S/TB
          3. fade=in:st=S:d=FADE_DUR:alpha=1  (fade in all'inizio del gap)
          4. fade=out:st=(E-FADE_DUR):d=FADE_DUR:alpha=1  (fade out alla fine)
          5. overlay centrato, eof_action=pass (scompare quando finisce)
        I gap si sovrappongono sulla chain: bg → [+logo0] → [+logo1] → ...
        """
        # Escape path ASS per Windows: C:\foo\bar.ass → C\:/foo/bar.ass
        ass_esc = ass_path.replace("\\", "/")
        ass_esc = re.sub(r'^([A-Za-z]):', r'\1\\:', ass_esc)

        logo_path = self.logo_path
        has_logo  = bool(logo_gaps) and logo_path and Path(logo_path).exists()
        logo_h    = int(self.height * LOGO_H_RATIO)
        F         = FADE_DUR

        base_vf = (
            f"scale={self.width}:{self.height}:force_original_aspect_ratio=increase,"
            f"crop={self.width}:{self.height},"
            f"subtitles='{ass_esc}'"
        )

        if has_logo:
            n = len(logo_gaps)

            # ── Costruisce filter_complex ──────────────────────────────────
            parts = []

            # 1. Background + sottotitoli
            parts.append(f"[0:v]{base_vf}[bg]")

            # 2. Per ogni gap: scala logo, shift PTS, fade in+out
            #    Input 2 è il logo PNG in loop → PTS parte da 0 e cresce.
            #    Con setpts=PTS+S/TB spostiamo i timestamp al momento giusto
            #    così l'overlay sa quando usare questi frame.
            split_labels = "".join(f"[ls{i}]" for i in range(n))
            parts.append(
                f"[2:v]scale=-1:{logo_h}:force_original_aspect_ratio=decrease,"
                f"format=rgba,split={n}{split_labels}"
            )

            for i, (s, e) in enumerate(logo_gaps):
                fade_out_st = max(s, e - F)
                parts.append(
                    f"[ls{i}]setpts=PTS+{s:.3f}/TB,"
                    f"fade=t=in:st={s:.3f}:d={F:.3f}:alpha=1,"
                    f"fade=t=out:st={fade_out_st:.3f}:d={F:.3f}:alpha=1[l{i}]"
                )

            # 3. Chain di overlay: bg → v0 → v1 → ... → out
            prev = "[bg]"
            for i, (s, e) in enumerate(logo_gaps):
                out_label = "[out]" if i == n - 1 else f"[v{i}]"
                parts.append(
                    f"{prev}[l{i}]overlay=x=(W-w)/2:y=(H-h)/2:eof_action=pass{out_label}"
                )
                prev = f"[v{i}]"

            filter_complex = ";".join(parts)

            cmd = [
                "ffmpeg", "-y",
                # Input 0: background
                "-loop", "1", "-framerate", str(self.fps), "-i", cover_path,
                # Input 1: audio
                "-i", audio_path,
                # Input 2: logo (loop)
                "-loop", "1", "-framerate", str(self.fps), "-i", logo_path,
                "-filter_complex", filter_complex,
                "-map", "[out]",
                "-map", "1:a",
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                "-t", str(duration),
                "-metadata", f"title={title}",
                "-metadata", f"artist={artist}",
                "-movflags", "+faststart",
                output_path,
            ]
            logger.info(f"Rendering con logo ({len(logo_gaps)} gap, fade {F}s): {Path(output_path).name}")

        else:
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-framerate", str(self.fps), "-i", cover_path,
                "-i", audio_path,
                "-vf", base_vf,
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                "-t", str(duration),
                "-metadata", f"title={title}",
                "-metadata", f"artist={artist}",
                "-movflags", "+faststart",
                output_path,
            ]
            if logo_gaps and not (logo_path and Path(logo_path).exists()):
                logger.info(f"Rendering senza logo (logo.png non trovato in '{logo_path}')")
            else:
                logger.info(f"Rendering: {Path(output_path).name}")

        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if r.returncode != 0:
                logger.error(f"FFmpeg errore:\n{r.stderr[-2000:]}")
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.error("Timeout FFmpeg")
            return False

    # ── Utilities ──────────────────────────────────────────────────────────

    def _t(self, seconds: float) -> str:
        """Secondi → formato ASS H:MM:SS.cc"""
        s = max(0.0, seconds)
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        return f"{h}:{m:02d}:{s % 60:05.2f}"

    def _get_duration(self, audio_path: str) -> float:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "csv=p=0", audio_path],
                capture_output=True, text=True, timeout=30
            )
            if r.returncode == 0:
                return float(r.stdout.strip())
        except Exception:
            pass
        return 0.0

    def _plain_to_timed(self, plain_text: str, duration: float) -> list[dict]:
        lines = [l.strip() for l in plain_text.split("\n")
                 if l.strip() and not l.strip().startswith("[")]
        if not lines:
            return []
        start = 5.0
        end   = duration - 10.0
        step  = (end - start) / max(len(lines), 1)
        return [{"time": start + i * step, "text": l} for i, l in enumerate(lines)]
