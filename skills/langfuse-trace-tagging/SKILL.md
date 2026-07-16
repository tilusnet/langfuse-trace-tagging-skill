---
name: langfuse-trace-tagging
description: Tag Langfuse traces/sessions via the ingestion API. Use when the user asks to tag a Langfuse session, review/apply topic tags for a coding session, or manage trace tags. Handles credential discovery/setup generically, and works around Langfuse's tags-are-append-only-union API limitation by always getting a proposed tag table confirmed before applying anything (since applied tags cannot be removed or replaced).
---

# Langfuse Trace Tagging

Tags a Langfuse session's traces via the ingestion API. Read this whole skill before doing anything — the tag-mutation limitation in step 2 changes how you should approach every step after it.

This skill is written to be agent-agnostic: each step states the underlying requirement first, with a **concrete illustrative implementation for Claude Code** underneath. If you're a different agent (or maintaining this skill for one), keep the requirement and swap in your own agent's equivalent mechanism — e.g. whatever persistent-notes/memory feature, secret-storage convention, or confirmation-gating pattern it has. Contributions adding callouts for other agents are welcome.

## 1. Credential discovery

**Requirement**: before asking the user to set anything up from scratch, check whether Langfuse API credentials for this instance are already known/persisted somewhere accessible to you. If not, guide the user through first-time setup without ever having them paste the secret key directly into chat, and without silently persisting the secret anywhere without their explicit confirmation of *how* (not just *that*) it'll be stored.

> **Claude Code**: these are three *sequential, mandatory* attempts — try each one for real and only fall through to the next after the current one has actually failed. Do not skip ahead (e.g. do not jump straight to step 3's "ask the user to write both keys to a scratch file" just because step 1's memory lookup failed — step 2's Keychain check must be attempted first, every time, even without already knowing the public key).
> 1. Check memory for a `reference`-type entry documenting Langfuse API credentials (search for something like "langfuse api credentials"). If found, follow its retrieval instructions as written. Memory is scoped per-project, so this can legitimately miss even when credentials already exist elsewhere (e.g. in Keychain from a previous project) — that's expected, not a reason to skip step 2.
> 2. Otherwise, before asking the user to type anything, check whether Keychain already has an entry under this skill's service name — without needing the public key up front. First, tell the user in one short line what you're about to do and why (e.g. "Checking macOS Keychain for an existing `langfuse-trace-tagging` credential entry — you may see a permission prompt from Keychain itself, that's expected and safe to allow"), since the OS-level access prompt this command can trigger otherwise reads as unexplained and alarming. Then run (note: no `-g`/`-w` here — this deliberately omits the secret from output; adding either flag would print the password itself to the visible tool output/transcript, not just the account):
>    ```bash
>    security find-generic-password -s "langfuse-trace-tagging"
>    ```
>    This prints the item's attributes, including `"acct"` (the public key, safe to show) and `"icmt"` (comment — this skill's convention stores the Langfuse **host URL** here, since Keychain generic-password items have no dedicated host field). If `icmt` is populated, that's the host — no need to ask the user for it. If an entry is found, tell the user which public key (and host, if present in `icmt`) it's under and ask them to confirm it's the right one to use — do not silently assume a found entry is the right one, since a machine could plausibly have entries for more than one Langfuse instance. Only ask for the host directly if `icmt` is empty (an older entry saved before this convention existed). If confirmed, retrieve the secret for real use — only now, with a specific known-good account, is `-w` safe to add:
>    ```bash
>    security find-generic-password -a "<confirmed-public-key>" -s "langfuse-trace-tagging" -w
>    ```
>    Then — since this path bypassed step 1's memory lookup — offer to save a `reference` memory entry now (see step 3's last bullet) so a future session can skip straight to step 1, and stop here; do not proceed to step 3. If the user says it's the wrong key pair (or no entry was found at all), ask them for the correct public key and host URL, then retry the `-a "<public-key>" ... -w` lookup for that specific account before falling through to step 3.
> 3. Only if no matching Keychain entry exists for the confirmed public key, run first-time setup:
>    - Ask the user to save `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and the Langfuse host URL into a local scratch file using their own editor, then tell you the file path.
>    - `source` that file into environment variables — never echo the secret value in any visible command or output.
>    - **Explicitly ask the user for confirmation before writing anything to Keychain.** Do not treat this as implied by earlier instructions to "set up credentials" — a Keychain write is a new, distinct action worth its own confirmation.
>    - Once confirmed, write it with the host tucked into the comment field so future Keychain-only lookups (step 2) don't need to ask for it again:
>      ```bash
>      security add-generic-password -a "<public-key>" -s "langfuse-trace-tagging" -w "<secret-key>" -j "<host-url>"
>      ```
>    - Offer to save a `reference` memory entry (e.g. named `langfuse-api-credentials`) documenting the public key, the host URL, and the Keychain service/account convention used (including that host lives in the comment field), so a future session can rediscover it without repeating this setup. Then delete the temp credentials file.

Throughout, regardless of agent: never print the secret key value in a visible command. Always source it from a file or secret store straight into an environment variable.

## 2. The tag limitation you must design around

**Langfuse trace tags can be added via the API, but never removed or replaced.** Confirmed via Langfuse's own issue tracker — a request to support tag updates/removal was closed "not planned" (`langfuse/langfuse#8937`), and multiple earlier feature requests for the same thing went unaddressed. The underlying mechanism: repeated tag-bearing events for the same trace ID are merged as a **unique union**, not a replace — there is structurally no way to represent "this tag was removed."

