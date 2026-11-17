- **`/workspace/<folder-name>` is the human's checkout, live.** It is mounted under
  its host folder's name, and the session starts in it. Commits, branch switches and
  edits appear outside the container immediately; switching branches moves their
  working tree too. Say so when you do it. Anything written beside it, in
  `/workspace` itself, is lost when the container exits.
