# Independent check of kn.jsonl (Kannada, Kanglish plus Kannada script)

Method: every row was read by text alone, the label was decided independently (language, single vs multi,
each step's action and slots per SLOTS.md), then compared with the row. eval/ was not opened. Scripted checks
also ran: slot values are substrings on token boundaries, joiners occur in the text, step slot order follows
text order, joiner count equals steps minus one, no exact or near duplicates, script/lang agreement.

## Counts

- Rows checked: 420
- Rows kept unchanged: 409
- Rows fixed: 4
- Rows deleted: 7 (no near-duplicates found; the only near-similar pairs, 178/187 and 323/332 in the original
  numbering, have different actions or opposite meanings and were kept)
- Rows after: 413

Final language counts: kn 405, none 7, en 1. Final action counts: multi 156, web_search 43, play_media 38,
send_message 32, type_text 28, open_app 27, open_website 23, media_control 20, click 18, read_screen 10,
unknown 10, cancel 8.

## Fixed rows (original 0-based row number in the pre-check file)

1. Row 71, text `ಫ್ಲಿಪ್ಕಾರ್ಟ್ ವೆಬ್ಸೈಟ್ ತೆರೆದು ಕಾರ್ಟ್ ಮೇಲೆ ಕ್ಲಿಕ್ ಮಾಡಿ`
   - Before: step 2 click target `ಕಾರ್ಟ್`. After: text rewritten to `... ತೆರೆದು ಲಾಗಿನ್ ಮೇಲೆ ಕ್ಲಿಕ್ ಮಾಡಿ`, target `ಲಾಗಿನ್`.
   - Why: `ಕಾರ್ಟ್` also occurs inside `ಫ್ಲಿಪ್ಕಾರ್ಟ್` (the site name), so a first-occurrence span search would label
     the middle of a word (a cut-word slot). Rewrote the text so the target is unambiguous.
2. Row 163, text `ರಾಮ ಮತ್ತು ಲಕ್ಷ್ಮಣ ಕಥೆ ಹಾಕು`
   - Before: play_media query `ರಾಮ ಮತ್ತು ಲಕ್ಷ್ಮಣ`. After: query `ರಾಮ ಮತ್ತು ಲಕ್ಷ್ಮಣ ಕಥೆ`.
   - Why: `ಕಥೆ` (story) is not a generic "songs/video" word, it describes what to play; the twin row
     `ಶಿವ ಮತ್ತು ಪಾರ್ವತಿ ಕಥೆ ಹಾಕು` already keeps it. Made the two consistent.
3. Row 217, text `ammanu mattu magalu serial latest episode haaku`
   - Before: play_media query `ammanu mattu magalu`. After: query `ammanu mattu magalu serial latest episode`.
   - Why: "serial latest episode" says what to play (the comparable row `comedy khiladigalu latest episode`
     keeps it); only a generic songs/video word may be dropped.
4. Row 270, text `who sang varaha roopam`
   - Before: lang `kn`. After: lang `en`.
   - Why: the text is pure English with no Kannada word; a language head must not learn "kn" for it.

## Deleted rows

1. Row 85 `video nillisi ee actor hesaru enu hudku` (media_control pause + web_search `ee actor hesaru enu`).
   Why: "what is this actor's name" depends on what is on screen; the query is not searchable and the real
   intent (read_screen / identify) is ambiguous.
2. Row 111 `ಸ್ಕ್ರೀನ್ ಓದು ಮತ್ತು ಈ ಪದದ ಅರ್ಥ ಹುಡುಕು` (read_screen + web_search `ಈ ಪದದ ಅರ್ಥ`).
   Why: "meaning of this word" has no word; the second step is unsearchable and ambiguous.
3. Row 114 `varaha roopam lyrics hudku mattu adanna youtube alli haaku` (web_search + play_media app only).
   Why: step 2 plays "that" (adanna) and relies on the previous step's content, which a step-local labelling
   cannot express; no query slot is possible.
4. Row 126 `chrome ondsala open maadi how to apply ration card karnataka anta hudku`.
   Why: "ondsala" (a garbled "ondu sala") is not something real speakers say; joiner `ondsala open maadi`
   is unnatural.
5. Row 200 `matte matte haadu haaku` (play_media query `matte matte`).
   Why: "matte matte" is "again and again", not a title; ambiguous between a play-again request and a song.
6. Row 203 `ninna nodi amele haadu haaku` (play_media query `ninna nodi amele`).
   Why: reads as "after seeing you, play a song" (a sequence), not a title; the query is a guess and the row
   teaches a nonsense title that contains a conjunction word.
7. Row 297 `gaana haaku` (play_media, no slots).
   Why: `gaana` is Hindi for song and also the Gaana app; Kannada speakers say haadu. Ambiguous between
   play_media with no query and play_media/open of the app (row 33 uses gaana as an app).

## Main error patterns

- The file was already in good shape (about 3.6 percent touched). Slots were all exact substrings on token
  boundaries, request words were excluded everywhere (the REQWORD hits are all legitimate: inside dictated or
  message text, or the `ಹುಡುಕು` search-box label), step order matched text order, joiners were present.
- Slot boundary: one case where the slot string also occurs inside another word (row 71), a labelling hazard.
- Inconsistent trailing-word dropping in play_media queries (rows 163, 217): descriptive words were dropped
  while sibling rows kept them.
- Wrong language code for English-only text (row 270).
- Unnatural or ambiguous speech (7 rows): references to on-screen content ("this actor", "this word"),
  steps that depend on the previous step's content ("adanna"), garbled fillers ("ondsala"), non-title
  phrases made into queries, and the gaana song/app ambiguity.
- Things reviewed and deliberately kept: dictated or message text that contains conjunctions or verbs
  (rows 176-197, 218, 219: hard negatives for splitting), titles with "and"/"mattu" (160-175, 198-217),
  gmail/irctc style names as open_website, "bgm"/"movie"/"cartoon" dropped from play_media queries as generic
  trailing words, participle joiners such as `open maadi` and `open maadkondu`, incomplete rows 353 and 354.

## Validity re-check after rewrite

Python check run on the rewritten file: every line parses as JSON, every slot value of every step is a
substring of the text, every joiner occurs in the text: OK, 413 rows.
