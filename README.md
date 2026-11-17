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
- **Nightly digest (optional).** `lamellar nightly` (from cron) has a local model read the library one window at a time and write short notes. A program checks every note, not another model:
  - every quote must appear word for word in the passages it cites
  - every term, number and name must appear in those passages
  - the note must be written in the same language as what it quotes
  - a note with no refs is void

  A note that fails is sent back at most twice, then dropped. Progress is saved per window, so a run that stops resumes where it left off. If a user job starts, the digest yields.

## Measured

**Documents: 30 fact questions about one book (16 chapters), 9B model, two runs each**
- Without the plate: 12.5 of 30 right.
- Passages only: 28.5 of 30.
- Notes plus passages: 28.5 of 30.
- With the plate, answers were faster (about 3 s instead of 8–20 s) and none timed out.
- 10 unrelated questions had nothing added.

Notes gave nothing extra on single-fact questions; the gain comes from searching the passages. Notes are kept as a map for long libraries and for questions that span several places. That use is not measured yet.

**Conversations: 20 real conversations replayed, then 20 follow-up questions asked in new sessions** (see below)
- Without the conversation library: 1 of 20 right.
- With it: see the table in `docs` (filled in by the KL9 run).

## Conversation library (off by default)

Set `"chat_write": true` and each user gets a private library: `chat-<bot>-<chat>.db`. One user never sees another user's library.

**What is saved**
- After an answer is delivered (the user does not wait), the plate saves what the user said and what the tools returned.
- It saves only when the exchange used a tool or the user stated facts. Pure chat is skipped and calls no model.
- The assistant's own words are never saved as a source.
- Content on the never-remember list (passwords, keys, ID numbers, phone numbers, emails, amounts) is masked before storing.

**How it is searched**
- Conversation passages are mostly long tool outputs, so word overlap alone lets unrelated questions in.
- Each candidate passage is scored by the model and kept only at 3 of 10 or above (`chat_rerank`).

**Notes from conversations (`"chat_nodes": true`) are off by default.** When this mode is on:
- The model writes up to 3 notes per exchange. It is shown the nearest existing notes first, and can add, update (the old note is kept as superseded) or skip.
- QC is the same as the digest. In addition, a quote that comes from the assistant's answer is rejected.
- Notes start at low trust and fade unless an answer cites them (ACT-R style decay).
- Only notes that were hit directly and then cited are strengthened. Links between them are capped at 3 and decay every night.
- Markdown under `~/.lamellar/kal/<lib>/` is the source of truth; `lamellar kal rebuild --lib <lib>` rebuilds the database from it.

Why off: in the replay, a 27B writer invented details in 3 of 24 notes that passed the program QC. Examples: a count of 27 written as 17, a wrong file name, a date that was not there.

## Commands

```
lamellar kal add <folder> [--lib NAME]     index a folder (or one file)
lamellar kal search "<query>"              search the libraries
lamellar kal read <ref>                    show one passage or note in full
lamellar kal context "<task>"              show what would be added to a task
lamellar kal status [--lib NAME]           files, passages, digest progress, QC pass rate
lamellar kal learn [--minutes N]           run the digest now
lamellar kal chats                         conversation libraries
lamellar kal confirm <id> --lib chat-…     raise a conversation note to normal trust
lamellar kal rebuild --lib chat-…          rebuild a conversation library from its markdown
```

## Settings (`worker.json`, key `kal`)

```
libs              ["default"]  libraries used when answering
max_chars         3000         cap on added text
min_rel, rel_top  0.05, 0.5    relevance floor, and share of the best hit to keep
use_nodes         true         use digest notes
rerank_model      ""           optional reranker for all libraries (one request per candidate)
digest_model      ""           model for the nightly digest (default: the main model)
night_minutes     120          digest time budget
chat_write        false        conversation library on/off
chat_nodes        false        also write notes from conversations
chat_rerank       true         rerank conversation passages (keep >= chat_rerank_keep, default 3)
```

PDF is not supported in this version.

## Tests

```
git clone https://github.com/lamellar26/lamellar && git clone https://github.com/lamellar26/lamellar-pack-kal kal
cd kal && python3 -m unittest discover -s tests
```

Apache-2.0. See LICENSE.
