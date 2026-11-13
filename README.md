# Ed Discussion MCP · read only

[![Tests](https://github.com/RyGuy108/ed-discussion-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/RyGuy108/ed-discussion-mcp-server/actions/workflows/ci.yml)

A local Python MCP server for agents that need to look up what a course actually said. It uses an Ed personal access token with the Ed Discussion JSON API. It does not use browser cookies or scrape the discussion UI.

## Run it without an Ed account

Requires Python 3.11+ and a client that supports local MCP over stdio.

```bash
cd ed-discussion-mcp
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -v
.venv/bin/python smoke.py
```

On Windows use `.venv\Scripts\python.exe` in place of `.venv/bin/python`. Both commands after installation work offline. The tests start a real MCP stdio session with an in-process mock Ed transport. The demo uses invented threads and prints their tool responses.

## Connect to your account

1. Generate a personal access token at [Ed's US API Token settings](https://edstem.org/us/settings/api-tokens). The server reads `ED_API_TOKEN` at launch. Keep it in your MCP client's secret environment configuration rather than committing it, passing it on the command line, or pasting it into an agent prompt. Set `ED_REGION` to `au` or `eu` if needed. The default is `us`.
2. In a local MCP client, register the absolute path to the virtual environment's Python interpreter and the absolute path to `server.py`. This is a local stdio server, not a hosted ChatGPT connector. No port is opened.
3. Optionally test your credentials with `.venv/bin/python smoke.py --live` after setting the token in that shell's environment. Add `--course-id YOUR_COURSE_ID --query "Project 3 empty input"` to exercise a course lookup. This sends real Ed GETs and displays the selected results in your terminal.

Example config for clients that use an `mcpServers` JSON object. This assumes the client process already inherits the token. If your client does not inherit it, configure it through the client's environment or secret settings.

```json
{
  "mcpServers": {
    "ed-discussion": {
      "command": "/absolute/path/ed-discussion-mcp/.venv/bin/python",
      "args": ["/absolute/path/ed-discussion-mcp/server.py"],
      "env": { "ED_REGION": "us" }
    }
  }
}
```

## What an agent can do

| Tool | Use it to |
| --- | --- |
| `list_courses` | Resolve a class name to its course ID. Returns course names, codes, status and Ed links. |
| `list_categories` | Browse category and subcategory names. Uses course settings or labels observed on the first feed page if settings omit them. |
| `list_threads` | Browse threads by category, type or a screenshot filter. |
| `search_threads` | Rank keyword matches from titles, categories and any original post text the feed includes. |
| `get_thread` | Read a post, answers and nested comments with author roles and links. Takes an Ed URL, global ID, or course ID plus visible thread number. |
| `find_clarifications` | Look inside candidate threads for relevant original posts, answers and comments. Returns short evidence excerpts and Ed thread URLs. |
| `list_announcements` | Find recent announcements and identify pinned ones. |

Example prompt:

> In CS 314, did course staff clarify whether the assignment needs to handle empty input? Give me the answer, link the original Ed thread, and flag anything that is only a student's interpretation.

### What the harness covers

Run `python -m unittest discover -v` for the offline suite. It checks the real MCP
handshake and all seven tools, fixture accounts with student, tutor, staff,
instructor, admin and unknown roles, archived and empty course lists, regional
hosts, missing filter fields, anonymous authors, private replies, pagination,
reply-only matches, malformed responses, timeouts and rate limiting. GitHub Actions
runs the same tests and fixture smoke demo on Linux, macOS and Windows.

### Search and filters

- The screenshot filters are `all`, `unread`, `new_replies`, `unanswered`, `unresolved`, `endorsed`, `watching`, `starred`, `private`, `public`, `staff`, and `me`. They are computed from metadata Ed returns. Incomplete fields are reported as `filter_metadata_missing`. The Me filter does not guess authorship of anonymous posts. Public means non-private within Ed, not public on the web.
- Clarification search ranks staff-authored evidence first, then Ed-endorsed, accepted, and community evidence. Accepted is not the same as staff-approved. Search is keyword-based and bounded. A portion of the detail budget is reserved for likely reply-only matches, but some may still be missed. If a detail request is rate-limited, the tool stops after the in-flight batch and reports partial results and warnings. A missing result is not proof that staff never answered. Use a more specific query or increase the scan and detail budgets.
- `scan.next_offset` advances to a new feed window. `next_result_offset` pages through matches in the same window with the same arguments. The live feed can change between calls. Replies have their own `next_reply_offset`. Text is returned as inert excerpts or bounded plain text, not rendered HTML. Attachment links are returned if present but never fetched.
- Private threads and reply branches are skipped by clarification search unless explicitly requested. Direct reads and feed browsing can return private posts visible to your token. The server doesn't store or cache posts.

### Read-only guarantee and API limitations

The transport allows only four GET route patterns on fixed Ed regional hosts. It follows no redirects and exposes no generic HTTP tool. There are no tools for posting, replying, liking, marking seen, following, starring, editing, deleting, or changing settings. Ed itself may log requests or record views. The token may have broader permissions than this server uses. Chat and direct messages are not implemented.

The project uses the Ed API, but route and response details are based on community-documented API behavior and could change. The automated tests use fixtures. Live authentication and behavior for your institution are not verified until you run the live smoke test.

API references: [Ed token settings](https://edstem.org/us/settings/api-tokens), [Ed API beta notes from EPFL](https://www.epfl.ch/education/educational-initiatives/wp-content/uploads/2025/04/QUICKSTART_EdDiscussion_exercises_lectures.pdf), [edapi endpoint and response notes](https://github.com/smartspot2/edapi/blob/master/docs/api_docs.md), and [edstem Rust client's GET routes](https://docs.rs/edstem/latest/src/edstem/lib.rs.html).
