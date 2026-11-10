# Development status — 6 November 2026

This document summarizes the project implementation and its known verification limits.
The pasted conversation described planned work; it was not a test report.
Completed checks and remaining work are separated below. Update this file
when integration or model artifacts change.

## Project map

Bol OS is a Windows voice assistant for natural, code mixed commands in Hindi,
English, Kannada, Tamil, Telugu, Marathi, Malayalam, Bengali, Gujarati and
Punjabi. It targets people who find typing or computer interfaces difficult.

The main runtime path is:

1. `bolos/__main__.py` loads configuration and wires voice input, local parsing,
   System 2, Windows execution, preferences and the status overlay.
2. `bolos/listen.py`, `audio.py`, `speech.py`, `wake.py` and `tts_cache.py` provide
   microphone capture, endpointing, Gnani Prisma transcription, wake detection
   and spoken replies. Gnani receives speech audio for recognition and reply
   text for synthesis; these components are not an offline speech stack.
3. `bolos/decide.py` combines the local action model and slot tagger.
   `bolos/fast/` runs their trained weights in NumPy. Confident, complete
   commands can proceed locally; `bolos/llm.py` supplies System 2 fallback.
4. `bolos/pipeline.py` is the policy boundary for understanding, confirmation,
   execution, replies and learning. `bolos/compound.py` validates and connects
   multiple steps, including carrying a browser into a later search.
5. `bolos/actions.py` defines the validated action schema. `bolos/executor/`
   implements Windows apps, websites, UI Automation, media and WhatsApp.
6. `bolos/preferences.py`, `history.py`, `responses.py` and `overlay.py`
   implement correction memory, command events, multilingual wording and
   visible progress. Form answers must remain outside histories, learned
   habits and remote LLM prompts.

`train/` contains labelled data checks, dataset construction, action/slot
training and calibration. `train/wake/` separately creates and trains the wake
detector. `eval/` contains existing regression sets, scoring and real voice
recording tools. `docs/demo/` is the local fake data form test environment.

## Initial handoff audit

The initial snapshot contained substantial implementations of hands free
listening, streaming speech, compound planning, corrections/history and the
overlay. The public entry point and assistant still used the older single turn
push to talk path, so the new modules were not yet an integrated application.
The form assistant itself was absent; only draft form intent rows and a
partial demo page existed.

The installed `system1.npz` initially contained 11 original actions and no
language head or `multi` class. `dataset.jsonl` initially contained 12,690 rows
with no language labels or compound commands. `wake.npz` was absent, and the
wake README still had result placeholders. These facts must be rechecked
after training; adding runtime interfaces does not update trained weights.

The workspace had no Git repository. A pre hands free backup exists at
a local pre-hands-free backup.

## Verified data and schema checks

The continuation connects the streaming `Listener` to the public entry point,
with hands free confirmations, cached replies, private form answer capture
and local Windows speech for private read backs. The assistant now connects
compound planning/browser carry over, correction retries, language selection,
thinking feedback and command history. Local form intents are intercepted
before legacy parsing/System 2; form content read requests use local guidance.
Runtime wiring and policy tests pass without live service calls. This does
not yet establish physical microphone or browser accessibility behavior.

The training validator now includes `explain_form`, `fill_form`, `check_site`
and `upload_file`, matching the runtime action schema. The first three take no
personal values as slots. `upload_file` accepts an optional copied `target`.
The supported compound limit is consistently two or three steps.

Raw source validation after that change:

| Source | Files | Rows | Invalid rows |
| --- | ---: | ---: | ---: |
| `train/data/slots/` | 26 | 13,259 | 0 |
| `train/data/r3/` | 20 | 5,476 | 0 |

Round 3 includes 1,595 compound commands: 1,346 have two steps and 249 have
three. No four step rows were discarded when aligning the limit. It includes
351 `explain_form`, 350 `fill_form` and 250 `check_site` rows. There are no
`upload_file` examples in these raw files. Round 3 has labels for all ten
languages plus 108 language neutral rows. Older slot rows lack language labels.

`tests/test_training_schema.py` checks runtime/training action and field
consistency, optional upload targets, exact copied upload spans and the
compound limit. Its six tests pass. Ruff passes for that test file and
`train/check_rows.py`. The combined existing fast model tests and new schema
tests pass (59 tests). Raw row validity checks schema and copied spans; it does
not establish linguistic quality or model accuracy.

