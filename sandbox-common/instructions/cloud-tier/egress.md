- **Egress goes through the governed proxy or not at all.** Allowlisted hosts
  work normally. An unknown host is not refused outright — it is **held for a
  human decision**, so a request can hang until someone approves it or a timeout
  fires. Prefer `{{web_fetch_tool}}` over raw sockets, and expect a first request to a new
  host to be slow rather than to fail.
