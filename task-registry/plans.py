# SPDX-License-Identifier: Apache-2.0
"""Plans: how a task will be done, written by the agent that refined it and approved
with the task. Each ``submit_plan`` adds a row and none is ever changed; a task's
current plan is its latest one not superseded. A plan's decisions are its questions for
the operator, and an answer is the operator's, given with the approve. The checks, the
plain writes and the shape each is read in; who may write a plan is in ``agent_moves``,
and only ``operator_moves`` writes an answer."""
from __future__ import annotations

import sqlite3
import time

import claims
import notes
import tasks
from taskdb import Refused

# The caps are the plan's shape as much as storage: a plan that does not fit is over-
# detailed, or more than one task (`task-registry/DESIGN.md`, "Plans").
TEXTS = {"approach": 4000, "tests": 2000, "out_of_scope": 1500, "risks": 1500,
         "review_verdict": 1000}

#: The most review rounds a refiner runs before handing a plan in: the operator's cap,
#: which `claim_plan`'s description states. The store does not apply it: the count is
#: the agent's own word, so a refusal would only push one that ran over into reporting
#: a false number. `REVIEW_ROUNDS_MAX` is a bound on what the store keeps.
REVIEW_ROUNDS_CAP = 5
REVIEW_ROUNDS_MAX = 20

#: A plan's decisions, capped as its parts are and for the same reason: more than a few
#: is judgement handed back.
DECISIONS_MAX = 5
OPTIONS_MIN, OPTIONS_MAX = 2, 5
DECISION_TEXTS = {"question": 300, "why": 1000}
OPTION_TEXTS = {"label": 120, "meaning": 600}
#: An answer's "other" text: the operator's own words, as long as a note may be.
OTHER_MAX = notes.NOTE_MAX

_DECISION_FIELDS = frozenset({"question", "why", "options", "assumes", "multi_select"})
_ANSWER_FIELDS = frozenset({"options", "other"})


def check(approach: object, footprint: object, tests: object, out_of_scope: object,
          risks: object, review_rounds: object, review_verdict: object) -> list[str]:
    """Refuse a plan missing a part or past its caps; return its footprint, normalized."""
    given = {"approach": approach, "tests": tests, "out_of_scope": out_of_scope,
             "risks": risks, "review_verdict": review_verdict}
    for name, limit in TEXTS.items():
        tasks.check_text(given[name], limit, f"a plan's {name}", required=True)
    if type(review_rounds) is not int or not 0 < review_rounds <= REVIEW_ROUNDS_MAX:
        raise Refused(f"review_rounds is how many times a fresh-context reviewer read "
                      f"the plan, 1 to {REVIEW_ROUNDS_MAX}")
    paths = claims.footprint(footprint)
    if "." in paths:
        raise Refused("a plan's footprint names where the change goes; '.' is the whole "
                      "repo: name its top-level directories if it truly spans them")
    return paths


def check_decisions(decisions: object) -> list[dict]:
    """Refuse decisions not in a decision's shape; return them as a read shows them,
    each option marked whether the plan as written assumes it."""
    if not isinstance(decisions, (list, tuple)) or len(decisions) > DECISIONS_MAX:
        raise Refused(f"decisions is a list of at most {DECISIONS_MAX}, empty when the "
                      f"body, the repo and its design settle everything")
    return [_check_decision(f"decisions[{i}]", d) for i, d in enumerate(decisions)]


def _check_decision(where: str, decision: object) -> dict:
    _check_fields(where, decision, _DECISION_FIELDS, _DECISION_FIELDS - {"multi_select"})
    for name, limit in DECISION_TEXTS.items():
        tasks.check_text(decision[name], limit, f"{where}.{name}", required=True)
    multi = tasks.check_flag(decision.get("multi_select", False), f"{where}.multi_select")
    options = decision["options"]
    if (not isinstance(options, (list, tuple))
            or not OPTIONS_MIN <= len(options) <= OPTIONS_MAX):
        raise Refused(f"{where}.options is a list of {OPTIONS_MIN} to {OPTIONS_MAX} "
                      f"options")
    for j, option in enumerate(options):
        _check_fields(f"{where}.options[{j}]", option, set(OPTION_TEXTS), set(OPTION_TEXTS))
        for name, limit in OPTION_TEXTS.items():
            tasks.check_text(option[name], limit, f"{where}.options[{j}].{name}",
                             required=True)
    labels = [o["label"] for o in options]
    # Told apart as the operator reads them, not only as stored.
    if len({label.strip().casefold() for label in labels}) != len(labels):
        raise Refused(f"{where}.options have labels that differ only in case or spacing")
    assumes = decision["assumes"]
    if (not isinstance(assumes, (list, tuple))
            or any(not isinstance(a, str) or a not in labels for a in assumes)
            or len(set(assumes)) != len(assumes)):
        raise Refused(f"{where}.assumes names options by their labels, each once")
    if not (len(assumes) >= 1 if multi else len(assumes) == 1):
        raise Refused(f"{where}.assumes names what the plan as written assumes: "
                      + ("at least one option of a multi-select decision" if multi
                         else "exactly one option, unless the decision is multi_select"))
    return {"question": decision["question"], "why": decision["why"],
            "multi_select": multi,
            "options": [{"label": o["label"], "meaning": o["meaning"],
                         "assumed": o["label"] in assumes} for o in options]}