`tests/test_pipeline_integration.py` adds 21 passing checks of the connected
assistant: actual local splitting and browser carry over, stopping after an
execution failure, rejecting incomplete/unknown/cancel/oversized remote plans
before executing, wait acknowledgement, hands free message confirmation,
private history redaction, correction learning and old history updates,
private reclassification cleanup, script priority and language continuity,
form routing ahead of parsing and remote calls, masked form history/overlay
requests, local form screen explanations and blocking form dialogue compounds.
These tests use fake speech/Windows/LLM boundaries and make no live API calls.
Ruff passes for this integration test file.

Compound safety also covers an older model confidently recognizing only the
first task: when a known complete request precedes an unresolved later task,
the pipeline now resolves the complete plan with System 2 or executes nothing.
Protected query/dictation spans and title conjunctions remain intact. The
existing compound tests plus `tests/test_compound_safety.py` pass (112 tests),
including request/title boundary cases in Latin and native scripts.

## Evaluation boundaries

Existing frozen v3 and v5 regression sets are available. The handoff's promised
`eval/commands_v6.jsonl` and speech variant were absent. No v6 score should be
reported until a genuinely separate set has been created and frozen. Do not
relabel or rewrite v5 to make new results look better.

`train/build_dataset.py` removes exact, near character shingle and folded word
set overlap with all evaluation files. Rebuild the dataset after schema changes
and before training. Keep candidate model output separate, measure regression
and abstention behavior, then install a validated candidate.

The README's earlier scores describe synthetic commands and Timbre generated
speech. They do not demonstrate real person accuracy, cross microphone
reliability or physical speaker to microphone behavior.

## Guide mode ("dikhao"), 7 November 2026

