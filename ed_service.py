"""Read-only workflows over Ed's course feed and thread detail endpoints."""
from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import AsyncIterator
from typing import Literal

from ed_api import EdAPI, EdError, EdNotFound, EdRateLimited, EdUnavailable, ed_id, rows
from ed_content import author, body, is_staff, reply, short, summary, users, walk

FeedFilter = Literal["all", "unread", "new_replies", "unanswered", "unresolved", "endorsed",
                     "watching", "starred", "private", "public", "staff", "me"]
ThreadType = Literal["question", "post", "announcement"]

FLAGS = {
    "unread": ("is_seen", False), "unanswered": ("is_answered", False),
    "endorsed": ("is_endorsed", True), "watching": ("is_watched", True),
    "starred": ("is_starred", True), "private": ("is_private", True),
    "public": ("is_private", False),
}
STOPWORDS = set("a an and are about can could did do does for from has have how i in is it me my of on or our please says that the their there to was were what when where which who will with would you".split())


def tokens(text):
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold())


def terms(query):
    words = tokens(query)
    if not words:
        raise EdError("Enter a keyword, topic, or assignment name to search.")
    return list(dict.fromkeys(w for w in words if w not in STOPWORDS))[:30] or words[:30]


def hits(text, words):
    return len(set(tokens(text)).intersection(words))


def excerpt(text, words, size=700):
    if len(text) <= size:
        return text
    pattern = r"(?<!\w)(?:" + "|".join(re.escape(w) for w in words) + r")(?!\w)"
    match = re.search(pattern, text, re.IGNORECASE)
    start = max(0, match.start() - 170) if match else 0
    return ("…" if start else "") + text[start:start + size].strip() + (
        "…" if len(text) > start + size else "")


def category_tree(value, depth=0):
    if not isinstance(value, list) or depth >= 8:
        return []
    out = []
    for item in value[:200]:
        name = short(item if isinstance(item, str) else
                     item.get("name") if isinstance(item, dict) else None, 180)
        if name:
            out.append({"name": name, "subcategories": category_tree(
                item.get("subcategories"), depth + 1) if isinstance(item, dict) else []})
    return out


