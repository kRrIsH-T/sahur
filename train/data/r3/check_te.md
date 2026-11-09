# Check of train/data/r3/te.jsonl (Telugu, romanised plus Telugu script)

Method: every row was read from the text alone first (language, single vs multi, each step's action and slots
per SLOTS.md, step boundary at the conjunction or verb-participle join), then compared with the stored label.
Automated checks on top: every slot value is a substring of the text, slot values start and end on a word
boundary (except the Telugu dative suffix on a contact, see below), no request words at the edges of
query/site/app/contact/target slots, every joiner occurs in the text, every media_control step has a control,
no multi step with an empty required slot, no duplicates or near duplicates (difflib ratio > 0.85).

## Summary counts

| item | count |
|---|---|
| rows checked | 427 |
| rows fixed (label changed) | 0 |
| rows deleted | 1 |
| near duplicates removed | 0 (none found; highest ratio between different-meaning rows was 0.85, simple templates such as "X open chey") |
| rows after cleanup | 426 |

Composition before: 161 multi (2 to 3 steps), 50 web_search, 41 send_message, 38 play_media, 24 type_text,
23 open_app, 22 media_control, 19 click, 15 open_website, 15 unknown, 10 cancel, 9 read_screen.
Language codes: 419 te, 8 none (all single language-neutral words such as "spotify", "Notepad", "pause", "next",
"cancel", "github.com"); all judged correct.

## Changes

### Deleted (1)

| row text | before | why |
|---|---|---|
| Zoom open chey tarvata join meeda click chey aa tarvata mute chey | multi: open_app Zoom, click join, media_control mute | Genuinely ambiguous: after joining a Zoom call "mute chey" means mute the microphone inside Zoom (a click on the Mute button), not system media mute. The label media_control/mute would teach the wrong behaviour; no other label is clearly right either. |

### Fixed (0)

No label was clearly wrong, so none was changed.

## Things examined and deliberately kept

- Telugu contacts with the dative suffix glued on in Telugu script (నాన్నకి -> `నాన్న`, అమ్మకి -> `అమ్మ`,
  రవికి -> `రవి`, సాయికి -> `సాయి`; rows with these contacts: 9 rows). The slot is a substring, it drops the
  "ki" suffix as SLOTS.md requires ("without ko/ku/kku/ge/ki"), so this is the intended behaviour, not a cut word.
- "tarvata paata pettu" (media_control next) and "paata pettu" (play_media, no query): both natural, the first
  is a useful contrast row for the conjunction word "tarvata" in a single command.
- Row "laptop open chesi mail check cheyyi" inside a message and the other "X chesi Y" messages / typed text:
  correctly single send_message / type_text (conjunction inside the dictated text, per F4).
- Titles with "and" / "mariyu" / "aandu" (Tom and Jerry, Romeo and Juliet, Fast and Furious, Simon and
  Garfunkel, Save and Continue, Questions and Answers ...) are correctly single commands.
- "inka" used as "and also" joiner, variants of "tarvata" (taruvata, tarvatha, aatarvata, tarwata) as joiners:
  plausible real speech, kept. Joiners all occur in the text.
- "Sticky Notes kavali open chey", "Teams open cheyyi meeting undi": slightly loose word order but natural
  spoken Telugu, labels right.
- flipkart / cricbuzz / gmail "open chey" labelled open_website: consistent with SLOTS.md (sites that are
  only websites).
- Gold-standard conventions checked for consistency: trailing "songs/paatalu/video" dropped after a name,
  kept when it is the whole description ("old telugu songs", "devotional songs", "kids rhymes").

## Main error patterns found

The file was already clean: there was no wrong action, no wrong step boundary, no slot cutting a word, no
request word inside a slot, no wrong language code and no missing step. The only issue type was:
1. Context-dependent ambiguity in a multi-step row (a bare "mute" after a call app step).

## Final validity check

Re-run after rewriting: every line parses as JSON, every slot value of every step (and top level) is a
substring of the text, joiners occur in the text, 426 rows remain (see below).
