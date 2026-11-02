# Honest limits and known issues

## Honest limits

- **Whether a relation holds one value at a time is a model's answer.** The
  first time the judge meets two facts of a relation (since 0.1.23; before,
  at the relation's birth), a model is asked whether a subject holds one
  value of it at a time, of the kind of the two values in conflict, and the
  majority of three answers is kept in that end user's vocabulary. The judge
  reads it: a relation kept as "many" never has its old value closed by a new
  one. Until 0.1.15 it was one answer about the relation in general, and four fresh
  installs in twenty-one learned that a customer can be on many plans. Measured
  on 0.1.15: the example at the top closed the old plan in twenty fresh
  installs out of twenty, and the question was right 120 times in 120 on plan
  relations and 80 in 80 on relations that do hold many values (`likes`,
  `uses`, `speaks`, a newsletter). It is not right everywhere: `works_at` a
  company came back "many" 11 times in 20, so a change of employer may leave
  both employers true. Look with `include_invalidated=true` when it matters,
  and close a stale fact yourself with `forget` and its `at`. An install that
  learned its relations before 0.1.15 keeps the old answers until
  `manage reclassify-relations` asks again (a dry run; `--apply` writes).
- **A fact can be closed by one that does not replace it.** On long chats the
  judge closes some facts that were still true: a rephrasing ("2 younger
  kids" after "husband and kids"), two facts true together, or one past
  occurrence of a habit ("went to the beach last weekend" closing "visits the
  beach"). Measured on LoCoMo with gpt-oss-120b (6-7 Oct 2026): about 2% of the
  facts ended closed by another (140 of 5,935), and on one conversation none of
  its 20 closures was a real change. Since 0.1.23, on LoCoMo's first five
  conversations: 17 facts, against 53 with 0.1.22. What is left is
  mostly a new state read as replacing an older compatible one, and relations
  the vocabulary merged though their values are of different kinds (a club,
  a sponsor and a team in one "signed with"). A closed fact stays readable with
  `include_invalidated=true`, and since 0.1.23 a memory whose facts were all
  closed stays in the block, marked as history.
- **The learned predicate vocabulary is a model's judgement.** Different
  phrasings of one relation collapse into one only when the model says they
  are the same; since 0.1.23 no hand-written list of words merges anything,
  in any language. It is sometimes wrong both ways: reviewing what an install
  learned on all of LoCoMo, the model separated 337 phrases it had merged at
  their birth, and some of those separations were themselves wrong.
  `manage review-vocabulary` asks again (a dry run; `--apply` separates).
  Contradiction detection still fires through the semantic path when two
  phrasings stay apart.
- **A colleague with the same first name can close a fact of the user.** In
  one end user's memory, "Marco Bianchi eats meat at every meal" can close
  "the user is vegetarian" when the user is called Marco: the judge cannot
  always tell a name the user goes by from a namesake's. Declaring every
  name the person is called by (`POST /v1/users/{id}/names`, `korely init
  --name` once per name) makes their own facts read as theirs; on our
  identity bench such wrong closures still happen. A closed
  fact stays readable with `include_invalidated=true`.
- **One thing named in two languages is two things, unless the same-object
  question is on.** "lives in Italia" and "lives in Italy" are stored with
  two objects: the extraction keeps the memory's own words, and the store
  learns that two names are one only when a memory says so, as a rename, or
  when an operator declares it (`POST /admin/aliases`). Since memory_core
  6.1.0 the store can ask the writer of the project's region, for every
  object a write names that the end user's memory does not name yet,
  whether it is the same thing as one of the memory's open objects in the
  same relation family whose facts read alike; it is off
  (`services/facts/objects.SAME_OBJECT`). The writer describes each name in
  its context, then answers twice, framed two ways, and two "same" write a
  link: no fact changes, the reads show the two as one ("Italia (written:
  Italy)"), and `DELETE /admin/object-links/{id}` puts the memory back
  exactly as it was. A link is read only in the relation family it was
  decided in: "Florence" linked to "Firenze" among the places leaves an
  aunt called Florence as she was written, and an employer called Paris
  is not the city. People are never asked (a nickname is not a
  translation), nor a name with a comma or over five words ("Washington,
  D.C."), and a name the memory already holds is never asked again: within
  one family, the same written name is one object whatever it means
  ("Paris" the city and a place called Paris in Texas), on or off. On our bench of
  30 Oct 2026 (138 hard negatives with no disambiguator in their own
  sentence: exonyms, New-World homonyms, a company and its parent or
  brand, a place and what contains it), Gemini 3.1 Flash-Lite united 1
  wrongly (95% interval 0.1% to 4.0%) and gpt-oss-120b on Scaleway 0 (0% to
  2.7%), but 1 of 24 older negatives; both missed 1 or 2 of 42 true pairs.
  `manage review-objects` asks the question for the objects already stored,
  a dry run to a file and then `--apply` of exactly that file. Until two
  names are one, a search finds both by meaning, and whether a later fact
  about one closes the other is left to the judge reading both: a link
  changes neither what is written nor what the judge reads. Forgetting or
  erasing either memory of a link takes the link, and its two names are
  then two objects for good: the one that stays was decided when it came,
  and is not asked again. Not measured yet, and needed before it goes on:
  a bench held out from the one the prompts were written on (the 30 Oct
  numbers are in-sample); the second reading reads the same descriptions
  as the first, not the contexts alone; whether the contradiction judge
  closes "lives in Italia" when "lives in Italy" arrives (the bench asks
  the question directly, not through a write); and a way to turn it on for
  one project at a time.
- **Each end user learns their own relation names.** The vocabulary is one
  per end user of a project, so that nothing of one end user (a value, a
  vote, a phrase) shapes how another's facts are read or judged. The price:
  two end users can store the same relation under two names (`works_at`,
  `employed_by`), and each end user's first writes ask the model about
  relations another end user already taught. `GET /v1/facts?predicate=`
  resolves the phrase in each end user's vocabulary.
- **A rename reaches the past.** When the store learns that something has a
  new name (an alias: made by itself from a memory saying something was
  renamed, by an operator with `POST /admin/aliases`, or by a key that
  declares an end user's name with `POST /v1/users/{id}/names`), every fact
  of that end user in that project and agent namespace takes the new
  canonical name, closed facts included, and two open facts that now say the
  same thing become one: the older row stays and the newer is deleted. A
  plan that meets the same thing done is not merged: it closes where the
  fact begins and stays in the history. Removing the alias
  (`DELETE /admin/aliases/{id}`) gives every fact back the names its words
  give, and does not split what it merged: the deleted row and the closed
  plan stay as they are.
  A link between two names judged one thing (above) is not an alias: it
  changes no fact, and its removal gives back exactly what was there.
  `manage review-vocabulary --apply` moves the facts of a phrase it
  separates to their own relation the same way. The dates stay, and
  `subject` and `object` keep the words as written; but `as_of` on an earlier
  date answers with today's `predicate`, `subject_canonical` and
  `object_canonical`, and with one row where there were two.
- **Extraction quality is the model's.** Facts are mined by an LLM. It invents
  predicates, it misses things, and it costs money per write. No read calls
  that model: a search and a context compute one vector of the question, on
  this machine with the default `EMBEDDING_PROVIDER=local` and billed to the
  Google key with `gemini`, and every other read is SQL.
- **A change told implicitly or in the past tense can leave the old value
  open.** Found on 31 Oct 2026 in a real agent environment (Hermes, engine
  6.1.1): "Tom lives in Berlin and works at a bank", then "Tom moved to Munich
  and joined a startup", left Berlin and the bank open, with Munich and the
  startup stored as already finished; the same in Italian ("si è trasferito a
  Milano"). A change told as "now I live in Lisbon" works. The same end user
  can also appear as two subjects (the name in the text and the `user_id`), and
  "te verde" and "tè verde" are two things. 1.0.3 fixed one cause of it for a
  writer of the gpt-oss family chosen on the Models page (the Cloud's European
  one among them), which until then did not get the extraction rules measured
  for it (see the CHANGELOG). With them, in a bench of ours that is not part of
  this release (the 14 hardest change cases, three runs each, the real writer,
  one session), the changes that ended right went from 0 of 33 to 7 of 33: a
  small sample, better and not solved. Until it is, an agent that knows a fact
  ended can close it itself (`korely_correct_fact`).
- **Windows is tested only on Windows Server 2022.** Korely was built from
  source, started, written to and searched on Windows Server 2022 with Docker
  Engine inside WSL2 (31 Oct 2026, version 1.0.2). Windows 10 and 11 with Docker
  Desktop use the same WSL2 and are not tested yet, and neither are the
  installer and an upgrade from an earlier version there.
- **More than one process needs sticky MCP sessions.** Two processes on one
  Postgres, both with their worker, took 2,000 memories at once: each memory
  got its vector, reached the writer once and sent its webhook once, with no
  error (`scripts/two_processes.sh`, 30 Oct 2026; run it on your machine, no
  key needed). The job queues and the locks are in Postgres, so any process
  can take any request; an MCP session lives in the process that opened it,
  so a load balancer in front must keep each MCP client on one process.
  Thirty agents of one account on the same two processes, 15 on REST and 15
  on MCP sessions, each wrote 20 memories and read 20 times, with the
  client's retries off: 600 of 600 writes stored once, every one reaching the
  writer once, 600 of 600 reads answered, no search refused as busy, no
  error in either log, in 26 seconds on a laptop with the writer faked;
  reads p50 0.6 s, p95 1.6 s, slowest 2.2 s, because a burst waits for its
  vectors (below) (`scripts/concurrent_agents.sh`, 31 Oct 2026, no key
  needed). More than two processes, or more than one machine, has not been
  measured.
- **A twelve-hour run held, and forty-eight hours are not measured.** One API
  process and one Postgres on a 4-CPU, 8 GB server, 50 end users, one memory
  written and then searched and read as a context about every 1.2 seconds
  (36,027 memories and as many of each read in 12.2 hours), with the writer
  faked, so it measures the process, the queues and the disk and not the
  facts, and the API restarted once at the twelfth hour (`scripts/soak.sh`,
  1 Nov 2026, no key needed). The API's memory stayed flat: 591 MiB after
  the warm-up, 585 MiB at the end, 616 MiB at most (and back to its usual
  size minutes after the restart). Not one line of its log was an error,
  no memory was left waiting, and 5 requests failed around the restart. The
  database grew from 9 to 467 MiB, about 13 KB a memory with its vector, which
  is about 900 MiB a day at that pace; with facts written it grows more, and
  that was not measured. Hours of load on several processes or machines, and
  a database of millions of memories, are not measured either.
- **A burst waits for its vectors.** The local embedder computes one vector
  at a time per process, and an account has only a few places there at once
  in each process (`core/threads.CALLS_PER_ACCOUNT`, four), shared by its
  REST and MCP calls; past them a search or a context waits its turn (at most 20 s, then
  503 `search_unavailable` with `Retry-After`), and a memory is stored
  without its vector and the worker computes it later, and until then a
  search does not find it. In the run above, on a laptop, one account sent
  2,000 memories in about 20 seconds, 32 at a time, and the last vector came
  about nine minutes later.

## Known issues in this version

The review before this version found these and left them for the
next patch. None of them shows one account's data to another, loses data or
keeps an erased person's data; each one delays work or costs time on an
install shared by many accounts. A single-account install meets almost none.

- **The workers' turns between accounts are not fair in every case.** The
  facts drain orders the accounts it serves by the event time a client
  sends, so backdated imports go first; the vector drain serves the 50
  accounts with the oldest due rows per pass; the webhook round can skip the
  same few accounts while others have a backlog and their receivers are slow.
  Work is delayed, never lost.
- **An account's own long erasure can pause the workers.** While a project
  erasure with a very large trail runs, the batch, vector and facts workers
  can wait on the rows it holds, for every account, until it commits; the
  trail pass of a project erasure costs ids times trail rows.
- **Revoked API keys are kept.** At most 100 live keys, but revoked ones are
  never deleted, and a project erasure reads all of the account's key rows.
- **Webhook name lookups share one pool.** Eight accounts whose endpoints'
  names resolve slowly can make other accounts' webhook checks time out
  while it lasts.
- **The Markdown export** (the optional export directory) can be held by one
  account that keeps asking for it, delaying the others' exports.
- **Old metadata with a malformed character.** A memory stored before 0.1.41
  whose `metadata` holds a lone UTF-16 surrogate makes a metadata-filtered
  search of that account answer 500. Such values are refused at every door
  since this version.
- **A restore's replay can file one erasure twice.** If an error comes after
  a repeated erasure has committed, its new record stays next to the
  original. The erasure itself is complete; the extra record is harmless.
