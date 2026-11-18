# kal: the knowledge plate for lamellar

[lamellar](https://github.com/lamellar26/lamellar) is armor for small local models: plates you add one at a time, each making a small model more capable. **kal** is the knowledge plate. It works offline and uses only the Python standard library.

```
lamellar pack install https://github.com/lamellar26/lamellar-pack-kal
lamellar kal add ~/Documents/handbook      # md, txt, docx; searchable at once
lamellar run "who approves leave requests?"
```

## What it does

- **Your folders become a library.** `lamellar kal add <folder>` splits files into passages and indexes them in SQLite (`~/.lamellar/kal/<lib>.db`). You can search them right away. Running it again skips files that have not changed.
- **Before every task, relevant passages are added to it.** Each passage carries a `[ref]` to its source, and the total is capped (3,000 characters by default). Unrelated tasks get nothing added. The model can also call `kal_search` and `kal_read(ref)` while it works.

## Measured

**Documents: 30 fact questions about one book (16 chapters), 9B model, two runs each**
- Without the plate: 12.5 of 30 right.
- Passages only: 28.5 of 30.
- Model-written notes plus passages: 28.5 of 30, no better than passages alone.
- With the plate, answers were faster (about 3 s instead of 8–20 s) and none timed out.
- 10 unrelated questions had nothing added.

The gain comes from searching the passages, so model-written notes were removed in 0.3.0.

**Conversations: 20 real conversations replayed, then one follow-up question each, asked in a new session** (9B model, two runs each)
- Without the conversation library: 1 and 3 of 20 right (the model searched the machine with tools and mostly did not find it).
- Conversation passages (the default): 11 and 14 of 20.
- Passages plus notes written from the conversations: 14 and 14, but a 27B writer invented details in 3 of 24 notes that passed the program QC (a count of 27 written as 17, a wrong file name, a date that was not there). Notes were removed in 0.3.0.
- Unrelated questions: without reranking, 6 of 10 had text added; with it, 1 of 20 checks did (1,193 characters).
- The rerank threshold and the writer prompt were tuned on this same set; a fresh set is needed for a clean number.

## Conversation library (on by default)

Each user gets a private library: `chat-<bot>-<chat>.db`. One user never sees another user's library. It stays on your machine. In a group chat it keeps what everyone in the group said; set `"chat_write": false` for a group that has not agreed to this.

**What is saved**
- After an answer is delivered (the user does not wait), the plate saves what the user said and what the tools returned.
- It saves only when the exchange used a tool or the user stated facts. Pure chat is skipped and calls no model.
- The assistant's own words are never saved as a source.
- Content on the never-remember list (passwords, keys, ID numbers, phone numbers, emails, amounts) is masked before storing.

**When the user says "note this"** ("remember this", "take a note", or the same in Chinese)
- The model saves the user's own words with the `kal_note` tool, and the reply shows what was saved.
- If the model does not call `kal_note`, the message is saved after the answer anyway, whatever its length. A bare "note this" also keeps the message before it.
- If the answer says "saved" but nothing was saved, the answer is sent back to the model before the user sees it.
- `kal_note` works even with `chat_write` off: asking is consent.
- **On its own** (with `chat_write` on): when the user tells something that will matter later (preferences, decisions, plans, people), the model may save it with `kal_note` and says in one line what it kept. Such a note must be the user's own words: if 60% of it is not in what the user said, it is refused, so the model cannot write its own guesses into the library. Stored as `chat/auto-*`.
- Never while the model is fixing a wrong "the library has it" claim.

**Where an answer says it came from**
- If an answer says "the library has X", X must be in what the library returned this turn (search, read, note or injected material). Quoted and bold names are checked one by one.
- If X only came from earlier in the conversation, the answer is sent back: it must say it came from the conversation, which is gone when the session ends.

**How it is searched**
- Conversation passages are mostly long tool outputs, so word overlap alone lets unrelated questions in.
- Each candidate passage is scored by the model and kept only at 3 of 10 or above (`chat_rerank`).

Markdown under `~/.lamellar/kal/<lib>/sources/` is the source of truth; `lamellar kal rebuild --lib <lib>` rebuilds the database from it. No model is called when saving.

## Commands

```
lamellar kal add <folder> [--lib NAME]     index a folder (or one file)
lamellar kal search "<query>"              search the libraries
lamellar kal read <ref>                    show one passage in full
lamellar kal context "<task>"              show what would be added to a task
lamellar kal status [--lib NAME]           files and passages
lamellar kal chats                         conversation libraries
lamellar kal rebuild --lib chat-…          rebuild a conversation library from its markdown
```

## Settings (`worker.json`, key `kal`)

```
libs              ["default"]  libraries used when answering
max_chars         3000         cap on added text
min_rel, rel_top  0.05, 0.5    relevance floor, and share of the best hit to keep
rerank_model      ""           optional reranker for all libraries (one request per candidate)
chat_write        true         conversation library on/off (false: only explicit "note this" is kept)
chat_rerank       true         rerank conversation passages (keep >= chat_rerank_keep, default 3)
```

PDF is not supported in this version.

## Tests

```
git clone https://github.com/lamellar26/lamellar && git clone https://github.com/lamellar26/lamellar-pack-kal kal
cd kal && python3 -m unittest discover -s tests
```

Apache-2.0. See LICENSE.
