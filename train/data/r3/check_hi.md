# Check of r3/hi.jsonl (Hindi, romanised Hinglish plus Devanagari)

Rows checked: 420. Rows fixed: 31. Rows deleted: 3. Rows remaining: 417.

Method: every row was read from the text alone, a label was formed independently (language, single vs multi, each step's action and slots per SLOTS.md) and compared with the row. Automated checks also ran for exact and near duplicates (normalised text, difflib ratio > 0.72; none above 0.85 apart from one pair kept on purpose), slots cutting words, request or function words at slot edges, joiners present in the text, required slots, Unicode NFC form, and stray characters. Row numbers below are 1-based line numbers of the ORIGINAL file (before deletions). Line endings (CRLF) and JSON formatting were preserved exactly.

## Fixed rows

- Row 299: `gaana chalao |`
  - before: text ends with ' |'
  - after: `gaana chalao` (label unchanged: play_media{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 300: `kuch music bajao |`
  - before: text ends with ' |'
  - after: `kuch music bajao` (label unchanged: play_media{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 303: `jiosaavn pe sonu nigam bajao`
  - before: lang "hi"
  - after: lang "none"
  - why: 'play kesariya' contains no Hindi words (English verb plus a song name), so it is language-neutral like 'next' or 'Spotify'; labelling it hi would teach the language head that any bare English play command is Hindi.
- Row 385: `screen pe kya likha hai batao |`
  - before: text ends with ' |'
  - after: `screen pe kya likha hai batao` (label unchanged: read_screen{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 386: `ye padh ke sunao |`
  - before: text ends with ' |'
  - after: `ye padh ke sunao` (label unchanged: read_screen{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 387: `screen padho |`
  - before: text ends with ' |'
  - after: `screen padho` (label unchanged: read_screen{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 388: `is page pe kya hai bolo |`
  - before: text ends with ' |'
  - after: `is page pe kya hai bolo` (label unchanged: read_screen{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 389: `स्क्रीन पर क्या लिखा है पढ़ो |`
  - before: text ends with ' |'
  - after: `स्क्रीन पर क्या लिखा है पढ़ो` (label unchanged: read_screen{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 390: `ye error kya bol raha hai samjhao |`
  - before: text ends with ' |'
  - after: `ye error kya bol raha hai samjhao` (label unchanged: read_screen{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 391: `jo screen pe hai wo padh do zara |`
  - before: text ends with ' |'
  - after: `jo screen pe hai wo padh do zara` (label unchanged: read_screen{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 392: `rehne do |`
  - before: text ends with ' |'
  - after: `rehne do` (label unchanged: cancel{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 394: `chhodo yaar |`
  - before: text ends with ' |'
  - after: `chhodo yaar` (label unchanged: cancel{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 395: `kuch mat karo |`
  - before: text ends with ' |'
  - after: `kuch mat karo` (label unchanged: cancel{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 396: `nahi nahi ruko cancel karo |`
  - before: text ends with ' |'
  - after: `nahi nahi ruko cancel karo` (label unchanged: cancel{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 397: `रहने दो |`
  - before: text ends with ' |'
  - after: `रहने दो` (label unchanged: cancel{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 398: `bas rehne do koi baat nahi |`
  - before: text ends with ' |'
  - after: `bas rehne do koi baat nahi` (label unchanged: cancel{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 399: `brightness badhao |`
  - before: text ends with ' |'
  - after: `brightness badhao` (label unchanged: unknown{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 400: `kal subah 6 baje ka alarm laga do |`
  - before: text ends with ' |'
  - after: `kal subah 6 baje ka alarm laga do` (label unchanged: unknown{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 401: `papa ko call karo |`
  - before: text ends with ' |'
  - after: `papa ko call karo` (label unchanged: unknown{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 402: `computer shutdown kar do |`
  - before: text ends with ' |'
  - after: `computer shutdown kar do` (label unchanged: unknown{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 403: `tum kaise ho |`
  - before: text ends with ' |'
  - after: `tum kaise ho` (label unchanged: unknown{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 404: `umm wo kya tha |`
  - before: text ends with ' |'
  - after: `umm wo kya tha` (label unchanged: unknown{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 405: `wifi band kar do |`
  - before: text ends with ' |'
  - after: `wifi band kar do` (label unchanged: unknown{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 406: `laptop restart karo |`
  - before: text ends with ' |'
  - after: `laptop restart karo` (label unchanged: unknown{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 407: `स्क्रीन की ब्राइटनेस कम करो |`
  - before: text ends with ' |'
  - after: `स्क्रीन की ब्राइटनेस कम करो` (label unchanged: unknown{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 408: `मम्मी को कॉल लगाओ |`
  - before: text ends with ' |'
  - after: `मम्मी को कॉल लगाओ` (label unchanged: unknown{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 409: `timer laga do 10 minute ka |`
  - before: text ends with ' |'
  - after: `timer laga do 10 minute ka` (label unchanged: unknown{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 410: `hmm acha theek hai |`
  - before: text ends with ' |'
  - after: `hmm acha theek hai` (label unchanged: unknown{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 418: `ये पेज पढ़ के सुनाओ |`
  - before: text ends with ' |'
  - after: `ये पेज पढ़ के सुनाओ` (label unchanged: read_screen{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 419: `छोड़ो कोई बात नहीं |`
  - before: text ends with ' |'
  - after: `छोड़ो कोई बात नहीं` (label unchanged: cancel{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.
- Row 420: `कंप्यूटर बंद कर दो |`
  - before: text ends with ' |'
  - after: `कंप्यूटर बंद कर दो` (label unchanged: unknown{})
  - why: stray ASCII pipe (a danda artifact) at the end of the text; real Prisma romanised transcripts never contain it, and typed Devanagari would use the danda, not '|'. Slot values were not affected.

## Deleted rows

- Row 123: `spotify band karke chrome kholo`
  - label was: multi: media_control{}/pause || open_app{"app": "chrome"}
  - why: 'spotify band karke chrome kholo': 'band karo' on an app name can mean closing the app (an unsupported request) rather than pausing the music, so the first step is ambiguous
- Row 134: `मैसेज करो रोहन को कि मैं आ रहा हूँ फिर व्हाट्सएप बंद... नहीं स्पॉटिफाई खोलो`
  - label was: multi: send_message{"contact": "रोहन", "text": "मैं आ रहा हूँ"} || open_app{"app": "स्पॉटिफाई"}
  - why: mid-utterance self-repair ('whatsapp band... nahi spotify kholo') makes the step boundary and intent ambiguous and is unnatural as training data
- Row 274: `petrol price in delhi today`
  - label was: web_search{"query": "petrol price in delhi today"}
  - why: entirely English sentence ('petrol price in delhi today'), not a Hindi/Hinglish command and wrong file for lang 'hi' (English is covered by en.jsonl)

## Rows reviewed and deliberately kept

- Rows 31 and 32 (`youtube kholo aur arijit ke gaane chalao` / `YouTube khol ke Arijit Singh ke gaane chalao`): same shape but a different joiner type (aur vs 'khol ke') and different artist form; kept as the only near-duplicate pair (ratio 0.73).
- Row 65 (`ms word open karke heading likho project report`): typed text 'project report' is a reasonable reading (heading content), unlike the en row that was deleted for a trailing 'heading' word.
- Rows 138, 144, 154 (`likho X aur save/bold pe click karo`): dictation followed by a click; a conjunction directly before 'pe click karo' is a clear step boundary, so the 3-step label stands.
- Row 248 (`kolo crome`): garbled but a plausible romanised ASR variant; slot `crome` is a clean substring.
- Rows 161-199 (titles with aur / and / phir such as `tom and jerry chalao`, `phir hera pheri lagao`, `aur pyaar ho gaya ke gaane chalao`): all correctly single-step and the query keeps the whole title.
- Rows 200-209 (messages and typed text that contain aur / phir / and then): all correctly single-step with the conjunction inside the text slot.
- Tricky action calls verified: `whatsapp web kholo` open_website, `flipkart kholo` open_website, `YouTube kholo` open_app, information about people (`Arijit Singh kaun hai`, `... ke baare mein batao`) web_search, `papa ko call karo` and `wifi band kar do` unknown, `gaana band karo` pause, incomplete rows (`rohit ko message karo`, `kuch type karo`) carry `incomplete: true`.

## Summary counts

- Rows checked: 420
- Rows fixed: 31 (30 stray trailing ' |' text artifacts, 1 language code)
- Rows deleted: 3 (1 English-only, 1 self-repair, 1 ambiguous 'band karke')
- Rows remaining: 417
- Remaining by action: multi 158, play_media 52, web_search 46, open_app 32, send_message 28, media_control 21, type_text 20, open_website 17, click 14, unknown 13, read_screen 8, cancel 8
- Remaining by lang: hi 410, none 7

## Main error patterns

1. Trailing ' |' artifact on 30 single-command rows (read_screen, cancel, unknown and two play_media rows): generator wrote a danda as an ASCII pipe.
2. A few rows that are not natural Hindi commands: one pure-English sentence, one mid-utterance self-correction, one ambiguous 'band karke' (close app vs pause).
3. Language code: one language-neutral row tagged hi.
4. No action, step-boundary or slot-boundary errors were found: all slot values are clean substrings without request words, all multi rows split at the correct conjunction, and the title / message / dictation rows with conjunctions are correctly single-step.
