"""The typed action schema. Models only ever produce these; the executor only accepts these."""

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter


class OpenApp(BaseModel):
    action: Literal["open_app"] = "open_app"
    app: str = Field(min_length=1)


# The browser a website or search opens in ("Chrome", "Edge", "Firefox", "Brave", "Opera"; None: the default
# one). Never a slot System 1 or the LLM fills: a multi-step command sets it when an earlier step opened that
# browser ("Chrome kholo aur weather search karo"), see bolos.compound.carry_over.
Browser = str | None


class OpenWebsite(BaseModel):
    action: Literal["open_website"] = "open_website"
    site: str = Field(min_length=1)
    browser: Browser = None


class SendMessage(BaseModel):
    action: Literal["send_message"] = "send_message"
    app: Literal["whatsapp"] = "whatsapp"
    contact: str = Field(min_length=1)
    text: str = Field(min_length=1)


class WebSearch(BaseModel):
    action: Literal["web_search"] = "web_search"
    query: str = Field(min_length=1)
    site: str | None = None  # search inside this site ("Amazon pe shoes dhundho"), else Google
    browser: Browser = None


class PlayMedia(BaseModel):
    action: Literal["play_media"] = "play_media"
    query: str = ""  # empty: just play music ("gaana chalao")
    app: str | None = None  # the service the user named; None: their usual one


CONTROLS = ("pause", "resume", "next", "previous", "volume_up", "volume_down", "mute")


class MediaControl(BaseModel):
    action: Literal["media_control"] = "media_control"
    control: Literal["pause", "resume", "next", "previous", "volume_up", "volume_down", "mute"]


class TypeText(BaseModel):
    action: Literal["type_text"] = "type_text"
    text: str = Field(min_length=1)


class ReadScreen(BaseModel):
    action: Literal["read_screen"] = "read_screen"


class Click(BaseModel):
    action: Literal["click"] = "click"
    target: str = Field(min_length=1)


class ExplainForm(BaseModel):
    action: Literal["explain_form"] = "explain_form"


class FillForm(BaseModel):
    action: Literal["fill_form"] = "fill_form"


class CheckSite(BaseModel):
    action: Literal["check_site"] = "check_site"


class UploadFile(BaseModel):
    action: Literal["upload_file"] = "upload_file"
    target: str = ""


class ShowHow(BaseModel):
    """Guide mode ("WhatsApp pe photo kaise bhejte hain, dikhao"): point at each step for the user to do.
    Recognised locally (bolos.guide.intent), never by System 1 or System 2, so it is not in ACTION_DESCRIPTIONS
    (whose order the trained models follow) and never becomes training data or a habit."""

    action: Literal["show_how"] = "show_how"
    goal: str = Field(min_length=1)


class DriveScreen(BaseModel):
    action: Literal["drive_screen"] = "drive_screen"
    goal: str = Field(min_length=1)


class Cancel(BaseModel):
    action: Literal["cancel"] = "cancel"


class Unknown(BaseModel):
    action: Literal["unknown"] = "unknown"


Action = Annotated[
    Union[OpenApp, OpenWebsite, SendMessage, WebSearch, PlayMedia, MediaControl, TypeText, ReadScreen, Click,
          ExplainForm, FillForm, CheckSite, UploadFile, ShowHow, DriveScreen, Cancel, Unknown],
    Field(discriminator="action"),
]
ACTION_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)

# What each action means, in words a model (or the LLM) chooses between.
ACTION_DESCRIPTIONS: dict[str, str] = {
    "open_app": "open, start or launch an application, program or service by name",
    "open_website": "open a website by name or address",
    "send_message": "send a message or text to a person on WhatsApp",
    "web_search": "look something up: questions, facts, news, weather, prices, tell me about a person, film or "
                  "song, explain something; optionally search inside a named site",
    "play_media": "play a song, artist, album, playlist, genre, podcast or video, optionally on a named service",
    "media_control": "pause, resume or skip the music or video already playing, or change the volume",
    "type_text": "type or write some text into the current text box or document",
    "read_screen": "read out or explain what is on the screen right now",
    "click": "click or press a button or link that is visible on the screen",
    "explain_form": "explain the form on screen, its fields, required information and documents",
    "fill_form": "help fill the form on screen with a spoken question for each field",
    "check_site": "check the current website address for warning signs; never certify it as genuine",
    "upload_file": "help choose a document to upload to the current form",
    "cancel": "stop, cancel, never mind, do nothing",
    "unknown": "anything else: chatter, unclear speech, or something this assistant can't do",
}
ACTION_NAMES = list(ACTION_DESCRIPTIONS)

# Fields each action needs, extracted from the command (by System 1's tagger, else the LLM).
SLOT_FIELDS: dict[str, list[str]] = {
    "open_app": ["app"],
    "open_website": ["site"],
    "send_message": ["contact", "text"],
    "web_search": ["query", "site"],
    "play_media": ["query", "app"],
    "type_text": ["text"],
    "click": ["target"],
    "upload_file": ["target"],
}
REQUIRED_FIELDS: dict[str, set[str]] = {
    "open_app": {"app"},
    "open_website": {"site"},
    "send_message": {"contact", "text"},
    "web_search": {"query"},
    "play_media": set(),
    "type_text": {"text"},
    "click": {"target"},
    "upload_file": set(),
}

FORM_ACTIONS = {"explain_form", "fill_form", "check_site", "upload_file"}
# Dialogues that speak for themselves and must run alone, never as one step of a compound command.
GUIDE_ACTIONS = {"show_how", "drive_screen"}
DIALOGUE_ACTIONS = FORM_ACTIONS | GUIDE_ACTIONS

# Actions that change something outside this computer: always read back and confirm first.
RISKY_ACTIONS = {"send_message"}


def parse_action(data: dict) -> Action:
    return ACTION_ADAPTER.validate_python(data)
