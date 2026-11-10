# Sahur

Control Windows by speaking naturally in Indian languages, including code-mixed speech.

Sahur helps people who find typing or navigating menus difficult. Press **Ctrl+Space** and speak in your own
words to open an app, learn a task step by step, or fill in a form one field at a time.

[Read the one-page solution pitch](docs/SOLUTION_PITCH.md)

![Sahur desktop widget cycling through its hop, listening and reply animations](docs/sahur-widget.gif)

> "Raju ko WhatsApp karo ki 5 minute lagenge" -> Sahur reads the message back. Say "haan" to open the draft in WhatsApp, then check the name and message and press **Send**.
> "Spotify pe Radiohead chalao" -> Spotify starts Radiohead. "Gaana rok do" pauses it.
> "Lata Mangeshkar ke baare mein batao" -> a search about her, not a song.
> "Maine ek file download ki hai, use kholo" -> Sahur opens the newest file in Downloads.

Built for people who can't type easily: motor or vision impairments, elderly users, low literacy.
Windows Voice Access understands fixed English command words. Sahur understands how people actually talk.

## Development update — 6 November 2026

The public app now connects the streaming listener, automatic follow-up listening, compound commands,
language selection, correction retries, redacted command history and the overlay's decision/timer display.
Tap Ctrl+Space to stop recording automatically after speech, or hold it for push-to-talk.
Wake listening is an explicit opt-in (`--wake-word` or `ENABLE_WAKE_WORD=1`) and needs a separately
validated model; the app continues with the hotkey when that model is absent.

“Explain this form” / “is form mein kya bharna hai” reads accessible field labels and local explanations.
“Help me fill this form” / “form bharne mein madad karo” fills it like dictation: each field in turn gets an
orange ring and the cursor, Sahur asks for it in plain words (“अपना पूरा नाम बताइए।”), and the box is filled
the moment you answer. “Galat” goes back to the previous field, “skip” leaves one, “ruko” stops. Names,
addresses, cities and states are written the way forms want them (see below). The question about online
speech recognition is asked the first time you fill a form by voice. Form answers are never spoken back and do not go through
Codex, command history or learned habits.
Submission requires a separate yes; skipped, protected or unsupported controls prevent automatic submission.
For a click, say the label you see on the screen. Sahur asks for confirmation and looks for one matching
visible control. After an action opens or navigates a window, typing/clicking
requires a new command until the destination can be verified.

Speech input is sent to Gnani. Form answers are heard as private turns (never logged) and are shown in the
box on screen, never read aloud. Session wording covers ten languages; detailed field explanations currently
cover English and Hindi.

Indian names and places (`bolos/forms/indian.py`): while a name field is asked, recognition favours about 250
common first names and 150 surnames; a city or address favours the 36 states and union territories and about
230 cities. Answers are then written as on an ID card: “raamesh kumaar” becomes “Ramesh Kumar”, a name spelled
letter by letter is joined up, “12 mg road dilli” becomes “12 MG Road Delhi”, and a valid spelling is never
swapped for a commoner one (“Dipak” stays “Dipak”). Dropdowns in any Indian script take the spoken answer:
“karnataka” chooses “कर्नाटक”, “kisaan” chooses “किसान”, “ek se paanch lakh” chooses “1 से 5 लाख”. Hindi
number words work in dates (“pandrah august unnis sau pachasi”). The speech model itself is Gnani's and is
not retrained; these lists bias and clean up what it hears.
Uploads and saved encrypted profiles remain unimplemented. Site guidance cannot certify authenticity.
Use fake data while testing; this has not been validated on real KYC submissions.

See [development status](docs/DEVELOPMENT_STATUS.md) for evidence and remaining work,
[local form test instructions](docs/demo/README.md), and [the project map](graphify-out/graph.html).

## How it works