`bolos/guide/` adds a `show_how` action, routed locally like the form requests: `intent.py` (requests and the
stop/next/repeat words), `screen.py` (screenshot of the user's monitor, numbered UI Automation marks with the
taskbar's own share, focused text box, screen fingerprint), `planner.py` (System 2 prompt, schema, validation),
`pointer.py` (per-pixel-alpha ring window on its own thread) and `session.py` (the loop). Codex now accepts
images (`codex exec -i`), and the OpenAI-compatible backend sends them as `image_url` parts.

Verified on this PC (2560x1440 at 150 %, Codex gpt-6-luna low effort), with a harness playing the user because
Deskflow held the cursor on another computer, which makes Windows refuse synthetic mouse input. The harness
pressed each ringed element through UI Automation and reported the click where it happened. Real screenshots,
marks, Codex plans, pointer window and status overlay were used.

- "Settings mein Bluetooth wala page kaise kholte hain, dikhao" from a closed Settings: taskbar Settings icon,
  hamburger (replanned once because Settings opened in its narrow layout), "Bluetooth & devices"; done, 28.5 s,
  2 Codex calls of 5-10 s.
- "Settings mein wallpaper wala Background page kaise kholte hain, dikhao" through the real Assistant (System 1
  language detection hi-IN, local routing, real overlay): 3 steps, 1 Codex call, 16.5 s.
- Live runs found and fixed: a predicted "Search" step pointing at the taskbar's Windows Search instead of the
  Settings search box; a click into a text box finishing a typing step; a ring on a 956x9 sliver of a card
  scrolled out of view; replanning before a just-launched app had shown its window.

Not verified yet: the 15 ms mouse-button polling with a real hand on the mouse (no physical input was possible
during the runs), voice steering ("ho gaya", "ruko") through the microphone, and multi-monitor setups. The ring
is hidden under the Start menu and the Windows Search panel (checked live: only UIAccess apps can draw there).

## Guide mode fixes from the first real use, 8 November 2026

Every attempt in `history.jsonl` from 00:37-00:40 failed, for three reasons, all fixed with tests built from
those transcripts: the show/teach verb arrived misspelt (shikha, bikhau, dikaho, dekhako); plain "Bluetooth
kaise on karein" had no show verb; and the one recognised request ended as `guide_form`, because
`FormAssistant.has_form()` counts any text box (the Codex app's message box) as a form. The same check turned
every "screen par kya hai" into a form explanation.

- The guide's gate is now `shows_personal_details()` (a form with something typed in), read with
  `fields.scan(options=False)` so no dropdown is opened; read-screen explains a form only when
  `ready_to_fill()` finds three or more text boxes.
- Read-screen sends a screenshot with the window text (`describe_screen(..., image=...)`), deleted after use.
- The guide opens official sites (`planner.official`), knows the official starting pages, hands over to
  `FormAssistant.fill` when an application goal reaches its form (`guide_form_ready`), and says its opening
  line while the first plan is made.
- Form read-back falls back to a local Indian-English voice (OneCore voices are now used) instead of stopping.
- Checks are 0.2-0.6 s UI Automation scans, run only before screenshots and, for application goals, between
  steps.

Verified offline: 1760+ tests, and the real System 1 parser routes all seven failed transcripts to
`show_how`, "screen par kya h" to `read_screen`, and ordinary commands unchanged. Live on the official site
(through the in-app browser, read-only): the route tinpan.proteantech.in, PAN Apply Now, Apply Online, Apply now
to the Online PAN application form, which has Application Type and Category dropdowns, First/Middle/Last Name,
Date of Birth, Email, Mobile, reCAPTCHA and Submit. Not yet verified: the whole spoken PAN lesson and form fill
on the user's Chrome (the user was using the PC), and whether the form assistant reads every field of that
page (dropdown options, the date field's format).

### Live PAN rehearsal, 8 November 2026 (with the user's go-ahead)

Six runs of "PAN card ke liye apply karna hai" through the real Assistant, Codex plans, overlay and form
assistant, with a harness playing the user (Deskflow again blocked synthetic mouse input, so it pressed each
ringed element through UI Automation). The final run, from a minimized desktop, reached the form in 36 s:
opening line at 1.5 s; Bol OS opened https://tinpan.proteantech.in/; rings on Reject All (cookie banner),
Continue (the site's Important Notice pop-up) and Apply now; the Online PAN application form was detected and
handed to the form session, which spoke its intro and stopped because the harness cannot answer privately.
Nothing was typed or submitted on the site.

Fixed from these runs: Codex asking the user to open Chrome first and making "wait for the page" steps (prompt,
and a page-load wait that needs the page's links listed and stable); a whole page or tab as a target (now
rejected); `user_window()` returning a minimized foreground window; the status card covering the banner button
the ring pointed at (it now moves to another corner); and, in the form scanner, on that page: dropdowns labelled
"----Please Select------" (now the label text before them), the consent checkbox labelled "Mobile Number" (now
its own sentence, and treated as the user's to tick), reCAPTCHA offered as a checkbox (now never filled), and
required stars in separate spans (now detected: type, category, surname, date of birth, email, mobile).

Still to verify with a person: the spoken form session on that page (dropdown choices, date entry, English
read-back), with invented details and without Submit.

## Dashboard, 8 November 2026

`bolos/dashboard/` serves a local page (127.0.0.1:8765, Host-checked, POSTs need JSON plus an `X-Bolos`
header) from the running app; `bolos/user_settings.py` keeps the changes in `settings.json` (over `.env`), and
`bolos/controls.py` applies them live: the listener can now switch microphone (`set_input_device`), hotkey
(`set_hotkey`, the old one stays if the new one is taken) and settings (`update_settings`), the player and
SAPI read-back follow the chosen speaker, the overlay can hide, show and change corner, the history can be
switched off, and screenshots to System 2 can be refused. History entries now record `ok`.

Checked live against a separate test instance (its own hotkey and settings file, next to the user's running
Bol OS): hide/show, corner, hotkey change and a refused hotkey already held by another program, switching to the
laptop microphone (the meter followed it), the test sentence (Timbre) and the private voice, the restart banner,
reset, and the standalone page stepping aside when Bol OS starts. Found and fixed on the way, older than the
dashboard: the status card started in the top-left corner, because Tk resets a position given before the
window is mapped; it stayed there until its size first changed.

## The pointer beside the cursor, 8 November 2026

`bolos/buddy.py` ports the open-source Clicky's cursor companion (OverlayWindow.swift, MIT) to Windows: one
small layered, click-through, never-focused window that trails the cursor on a spring at a fixed offset; four
bars driven by the microphone level while listening and an arc while thinking (cross-faded); the eased
quadratic-bezier flight to a target that turns to face its direction and swells at the apex; a label bubble
that pops in and types itself (GDI text, so Indic scripts are shaped), holds 3 s, fades and flies back, the
return cancelled by a 100-point mouse move; transient mode when switched off; hiding with the system cursor;
and a first-run hello in the reply language. `guide.pointer.Pointer(buddy)` lets the buddy fly and shows the
ring as it lands, for answers, guide steps and take-over steps alike. Dashboard: "Show the pointer beside the
cursor" (live) and "Show the pointer's hello". The look is Bol OS's own (orange, its bar mark, dark text on
an orange bubble); none of Clicky's character, wording, music or video was copied.

Checked live on this PC (2560x1440 at 150 %): the hello in Hindi, bars while listening, the arc while
thinking, the flight, the bubble at the target, the return. 20 new tests in `tests/test_buddy.py`.

## Computer use verification (8 November 2026)

The live Windows Calculator run used real Windows accessibility controls, a screenshot cropped to Calculator,
Codex vision and the Bol OS ring. The test driver clicked the highlighted navigation menu and then Scientific.
Bol OS advanced to the second ring and reported completion after Calculator switched modes. The screen read
identified Scientific mode and the number on the display. A separate click command left the display alone after
“no” and entered 7 after “yes”. The dialogue harness injected transcripts and captured replies as text.

The browser tests use local fake KYC forms in disposable profiles. They wait for the page's accessibility tree
and fail if expected fields are missing. They check exact values after entry and ensure the declaration stays
unticked and Submit stays untouched when there are skipped or manual fields. All 20 expected values matched
in each of seven Chrome runs and two Edge runs. A separate three-field loopback test verified that declining
Submit left the form open and agreeing reached the local confirmation page. The standard suite passed 1,811
tests with 6 opt-in or wake-model skips. The two opt-in browser tests also passed when enabled separately.

Manual acceptance checks

1. With a real microphone and speaker, ask “What am I looking at?” and “Screen par kya hai?” in front of a
   nonprivate app. Check the app name, visible content, reply language, response time and status card. Try a
   visual-only control with screenshots enabled and repeat with screenshots disabled.
2. Say “Show me how to switch Calculator to Scientific mode.” Follow each ring with a physical mouse or touchpad.
   Ask for a repeat, go off path, switch windows and say stop. Verify one step per action and that the ring clears
   on stop. Try a scroll step and a right-click step in File Explorer.
3. Move the target app across monitors with different display scaling. Check the ring and the status card at
   the edges of each display, including cases where the target is in a popup. Start and Windows Search can hide
   the ring, so check that the spoken and written instructions are sufficient there.
4. Run a complete push-to-talk form session using the local demo and invented data. Listen to the Windows
   private readback, reject an incorrect value, retry, skip a field and check that Submit stays untouched.
   Try a device switch and shutdown. Test wake-word activation if the trained model is installed.

## Work still requiring evidence

- Whole application tests through the public entry point after all new modules
  are connected, including clean shutdown and follow up listening.
- Updated action/language/compound model training and regression results.
- A trained wake artifact, held out false trigger measurement, and real voice
  microphone checks. The app must remain usable without the wake model.
- A complete fake data form session through real browser/UI Automation,
  including required fields, validation errors, cancellation, confirmation,
  blocked secrets and explicit final submission.
- Review of all ten languages' prompts by speakers of those languages. Generated
  translations and structural tests are not a native language review.
- Upload choice/consent, encrypted optional profile storage and deletion if
  implemented; do not claim these are ready based on intent routing alone.

The source graph under `graphify-out/` is supporting navigation, not a runtime
verification. It excludes private settings, contacts, recordings, generated
datasets, trained models and caches.

## Form filling like dictation, 9 November 2026

The form session (`bolos/forms/session.py`) no longer reads each answer back and waits for "haan" before
typing it. Each field is scrolled into view and focused (`windows.focus`), ringed with a snug variant of the
guide's ring (`Sprite(snug=True)`, which stays off the label above the box) while the buddy flies to it with
the field's name, and asked by concept in the reply language (`q_name`, `q_dob`, `q_state`... in prompts.py;
other fields use "{label} बताइए।"). The answer is entered as soon as it is heard and the session moves on.
"Galat" clears the previous field and asks it again; the value on screen is the confirmation, so values are
never spoken and the local read-back voice (`bolos/local_voice.py`, the dashboard's "Play the private voice"
button and its health check) is gone. Submit still needs its own clear yes. The consent question is asked
once per PC (`.bolos-form-consent` beside the settings file), then a one-line how-to. A saved profile answer
is put in the box and kept with "haan". One silence asks again; a second one stops.

`bolos/forms/indian.py` holds common Indian first names and surnames, the 36 states and union territories in
English, Hindi and their own scripts with spoken short forms, and about 230 cities with colloquial names. They
become per-answer Prisma keyterms (`Listener.private(hints)` swaps them in for one turn) and drive the clean-up
of names and places. Dropdown options in any Indic script are romanised through the shared ISCII layout and
compared loosely with the transliterated answer; Hindi number words 1-99 work in dates.

Two pre-existing flakes in the live browser run were fixed on the way: a field could be skipped as "already
filled" because the first scan reads dropdown values while opening each list (skipping now uses a scan that
opens nothing), and a dropdown pick is retried once on the freshly found control. `eval/forms_e2e.py` now
follows the ring to know the field, says "galat" when the box shows a wrong value, and prints UTF-8 on a
cp1252 console. Results in Chrome: 20/20 in hi, ta, kn, te, mr, bn and en; Hindi 18 of 20 on the first answer
(was 13).



## Questions, follow-ups and conversation memory, 11 November 2026

Until now every question became a browser search, and nothing remembered the command before. The history
showed what that cost: "Narendra Modi kaun hai?" opened Google, "Are you code expert?" tried to open an app
called "Are you code", "mere liye sau shabd bolo" and "WSL install karein" were "not understood", and "Phir se
dikhao" after a lesson resumed the music.

- `bolos/actions.py` has an `answer` action (the words to say). Only System 2 chooses it, so it is not in
  `ACTION_DESCRIPTIONS`, is never training data and is never a habit.
- `bolos/conversation.py` keeps the last four turns for three minutes, in memory only, and recognises a
  command that points back at them (pronouns in the ten languages), a request to search, and live facts.
  Messages, dictated text, forms, files and screen read-outs are never added.
- `LLM.converse` (`bolos/llm.py`) is one System 2 call that sees the conversation, the date and time, and the
  reply language. It returns 1-3 steps (an answer alone or last) and the language it heard. `resolve`,
  `resolve_steps`, `fill` and `correct` are unchanged, so the saved System 2 answers behind the README's
  scores still match their prompts.
- `bolos/pipeline.py` asks it, once per command, when the words point back at the conversation, for a
  question System 1 would search, for chatter System 1 drops as unknown, for "play" with nothing clear to
  play, for an app this PC does not have (the installed names go along, so "Cidian" becomes Obsidian), and
  wherever `resolve_steps` was asked before. When it has nothing usable, what System 1 is sure of still
  runs. Where System 1 was sure of something else, and for a bare "haan" to an offer, only answers, opening,
  searching and playing are accepted: never typing, a click or a message. A follow-up is never a habit.
- Reply language: the script decides; otherwise System 2 reads the words and the local detector's guess is
  only its fallback.

Verified: 2,314 tests pass (85 new in `tests/test_conversation.py`), ruff is clean. Live, through the real
Assistant, System 1 models, app catalogue and the Claude CLI (haiku, low effort), with a runner that records
instead of acting: about 45 turns in Hindi, English, Kannada, Tamil, Telugu and Marathi, covering answers,
continued questions, "search that", "send that to Raju" (read back first), "type that", "play it on YouTube",
"haan" to an offer, small talk, requests Sahur cannot do, and plain commands, which stayed local and instant.
Answers took 3.6-7 s. The real entry point (`python -m bolos --text --no-speech`) answered a question and its
follow-up and logged them without learning a habit. Timbre synthesised a 751-character answer (59 s of audio)
over REST and SSE, first audio after 0.44 s.

Not verified: answers spoken through the speaker and heard through the microphone in one session, and the
downstream actions on the real desktop (the live runs recorded them instead of opening anything).

Known weak spot: before System 2 has read the words, "one moment" is said in the language the small local
detector guesses, which for some Hinglish sentences is Gujarati or Punjabi ("brightness kam karo", "haan
search kar do"). The answer that follows is in the right language. The fix is a better language head, not a
rule here.
