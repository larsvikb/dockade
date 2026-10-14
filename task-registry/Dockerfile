# SPDX-License-Identifier: Apache-2.0
# Task registry — the work list agent sessions and the operator share
# (task-registry/DESIGN.md). Not an egress choke point, so the base is pinned by tag
# like the gateway's and the control plane's. Build context is this directory.
FROM python:3.12-slim

# Non-root runtime user, and mostly a FALLBACK, as in tool-gateway/Dockerfile: compose
# runs this service as the invoking user so it can read its secret file, which is the
# host user's at mode 0600. 10004: each image keeps a uid of its own.
RUN useradd --system --create-home --home-dir /home/tasks --uid 10004 tasks

WORKDIR /opt/task-registry

# Deps first for layer caching; pinned in requirements.txt.
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# One line per module, as in control-plane/Dockerfile: an added module is a deliberate
# line here (tests/test_topology.py fails without it).
COPY serve.py ./serve.py
COPY taskdb.py ./taskdb.py
COPY tasks.py ./tasks.py
COPY claims.py ./claims.py
COPY notes.py ./notes.py
COPY plans.py ./plans.py
COPY reads.py ./reads.py
COPY agent_moves.py ./agent_moves.py
COPY operator_moves.py ./operator_moves.py
COPY agent_tools.py ./agent_tools.py
COPY agent_api.py ./agent_api.py
COPY operator_api.py ./operator_api.py
# Not served: run by `make backup` and `make restore-tasks` in a throwaway container.
COPY backup.py ./backup.py
COPY export.py ./export.py

# The store's directory, made in the IMAGE so a fresh named volume mounted here takes
# its mode. 1777 for the reason tool-gateway/Dockerfile gives its audit directory: the
# uid compose runs this as is known only at `up`. Only this service mounts the volume.
RUN mkdir -p /var/lib/task-registry && chmod 1777 /var/lib/task-registry

USER tasks
# 8110 the agent listener, 8111 the operator listener; where each binds is
# TASKS_AGENT_BIND and TASKS_OPERATOR_BIND (serve.py).
EXPOSE 8110 8111
# `python serve.py`, not `uvicorn`: the bind check runs before either listener opens.
ENTRYPOINT ["python", "serve.py"]
