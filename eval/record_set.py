"""Record a real person reading test commands, and turn the recordings into a speech test set.

    uv run python eval/record_set.py eval/commands_v6.jsonl --speaker ram
    uv run python eval/record_set.py my_commands.csv --speaker asha --set kannada_home --languages row
    uv run python eval/record_set.py eval/commands_v6.jsonl --speaker ram --auto      # next one starts by itself
    uv run python eval/record_set.py eval/commands_v6.jsonl --speaker ram --retranscribe   # Prisma again, no mic

Commands are shown one at a time. Press Enter and read the command aloud; recording stops by itself after
~0.8 s of silence (a local energy detector; Enter stops it at once). Esc skips a command, r records the
previous one again, q quits. Each reading is saved as a WAV under eval/recordings/<set>/<speaker>/ and
transcribed by Prisma with the app's own settings and recognition hints, so the transcript is what Bol OS
would have heard.

The output (default eval/<set>_speech_<speaker>.jsonl) has the format of eval/commands_v5_speech.jsonl, so
`uv run python eval/evaluate.py --sets <file name>` scores it: each source row with "text" set to the
transcript, "typed" to the text that was read, plus "speaker" and "audio" (the WAV, relative to eval/). CSV
sources (a "command" column, no slots) get their slot fields listed as "disputed", which evaluate.py leaves
out of the field scores.

Resumable: readings already in the output are not asked again, nor are skipped ones (--retry-skipped asks
them again); a WAV recorded but not yet transcribed (a crash, no network) is transcribed on the next run.

The Gnani key allows one active request for Prisma and Timbre together, so transcription waits for the same
cross-process lock Timbre uses (bolos.speech.timbre_turn) and says so while another process holds it.
"""

import argparse
import csv
import hashlib
import io
import json
import os
import queue
import re
import sys
import time
import wave
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "eval"
sys.path.insert(0, str(ROOT))

RATE = 16000
FRAME = 480  # 30 ms
FRAME_S = FRAME / RATE
LANG_NAMES = {"hi": "Hindi", "en": "English", "kn": "Kannada", "ta": "Tamil", "te": "Telugu", "mr": "Marathi",
              "ml": "Malayalam", "bn": "Bengali", "gu": "Gujarati", "pa": "Punjabi", "none": "any language"}


# Endpointing


def frame_db(frame: np.ndarray) -> float:
    """Loudness of a frame in dB relative to full scale (int16)."""
    rms = float(np.sqrt(np.mean(np.square(frame.astype(np.float64))))) if len(frame) else 0.0
    return 20 * np.log10(rms / 32768 + 1e-10)


def noise_floor_db(frames: list[np.ndarray]) -> float:
    """The room's background level from audio heard before the reading. A quiet percentile, so a cough or
    a word in it doesn't count; clamped, so a silent (digital zero) or noisy start can't make the detector
    deaf or trigger-happy."""
    if not frames:
        return -60.0
    return float(np.clip(np.percentile([frame_db(f) for f in frames], 20), -75.0, -40.0))


