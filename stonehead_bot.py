"""
StoneHead Discord bot — v1.

One command: /strain <name>. Calls api/strain-lookup on stoneheadai.com and
returns StoneHead's answer as an embed. No memory, no vibe tab, no accounts.

Every card carries a 🔁 reaction: tapping it asks for a different strain with
a close profile. That strain is chosen SERVER SIDE from a precomputed table of
profile neighbours, not by the model, because a model asked for "something
similar" names strains that were never in the database.

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
from collections import OrderedDict

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

# The "more like this" button. Deliberately not cannabis-themed: a leaf on a
# strain card reads as decoration and nobody taps decoration. A repeat arrow
# reads as "again", which is the whole offer.
MORE_EMOJI = "\U0001F501"  # 🔁

# Which strain each card the bot posted was about, so a tap can ask for
# something like it. In memory ON PURPOSE: it is a UI affordance, not a
# record, and a restart losing it costs a dead button on old cards rather
# than anything anyone can notice. Bounded so a long-running process cannot
# grow it without limit.
CARD_STRAINS: "OrderedDict[int, str]" = OrderedDict()
CARD_MEMORY = 500

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

# Reactions are in the default set; message content is not, and is not wanted.
# The bot reads slash commands and reaction events, neither of which needs a
# privileged intent.
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

session: aiohttp.ClientSession | None = None
commands_synced = False


# ---------------------------------------------------------------- helpers

def channel_allows(interaction: discord.Interaction) -> bool:
    """Age-restricted channels and DMs only."""
    return channel_ok(interaction.channel)


def channel_ok(channel) -> bool:
    """The gate itself, on a bare channel.

    Split out from channel_allows because the reaction handler has a channel
    and no interaction, and a card posted in a channel that has since been
    un-flagged must stop answering — the gate has to hold at the moment of the
    reply, not only at the moment of the command.
    """
    if not REQUIRE_AGE_RESTRICTED:
        return True
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


def card_embed(reply: str, answered, strain_data, *, fallback_title: str, footer: str | None = None) -> discord.Embed:
    """One card renderer for both a lookup and a recommendation.

    The title names the strain IN THE CARD, never the one that was typed. The
    endpoint answers a miss with a different strain and says so in the reply,
    so a title echoing the query would label that body with a name it never
    describes — the one mismatch nobody downstream can catch. Nothing to name
    (a safety reply, an older endpoint deploy) falls back to what was typed,
    the only strain either side knows about.
    """
    embed = discord.Embed(
        title=strain_title(answered) if answered else fallback_title,
        description=trim(reply),
        color=GREEN,
    )
    # Structured fields only when the endpoint sent a record — a safety reply
    # and an older endpoint deploy both arrive as None and leave the embed
    # exactly as it was before.
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

    embed.set_footer(
        text=footer or f"StoneHead AI · full conversations at {SITE_URL.replace('https://', '')}"
    )
    return embed


async def offer_more(message: discord.Message | None, strain) -> None:
    """Put the "more like this" button on a card, and remember what it was about.

    Only on a card that actually named a strain: a safety reply has nothing to
    be similar to, and offering to continue from one would be the worst
    possible moment to change the subject.

    A failure here is swallowed. Missing Add Reactions permission in somebody
    else's server is an ordinary state, not an error worth a traceback, and
    the card itself has already landed.
    """
    if message is None or not strain:
        return
    try:
        await message.add_reaction(MORE_EMOJI)
    except discord.HTTPException as err:
        log.info("could not add the more-like-this reaction: %s", err)
        return
    CARD_STRAINS[message.id] = strain
    while len(CARD_STRAINS) > CARD_MEMORY:
        CARD_STRAINS.popitem(last=False)


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


async def ask_similar(source_strain: str, user_id: str, guild_id: str | None):
    """Ask for a strain like `source_strain`. Returns (reply, strain, strain_data).

    Same endpoint, same secret, same rate limiter — one mode flag apart. The
    STRAIN IS CHOSEN SERVER SIDE, from a precomputed table, and that is the
    whole point of the feature: a model asked to think of a similar strain
    invents one that was never in the database.

    Returns None when there is nothing to recommend (404). Roughly 190 of the
    2,351 records have no profile to score against, and saying nothing is a
    better answer there than apologising for a button somebody tapped.
    """
    assert session is not None
    payload = {
        "mode": "similar",
        "source_strain": source_strain,
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
        if res.status == 404:
            return None
        if res.status == 429:
            raise RateLimited()
        if res.status == 401:
            log.error("endpoint rejected the shared secret — check STONEHEAD_BOT_SECRET")
            raise Unavailable()
        if res.status != 200:
            body = (await res.text())[:200]
            log.error("similar failed status=%s body=%s", res.status, body)
            raise Unavailable()

        data = await res.json()
        reply = (data.get("reply") or "").strip()
        if not reply:
            raise Unavailable()
        return reply, data.get("strain"), data.get("strain_data")


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

    embed = card_embed(reply, answered, strain_data, fallback_title=name)
    message = await interaction.followup.send(embed=embed, wait=True)
    await offer_more(message, answered)
    log.info(
        "strain lookup guild=%s user=%s query=%r matched=%s answered=%s",
        interaction.guild_id, interaction.user.id, name, matched, answered,
    )


# ---------------------------------------------------------------- reaction

@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """"More like this" — tap the card, get a different strain with a close profile.

    NO 3-SECOND DEADLINE HERE. A reaction arrives as a gateway event, not an
    interaction, so there is no ack window and no defer dance: the bot posts
    when it is ready.

    RAW, not on_reaction_add. The cached-message version only fires for
    messages the process has in memory, which in a busy channel is a coin
    flip. The card's own strain comes from CARD_STRAINS rather than from the
    message, so an uncached message costs nothing.
    """
    if str(payload.emoji) != MORE_EMOJI:
        return
    # The bot adds this reaction itself, and that add comes straight back as
    # an event. Without this the card answers itself the moment it is posted.
    if client.user is not None and payload.user_id == client.user.id:
        return
    source = CARD_STRAINS.get(payload.message_id)
    if not source:
        # Not one of our cards, or one from before the last restart. In-memory
        # by design — see CARD_STRAINS.
        return

    channel = client.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(payload.channel_id)
        except discord.HTTPException:
            return
    if not channel_ok(channel):
        # The card was posted in an age-restricted channel and the flag has
        # since come off. The gate has to hold now, not only at post time.
        return

    try:
        result = await ask_similar(source, payload.user_id, payload.guild_id)
    except RateLimited:
        # Silent on purpose. There is no ephemeral reply to a reaction, so the
        # only way to say "slow down" is a public message in a channel where
        # somebody is already tapping repeatedly — which is more noise than
        # the thing it is complaining about. The tap simply does nothing.
        log.info("more-like-this rate limited guild=%s user=%s", payload.guild_id, payload.user_id)
        return
    except (Unavailable, asyncio.TimeoutError, aiohttp.ClientError):
        log.info("more-like-this unavailable guild=%s source=%r", payload.guild_id, source)
        return

    if result is None:
        # Nothing close enough in the table to be worth recommending. Saying
        # nothing beats apologising for a button.
        log.info("no similar strains for %r", source)
        return

    reply, rec, strain_data = result
    embed = card_embed(
        reply,
        rec,
        strain_data,
        fallback_title=strain_title(source),
        footer=f"Like {strain_title(source)} · StoneHead AI",
    )

    # Posted as a reply to the card, so the pair reads as one exchange rather
    # than as two loose cards in the scrollback.
    message = None
    try:
        original = await channel.fetch_message(payload.message_id)
        message = await original.reply(embed=embed, mention_author=False)
    except discord.HTTPException:
        try:
            message = await channel.send(embed=embed)
        except discord.HTTPException as err:
            log.info("could not post the recommendation: %s", err)
            return

    # The recommendation is a card like any other, so it carries the button
    # too. Chaining is the natural way to use this, and every hop still costs
    # a human tap and a slot in that person's hourly limit.
    await offer_more(message, rec)

    log.info(
        "more like this guild=%s user=%s source=%s rec=%s",
        payload.guild_id, payload.user_id, source, rec,
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
