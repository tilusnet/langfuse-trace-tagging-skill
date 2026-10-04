#!/usr/bin/env python3
"""
lf_labels.py - tag-like labels on existing Langfuse traces, for Langfuse v4 (events_only).

Why this exists: in Langfuse v4, trace/observation tags are immutable (set at creation only), and re-ingesting
a trace to add a tag creates duplicates. Langfuse's own guidance for classifying traces *after* they were
created is to use scores. This tool stores each label as one CATEGORICAL score:

    name = "tag"  (configurable with --name)      value = the tag string, e.g. "langfuse:v4-upgrade"

Several labels per trace are several scores with the same name. Unlike real tags they can be removed.

Each label also gets a DERIVED second score for UI filtering (the Langfuse UI caps how many categorical values it
lists, so hundreds of full tags are hard to browse):

    name = "tag-topic"  (configurable with --topic-name)    value = the part of the tag before its first colon, e.g. "langfuse"

One topic score per (trace, topic), however many tags share it. Tags without a colon get no topic. `tag` is the
source of truth; `sync-topics` rebuilds `tag-topic` from it.

Verified behaviour this tool is built around (Langfuse v4.50.0 self-hosted):
  * `timestamp` in the POST /api/public/scores body is IGNORED (the server stamps its own). A score is identified
    by id + name + DATE, so re-sending on a later day does NOT overwrite - it duplicates. Hence `apply` reads the
    existing labels and creates only the missing ones (it never relies on overwrite).
  * Scores are not validated against the project: a score for a trace id that does not exist still succeeds.
    Hence `apply` verifies trace ids against the session first.
  * DELETE /api/public/scores/{id} returns 202 and is processed by a worker in batches: removal can take up to
    to show: each API delete is its own queue job and the worker runs one job per 2 minutes by default,
    so N API removals take ~2N minutes (the UI's multi-select delete uses a single job for all selected scores).
  * Reads never request the `io` field group (trace input/output), only ids/names/tags.

Credentials come from the environment, never from arguments:
    LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST   (host incl. any base path, e.g. https://x/svc/langfuse)

Usage:
    lf_labels.py info
    lf_labels.py traces  --session SID [--json]
    lf_labels.py apply   --session SID --labels labels.json [--prune] [--dry-run] [--undo-file undo.json]
    lf_labels.py remove  --undo-file undo.json            |  --session SID --trace TID --tag TAG
    lf_labels.py query   --tag A [--tag B] [--match any|all] [--session SID] [--no-legacy-tags] [--details]
    lf_labels.py query   --topic T [--topic U] [--match any|all] [--session SID] [--details]
    lf_labels.py sync-topics [--session SID] [--only-topic T] [--write]       (dry run unless --write)

labels.json:  {"<traceId>": ["tag-a", "tag-b"], ...}
"""
import argparse
import base64
import collections
import concurrent.futures
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_NAME = "tag"
DEFAULT_TOPIC_NAME = "tag-topic"
SOURCE = "langfuse-v4-trace-tagging"
MAX_TAG_LEN = 200  # Langfuse's own limit for real tags; keep labels compatible with it
SCORES_PAGE = 100  # v3 scores API max page size
OBS_PAGE = 1000    # observations v2 max page size
TRACE_CHUNK = 20   # trace ids per scores query (keeps URLs short)


