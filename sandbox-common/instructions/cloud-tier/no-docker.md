- **No Docker.** `make lint`, `make consistency` and `make test` all work here.
  `make verify-build`, `make check-boundary` and anything driving `docker
  compose` do not — they need the host, so ask rather than working around them.