Practical consequence: **once you apply a tag to a trace, it is there permanently**, short of destructively deleting and recreating the trace (real data loss — you'd lose its spans, timing, and I/O too). A "tag broadly now, narrow it down later" approach does not work — the narrower second pass just unions with the first and nothing shrinks.

**Hard rule that follows from this**: always build and show the user a full proposed tag table and get explicit confirmation *before* calling the ingestion API. Never apply anything "provisionally." If the user later asks to reduce or correct an already-applied tag set, tell them plainly that it isn't possible via API/SDK/UI, and the only paths are living with the current tags or destructively rebuilding the traces.

## 3. Identify which session to tag

**Requirement**: work out which Langfuse session the tags apply to before building the proposal, offering the ongoing/current session as a convenient default when the user's request is ambiguous about which session they mean.

> **Claude Code**: if the user says something like "tag this session," "tag this conversation," or otherwise doesn't name a specific session, offer the **current session** as the likely target rather than making them go look up an ID:
> - The current session's ID is available directly as the `CLAUDE_CODE_SESSION_ID` environment variable. This is the exact same value the `langfuse-observability` plugin (if installed) sends as each trace's `session_id` field with no transformation — so it's safe to pass straight through as `--session-id` in step 5's lookup.
> - Still name it and confirm before using it (e.g. "Tag the current session (`<value of CLAUDE_CODE_SESSION_ID>`)?") — don't assume silently, since "this session" is sometimes said about a past/different session shown on screen.
> - If the user names a specific session ID, or points you at one in the Langfuse UI, use that instead of the env var.
> - The credentials this skill uses (step 1) only need to belong to the *same Langfuse project* as the session being tagged — a project can have more than one valid key pair, so the key pair that originally ingested the traces does **not** need to match the one this skill uses to tag them.

## 4. Propose tags for confirmation

- Use a `topic:issue` naming convention, e.g. `service-name:short-issue-slug`.
- Derive proposed tags from the conversation content you already have in context — do not re-fetch raw trace input/output to do this (see the safety note in step 5).
- Present the proposal as a markdown table: trace/turn range (or "whole session" if uniform) → proposed tag(s) → one-line rationale.
- Get explicit confirmation or adjustment from the user before proceeding to step 6. Treat silence or a vague "sounds good" as insufficient if the table is large or the tags are consequential — ask directly if anything is ambiguous.

## 5. Fetching trace metadata safely

If you need the list of traces for a session (IDs, timestamps, turn numbers) to build the proposal table:

```bash
langfuse-cli api traces list --session-id "<session-id>" --fields core --json
```

**Never fetch or persist the `io` field group (trace input/output) to disk.** Trace content can contain secrets pasted during the session — API keys, passwords, tokens, connection strings. If you genuinely need per-trace content to classify topics accurately, view it via a tool call in-session rather than writing it to any file, and never copy it into a scratch file for later reference.

## 6. Applying tags

Once the table is confirmed:

1. For each trace, compute its **full desired tag set** client-side (existing tags ∪ new tags — remember the API only unions, so there's no reason to send a partial list expecting it to replace anything).
2. Build a batch of ingestion events:
   ```json
   {
     "batch": [
       {
         "id": "<fresh-uuid-per-event>",
         "type": "trace-create",
         "timestamp": "<iso-8601-now>",
         "body": { "id": "<existing-trace-id>", "tags": ["tag-a", "tag-b"] }
       }
     ]
   }
   ```
3. POST to `<host>/api/public/ingestion` with header `Authorization: Basic <base64(public_key:secret_key)>`.
4. Chunk at roughly 50 events per batch (the endpoint has a 3.5MB total batch-size limit).
5. Verify by re-fetching one sample trace afterward and confirming its tags match what was intended.