@dataclass
class Endpointer:
    """Finds the start and end of one reading in 30 ms frames: speech is a frame `margin_db` above the
    background; it starts after `start_s` of speech in a row and ends after `silence_s` of quiet. A burst
    shorter than `min_speech_s` (a key click, a cough) is ignored.

    Once someone is reading, a frame `below_peak_db` under the loudest part of the reading is quiet too, so
    a background that was measured too low (a very quiet room, then a fan comes on) can't keep a reading
    open until `max_s`."""

    noise_db: float
    margin_db: float = 10.0
    below_peak_db: float = 35.0
    start_s: float = 0.09
    silence_s: float = 0.8
    min_speech_s: float = 0.25
    max_wait_s: float = 10.0
    max_s: float = 28.0  # Prisma's REST limit is 30 s
    state: str = "waiting"  # -> "speech" -> "done", or "no_speech"
    n: int = 0  # frames fed
    start: int = -1  # first speech frame
    end: int = -1  # one past the last speech frame
    peak_db: float = -200.0  # loudest frame of the reading so far
    _run: int = 0  # speech frames in a row, while waiting
    _quiet: int = 0  # quiet frames in a row, while speaking
    levels: list[float] = field(default_factory=list)

    @property
    def threshold(self) -> float:
        if self.state == "speech":
            return max(self.noise_db + self.margin_db, self.peak_db - self.below_peak_db)
        return self.noise_db + self.margin_db

    def feed(self, frame: np.ndarray) -> str:
        db = frame_db(frame)
        self.levels.append(db)
        loud = db > self.threshold
        i, self.n = self.n, self.n + 1
        if self.state == "waiting":
            if loud:
                self._run += 1
                if self._run * FRAME_S >= self.start_s:
                    self.state, self.start, self.end, self._quiet = "speech", i - self._run + 1, i + 1, 0
                    self.peak_db = max(self.levels[-self._run:])
            else:
                self._run = 0
                self.noise_db = float(np.clip(0.95 * self.noise_db + 0.05 * db, -75.0, -40.0))  # follow the room
                if self.n * FRAME_S >= self.max_wait_s:
                    self.state = "no_speech"
        elif self.state == "speech":
            if loud:
                self.peak_db = max(self.peak_db, db)
                self.end, self._quiet = i + 1, 0
            else:
                self._quiet += 1
            if self._quiet * FRAME_S >= self.silence_s:
                if (self.end - self.start) * FRAME_S < self.min_speech_s:  # a click, not a reading
                    self.state, self.start, self.end, self._run, self.peak_db = "waiting", -1, -1, 0, -200.0
                    if self.n * FRAME_S >= self.max_wait_s:
                        self.state = "no_speech"
                else:
                    self.state = "done"
            elif (i + 1 - self.start) * FRAME_S >= self.max_s:
                self.state = "done"
        return self.state

    def stop(self) -> str:
        """The reader pressed Enter: done now if anything was said."""
        self.state = "done" if self.state == "speech" else "no_speech"
        return self.state


def cut(frames: list[np.ndarray], start: int, end: int, pre_s: float = 0.3, post_s: float = 0.4) -> np.ndarray:
    """The reading with a little audio either side: words start softly, and Prisma likes a short tail."""
    a = max(0, start - round(pre_s / FRAME_S))
    b = min(len(frames), end + round(post_s / FRAME_S))
    return np.concatenate(frames[a:b]) if b > a else np.zeros(0, np.int16)


def to_wav(samples: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.astype(np.int16).tobytes())
    return buf.getvalue()


def read_wav(data: bytes) -> np.ndarray:
    """16 kHz mono int16 samples from a WAV (any rate, mono or stereo)."""
    with wave.open(io.BytesIO(data), "rb") as w:
        rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError("only 16-bit WAVs are supported")
    x = np.frombuffer(raw, np.int16).reshape(-1, channels).mean(axis=1)
    if rate != RATE:
        x = np.interp(np.arange(0, len(x), rate / RATE), np.arange(len(x)), x)
    return x.astype(np.int16)


# Audio sources


class MicSource:
    """The default microphone, 30 ms frames. Open for the whole session, so the background level is known
    from before each reading and a word started a moment early is still caught."""

    def __init__(self, device=None):
        import sounddevice as sd

        self.frames: queue.Queue = queue.Queue()
        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME, device=device,
                                     callback=lambda data, *_: self.frames.put(data[:, 0].copy()))
        self.stream.start()

    def arm(self, row: dict) -> None:
        pass

    def read(self, timeout: float = 0.1) -> np.ndarray | None:
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self.stream.stop()
        self.stream.close()