def _check_fields(where: str, given: object, known: set | frozenset,
                  required: set | frozenset) -> None:
    if not isinstance(given, dict):
        raise Refused(f"{where} is an object of {', '.join(sorted(known))}")
    unknown = sorted(set(given) - known, key=str)
    if unknown:
        raise Refused(f"{where} has no field {', '.join(repr(k)[:60] for k in unknown)}; "
                      f"it takes {', '.join(sorted(known))}")
    missing = sorted(required - set(given))
    if missing:
        raise Refused(f"{where} is missing {', '.join(missing)}")


def check_answers(decisions: list[dict], answers: object) -> list[dict]:
    """Refuse answers that are not one per decision, each in its decision's shape;
    return them, options in order. A single-choice decision is answered by one option
    or an "other" text, never both; a multi-select by any of its options, an "other"
    text, or both, but not by nothing. ``decisions`` as a read shows them."""
    if not isinstance(answers, (list, tuple)) or len(answers) != len(decisions):
        raise Refused(f"answers is a list of {len(decisions)}, one per decision of the "
                      f"plan, in its order")
    checked = []
    for i, (decision, answer) in enumerate(zip(decisions, answers, strict=True)):
        where = f"answers[{i}]"
        _check_fields(where, answer, _ANSWER_FIELDS, frozenset())
        options, other = answer.get("options", ()), answer.get("other")
        if (not isinstance(options, (list, tuple))
                or any(type(o) is not int or not 0 <= o < len(decision["options"])
                       for o in options)
                or len(set(options)) != len(options)):
            raise Refused(f"{where}.options names options of decision {i} by position, "
                          f"0 to {len(decision['options']) - 1}, each once")
        tasks.check_text(other, OTHER_MAX, f"{where}.other")
        chosen = len(options) + (other is not None)
        if decision["multi_select"] and chosen == 0:
            raise Refused(f"{where} answers a multi-select decision with at least one "
                          f"of its options, an other text, or both")
        if not decision["multi_select"] and chosen != 1:
            raise Refused(f"{where} answers a single-choice decision with one option or "
                          f"an other text, not both")
        checked.append({"options": sorted(options), "other": other})
    return checked


def outcome(decisions: list[dict], answers: object) -> str:
    """The state an approve with ``answers`` takes its task to: ``open`` when every
    answer is exactly the options the plan assumes, with no "other" text, otherwise
    ``proposed``, for a plan written from the answers. Refuses what ``check_answers``
    does. Pure, so another surface can hold its own rule to this one."""
    checked = check_answers(decisions, answers)
    assumed = all(
        answer["other"] is None and answer["options"] == [
            j for j, option in enumerate(decision["options"]) if option["assumed"]]
        for decision, answer in zip(decisions, checked, strict=True))
    return "open" if assumed else "proposed"


def insert(conn: sqlite3.Connection, task: int, revision: int, author: str,
           fields: dict, paths: list[str], decisions: list[dict]) -> int:
    """Write a plan, its footprint and its decisions, as ``check`` and
    ``check_decisions`` returned them."""
    plan = conn.execute(
        "INSERT INTO plans(task_id, task_revision, author, created_at, approach, tests, "
        "out_of_scope, risks, review_rounds, review_verdict) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task, revision, author, time.time(), fields["approach"], fields["tests"],
         fields["out_of_scope"], fields["risks"], fields["review_rounds"],
         fields["review_verdict"])).lastrowid
    conn.executemany("INSERT INTO plan_paths(plan_id, path) VALUES (?, ?)",
                     [(plan, p) for p in paths])
    for i, decision in enumerate(decisions):
        row = conn.execute(
            "INSERT INTO decisions(plan_id, position, question, why, multi_select) "
            "VALUES (?, ?, ?, ?, ?)", (plan, i, decision["question"], decision["why"],
                                       int(decision["multi_select"]))).lastrowid
        conn.executemany(
            "INSERT INTO decision_options(decision_id, position, label, meaning, "
            "assumed) VALUES (?, ?, ?, ?, ?)",
            [(row, j, o["label"], o["meaning"], int(o["assumed"]))
             for j, o in enumerate(decision["options"])])
    return plan


def answer(conn: sqlite3.Connection, plan: int, answers: list[dict], author: str) -> None:
    """Write ``answers``, as ``check_answers`` returned them, to the plan's decisions.
    The operator's write: only ``operator_moves`` calls it."""
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM decisions WHERE plan_id = ? ORDER BY position", (plan,))]
    now = time.time()
    for decision, given in zip(ids, answers, strict=True):
        conn.execute("INSERT INTO answers(decision_id, author, created_at, other) "
                     "VALUES (?, ?, ?, ?)", (decision, author, now, given["other"]))
        conn.executemany("INSERT INTO answer_options(decision_id, position) VALUES (?, ?)",
                         [(decision, j) for j in given["options"]])


