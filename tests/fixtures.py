"""Invented courses and posts modeled on Ed's API shape."""
from copy import deepcopy

import httpx

COURSE_ID = 311
ACCOUNT = {
    "user": {"id": 1, "email": "not-in-output@example.invalid"},
    "courses": [{"course": {"id": COURSE_ID, "name": "Discrete Structures Demo", "code": "CS 311",
                            "status": "active", "settings": {"discussion": {
                                "categories_new": [{"name": "Assignments", "subcategories": [
                                    {"name": "Project 3", "subcategories": []}]}]}}},
                 "role": {"role": "student"}}],
}
USERS = [
    {"id": 1, "name": "Demo Student", "course_role": "student"},
    {"id": 2, "name": "Demo TA", "course_role": "staff"},
    {"id": 3, "name": "Anonymous identity must not leak", "course_role": "student"},
]


def thread(id, title, text, **fields):
    return {"id": id, "number": id - 1000, "course_id": COURSE_ID, "user_id": 1,
            "title": title, "content": f"<document><paragraph>{text}</paragraph></document>",
            "created_at": "2026-10-12T12:00:00Z", "type": "question",
            "category": "Assignments", "subcategory": "Project 3", "is_private": False,
            "is_answered": False, "is_seen": True, "is_starred": False, "is_watched": False,
            "is_endorsed": False, "is_pinned": False, "new_reply_count": 0, "unresolved_count": 0,
            **fields}


def reply(id, text, **fields):
    return {"id": id, "user_id": 2, "content": text, **fields}


FEED = [
    thread(1001, "Project 3 input", "Can an empty input be valid?", is_answered=True,
           is_seen=False, is_watched=True, is_starred=True, new_reply_count=1,
           unresolved_count=1, is_staff_answered=True, reply_count=3),
    thread(1002, "Project 3 input theory", "I think empty input is invalid.", user_id=3,
           is_anonymous=True, is_endorsed=True, is_answered=True),
    thread(1003, "A private Project 3 input note", "Private thread.", is_private=True),
    thread(1004, "Grader behavior", "General question.", reply_count=1, is_staff_answered=True),
    thread(1005, "Submission formatting", "Check the checklist.", type="post"),
]
PIN = thread(1006, "Project 3 input announcement", "Read the updated handout.",
             type="announcement", user_id=2, is_pinned=True)
DETAILS = {item["id"]: deepcopy(item) for item in FEED + [PIN]}
DETAILS[1001].update(
    accepted_id=91,
    answers=[reply(91, "<paragraph>Yes. Empty input is valid.</paragraph>"
                   '<file url="https://static.us.edusercontent.com/files/demo"/>', comments=[
                       reply(92, "<paragraph>Return the empty set.</paragraph>", is_endorsed=True, user_id=1),
                       reply(93, "<paragraph>Private answer details for Project 3 input.</paragraph>",
                             is_private=True, comments=[reply(94, "Nested private input details.")]),
                   ])],
    comments=[reply(95, "Deleted", deleted_at="2026-10-13T00:00:00Z")],
)
DETAILS[1004]["comments"] = [reply(96, "For Project 3 input, accept a zero-length list.")]


class FakeEd:
    def __init__(self):
        self.requests = []

    def __call__(self, req):
        self.requests.append(req)
        assert req.method == "GET"
        assert req.url.host == "us.edstem.org"
        assert req.headers["Authorization"] == "Bearer fixture-token"
        assert not req.content
        parts = req.url.path.split("/")
        if req.url.path == "/api/user":
            return httpx.Response(200, json=ACCOUNT)
        if req.url.path == "/api/courses/311/threads":
            offset, limit = int(req.url.params["offset"]), int(req.url.params["limit"])
            return httpx.Response(200, json={
                "threads": FEED[offset:offset + limit],
                "pinned_threads": [PIN], "users": USERS, "has_more": offset + limit < len(FEED),
            })
        if len(parts) == 4 and parts[:3] == ["", "api", "threads"]:
            ident = int(parts[-1])
        elif len(parts) == 6 and parts[:5] == ["", "api", "courses", "311", "threads"]:
            ident = 1000 + int(parts[-1])
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"thread": DETAILS[ident], "users": USERS}) if ident in DETAILS else httpx.Response(404)
