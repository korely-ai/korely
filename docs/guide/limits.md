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
- **On an agent's notes, most of the facts the judge closed were still
  true.** Korely checks whether a new fact contradicts an old one. If it
  finds a contradiction, it closes the old fact with a date instead of
  deleting it. That check is a model's judgement, and on notes that speak of
  many things it can read a fact about one thing as a change of another:
  "the SDK is licensed MIT" closed by "the license is Apache 2.0", written
  about another repository, or two facts of one subject under one relation
  that are true together. Measured on 12 Nov 2026 on 43 notes of our own
  team of agents, each written with its `agent_id` (memory_core 6.9.0, the
  judge and its candidates unchanged in this version; Gemini 3.1 Flash-Lite
  as writer and judge, two runs): 6 facts closed, 5 of them wrongly, the
  same 6 in both runs. Two more ways a fact ends, or looks replaced, with
  nothing changed:
  - A fact told in the past tense ends where it was said, and when the same
    memory states one open fact of the same subject and relation with
    another value, the store points to that fact as its replacement, with no
    model asked (`memory_core/worker.py`): "the full run cost 8.20 euros"
    (paid) and "a full run is estimated at 13.06 euros", in one note, read
    as a cost replaced by the estimate (3 closures in three runs on those
    notes written with no ids, 12 Nov 2026).
  - A fact you post (`POST /v1/facts`) whose relation is one or two letters
    of a script with capitals ("è", "ha", "fa", "is") is stored under the
    relation `unknown` (`memory_core/predicate_forms.py`), so every such
    fact of one subject is one relation to the judge, and one can close
    another (seen on Korely Cloud 1.0.11, 6 Nov 2026; the rule is the same
    in this version).

  A closed fact is not deleted: `include_invalidated=true` shows it, and its
  memory stays in the context block. What to do: when the answer matters,
  read the memories, not only the facts; post a fact closed by mistake again
  (`POST /v1/facts`) to make it true; give a fact you post a relation that
  says what it is (`has_license`, `costs`, `works_at`).
- **A correction told as a new memory, or posted with only a day, can lose
  to the fact it corrects.** A memory that states the right value goes
  through the same check as any memory, so when the judge does not read it
  as a contradiction the wrong fact stays true beside it: on our team's
  memory (Korely Cloud 1.0.13, notes written with no ids, 9 Nov 2026) a note
  giving the right formula of a database trigger left the wrong one true 25
  minutes later, and still the next morning. A `valid_from` with a day and
  no time (`"2026-11-06"`) is midnight UTC of that day: a wrong fact read
  from a memory at 20:56 that day began later, so the judge keeps the wrong
  one true and stores the correction as history, as it does with any fact
  that began before an open one (seen on Korely Cloud 1.0.11, 7 Nov 2026;
  dates are read the same way in this version). And a relation the model
  holds as many values closes nothing: "LoCoMo has_score 86.9%", posted
  beside "LoCoMo has_score 93.6%", left both true (Korely Cloud 1.0.11, 6
  Nov 2026; whether a relation holds one value is the first limit above).
  What to do: correct the fact itself, by its id, while it is still open.
  `PATCH /v1/facts/{id}` (`korely_correct_fact` over MCP) corrects only a
  fact still open (a closed one answers 409 `fact_not_current`); it closes
  the old fact itself, and the new value starts now (for a fact dated
  ahead, where the old one was to begin: [Time](time.md)). It may call the
  model: the new value is resolved and judged as any fact you post. When
  the change happened on a past date,
  forget the old fact with its `at` (`POST /v1/facts/{id}/forget`) and post
  the new one with a `valid_from` that carries the time
  (`"2026-11-06T21:08:00Z"`).
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
- **One person can be two subjects: the name in the text and the
  `user_id`.** The writer names a person as the text does or by the
  `user_id`, and not always the same way. "Giulia is on the Advanced plan.
  She prefers email follow-ups. She is based in Turin.", written under two
  `user_id`s, gave "Giulia" as the subject under one and the `user_id` as the
  subject of all three facts under the other; the `user_id` came out in 2
  runs of 3 (Korely Cloud 1.0.0, European region, 31 Oct and 1 Nov 2026).
  Two agents of one end user that wrote the same fact left two open facts,
  one under the `user_id` and one under the name, and "Tom" and "test-tom"
  were two people for the same writer: a change told under one name can
  leave the old value of the other open. What to do: declare every name the
  person goes by before writing about them (`POST /v1/users/{id}/names`,
  `korely init --name`), so that their facts read as theirs; a name declared
  after the facts were written joins them but does not judge them again
  (below). A `user_id` that is an email address, or begins with a digit,
  cannot be given a name: the call answers 422, and its person stays two
  subjects. Only a `user_id` that is itself a name can have one: it begins
  with a letter and has at most 60 characters and 5 words, of letters,
  digits, spaces and `_ . - & ' / +` (so `|` and `:`, as in `auth0|...`,
  answer 422 too, and so do the many UUIDs that begin with a digit). Give
  each person an opaque `user_id` that begins with a letter (`c4821`).
- **A fact written directly is about the name it is written with.** Since
  1.0.12 (memory_core 6.5.0 and 6.6.0) a fact posted with `POST /v1/facts`,
  or a correction whose subject is yours (`PATCH /v1/facts/{id}` with
  `subject`, or of a fact you wrote), is weighed only against the facts
  about the same subject, as the store names it after its aliases (a
  declared name, a rename) and compared with the database's `lower()`: a
  name spelled with other capitals is the same subject when `lower()` gives
  one form for both (`ACME`/`acme`); a shorter name, an article, a second
  space, a different Unicode form (`Straße`/`STRASSE`, full-width letters,
  `Café` written composed and decomposed, the Greek final sigma of
  `ΟΔΟΣ`/`οδος`) make another subject. It is weighed as well against
  the facts about the same two things the other way round. Before, it was
  also weighed against the facts nearest by meaning of any subject, and on
  a store of an agent team's own facts (6 Nov 2026) 108 of the 111 closures
  between two direct facts were between two subjects. What it costs: a
  person written about by name ("Maria") whose memories are stored under
  their `user_id` is two subjects until the name is declared
  (`POST /v1/users/{id}/names`), and a name is declared in one agent
  namespace: a direct fact written with another `agent_id` does not read
  it, and the old value stays true beside the new one. The same holds the
  other way round: a fact read from a memory is weighed against a fact
  written directly only about the same subject, so "u-42 now works at
  Globex", read from a memory, leaves "Maria works at Acme" true until the
  name is declared. Declaring a name reads the facts already written about
  it as the end user's but does not judge them again: two values written
  before the declaration stay open side by side. An account declares at
  most 1,000 names, and only a `user_id` that is itself a name can have one
  (422 otherwise). A correction that gives only the object or the relation
  of a fact read from a memory is weighed as that fact was, across the
  extraction's names; give `subject` to make it yours. Two facts read from
  memories are weighed as before, across the names the extraction gives one
  entity. Not measured: the change was tested with a scripted judge only,
  so how many right closures a real judge loses on a store that mixes
  direct and extracted facts is not known. A fact closed by mistake stays
  readable with `include_invalidated=true`; post it again to make it true
  again.
