- **Egress goes through the governed proxy or not at all.** Allowlisted hosts
  work normally. An unknown host is not refused outright — it is **held for a
  human decision**, so a request can hang until someone approves it or a timeout
  fires. Prefer `{{web_fetch_tool}}` over raw sockets, and expect a first request to a new
  host to be slow rather than to fail. A refusal is either an answer or a wait. A
  403 from the proxy, with or without its `egress denied by policy` body, or a
  fetch the client calls blocked, is the answer: do not retry it. A 503 from the
  proxy, with or without its `egress not decided` body, a refusal shown only as
  refused, or a timeout on a new host is not a no: **retry once after a minute**,
  then say so rather than retry on.
