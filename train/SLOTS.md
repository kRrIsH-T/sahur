# Labelling spec: actions and slots

One row per command, JSON on one line:

```json
{"text": "Spotify pe Radiohead chalao", "action": "play_media", "slots": {"app": "Spotify", "query": "Radiohead"}}
```

Every slot value is copied **exactly** from `text`: same spelling, same case, same script, a contiguous
piece of it. Never translate, correct, expand or add words. Leave a slot out when it isn't said.
`train/check_rows.py` rejects rows that break this.

| action | slots | meaning |
|---|---|---|
| `open_app` | `app` (required) | open, start or launch an application, program or well-known service by name |
| `open_website` | `site` (required) | open a website: the user says website/site/page, a domain or "dot com", or a site that is only a website |
| `web_search` | `query` (required), `site` | look something up: questions, facts, news, weather, prices, "tell me about X", "explain X", "X kaun hai" |
| `play_media` | `query`, `app` | play a song, artist, album, genre, podcast or video; `app` is the service if named |
| `media_control` | `control` (required, not a span) | pause, resume, next, previous, volume_up, volume_down, mute |
| `send_message` | `contact` (required), `text` (required) | send a WhatsApp message to a person |
| `type_text` | `text` (required) | type or write text into the current box or document |
| `click` | `target` (required) | click or press a visible button, link, tab or menu item |
| `read_screen` | none | read out or explain what is on the screen right now |
| `cancel` | none | stop the current request, never mind, do nothing |
| `unknown` | none | chatter, unclear speech, or a request Bol OS can't do (brightness, alarms, calls, shutting down, ...) |

## Slot boundaries

- `app`: the app or service name only, without "app", "application", "wala", "ko", "ka", "ge", "la", "ah".
  "Zoom wala app kholo" -> `Zoom`. "VS Code open karo" -> `VS Code`. A generic word is fine when it is all
  the user said: "browser kholo" -> `browser`, "calculator chalu karo" -> `calculator`.
- `site`: the site name or address only, without "website", "site", "ki", "pe", "par", "la", "alli".
  "Amazon ki website kholo" -> `Amazon`. "open github dot com" -> `github dot com`.
- `query` for `web_search`: what to look up, without the request words (batao, bataiye, search karo,
  dhundho, google karo, tell me, tell me about, explain, samjhao, hudku, helu, sollu, cheppu, please).
  Keep question words, they make a good search: "India ka prime minister kaun hai" -> the whole text
  (minus final punctuation); "Bangalore ka weather batao" -> `Bangalore ka weather`;
  "tell me about Radiohead" -> `Radiohead`; "samjhao photosynthesis kya hai" -> `photosynthesis kya hai`.
  `site` when the user names where to search: "Amazon pe shoes dhundho" -> site `Amazon`, query `shoes`.
- `query` for `play_media`: what to play. Drop a generic trailing word that only means "songs/video"
  after a name ("Arijit ke gaane" -> `Arijit`, "Radiohead songs" -> `Radiohead`, "Ilayaraja paattu" ->
  `Ilayaraja`), but keep it when it is the whole description ("old Hindi songs" -> `old Hindi songs`,
  "lo-fi music" -> `lo-fi music`, "bhajans" -> `bhajans`). No query when only "gaana/music/kuch" is said:
  "gaana chalao" -> no `query`. `app` is the service: Spotify, YouTube, YouTube Music, JioSaavn, Gaana,
  Wynk, Apple Music, Amazon Music.
- `contact`: the person's name or relation as spoken, without ko/ku/kku/ge/ki/ne/la/ki/ku/ke:
  "Raju ko" -> `Raju`, "amma ge" -> `amma`, "Ravi bhai ko" -> `Ravi bhai`.
- `text` (message or typing): the words to send or type, without the command verbs and connectors
  (ki, ke, bolo, likho, message karo, type karo, anta, antha, nu, ani, that). "Mummy ko bolo ki main
  late hoon" -> `main late hoon`.
- `target`: the label of the thing to click, without button/link/wala/pe/ko/dabao/click:
  "submit button dabao" -> `submit`, "Next pe click karo" -> `Next`.
- `control`: one of `pause`, `resume`, `next`, `previous`, `volume_up`, `volume_down`, `mute`.

A request that clearly asks for an action but leaves out a required slot ("Ravi ge message kalsu" names no
message) keeps its action, tags what is there, and gets `"incomplete": true`. Bol OS then asks for the
rest instead of treating it as chatter.

## Tricky cases

- Information about a person, band, film or song is `web_search`, even when it names an artist:
  "Lata Mangeshkar ke baare mein batao", "Radiohead kaun hai", "who sang Tum Hi Ho", "Creep ke lyrics".
  Playing it is `play_media`: "Lata Mangeshkar ke gaane chalao", "play Creep".
- "YouTube kholo" (just open it) is `open_app` with app `YouTube`; "YouTube pe Arijit chalao" is
  `play_media`; "YouTube pe cooking videos search karo" is `web_search` with site `YouTube`.
- Pausing, resuming or skipping the music that is already playing is `media_control`:
  "gaana rok do" -> pause, "agla gaana" -> next, "awaaz badhao" -> volume_up. "rehne do" or "cancel" about
  a request is `cancel`. Brightness, alarms, calls, shutdown stay `unknown`.