- **A correction stands through an edit of its memory, also one that says
  the old value again** (since 1.0.29). Since 1.0.27 a correction
  (`PATCH /v1/facts/{id}`) names no memory, so an edit or a forget of the
  memory of the fact it corrected leaves it true (an erasure of that memory
  still deletes it, with the words it was made of). Until 1.0.28 an edited
  text was extracted whole, so a new text that still said the old value
  ("Sofia works at Acme Corp" plus a sentence) wrote it again and a judge
  could close your correction; since 1.0.29 the old value you corrected is
  your word that it is over, and the edit does not write it again (as a
  forgotten fact, below): Acme Corp stays closed and Beacon Labs true
  (proved with judges written by hand in the tests, not measured with a
  real model). Post the old value (`POST /v1/facts`) to make it true again. A correction made
  before 1.0.27 still names the memory and goes with it; a correction to a
  value a memory already states as true lands on that memory's row, said
  again, and goes when that memory does, as a fact you post on the same
  value does.
- **Writers who share one memory through MCP without an `agent_id` are
  one writer.** Since 1.0.19 every write over REST needs a `user_id` or an
  `agent_id`; over MCP, `korely_add` with neither is written in the
  project's memory with no end user, as before, and the assistant's
  declared name (`client`) says who wrote it. Several agents that write
  through MCP that way, each with no `agent_id`, are read as one person,
  `user`, as every writer without ids was before 1.0.19, and the judge can
  close one agent's fact with another's: they should each pass their
  `agent_id`. The client's name is a label, not a namespace: it scopes no
  read and the judge does not read it.
