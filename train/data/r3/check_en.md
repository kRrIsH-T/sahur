# Check of r3/en.jsonl (Indian English)

Rows checked: 482 (the file on disk at the start of this pass; an earlier pass had checked a previous 492-row version, so every row was re-read). Rows fixed: 3. Rows deleted: 3. Rows remaining: 479.

Method: every row was read from the text alone, a label was formed independently (language, single vs multi, each step's action and slots per SLOTS.md) and compared with the row. Automated checks also ran: every slot value is a substring of the text and does not cut a word; request words at slot edges; required slots present per action; control values valid; joiners present in text; step order follows text order; exact and near-duplicates (normalised text, difflib ratio > 0.88, same action). The two near-duplicate hits were kept: 131 vs 151 ("open excel and outlook" vs "open excel word and outlook", two vs three steps) and "increase the volume" vs "decrease the volume" (opposite controls).

## Fixed rows

Click targets for text inputs were inconsistent: "name field" -> `name`, "subject field" -> `subject`, "download button" -> `download`, "cart icon" -> `cart`, "details tab" -> `details`, but "search box", "message box", "search bar" kept the container word. Made them follow the majority rule (label only, no widget word).

- `click on the search box and type wireless earbuds under 2000`
  - before: step 1 = click{"target": "search box"}
  - after: step 1 = click{"target": "search"}
  - why: widget word "box" is not part of the label (consistent with "name field" -> `name`)

- `tap on the message box then type running late will join soon`
  - before: step 1 = click{"target": "message box"}
  - after: step 1 = click{"target": "message"}
  - why: same as above

- `click on the search bar type Leo movie review and read the screen`
  - before: step 2 = click{"target": "search bar"} (step 1 of 3)
  - after: step 1 = click{"target": "search"}
  - why: same as above

## Deleted rows

- `open crome and search sbi fd interest rates`
  - label was: multi: open_app{"app": "crome"} || web_search{"query": "sbi fd interest rates"}
  - why: a misspelling cannot come from speech; the app slot "crome" would teach a non-word

- `open micro soft teams`
  - label was: open_app{"app": "micro soft teams"}
  - why: implausible transcript split of "Microsoft" inside a product name; unnatural

- `open stream app`
  - label was: open_app{"app": "stream"}
  - why: "stream" is not a recognisable app name; ambiguous for real speakers (Steam? Streamlabs? a generic word)

## Summary counts

- before: 482 rows ({'multi': 162, 'open_app': 70, 'play_media': 52, 'web_search': 46, 'send_message': 34, 'media_control': 28, 'open_website': 26, 'type_text': 25, 'click': 17, 'read_screen': 8, 'cancel': 7, 'unknown': 7})
- after: 479 rows ({'multi': 161, 'open_app': 68, 'play_media': 52, 'web_search': 46, 'send_message': 34, 'media_control': 28, 'open_website': 26, 'type_text': 25, 'click': 17, 'read_screen': 8, 'cancel': 7, 'unknown': 7})
- language codes: 'en' 419, 'none' 60 after the pass; no wrong language codes found (Hinglish particles such as yaar/da/bhai inside English sentences stay `en`; verb-less app names, bare media controls and bare domains stay `none`).
- fixed: 3 (inconsistent click target boundary)
- deleted: 3 (misspelling 1, unnatural/ambiguous app name 2)
- no wrong actions, wrong step boundaries, missing steps, cut-word slots or slots with request words were found; all slot values were substrings of the text before and after.

## Main error patterns

1. Click targets that keep a widget word (box, bar) while sibling rows drop it (field, button, icon, tab).
2. Non-speech artefacts in app names (misspelling "crome", split "micro soft", the vague "stream").
3. The file is otherwise clean: hard-negative single rows (titles and sentences containing "and", "then", "also", "after that") were all verified as correctly single, and message/typed text that spans a conjunction is labelled as one step.

Judgement calls kept as is: typed shorthand "n" for "and" as a joiner (typed commands are supported), "note pad" / "you tube" / "whats app" style splits (plausible transliteration output), service+name rows without a verb ("YouTube Arijit") as play_media.
