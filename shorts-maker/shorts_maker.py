#!/usr/bin/env python3
"""Transforma um trecho de video (URL ou arquivo local) em um Short vertical 1080x1920.

Passos: baixa/corta o trecho -> (opcional) gera legendas -> converte para 9:16 -> exporta MP4.
Requer ffmpeg/ffprobe. yt-dlp so e necessario para URLs; faster-whisper so para legendas automaticas.
"""
from __future__ import annotations

import argparse
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

WIDTH, HEIGHT = 1080, 1920
ASS_PLAY_RES_Y = 288
SHORTS_MAX_SECONDS = 180
REUSE_REMINDER = (
    "Lembrete: use apenas conteudo seu, com permissao do criador ou com licenca que permita reuso "
    "(veja o README)."
)


class ShortsError(Exception):
    """Erro esperado (entrada invalida, ferramenta ausente, comando falhou)."""


class Word(NamedTuple):
    start: float
    end: float
    text: str


@dataclass
class Job:
    source: str
    start: float
    end: float | None
    title: str | None = None


def is_url(source: str) -> bool:
    return re.match(r"^https?://", source) is not None


# ---------------------------------------------------------------- time helpers

def parse_time(value: str) -> float:
    """Aceita SS, MM:SS ou HH:MM:SS (com decimais no ultimo campo)."""
    parts = value.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"tempo invalido: {value!r}")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    if seconds < 0:
        raise ValueError(f"tempo negativo: {value!r}")
    return seconds


def fmt_hms(seconds: float) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{secs:06.3f}"


def fmt_srt_time(seconds: float) -> str:
    total_ms = round(seconds * 1000)
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


# -------------------------------------------------------------------- captions

def group_words(words: list[Word], max_words: int = 3, max_gap: float = 0.7) -> list[Word]:
    """Agrupa palavras em legendas curtas (estilo Shorts): poucas palavras por vez."""
    cues: list[Word] = []
    current: list[Word] = []

    def flush() -> None:
        if current:
            cues.append(Word(current[0].start, current[-1].end, " ".join(w.text for w in current)))
            current.clear()

    for word in words:
        if current and (
            len(current) >= max_words
            or word.start - current[-1].end > max_gap
            or current[-1].text.endswith((".", "?", "!"))
        ):
            flush()
        current.append(word)
    flush()
    return cues


def to_srt(cues: list[Word]) -> str:
    blocks = [
        f"{i}\n{fmt_srt_time(c.start)} --> {fmt_srt_time(c.end)}\n{c.text}\n"
        for i, c in enumerate(cues, start=1)
    ]
    return "\n".join(blocks)


def transcribe(audio: Path, language: str, model_size: str) -> list[Word]:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise ShortsError(
            "Legendas automaticas precisam do faster-whisper: pip install faster-whisper "
            "(ou passe um arquivo pronto com --srt)."
        )
    model = WhisperModel(model_size, compute_type="int8")
    segments, _ = model.transcribe(str(audio), language=language, word_timestamps=True, vad_filter=True)
    return [
        Word(w.start, w.end, w.word.strip())
        for seg in segments
        for w in (seg.words or [])
        if w.word.strip()
    ]


# ------------------------------------------------------------------ ffmpeg bits

def run(cmd: list[str], dry_run: bool, cwd: Path | None = None) -> None:
    print("$", shlex.join(cmd), file=sys.stderr)
    if dry_run:
        return
    try:
        subprocess.run(cmd, check=True, cwd=cwd)
    except FileNotFoundError:
        raise ShortsError(f"comando nao encontrado: {cmd[0]}")
    except subprocess.CalledProcessError as exc:
        raise ShortsError(f"{cmd[0]} falhou (codigo {exc.returncode})")


def probe_duration(path: Path) -> float | None:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def build_filter_graph(layout: str, crop_pos: float, captions: bool, style: str) -> str:
    w, h = WIDTH, HEIGHT
    if layout == "blur":
        # Fundo: o proprio video ampliado e desfocado (calculado em baixa resolucao por velocidade).
        base = (
            "[0:v]split=2[a][b];"
            f"[a]scale={w // 4}:{h // 4}:force_original_aspect_ratio=increase,crop={w // 4}:{h // 4},"
            f"boxblur=10:2,scale={w}:{h},setsar=1[bg];"
            f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base]"
        )
    elif layout == "crop":
        base = (
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h}:(iw-{w})*{crop_pos}:(ih-{h})/2,setsar=1[base]"
        )
    elif layout == "fit":
        base = (
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[base]"
        )
    else:
        raise ShortsError(f"layout desconhecido: {layout}")
    if captions:
        return f"{base};[base]subtitles=captions.srt:force_style='{style}'[v]"
    return f"{base};[base]null[v]"