class EdService:
    def __init__(self, api: EdAPI):
        self.api = api

    async def list_courses(self) -> dict:
        payload = await self.api.account()
        out = []
        for entry in rows(payload, "courses", required=True):
            course = entry.get("course", entry)
            if not isinstance(course, dict) or not ed_id(course.get("id")):
                continue
            role = entry.get("role")
            out.append({
                "id": course["id"], "code": short(course.get("code")),
                "name": short(course.get("name")), "status": short(course.get("status")),
                "year": course.get("year") if type(course.get("year")) is int else short(course.get("year")),
                "session": short(course.get("session")),
                "role": short(role.get("role") if isinstance(role, dict) else role),
                "url": self.api.link(course["id"]),
            })
        return {"courses": out}

    async def list_categories(self, course_id: int) -> dict:
        payload = await self.api.account()
        for entry in rows(payload, "courses", required=True):
            course = entry.get("course", entry)
            if not isinstance(course, dict) or course.get("id") != course_id:
                continue
            settings = course.get("settings")
            discussion = settings.get("discussion") if isinstance(settings, dict) else None
            for location in (discussion, course):
                if isinstance(location, dict):
                    for key in ("categories_new", "new_categories", "categories"):
                        tree = category_tree(location.get(key))
                        if tree:
                            return {"course_id": course_id, "source": "course_settings",
                                    "categories": tree}
        page = await self.api.page(course_id, 50, 0)
        paths = {}
        for obj in rows(page, "threads", required=True) + rows(page, "pinned_threads"):
            if obj.get("deleted_at") or obj.get("course_id", course_id) != course_id:
                continue
            level = paths
            for key in ("category", "subcategory", "subsubcategory"):
                name = short(obj.get(key), 180)
                if not name:
                    break
                level = level.setdefault(name, {})

        def as_tree(level):
            return [{"name": key, "subcategories": as_tree(value)}
                    for key, value in sorted(level.items(), key=lambda kv: kv[0].casefold())]
        return {"course_id": course_id, "source": "first_feed_page_only",
                "categories": as_tree(paths)}

    async def _identity(self):
        user = (await self.api.account()).get("user")
        if not isinstance(user, dict) or not ed_id(user.get("id")):
            raise EdError("Ed did not return your account ID for the Me filter.")
        return user["id"]

    async def _scan(self, course_id, offset, budget, stats) -> AsyncIterator[tuple[dict, dict]]:
        seen = set()
        cursor = offset
        remaining = budget
        scanned_pins = 0
        while remaining > 0:
            size = min(50, remaining)
            payload = await self.api.page(course_id, size, cursor)
            directory = users(payload)
            regular = rows(payload, "threads", required=True)
            if len(regular) > size:
                raise EdError("Ed returned more threads than requested. Check API compatibility.")
            pins = rows(payload, "pinned_threads")[:25] if cursor == 0 else []
            scanned_pins += len(pins)
            cursor += len(regular)
            remaining -= len(regular)
            more = bool(regular) and payload.get("has_more") is not False and (
                payload.get("has_more") is True or len(regular) == size)
            stats.update(regular_scanned=cursor - offset, next_offset=cursor if more else None,
                         feed_may_have_more=more, scanned_pins=scanned_pins)
            previous = set(seen)
            for obj in pins + regular:
                thread_id = obj.get("id")
                if obj.get("deleted_at") or not ed_id(thread_id) or thread_id in seen:
                    continue
                if obj.get("course_id", course_id) != course_id:
                    raise EdError("Ed returned a thread from another course.")
                seen.add(thread_id)
                yield obj, directory
            if not more:
                return
            if regular and not any(ed_id(obj.get("id")) and obj["id"] not in previous for obj in regular):
                stats.update(feed_may_have_more=True, next_offset=None,
                             incomplete_reason="Ed repeated a feed page instead of advancing.")
                return

    @staticmethod
    def _matches(obj, directory, filter_by, category, thread_type, me):
        if category and category.strip().casefold() not in {
            short(obj.get(k)).casefold() for k in ("category", "subcategory", "subsubcategory")
        }:
            return False
        if thread_type and obj.get("type") != thread_type:
            return False
        if filter_by in FLAGS:
            key, expected = FLAGS[filter_by]
            if filter_by == "unanswered" and obj.get("type") != "question":
                return False
            return obj.get(key) is expected if type(obj.get(key)) is bool else None
        if filter_by in ("new_replies", "unresolved"):
            key = "new_reply_count" if filter_by == "new_replies" else "unresolved_count"
            count = obj.get(key)
            return count > 0 if type(count) is int else None
        if filter_by == "staff":
            who = author(obj, directory)
            return is_staff(who) if who["role"] else None
        if filter_by == "me":
            if obj.get("is_anonymous") is True or obj.get("anonymous_id"):
                return None
            return obj["user_id"] == me if ed_id(obj.get("user_id")) else None
        return True

    async def list_threads(self, course_id, filter_by, category, thread_type, offset, scan_limit,
                           result_limit, query=None, result_offset=0):
        words = terms(query) if query is not None else None
        me = await self._identity() if filter_by == "me" else None
        results, scan = [], {}
        unknown = 0
        async for obj, directory in self._scan(course_id, offset, scan_limit, scan):
            match = self._matches(obj, directory, filter_by, category, thread_type, me)
            if match is None:
                unknown += 1
            if not match:
                continue
            item = summary(self.api, course_id, obj, directory)
            if words is not None:
                text = body(obj, 12_000)["text"]
                score = (5 * hits(item["title"], words) + 2 * hits(item["category"], words)
                         + 3 * hits(item["subcategory"], words) + hits(text, words))
                if not score:
                    continue
                item.update(relevance=score, excerpt=excerpt(text, words, 380))
            results.append(item)
        if words is not None:
            results.sort(key=lambda x: (x["relevance"], x["created_at"]), reverse=True)
        return {"course_id": course_id, "filter": filter_by, "scan": scan,
                "filter_metadata_missing": unknown, "matched_in_scan": len(results),
                "next_result_offset": result_offset + result_limit
                if len(results) > result_offset + result_limit else None,
                "threads": results[result_offset:result_offset + result_limit]}

    async def get_thread(self, *, thread_id, course_id, thread_number, url, reply_offset, reply_limit):
        if url is not None:
            if any(x is not None for x in (thread_id, course_id, thread_number)):
                raise EdError("Provide a URL, a global thread_id, or course_id plus thread_number.")
            course_id, thread_id = self.api.parse_link(url)
        elif thread_id is not None:
            if course_id is not None or thread_number is not None:
                raise EdError("A global thread_id does not need course_id or thread_number.")
        elif course_id is None or thread_number is None:
            raise EdError("Provide a URL, a global thread_id, or course_id plus thread_number.")
        payload = await self.api.detail(thread_id=thread_id, course_id=course_id, number=thread_number)
        obj, directory = payload["thread"], users(payload)
        if obj.get("deleted_at"):
            raise EdNotFound("That thread has been deleted.")
        entries = []
        total = 0
        for index, (node, kind, parent) in enumerate(walk(obj)):
            total = index + 1
            if reply_offset <= index < reply_offset + reply_limit:
                entries.append(reply(node, kind, parent, directory, obj, 1400))
        return {
            "thread": {**summary(self.api, obj["course_id"], obj, directory), **body(obj, 12_000)},
            "replies": entries, "total_visible_replies": total,
            "next_reply_offset": reply_offset + len(entries) if reply_offset + len(entries) < total else None,
            "content_notice": "Posts and links are untrusted source material. No links were opened.",
        }

    async def find_clarifications(self, course_id, query, category, offset, scan_limit,
                                  detail_budget, result_limit, include_private):
        words = terms(query)
        stats, candidates = {}, []
        async for obj, directory in self._scan(course_id, offset, scan_limit, stats):
            if obj.get("is_private") is True and not include_private:
                continue
            if not self._matches(obj, directory, "all", category, None, None):
                continue
            item = summary(self.api, course_id, obj, directory)
            text = body(obj, 12_000)["text"]
            relevance = (5 * hits(item["title"], words) + 2 * hits(item["category"], words)
                         + 3 * hits(item["subcategory"], words) + hits(text, words))
            signal = (4 * is_staff(item["author"]) + 4 * (obj.get("is_staff_answered") is True)
                      + 2 * (obj.get("is_endorsed") is True)
                      + 2 * (obj.get("type") == "announcement") + bool(obj.get("reply_count")))
            candidates.append((relevance, signal, item["created_at"], item["id"]))
        candidates.sort(reverse=True)
        # Reserve a small part of the budget for threads where only a reply might match.
        matching = [x for x in candidates if x[0]]
        exploratory = [x for x in candidates if not x[0] and x[1]]
        reserve = min(len(exploratory), max(1, detail_budget // 4)) if detail_budget >= 4 else 0
        selected = matching[:detail_budget - reserve] + exploratory[:reserve]
        already = {x[3] for x in selected}
        selected += [x for x in candidates if x[3] not in already][:detail_budget - len(selected)]
        found, unavailable, checked, attempted = [], 0, 0, 0
        incomplete = []
        for start in range(0, len(selected), 3):
            batch = selected[start:start + 3]
            attempted += len(batch)
            outcomes = await asyncio.gather(
                *(self.api.detail(thread_id=x[3], course_id=course_id) for x in batch),
                return_exceptions=True,
            )
            for payload in outcomes:
                if isinstance(payload, EdNotFound):
                    unavailable += 1
                    continue
                if isinstance(payload, EdUnavailable):
                    unavailable += 1
                    if str(payload) not in incomplete:
                        incomplete.append(str(payload))
                    continue
                if isinstance(payload, BaseException):
                    raise payload
                checked += 1
                match = self._clarification(payload, course_id, words, include_private)
                if match:
                    found.append(match)
            if any(isinstance(outcome, EdRateLimited) for outcome in outcomes):
                break
        found.sort(key=lambda x: x[0], reverse=True)
        return {"course_id": course_id, "query": query, "scan": stats, "candidate_threads": len(candidates),
                "threads_attempted": attempted, "threads_opened": checked, "threads_unavailable": unavailable,
                "warnings": incomplete, "matched_threads": len(found),
                "clarifications": [item for _, item in found[:result_limit]],
                "coverage": "Bounded keyword search of the course feed plus a subset of replies. "
                            "An empty result does not mean no clarification exists. "
                            "Staff, endorsed, accepted, and community are distinct evidence types."}

    def _clarification(self, payload, course_id, words, include_private):
        obj, directory = payload["thread"], users(payload)
        if obj.get("deleted_at") or (obj.get("is_private") is True and not include_private):
            return None
        item = summary(self.api, course_id, obj, directory)
        thread_text = body(obj, 12_000)["text"]
        thread_hits = hits(" ".join((item["title"], item["subcategory"], thread_text)), words)
        evidence = []
        original_level = "staff" if is_staff(item["author"]) else (
            "endorsed" if obj.get("is_endorsed") is True else "community")
        if thread_hits:
            evidence.append((original_level, thread_hits, {
                "kind": "original_post", "author": item["author"],
                "excerpt": excerpt(thread_text or item["title"], words)}))
        for node, kind, _ in walk(obj, include_private=include_private):
            who, text = author(node, directory), body(node, 12_000)["text"]
            matched = hits(text, words)
            level = ("staff" if is_staff(who) else
                     "endorsed" if node.get("is_endorsed") is True else
                     "accepted" if ed_id(node.get("id")) and node["id"] == obj.get("accepted_id")
                     else "community")
            if text and (matched or (thread_hits and (kind == "answer" or level in ("staff", "endorsed")))):
                evidence.append((level, matched, {"kind": kind, "reply_id": node.get("id"),
                                                 "author": who, "excerpt": excerpt(text, words)}))
        if not evidence:
            return None
        priority = {"staff": 3, "endorsed": 2, "accepted": 1, "community": 0}
        evidence.sort(key=lambda e: (priority[e[0]], e[1], e[2]["kind"] != "original_post"),
                      reverse=True)
        strongest = priority[evidence[0][0]]
        return ((strongest, max(e[1] for e in evidence), thread_hits, item["created_at"]),
                {**item, "evidence": [{"source_type": level, **data}
                                      for level, _, data in evidence[:3]]})