```
tap/hold hotkey -> streaming Prisma v3.0 (Gnani), with REST fallback
            -> System 1 detects the reply language from the transcript
            -> local form dialogue when requested (answers bypass the command parser and Codex)
            -> habits: a command System 2 worked out before is reused instantly
            -> System 1 (our own from-scratch models, on the laptop, ~1 ms):
                 action model: which of 11 actions, calibrated, abstains when unsure
                 slot tagger:  which words are the app, site, song, contact, message, button
            -> System 2 (Codex CLI, gpt-6-luna at low effort; or Gemini), only for what System 1 is unsure of
            -> typed Action (validated, the only thing the executor accepts)
            -> your preferences: the music/video service you usually use, names you taught it
            -> spoken read-back + "haan" before opening a WhatsApp draft
            -> Windows executor (every Start menu app, websites, Spotify, media sessions, UI Automation)
            -> Timbre v3.0 (spoken reply, Gnani)
```

Models never touch the computer directly. Unclear commands do nothing and ask again.

| Action | Example |
|---|---|
| `open_app` | "VS Code khol do", "ಕ್ಯಾಲ್ಕುಲೇಟರ್ ತೆರೆ", "Zoom wala app open karo" (any of the PC's apps) |
| `open_website` | "Flipkart ki website kholo", "open github dot com", "IRCTC site kholo" |
| `web_search` | "Bangalore ka weather batao", "Radiohead kaun hai", "Amazon pe running shoes dhundho" |
| `find_file` | "Budget report file dhoondo", "Open budget report PDF" |
| `open_recent_download` | "Maine ek file download ki hai, use kholo", "Open my latest download" |
| `play_media` | "Spotify pe Radiohead chalao", "YouTube pe Arijit ke gaane", "kuch lo-fi bajao" |
| `media_control` | "gaana rok do", "agla gaana", "awaaz badhao", "phir se chalao" |
| `send_message` | "Mummy ko message bhejo ki main ghar aa raha hoon" |
| `type_text` | "likho ki meeting kal subah das baje hai" |
| `read_screen` | "Ee screen alli enu ide heLu" |
| `click` | "submit button dabao" |
| `show_how` | "Settings mein Bluetooth kaise kholte hain, dikhao", "folder banana sikhao" (guide mode, below) |
| `cancel` | "rehne do" |

### Finding files by voice

Say “Budget report file dhoondo” to select a matching file in File Explorer, or “Budget report PDF kholo”
to open it. Sahur looks in Downloads, Documents and Desktop. If several files match, say more of the name.
For “Maine ek file download ki hai, use kholo,” Sahur uses the newest file in Downloads. If it is still
downloading, Sahur asks you to wait. Documents, pictures and media open in their usual apps. Other file
types are selected in File Explorer for you to open there.

### Guide mode: "dikhao"

Ask “Where is the Export button?” or “What does this button do?” while pointing at the screen. Sahur uses the
foreground app image, accessibility labels and the location of your pointer. When the answer refers to a
visible control, an animated pointer flies to it and leaves a ring. Follow-up questions use up to four recent
answers from the same window for three minutes. Captures are deleted after each request.

For direct control, say “Take over and switch Calculator to Scientific mode.” Sahur highlights the next
control, asks you before clicking it, and looks again to check what changed. This mode works with visible,
named foreground controls. For walkthroughs and controls without accessibility labels, use “show me how.”
Speech input and ordinary spoken replies use Gnani Prisma and Timbre.

Say “PAN ka form bharna h” or ask to fill a named form in any supported language. If a fillable form is already
open, Sahur can start there. Otherwise it opens the issuing portal when it recognizes the site, or opens a
search for the form, then guides you to the fillable page. Sahur uses Gnani to recognise spoken field answers.

The cursor flight adapts the original [Clicky](https://github.com/farzaa/clicky) animation to Windows.
See the [upstream MIT notice](docs/CLICKY_LICENSE.txt).

Ask Sahur to *show* you how ("WhatsApp pe photo kaise bhejte hain, dikhao", "folder banana sikhao", "show me
how to change the wallpaper") and it teaches instead of doing: an orange ring appears around the thing to
click, the step is spoken in your language, and the next step comes when you click inside the ring.
Click Sahur to see the latest step in the details card. Sahur never clicks or types in guide mode.

For “Wi‑Fi hotspot kaise chalu karein” or “wallpaper kaise badle,” Sahur opens the relevant Windows Settings
page and explains what to do next. This also works for Wi‑Fi, Bluetooth and Night light.

- One System 2 call (with a screenshot plus the numbered UI Automation elements on screen) plans the next
  step and predicts the ones after it by their on-screen label; those are then found locally with UI Automation
  after each click, so a 3-step lesson usually needs one or two calls (about 6 s each).
- Say "ho gaya" / "aage" to move on, "phir se" to hear the step again, "ruko" / "bas" to stop. Any other command
  ends the lesson and runs. Typing steps finish when you press Enter or what comes next appears.
- A click somewhere else that changes the screen means you went another way: Sahur looks again and continues
  from there. With no progress it repeats the step once after 25 s and stops after 2 minutes.
- Requests are recognised locally (`bolos/guide/intent.py`) in all ten languages: a teach verb ("sikhao",
  "teach me"), a how-word with a show verb ("kaise ... dikhao"), a how-word with a computer word ("Bluetooth
  kaise on karein"), or applying for a document ("PAN card ke liye apply karna hai"). Show and teach verbs are
  also matched by shape, because Prisma writes what it hears (dikaho, bikhau, dekhako, shikha were all real
  transcripts). "Biryani kaise banate hain" stays a web search, "photos dikhao" and "Bluetooth on karo" stay
  normal commands. On the frozen eval sets, 3 of 600 commands (all web searches) now start a lesson instead.
  `show_how` is not a System 1 class and never becomes training data or a habit.
- Sahur starts talking at once ("theek hai, main screen par dikhati hoon") while the first plan is made.
- The general guide starts from an official site's home page (`https://` on `*.gov.in`, `*.nic.in`, or
  Protean / UTIITSL, the Income Tax Department's PAN agencies). It then shows you which links to follow.
  For cookie banners, it suggests the option that shares the least.

**Applying for a PAN card.** Say “PAN card ke liye apply karna hai” and Sahur opens Protean’s Online PAN
application directly. If the form is already open, Sahur picks up at the next empty field. It highlights each
field, asks once and puts the answer on screen. Say “galat” to go back or “skip” to leave an optional field.
Read the privacy notice, complete the “I’m not a robot” check and submit on the page when you’re ready.

- Guide mode sends an image of the active monitor to System 2 when screenshots are enabled. Close private
  windows first. Detected forms with typed details or secret fields stay with local form guidance. A guide
  for an application can hand over to form filling once the form appears.
- Ask "Screen par kya hai?", "What am I looking at?", or "What does this error on my screen mean?" The
  description uses Windows accessibility text and, when enabled, an image cropped to the foreground app.
  Sahur speaks the answer. Click Sahur to read it. With images off, Sahur uses accessibility text.

## Setup

```bash
uv sync
copy .env.example .env                    # then fill in GNANI_API_KEY
copy contacts.example.json contacts.json  # your contacts, with names in every script you say them
```

System 2 is the Codex CLI when it is installed and signed in (`codex login`); set `LLM_PROVIDER=openai`
and `GEMINI_API_KEY` to use Gemini instead.

If voice setup is incomplete, Sahur opens its settings page so you can check what still needs to be set up
and test your microphone and chime.

## Run

```bash
uv run python -m bolos                    # tap Ctrl+Space for auto-stop, or hold while speaking
uv run python -m bolos --text             # type commands instead (no Gnani key needed)
uv run python -m bolos --text --dry-run   # show what would run, touch nothing, learn nothing
uv run python -m bolos --text --no-speech # terminal replies without speech synthesis
uv run python -m bolos --wake-word        # optional wake listener when a validated artifact is installed
uv run python -m bolos --dashboard        # also open the dashboard in the browser
```

### The Sahur companion

Drag the Tung Tung Tung Sahur widget anywhere on your desktop. Sahur remembers where you drop it.
Sahur animates when you move it, during listening and replies, and when the guide highlights a target.
Click Sahur to open or close the details card with the latest transcript, reply, and command information.

Use **Preview Sahur** on the dashboard to play the hop animation. Turning off **Show the Sahur companion**
makes Sahur appear only during commands. The standalone pointer adapts motion from Clicky (MIT,
`docs/CLICKY_LICENSE.txt`).

### Dashboard

A running Sahur serves a settings and statistics page at http://127.0.0.1:8765/ (printed at start; only
this computer can reach it). `uv run python -m bolos.dashboard` opens it, or serves it on its own while Sahur
is not running, in which case changes are saved for the next start.

- **Before you record:** a live microphone meter, buttons that play a test sentence and the start chime, and
  a checklist for Gnani, System 2, devices, Sahur's details card, and screenshots.
- **Settings that apply at once:** microphone and speaker, chimes, companion visibility, click-to-open details,
  the push-to-talk hotkey (click the box and press the keys), reply language, languages to recognise, the
  Timbre voice, whether System 2 may see screenshots, and whether to keep the command history.
- **Settings read at start** (marked "After restart"): who decides, the System 2 provider, the Codex model and
  effort, the wake word.
- **Statistics** from `history.jsonl`: commands, how many worked, median time, how many were decided on this
  PC, commands per day, who decided and how fast, most used commands, languages, triggers, and the latest
  commands. "Start fresh statistics" renames the old history instead of deleting it.

Changes are saved in `settings.json` and win over `.env`; "Reset all to defaults" goes back to `.env`.

### If Sahur needs help hearing you

- **Try one short request.** Tap the shortcut shown under **Before you speak**, then say “Open Calculator.”
- **Check your microphone.** Speak while watching **Microphone check**. If the bars stay still, reconnect your
  microphone or allow microphone access in Windows, then select **Retry microphone**. You can choose
  **Windows default** under **Sound** if the microphone you picked was unplugged.
- **Check your sound.** Select **Play the chime**. If you cannot hear it, check the speaker under **Sound**
  and turn up the Windows volume.
- **Change the shortcut.** If another app already uses it, select **Shortcut to speak** under **Voice and language**
  and press a different combination.
- **Try the full name.** If Sahur cannot tell which contact you mean, say the person's full name. Give contacts
  distinct names during setup if they share a name.

## What it learns about you

Kept in `preferences.json` next to the app:

- **Your music and video services.** "Radiohead chalao" goes to the service you name most often for
  music (Spotify until you've named one); videos ("Bahubali trailer") are counted separately.
- **Your names for apps and sites.** An app found by a loose match ("visual studio" -> Visual Studio Code)
  is remembered under the name you used.
- **Habits.** A command only System 2 could work out is remembered with its action, so the next time is
  instant and offline. Messages are never remembered; each one goes through the full check.
- **"Galat tha" / "that was wrong"** right after a command forgets what it just learned.
- **"Galat tha, Spotify pe chalao"** retries a single previous command through System 2. Successful,
  non-private corrections become user-taught habits and local training rows. Compound corrections need
  a complete restated command. Form values, messages and dictated text never become correction habits.

`history.jsonl` next to preferences records commands, decisions, outcomes and timing. Message/dictation
text is redacted by default; form answers never enter that path. Dry runs create no history or learning
and make no LLM/TTS calls.

## The System 1 models

The models are trained from scratch and run in NumPy. PyTorch is used for training.

**Language classifier.** The local classifier detects ten languages from Prisma's romanized text before
Sahur replies. Native script or an explicit language code takes priority when available. For unclear or
language-neutral input, Sahur keeps the previous turn's language or starts with the configured reply
language. Rebuild the classifier with `python train/train_language.py`.

**Action model.** fastText-style: hashed word bigrams and character 1-4 grams (plus a spelling-folded copy,
so "kholo/kolo" and "maadu/madu" share features), mean-pooled into a 48-dim vector, with two linear heads:
the action (11) and, for `media_control`, which control (7). Three seeds are merged into one file.

- **Training objective: RLCD**, the objective Laya uses: soft cross-entropy plus a group-relative
  policy-gradient term over noisy logit samples rewarded by a strictly proper score (log + spherical).
  In the first version it matched plain cross-entropy in accuracy (as Laya's own experiments found) but
  was confident on ~5 points more commands, so it needs System 2 less often.
- **Calibration and abstention**: one temperature per head and a confidence threshold per action, fitted
  on a held-out split augmented to look like noisy speech. Thresholds keep the estimated error under 3%
  for typing and clicking and 6% for the rest. Sahur asks before clicking or opening a message draft.
  Below a threshold System 2 decides.

**Slot tagger.** Words are split before the case endings Kannada, Tamil, Telugu, Malayalam, Marathi and
Bengali join to names ("Ravi-ge", "Suresh-ku", "அம்மா-வுக்கு"), so a slot can be just the name. Per token:
a mean of hashed embeddings of the word, its folded spelling, character 2-4 grams, shape and position, plus
an embedding of the chosen action; a bidirectional GRU (96 per direction) reads the sentence and a linear
layer scores BIO tags for six slots (app, site, query, contact, text, target). Decoding only allows valid
BIO sequences and the slots the chosen action takes. Each slot's confidence is the lowest probability of
the tags that make it up, including the tag right after it, so a span cut short or run long counts as
unsure, and a slot found in two pieces is never sure. Thresholds per slot (3% target error for message
text and contacts, 10% for search and song queries, which are cheap to repeat, 5% for the rest) decide what
System 1 fills alone. Values are copied from the transcript exactly, never rewritten.

**Data**: 12,690 commands written and labelled by Claude subagents to one spec
(`train/SLOTS.md`), validated by `train/check_rows.py`: the earlier 7,700 commands with slots added, plus
new ones for any-app opening, websites, information requests about artists and films (the old model
mistook these for "play"), named music services, media controls, minimal pairs between all of these, and
native scripts, and a second round on media-control directions (louder vs softer, pause vs resume) and
exact query boundaries ("Flipkart par Philips ka trimmer dhundhiye" -> "Philips ka trimmer"). Rows that
match a test command exactly, nearly, or as a reworded or respelled copy are removed
(`train/build_dataset.py`). Augmentation: romanised spelling swaps, doubled letters, typos,
dropped and added fillers, a name split in two the way recognition does ("radio head"), Prisma casing.

## Results

**Test set (the fair estimate): `eval/commands_v5.jsonl`**, 210 commands in Marathi, Hindi, Telugu, Kannada,
Tamil and English, written by three personas who saw no training data, labelled (action and every slot) by
three labellers who saw neither the writers' labels nor each other's, kept only where all three agreed (all
210 did; they are the same model family, so this shows the spec is clear rather than how people would
disagree), and frozen before the final models were trained. Its speech version is spoken by Timbre and
transcribed by Prisma exactly as the app does (`eval/make_speech_set.py`).

| Understanding (`DECIDER`) | v5 typed: action | all fields right | v5 speech: action | all fields right | Per command |
|---|---|---|---|---|---|
| Codex alone (`llm`, gpt-6-luna, low effort) | 97.6% | 89.5% | 85.7% | 66.2% | ~8-9 s |
| System 1 alone, best guess (`fast`) | 97.1% | - | 94.3% | - | ~1 ms, offline |
| **System 1 + Codex when unsure (`system1`, default)** | **97.6%** | **96.2%** | **93.3%** | **76.2%** | ~2 ms; Codex on 9% typed, 15% speech |

"All fields right" means the action and every field (app, site, query, contact, message, button) are right,
allowing harmless spelling differences ("Arijit Singh" for "Arijit"); a message with a changed number or an
added "nahi" counts as wrong. For speech, fields whose gold value is written in a different script from the
transcript (gold "एक्सेल", heard "Excel") can't be compared and are left out. Alone, System 1 acts on 91%
(typed) and 87% (speech) of v5 and is right on 98.4% and 97.3% of those; the rest it hands to Codex.

**Test set v3** (`eval/commands_v3.csv`, 180 commands, action labels only; frozen in the first round; three
volume requests relabelled from unknown to media_control when Sahur learned to change the volume):

| | v3 typed | v3 speech |
|---|---|---|
| Laya multilingual, zero-shot | 45.0% | 48.3% |
| First System 1 (9 actions, no slots), best guess | 92.2% | 96.1% |
| **This System 1, best guess** | **96.1%** | **93.9%** |
| **System 1 + Codex when unsure** | **96.7%** | **93.3%** |

**Dev sets** guided data and model choices, so their scores are optimistic: v1 (100 hand-written), v2 (240),
and v4 (210, written like v5 but studied in detail while fixing the slot tagger, so demoted from test to dev).
System 1's best guess: v1 96.0% / 94.0% (typed / speech), v2 98.3% / 91.7%, v4 95.7% / 92.4%.

What the evidence supports: on what people type, System 1 alone matches Codex alone on the action and gets
more fields exactly right; on speech it is clearly better, because it was trained on Prisma-style romanised,
misspelt transcripts. Asking Codex only when System 1 is unsure is the best of both: Codex's coverage of odd
phrasings, System 1's speed and robustness, with Codex asked for about one command in eight. With 210
commands the 95% interval is about +/-3 points near 95%, so 1-2 point differences are noise. All test data is
synthetic or TTS-spoken: real users will be harder, so log and label real transcripts next.

```bash
uv run pytest
uv run python train/build_dataset.py           # validate, de-duplicate, remove anything close to a test command
powershell -File train/sweep_v2.ps1            # 3 action-model seeds, ensemble, slot tagger, install (~35 min)
uv run python eval/evaluate.py                 # System 1 alone on every set
uv run python eval/evaluate.py --system2 cached --sets commands_v5.jsonl commands_v5_speech.jsonl
uv run python eval/make_speech_set.py eval/commands_v5.jsonl eval/commands_v5_speech.jsonl
```

Codex answers used in scoring are saved in `eval/results/system2_answers.json` (keyed by model, effort and
prompt version), so the numbers can be re-checked without new calls.

**Without System 2** (`DECIDER=fast`, or Codex unavailable) the app still does what System 1 is sure of, and
searches or plays with System 1's own query when unsure of it. Messages, typing and clicks are never guessed.

**Codex as System 2** runs `codex exec` with the user's config ignored, a read-only sandbox, an empty working
folder and its tools switched off (shell, browser, computer use, plugins), so text on the screen being read
aloud can't make it read files on the PC.

### Known limitations

- Spotify playback needs the desktop app started with accessibility on; Sahur restarts Spotify once to do
  that. While Windows throttles a hidden or locked screen, starting a song can take 20 s instead of ~8 s.
- WhatsApp needs the desktop app. Sahur opens the draft after you confirm it, then you check the name and
  message and press **Send** in WhatsApp. Say any remaining requests after you finish with the draft.
- Services Sahur can't drive (JioSaavn, Gaana, Netflix, ...) open their search page; the user picks the item.
- Guide mode's ring cannot be drawn over the Start menu or the Windows Search panel: Windows reserves the layer
  above them for signed UIAccess apps. The planner prefers taskbar icons and app windows; when a step is inside
  Start or Search, Sahur speaks it and shows it in Sahur's details card when opened.

## Gnani models used

- **Prisma v3.0** speech to text, with Latin-script output and contact and app names as recognition hints
- **Timbre v3.0** text to speech, one voice across all 10 languages

