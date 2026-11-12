# The dashboard

The screen of a Korely Agent install: API keys, memories, facts and their
history, the graph, agents, projects, webhooks, requests, model usage, and the
model provider's key.

It is not deployed on its own. The image builds it (the first stage of the
`Dockerfile`) and the application serves it at `/` from the same process that
holds the memories, so it calls the API at the address it was loaded from and
there is no URL to configure.

## Signing in

With an operator's username and password, made on the install:

```bash
docker compose exec api python -m korely_agent.manage create-operator \
  --tenant <tenant id> --username you
```

The password is printed once. Signing in opens a session (`POST /admin/session`,
a `kor_sess_` token that expires after `SESSION_HOURS`), and the screen sends
that token and nothing else. The `kor_self_` API keys are for your agents
calling `/v1`; the screen never uses one. For single sign-on, put your own
gateway in front and set `TRUST_AUTH_HEADER` (see `.env.example` at the root).

## Working on it

Vite, React, TypeScript and Tailwind. With an install answering on port 8000:

```bash
npm ci
KORELY_BACKEND_URL=http://localhost:8000 npm run dev    # http://localhost:5174
```

`KORELY_BACKEND_URL` has to be in the shell: `vite.config.ts` reads it from the
environment when it starts, and a `.env` file in this directory is not read
for it. Left unset, the page calls the address it was served from, which is
what every build that ships does.

`npm run build` is `tsc --noEmit && vite build`, the same command the image
runs, so a type error fails here before it fails an image build.
