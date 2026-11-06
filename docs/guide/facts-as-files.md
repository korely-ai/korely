# The facts, as files you can read

Put `FACTS_EXPORT_DIR=/data/facts` in `.env` (the line is there, commented),
`docker compose up -d`, and a worker writes the store out as Markdown too, every
five minutes, one file per end user id of the account, into
`./facts/<tenant id>/<end user>.md` on this machine: the same id in two
projects, or from two agents, is one file. An id that is not already a plain lowercase name (`c4821`,
`maria.rossi`) is written safely with `~` and a short digest after it
(`Anna~3f0c...md`, `repo_a~9b1e...md` for `repo:a`), so two end users never
share a file; the id itself is the file's first line. The database stays the
source of truth and nothing reads these back:

```markdown
# c4821

## What is true now

| subject | predicate | object | since | until |
| --- | --- | --- | --- | --- |
| c4821 | subscribes_to | Basic plan | 2026-06-20 |  |

## What was true before

Kept, not deleted. `until` is the day a later fact replaced it.

| subject | predicate | object | since | until |
| --- | --- | --- | --- | --- |
| c4821 | subscribes_to | Priority Support plan | 2026-03-15 | 2026-06-20 |
```

If it cannot write, `/health` says so under `export` (and turns `degraded`),
and `manage status` has an `export` line naming the directory.

`git init` inside `./facts` and `git diff` is a review of what your agent
learned this week. It is people's data, like `backups/`: keep that repository
off anything public. An erasure (an end user, an agent, a project erased)
rewrites the tenant's files at once (or, while another export is under way,
that export does before it ends) and removes the file of an end user with
no fact left; the next pass does the same for anything else that went. What
a git repository of yours already committed is yours to rewrite: the erasure
reaches the files, not their history. That is not decoration. A store can hold the wrong
dates and still pass every test and every benchmark, because an answer gets
reconstructed from whatever is in front of the model. A table you can read
cannot do that.