class SimulatedMic:
    """Stands in for the microphone in a check of the whole tool without anyone speaking: each recording
    "hears" faint noise, then a WAV given for that row, then noise again. Frames come at real-time pace
    unless fast=True."""

    def __init__(self, clips: dict[str, bytes], fast: bool = False, lead_s: float = 0.6, seed: int = 0):
        self.clips, self.fast, self.lead_s = clips, fast, lead_s
        self.rng = np.random.default_rng(seed)
        self.pending: list[np.ndarray] = []
        self.next_at = time.monotonic()

    def _noise(self, n: int) -> np.ndarray:
        return (self.rng.normal(0, 30, n)).astype(np.int16)  # about -60 dBFS

    def arm(self, row: dict) -> None:
        clip = self.clips.get(row["text"])
        speech = read_wav(clip) if clip else np.zeros(0, np.int16)
        # A real microphone hears the room under the words too, never digital silence (TTS clips have some).
        speech = np.clip(speech.astype(np.int32) + self._noise(len(speech)), -32768, 32767).astype(np.int16)
        audio = np.concatenate([self._noise(int(self.lead_s * RATE)), speech])
        self.pending = [audio[i:i + FRAME] for i in range(0, len(audio) - FRAME + 1, FRAME)]

    def read(self, timeout: float = 0.1) -> np.ndarray | None:
        if not self.fast:
            now = time.monotonic()
            if self.next_at - now > timeout:  # nothing due yet, like a real stream
                time.sleep(timeout)
                return None
            time.sleep(max(0.0, self.next_at - now))
            self.next_at = max(self.next_at, now - 0.5) + FRAME_S
        return self.pending.pop(0) if self.pending else self._noise(FRAME)

    def close(self) -> None:
        pass


# Keys


class ConsoleKeys:
    """Single key presses from the console (Windows)."""

    NAMES = {"\r": "enter", "\n": "enter", "\x1b": "esc", " ": "enter"}

    def poll(self) -> str | None:
        import msvcrt

        if not msvcrt.kbhit():
            return None
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):  # arrows and function keys come in two parts
            msvcrt.getwch()
            return None
        if ch == "\x03":
            raise KeyboardInterrupt
        return self.NAMES.get(ch, ch.lower())


class ScriptedKeys:
    """Key presses from a list, for checks without a person at the keyboard. None in the list means "no key
    this time", and so does every poll once the list runs out."""

    def __init__(self, keys: list[str | None]):
        self.keys = list(keys)

    def poll(self) -> str | None:
        return self.keys.pop(0) if self.keys else None


# The set being recorded


def load_commands(path: Path) -> list[dict]:
    """Rows to read: JSONL with "text", or CSV with "command" (or "text"). CSV rows have no slot labels, so
    their slot fields are marked unscorable for evaluate.py."""
    if path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    else:
        from bolos import actions as a

        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = []
            for r in csv.DictReader(f):
                row = {k: v for k, v in r.items() if k != "command" and v not in (None, "")}
                row["text"] = (r.get("command") or r.get("text") or "").strip()
                if row.get("action"):
                    row["slots"] = {}
                    row["disputed"] = list(a.SLOT_FIELDS.get(row["action"], []))
                rows.append(row)
    return [r for r in rows if isinstance(r, dict) and str(r.get("text", "")).strip()]


def row_key(row: dict, index: int) -> str:
    """A stable name for a row across runs: its id, else its position and text."""
    if row.get("id") not in (None, ""):
        return re.sub(r"[^\w-]", "_", str(row["id"]))
    return f"{index + 1:04d}-{hashlib.sha1(row['text'].encode()).hexdigest()[:8]}"


def safe_name(name: str) -> str:
    return re.sub(r"[^\w-]+", "_", name.strip()).strip("_") or "speaker"


class Store:
    """The output set, rewritten whole (atomically) after every change, plus the keys the reader skipped."""

    def __init__(self, out: Path, skipped: Path):
        self.out, self.skipped_path = out, skipped
        self.rows: dict[str, dict] = {}
        if out.exists():
            for line in out.read_text(encoding="utf-8-sig").splitlines():
                if line.strip() and (row := json.loads(line)).get("key"):
                    self.rows[row["key"]] = row
        self.skipped: set[str] = set(json.loads(skipped.read_text(encoding="utf-8"))) if skipped.exists() else set()

    def put(self, row: dict, order: list[str]) -> None:
        self.rows[row["key"]] = row
        self.skipped.discard(row["key"])
        self.save(order)

    def drop(self, key: str, order: list[str]) -> None:
        self.rows.pop(key, None)
        self.save(order)

    def skip(self, key: str, order: list[str]) -> None:
        self.skipped.add(key)
        self.save(order)

    def save(self, order: list[str]) -> None:
        rank = {k: i for i, k in enumerate(order)}
        rows = sorted(self.rows.values(), key=lambda r: rank.get(r["key"], len(rank)))
        self.out.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.out.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        tmp.replace(self.out)
        self.skipped_path.parent.mkdir(parents=True, exist_ok=True)
        self.skipped_path.write_text(json.dumps(sorted(self.skipped)), encoding="utf-8")


