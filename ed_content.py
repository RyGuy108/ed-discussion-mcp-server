"""Normalize untrusted Ed text, discussion metadata, and reply trees."""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

from ed_api import EdError, ed_id, rows

BLOCKS = {"p", "paragraph", "heading", "div", "li", "list-item", "br", "pre", "snippet",
          "callout", "blockquote", "h1", "h2", "h3", "h4", "hr", "tr"}
HIDDEN = {"script", "style", "template", "iframe", "object", "embed", "noscript"}
STAFF_ROLES = {"staff", "admin", "instructor", "tutor", "teacher", "ta", "teaching assistant"}


class EdMarkup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        if tag in HIDDEN:
            self.hidden.append(tag)
        if self.hidden:
            return
        if tag in BLOCKS:
            self.parts.append("\n")
        if tag == "math":
            self.parts.append("$")
        attr = dict(attrs)
        keys = {"link": "href", "a": "href", "image": "src", "img": "src",
                "file": "url", "attachment": "url"}
        target = attr.get(keys.get(tag, ""))
        if target and len(target) <= 2048 and len(self.links) < 20:
            try:
                url = urlsplit(target)
                if (url.scheme == "https" and url.hostname and not url.username
                        and not url.password and target not in self.links):
                    self.links.append(target)
            except ValueError:
                pass

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.hidden:
            if self.hidden[-1] == tag:
                self.hidden.pop()
            return
        if tag in BLOCKS:
            self.parts.append("\n")
        if tag == "math":
            self.parts.append("$")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def body(obj: dict, limit: int) -> dict:
    raw = next((obj[k] for k in ("content", "document", "plaintext")
                if isinstance(obj.get(k), str) and obj[k]), "")
    parser = EdMarkup()
    parser.feed(raw[:100_000])
    parser.close()
    rendered = re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()
    return {"text": rendered[:limit], "text_truncated": len(rendered) > limit or len(raw) > 100_000,
            "links": parser.links}


def users(payload: dict) -> dict:
    return {u["id"]: u for u in rows(payload, "users") if ed_id(u.get("id"))}


def author(obj: dict, directory: dict) -> dict:
    if obj.get("is_anonymous") is True or obj.get("anonymous_id"):
        return {"name": "Anonymous", "role": None}
    ident = obj.get("user_id")
    entry = directory.get(ident, {}) if ed_id(ident) else {}
    embedded = obj["user"] if isinstance(obj.get("user"), dict) else {}
    role = next((value.strip() for value in (entry.get("course_role"), obj.get("course_role"),
                 embedded.get("course_role"), obj.get("user_role"))
                 if isinstance(value, str) and value.strip()), None)
    name = short(entry.get("name"), 200) or short(embedded.get("name"), 200) or None
    return {"name": name, "role": short(role, 80) or None}


def short(value, size=350):
    return value[:size] if isinstance(value, str) else ""


def is_staff(who: dict) -> bool:
    return (who.get("role") or "").casefold() in STAFF_ROLES


def summary(api, course_id: int, obj: dict, directory: dict) -> dict:
    thread_id = obj.get("id")
    if not ed_id(thread_id) or obj.get("course_id", course_id) != course_id:
        raise EdError("Ed returned an invalid or mismatched thread in the course feed.")
    out = {
        "id": thread_id, "course_id": course_id, "url": api.link(course_id, thread_id),
        "number": obj.get("number") if ed_id(obj.get("number")) else None,
        "author": author(obj, directory),
    }
    for key in ("type", "title", "category", "subcategory", "subsubcategory", "created_at", "updated_at"):
        out[key] = short(obj.get(key), 400 if key == "title" else 180)
    for key in ("is_seen", "is_starred", "is_watched", "is_private", "is_endorsed", "is_answered",
                "is_staff_answered", "is_pinned", "is_locked", "is_megathread"):
        if type(obj.get(key)) is bool:
            out[key] = obj[key]
    for key in ("reply_count", "unresolved_count", "new_reply_count", "duplicate_id"):
        if type(obj.get(key)) is int:
            out[key] = obj[key]
    return out


def walk(obj: dict, *, include_private: bool = True):
    stack = [(node, kind, None) for kind, key in (("comment", "comments"), ("answer", "answers"))
             for node in reversed(rows(obj, key))]
    while stack:
        node, kind, parent = stack.pop()
        if node.get("deleted_at") or (node.get("is_private") is True and not include_private):
            continue
        yield node, kind, parent
        ident = node.get("id")
        stack.extend((child, "comment", ident if ed_id(ident) else None)
                     for child in reversed(rows(node, "comments")))


def reply(node, kind, parent, directory, thread, limit):
    return {
        "id": node.get("id") if ed_id(node.get("id")) else None,
        "parent_id": parent, "type": kind, "author": author(node, directory),
        "created_at": short(node.get("created_at"), 80),
        "is_endorsed": node.get("is_endorsed") is True,
        "is_accepted": ed_id(node.get("id")) and node["id"] == thread.get("accepted_id"),
        "is_resolved": node.get("is_resolved") if type(node.get("is_resolved")) is bool else None,
        "is_private": node.get("is_private") is True,
        **body(node, limit),
    }
