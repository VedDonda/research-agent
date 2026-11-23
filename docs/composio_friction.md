# Composio friction log

Friction we hit while building on Composio, recorded as product-ops feedback. These are facts observed with SDK `composio==0.34.0` on 2026-11-23.

| # | What happened | Evidence | Impact |
|---|---|---|---|
| 1 | `toolkits.get()` returns at most 1,000 toolkits. The full catalogue (1,865) needs cursor paging through `client.toolkits.list(cursor=…)`. | The list came back with exactly 1,000 items; paging returned 1,865. | Easy to miss: "does Composio have a toolkit for X?" would give false "no"s. |
| 2 | `tools.get_raw_composio_tools(toolkits=["composio_search"])` returns `[]`, although the toolkit reports `tools_count=21`. The slugs only resolve one by one with `get_raw_composio_tool_by_slug`. | `COMPOSIO_SEARCH_DUCK_DUCK_GO`, `_WEB`, `_TAVILY`, `_FETCH_URL_CONTENT`, … were found by guessing. | You can't discover the tools from the SDK. |
| 3 | The docs page `docs.composio.dev/toolkits/composio_search` returns 404. The marketing page lists tool display names but no slugs. | — | Same as #2. |
| 4 | **Every `COMPOSIO_SEARCH` tool returns HTTP 410:** "Toolkit COMPOSIO_SEARCH version … is retired and its tools can no longer be executed." This happens for the latest version (`20261119_00`), which `toolkits.get()` still lists as current, and for older pinned versions, through both `tools.execute` and sessions. | 410 on `20261119_00`, `20261116_00`, `20261111_00`, `20261101_00`, `20260903_00`, `20260618_00`. | The plan's free search and scrape layer is gone. |
| 5 | Toolkits marked `instant: supported` (Firecrawl, You.com) return 403 "Instant accounts are not enabled for …". The SDK docstring says the project must allow Instant usage, and Instant calls are charged. | `FIRECRAWL_SEARCH`, `FIRECRAWL_SCRAPE` and all `YOUSEARCH_*` tools returned 403. | There's no zero-setup search; you need either your own third-party key or paid Instant usage. |
