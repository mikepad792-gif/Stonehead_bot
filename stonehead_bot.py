"""
StoneHead Discord bot — v1.

One command: /strain <name>. Calls api/strain-lookup on stoneheadai.com and
returns StoneHead's answer as an embed. No memory, no vibe tab, no accounts.

The bot is marketing. It's free, there's no signup, and the thing worth making
an account for (vibe, memory) is the part a public channel can't hold anyway.

Setup:
    pip install -r requirements.txt
    cp .env.example .env     # then fill it in
    python stonehead_bot.py

Deploying it somewhere that stays up: see DEPLOY in README.md. It holds a
websocket to Discord's gateway, so it needs an always-on process — a serverless
function cannot host it.

Install by hand, per server, with permission from the owner. Do not list in
the App Directory — you can't control who's typing in a server you don't run.
"""

import os
import asyncio
import logging

import aiohttp
import discord
from discord import app_commands

# Secrets come from the environment. Some panels — bot-hosting.net and other
# Pterodactyl hosts among them — give you a file manager more readily than an
# environment-variable editor, so a .env sitting next to this file is read too.
# Optional by design: a real environment variable always wins, and the bot runs
# fine when python-dotenv isn't installed.
try:
    from dotenv import load_dotenv

    load_dotenv(override=False)
except ImportError:
    pass

# ---------------------------------------------------------------- config

API_URL = os.environ.get("STONEHEAD_API", "https://stoneheadai.com/api/strain-lookup")
BOT_SECRET = os.environ.get("STONEHEAD_BOT_SECRET", "")
SITE_URL = "https://stoneheadai.com"

# Discord's own limits.
EMBED_DESCRIPTION_MAX = 4096
REPLY_SOFT_MAX = 1400          # keep it readable in a busy channel

# Client-side timeout. The endpoint itself is bounded by Netlify's 10s.
REQUEST_TIMEOUT = 12

GREEN = 0x4A7C4E

# Only respond in channels flagged age-restricted, or in DMs. Servers where
# the owner hasn't age-gated anything get nothing — the app controls its own
# age gate, and in someone else's server this is the only lever there is.
#
# Env-overridable for local and staging runs, where no real server is on the
# other end. The default is ON and ONLY the exact string "0" turns it off, so
# a missing, empty, or fat-fingered value keeps the gate closed — forgetting
# this variable can never open it. Never set it to 0 in a server you don't own.
REQUIRE_AGE_RESTRICTED = os.environ.get("REQUIRE_AGE_RESTRICTED", "1") != "0"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("stonehead")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

session: aiohttp.ClientSession | None = None
commands_synced = False


# ---------------------------------------------------------------- helpers

def channel_allows(interaction: discord.Interaction) -> bool:
    """Age-restricted channels and DMs only."""
    if not REQUIRE_AGE_RESTRICTED:
        return True
    channel = interaction.channel
    if channel is None or isinstance(channel, discord.DMChannel):
        return True
    # is_nsfw() rather than the .nsfw attribute. discord.Thread has no .nsfw
    # at all, so reading the attribute refused every thread — including
    # threads inside a channel the owner HAD age-restricted, which is the
    # case the gate is supposed to allow. The method exists on threads and
    # reads the parent channel's flag, which is the real answer.
    #
    # A channel type without the method (PartialMessageable, anything
    # uncached) falls through to False. Unknown is not permitted: the whole
    # point of this gate is that it holds in servers we don't run.
    check = getattr(channel, "is_nsfw", None)
    return bool(check()) if callable(check) else False


def add_field(embed: discord.Embed, label: str, value) -> None:
    """Add an inline field, or nothing at all when the value is absent.

    Skipping rather than showing a blank is what lets a partial record still
    render as a finished card. Roughly 4% of the database has no effects and
    7% no flavour, and an unrated strain carries a rating of 0 — "Rating: 0/5"
    reads as a terrible strain rather than one nobody scored.
    """
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(item).strip() for item in value if str(item).strip())
    elif value is not None:
        value = str(value).strip()
    if value:
        embed.add_field(name=label, value=value, inline=True)


def strain_title(raw: str) -> str:
    """Turn a dataset key into something that reads as a name.

    The strain database is keyed with hyphens for spaces, so the endpoint
    answers with "Northern-Lights". A title is read, not clicked.
    """
    pretty = raw.replace("-", " ").strip()
    return pretty or raw


def trim(text: str) -> str:
    """Cut to something a channel can absorb, on a sentence boundary."""
    text = text.strip()
    if len(text) <= REPLY_SOFT_MAX:
        return text[:EMBED_DESCRIPTION_MAX]

    cut = text[:REPLY_SOFT_MAX]
    for mark in (". ", "! ", "? ", "\n"):
        idx = cut.rfind(mark)
        if idx > REPLY_SOFT_MAX * 0.6:
            return cut[: idx + 1].strip()
    return cut.rstrip() + "…"