def current(conn: sqlite3.Connection, task: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM plans WHERE task_id = ? AND superseded_at IS NULL "
                        "ORDER BY id DESC LIMIT 1", (task,)).fetchone()


def paths(conn: sqlite3.Connection, plan: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT path FROM plan_paths WHERE plan_id = ? ORDER BY rowid", (plan,))]


def shown(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """A plan as a read returns it: data, each part apart, never spliced into the
    task's own text."""
    return {"id": row["id"], "task": row["task_id"], "task_revision": row["task_revision"],
            "author": row["author"], "at": row["created_at"],
            "approach": row["approach"], "footprint": paths(conn, row["id"]),
            "tests": row["tests"], "out_of_scope": row["out_of_scope"],
            "risks": row["risks"], "review_rounds": row["review_rounds"],
            "review_verdict": row["review_verdict"],
            "decisions": decisions(conn, row["id"]),
            "superseded_at": row["superseded_at"]}


def decisions(conn: sqlite3.Connection, plan: int) -> list[dict]:
    """A plan's decisions as a read shows them, in order: an option's position is its
    place in its list, which is how an answer names it."""
    found = []
    for d in conn.execute("SELECT * FROM decisions WHERE plan_id = ? ORDER BY position",
                          (plan,)).fetchall():
        options = conn.execute("SELECT * FROM decision_options WHERE decision_id = ? "
                               "ORDER BY position", (d["id"],))
        found.append({"question": d["question"], "why": d["why"],
                      "multi_select": bool(d["multi_select"]),
                      "options": [{"label": o["label"], "meaning": o["meaning"],
                                   "assumed": bool(o["assumed"])} for o in options]})
    return found


def answered(conn: sqlite3.Connection, plan: int) -> bool:
    return conn.execute("SELECT 1 FROM answers a JOIN decisions d ON d.id = a.decision_id "
                        "WHERE d.plan_id = ? LIMIT 1", (plan,)).fetchone() is not None


def unanswered(conn: sqlite3.Connection, plan: int) -> int:
    """How many of the plan's decisions have no answer."""
    return conn.execute("SELECT COUNT(*) FROM decisions d WHERE d.plan_id = ? AND NOT "
                        "EXISTS (SELECT 1 FROM answers a WHERE a.decision_id = d.id)",
                        (plan,)).fetchone()[0]


def answers(conn: sqlite3.Connection, task: int) -> list[dict]:
    """Every answer the task's plans have had, the latest plan's first and each plan's
    in its decisions' order, with the question and the options chosen in the text the
    operator saw. Unpaged: each plan has at most ``DECISIONS_MAX``."""
    found = []
    for a in conn.execute(
            "SELECT a.*, d.plan_id, d.position, d.question FROM answers a "
            "JOIN decisions d ON d.id = a.decision_id JOIN plans p ON p.id = d.plan_id "
            "WHERE p.task_id = ? ORDER BY d.plan_id DESC, d.position", (task,)).fetchall():
        chosen = conn.execute(
            "SELECT o.* FROM answer_options c JOIN decision_options o "
            "ON o.decision_id = c.decision_id AND o.position = c.position "
            "WHERE c.decision_id = ? ORDER BY o.position", (a["decision_id"],))
        found.append({"plan": a["plan_id"], "decision": a["position"],
                      "question": a["question"],
                      "options": [{"position": o["position"], "label": o["label"],
                                   "meaning": o["meaning"]} for o in chosen],
                      "other": a["other"], "author": a["author"], "at": a["created_at"]})
    return found


def latest(conn: sqlite3.Connection, task: int) -> dict | None:
    """The current plan, or with none the latest superseded one, which is what the next
    refiner works from; ``None`` for a task never planned. The latest row is the current
    one whenever there is one: superseding marks every plan a task has so far."""
    row = conn.execute("SELECT * FROM plans WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                       (task,)).fetchone()
    return None if row is None else shown(conn, row)


def every(conn: sqlite3.Connection, task: int) -> list[dict]:
    return [shown(conn, r) for r in conn.execute(
        "SELECT * FROM plans WHERE task_id = ? ORDER BY id DESC", (task,))]


def overlaps(conn: sqlite3.Connection, repo_id: int, task: int,
             mine: list[str]) -> list[dict]:
    """The current plans of the repo's other refined and open tasks whose footprint
    overlaps ``mine``. Advisory, as a claim's overlaps are."""
    found = []
    for other in conn.execute(
            "SELECT p.id, p.task_id, t.title, t.state FROM plans p "
            "JOIN tasks t ON t.id = p.task_id WHERE t.repo_id = ? AND p.task_id != ? "
            "AND p.superseded_at IS NULL AND t.state IN ('refined', 'open') "
            "ORDER BY p.task_id", (repo_id, task)):
        shared = claims.shared(paths(conn, other["id"]), mine)
        if shared:
            found.append({"task": other["task_id"], "title": other["title"],
                          "state": other["state"], "plan": other["id"], "paths": shared})
    return found
