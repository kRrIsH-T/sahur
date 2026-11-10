"""Explain a form in plain words: a short spoken summary first, then each field on request, and page errors.

Works on bolos.forms.fields.Form (title, url, fields, buttons, errors, headings). Only labels, counts and the
kinds catalog are spoken; field values never are.
"""

import re
from dataclasses import dataclass

from bolos.forms import kinds
from bolos.forms.prompts import prompt


@dataclass(frozen=True)
class Explanation:
    summary: str  # spoken first: what the form is, how many things, documents, what the user must type
    fields: list[str]  # one plain line per field, in screen order ("Number 3, IFSC code. Required. ...")
    concepts: list[str]  # the concept of each field, same order (for follow-up questions)


def _listing(items: list[str], language: str | None) -> str:
    return ", ".join(items)


def _title(form) -> str:
    title = (getattr(form, "title", "") or "").strip()
    # Browser window titles end with the browser's name; the page part is enough.
    title = re.sub(r"\s+[-|]\s+(Google Chrome|Microsoft​? Edge|Mozilla Firefox|Brave)$", "", title)
    headings = getattr(form, "headings", None) or []
    return (headings[0] if headings else title).strip()


def field_line(n: int, field, language: str | None) -> str:
    concept = kinds.classify(field)
    need = prompt("required" if field.required else "optional", language)
    if getattr(field, "value", None) and kinds.sensitivity(concept) != kinds.NEVER_FILL:
        need = f"{need}, {prompt('has_value', language)}"
    text = kinds.explain(concept, language)
    where = kinds.where_to_find(concept, language)
    if where:
        text = f"{text} {where}"
    if field.options and concept not in {"gender"}:
        text = f"{text} {', '.join(field.options[:8])}"
    return prompt("field_line", language, n=n, label=field.label, need=need, text=text)


def explain(form, language: str | None) -> Explanation:
    fields = list(getattr(form, "fields", ()) or ())
    concepts = [kinds.classify(f) for f in fields]
    required = sum(1 for f in fields if f.required)
    title = _title(form)
    parts = [prompt("summary" if title else "summary_untitled", language, title=title, count=len(fields),
                    required=required)]
    documents = kinds.documents(concepts, language)
    if documents:
        parts.append(prompt("documents", language, items=_listing(documents, language)))
    yours = [f.label for f, c in zip(fields, concepts, strict=True)
             if kinds.sensitivity(c) in {kinds.NEVER_FILL, kinds.MANUAL}]
    if yours:
        parts.append(prompt("you_type", language, items=", ".join(yours)))
    lines = [field_line(i + 1, f, language) for i, f in enumerate(fields)]
    return Explanation(" ".join(parts), lines, concepts)


_REQUIRED_ERROR = re.compile(r"required|missing|empty|please (fill|enter|select)|cannot be blank|mandatory|"
                             r"आवश्यक|ज़रूरी|जरूरी|अनिवार्य|ಕಡ್ಡಾಯ|கட்டாய|తప్పనిసరి|ആവശ്യ|আবশ্যিক|જરૂરી|ਜ਼ਰੂਰੀ", re.I)


_FORMAT_ERROR = re.compile(r"valid|incorrect|format|must (be|have|contain|start)|digits|characters|wrong|match|"
                           r"अमान्य|गलत|सही", re.I)


def page_error(message: str, form, language: str | None) -> str | None:
    """One page error ('Mobile number: Please enter a valid number') explained in the user's language."""
    fields = sorted(getattr(form, "fields", ()) or (), key=lambda f: -len(f.label))
    text = message.strip()
    field = next((f for f in fields if f.label and text.casefold().startswith(f.label.casefold())), None)
    if field is None:
        field = next((f for f in fields if f.label and f.label.casefold() in text.casefold()), None)
    if field is None:
        return None  # an unknown page message may echo an entered value
    if _REQUIRED_ERROR.search(text) and not _FORMAT_ERROR.search(text):
        hint = prompt("required", language)
    else:
        hint = kinds.explain(kinds.classify(field), language)
    return f"{field.label}. {hint}"


def page_errors(messages: list[str], form, language: str | None) -> list[str]:
    if not messages:
        return []
    lines = [prompt("page_errors", language, count=len(messages))]
    lines += [text for message in messages if (text := page_error(message, form, language)) is not None]
    return lines