async def ask_stonehead(query: str, user_id: str, guild_id: str | None):
    """POST to the lookup endpoint. Returns (reply, matched, strain, strain_data)."""
    assert session is not None
    payload = {
        "query": query,
        "discord_user_id": str(user_id),
        "guild_id": str(guild_id) if guild_id else None,
    }
    headers = {"X-Bot-Secret": BOT_SECRET, "Content-Type": "application/json"}

    async with session.post(
        API_URL,
        json=payload,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
    ) as res:
        if res.status == 429:
            raise RateLimited()
        if res.status == 401:
            log.error("endpoint rejected the shared secret — check STONEHEAD_BOT_SECRET")
            raise Unavailable()
        if res.status != 200:
            body = (await res.text())[:200]
            log.error("lookup failed status=%s body=%s", res.status, body)
            raise Unavailable()

        data = await res.json()
        reply = (data.get("reply") or "").strip()
        if not reply:
            raise Unavailable()
        # strain and strain_data are both absent on every safety reply and on
        # older deploys of the endpoint, so .get() returning None is a normal
        # state, not a fault.
        return (
            reply,
            bool(data.get("matched")),
            data.get("strain"),
            data.get("strain_data"),
        )


class RateLimited(Exception):
    pass


class Unavailable(Exception):
    pass


# ---------------------------------------------------------------- command

@tree.command(name="strain", description="Ask StoneHead about a strain")
@app_commands.describe(name="Strain name, e.g. blue dream, chem's sister")
async def strain(interaction: discord.Interaction, name: str):
    if not channel_allows(interaction):
        await interaction.response.send_message(
            "This one only works in age-restricted channels. A mod can flag a "
            "channel 18+ in its settings.",
            ephemeral=True,
        )
        return

    name = name.strip()
    if not name:
        await interaction.response.send_message("Give me a strain name.", ephemeral=True)
        return
    if len(name) > 200:
        await interaction.response.send_message("That's a long one, shorten it up.", ephemeral=True)
        return

    # The model call can take several seconds; defer or Discord times out at 3.
    await interaction.response.defer()

    try:
        reply, matched, answered, strain_data = await ask_stonehead(
            name,
            interaction.user.id,
            interaction.guild_id,
        )
    except RateLimited:
        await interaction.followup.send(
            "Easy. Give it a few minutes and ask again.", ephemeral=True
        )
        return
    except (Unavailable, asyncio.TimeoutError, aiohttp.ClientError):
        await interaction.followup.send(
            f"He's not answering right now. Try again in a bit, or come find him at {SITE_URL}",
            ephemeral=True,
        )
        return

    # The title names the strain IN THE CARD, never the one that was typed.
    # The endpoint now answers a miss with a different strain and says so in
    # the reply, so a title echoing the query would label that body with a
    # name it never describes — the one mismatch nobody downstream can catch.
    # Nothing to name (a safety reply, an older endpoint deploy) falls back to
    # the query, which is the only strain either side knows about.
    embed = discord.Embed(
        title=strain_title(answered) if answered else name,
        description=trim(reply),
        color=GREEN,
    )
    # Structured fields only when the endpoint sent a record — a miss, a safety
    # reply, and an older endpoint deploy all arrive as None and leave the
    # embed exactly as it was before.
    if strain_data:
        add_field(embed, "Type", strain_data.get("type"))
        add_field(embed, "Effects", strain_data.get("effects"))
        add_field(embed, "Flavour", strain_data.get("flavor"))
        rating = strain_data.get("rating")
        add_field(embed, "Rating", f"{rating}/5" if rating else None)

    # The bot's own avatar. An embed with no image reads as a wall of text
    # beside one that has a thumbnail, and this costs nothing to carry.
    if client.user is not None:
        embed.set_thumbnail(url=client.user.display_avatar.url)

    embed.set_footer(text=f"StoneHead AI · full conversations at {SITE_URL.replace('https://', '')}")

    await interaction.followup.send(embed=embed)
    log.info(
        "strain lookup guild=%s user=%s query=%r matched=%s answered=%s",
        interaction.guild_id, interaction.user.id, name, matched, answered,
    )


# ---------------------------------------------------------------- lifecycle

@client.event
async def on_ready():
    global session, commands_synced
    if session is None:
        session = aiohttp.ClientSession()
    # Once per PROCESS, not once per connect. on_ready fires again on every
    # gateway resume, and a global command sync is rate limited — on a host
    # that reconnects often, re-syncing here is a steady drip of identical
    # calls against the limit that exists to stop exactly that.
    if not commands_synced:
        await tree.sync()
        commands_synced = True
    log.info("connected as %s — in %d servers", client.user, len(client.guilds))


def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("set DISCORD_TOKEN")
    if not BOT_SECRET:
        raise SystemExit("set STONEHEAD_BOT_SECRET (must match BOT_SHARED_SECRET on the API)")
    client.run(token)


if __name__ == "__main__":
    main()
