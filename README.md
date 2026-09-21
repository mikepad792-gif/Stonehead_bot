# Stonehead_bot

The strain lookup Discord bot that connects to the StoneHead databases.

One slash command, `/strain <name>`. It posts the name to
`api/strain-lookup` on stoneheadai.com and returns StoneHead's answer as an
embed. No memory, no vibe tab, no accounts — those live on the site, behind a
login, which is the point.

Type a family name without its prefix — `/strain thunder fuck og` — and it
answers with a row of buttons naming the members it holds, rather than "never
heard of that one". Picking one locks the row, marks the choice, and edits the list into that
strain's card once the lookup returns.

Every card it posts carries a 🔁 reaction. Tapping it gets a different strain
with a close profile. **The strain is chosen server-side from a precomputed
table, never by the model** — a model asked for "something similar" reliably
names a strain that was never in the database.

## How it fits together

```
Discord  ──/strain blue dream──>  stonehead_bot.py   (always-on process)
                                        │
                                        │  POST, X-Bot-Secret header
                                        v
                          stoneheadai.com/api/strain-lookup
                                        │
                                        │  strain DB + safety layers + model
                                        v
                          { reply, matched, strain, tier, strain_data }

Discord  ──🔁 on a card─────>  stonehead_bot.py
                                        │
                                        │  POST, mode: "similar", source_strain
                                        v
                          stoneheadai.com/api/strain-lookup
                                        │
                                        │  precomputed neighbour table, then model
                                        v
                          { reply, strain, source_strain, strain_data }
```

The bot itself holds no data and makes no decisions about content. Retrieval,
the honest-miss rule, the crisis and substance intercepts, and the age floor
all live server-side in the StoneHead repo — so they cannot be bypassed by
anyone running a modified copy of this file.

**The AI model is not configured here either.** The bot never talks to
OpenRouter; it POSTs a strain name and renders what comes back. Which model
answers is `AI_MODEL_BOT` in the site's Netlify environment (blank inherits
`AI_MODEL`). There is nothing to set on the bot host — the only two variables
that belong there are `DISCORD_TOKEN` and `STONEHEAD_BOT_SECRET`.

## Before it will answer anything

Both halves have to be in place, and they share one secret:

1. **The endpoint is deployed** — `api/strain-lookup.js` in the StoneHead repo,
   with the `/api/strain-lookup` redirect in `netlify.toml` and migrations
   `012_bot_usage.sql` and `013_bot_user_state.sql` applied to the database.
2. **`BOT_SHARED_SECRET`** is set in the site's Netlify environment.
3. **`STONEHEAD_BOT_SECRET`** here is set to *the same value*.

If the secret doesn't match you get 401s in the bot log and "he's not answering
right now" in Discord. If the endpoint isn't deployed at all, the request falls
through to the site's SPA fallback, comes back as HTML with a 200, and reads the
same way from Discord.

## Running it locally

```bash
pip install -r requirements.txt
cp .env.example .env     # fill in DISCORD_TOKEN and STONEHEAD_BOT_SECRET
python stonehead_bot.py
```

It should log `connected as <name> — in N servers`. Slash commands are synced
globally on connect and can take up to an hour to appear the first time; they
show up immediately in a server the bot was just invited to.

## DEPLOY

This bot holds a persistent websocket to Discord's gateway. It is a
long-running process, not a request handler — **a serverless function cannot
host it.** Netlify, where the site itself lives, is the wrong shape for this
and caps functions at 10 seconds.

### bot-hosting.net

A free Pterodactyl-based host. The panel wording below is from their general
Python setup; if a field name differs, the concepts map one-to-one onto any
Pterodactyl panel.

1. Sign in at <https://bot-hosting.net/login> with Discord and create a
   server, choosing the **Python** egg.
2. Upload `stonehead_bot.py`, `requirements.txt` and `.env` through the file
   manager — or point the panel at this GitHub repo if your plan offers the
   pull-from-git option. **Upload `.env`, never commit it.**
3. In the **Startup** tab, set the app's Python file to `stonehead_bot.py`.
   `requirements.txt` is installed automatically on boot; if your egg asks for
   an install command instead, use `pip install -r requirements.txt`.
4. Set `DISCORD_TOKEN` and `STONEHEAD_BOT_SECRET` as panel variables if the
   Startup tab exposes them. If it doesn't, the `.env` file from step 2 covers
   it — `stonehead_bot.py` reads one when `python-dotenv` is installed.
5. Start it, and watch the console for `connected as ...`.

Whatever the host, two rules:

- **The token and the secret go in the panel or in `.env` — never in the code
  and never in the repo.** This repository is public.
- **Check the log after the first deploy.** `endpoint rejected the shared
  secret` means the two halves disagree, and it is by far the most common way
  this comes up dead on arrival.

### Anywhere else

Any always-on Python host works — Railway, Fly.io, a Render background worker,
a VPS under `systemd`. The requirements are the same everywhere: install
`requirements.txt`, run `python stonehead_bot.py`, set the two environment
variables, and keep the process alive with a restart-on-failure policy.

## Where it will and won't talk

The age gate is **on by default**: the bot answers in DMs and in channels a
server has flagged age-restricted, and nowhere else. In a server you don't
run, that flag is the only lever you have over who is typing, so it stays on.

**Threads inherit their parent channel's age restriction and cannot be gated
on their own.** Discord has no per-thread age flag, so a thread is answerable
exactly when the channel it lives in is age-restricted. Flag the channel and
its threads follow; there is no way to open one thread without the rest.

`REQUIRE_AGE_RESTRICTED=0` turns the gate off, **for local and staging testing
only**. Only the exact string `0` disables it — blank, `false`, `no` and
anything else leave it on, so a typo can't quietly open the gate. Never set it
to 0 in a server that isn't yours.

## Permissions it needs

Invite it with **Send Messages**, **Embed Links**, **Read Message History** and
**Add Reactions**.

- *Embed Links* — without it every answer is silently dropped, since the bot
  only ever posts embeds.
- *Add Reactions* — without it the cards still post, just with no 🔁 to tap.
  The bot logs that and carries on rather than failing the command.
- *Read Message History* — lets the recommendation land as a reply to the card
  it came from instead of as a loose message below it.

No privileged intents. The bot never reads message content; it sees slash
commands and reactions, both of which Discord sends without one.

## Tests

```
python3 bot-check.py
```

No Discord connection, no HTTP, no token — every boundary is faked, so the
assertions are about the bot's own decisions. The load-bearing ones are the
guards: a card titled with the wrong strain, a picker button a stranger can
press, and a reaction the bot answers itself are all silent failures.

## Environment variables

| Variable | |
|---|---|
| `DISCORD_TOKEN` | **Required.** Bot token from the Discord developer portal. |
| `STONEHEAD_BOT_SECRET` | **Required.** Must match `BOT_SHARED_SECRET` in the site's Netlify environment. |
| `STONEHEAD_API` | Optional. Defaults to the production endpoint; override for a deploy preview or `netlify dev`. |
| `REQUIRE_AGE_RESTRICTED` | Optional. Defaults to on. `0` disables the age gate — testing only. |

That's the whole list. In particular there is **no model setting here** — see
the note above on where that lives.

Install it by hand, per server, with the owner's permission. Do not list it in
the App Directory.