def stt_languages(choice: str, row_lang: str | None, app_languages: str) -> str:
    """Prisma's language_code for a row: the app's own list ("app"), the row's language with Indian English
    ("row"; Hinglish "hi-en" is Hindi), or a list given as is ("hi-IN,bn-IN")."""
    if choice == "app":
        return app_languages
    if choice == "row":
        code = (row_lang or "").lower().split("-")[0]
        return f"{code}-IN,en-IN" if code in LANG_NAMES and code not in ("en", "none") else app_languages
    return choice


# The session


def meter(db: float, threshold: float) -> str:
    filled = int(np.clip((db + 70) / 60, 0, 1) * 30)
    return "[" + "#" * filled + " " * (30 - filled) + f"] {db:5.0f} dB {'speech' if db > threshold else ''}   "


def record_one(source, keys, before: list[np.ndarray], silence_s: float, show=print) -> np.ndarray | str | None:
    """One reading: samples, "esc" (skip), or None if nothing was said in time."""
    ep = Endpointer(noise_floor_db(before), silence_s=silence_s)
    frames = list(before[-round(0.3 / FRAME_S):])  # a word begun a moment early
    offset = len(frames)
    while ep.state in ("waiting", "speech"):
        key = keys.poll()
        if key == "esc":
            return "esc"
        if key == "enter" and ep.n * FRAME_S > 0.3:
            ep.stop()
            break
        frame = source.read()
        if frame is None:
            continue
        frames.append(frame)
        ep.feed(frame)
        if ep.n % 3 == 0:
            show("\r  " + meter(ep.levels[-1], ep.threshold), end="")
    show("\r" + " " * 60 + "\r", end="")
    if ep.state != "done":
        return None
    return cut(frames, ep.start + offset, ep.end + offset)


def drain(source, ring: list[np.ndarray], keep: int) -> None:
    """Move the frames that have arrived into the ring, keeping the last `keep`."""
    for _ in range(keep):
        if (frame := source.read(timeout=0)) is None:
            break
        ring.append(frame)
    del ring[:-keep]


def wait_key(source, keys, ring: list[np.ndarray], auto: float | None) -> str:
    """The next key, while keeping the last second of background audio. In auto mode, "enter" by itself."""
    deadline = time.monotonic() + auto if auto is not None else None
    while True:
        drain(source, ring, keep=round(1.5 / FRAME_S))
        if (key := keys.poll()) is not None:
            return key
        if deadline is not None and time.monotonic() >= deadline:
            return "enter"
        time.sleep(0.02)