# ----------------------------------------------------------------------------- HTTP
def _creds():
    try:
        return (os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"], os.environ["LANGFUSE_HOST"].rstrip("/"))
    except KeyError as e:
        sys.exit(f"missing environment variable {e}; source the Langfuse credentials first (see SKILL.md step 1)")


def http(method, path, params=None, body=None, auth=True, retries=3):
    pk, sk, host = _creds()
    url = host + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{pk}:{sk}".encode()).decode()
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            return e.code, _safe_json(e.read().decode())
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise


def _safe_json(s):
    try:
        return json.loads(s)
    except Exception:
        return {"raw": s[:300]}


# ----------------------------------------------------------------------------- reads
def server_version():
    st, d = http("GET", "/api/public/health", auth=False)
    return (d or {}).get("version") if st == 200 else None


def session_roots(session_id):
    """Root observations (one per trace) of a session, oldest first. Never requests io."""
    rows, cursor = [], None
    while True:
        params = {"sessionId": session_id, "isRootObservation": "true", "fields": "basic,trace_context", "limit": OBS_PAGE}
        if cursor:
            params["cursor"] = cursor
        st, d = http("GET", "/api/public/v2/observations", params)
        if st != 200:
            sys.exit(f"observations v2 read failed: HTTP {st} {json.dumps(d)[:200]}")
        rows += d.get("data", [])
        cursor = (d.get("meta") or {}).get("cursor")
        if not cursor:
            break
    # one root per trace: keep the earliest row per traceId
    by_trace = {}
    for r in sorted(rows, key=lambda r: r.get("startTime") or ""):
        by_trace.setdefault(r["traceId"], r)
    return sorted(by_trace.values(), key=lambda r: r.get("startTime") or "")


def label_scores(trace_ids, name):
    """All label scores (name=<name>) for the given traces -> list of {id, traceId, value}."""
    out = []
    ids = list(trace_ids)
    for i in range(0, len(ids), TRACE_CHUNK):
        chunk = ids[i:i + TRACE_CHUNK]
        cursor = None
        while True:
            params = {"traceId": ",".join(chunk), "name": name, "dataType": "CATEGORICAL", "fields": "subject", "limit": SCORES_PAGE}
            if cursor:
                params["cursor"] = cursor
            st, d = http("GET", "/api/public/v3/scores", params)
            if st != 200:
                sys.exit(f"scores v3 read failed: HTTP {st} {json.dumps(d)[:200]}")
            for s in d.get("data", []):
                subj = s.get("subject") or {}
                out.append({"id": s["id"], "traceId": subj.get("id") or subj.get("traceId"), "value": s.get("value")})
            cursor = (d.get("meta") or {}).get("cursor")
            if not cursor:
                break
    return out


def labels_by_trace(trace_ids, name):
    m = {}
    for s in label_scores(trace_ids, name):
        m.setdefault(s["traceId"], []).append(s)
    return m


# ----------------------------------------------------------------------------- commands
def cmd_info(a):
    ver = server_version()
    print(f"Langfuse version: {ver or 'unknown'}")
    st, d = http("GET", "/api/public/projects")
    if st == 200:
        print("Project(s) for this key pair:", ", ".join(f"{p.get('name')} ({p.get('id')})" for p in d.get("data", [])))
    major = int(ver.split(".")[0]) if ver and ver[0].isdigit() else None
    if major is None:
        print("-> could not determine the major version")
    elif major >= 4:
        print("-> v4: tags are immutable after creation; this tool (the langfuse-v4-trace-tagging skill) stores labels as scores.")
    else:
        print("-> v3: not supported by this tool. Use the langfuse-v3-trace-tagging skill (real tags) instead.")


def cmd_traces(a):
    roots = session_roots(a.session)
    labels = labels_by_trace([r["traceId"] for r in roots], a.name)
    rows = []
    for n, r in enumerate(roots, 1):
        rows.append({"n": n, "traceId": r["traceId"], "startTime": r.get("startTime"), "traceName": r.get("traceName") or r.get("name"),
                     "tags": r.get("tags") or [], "labels": sorted(s["value"] for s in labels.get(r["traceId"], []))})
    if a.json:
        print(json.dumps(rows, indent=1))
        return
    print(f"{len(rows)} trace(s) in session {a.session}")
    for r in rows:
        print(f"{r['n']:>3}  {r['startTime']}  {r['traceId']}  {r['traceName']}  tags={r['tags']}  labels={r['labels']}")


def _validate_tag(t):
    if not isinstance(t, str) or not t.strip() or t != t.strip():
        sys.exit(f"invalid tag {t!r}: must be a non-empty string without leading/trailing spaces")
    if len(t) > MAX_TAG_LEN:
        sys.exit(f"tag too long ({len(t)} > {MAX_TAG_LEN}): {t[:40]}...")


def label_id(name, trace_id, tag):
    return "lbl-" + hashlib.sha1(f"{name}\x1f{trace_id}\x1f{tag}".encode()).hexdigest()[:40]


def topic_of(tag):
    """The part of a tag before its first colon, or None for a colon-less tag (or an empty topic)."""
    return tag.split(":", 1)[0] or None if ":" in tag else None


def _post_label(name, trace_id, value, run, derived=False):
    sid = label_id(name, trace_id, value)
    meta = {"source": SOURCE, "run": run}
    if derived:
        meta["derived"] = True
    st, d = http("POST", "/api/public/scores", body={"id": sid, "traceId": trace_id, "name": name, "value": value,
                                                      "dataType": "CATEGORICAL", "metadata": meta})
    return sid, st, d


def _delete_scores(ids):
    ok = 0
    for i in ids:
        st, _ = http("DELETE", f"/api/public/scores/{urllib.parse.quote(i)}")
        ok += st in (200, 202)
    return ok


def cmd_apply(a):
    wanted = json.load(open(a.labels))
    if not isinstance(wanted, dict):
        sys.exit("labels file must be a JSON object {traceId: [tags]}")
    for t, tags in wanted.items():
        for tag in tags:
            _validate_tag(tag)
    roots = session_roots(a.session)
    valid = {r["traceId"] for r in roots}
    unknown = [t for t in wanted if t not in valid]
    if unknown:
        sys.exit(f"{len(unknown)} trace id(s) are not in session {a.session} (scores are NOT validated by Langfuse, so refusing): {unknown[:5]}")
    existing = labels_by_trace(list(wanted), a.name)
    existing_topics = labels_by_trace(list(wanted), a.topic_name)
    run = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    to_create, to_delete = [], []
    topic_create, topic_delete = [], []
    for t, tags in wanted.items():
        have = {}
        for s in existing.get(t, []):
            have.setdefault(s["value"], []).append(s["id"])
        for tag in dict.fromkeys(tags):  # de-dup, keep order
            if tag not in have:
                to_create.append((t, tag))
        if a.prune:
            for val, ids in have.items():
                if val not in tags:
                    to_delete += [(t, val, i) for i in ids]
                else:
                    to_delete += [(t, val, i) for i in ids[1:]]  # duplicate copies of a wanted label
        # derived topic scores: one per (trace, topic); with --prune the file is the complete label set
        desired = {topic_of(x) for x in (tags if a.prune else list(have) + list(tags))} - {None}
        topic_have = {}
        for s in existing_topics.get(t, []):
            topic_have.setdefault(s["value"], []).append(s["id"])
        topic_create += [(t, tp) for tp in sorted(desired) if tp not in topic_have]
        if a.prune:
            for val, ids in topic_have.items():
                if val not in desired:
                    topic_delete += [(t, val, i) for i in ids]
                else:
                    topic_delete += [(t, val, i) for i in ids[1:]]
    print(f"plan: {len(to_create)} label(s) to create, {len(to_delete)} to delete, "
          f"{sum(len(set(v)) for v in wanted.values()) - len(to_create)} already present, across {len(wanted)} trace(s)")
    print(f"      {len(topic_create)} topic score(s) to create, {len(topic_delete)} to delete")
    for t, tag in to_create[:50]:
        print(f"  + {t}  {tag}")
    for t, val, i in to_delete[:50]:
        print(f"  - {t}  {val}  ({i})")
    for t, tp in topic_create[:50]:
        print(f"  + {t}  [topic] {tp}")
    for t, val, i in topic_delete[:50]:
        print(f"  - {t}  [topic] {val}  ({i})")
    if a.dry_run or not (to_create or to_delete or topic_create or topic_delete):
        print("dry run / nothing to do." if a.dry_run else "nothing to do.")
        return

    def create(item):
        t, tag = item
        sid, st, d = _post_label(a.name, t, tag, run)
        return item, sid, st, d

    def create_topic(item):
        t, tp = item
        sid, st, d = _post_label(a.topic_name, t, tp, run, derived=True)
        return item, sid, st, d

    created, failed, created_topics, failed_topics = [], [], [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        for item, sid, st, d in ex.map(create, to_create):
            (created if st == 200 else failed).append((item, sid, st, d))
        for item, sid, st, d in ex.map(create_topic, topic_create):
            (created_topics if st == 200 else failed_topics).append((item, sid, st, d))
    deleted = _delete_scores([i for _, _, i in to_delete + topic_delete])
    print(f"created {len(created)} label(s) and {len(created_topics)} topic score(s), failed {len(failed) + len(failed_topics)}, "
          f"delete requests accepted {deleted}"
          + (f" (the worker deletes ~1 score per 2 min, so these take ~{2 * deleted} min to show)" if deleted else ""))
    for item, sid, st, d in (failed + failed_topics)[:10]:
        print(f"  FAILED {item} -> HTTP {st} {json.dumps(d)[:160]}")
    if a.undo_file:
        json.dump({"name": a.name, "topic_name": a.topic_name, "run": run,
                   "created": [{"traceId": it[0], "tag": it[1], "id": sid} for it, sid, _, _ in created],
                   "topics": [{"traceId": it[0], "topic": it[1], "id": sid} for it, sid, _, _ in created_topics]},
                  open(a.undo_file, "w"), indent=1)
        print(f"undo file written: {a.undo_file} (ids only; `remove --undo-file` reverts this run)")


def cmd_remove(a):
    ids = []
    if a.undo_file:
        u = json.load(open(a.undo_file))
        # topic scores in the file were created by that run because no same-topic tag existed (or no topic score) then
        ids = [c["id"] for c in u.get("created", [])] + [c["id"] for c in u.get("topics", [])]
    else:
        if not (a.session and a.trace and a.tag):
            sys.exit("remove needs --undo-file, or --session + --trace + --tag")
        if a.trace not in {r["traceId"] for r in session_roots(a.session)}:
            sys.exit("trace not in that session")
        tag_ids = [s["id"] for s in labels_by_trace([a.trace], a.name).get(a.trace, []) if s["value"] == a.tag]
        ids = list(tag_ids)
        tp = topic_of(a.tag)
        if tp and tag_ids:
            # deletes are async, so exclude the ones just requested when looking at what remains
            remaining = [s["value"] for s in labels_by_trace([a.trace], a.name).get(a.trace, []) if s["id"] not in set(tag_ids)]
            if not any(topic_of(v) == tp for v in remaining):
                ids += [s["id"] for s in labels_by_trace([a.trace], a.topic_name).get(a.trace, []) if s["value"] == tp]
                print(f"no other '{tp}:' label left on this trace: its {a.topic_name} score goes too")
    ok = _delete_scores(ids)
    print(f"delete requested for {ok}/{len(ids)} score(s); the worker deletes ~1 score per 2 min, so allow ~{2 * ok} min")


def _label_traces(tag, name):
    out, cursor = set(), None
    while True:
        params = {"name": name, "dataType": "CATEGORICAL", "value": tag, "fields": "subject", "limit": SCORES_PAGE}
        if cursor:
            params["cursor"] = cursor
        st, d = http("GET", "/api/public/v3/scores", params)
        if st != 200:
            sys.exit(f"scores v3 read failed: HTTP {st} {json.dumps(d)[:200]}")
        for s in d.get("data", []):
            subj = s.get("subject") or {}
            if subj.get("kind") == "trace":
                out.add(subj["id"])
            elif subj.get("traceId"):
                out.add(subj["traceId"])
        cursor = (d.get("meta") or {}).get("cursor")
        if not cursor:
            return out


def _legacy_tag_traces(tag, session):
    conds = [{"type": "arrayOptions", "column": "tags", "operator": "any of", "value": [tag]},
             {"type": "boolean", "column": "isRootObservation", "operator": "=", "value": True}]
    if session:
        conds.append({"type": "string", "column": "sessionId", "operator": "=", "value": session})
    out, cursor = set(), None
    while True:
        params = {"filter": json.dumps(conds), "fields": "basic", "limit": OBS_PAGE}
        if cursor:
            params["cursor"] = cursor
        st, d = http("GET", "/api/public/v2/observations", params)
        if st != 200:
            sys.exit(f"observations v2 read failed: HTTP {st} {json.dumps(d)[:200]}")
        out |= {r["traceId"] for r in d.get("data", [])}
        cursor = (d.get("meta") or {}).get("cursor")
        if not cursor:
            return out


def cmd_sync_topics(a):
    """Rebuild the derived topic scores from the tag scores (dry run unless --write)."""
    scope = {r["traceId"] for r in session_roots(a.session)} if a.session else None
    tags, topics = {}, {}
    for name, into in ((a.name, tags), (a.topic_name, topics)):
        for s in _all_scores(name):
            if scope is None or s["traceId"] in scope:
                into.setdefault(s["traceId"], []).append(s)
    run = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    create, delete = [], []
    for t in sorted(set(tags) | set(topics)):
        desired = {topic_of(s["value"]) for s in tags.get(t, [])} - {None}
        have = {}
        for s in topics.get(t, []):
            have.setdefault(s["value"], []).append(s["id"])
        if a.only_topic:  # pilot: leave every other topic alone
            desired &= set(a.only_topic)
            have = {k: v for k, v in have.items() if k in a.only_topic}
        create += [(t, tp) for tp in sorted(desired) if tp not in have]
        for val, ids in have.items():
            delete += [(t, val, i) for i in (ids if val not in desired else ids[1:])]
    n_untopiced = sum(1 for ss in tags.values() for s in ss if topic_of(s["value"]) is None)
    pairs = collections.Counter(tp for ss in ([{topic_of(s["value"]) for s in tags.get(t, [])} - {None} for t in tags]) for tp in ss)
    print(f"{sum(len(v) for v in tags.values())} {a.name} score(s) on {len(tags)} trace(s) -> {sum(pairs.values())} (trace, topic) pair(s) "
          f"over {len(pairs)} topic(s); {n_untopiced} colon-less tag(s) get no topic")
    for tp, n in pairs.most_common():
        print(f"  {n:>5}  {tp}")
    print(f"plan: {len(create)} topic score(s) to create, {len(delete)} orphan/duplicate topic score(s) to delete "
          f"({sum(len(v) for v in topics.values())} exist now)")
    for t, tp in create[:20]:
        print(f"  + {t}  {tp}")
    for t, val, i in delete[:20]:
        print(f"  - {t}  {val}  ({i})")
    if not (create or delete):
        print("in sync, nothing to do.")
        return
    if not a.write:
        print("dry run: re-run with --write to apply.")
        return

    def mk(item):
        sid, st, d = _post_label(a.topic_name, item[0], item[1], run, derived=True)
        return item, st, d

    ok, failed = 0, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        for item, st, d in ex.map(mk, create):
            if st == 200:
                ok += 1
            else:
                failed.append((item, st, d))
    deleted = _delete_scores([i for _, _, i in delete])
    print(f"created {ok}, failed {len(failed)}, delete requests accepted {deleted}"
          + (f" (~{2 * deleted} min to show)" if deleted else ""))
    for item, st, d in failed[:10]:
        print(f"  FAILED {item} -> HTTP {st} {json.dumps(d)[:160]}")


def _all_scores(name):
    """Every categorical trace-level score called `name` -> list of {id, traceId, value}."""
    out, cursor = [], None
    while True:
        params = {"name": name, "dataType": "CATEGORICAL", "fields": "subject", "limit": SCORES_PAGE}
        if cursor:
            params["cursor"] = cursor
        st, d = http("GET", "/api/public/v3/scores", params)
        if st != 200:
            sys.exit(f"scores v3 read failed: HTTP {st} {json.dumps(d)[:200]}")
        for s in d.get("data", []):
            subj = s.get("subject") or {}
            tid = subj["id"] if subj.get("kind") == "trace" else subj.get("traceId")
            if tid:
                out.append({"id": s["id"], "traceId": tid, "value": s.get("value")})
        cursor = (d.get("meta") or {}).get("cursor")
        if not cursor:
            return out


def cmd_query(a):
    if bool(a.tag) == bool(a.topic):
        sys.exit("query needs --tag or --topic (not both)")
    keys, name = (a.topic, a.topic_name) if a.topic else (a.tag, a.name)
    legacy_on = bool(a.tag) and not a.no_legacy_tags  # real tags have no topic scores
    per_tag = {}
    for tag in keys:
        s = _label_traces(tag, name)
        legacy = _legacy_tag_traces(tag, a.session) if legacy_on else set()
        per_tag[tag] = (s, legacy)
    sets = [l | t for l, t in per_tag.values()]
    hits = set.intersection(*sets) if a.match == "all" else set.union(*sets)
    if a.session:  # label scores are not session-filterable together with traceId lists; restrict via the session's traces
        hits &= {r["traceId"] for r in session_roots(a.session)}
    print(f"{len(hits)} trace(s) match {a.match} of {keys}" + (" (labels + real tags)" if legacy_on else " (topics, from labels only)" if a.topic else " (labels only)"))
    for t in sorted(hits):
        src = ",".join(f"{tag}:{'label' if t in per_tag[tag][0] else ''}{'+' if t in per_tag[tag][0] and t in per_tag[tag][1] else ''}{'tag' if t in per_tag[tag][1] else ''}"
                       for tag in keys if t in per_tag[tag][0] | per_tag[tag][1])
        line = f"  {t}  [{src}]"
        if a.details:
            st, d = http("GET", "/api/public/v2/observations", {"traceId": t, "isRootObservation": "true", "fields": "basic,trace_context", "limit": 1})
            r = (d.get("data") or [{}])[0] if st == 200 else {}
            line += f"  {r.get('startTime')}  session={r.get('sessionId')}  {r.get('traceName')}"
        print(line)


# ----------------------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", default=DEFAULT_NAME, help="score name used for labels (default: tag)")
    p.add_argument("--topic-name", default=DEFAULT_TOPIC_NAME, help="score name for the derived topic scores (default: tag-topic)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info").set_defaults(fn=cmd_info)
    s = sub.add_parser("traces"); s.add_argument("--session", required=True); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_traces)
    s = sub.add_parser("apply"); s.add_argument("--session", required=True); s.add_argument("--labels", required=True)
    s.add_argument("--prune", action="store_true", help="also delete labels on these traces that are not in the file")
    s.add_argument("--dry-run", action="store_true"); s.add_argument("--undo-file"); s.set_defaults(fn=cmd_apply)
    s = sub.add_parser("remove"); s.add_argument("--undo-file"); s.add_argument("--session"); s.add_argument("--trace"); s.add_argument("--tag"); s.set_defaults(fn=cmd_remove)
    s = sub.add_parser("query"); s.add_argument("--tag", action="append"); s.add_argument("--topic", action="append"); s.add_argument("--match", choices=("any", "all"), default="any")
    s.add_argument("--session"); s.add_argument("--no-legacy-tags", action="store_true"); s.add_argument("--details", action="store_true"); s.set_defaults(fn=cmd_query)
    s = sub.add_parser("sync-topics"); s.add_argument("--session"); s.add_argument("--write", action="store_true", help="apply the plan (default: dry run)")
    s.add_argument("--only-topic", action="append", help="restrict creates/deletes to this topic (repeatable), e.g. for a pilot")
    s.set_defaults(fn=cmd_sync_topics)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
