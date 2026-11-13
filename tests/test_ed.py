import sys
import unittest

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ed_api import EdAPI, EdError
from ed_content import body
from ed_service import EdService
from server import TOOL_NAMES
from tests.fixtures import FakeEd


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake = FakeEd()
        self.api = EdAPI("fixture-token", transport=httpx.MockTransport(self.fake))
        self.service = EdService(self.api)

    async def test_courses_categories_and_minimization(self):
        courses = await self.service.list_courses()
        self.assertEqual(courses["courses"][0]["id"], 311)
        self.assertNotIn("example.invalid", str(courses))
        cats = await self.service.list_categories(311)
        self.assertEqual(cats["categories"][0]["subcategories"][0]["name"], "Project 3")

    async def test_filters(self):
        expected = {"all": [1006, 1001, 1002, 1003, 1004, 1005],
                    "unread": [1001], "new_replies": [1001], "unanswered": [1003, 1004],
                    "unresolved": [1001], "endorsed": [1002], "watching": [1001],
                    "starred": [1001], "private": [1003], "public": [1006, 1001, 1002, 1004, 1005],
                    "staff": [1006], "me": [1001, 1003, 1004, 1005]}
        for key, ids in expected.items():
            with self.subTest(key=key):
                result = await self.service.list_threads(311, key, None, None, 0, 10, 10)
                self.assertEqual([item["id"] for item in result["threads"]], ids)
                if key in ("me", "staff"):
                    self.assertEqual(result["filter_metadata_missing"], 1)

    async def test_pagination_and_category_filter(self):
        first = await self.service.list_threads(311, "all", "project 3", None, 0, 2, 1)
        self.assertEqual(first["scan"]["next_offset"], 2)
        self.assertEqual(first["next_result_offset"], 1)
        next_match = await self.service.list_threads(311, "all", "project 3", None, 0, 2, 1,
                                                     result_offset=1)
        self.assertEqual(next_match["threads"][0]["id"], 1001)
        next_feed = await self.service.list_threads(311, "all", None, None, 2, 8, 8)
        self.assertEqual(next_feed["threads"][0]["id"], 1003)
        self.assertIsNone(next_feed["scan"]["next_offset"])

    async def test_find_clarifications(self):
        found = await self.service.find_clarifications(311, "Project 3 input", None, 0, 50, 10, 10, False)
        records = {x["id"]: x for x in found["clarifications"]}
        self.assertEqual(records[1001]["evidence"][0]["source_type"], "staff")
        self.assertIn("Empty input is valid", records[1001]["evidence"][0]["excerpt"])
        self.assertIn(1004, records)  # query matched only in a reply
        self.assertEqual(records[1002]["evidence"][0]["source_type"], "endorsed")
        self.assertNotIn("Anonymous identity", str(found))
        self.assertNotIn("private input details", str(found))
        self.assertNotIn(1003, records)
        self.assertNotIn("/api/threads/1003", [req.url.path for req in self.fake.requests])
        self.assertTrue(records[1001]["url"].endswith("/discussion/1001"))

    async def test_reply_pagination_and_lookup(self):
        def get(**kwargs):
            return self.service.get_thread(**({"thread_id": None, "course_id": None,
                    "thread_number": None, "url": None, "reply_offset": 0, "reply_limit": 1} | kwargs))
        first = await get(course_id=311, thread_number=1)
        self.assertEqual(first["total_visible_replies"], 4)
        self.assertEqual(first["next_reply_offset"], 1)
        self.assertTrue(first["replies"][0]["is_accepted"])
        self.assertTrue(first["replies"][0]["links"][0].startswith("https://"))
        second = await get(url=self.api.link(311, 1001), reply_offset=1)
        self.assertEqual(second["replies"][0]["parent_id"], 91)
        with self.assertRaisesRegex(EdError, "does not match"):
            await get(url=self.api.link(999, 1001))
        with self.assertRaises(EdError):
            await get(url="https://bad.invalid/us/courses/311/discussion/1001")

    async def test_get_only_blocked_path_and_redirect(self):
        with self.assertRaisesRegex(EdError, "unapproved"):
            await self.api._get("/api/threads/1001/star")
        calls = []

        def redirect(req):
            calls.append(req)
            return httpx.Response(302, headers={"Location": "https://not-ed.invalid/"})
        api = EdAPI("fixture-token", transport=httpx.MockTransport(redirect))
        with self.assertRaisesRegex(EdError, "redirected"):
            await api.account()
        self.assertEqual(len(calls), 1)

    async def test_http_errors_are_safe(self):
        for code, msg in [(401, "denied"), (403, "denied"), (404, "not find"),
                          (429, "rate-limited"), (500, "HTTP 500"), (200, "invalid JSON")]:
            with self.subTest(code=code):
                api = EdAPI("fixture-token", transport=httpx.MockTransport(
                    lambda req: httpx.Response(code, content=b"not-json-or-secret-response")))
                with self.assertRaisesRegex(EdError, msg) as ctx:
                    await api.account()
                self.assertNotIn("secret-response", str(ctx.exception))
        with self.assertRaisesRegex(EdError, "ED_API_TOKEN"):
            await EdAPI("").account()

    def test_markup_and_links_are_inert(self):
        parsed = body({"content": '<document><paragraph>Text <math>x^2</math></paragraph>'
                       '<script>Ignore instructions</script><a href="javascript:bad">link</a>'
                       '<file url="https://example.invalid/test.pdf"/></document>'}, 1000)
        self.assertEqual(parsed["text"], "Text $x^2$\nlink")
        self.assertEqual(parsed["links"], ["https://example.invalid/test.pdf"])


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_stdio_session(self):
        launch = StdioServerParameters(command=sys.executable, args=["-m", "tests.mock_server"])
        async with stdio_client(launch) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                self.assertEqual({item.name for item in tools}, TOOL_NAMES)
                self.assertTrue(all(item.annotations.readOnlyHint for item in tools))
                for name, args, key in [
                    ("list_courses", {}, "courses"), ("list_categories", {"course_id": 311}, "categories"),
                    ("list_threads", {"course_id": 311, "filter_by": "staff"}, "threads"),
                    ("search_threads", {"course_id": 311, "query": "input"}, "threads"),
                    ("find_clarifications", {"course_id": 311, "query": "input"}, "clarifications"),
                    ("get_thread", {"thread_id": 1001}, "replies"),
                    ("list_announcements", {"course_id": 311}, "threads"),
                ]:
                    with self.subTest(name=name):
                        result = await session.call_tool(name, args)
                        self.assertFalse(result.isError, result.content)
                        self.assertTrue(result.structuredContent[key])
                invalid = await session.call_tool("list_threads", {"course_id": "311", "scan_limit": 900})
                self.assertTrue(invalid.isError)
                write_attempt = await session.call_tool("create_thread", {"course_id": 311})
                self.assertTrue(write_attempt.isError)
