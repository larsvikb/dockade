# SPDX-License-Identifier: Apache-2.0
# Tasks UI. A stateless relay + static server, on tasks-ui-net (host-loopback publish)
# and tasks-operator-net (to reach the task registry's operator listener), and on no
# other network. Base pinned by tag, as the control-plane UI's is. Built from the repo
# root, to copy ui-common/ (the browser guards and modules both UIs share) beside its
# own files; everything lands in one directory, where app.py and the page import by
# plain name.
FROM python:3.12-slim

# Non-root runtime user. The UI needs no privileges.
RUN useradd --system --create-home --home-dir /home/tasksui --uid 10005 tasksui

WORKDIR /opt/tasks-ui

COPY tasks-ui/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY tasks-ui/app.py ./app.py
COPY ui-common/guards.py ./guards.py
COPY tasks-ui/index.html ./index.html
# The page's modules, one line each: a test holds this list equal to UI_MODULES in
# app.py.
COPY tasks-ui/app.js ./app.js
COPY ui-common/escape.js ./escape.js
COPY ui-common/list-status.js ./list-status.js
COPY tasks-ui/moves.js ./moves.js
COPY tasks-ui/tasks.js ./tasks.js
COPY ui-common/time.js ./time.js

USER tasksui
EXPOSE 8091
# app.py serves itself, so it can refuse a bind before the socket opens (check_bind).
ENTRYPOINT ["python", "app.py"]
