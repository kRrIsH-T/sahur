# "Hey Bol OS" wake word

A small keyword-spotting model trained from scratch on synthetic speech (Gnani Timbre voices), exported to
numpy and run by `bolos/wake.py` on the microphone stream.

## Pipeline

```
uv run python train/wake/generate.py --dry-run   # plan: what would be synthesised, how many requests
uv run python train/wake/generate.py             # synthesise what is missing (cached, resumable)
uv run python train/wake/corpus.py               # pack clips per split into work/corpus_<split>.npy/.json
uv run python train/wake/train.py                # train; writes work/wake_candidate.npz
uv run python train/wake/evaluate.py --install   # streaming eval, threshold choice, install + test fixtures
```

| file | what it does |
| --- | --- |
| `texts.py` | phrase spellings per language, near misses, ordinary sentences in all 10 languages |
| `tts.py` | cached Timbre synthesis (holds `bolos.speech.timbre_turn` for every request) |
| `generate.py` | the corpus plan: voices, splits, speeds, texts |
| `corpus.py` | packs clips and finds where the speech starts/ends |
| `augment.py` | speed, level, synthetic reverb, mic colouring, noise (white/pink/brown/fan/babble/music), clipping |
| `windows.py` | labelled 1.6 s training windows (positive, partial phrase, after phrase, near miss, speech, noise) |
| `train.py` | torch training (CPU or GPU), hard-negative mining, export with batch norm folded |
| `evaluate.py` | runs the real streaming runtime on long held-out streams; recall, false accepts/hour, latency |

`audio/` holds every synthesised clip (16 kHz WAV, file name = hash of model, voice, language, text, speed)
and `manifest.jsonl`; a clip is never requested twice. `work/` holds packed corpora, candidates and
`report.json`. `fixtures/` holds a few held-out clips for `tests/test_wake.py`.

## Data

The existing Timbre cache contains all 2,103 planned clips. Repacking it on 6 November 2026 produced:

| Split | Clips | Distinct voices | Positive | Near miss | Ordinary speech | Total audio |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Train | 1,109 | 35 | 343 | 246 | 520 | 1.720 h |
| Validation | 281 | 7 | 79 | 82 | 120 | 0.395 h |
| Test | 713 | 13 | 129 | 164 | 420 | 1.312 h |

Voices are disjoint across splits. Every split covers the ten supported languages. These are synthetic
voices, not recordings of real people. The old packed test corpus omitted its 420 ordinary speech clips;
the repack includes them, so context and false-trigger evaluation can run without falling back to training
speech. No new synthesis requests were made during this continuation.

## Model

The CNN has 125,441 parameters. No trained wake model was installed at handoff. The original 32 epoch
training plan was stopped after its first epoch because measured CPU throughput implied about an hour.
The current bounded experiment uses 8 epochs, 16,000 fresh augmented windows per epoch, seed 0, 8 data
workers and 3 PyTorch CPU threads. Its candidate and log are kept separately in `work/`:

- `wake_candidate_20261106_experiment.npz`
- `train_20261106_experiment.log`

Checkpoint selection uses validation windows only. Streaming threshold selection uses validation voices
only. The held-out test stream is scored once at that threshold. `evaluate.py --install` now requires both
validation and test recall of at least 0.80 and observed false accepts of at most 0.50 per hour; a failed
gate preserves the installed model and fixtures. These are empirical gates, not a statistical guarantee.

## Results (synthetic benchmark, held-out voices)

The bounded experiment is in progress; there is no final held-out score yet. The app keeps wake detection
off unless explicitly enabled with `--wake-word` or `ENABLE_WAKE_WORD=1`, and remains usable by tapping or
holding the hotkey when no trained model is installed. See `work/report.json` after streaming evaluation.

A CPU check of the random reference architecture processed 10 seconds of synthetic audio in 0.375 CPU
seconds on this laptop. This measures computational cost only; random weights have no detection-quality
claim. The final trained candidate needs its own CPU and held-out evaluation.

These numbers are for synthetic Timbre voices put through synthetic rooms, microphones and noise. Real
people on real laptop microphones still need checking: run `uv run python -m bolos.wake live`, say the
phrase in a few ways and from a few distances, and leave it running during normal conversation and
videos to count false triggers. `uv run python -m bolos.wake enroll` lowers the threshold for one voice
when the default is too strict for it (never below the level that gives 1 false accept/hour on the
held-out set).
