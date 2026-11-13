"""Regression tests for account variants, inconsistent feeds and interrupted reads."""
import json
import unittest
from copy import deepcopy
from unittest.mock import patch

import httpx

from ed_api import EdAPI, EdAccessDenied, EdError, EdUnavailable
from ed_content import author, is_staff
from ed_service import EdService, terms
from tests.fixtures import ACCOUNT, DETAILS, FEED, FakeEd, PIN


def service(handler, region="us"):
    return EdService(EdAPI("fixture-token", region, httpx.MockTransport(handler)))


class AccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_students_tas_instructors_admins_and_archived_courses(self):
        for role in ("student", "tutor", "staff", "instructor", "admin", "observer"):
            with self.subTest(role=role):
                data = deepcopy(ACCOUNT)
                data["courses"][0]["role"]["role"] = role
                data["courses"].append({"id": 50, "status": "archived", "name": "Past course"})
                result = await service(lambda _: httpx.Response(200, json=data)).list_courses()
                self.assertEqual([x["id"] for x in result["courses"]], [311, 50])
                self.assertEqual(result["courses"][0]["role"], role)
                self.assertEqual(result["courses"][1]["status"], "archived")
                self.assertNotIn("email", str(result))

    async def test_no_enrollments_and_missing_account_shape(self):
        result = await service(lambda _: httpx.Response(200, json={"courses": []})).list_courses()
        self.assertEqual(result["courses"], [])
        with self.assertRaisesRegex(EdError, "courses"):
            await service(lambda _: httpx.Response(200, json={"user": {}})).list_courses()

    async def test_category_fallback_and_denied_course(self):
        fake = FakeEd()
        def get(req):
            if req.url.path == "/api/user":
                return httpx.Response(200, json={"courses": []})
            if "/999/" in req.url.path:
                return httpx.Response(403)
            return fake(req)
        svc = service(get)
        result = await svc.list_categories(311)
        self.assertEqual(result["source"], "first_feed_page_only")
        self.assertEqual(result["categories"][0]["name"], "Assignments")
        with self.assertRaises(EdAccessDenied):
            await svc.list_categories(999)

    def test_course_role_not_global_role_and_anonymous(self):
        directory = {4: {"name": "Name", "role": "admin"}}
        self.assertFalse(is_staff(author({"user_id": 4}, directory)))
        self.assertTrue(is_staff(author({"user_id": 4, "user": {"course_role": " Tutor "}}, directory)))
        self.assertEqual(author({"user_id": 4, "anonymous_id": 8, "course_role": "staff"}, directory),
                         {"name": "Anonymous", "role": None})
        self.assertEqual(author({"user_id": [], "user": {"name": "Guest", "course_role": 3}}, {}),
                         {"name": "Guest", "role": None})

    async def test_fixed_regional_hosts_and_links(self):
        for region in ("us", "au", "eu"):
            with self.subTest(region=region):
                def check(req):
                    self.assertEqual(req.method, "GET")
                    self.assertEqual(req.url.host, region + ".edstem.org")
                    self.assertEqual(req.headers["Authorization"], "Bearer fixture-token")
                    return httpx.Response(200, json={"courses": []})
                svc = service(check, region.upper())
                self.assertEqual((await svc.list_courses())["courses"], [])
                self.assertEqual(svc.api.parse_link(svc.api.link(311, 1001) + "#a"), (311, 1001))
                with self.assertRaises(EdError):
                    svc.api.parse_link("https://edstem.org@invalid.example/us/courses/311/discussion/1001")

    async def test_no_body_leak_on_network_or_bad_payload(self):
        def fail(req):
            raise httpx.ReadTimeout("private contents")
        with self.assertRaisesRegex(EdUnavailable, "Could not reach") as ctx:
            await service(fail).api.account()
        self.assertNotIn("private contents", str(ctx.exception))
        for payload in ([], 3, None):
            with self.assertRaisesRegex(EdError, "JSON object"):
                await service(lambda _: httpx.Response(200, content=json.dumps(payload))).api.account()
        with patch("ed_api.MAX_BYTES", 15):
            with self.assertRaisesRegex(EdError, "limit"):
                await service(lambda _: httpx.Response(200, content=b"x" * 16)).api.account()


class SearchEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unicode_punctuation_and_missing_metadata(self):
        raw = deepcopy(FEED[0])
        raw.update(title="Projet café-initialisation")
        raw.pop("is_seen")
        svc = service(lambda _: httpx.Response(200, json={"threads": [raw]}))
        result = await svc.list_threads(311, "all", None, None, 0, 50, 10, "cafe\u0301 initialisation")
        self.assertGreaterEqual(result["threads"][0]["relevance"], 10)
        result = await svc.list_threads(311, "unread", None, None, 0, 50, 10)
        self.assertEqual(result["filter_metadata_missing"], 1)
        self.assertEqual(result["threads"], [])
        with self.assertRaises(EdError):
            terms("!?")

    async def test_repeated_feed_stops_without_paging_forever(self):
        calls = []
        def repeated(req):
            calls.append(req.url.params["offset"])
            return httpx.Response(200, json={"threads": FEED, "has_more": True, "pinned_threads": [PIN]})
        result = await service(repeated).list_threads(311, "all", None, None, 0, 200, 20)
        self.assertEqual(calls, ["0", "5"])
        self.assertIn("repeated", result["scan"]["incomplete_reason"])
        self.assertIsNone(result["scan"]["next_offset"])
        self.assertEqual(result["scan"]["scanned_pins"], 1)

    async def test_reply_only_budget_reserve(self):
        result = await service(FakeEd()).find_clarifications(311, "Project 3 input", None, 0, 50, 4, 10, False)
        self.assertEqual(result["threads_attempted"], 4)
        self.assertIn(1004, [x["id"] for x in result["clarifications"]])

    async def test_rate_limit_preserves_partial_results(self):
        fake = FakeEd()
        def rate_limit(req):
            if req.url.path == "/api/threads/1002":
                return httpx.Response(429)
            return fake(req)
        result = await service(rate_limit).find_clarifications(311, "Project 3 input", None, 0, 50, 10, 10, False)
        self.assertEqual(result["threads_attempted"], 3)
        self.assertEqual(result["threads_unavailable"], 1)
        self.assertIn("rate-limited", result["warnings"][0])
        self.assertTrue(result["clarifications"])

    async def test_vanished_candidate_and_server_failure_do_not_erase_success(self):
        fake = FakeEd()
        def flake(req):
            if req.url.path.endswith("/1002"):
                return httpx.Response(404)
            if req.url.path.endswith("/1004"):
                return httpx.Response(503)
            return fake(req)
        result = await service(flake).find_clarifications(311, "Project 3 input", None, 0, 50, 10, 10, False)
        self.assertEqual(result["threads_unavailable"], 2)
        self.assertEqual(result["warnings"], ["Ed returned HTTP 503."])
        self.assertTrue(result["clarifications"])

    async def test_private_opt_in_does_not_change_permissions(self):
        result = await service(FakeEd()).find_clarifications(311, "Project 3 input", None, 0, 50, 10, 10, True)
        self.assertIn(1003, [x["id"] for x in result["clarifications"]])
        def denied(req):
            if req.url.path.endswith("/1003"):
                return httpx.Response(403)
            return FakeEd()(req)
        with self.assertRaises(EdAccessDenied):
            await service(denied).find_clarifications(311, "Project 3 input", None, 0, 50, 10, 10, True)

    async def test_deleted_thread_does_not_return_body(self):
        obj = deepcopy(DETAILS[1001])
        obj["deleted_at"] = "2026-10-11"
        svc = service(lambda _: httpx.Response(200, json={"thread": obj}))
        with self.assertRaisesRegex(EdError, "deleted"):
            await svc.get_thread(thread_id=1001, course_id=None, thread_number=None, url=None,
                                 reply_offset=0, reply_limit=10)
