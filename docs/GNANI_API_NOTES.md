# Gnani API verification — 6 November 2026

Checked the current official [REST documentation](https://docs.gnani.ai/api/STT/speech-to-text.md)
and [streaming documentation](https://docs.gnani.ai/api/STT/stt-websocket.md) during the continuation.

- Prisma v3 supports the ten configured language families. Explicit candidate lists contain at most
  four languages; `indic-auto` selects across supported languages. Keep this distinction when changing
  `STT_LANGUAGES` rather than sending a ten-language list.
- Romanised `transliterate` output does not return detected-language metadata. Local language inference
  or the configured fallback is still needed for spoken replies.
- Streaming uses 16-bit little-endian mono PCM in 1,024-byte frames. At 16 kHz, pace frames 32 ms apart,
  including queued audio, rather than bursting a backlog. `commit` closes the current utterance.
- Speech-start, processing and final transcript events describe segment progress; a missing final
  transcript must not turn an unfinished command into an executable partial command.
- REST clips are limited to 30 seconds. A provider `truncated` result is incomplete and must be refused.

This is protocol verification from documentation, not a live service or physical microphone test.
