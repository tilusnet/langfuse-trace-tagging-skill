---
name: langfuse-v4-trace-tagging
description: For Langfuse v4 ONLY (on Langfuse v3 use the langfuse-v3-trace-tagging skill instead). Add, remove and query tag-like labels on existing Langfuse traces/sessions. Use when the user asks to tag or label a Langfuse session, apply topic tags to a coding session, find traces by tag/label, or remove labels. Needed because Langfuse v4 tags are immutable after creation; labels are stored as categorical scores through a bundled helper script. Accepts an optional comma-separated list of suggested tags as its argument (e.g. `tag1,tag2`) to bias classification toward a known set.
---

# Langfuse Trace Tagging (Langfuse v4)

Gives existing Langfuse traces tag-like **labels** after the fact. In Langfuse v4 real tags are set at creation and can't be added, edited or removed afterwards, and re-ingesting a trace to change it creates duplicates. Langfuse's own guidance for classifying traces later is to use **scores**, so each label here is one **categorical score** named `tag`, with the tag string as its value (e.g. `langfuse:v4-upgrade`). Several labels on a trace are several scores. Real tags already on traces are untouched and still count in queries.

All Langfuse calls go through the bundled helper `scripts/lf_labels.py` (Python 3, standard library only; path relative to this skill's directory). Run it with `--help` for the full usage.

## Know before you start

- **Labels are scores, not tags.** They appear in the Langfuse Scores views and the filter bar (`scores.tag:<value>`; quote values containing `:` or spaces), **not** in the tag filter chips. Tell the user this once, up front.
- **Each label also gets a derived `tag-topic` score** (the part of the tag before its first colon, e.g. `langfuse:v4-upgrade` → `langfuse`; one per trace and topic, none for colon-less tags). Reason: the Langfuse UI shows only a handful of categorical values in its search bar and sidebar, so hundreds of full tags are hard to browse, while a few dozen topics fit. `tag` is the source of truth; `apply` and `remove` keep `tag-topic` in step, and `sync-topics` rebuilds it. Use the `topic:issue` convention so every label has a topic.
- **Labels can be removed.** Deletions are queued and Langfuse works through them in the background, about one label every 2 minutes, so a removal takes roughly 2 minutes per label to show. Request them all at once and just tell the user how long to expect.
- **Langfuse does not validate scores against traces**: a score for a trace ID that doesn't exist, or sits in another project, still succeeds. The helper checks trace IDs against the session first, and `info` shows which project the key pair belongs to — confirm it is the project that holds the session.
- **Never fetch trace input/output** (the `io` field group). It can contain secrets pasted during the session. The helper only requests IDs, names, tags and timestamps; if you ever need content to classify, look at it in the conversation rather than writing it to a file.

## 1. Credentials and version check

You need `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_HOST` (host including any base path, e.g. `https://example.org/svc/langfuse`) in the environment. Never print the secret key or have the user paste it into chat.

> **Claude Code**: if the variables are not already set, look for stored credentials (a memory/notes entry about Langfuse API credentials first). On macOS, check Keychain service `langfuse-trace-tagging` — the credential name is unchanged and shared with the v3 skill, so existing credentials work — with the attributes-only form `security find-generic-password -s "langfuse-trace-tagging"` (never `-g`); the account is the public key, the comment is a note and the generic attribute the host. Tell the user which entry you found and get confirmation, then pull the secret straight into the variable: `export LANGFUSE_SECRET_KEY=$(security find-generic-password -a "<public-key>" -s "langfuse-trace-tagging" -w)`. Otherwise (or on another platform) ask the user to write the three variables to a scratch file with their own editor, `source` it, and delete it afterwards; offer to store the pair in the platform's native secret store only with their explicit confirmation.

Then run:

```bash
python3 scripts/lf_labels.py info
```

If it reports **Langfuse 3.x**, stop: this skill is for v4. Use the `langfuse-v3-trace-tagging` skill.

## 2. Identify the session

Work out which session to label. If the user says "this session" or doesn't name one, offer the current session as the default and confirm it before using it.

> **Claude Code**: the current session's ID is the `CLAUDE_CODE_SESSION_ID` environment variable — the same value the `langfuse-observability` plugin sends as each trace's session ID. Say in one line why you are reading it, then name it and confirm (e.g. "Label the current session `<id>`?"). If the user names another session ID, use that instead.

## 3. List the session's traces

```bash
python3 scripts/lf_labels.py traces --session "<session-id>"          # table: n, start time, trace id, name, real tags, existing labels
python3 scripts/lf_labels.py traces --session "<session-id>" --json   # same, machine-readable
```

Use this to see turn numbers and trace IDs, and whether labels already exist. Pagination is handled for you.

## 4. Propose labels and get confirmation

- If the user passed a comma-separated suggested-tag list, try every trace against it first and don't force bad fits. Mark in the proposal which tags came from the suggestion and which are new, and say explicitly if a suggested tag matched nothing.
- Otherwise use a `topic:issue` convention, e.g. `service-name:short-issue-slug`. Tags are limited to 200 characters.
- Derive labels from the conversation you already have in context. Don't re-fetch trace content.
- Present a markdown table: trace/turn range → label(s) → one-line rationale. Get explicit confirmation or adjustments before applying.
- On long sessions, post short progress updates while you classify rather than going silent.

## 5. Apply

Write the confirmed labels to a JSON file, `{"<traceId>": ["tag-a", "tag-b"], ...}` (a scratch file in the working area, containing only trace IDs and tags), then:

```bash
python3 scripts/lf_labels.py apply --session "<session-id>" --labels labels.json --dry-run     # shows the plan, writes nothing
python3 scripts/lf_labels.py apply --session "<session-id>" --labels labels.json --undo-file undo.json
```

`apply` reads the labels that already exist, creates only the missing ones (plus the missing `tag-topic` scores, listed in the plan), and refuses trace IDs that aren't in the session, so re-running is safe. Add `--prune` to also delete labels on those traces that are **not** in the file (use it only when the user wants the file to be the complete label set). Show the user the dry-run plan before the real run, and tell them where the undo file is. Verify by re-running `traces` (new labels show within seconds; deletions take about 2 minutes per label), then give the user a link: the session view is `<host>/project/<projectId>/sessions/<sessionId>`, and `info` prints the project ID.

## 6. Query and remove labels

```bash
python3 scripts/lf_labels.py query --tag "topic:issue"                              # every trace labelled or tagged with it
python3 scripts/lf_labels.py query --tag a --tag b --match all [--session "<id>"]   # traces having both (default: any)
python3 scripts/lf_labels.py query --tag "topic:issue" --details                    # adds start time, session, trace name
python3 scripts/lf_labels.py query --tag claude-code --no-legacy-tags               # labels only
```

By default a query returns the union of **label scores and real tags**, so historic tags still count; each hit shows which kind it came from. Combining labels with other trace attributes (cost, model, time range) isn't one call: query labels first, then filter the returned trace IDs.

```bash
python3 scripts/lf_labels.py remove --undo-file undo.json                                   # revert a whole apply run
python3 scripts/lf_labels.py remove --session "<id>" --trace "<traceId>" --tag "topic:issue" # remove one label
```

Removing a single label also removes the trace's `tag-topic` score if no other label on that trace shares the topic; `--undo-file` reverts the topic scores that run created. 

```bash
python3 scripts/lf_labels.py query --topic langfuse [--topic litellm] [--match any|all]   # traces with any label in a topic (labels only; real tags have no topic)
python3 scripts/lf_labels.py sync-topics [--session "<id>"]                                # dry run: topic histogram + scores to create/delete
python3 scripts/lf_labels.py sync-topics --write                                           # apply it (back-fill, or repair drift)
python3 scripts/lf_labels.py sync-topics --only-topic docker --write                       # pilot: touch one topic only (repeatable)
```

`sync-topics` compares every `tag` score with the `tag-topic` scores and creates the missing ones; orphan or duplicate topic scores are deleted (slow, see above). It is a dry run unless `--write` is given: show the user the plan first.

**In the UI**: filter `tag-topic` in the sidebar (Categorical Scores) or with `scores.tag-topic:<topic>`, then narrow by `tag`.

Both removal forms queue all deletions at once; Langfuse works through them in the background, so tell the user to expect roughly 2 minutes per label before `traces`/`query` stop showing it.