def run(rows: list[dict], *, set_name: str, speaker: str, out: Path, source, keys, transcribe, show=print,
        auto: float | None = None, retry_skipped: bool = False, retranscribe: bool = False,
        silence_s: float = 0.8) -> Store:
    """Record (or re-transcribe) the rows; `transcribe(wav_bytes, row) -> str`."""
    folder = EVAL / "recordings" / safe_name(set_name) / safe_name(speaker)
    store = Store(out, folder.parent / f"{safe_name(speaker)}.skipped.json")
    keys_in_order = [row_key(r, i) for i, r in enumerate(rows)]
    if retry_skipped:
        store.skipped.clear()

    def finish(i: int, wav_path: Path) -> bool:
        row, key = rows[i], keys_in_order[i]
        try:
            heard = transcribe(wav_path.read_bytes(), row)
        except Exception as e:  # network or quota: the WAV stays, the next run transcribes it
            show(f"  transcription failed ({e}); it will be retried on the next run")
            return False
        show(f"  heard: {heard!r}")
        store.put({**{k: v for k, v in row.items() if k != "text"}, "text": heard, "typed": row["text"],
                   "speaker": speaker, "audio": wav_path.relative_to(EVAL).as_posix(), "key": key,
                   "recorded": datetime.fromtimestamp(wav_path.stat().st_mtime).astimezone()
                   .isoformat(timespec="seconds")}, keys_in_order)
        return True

    if retranscribe:
        for i, key in enumerate(keys_in_order):
            if (wav_path := folder / f"{key}.wav").exists():
                show(f"[{i + 1}/{len(rows)}] {rows[i]['text']}")
                finish(i, wav_path)
        return store

    ring: list[np.ndarray] = []
    i = 0
    while i < len(rows):
        row, key = rows[i], keys_in_order[i]
        wav_path = folder / f"{key}.wav"
        if key in store.rows or key in store.skipped:
            i += 1
            continue
        lang = str(row.get("lang", "")).split("-")[0]
        show(f"\n[{i + 1}/{len(rows)}] ({LANG_NAMES.get(lang, row.get('lang', '?'))})\n\n    {row['text']}\n")
        if wav_path.exists():  # recorded last time, never transcribed
            finish(i, wav_path)
            i += 1
            continue
        show("  Enter: read it aloud   Esc: skip   r: redo the previous one   q: quit"
             + (f"   (starting in {auto:.0f} s)" if auto else ""))
        key_pressed = wait_key(source, keys, ring, auto)
        if key_pressed == "q":
            break
        if key_pressed == "esc":
            store.skip(key, keys_in_order)
            show("  skipped")
            i += 1
            continue
        if key_pressed == "r":
            done_before = [j for j in range(i) if keys_in_order[j] in store.rows or keys_in_order[j] in store.skipped]
            if done_before:
                j = done_before[-1]
                store.skipped.discard(keys_in_order[j])
                store.drop(keys_in_order[j], keys_in_order)
                (folder / f"{keys_in_order[j]}.wav").unlink(missing_ok=True)
                i = j
            continue
        if key_pressed != "enter":
            continue
        show("  Speak now...")
        source.arm(row)
        samples = record_one(source, keys, ring, silence_s, show)
        ring.clear()
        if isinstance(samples, str):  # Esc while recording
            store.skip(key, keys_in_order)
            show("  skipped")
            i += 1
            continue
        if samples is None:
            show("  I didn't hear anything. Let's try that one again.")
            continue
        folder.mkdir(parents=True, exist_ok=True)
        tmp = wav_path.with_suffix(".tmp")
        tmp.write_bytes(to_wav(samples))
        tmp.replace(wav_path)
        show(f"  recorded {len(samples) / RATE:.1f} s")
        finish(i, wav_path)
        i += 1
    done = sum(k in store.rows for k in keys_in_order)
    show(f"\n{done}/{len(rows)} recorded, {len(store.skipped)} skipped -> {out}")
    return store


def gnani_turn(call, *, turn, busy: type[Exception], show=print, attempts: int = 4, pause_s: float = 2.0):
    """call() while holding the Gnani key. The key allows one active request for Prisma and Timbre together
    (measured: Prisma answers 429 while another process synthesises), so hold the shared cross-process lock
    `turn` (saying so when it has to wait; it raises `busy` when it can't be had in time), and retry a 429
    from a user that doesn't take the lock (a live stream)."""
    for attempt in range(attempts):
        try:
            with ExitStack() as held:
                try:
                    held.enter_context(turn(timeout=0.5))
                except busy:
                    show("  waiting for Gnani (another request is using the key)...")
                    held.enter_context(turn(timeout=600.0))
                return call()
        except Exception as e:
            if "429" not in str(e) or attempt == attempts - 1:
                raise
            time.sleep(pause_s * 2 ** attempt)
    raise ValueError("attempts must be at least 1")