- **An `agent_id` on a read keeps one agent's rows, while a write is weighed
  against every agent's.** In a memory several agents share, the judge
  weighs a new fact against the facts of every agent of the same end user
  about the same subject, and a read with an `agent_id` (`GET /v1/context`,
  `GET /v1/facts`) shows only that agent's facts. So an agent
  that reads with its own id does not see the change another agent made to
  a fact it wrote: its fact is closed and the new one is not its own. Agents
  that share a memory read without an `agent_id`, or with `reader`, which
  filters nothing ([One memory for a team of agents](api.md#what-to-save-so-the-memory-stays-clean)).
- **A program that wrote without ids and now sends a `user_id` has two
  memories.** A memory stored with no ids before 1.0.19 is not rewritten,
  and the facts of a write with a `user_id` are not weighed against it: a
  change written after the upgrade leaves an old fact with no id open until
  it is corrected or forgotten by hand. The upgrade does not move the old
  rows to the new id.
- **An id that names nobody is told by its Unicode general category.**
  An id made only of whitespace, format characters (category Cf, such as
  U+200B or U+FEFF) or marks (categories Mn, Mc, Me, with no letter to
  carry them) is no identity for the write's rule. The fillers and blanks
  that Unicode files as letters or symbols are not caught: U+3164 HANGUL
  FILLER (Lo, which Unicode lists as default-ignorable in a data file Python
  does not carry) and U+2800 BRAILLE PATTERN BLANK (So) count as an
  identity. The engine reads ids by its own rule: an agent id is blank when
  only whitespace is left (`memory_core/owner.agent_of`), and any `user_id`
  is an end user; an id the write's rule refuses never reaches it alone.
- **In a memory with no `user_id`, two spellings of one thing are two
  subjects.** Since 1.0.13 (memory_core 6.7.0) a fact of a memory
  written with an `agent_id` and no `user_id` is weighed only against the
  facts about the same subject, as the store names it after the aliases of
  the agent that writes and compared with the database's `lower()`, and
  against the facts about the same two things the other way round. Before,
  it was also weighed against the ten facts nearest by meaning of any
  subject: on the memory of our own team of agents (422 facts, 7 Nov 2026)
  21 of 32 closures were wrong, and replayed with the same verdicts of the
  judge (no model called: an estimate) the new rule keeps 4 of them. What
  it costs: "Cloud", "Korely Cloud" and "the Korely Cloud" are three
  subjects until an alias unites them in the namespace of the agent that
  writes (`POST /admin/aliases` with its `agent_id`), so "Korely Cloud runs
  version 1.0.12" leaves "Cloud has version 1.0.11" true. On that memory 7
  of the 8 right closures were between two subjects (4 of them between two
  spellings of the Cloud), and they are lost with the wrong ones. The rule
  holds both ways. Since 1.0.19 (memory_core 6.9.0) it holds for every
  memory with no `user_id`, one whose writes carried no ids included, and a
  fact meets the facts of its entity in either role: their subject is its
  subject, their object is its subject, or their subject is its object
  ("Acme employs user", then "user works at Globex", still meet); two facts
  that only share an object do not. With no end user the store cannot know
  the memory is one person's. Measured on our team's memory written with no
  ids (12 Nov 2026, Gemini 3.1 Flash-Lite, the extraction recorded once and
  replayed, the judge asked again in three runs per version, every closure
  read on its two memories): wrong closures 19 of 41 before and 12 of 30
  after (none between two subjects, 7 before); outdated facts closed by the
  newer fact of the same subject 14 before and 18 after; outdated facts
  closed by a fact of another subject, right by chance, 7 before and 0
  after, so outdated facts closed in all 21 before and 18 after. The 3 left
  open are one outdated version that no fact of its own subject says
  changed. Without ids the first person is stored as the subject "user", in
  every writer's memory: two rules of two writers can still close each other
  there (9 of the 12 wrong closures after; with an `agent_id` the first
  person is the agent and there were none), and a name a writer goes by is
  another subject until an operator declares it (`POST /admin/aliases`
  with `alias` the name, `canonical` "user", and no `user_id` nor
  `agent_id`). An `agent_id` that is empty or only whitespace is no agent
  for the extraction, which frames the memory by it; the database applies
  that rule with `btrim`, which removes characters in a UTF8 database and
  bytes in a SQL_ASCII one, where an `agent_id` written with characters
  other than ASCII may be framed differently. What stays:
  two facts of one subject that are true together ("the repository is
  private", "its pushes are suspended") can still close each other, since
  the judge reads them as a change. Not
  measured with a real judge: the facts of one subject now shown in place
  of the others (197 pairs on that memory) were never judged.
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
- **Facts read from an agent's notes can have the wrong subject or the
  wrong time.** Since 1.0.12 a memory written with an `agent_id` and no
  `user_id` is told it belongs to no end user: its subject is what the text
  names, its first person is that agent, and a rule in force is asked as
  current (see the CHANGELOG); the prompts are the same in this version.
  Up to 1.0.11, on the 58 memories of our own team of agents, 74 of 168
  facts were right and none of the 48 with `user` as their subject or
  object was. With the prompts of 1.0.12, on those 58 memories, each
  written with its `agent_id`, every fact read by hand (7 Nov 2026): 169 of
  212 right with Gemini 3.1 Flash-Lite and 174 of 224 with gpt-oss-120b,
  about 8 in 10. On 16 Nov 2026, 43 other notes read with the same frame on
  a development build gave 173 of 193 right with Gemini 3.1 Flash-Lite. What
  the wrong ones look like: a decision of a person given to the agent that
  wrote it down (16 of Gemini's 21 wrong subjects on 7 Nov); a dated history
  read as current ("the Cloud runs 1.0.7", then 1.0.8, 1.0.9 and 1.0.10, all
  current at once); a state that still holds written in the past; a
  direction turned round ("merge main into the branch" stored as "merge the
  branch into main"); a count of errors stored as a rate of success (20
  wrong answers in 24 read as "accurate in 20 of 24"). The note itself is
  kept word for word and the context block shows it with its facts. What to
  do: when the answer matters, read the memories, not only the facts, and
  correct a wrong fact by its id (`PATCH /v1/facts/{id}`,
  `korely_correct_fact` over MCP). A memory with neither a `user_id` nor an
  `agent_id` (since 1.0.19 only an assistant's over MCP, or one stored
  before) is read as in 1.0.11, as the memory of an end user named "user":
  a team whose agents write over MCP without their `agent_id` keeps the
  `user` facts described above (on 16 Nov 2026, 43 notes written so gave 83
  of 143 facts right with Gemini 3.1 Flash-Lite, 54 of the others with the
  wrong subject). The new tense rule (a procedure, a rule in force, the
  lasting result of an approval are current) is asked only of an agent's
  memory with no `user_id`; every other memory keeps the rule of before.
- **A change told implicitly or in the past tense can leave the old value
  open.** Korely checks whether a new fact contradicts an old one. If it
  finds a contradiction, it closes the old fact with a date instead of
  deleting it. A change told as the new state ("now I live in Lisbon") is
  found most of the time; a change told as an event can be missed. Found on
  31 Oct 2026 in a real agent environment (Hermes, engine 6.1.1): "Tom lives
  in Berlin and works at a bank", then "Tom moved to Munich and joined a
  startup", left Berlin and the bank open, with Munich and the startup
  stored as already finished; the same in Italian ("si è trasferito a
  Milano"). The writer may also store the new value under another relation
  (`relocated_to`, `is_at`, `switched_to`) that never meets the old one
  (`lives_in`). The same with a successor named after a past tense (a note
  of the shape "Ana no longer reviews the changes; Ben does now" wrote a row
  for Ben and left Ana reviewing) and with a change said in passing (a note
  that the test database is now beta and alfa is retired wrote no fact for
  beta and left alfa current), both on Korely Cloud 1.0.15 (10 Nov 2026).
  How often depends on the writer:
  - gpt-oss-120b on Scaleway (the Cloud's European region): measured on 15
    Nov 2026 on a development build of the engine, not on a release
    (memory_core 6.13.0; this version is not measured). Our bench of 68
    change cases ended right in 41 with that build's past-tense reading off
    and in 43 with it on, a reading no release has (the 68 include 24 where
    nothing should close, and in each run one of those closed a fact still
    true). Of the 68, 10 told a change in the past tense that the engine
    could reach: the writer closed the earlier value in at most 2 of the 10
    (2 in one run, 1 in the other), so it left the old fact open in at least
    8. Ten cases are a warning, not a rate. On that writer a price told
    twice (50 euros, then 55) left the first memory with no fact 10 times in
    30 (Korely Cloud 1.0.0, 31 Oct 2026); on 15 Nov it ended right in both
    runs, too few to call it solved.
  - Gemini 3.1 Flash-Lite (the region `global`): on 31 Oct 2026, on the
    engine of that day and the 50 cases the bench had then, 43 ended right,
    5 wrong and 2 were not scored. Not measured since.

  The same end user can also appear as two subjects (above), and "te verde"
  and "tè verde" are two things. 1.0.3 fixed one cause of it for a
  gpt-oss-120b writer chosen on the Models page (the Cloud's European one
  among them), which until then did not get the extraction rules measured
  for it (see the CHANGELOG). Up to 1.0.7 those rules went to any writer
  with `gpt-oss` in its model name. From the release after 1.0.7 they go to
  a writer whose model name names gpt-oss-120b, whichever host serves it and
  however it spells it (`gpt-oss-120b` on Scaleway and OVHcloud,
  `openai/gpt-oss-120b:nitro` on OpenRouter, `gpt-oss:120b` on Ollama,
  whatever the letter case). The name is all the product sees, so a
  fine-tune named after those weights (`...-gpt-oss-120b-finetuned`) gets
  them too; another gpt-oss (gpt-oss-20b, `gpt-oss-safeguard-120b`), or a
  name that does not say the size (a model served as `gpt-oss`), writes
  without them, as every writer they were not measured on. With them,
  in a bench of ours that is not part of
  this release (the 14 hardest change cases, three runs each, the real writer,
  one session), the changes that ended right went from 0 of 33 to 7 of 33: a
  small sample, better and not solved. What to do: tell the new state in the
  present tense ("Tom lives in Munich now"), or, when you know a fact ended,
  end it yourself: while it is still open, correct it by its id (`PATCH
  /v1/facts/{id}`, `korely_correct_fact` over MCP), forget it with the date
  it ended (`POST /v1/facts/{id}/forget` with `at`), or post it in the past
  tense (`POST /v1/facts` with `tense` `past`), which closes the open fact
  it restates.
- **A fact true again after a deletion does not see a contradiction told in
  another relation.** Deleting the memory that closed a fact gives the fact
  back unless something open still says otherwise of the same subject, in the
  fact's relation or in the relation of what closed it ([Time](time.md)). No
  model is asked on a deletion, so a value filed under a third relation is
  not read as one: "I live in Paris", "now I live in Zanzibar", and Rome
  stored under a relation of its own ("relocated to"); forgetting the
  Zanzibar memory gives Paris back beside Rome, where the judge, on a write,
  would have weighed the two. Close the stale one yourself with `forget` and
  its `at` (`korely_correct_fact` over MCP).
- **On a deletion, one value under two names in two agents is two values.**
  A deletion compares names as the store holds them, and a name is resolved
  in the memory of its own agent (an alias is a statement of one agent
  namespace). Paris told to agents a and b, and known to b as "Parigi" by
  an alias of b's own: deleting the memory that closed both gives back the
  copy that began earlier (its `valid_from`: the memory's `timestamp`, else
  the date its text states, else when it arrived), and that open copy keeps
  the other closed, as another value would
  ([Time](time.md)). No model is asked on a deletion, so in
  doubt the copy stays closed, as history.
- **A new value closes only the copies its judge reads.** A write weighs a
  bounded set of earlier facts: the ten newest of the same subject and
  relation, and the ten nearest by meaning (`memory_core/candidates.py`;
  for a fact written directly or a correction, the ten nearest by meaning
  about the same subject, and a fact written directly is among another
  fact's nearest only about that fact's subject; since 1.0.13 the same for
  a fact of an agent's memory with no `user_id`).
  A value told to more agents of one end user than that is closed only in
  the copies read: in a test with Paris told to 25 agents, "now I live in
  Zanzibar" closed 10 and left 15 open beside it. Deleting that memory gives
  back the 10 it closed, and `facts_reopened` says 10. Close the others
  yourself with `forget` and its `at` (`korely_correct_fact` over MCP).
- **A fact whose end was brought earlier still points at what was to
  replace it.** A fact the judge closed on a day still ahead, and that you
  forget or post in the past tense, or a memory says is over, or an edit
  takes away, before that day, ends where it was ended, and since 1.0.14 a
  deletion of what was to replace it no longer gives it back while that
  word stands (see the CHANGELOG). Its row keeps `invalidated_by`, the fact
  that was to begin on that day: `GET /v1/facts` with
  `include_invalidated=true` shows it, so the dashboard calls the fact
  superseded rather than ended; a correction of it answers 409
  `fact_not_current` naming that fact as the one to correct; and when a
  memory's statement brought the end earlier, its `fact.invalidated`
  webhook names that fact too (a forget's names none). Read the dates:
  `invalid_at` is when the fact ended, whatever `invalidated_by` names.
- **Deleting the memory that said a fact was over gives it back only when
  nothing else closes it.** "I live in Paris", then "I no longer live in
  Paris": deleting the second memory gives Paris back (since 1.0.14). When
  the judge had also named a value for Paris on a later day (Zanzibar from
  tomorrow) and that value still stands, deleting the memory that said
  Paris was over gives nothing back, and Paris stays closed from the end
  that memory gave it, also on the days before Zanzibar begins; deleting
  Zanzibar's memory after it gives Paris back from tomorrow. Post the fact
  again (`POST /v1/facts`) to make it true sooner. A fact the judgement of
  that statement closed ("I love Paris", the same two entities) follows
  Paris: it comes back only when Paris does, and stays closed while Paris
  does. One order keeps it closed for good (measured on 1.0.14-dev, 10 Nov
  2026): when two memories say Paris is over and only the first one's
  judgement closed "I love Paris", deleting the first and then the second
  gives Paris back but not "I love Paris"; deleting them in the other order
  gives both back. Post it again (`POST /v1/facts`) to make it true.
- **The facts ended in place before 1.0.14 carry no record of who ended
  them.** A deletion knows who ended a fact in place from what that ending
  wrote when it happened: a memory's past statement or an edit, since
  1.0.14, records the memory (`agent_fact_ended_by`, every memory that says
  it); a forget or a correction by hand has written its record since 0.1.1,
  and a past fact posted by hand writes one of its own (scope `past`) since
  1.0.14.
  Nothing guesses it for
  the rows ended earlier. So a fact a memory said was over, or an edit took
  away, before the day the judge had closed it at, in 1.0.13 or earlier,
  reads as until 1.0.13: deleting the memory of the value dated ahead gives
  it back while the memory that said it was over is live; and deleting the
  memory that said it was over gives it back no more than in 1.0.13. The
  same for a past fact posted by hand before 1.0.14. Forget the fact by hand
  (`POST /v1/facts/{id}/forget`) to make it your word.
- **A forget with an `at` ahead loses that date when the value that closed
  the fact sooner goes.** Forget Paris with an `at` five days ahead, then
  write "From <tomorrow> I live in Zanzibar": the judge closes Paris
  tomorrow, sooner than your date. Delete Zanzibar's memory and Paris
  stays closed from tomorrow, not until the day you gave: a forget is your
  word and is never given back, and its record keeps neither the `at` nor
  the end it moved (until 1.0.13 Paris came back true for good). Post Paris
  again from tomorrow (`POST /v1/facts` with `valid_from`) and forget it
  with your `at`.
- **An edit holds a forgotten fact only when it is the same fact of a
  triple the memory stated** (since 1.0.27). A fact you forgot, corrected
  or posted in the past tense, and have not posted again since, stays over
  when a memory that stated it is edited and the new text states the same
  subject, relation and value as the store names them (what memories said
  of it in between, and what became of them, does not count: your forget
  is the last word until you post the fact again). A restatement the store reads
  under another relation ("relocated to" for "lives in") is another fact and
  is written; another memory that never stated the fact, edited to say it,
  writes it as a new memory would (`POST /v1/memories` writes a forgotten
  fact again too: a new memory is a new statement). A fact closed by a
  newer value, not by you, is written again by an edit that states it. A
  correction stays true through an edit of its memory; a correction is told
  from a forget by the store's own record (the forget record written when
  the fact ended, and the fact pointing at the value that begins where it
  ends), so a forget of a fact the judge had already closed, with a pointer
  to its successor, does not make that successor a correction.
- **A value posted by hand and then forgotten by hand keeps closed what it
  closed.** "I live in Paris", then Zanzibar posted with `POST /v1/facts`,
  which closes Paris; forget Zanzibar (`POST /v1/facts/{id}/forget`): Paris
  stays closed, as it always has, since a forget says Zanzibar was true
  until then. Deleting a memory that also said Paris was over ("I used to
  live in Paris") does not give it back either, although since 1.0.14 such
  a deletion gives back a fact whose closer was retracted with its
  memories: a closer with no memory is never read as retracted (a design
  choice, kept by a test). Post Paris again to make it true.
- **The order of the memories in the context block is measured only for an
  end user's turns.** A run of an end user's turns written one after the
  other is printed in the order said, at the place of the most relevant of
  them (measured on all of LoCoMo for 0.1.23, against a reading of the same
  day: 1,255 answers against 1,245).
  Since 1.0.22 a memory with no `user_id` is in no run and keeps the
  search's order; that change is not measured with a reader, and turns of
  one person's conversation written with no `user_id` are no longer printed
  as one exchange (write them with `user_id`). Inside an end user's run the
  oldest turn comes first: at a small budget (400 tokens) the most relevant
  turn, when it is the newest of its run, can be cut to its first words
  behind the older ones. Changing that would change the benchmarks' blocks,
  so it waits for a measure. Tests rebuild 80 blocks of the benchmarks'
  shape: since 1.0.30, 14 that ended with a fragment of a memory cut after a
  word end otherwise (the fragment is left out, and in 4 a shorter memory
  after it comes in); the other 66 are identical to 1.0.21's.
- **A sentence ends where Unicode's default rules end it, abbreviations
  included** (since 1.0.30). The block cuts a memory only after a complete
  sentence, as UAX #29 section 5 finds it, which, as the annex itself says,
  "cannot detect cases such as '...Mr. Jones...'": a full stop after an
  abbreviation or a list number followed by a capital ("Mr. Jones",
  "1. Buy milk") ends a sentence there. Every cut of a memory, after a
  sentence or marked "…", shows at least 40 characters as a reader sees
  them (Unicode's extended grapheme clusters: a letter and its combining
  accent are one, a family emoji of four joined people is one; the day in
  front included), so it never stops after "Mr." alone, but it can stop
  after "Mr. Jones said so. Mr." when that is past 40; a memory of which
  not even that fits is left out and named. 40 such characters take more
  bytes in some scripts (Thai or Chinese about 120, Thai with its marks
  about 360, a family emoji 25 each), so at a small budget such a memory is
  left out sooner than a Latin one of the same length in characters. A text whose sentences begin with a
  lower-case letter after the full stop ("ok. then i left.") is one sentence
  by the annex's rule SB8 (a full stop before a lower-case word ends none,
  as in "e.g. the"), and so is a text with no terminator (a list on one
  line, Thai): such a memory, when no sentence ends in its first 1,200
  characters, is cut after a word and marked "…", as before 1.0.30. Over MCP
  the agent is told how many memories were left out, not which
  (`memories_left_out` is REST's). The effect on the answers was measured
  once, on 16 Nov 2026, on one LoCoMo conversation (152 questions, a
  gpt-oss reader and judge): the blocks of 95 questions change, and the
  answers lost stay within what a second read of the code of before changes
  by itself (6 answers), the criterion written before the run. One
  conversation shows only a large loss; the benchmarks whole are not
  measured.
- **The sentences of a long note the context block shows are chosen by
  shared words, and only for memories with no end user.** Since 1.0.31 the
  first memory of a block is whole as before, and the second to the fifth,
  when they have no `user_id` (an agent's notes), share the room that is
  left: one too long for its share is shown by its whole sentences that
  hold the most of the question's words, each word weighing more the fewer
  of the notes found hold it. Words match as written, case, compatibility
  forms and accents aside, and by pairs of characters in the scripts
  written without spaces (Chinese, Japanese, Thai): an inflected form, a
  synonym, a question in another language than the note choose no
  sentence, and the note is then cut after its first sentences, as in
  1.0.30. Hangul is paired like the ideographs although Korean writes
  spaces, and a word of one syllable or one ideograph is a term alone: it
  does not meet the pairs its longer or inflected forms make. Dropping the
  nonspacing marks so that an accent or a vowel mark does not keep two
  spellings apart also makes one term of words that differ only by such a
  mark (tones in Vietnamese, vowel signs in Thai and Devanagari). A script
  written without spaces that is neither in Unicode's line-breaking class
  SA nor wide in East Asia (Javanese, for one) is not paired, so its
  unspaced runs match only whole. The weights read every memory the search
  brought once more: nothing to notice on notes of 700 characters, up to
  0.9 s when all of them are hostile texts of 16,000. The 40 characters of
  a cut are counted on the memory's own words only for the sentences
  chosen by the question; a note cut as in 1.0.30 counts its day too. The
  search is unchanged: a note whose answer is one sentence
  among many has one vector for all of it and can rank below the notes
  shown (the windows of `MEMORY_WINDOWS`, off by default since 1.0.33, are
  the step for that: the next item). A short line after the fifth memory can be left out where 1.0.30
  showed it (it is named in `memories_left_out`). Checked without a reader,
  on one team's notes in Italian (459 notes, twenty questions of known
  answer): whether a model answers better from one note and the sentences
  of four than from one note and the opening of another is not measured.
  An end user's memories are packed as in 1.0.30, the blocks the benchmarks
  measured.
- **The windows of long memories are off, and measured only without a
  reader.** `MEMORY_WINDOWS` (1.0.33, off by default) gives a memory longer
  than 100 words a vector for each window of 100 words, a new one every 50,
  and a search ranks the memory by the better of its own vector and its best
  window's. A window is shorter and more focused than its memory, so its
  cosine to a short question is higher than the memory's: a long memory
  gains a step on a short one by its length, and the more windows it has,
  the more chances one of them has to come close. On one store of an agent
  team's notes in Italian (488 notes, twenty questions of known answer, the
  local vectors, no reader; 17 Nov 2026), windows of 40, 60 and 80 words
  moved other notes in front of the answer and lost a question or two; 100
  words lost none and brought one in, at 800 and at 1,500 tokens. The size
  was chosen on those same twenty questions, and one question separates 80
  words from 100: a signal, not a proof. On LongMemEval (half its turns are
  longer than 100 words) it loses: measured with it off and on, without a
  reader (the oracle split, 100 questions, every fifth; the local vectors, a
  budget of 4,000 tokens, a store with no facts; 18 Nov 2026), the block
  held 150 of the 177 turns that hold an answer with it off and 146 with it
  on; 4 questions lost one of those turns and none gained one. There a long
  memory with many windows is ranked by its best one and takes the place of
  a short turn that holds the answer: the long turns are mostly the
  assistant's, and the answer is often in a short turn of the user's. So it
  stays off, and the steps set before measuring stopped at this first one,
  which calls no model. LoCoMo has no turn that long, so nothing of it
  changes. A window is placed in the line of the
  block sentence by sentence: where the line counts other sentences than the
  text (a form feed or a vertical tab inside a sentence), the line is cut as
  before. Among the window's sentences the ones that hold the question's
  words come first, then the ones that hold more of the window, so in a room
  too small for the whole window a sentence that answers in other words than
  the question's can be left for a longer one.
- **The windows cost CPU, or Gemini calls, and space, and no plan counts
  them.** One vector per window, one window per 50 words beyond the first
  100: a note of 130 words has 2; the longest memory (16,000 characters) has
  39 in Latin words of seven letters and 159 in ideographs, whose
  characters hold more text each (a script written without spaces is
  counted by pairs of letters). The worker computes at most
  20 a pass and a pass 30 seconds after the end of the one before, up to
  2,400 an hour (a fixed pace, not a setting), with the local
  embedder on this machine's CPU, and with `EMBEDDING_PROVIDER=gemini` as
  one billed call to Google each. In the database a window weighs about
  8.5 KB (measured on 2,000: 4.3 KB of row and vector, 4.2 KB of index, the
  HNSW one nearly all of it): with a window every 50 words, about 20 to 28
  times the bytes of the text (350 bytes of Latin words, 300 of ideographs,
  per window). The Cloud's plans count writes and
  reads, not windows nor their vectors.
- **How the context block splits its room between facts and memories is not
  measured for quality.** `GET /v1/context` pays the reader note (723 bytes,
  from 400 tokens) out of both shares (the part of the token budget given to
  facts and the part given to memories) and, with no memory to show, gives the
  facts the whole room (see the CHANGELOG). We measured what goes in the block
  at each budget, in bytes and counts; no benchmark has read the new blocks
  and graded the answers. The two benchmark scripts ask for 4,000 tokens
  (LongMemEval) and 2,000 per speaker (LoCoMo). For facts of ordinary length
  the block should be the same as before there, but that is arithmetic on
  those budgets, not a rerun. At 800 tokens, the default, the block can hold
  fewer memories than before.
- **The context block does not say when the answer is missing.** It brings
  the memories and facts nearest to the question, up to its budget, also
  when none of them answers it, and its scores did not tell the two cases
  apart. On our team's memory (Korely Cloud 1.0.11, 6 Nov 2026) "how much
  does the Pro plan cost a month?", which the memory does not hold, came
  back with 798
  tokens and 9 lines, among them prices of other things (a spending cap, a
  cost per user); its first score, 0.47, was higher than those of two
  questions whose answer was there (0.36 and 0.43), and a `min_score` of 0.5
  emptied it but emptied 75% of the questions that had their answer too.
  The note at the head of the block tells the reader not to abstain when the
  information is present; a note telling it to say when the answer is
  missing was measured on LongMemEval questions (8 Nov 2026) and left out,
  because the reader then refused answers the block did support. This
  version has no signal for a missing answer either. What to do: before an
  agent uses a line of the block, have it check that the line names the
  thing asked; with no such line, treat the answer as not found.
- **A search of the facts and the memories together is not measured for
  quality.** `types: ["memories", "facts"]` on `POST /v1/memories/search`
  and `korely_search` (since 1.0.13) merges the two rankings by rank, an
  alternation in which a memory comes first at equal rank, because how a
  fact's cosine compares with a memory's for the same question was not
  measured: the order says nothing of which kind answers better, no
  benchmark has graded answers read from it, and with `limit: 1` the best
  memory wins however close the best fact is. The facts are ranked by
  meaning alone, not by the context block's recency weight, so a fact
  confirmed yesterday and one a year old are ordered by how close they are
  to the question, and they have no `min_score` of their own (a floor with
  the facts is a 422). Tested with scripted vectors only.
- **The SDKs do not search the facts or the whole text yet.** In
  korely-memory (Python 0.1.28, JavaScript 0.1.17), Python's `search()`
  takes a fixed list of arguments, none of them `types` or `full_text`, and
  its `SearchHit` keeps only the six fields of 1.0.12, so `type`, `content`
  and `fact` are dropped from what it returns; JavaScript's `search()`
  refuses an option it does not know, `types` and `full_text` among them.
  Until they are updated, call `POST /v1/memories/search` directly, or the
  MCP's `korely_search`.
- **The model's usage is the account's, not a project's.** A call to the
  model is recorded with its agent, its stage and its tokens, not with its
  project, so `/admin/usage`, the dashboard's Usage page and the model's
  card and sidebar line count every project together, whichever project the
  switcher shows, and say "account". The memories, facts, agents, requests
  and audit trail on the dashboard follow the switcher.
- **A `GET` of `/v1` takes `run_id` without reading it on two routes.**
  Since 1.0.23 every `GET` of `/v1` answers 422 to a query parameter it
  does not take, naming each; `GET /v1/context` and `GET /v1/facts` still
  take `run_id`, which the n8n node 0.1.0 sends from its Run ID field, and
  answer as if it had not been sent (reads take no run; the node 0.1.1
  refuses it before sending). The routes of `/admin` still ignore a
  parameter they do not take.
- **`GET /v1/users` lists an end user once their data is stored.** Since
  1.0.23 it lists every end user the project holds anything of, as the
  erasure finds them (memories and facts forgotten or closed included, a
  name declared, the reads of a named reader, what was learned of them),
  with zero counts and no `last_active` when nothing of theirs is live,
  until their erasure. The items of a `/v1/batch` not imported yet are not
  read for it: their end users are listed once the job imports them, and
  `DELETE /v1/users/{id}/memories` erases a queued job's items too.
- **The dashboard's search is filed with its end user only when it was
  asked for one.** Its lists of memories and facts are filed once per
  project and end user whose rows they showed (since 1.0.23), and so is the
  text the Audit log opens; a search of the whole project keeps one row,
  with the ids it returned and no end user, so `GET /v1/audit?user_id=`
  does not find it. The reads of `/v1` without a `user_id` are filed the
  same way.
- **The Requests page counts the calls of your keys and apps, not the
  dashboard's own reads.** Every page an operator opens reads the memory,
  and each read is filed in the audit trail (the Audit log shows it, with
  the operator's name); since 1.0.22 the Requests page and the Home page's
  chart leave those reads out, as the plan always did. A change made from
  the dashboard (a fact corrected) still counts as a write. Both count from midnight UTC of the first day of the range,
  today included.
- **The dashboard on a phone is checked in one browser.** Its pages are
  drawn and measured at 390 by 844 px with a touch screen, in both themes,
  in Chromium (Playwright); Safari, Firefox and a real phone are not tested
  yet. Its colours are measured against WCAG 2.2 AA (4.5:1 for text) on
  the colours themselves, at rest: the theme's buttons, errors and
  warnings, every status written in a colour of the palette, and the
  sign-in, setup and consent pages. Not measured: the colour a button takes
  under the mouse. Under 4.5:1 still: the small grey labels on grey chips
  (a fact's family, an end user, a route; 4.4:1 in the light theme), the
  blue "Default" badge of a project (4.49:1 light, 4.25:1 dark), and the
  greyed rows of a revoked key (1.9-3.3:1).
- **Windows is tested only on Windows Server 2022.** Korely was built from
  source, started, written to and searched on Windows Server 2022 with Docker
  Engine inside WSL2 (31 Oct 2026, version 1.0.2). Windows 10 and 11 with Docker
  Desktop use the same WSL2 and are not tested yet, and neither are the
  installer and an upgrade from an earlier version there.
- **An Idempotency-Key covers two routes, and its retry has no facts.**
  Since 1.0.27 `POST /v1/memories` and `POST /v1/batch` make a request sent
  again with the same key once ([Retrying a
  write](api.md#retrying-a-write-the-idempotency-key)); `POST /v1/facts` and
  the MCP's `korely_add` do not take a key, so a request retried there is
  written, and on Korely Cloud counted, again. The answer to a retry is not
  the first answer kept, because keeping it would keep the memory's text a
  second time: it is the memory built from the retry's own request (the
  same request, by its fingerprint) with the memory's id, status and times
  now, and `facts: []`; the job is its id, status and count now. With
  `WRITE_ASYNC=false` the first answer carried the facts extracted inline
  and a retry carries none (read them with `GET /v1/memories/{id}`). Its
  `status` and `updated_at` are the memory's now, so a retry does show
  that the memory was edited or extracted since, never what was written.
  With `WRITE_ASYNC=false` a retry that comes once the memory is stored and
  before the first answer gets the memory as its request wrote it, not a
  409. A batch with a row of an erased end user loses its key with the
  erasure, so the same batch sent again is queued again whole, the other
  end users' rows too. The 409
  while a first request is being made is proved in the tests with a second
  database session holding the key; it was not measured under load.
- **install.sh does not upgrade, and the release's checks do not upgrade
  its folder.** A folder install.sh made is upgraded by hand, with the steps
  in [An install made by install.sh](operations.md#an-install-made-by-installsh).
  `scripts/acceptance_survives.sh`, run before every release, upgrades a
  checkout that holds data, not such a folder. The upgrade's block ran
  against the public v1.0.21 (15 Nov 2026), on a folder whose database was
  a stand-in: the four files checked, `.env` given the image by its digest,
  `.korely-install` its release. The start that follows, on a real
  install, and the way back were not run. The block goes only to the
  release main's install.sh names, the one whose digest is written there.
- **A restore's file of credentials is checked only on an install with a
  `SECRET_KEY`, and not for every change.** With a `SECRET_KEY`, the file
  `restore.sh` keeps (`manage credentials-export`) is signed and its age
  checked: a file changed by hand, written by another install, or older
  than the credentials the data holds is refused
  ([Backups](operations.md#backups)). With no `SECRET_KEY` nothing can sign
  it: the replay reads any file it is given, with a warning, and checks its
  age only when the file says it. And the age is read from what the data
  dates: a password reset with `manage reset-password` and no login after
  it leaves no moment, so an older file brings back the password it holds.
  A file of 1.0.31, read with `credentials-replay --unsigned-from-1.0.31`,
  is checked for neither: whoever wrote the file decides the passwords and
  webhook secrets it holds, and an older file brings older ones back.
  Since 1.0.32 (1.0.31 signed and dated no file).
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
- **Agent notices tell at the next context block, and only there.** An
  agent learns of a change when it next asks for its context block with
  `reader`; nothing is pushed, and `GET /v1/facts` keeps marks but tells
  nothing (a door of its own for every change waiting is not there yet). A
  change is told at most once, at most 3 a block (the rest are counted in
  `changed_more`), whether the agent reads the section or not, and an
  answer that fails after reserving them (the response lost on its way, an
  error after the block was built) loses them. The section takes room only
  after the known facts, from the memories' share, at most half of it when
  memories follow and never what the first memory needs to be shown (its
  heading and its shortest fragment): so with `reader` the block has the
  same known facts and the same `stable`, a block with memories keeps one,
  and a block that its known facts fill, with no memory, has no room for
  changes (they wait for a larger `token_budget`). So in a block with
  memories a small budget can tell nothing where a smaller one did, and
  where depends on the facts: in the tests' block of an end user (four short
  facts, three memories, two short changes), from 155 to 185 tokens the
  first memory fits and keeps its floor, and the room left for the section
  (94 bytes at 175 tokens) is less than the 148 a short change takes on the
  first line; below 155 no memory fits and the changes take the room; from
  190 tokens one short change is told, from 220 two (measured by the review
  in steps of 5 tokens); with the facts of 25 changes in the block the empty
  band was 175 to 225. It looks at the 20 newest changes and tells the
  newest that fit, a change too long for the room written short (each fact
  cut with an ellipsis) in its share of the room, the changes that wait
  after it counted, those not looked at yet too; one that does not fit its
  share waits while the others are packed and is then told in what they
  leave, alone if need be: a long change takes at most its share while
  others wait behind it. When, those packed, a place is free, a change of
  the 20 was passed over and the room left holds the smallest line a change
  can take (one that no longer holds, without its words: 42 bytes, 33 more
  for the heading on the first line), the block asks once for the next ones,
  up to 200 in all, and packs again: so a change that cannot be told there,
  for its words or its length, keeps out no short change the block has room
  for, as long as fewer than 200 such changes stand ahead of it; an older
  one waits as long as 200 stand ahead of it, counted in `changed_more`.
  That lookup has its cost: a queue made so, on purpose or not, asks for it
  at every read for as long as it stays so, and the review measured 33 SQL
  statements instead of 26 and 139 ms of CPU to pack 200 changes, at most
  one lookup more of at most 200 a read. One that does not fit even alone
  waits for a block with room: written short, a change needs 126 bytes (135
  when it was stated by a caller or read from a memory, 85 and 94 when it no
  longer holds), plus one for its line break, 33 on the section's first line
  for the heading. Newest first has its cost: a writer that changes three or
  more facts the reader had before every read keeps an older change out for
  as long as it goes on (counted in `changed_more`; in the review's probe,
  at 300 and 800 tokens, never told in 12 reads with three new short changes
  before each, told at the first read with one or two; at 400 tokens that
  block had no room for a change at all, the empty band above). Two facts
  that begin the same are each cut to show where they differ (the beginning,
  an ellipsis, the part that differs), and a change where not even that fits
  is not told short, so it waits for a block with room, keeping none out.
  Every cut of the block, of a memory as of a change, falls between two
  characters as a reader sees them (Unicode's extended grapheme clusters),
  except after the few Prepend signs of UAX #29 (U+0600 to U+0605 and some
  others), which the rule does not read.
  It is the reader's whatever the block's `agent_id`: an agent that reads
  several namespaces under one name is told the changes of all of them in
  any of its blocks (to keep them apart, read under two names).
  It does not follow `exclude_sessions`: what holds now may be a fact that
  only work sessions state. A read that failed (the search down and
  nothing to show) tells no change. A notice is
  as right as the closure it tells: in an agent's memory with no end user
  only the explicit ones (corrected, forgotten, stated by a caller) are
  told unless the read asks for `changes=all`. A fact ended in place by a
  past statement reads "read from a memory" whoever said it, a caller's own
  past statement (`POST /v1/facts` with `tense: past`) included: the store
  records that one, so that no deletion gives the fact back, but a notice
  does not tell it apart from a memory's, so such a change is never among
  the explicit ones. A correction or a forget whose transaction began before
  the fact itself was written (a race of moments: the end is then recorded
  at the fact's own `created_at`) reads the same way, never as explicit. A chain of what replaced a fact that passes through a fact
  the key may not read says "no longer true", even when a later fact of the
  chain is the key's. A fact true again in another row (given back after a
  deletion, or the same fact told again) is no change while that row is
  true; if that row ends later, a reader handed only the first is told it
  no longer holds, without what replaced it, and a reader handed both is
  told it twice in one block, once with what replaced it and once as no
  longer true (measured on 1.0.15-dev, 10 Nov 2026). A fact whose memory was
  forgotten is told without its words. A change of a fact written after
  extraction is known only once
  extraction has run. The reader is the name the
  caller gives, not checked against the key: with one key for several
  agents, or several keys in one project, one agent can read under
  another's name, leave marks under it and be told the changes waiting for
  that agent, which is then not told them (never seeing more than its own
  key). A name that only ever read is reached by `DELETE /v1/agents/{id}`
  but is not listed by `GET /v1/agents`, which lists the namespaces
  written under. Only facts are marked, never memories; only the context block and
  `GET /v1/facts` (and their MCP twins) mark, and only a read of what is
  true now. The marks of a project deleted without erasing it go with it,
  although its facts move to the Default project.
- **What a mark costs, and what it waits for.** A mark is written in a
  session of its own before the answer is sent: one row per fact handed
  over, about 322 bytes each, indexes included (8,000 rows measured). Measured on a laptop
  (Docker, in process, no network, 8 Nov 2026), 200 reads with `reader` and
  200 without: with `GET /v1/facts` handing over 40 facts it added 1 to 2
  ms at the median and 0 to 4 ms at the 95th percentile with the machine
  lightly loaded, and 5 to 6 ms at the 95th percentile with it busy; with
  200 facts a read, the most a page holds, on the busy machine, the median
  went from 8.5 to 13.4 ms and the 95th percentile from 12.6 to 31.3 ms.
  The context block hands over at most 10 facts. Not measured on a server.
  A mark waits at most 0.2 s for a lock another transaction holds on its
  rows (an erasure of the same facts, a deletion of the project or of a
  fact, a late vector judgement, a lock on the table) and 1 s for its
  statement, then it gives way: the read is answered all the same,
  `/health` counts it under `records.read_marks.gave_way` and stays `ok`. A
  fact another transaction holds longer than that stays unmarked, alone:
  the read's other facts are marked first, at once (tested with these
  deadlines: a deletion held for a second leaves three facts of four
  marked, and the mark returns in under 0.8 s). The context block's look at
  the changes and its reservation of the ones it shows wait the same 0.2 s:
  under a lock on the marks' table (a migration) a block with `reader`
  comes without the section (in under a second in the tests),
  `changed_more` null, and the changes wait for the next read.
- **What telling the changes costs, measured.** Four queries, whatever the
  number of changes asked: the changes with their origins and their count,
  every chain of what replaced them at once (walked by the database,
  however long), the facts' rows, and their memories (whose words may be
  told); a block that shows changes adds the one that reserves them. Measured on a laptop
  (Docker, in process, no model, 8 Nov 2026, the two versions one after the
  other on the same machine):
  - 10,000 facts of one end user, 20 readers each with a mark on every fact
    (200,000 marks), 1,000 of those facts ended since, every one a change
    still to tell each reader: the first 3 changes and the count of the
    other 997 took 20 ms at the median and 90 ms at the 95th percentile
    (144 and 200 before this version's review), 200 changes 38 and 88 ms
    (243 and 285 before; 377 and 522 in the first measure, on a busier
    machine). In that store `GET /v1/context` with `reader` took 39 ms at
    the median and 50 ms at the 95th percentile against 25 and 31 without
    (122 and 133 with `reader` before), and `GET /v1/facts` 16 against 13.
  - 200 changes, each down a chain of 61 facts that replaced one another:
    158 ms at the median (2.3 s before, and every one of the 200 said "no
    longer true" though a value held at the end of each chain: chains were
    followed 50 links at most).
  - 3 changes waiting for a reader among its 10,000 marks: 14 ms at the
    median and 18 ms at the 95th percentile (82 and 159 before); with 5,000
    forget records in the account, 19 and 32 ms (667 and 767 before: the
    records were read once for each change).
  Not measured on a server, nor with more than one request at a time.
- **No ceiling on the readers of a project.** Every name a caller gives
  is a reader of its own, and each keeps its own row for every fact it
  read, so a project read under many names keeps that many rows per fact,
  until they are 90 days old. A ceiling is not decided yet.
- **Declining an app's connection still sends the browser to the app.**
  `/authorize` redirects only to the consent page, and an app's address
  is reached only from there, once a person signed in and saw its host:
  approving or declining both send the browser back to that address (with
  `error=access_denied` when declined), as OAuth expects. Any https address
  can be registered, so the consent page's host line is what tells a
  person where they are sent (RFC 9700 section 4.11.2 leaves it to them).

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
- **An erasure made again keeps a restated fact with its words of before.**
  When a restore makes again the erasure of someone who wrote again after
  it, a fact that a memory written since restated stays, without the
  memories of before among its sources, but with its row's first wording,
  its start (`valid_from`) and the date the store learned it: a restatement
  records only which memory said it again. A memory written before the
  erasure goes whole, also when edited since, and a fact posted again by
  itself, or a name declared again, in the same words since, goes: neither
  leaves a date of its own on the row. A correction made since of a fact a
  memory of before first stated is made of that memory's words and goes,
  and the fact it corrected stays closed, as the correction said (the same
  as when that one memory is erased). A fact that comes back open because
  the fact that had closed it goes is kept closed only by a value that stays
  in its own relation or in the closer's: one that stays in another relation
  ("relocated_to Rome" beside "lives_in Turin") does not stop it, since no
  judge is asked on an erasure.
- **Deleting an agent namespace takes its facts that another namespace
  restated.** A fact of the namespace that a memory of another namespace
  states too is deleted with the namespace; that memory stays, without the
  fact, until it is written again. A restore that makes the deletion again
  does the same.
