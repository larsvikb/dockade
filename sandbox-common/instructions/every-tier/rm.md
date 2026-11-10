- **`rm` is aliased to `-I`.** One prompt for a recursive delete or three or more
  files, none for a single file — so ordinary removes work, and a bulk one from a
  non-interactive caller does nothing while **still exiting 0**. Use `rm -f` or
  `command rm` when deleting many at once, and confirm a destructive step by its
  result rather than by its exit status.