def caption_style(font: str | None, font_size: int, margin: int) -> str:
    # O ffmpeg converte .srt para ASS com escala interna fixa de altura 288; os valores em px
    # (relativos ao quadro de 1920 de altura) precisam ser convertidos para essa escala.
    k = ASS_PLAY_RES_Y / HEIGHT
    parts = [
        f"FontSize={font_size * k:.1f}",
        "Bold=1",
        "Alignment=2",
        f"MarginV={margin * k:.1f}",
        f"Outline={6 * k:.1f}",
        "Shadow=0",
        "PrimaryColour=&H00FFFFFF",
        "OutlineColour=&H00000000",
    ]
    if font:
        parts.insert(0, f"FontName={font}")
    return ",".join(parts)


# --------------------------------------------------------------------- pipeline

def acquire(job: Job, workdir: Path, dry_run: bool) -> tuple[Path, float, float | None]:
    """Devolve (arquivo, inicio dentro do arquivo, duracao do trecho)."""
    if is_url(job.source):
        print(REUSE_REMINDER, file=sys.stderr)
        run(
            [
                "yt-dlp", "--no-playlist",
                "--download-sections", f"*{fmt_hms(job.start)}-{fmt_hms(job.end)}",
                "--force-keyframes-at-cuts",
                "-f", "bv*[height<=1080]+ba/b",
                "--merge-output-format", "mp4",
                "-o", str(workdir / "source.%(ext)s"),
                job.source,
            ],
            dry_run,
        )
        if dry_run:
            return workdir / "source.mp4", 0.0, job.end - job.start
        files = [p for p in sorted(workdir.glob("source.*")) if p.suffix not in {".part", ".ytdl"}]
        if not files:
            raise ShortsError("yt-dlp nao gerou nenhum arquivo")
        return files[0], 0.0, job.end - job.start

    path = Path(job.source)
    if not path.is_file():
        raise ShortsError(f"arquivo nao encontrado: {job.source}")
    if job.end is not None:
        return path, job.start, job.end - job.start
    total = probe_duration(path)
    return path, job.start, (total - job.start) if total is not None else None


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-") or "short"


def unique_path(outdir: Path, base: str) -> Path:
    path, n = outdir / f"{base}.mp4", 2
    while path.exists():
        path = outdir / f"{base}_{n}.mp4"
        n += 1
    return path


def prepare_captions(src: Path, ss: float, dur: float | None, workdir: Path, args) -> bool:
    """Cria workdir/captions.srt (a partir de --srt ou do Whisper). True se houver legendas."""
    if args.srt:
        shutil.copyfile(args.srt, workdir / "captions.srt")
        return True
    if not args.captions:
        return False
    if args.dry_run:
        print("(dry-run) legendas automaticas seriam geradas aqui", file=sys.stderr)
        return True
    audio = workdir / "audio.wav"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(ss), "-i", str(src)]
    if dur:
        cmd += ["-t", f"{dur:.3f}"]
    run(cmd + ["-vn", "-ac", "1", "-ar", "16000", str(audio)], False)
    words = transcribe(audio, args.lang, args.whisper_model)
    if not words:
        print("Nenhuma fala detectada; exportando sem legendas.", file=sys.stderr)
        return False
    (workdir / "captions.srt").write_text(to_srt(group_words(words, args.words_per_cue)), encoding="utf-8")
    return True


def process(job: Job, args) -> Path:
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    base = slugify(job.title or args.name or Path(job.source).stem)
    out = unique_path(outdir, base)
    with tempfile.TemporaryDirectory(prefix="shorts_") as tmp:
        workdir = Path(tmp)
        src, ss, dur = acquire(job, workdir, args.dry_run)
        if dur is not None and dur > SHORTS_MAX_SECONDS:
            print(f"Aviso: o trecho tem {dur:.0f}s e o Short maximo e {SHORTS_MAX_SECONDS}s.", file=sys.stderr)
        has_captions = prepare_captions(src, ss, dur, workdir, args)
        style = caption_style(args.font, args.font_size, args.caption_margin)
        graph = build_filter_graph(args.layout, args.crop_pos, has_captions, style)
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats"]
        if ss:
            cmd += ["-ss", f"{ss:.3f}"]
        cmd += ["-i", str(src.resolve())]
        if dur:
            cmd += ["-t", f"{dur:.3f}"]
        cmd += [
            "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
            "-r", str(args.fps), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
            str(out.resolve()),
        ]
        run(cmd, args.dry_run, cwd=workdir)
    if args.title or args.credit or job.title:
        write_description(out, job.title or args.title, args)
    print(f"Pronto: {out}", file=sys.stderr)
    return out


