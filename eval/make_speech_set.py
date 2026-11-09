"""Turn a test CSV into what the app actually sees: speak each command with Timbre, transcribe with Prisma.

    python eval/make_speech_set.py eval/commands.csv eval/commands_speech.csv
    python eval/make_speech_set.py eval/commands.csv out.csv --hints none     # compare recognition hint settings
    python eval/make_speech_set.py eval/commands_v4.jsonl eval/commands_v4_speech.jsonl   # JSONL sets: "text" field

Uses the same Prisma settings and recognition hints as the app unless --hints says otherwise. Synthesized
audio is cached in eval/audio_cache/, so re-transcribing with other settings only calls Prisma. Labels are
copied unchanged; the original text is kept in a "typed" column (field, for JSONL).
"""

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bolos.__main__ import APP_HINTS, recognition_hints  # noqa: E402
from bolos.config import (
    ROOT,  # noqa: E402
    load_settings,  # noqa: E402
)
from bolos.contacts import ContactBook  # noqa: E402
from bolos.executor.apps import Catalog  # noqa: E402
from bolos.speech import Prisma, Timbre  # noqa: E402

CACHE = Path(__file__).resolve().parent / "audio_cache"


def spoken_language(lang_tag: str) -> str:
    """"hi-en" is spoken Hindi, "kn" is Kannada, "en" is Indian English."""
    return lang_tag.split("-")[0] + "-IN"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("src", type=Path)
    p.add_argument("dst", type=Path)
    p.add_argument("--hints", choices=["app", "none", "apps", "contacts"], default="app",
                   help="app = exactly what the running app sends")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    settings = load_settings()
    stt, tts = Prisma(settings), Timbre(settings)
    contacts = ContactBook.load(settings.contacts_path)
    hints = {"app": recognition_hints(contacts, Catalog(ROOT / ".cache" / "apps.json").names()), "none": [],
             "apps": APP_HINTS,
             "contacts": contacts.names()}[args.hints]

    jsonl = args.src.suffix == ".jsonl"
    text_field = "text" if jsonl else "command"
    if jsonl:
        rows = [json.loads(line) for line in args.src.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    else:
        with args.src.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

    def audio(row: dict) -> bytes:
        lang = spoken_language(row["lang"])
        key = hashlib.sha1(f"{settings.tts_model}|{settings.tts_voice}|{lang}|{row[text_field]}".encode()).hexdigest()
        path = CACHE / f"{key}.wav"
        if not path.exists():
            CACHE.mkdir(exist_ok=True)
            path.write_bytes(tts.synthesize(row[text_field], lang))
        return path.read_bytes()

    def transcribe(row: dict) -> dict:
        heard = stt.transcribe(audio(row), keyterms=hints).text
        print(f"{row[text_field]!r} -> {heard!r}")
        return {**row, "typed": row[text_field], text_field: heard}

    out = [transcribe(row) for row in rows]  # Timbre allows one active request per key

    if jsonl:
        args.dst.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out), encoding="utf-8")
    else:
        with args.dst.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(out[0]))
            writer.writeheader()
            writer.writerows(out)
    empty = sum(1 for r in out if not r[text_field])
    print(f"Wrote {len(out)} rows to {args.dst} ({empty} empty transcripts)")


if __name__ == "__main__":
    main()