def make_transcriber(languages: str, show=print):
    """Prisma as the app uses it: same model, format and recognition hints."""
    from dataclasses import replace

    from bolos.__main__ import APP_HINTS, recognition_hints
    from bolos.config import load_settings
    from bolos.contacts import ContactBook
    from bolos.speech import Prisma, SpeechError, timbre_turn

    settings = load_settings()
    if not settings.has_gnani:
        sys.exit("Transcribing needs GNANI_API_KEY in .env")
    try:
        from bolos.executor.apps import Catalog

        apps = Catalog(ROOT / ".cache" / "apps.json").names()
    except Exception:  # the installed-app list is a nicety; the common apps are hinted anyway
        apps = APP_HINTS
    hints = recognition_hints(ContactBook.load(settings.contacts_path), apps)
    app_languages = settings.stt_languages
    clients: dict[str, Prisma] = {}

    def transcribe(wav: bytes, row: dict) -> str:
        langs = stt_languages(languages, row.get("lang"), app_languages)
        if langs not in clients:
            clients[langs] = Prisma(replace(settings, stt_languages=langs))
        return gnani_turn(lambda: clients[langs].transcribe(wav, keyterms=hints).text, turn=timbre_turn,
                          busy=SpeechError, show=show)

    return transcribe, app_languages


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("src", type=Path, help="commands to read: JSONL with text, or CSV with command")
    p.add_argument("--speaker", default=os.environ.get("USERNAME", "speaker"), help="who is reading")
    p.add_argument("--set", dest="set_name", help="name for the recordings folder (default: the file's name)")
    p.add_argument("--out", type=Path, help="default: eval/<set>_speech_<speaker>.jsonl")
    p.add_argument("--languages", default="app",
                   help='Prisma languages: "app" (as the app, default), "row" (the row\'s language + English) '
                        'or a list like "hi-IN,bn-IN"')
    p.add_argument("--silence", type=float, default=0.8, help="seconds of quiet that end a reading")
    p.add_argument("--auto", type=float, nargs="?", const=1.5, default=None,
                   help="start each reading by itself after this many seconds (default 1.5)")
    p.add_argument("--retry-skipped", action="store_true", help="ask the skipped commands again")
    p.add_argument("--retranscribe", action="store_true", help="transcribe the saved WAVs again; no recording")
    p.add_argument("--device", help="microphone (a sounddevice index or name); default: the system's")
    p.add_argument("--simulate", type=Path,
                   help="no microphone: a JSON file mapping each command's text to a WAV path that is 'heard' "
                        "instead (to check the tool end to end); starts each reading by itself")
    p.add_argument("--limit", type=int, help="only the first N commands")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    rows = load_commands(args.src)[:args.limit]
    if not rows:
        sys.exit(f"No commands in {args.src}")
    set_name = args.set_name or args.src.stem
    out = args.out or EVAL / f"{safe_name(set_name)}_speech_{safe_name(args.speaker)}.jsonl"
    transcribe, app_languages = make_transcriber(args.languages)
    if args.languages == "app":
        heard = {code.split("-")[0] for code in app_languages.split(",")}
        missing = sorted({str(r.get("lang", "")).split("-")[0] for r in rows} - heard - {"", "none"})
        if missing:
            print(f"Note: the app's STT_LANGUAGES ({app_languages}) leave out {', '.join(missing)}; "
                  "use --languages row to transcribe each command in its own language.")

    if args.simulate:
        mapping = json.loads(args.simulate.read_text(encoding="utf-8-sig"))  # PowerShell writes a BOM
        source = SimulatedMic({text: Path(path).read_bytes() for text, path in mapping.items()})
        keys, auto = ScriptedKeys([]), 0.5
    else:
        device = int(args.device) if args.device and args.device.isdigit() else args.device
        source, keys, auto = (None if args.retranscribe else MicSource(device)), ConsoleKeys(), args.auto
    try:
        run(rows, set_name=set_name, speaker=args.speaker, out=out, source=source, keys=keys, transcribe=transcribe,
            auto=auto, retry_skipped=args.retry_skipped, retranscribe=args.retranscribe, silence_s=args.silence)
    except KeyboardInterrupt:
        print("\nStopped; run the same command again to carry on.")
    finally:
        if source is not None:
            source.close()


if __name__ == "__main__":
    main()