def write_description(video: Path, title: str | None, args) -> None:
    lines = [title or "", ""]
    if args.credit:
        lines.append(f"Credito: {args.credit}")
    if args.hashtags:
        lines.append(args.hashtags)
    video.with_suffix(".txt").write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


# -------------------------------------------------------------------------- CLI

def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Converte um trecho de video em um Short vertical 1080x1920.")
    p.add_argument("source", nargs="?", help="URL (yt-dlp) ou caminho de um video local")
    p.add_argument("--start", default="0", help="inicio do trecho: SS, MM:SS ou HH:MM:SS (padrao: 0)")
    p.add_argument("--end", help="fim do trecho (obrigatorio para URLs)")
    p.add_argument("--batch", metavar="ARQUIVO", help="arquivo com varias linhas: FONTE INICIO FIM [TITULO]")
    p.add_argument("-o", "--outdir", default="output", help="pasta de saida (padrao: output)")
    p.add_argument("--name", help="nome base do arquivo de saida")
    p.add_argument("--layout", choices=["blur", "crop", "fit"], default="blur",
                   help="blur: fundo desfocado | crop: corta o centro | fit: barras pretas")
    p.add_argument("--crop-pos", type=float, default=0.5,
                   help="no layout crop, posicao horizontal do corte de 0 (esquerda) a 1 (direita)")
    p.add_argument("--captions", action="store_true", help="gera legendas automaticas (faster-whisper)")
    p.add_argument("--srt", metavar="ARQUIVO", help="usa um .srt pronto (tempos relativos ao inicio do trecho)")
    p.add_argument("--lang", default="pt", help="idioma da fala para o Whisper (padrao: pt)")
    p.add_argument("--whisper-model", default="small", help="tiny, base, small, medium... (padrao: small)")
    p.add_argument("--words-per-cue", type=int, default=3, help="palavras por legenda (padrao: 3)")
    p.add_argument("--font", help="nome da fonte das legendas (precisa estar instalada)")
    p.add_argument("--font-size", type=int, default=72)
    p.add_argument("--caption-margin", type=int, default=520, help="distancia da legenda ate a borda inferior, em px")
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--title", help="titulo do Short (gera um .txt com titulo, credito e hashtags)")
    p.add_argument("--credit", help='credito para a descricao, ex.: "Canal X (com permissao)"')
    p.add_argument("--hashtags", default="#Shorts", help='hashtags da descricao (padrao: "#Shorts")')
    p.add_argument("--dry-run", action="store_true", help="so mostra os comandos, sem executar")
    args = p.parse_args(argv)
    if not 0 <= args.crop_pos <= 1:
        p.error("--crop-pos deve estar entre 0 e 1")
    if not args.source and not args.batch:
        p.error("informe uma FONTE ou --batch ARQUIVO")
    if args.captions and args.srt:
        p.error("use --captions ou --srt, nao os dois")
    return args


def load_jobs(args: argparse.Namespace) -> list[Job]:
    if not args.batch:
        start = parse_time(args.start)
        end = parse_time(args.end) if args.end else None
        return [Job(args.source, start, end, args.title)]
    jobs = []
    lines = Path(args.batch).read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines, start=1):
        fields = shlex.split(line, comments=True)
        if not fields:
            continue
        if len(fields) < 3:
            raise ShortsError(f"{args.batch}:{number}: esperado FONTE INICIO FIM [TITULO]")
        source, start, end, *title = fields
        jobs.append(Job(source, parse_time(start), parse_time(end), " ".join(title) or None))
    return jobs


def validate(jobs: list[Job]) -> None:
    for job in jobs:
        if is_url(job.source) and job.end is None:
            raise ShortsError(f"{job.source}: para URLs informe --end (evita baixar lives/VODs inteiros)")
        if job.end is not None and job.end <= job.start:
            raise ShortsError(f"{job.source}: o fim deve ser maior que o inicio")


def check_tools(need_ytdlp: bool) -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") + (("yt-dlp",) if need_ytdlp else ()) if not shutil.which(t)]
    if missing:
        raise ShortsError("ferramentas ausentes no PATH: " + ", ".join(missing) + " (veja o README)")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        jobs = load_jobs(args)
        validate(jobs)
        check_tools(any(is_url(j.source) for j in jobs))
    except (ShortsError, ValueError, OSError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2
    failures = 0
    for job in jobs:
        try:
            process(job, args)
        except ShortsError as exc:
            print(f"Erro em {job.source}: {exc}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
