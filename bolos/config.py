"""Settings loaded from the environment (and .env in the project root), with what was changed on the dashboard
(settings.json, bolos.user_settings) taking precedence when it is passed in."""

import os
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER_PREFIX = "your-"
GEMINI_OPENAI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

load_dotenv(ROOT / ".env")


def _is_set(value: str) -> bool:
    return bool(value) and not value.startswith(PLACEHOLDER_PREFIX)


@dataclass(frozen=True)
class Settings:
    gnani_api_key: str
    stt_model: str
    stt_languages: str
    stt_format: str
    user_language: str  # reply language when Prisma doesn't report one (it doesn't with "transliterate")
    tts_model: str
    tts_voice: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_rpm: int  # requests per minute to stay under; 0 means no pacing
    llm_provider: str  # "codex" (Codex CLI), "claude" (Claude Code CLI) or "openai" (any OpenAI-compatible API: Gemini, Evon, ...)
    codex_path: str
    codex_model: str
    codex_effort: str
    claude_path: str
    claude_model: str
    claude_effort: str
    laya_model: str
    decider: str  # "system1" (own model, LLM when unsure), "fast" (own model only), "llm" or "laya"
    fast_model: Path
    slot_model: Path
    preferences_path: Path
    confidence_threshold: float
    hotkey: str
    contacts_path: Path
    wake_enabled: bool = False  # explicit opt-in; requires a separately trained wake.npz
    codex_fast: bool = False

    @property
    def has_gnani(self) -> bool:
        return _is_set(self.gnani_api_key)

    @property
    def has_llm(self) -> bool:
        return _is_set(self.llm_api_key)


def load_settings(overrides: dict[str, object] | None = None) -> Settings:
    """Settings from the environment; `overrides` (Settings field -> value, from the dashboard) win."""
    settings = _from_env()
    return replace(settings, **overrides) if overrides else settings


def _from_env() -> Settings:
    env = os.environ.get
    # A Gemini key alone is enough: it implies Gemini's OpenAI-compatible endpoint.
    gemini_key = env("GEMINI_API_KEY", "")
    llm_key = env("LLM_API_KEY", "") or gemini_key
    default_url = GEMINI_OPENAI_URL if llm_key and llm_key == gemini_key else "https://api.openai.com/v1"
    return Settings(
        gnani_api_key=env("GNANI_API_KEY", ""),
        stt_model=env("STT_MODEL", "prisma-v3.0"),
        stt_languages=env("STT_LANGUAGES", "hi-IN,en-IN,kn-IN,ta-IN"),
        # Latin script for every language: with native scripts Prisma often wrote Hindi and Tamil speech
        # in Kannada script when several languages were allowed.
        stt_format=env("STT_FORMAT", "transliterate"),
        user_language=env("USER_LANGUAGE", "hi-IN"),
        tts_model=env("TTS_MODEL", "timbre-v3.0"),
        tts_voice=env("TTS_VOICE", "Shivani"),
        llm_base_url=env("LLM_BASE_URL", default_url),
        llm_api_key=llm_key,
        llm_model=env("LLM_MODEL", "gemini-3.5-flash-lite"),
        llm_rpm=int(env("LLM_RPM", "0")),
        llm_provider=env("LLM_PROVIDER", "codex" if shutil.which("codex") else "openai").strip().lower(),
        codex_path=env("CODEX_PATH", ""),
        codex_model=env("CODEX_MODEL", "gpt-6-luna"),
        codex_effort=env("CODEX_EFFORT", "low"),
        claude_path=env("CLAUDE_PATH", ""),
        claude_model=env("CLAUDE_MODEL", "haiku"),  # the fastest model; "sonnet" if answers need more care
        claude_effort=env("CLAUDE_EFFORT", "low"),
        laya_model=env("LAYA_MODEL", "multilingual"),
        decider=env("DECIDER", "system1"),
        fast_model=Path(env("FAST_MODEL", str(ROOT / "bolos" / "fast" / "models" / "system1.npz"))),
        slot_model=Path(env("SLOT_MODEL", str(ROOT / "bolos" / "fast" / "models" / "slots.npz"))),
        preferences_path=Path(env("PREFERENCES_PATH", str(ROOT / "preferences.json"))),
        confidence_threshold=float(env("CONFIDENCE_THRESHOLD", "0.55")),
        hotkey=env("HOTKEY", "ctrl+space"),
        contacts_path=Path(env("CONTACTS_PATH", str(ROOT / "contacts.json"))),
        wake_enabled=env("ENABLE_WAKE_WORD", "0").strip().lower() in {"1", "true", "yes", "on"},
        codex_fast=env("CODEX_FAST", "0").strip().lower() in {"1", "true", "yes", "on"},
    )
