"""
StoneHead Discord bot — v1.

One command: /strain <name>. Calls api/strain-lookup on stoneheadai.com and
returns StoneHead's answer as an embed. No memory, no vibe tab, no accounts.

A query naming a family rather than one strain ("thunder fuck og") comes back
as a row of buttons, one per member the database holds. Picking one edits the
list into that strain's card.

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

# The rest of the API (linking, the Moai Drop) lives next to the lookup.
# STONEHEAD_API_URL overrides it; otherwise it's STONEHEAD_API minus
# /strain-lookup, so a preview deploy set there is followed here too.
API_BASE = (
    os.environ.get("STONEHEAD_API_URL")
    or API_URL.rsplit("/strain-lookup", 1)[0]
).rstrip("/")


def _int_or_zero(raw) -> int:
    try:
        return int(str(raw or "").strip())
    except ValueError:
        return 0


# The Weekly Moai Drop host. ONLY this Discord user can run /giveaway, in any
# server: roles, admin and server ownership are ignored. Unset means nobody.
# The site checks the same id again on every giveaway call.
OWNER_DISCORD_ID = _int_or_zero(os.environ.get("OWNER_DISCORD_ID"))

# Who can run /drop-entries (read-only entry list), comma-separated. The
# host is always included. This list says nothing about who can enter or win.
ADMIN_DISCORD_IDS = {
    a for a in (_int_or_zero(x) for x in os.environ.get("ADMIN_DISCORD_IDS", "").split(",")) if a
}

# Servers where /giveaway exists at all. It is registered as a guild command
# in these only, never globally, so no other server ever sees it.
GIVEAWAY_GUILD_IDS = [
    g for g in (_int_or_zero(x) for x in os.environ.get("GIVEAWAY_GUILD_IDS", "").split(",")) if g
]

# Discord's own limits.
EMBED_DESCRIPTION_MAX = 4096
REPLY_SOFT_MAX = 1400          # keep it readable in a busy channel

# Client-side timeout. The endpoint itself is bounded by Netlify's 10s.
REQUEST_TIMEOUT = 12

# The bot's own version, separate from the app's (they ship on their own
# schedules). Bump it with every change that gets deployed, and add a line to
# the Versions list in README.md. MAJOR.MINOR.PATCH: a new command or feature
# is a minor bump, a fix is a patch.
VERSION = "1.2.0"

# Bumped by hand whenever the bot changes. Logged on connect, because
# "restart" on a Pterodactyl panel reboots the process with whatever files are
# already on disk — it does not pull. Without this there is no way to tell a
# deployed fix from an undeployed one except by guessing at behaviour.
BUILD = "2026-10-20-rec"

GREEN = 0x4A7C4E

# The "more like this" button. Deliberately not cannabis-themed: a leaf on a
# strain card reads as decoration and nobody taps decoration. A repeat arrow
# reads as "again", which is the whole offer.
MORE_EMOJI = "\U0001F501"  # 🔁


def same_emoji(a, b) -> bool:
    """Compare two emoji the way a person would, not the way bytes do.

    Discord does not guarantee which form a client sends. The bot adds this
    reaction programmatically, so its own copy is the bare code point; a
    client tapping the SAME reaction can report it with a trailing variation
    selector (U+FE0F), and occasionally with a zero-width joiner in the mix.
    A plain != then rejects the exact button the bot just drew.

    That failure is invisible: it happens before every log line and every
    guard, so the tap produces no reply, no error and no trace. Stripping the
    presentation characters is the whole fix.
    """
    strip = lambda e: "".join(c for c in str(e or "") if c not in "\uFE0F\uFE0E\u200D")
    return strip(a) == strip(b)

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

# Turn on the raw reaction trace. Off by default because a busy channel would
# bury everything else; set LOG_REACTIONS=1 on the host when the button is
# misbehaving and every reaction event that reaches the process is printed,
# whatever it is.
if os.environ.get("LOG_REACTIONS") == "1":
    log.setLevel(logging.DEBUG)
    log.info("LOG_REACTIONS=1 — every raw reaction event will be logged")

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
    """POST to the lookup endpoint. Returns the decoded body, or raises.

    A dict rather than a tuple: the endpoint answers in tiers now, and a
    family picker carries candidates where a card carries a record. Unpacking
    a six-tuple at two call sites is how those get silently swapped.
    """
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
        # Every key below is absent on some real response — a safety reply has
        # no strain, a card has no candidates, an older endpoint deploy has
        # neither — so a missing key is a normal state, not a fault.
        return {
            "reply": reply,
            "matched": bool(data.get("matched")),
            "strain": data.get("strain"),
            "strain_data": data.get("strain_data"),
            "tier": data.get("tier"),
            "candidates": data.get("candidates") or [],
        }


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


# ---------------------------------------------------------------- picker

# How long the buttons stay live. A greyed-out row reads as expired, which is
# a state people understand. The alternative — leaving them live forever —
# means a button whose handler died on a restart, and a dead live button reads
# as broken rather than as over.
PICKER_TIMEOUT = 600  # 10 minutes

# Picker messages with a lookup already running against them.
#
# Disabling the buttons stops almost every repeat tap, but the disable only
# takes effect once Discord renders the edit, and a double tap lands inside
# that window. Without this, one impatient person becomes four endpoint calls
# and four of their ten hourly lookups.
#
# Keyed by message id rather than by user: the picker belongs to a message,
# and that is the thing being edited.
PICKS_IN_FLIGHT: set[int] = set()


class StrainPicker(discord.ui.View):
    """One row of buttons, one per strain in the family the query could mean.

    Native components rather than reactions, and that is the whole point: a
    button labelled "Alaskan" needs no legend, where an emoji row needs
    decoding before anybody can use it.

    UNLIKE A REACTION, A BUTTON TAP IS A COMPONENT INTERACTION AND DOES HAVE
    THE 3-SECOND ACK WINDOW. The lookup behind it takes several seconds, so
    every handler defers first and edits after.
    """

    def __init__(self, candidates, requester_id: int, query: str):
        super().__init__(timeout=PICKER_TIMEOUT)
        self.requester_id = requester_id
        self.query = query
        self.message: discord.Message | None = None
        for cand in candidates[:5]:
            strain = cand.get("strain")
            if not strain:
                continue
            label = (cand.get("label") or strain.replace("-", " "))[:80]
            self.add_item(StrainPickButton(label=label, strain=strain))

    async def on_timeout(self):
        """Grey the row out. The list is over, and it should look over."""
        for item in self.children:
            item.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass  # deleted, or we lost access. Nothing to grey out.


class StrainPickButton(discord.ui.Button):
    def __init__(self, label: str, strain: str):
        # Self-describing custom_id: the handler reads the strain off the
        # button rather than looking it up in state it would have to keep.
        super().__init__(
            label=label,
            style=discord.ButtonStyle.secondary,
            custom_id=f"pick:{strain}"[:100],
        )
        self.strain = strain

    async def callback(self, interaction: discord.Interaction):
        view: StrainPicker = self.view
        message = interaction.message or view.message
        picked_name = strain_title(self.strain)

        # Requester only. In a busy channel the list belongs to whoever asked,
        # and anyone else tapping it would spend THEIR hourly budget answering
        # somebody else's question. Checked before anything else, so a
        # stranger is told rather than silently swallowed by the guard below.
        if interaction.user.id != view.requester_id:
            await interaction.response.send_message(
                "That one's for someone else. Run /strain and I'll sort you out.",
                ephemeral=True,
            )
            return

        # A lookup is already running against this message. Acknowledge so
        # Discord does not show "interaction failed", and do nothing else —
        # the loading line already on screen is the honest answer to "did that
        # register?". No await between the check and the add, so nothing can
        # interleave.
        key = message.id if message is not None else None
        if key is not None and key in PICKS_IN_FLIGHT:
            log.info("ignoring a repeat tap while %s is still loading", self.strain)
            await interaction.response.defer()
            return
        if key is not None:
            PICKS_IN_FLIGHT.add(key)

        # Held for the WHOLE operation, not just the lookup: the guard is about
        # what is on screen, and the message is only settled once it has become
        # the card or gone back to being a picker.
        try:
            await self._pick(interaction, view, message, picked_name)
        finally:
            if key is not None:
                PICKS_IN_FLIGHT.discard(key)

    async def _pick(self, interaction, view, message, picked_name: str):
        try:
            # ACKNOWLEDGE BY UPDATING THE MESSAGE, not by deferring.
            #
            # A silent defer satisfies the 3-second window and shows the person
            # nothing, so a 15-second lookup reads as a dead button — one
            # tester tapped four times. This spends the same acknowledgement on
            # feedback: the row locks, their choice goes green, and the text
            # says what is being pulled up. One action, both jobs.
            for item in view.children:
                item.disabled = True
            self.style = discord.ButtonStyle.success

            await interaction.response.edit_message(
                content=f"pulling up {picked_name}...",
                embed=None,
                view=view,
            )

            answer = await ask_stonehead(
                self.strain, interaction.user.id, interaction.guild_id
            )
        except RateLimited:
            await self._recover(
                interaction, view, message,
                "Easy. Give it a few minutes and ask again.",
            )
            return
        except (Unavailable, asyncio.TimeoutError, aiohttp.ClientError):
            await self._recover(
                interaction, view, message,
                f"Couldn't pull {picked_name} up just now. Try again in a bit, "
                f"or come find him at {SITE_URL}",
            )
            return

        answered = answer["strain"] or self.strain
        embed = card_embed(
            answer["reply"],
            answered,
            answer["strain_data"],
            fallback_title=picked_name,
        )

        # The list existed to ask a question that has now been answered, so it
        # becomes the card. Leaving it above invites a tap on a question
        # nobody is asking any more; dropping the view takes the buttons and
        # the loading line with it.
        view.stop()
        try:
            if message is not None:
                await message.edit(content=None, embed=embed, view=None)
            else:
                message = await interaction.followup.send(embed=embed, wait=True)
        except discord.HTTPException as err:
            log.info("could not edit the picker into a card: %s", err)
            return

        # The card that comes out of a pick is a card like any other, so it
        # carries the more-like-this button too. No special case.
        await offer_more(message, answered)

        log.info(
            "strain pick guild=%s user=%s query=%r picked=%s",
            interaction.guild_id, interaction.user.id, view.query, self.strain,
        )

    async def _recover(self, interaction, view, message, reason: str):
        """Put the buttons back so the pick can be retried.

        The one thing this must never do is leave the message sitting on
        "pulling up ..." forever. A loading line that never resolves is worse
        than the silent defer it replaced: it claims something is happening.
        """
        for item in view.children:
            item.disabled = False
            item.style = discord.ButtonStyle.secondary

        try:
            if message is not None:
                await message.edit(content=f"{reason}\n\nPick one and I'll try again.", view=view)
            else:
                await interaction.followup.send(reason, ephemeral=True)
        except discord.HTTPException as err:
            log.info("could not restore the picker after a failure: %s", err)


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
        answer = await ask_stonehead(name, interaction.user.id, interaction.guild_id)
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

    # A family the query could mean several members of. One line and a row of
    # buttons instead of a card, because picking for them is the thing that
    # went wrong before: "thunder fuck og" used to get "never heard of it"
    # while the file held five of them.
    if answer["tier"] == "family_picker" and answer["candidates"]:
        view = StrainPicker(answer["candidates"], interaction.user.id, name)
        view.message = await interaction.followup.send(
            answer["reply"], view=view, wait=True
        )
        log.info(
            "strain picker guild=%s user=%s query=%r candidates=%d",
            interaction.guild_id, interaction.user.id, name,
            len(answer["candidates"]),
        )
        return

    answered = answer["strain"]
    embed = card_embed(answer["reply"], answered, answer["strain_data"], fallback_title=name)
    message = await interaction.followup.send(embed=embed, wait=True)
    await offer_more(message, answered)
    log.info(
        "strain lookup guild=%s user=%s query=%r tier=%s matched=%s answered=%s",
        interaction.guild_id, interaction.user.id, name,
        answer["tier"], answer["matched"], answered,
    )


# ---------------------------------------------------------------- /rec

# The "another pick" button on a /rec card. Not 🔁: that one already means
# "more like THIS strain" on /strain cards, and this means "another one for
# the same ask".
REC_EMOJI = "\U0001F504"  # 🔄

# What each /rec card was asked, and every strain shown along that chain, so
# 🔄 never repeats one. Cards in the same chain share one dict. In memory like
# CARD_STRAINS: a restart costs a dead 🔄 on old cards, nothing more.
REC_CARDS: "OrderedDict[int, dict]" = OrderedDict()


def remember_rec(message, chain: dict) -> None:
    REC_CARDS[message.id] = chain
    while len(REC_CARDS) > CARD_MEMORY:
        REC_CARDS.popitem(last=False)


async def ask_rec(looking_for: str, user_id, guild_id, exclude=None):
    """POST mode "rec". Returns {reply, tier, strain, strain_data}, or raises.

    The site picks the strain, from the database, by effects and flavors; the
    bot never chooses one. `exclude` is every strain already shown for this ask.
    """
    assert session is not None
    payload = {
        "mode": "rec",
        "query": looking_for,
        "discord_user_id": str(user_id),
        "guild_id": str(guild_id) if guild_id else None,
    }
    if exclude:
        payload["exclude_strains"] = list(exclude)[-50:]
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
            log.error("rec failed status=%s body=%s", res.status, body)
            raise Unavailable()
        data = await res.json()
        reply = (data.get("reply") or "").strip()
        if not reply:
            raise Unavailable()
        return {
            "reply": reply,
            "tier": data.get("tier"),
            "strain": data.get("strain"),
            "strain_data": data.get("strain_data"),
        }


def asked_line(name, looking_for: str) -> str:
    """'**Chaos** asked: something good for a lazy Sunday', safe to post.

    A slash command's options don't show in the channel, so without this the
    card answers a question nobody else can see.
    """
    ask = discord.utils.escape_mentions(discord.utils.escape_markdown(looking_for))
    return f"**{safe_name(name)}** asked: {ask}"


def rec_card(answer: dict, looking_for: str) -> discord.Embed:
    """Exactly a /strain card: the strain's name, StoneHead's take, its fields."""
    return card_embed(answer["reply"], answer.get("strain"), answer.get("strain_data"), fallback_title=looking_for[:256])


@tree.command(name="rec", description="Get a strain pick for what you're in the mood for")
@app_commands.describe(looking_for="e.g. something chill for a movie night, or fruity, not too sleepy")
async def rec(interaction: discord.Interaction, looking_for: str):
    if not channel_allows(interaction):
        await interaction.response.send_message(
            "This one only works in age-restricted channels. A mod can flag a "
            "channel 18+ in its settings.",
            ephemeral=True,
        )
        return

    looking_for = looking_for.strip()
    if not looking_for:
        await interaction.response.send_message("Tell me what you're in the mood for.", ephemeral=True)
        return
    if len(looking_for) > 200:
        await interaction.response.send_message("That's a long one, shorten it up.", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        answer = await ask_rec(looking_for, interaction.user.id, interaction.guild_id)
    except RateLimited:
        await interaction.followup.send("Easy. Give it a few minutes and ask again.", ephemeral=True)
        return
    except (Unavailable, asyncio.TimeoutError, aiohttp.ClientError):
        await interaction.followup.send(
            f"He's not answering right now. Try again in a bit, or come find him at {SITE_URL}",
            ephemeral=True,
        )
        return

    message = await interaction.followup.send(
        content=asked_line(interaction.user.display_name, looking_for),
        embed=rec_card(answer, looking_for),
        allowed_mentions=discord.AllowedMentions.none(),
        wait=True,
    )
    # 🔄 only on a real pick. A "tell me more" or "nothing fits" line has no
    # next pick to offer.
    if answer["tier"] == "rec" and answer.get("strain"):
        try:
            await message.add_reaction(REC_EMOJI)
            remember_rec(message, {"ask": looking_for, "shown": [answer["strain"]]})
        except discord.HTTPException as err:
            log.info("could not add the another-pick reaction: %s", err)
    log.info("rec guild=%s user=%s tier=%s strain=%s", interaction.guild_id, interaction.user.id, answer["tier"], answer.get("strain"))


async def _handle_another_rec(payload: discord.RawReactionActionEvent):
    """🔄 on a /rec card: another strain for the same ask, never one already shown."""
    channel = client.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(payload.channel_id)
        except discord.HTTPException as err:
            log.info("another-pick could not resolve channel=%s: %s", payload.channel_id, err)
            return
    if not channel_ok(channel):
        log.info("another-pick refused by the age gate: channel=%s", payload.channel_id)
        return

    chain = REC_CARDS.get(payload.message_id)
    if chain is None:
        try:
            original = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        if client.user is None or original.author.id != client.user.id:
            return  # someone else's message wearing the same emoji
        await say_under_card(channel, payload.message_id, "Lost track of what that ask was. Run /rec again and I'll pick it back up.")
        return

    try:
        answer = await ask_rec(chain["ask"], payload.user_id, payload.guild_id, exclude=chain["shown"])
    except RateLimited:
        await say_under_card(channel, payload.message_id, "Easy. Give it a few minutes and tap again.")
        return
    except (Unavailable, asyncio.TimeoutError, aiohttp.ClientError):
        await say_under_card(channel, payload.message_id, "Couldn't pull up another one just now. Try again in a bit.")
        return

    if answer["tier"] != "rec" or not answer.get("strain"):
        # All shown, or nothing fits any more: the site's line, under the card.
        await say_under_card(channel, payload.message_id, answer["reply"])
        return

    content = f"Another pick for: {discord.utils.escape_mentions(discord.utils.escape_markdown(chain['ask']))}"
    embed = rec_card(answer, chain["ask"])
    message = None
    try:
        original = await channel.fetch_message(payload.message_id)
        message = await original.reply(content=content, embed=embed, mention_author=False,
                                       allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        try:
            message = await channel.send(content=content, embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as err:
            log.info("could not post another pick: %s", err)
            return

    chain["shown"].append(answer["strain"])
    try:
        await message.add_reaction(REC_EMOJI)
        remember_rec(message, chain)
    except discord.HTTPException as err:
        log.info("could not add the another-pick reaction: %s", err)
    log.info("another pick guild=%s user=%s strain=%s shown=%d", payload.guild_id, payload.user_id, answer["strain"], len(chain["shown"]))


# ---------------------------------------------------------------- reaction

async def say_under_card(channel, message_id: int, text: str) -> None:
    """Answer a reaction in the channel, under the card it was tapped on.

    EVERY dead end in the reaction path comes through here. A tap that does
    nothing is indistinguishable from a bot that has fallen over, and the
    person who taps four times and gives up is the one telling us the silence
    was the bug.
    """
    try:
        original = await channel.fetch_message(message_id)
        await original.reply(text, mention_author=False)
        return
    except discord.HTTPException:
        pass
    try:
        await channel.send(text)
    except discord.HTTPException as err:
        log.info("could not answer the reaction at all: %s", err)


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
    # UNCONDITIONAL, and the first statement in the function on purpose.
    #
    # Every log line so far has sat behind at least one check, so silence has
    # never distinguished "the gateway delivered nothing" from "it delivered
    # something a check rejected". A reaction event reaching this process now
    # always leaves a mark, whatever it is and whoever sent it.
    #
    # DEBUG level, so a busy channel does not flood the console: the panel
    # shows it while LOG_REACTIONS=1 is set and it costs nothing otherwise.
    log.debug(
        "raw reaction add: emoji=%r codepoints=%s user=%s message_id=%s channel=%s",
        str(payload.emoji), [hex(ord(c)) for c in str(payload.emoji)],
        payload.user_id, payload.message_id, payload.channel_id,
    )

    if same_emoji(payload.emoji, REC_EMOJI):
        if client.user is not None and payload.user_id == client.user.id:
            return  # the bot adding its own 🔄
        try:
            await _handle_another_rec(payload)
        except Exception:
            log.exception("another-pick blew up: message_id=%s", payload.message_id)
        return

    if not same_emoji(payload.emoji, MORE_EMOJI):
        # Logged ONLY when it lands on one of our own cards. A busy channel is
        # full of unrelated reactions and logging all of them would bury the
        # signal; a non-matching emoji on a card we posted is the one case
        # where the comparison itself is the suspect.
        if payload.message_id in CARD_STRAINS:
            log.info(
                "reaction on one of our cards but it did not match the button: "
                "got %r (codepoints %s), want %r (codepoints %s), message_id=%s",
                str(payload.emoji),
                [hex(ord(c)) for c in str(payload.emoji)],
                MORE_EMOJI,
                [hex(ord(c)) for c in MORE_EMOJI],
                payload.message_id,
            )
        return
    # The bot adds this reaction itself, and that add comes straight back as
    # an event. Without this the card answers itself the moment it is posted.
    if client.user is not None and payload.user_id == client.user.id:
        return

    try:
        await _handle_more_like_this(payload)
    except Exception:
        # discord.py catches what escapes an event handler and logs it to its
        # own logger, which is a place nobody is looking when the symptom is
        # "the button does nothing". An unexpected exception is the one
        # failure mode every guard below cannot describe, so it gets a
        # traceback and, where possible, a line in the channel.
        log.exception(
            "more-like-this blew up: message_id=%s user=%s", payload.message_id, payload.user_id
        )
        channel = client.get_channel(payload.channel_id)
        if channel is not None and channel_ok(channel):
            await say_under_card(
                channel, payload.message_id, "Something went wrong pulling that one up."
            )


@client.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    """Does nothing but say it happened.

    The bot has no behaviour on un-reacting. This exists so that "do reaction
    events reach this process at all" is answerable without guessing: if a
    REMOVE logs and an ADD never does, the gateway is delivering and something
    upstream of the add handler is eating it. If neither logs, nothing is
    being delivered and the fault is not in this file.
    """
    log.debug(
        "raw reaction remove: emoji=%r user=%s message_id=%s",
        str(payload.emoji), payload.user_id, payload.message_id,
    )


async def _handle_more_like_this(payload: discord.RawReactionActionEvent):

    # EVERY PATH THROUGH THIS HANDLER LEAVES A LINE, starting here.
    #
    # A tap that produced nothing was reported and could not be reproduced,
    # and the reason it could not be narrowed down is that silence from this
    # function was indistinguishable from the gateway never delivering the
    # event at all. With this line, the absence of a log is itself the
    # diagnosis: no line means the event never arrived (intents, gateway,
    # a restart), and a line followed by one of the guards below names
    # exactly which guard stopped it.
    log.info(
        "more-like-this tapped: message_id=%s user=%s channel=%s guild=%s known_card=%s",
        payload.message_id, payload.user_id, payload.channel_id, payload.guild_id,
        payload.message_id in CARD_STRAINS,
    )
    # WHAT THIS RESOLVES IS THE STRAIN ON THE CARD, never the query that
    # produced it. They differ on three tiers out of five: a corrected card
    # shows Zkittlez for a typed "skittlez", a no-match card shows a strain
    # nobody named, and a picker card is the PICKER MESSAGE EDITED IN PLACE,
    # so the id this arrives on was registered against the picked strain by
    # offer_more when the button turned the list into a card.
    source = CARD_STRAINS.get(payload.message_id)

    channel = client.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(payload.channel_id)
        except discord.HTTPException as err:
            log.info(
                "more-like-this could not resolve channel=%s: %s", payload.channel_id, err
            )
            return
    if not channel_ok(channel):
        # The card was posted in an age-restricted channel and the flag has
        # since come off. The gate has to hold now, not only at post time,
        # and this stays SILENT on purpose: the whole point of the gate is
        # that the bot does not talk in a channel that has not been flagged.
        #
        # Logged, though. A gate that refuses a channel the bot posted a card
        # in five minutes ago looks identical, from the outside, to the bot
        # being broken — so the log is the only thing that can tell them
        # apart. channel_ok falls through to False for any channel type
        # without is_nsfw(), which is deliberate and is also the most likely
        # way this refuses something it should not.
        log.info(
            "more-like-this refused by the age gate: channel=%s type=%s",
            payload.channel_id, type(channel).__name__,
        )
        return

    if not source:
        # Either one of our cards we have lost track of (CARD_STRAINS is in
        # memory, so a restart empties it) or somebody else's message wearing
        # the same emoji. Those need opposite treatment: the first is a
        # failure worth saying out loud, the second is none of our business.
        #
        # One fetch tells them apart, and it only happens on this path.
        try:
            original = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        if client.user is None or original.author.id != client.user.id:
            return  # not our card; a tap on someone else's message is theirs
        log.info(
            "more-like-this could not resolve a strain: message_id=%s guild=%s user=%s",
            payload.message_id, payload.guild_id, payload.user_id,
        )
        await say_under_card(
            channel,
            payload.message_id,
            "Lost track of which strain that card was. Run /strain again and "
            "I'll pick it back up.",
        )
        return

    try:
        result = await ask_similar(source, payload.user_id, payload.guild_id)
    except RateLimited:
        # This used to stay silent, on the reasoning that a "slow down" line in
        # a channel where somebody is already tapping is more noise than the
        # tapping. That was wrong: silence and a dead bot look identical from
        # the outside, and the person cannot tell which they are looking at.
        log.info("more-like-this rate limited guild=%s user=%s", payload.guild_id, payload.user_id)
        await say_under_card(
            channel, payload.message_id, "Easy. Give it a few minutes and tap again."
        )
        return
    except (Unavailable, asyncio.TimeoutError, aiohttp.ClientError):
        log.info(
            "more-like-this unavailable guild=%s source=%r message_id=%s",
            payload.guild_id, source, payload.message_id,
        )
        await say_under_card(
            channel,
            payload.message_id,
            f"Couldn't pull up anything like {strain_title(source)} just now. Try again in a bit.",
        )
        return

    if result is None:
        # Nothing close enough to recommend. 190 of the 2,351 records have no
        # profile to score against — no description, or no effects or flavour
        # list — so this is a reachable state and not a fault.
        # Hawaiian-Thunder-Fuck is one of them AND it sits in a picker row,
        # which is exactly how somebody finds this by accident.
        log.info("no similar strains for %r (message_id=%s)", source, payload.message_id)
        await say_under_card(
            channel,
            payload.message_id,
            f"Nothing in the book close enough to {strain_title(source)} to be worth "
            f"pointing you at.",
        )
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


# ---------------------------------------------------------------- the Weekly Moai Drop
#
# /link connects a Discord account to a StoneHead account (the site does the
# linking; the bot only hands out the link). The weekly round is a post with a
# 🗿 Enter button: tapping it is the entry, and every day that person used
# StoneHead that week adds one more, up to 7. The site keeps the entries,
# counts them and picks the winner. The bot posts, relays and animates.
#
# Every reply about someone's entries is EPHEMERAL. How many entries a person
# has says how often they use the app, so nobody's count is ever posted.
#
# A button tap, not a reaction: only an interaction can answer privately, and
# a DM fails for anyone with server DMs off.

ENTER_EMOJI = "\U0001F5FF"  # 🗿
ROUND_BUTTON_PREFIXES = ("enter:", "leave:")
NOT_HOST = "This one's just for the host."
NUDGE_EVERY = 600  # seconds between asks; the site decides if a nudge is due


async def call_api(path: str, payload: dict):
    """POST to a StoneHead endpoint. Returns (status, body); raises Unavailable."""
    assert session is not None
    headers = {"X-Bot-Secret": BOT_SECRET, "Content-Type": "application/json"}
    try:
        async with session.post(
            f"{API_BASE}/{path}",
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as res:
            if res.status == 401:
                log.error("%s rejected the shared secret, check STONEHEAD_BOT_SECRET", path)
                raise Unavailable()
            try:
                data = await res.json()
            except (aiohttp.ContentTypeError, ValueError):
                log.error("%s answered with something that isn't JSON (status %s)", path, res.status)
                raise Unavailable()
            return res.status, data if isinstance(data, dict) else {}
    except (asyncio.TimeoutError, aiohttp.ClientError) as err:
        log.info("%s unreachable: %s", path, err)
        raise Unavailable()


def is_host(user) -> bool:
    """The owner check. By user id only, never roles or permissions."""
    return bool(OWNER_DISCORD_ID) and getattr(user, "id", None) == OWNER_DISCORD_ID


def is_admin(user) -> bool:
    """/drop-entries viewers: ADMIN_DISCORD_IDS plus the host. By user id only."""
    return is_host(user) or getattr(user, "id", None) in ADMIN_DISCORD_IDS


def link_view(url: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Link my StoneHead account", url=url))
    return view


def enter_view(round_id: int, *, closed: bool = False) -> discord.ui.View:
    """The 🗿 Enter button on a round post. Handled in on_interaction by its
    custom_id, so it keeps working across restarts."""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        style=discord.ButtonStyle.secondary if closed else discord.ButtonStyle.success,
        label="Entries closed" if closed else "Enter",
        emoji=ENTER_EMOJI,
        custom_id=f"enter:{round_id}",
        disabled=closed,
    ))
    return view


def leave_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        style=discord.ButtonStyle.secondary, label="Leave this week", custom_id=f"leave:{round_id}",
    ))
    return view


def entry_reply_view(data: dict):
    """The button under a private entry reply: a link while unlinked, Leave once in."""
    if data.get("link_url"):
        return link_view(data["link_url"])
    if data.get("status") == "entered" and data.get("round_id"):
        return leave_view(int(data["round_id"]))
    return None


def safe_name(name) -> str:
    """A Discord display name, safe to print in a channel."""
    text = discord.utils.escape_mentions(discord.utils.escape_markdown(str(name or "someone")))
    return text[:40] or "someone"


def spin_frames(names, *, frames: int = 8):
    """(text, delay before showing it) for the spin, slowing toward the end.

    Never faster than one edit per 0.8s, which keeps a run of edits well under
    Discord's limit. The winner is picked before the first frame; this is
    only the show, and the last frame is replaced by the result.
    """
    pool = [safe_name(n) for n in names if n] or ["..."]
    out = []
    for i in range(frames):
        delay = round(0.8 + 0.005 * i * i, 3)  # 0.8s up to ~1.05s: about 7s in all
        out.append((f"{ENTER_EMOJI} Spinning... **{pool[i % len(pool)]}**", delay))
    return out


def result_text(data: dict) -> str:
    people = data.get("total_people") or 0
    entries = data.get("total_entries") or 0
    return (
        f"{ENTER_EMOJI} **<@{int(data['winner_discord_id'])}>** wins this week's Moai Drop! "
        f"{data.get('prize') or ''}. {people} {'person' if people == 1 else 'people'}, "
        f"{entries} {'entry' if entries == 1 else 'entries'}."
    )


async def close_round_post(channel_id, message_id, round_id) -> None:
    """Grey out a round's Enter button after a draw or a cancel."""
    if not channel_id or not message_id:
        return
    try:
        channel = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
        message = await channel.fetch_message(int(message_id))
        await message.edit(view=enter_view(int(round_id), closed=True))
    except (discord.HTTPException, ValueError) as err:
        log.info("could not close the round post %s: %s", message_id, err)


@tree.command(name="link", description="Connect your Discord to your StoneHead account")
async def link(interaction: discord.Interaction):
    # Private, and fine anywhere the bot can see: it says nothing about strains.
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        status, data = await call_api("bot-link-start", {
            "discord_id": str(interaction.user.id),
            "discord_username": interaction.user.display_name,
        })
    except Unavailable:
        status, data = 0, {}
    if status == 429:
        await interaction.followup.send(
            "You just made a few links. Use the newest one, or try again in 10 minutes.", ephemeral=True
        )
        return
    if status != 200 or not data.get("url"):
        await interaction.followup.send("Couldn't make a link right now. Try again in a minute.", ephemeral=True)
        return
    await interaction.followup.send(
        "Tap to link your StoneHead account. Link works for 15 minutes.",
        view=link_view(data["url"]),
        ephemeral=True,
    )
    log.info("link made guild=%s", interaction.guild_id)


@tree.command(name="entries", description="Check your entries in this week's Moai Drop")
async def entries(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        status, data = await call_api("bot-round-enter", {
            "action": "status",
            "guild_id": str(interaction.guild_id) if interaction.guild_id else None,
            "discord_id": str(interaction.user.id),
            "discord_username": interaction.user.display_name,
        })
    except Unavailable:
        status, data = 0, {}
    if status != 200 or not data.get("message"):
        await interaction.followup.send("Couldn't check right now. Try again in a minute.", ephemeral=True)
        return
    view = entry_reply_view(data)
    if view is None:
        await interaction.followup.send(data["message"], ephemeral=True)
    else:
        await interaction.followup.send(data["message"], view=view, ephemeral=True)


async def handle_round_button(interaction: discord.Interaction, custom_id: str) -> None:
    """🗿 Enter on a round post, or Leave this week under a private reply."""
    action, _, raw_id = custom_id.partition(":")
    if not raw_id.isdigit():
        return
    if not channel_allows(interaction):
        await interaction.response.send_message(
            "This one only works in age-restricted channels.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        status, data = await call_api("bot-round-enter", {
            "action": action,
            "round_id": int(raw_id),
            "discord_id": str(interaction.user.id),
            "discord_username": interaction.user.display_name,
        })
    except Unavailable:
        status, data = 0, {}
    if status != 200 or not data.get("message"):
        await interaction.followup.send("Couldn't reach StoneHead. Tap again in a minute.", ephemeral=True)
        return
    view = entry_reply_view(data)
    if view is None:
        await interaction.followup.send(data["message"], ephemeral=True)
    else:
        await interaction.followup.send(data["message"], view=view, ephemeral=True)
    log.info("round %s %s -> %s", raw_id, action, data.get("status"))


@client.event
async def on_interaction(interaction: discord.Interaction):
    """Round buttons, by custom_id. Slash commands and the strain picker are
    handled by the command tree and their views; this only claims its own."""
    if interaction.type is not discord.InteractionType.component:
        return
    custom_id = (interaction.data or {}).get("custom_id", "")
    if not custom_id.startswith(ROUND_BUTTON_PREFIXES):
        return
    try:
        await handle_round_button(interaction, custom_id)
    except Exception:
        log.exception("round button blew up: %s", custom_id)


class Giveaway(app_commands.Group):
    """/giveaway start | draw | cancel. Registered only in GIVEAWAY_GUILD_IDS,
    and only the host can run it."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_host(interaction.user):
            return True
        await interaction.response.send_message(NOT_HOST, ephemeral=True)
        log.info("non-host tried /giveaway user=%s guild=%s", interaction.user.id, interaction.guild_id)
        return False

    @app_commands.command(name="start", description="Post this week's Moai Drop")
    @app_commands.describe(
        prize="What the winner gets, as shown in the post",
        bonus_days="Free days for everyone who enters (0 for none)",
        pass_days="Pass days the winner gets (0 if the prize isn't a pass)",
    )
    async def start(
        self,
        interaction: discord.Interaction,
        prize: str,
        bonus_days: app_commands.Range[int, 0, 30] = 0,
        pass_days: app_commands.Range[int, 0, 365] = 30,
    ):
        if interaction.guild_id is None or not channel_allows(interaction):
            await interaction.response.send_message(
                "Start it in an age-restricted channel of the server.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            status, data = await call_api("bot-round-create", {
                "invoker_discord_id": str(interaction.user.id),
                "guild_id": str(interaction.guild_id),
                "channel_id": str(interaction.channel_id),
                "prize": prize,
                "bonus_days": bonus_days,
                "pass_days": pass_days,
            })
        except Unavailable:
            status, data = 0, {}
        if status != 200 or not data.get("round_id"):
            await interaction.followup.send(data.get("message") or "Couldn't start it. Try again in a minute.", ephemeral=True)
            return
        round_id = int(data["round_id"])
        try:
            post = await interaction.channel.send(data["post_text"], view=enter_view(round_id))
        except discord.HTTPException as err:
            log.error("could not post round %s: %s", round_id, err)
            await interaction.followup.send(
                "The round exists but I couldn't post in this channel. Check my permissions, "
                "then /giveaway cancel and start again.",
                ephemeral=True,
            )
            return
        try:
            await call_api("bot-round-message", {
                "invoker_discord_id": str(interaction.user.id),
                "round_id": round_id,
                "message_id": str(post.id),
            })
        except Unavailable:
            log.error("round %s posted but its message id wasn't saved", round_id)
        await interaction.followup.send(
            f"Round {round_id} is up. Run /giveaway draw here on Sunday.", ephemeral=True
        )
        log.info("round %s started guild=%s bonus=%s", round_id, interaction.guild_id, bonus_days)

    @app_commands.command(name="draw", description="Draw this week's Moai Drop winner, live")
    async def draw(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            status, data = await call_api("bot-round-draw", {
                "invoker_discord_id": str(interaction.user.id),
                "guild_id": str(interaction.guild_id),
            })
        except Unavailable:
            status, data = 0, {}
        if status != 200 or data.get("status") not in ("ok", "no_entries"):
            await interaction.followup.send(data.get("message") or "Couldn't draw. Try again in a minute.", ephemeral=True)
            return

        await close_round_post(data.get("channel_id"), data.get("message_id"), data.get("round_id"))
        channel = interaction.channel
        if data.get("channel_id"):
            channel = client.get_channel(int(data["channel_id"])) or channel

        if data["status"] == "no_entries":
            await channel.send(f"{ENTER_EMOJI} No counted entries this week. Watch for next week's post.")
            await interaction.followup.send("Drawn: nobody counted.", ephemeral=True)
            return

        await spin(channel, data)
        note = "Drawn."
        if data.get("winner_is_founder"):
            note += " Heads up: the winner is a founder, so no pass was granted. Give them a substitute prize."
        await interaction.followup.send(note, ephemeral=True)

    @app_commands.command(name="cancel", description="Call off this week's Moai Drop")
    async def cancel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            status, data = await call_api("bot-round-cancel", {
                "invoker_discord_id": str(interaction.user.id),
                "guild_id": str(interaction.guild_id),
            })
        except Unavailable:
            status, data = 0, {}
        if status != 200 or data.get("status") != "ok":
            await interaction.followup.send(data.get("message") or "Couldn't cancel. Try again in a minute.", ephemeral=True)
            return
        await close_round_post(data.get("channel_id"), data.get("message_id"), data.get("round_id"))
        await interaction.followup.send("Round cancelled. Nobody was granted anything.", ephemeral=True)


async def spin(channel, data: dict) -> None:
    """The show: one message, edited through the names, landing on the winner."""
    winner = int(data["winner_discord_id"])
    try:
        message = await channel.send(f"{ENTER_EMOJI} Spinning...")
        for text, delay in spin_frames(data.get("spin_names") or []):
            await asyncio.sleep(delay)
            await message.edit(content=text)
        await asyncio.sleep(0.9)
        await message.edit(
            content=result_text(data),
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=winner)]),
        )
    except discord.HTTPException as err:
        # The winner is already decided and granted; just say it plainly.
        log.info("spin failed, posting the result instead: %s", err)
        await channel.send(
            result_text(data),
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=winner)]),
        )


async def nudge_once() -> None:
    """Ask the site whether a round is due its 6 PM 'finish linking' line."""
    try:
        status, data = await call_api("bot-round-nudge", {})
    except Unavailable:
        return
    if status != 200:
        return
    for item in data.get("nudges") or []:
        try:
            channel = client.get_channel(int(item["channel_id"])) or await client.fetch_channel(int(item["channel_id"]))
            if channel_ok(channel):
                await channel.send(item["message"], allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, KeyError, ValueError) as err:
            log.info("nudge for round %s failed: %s", item.get("round_id"), err)


nudge_task: "asyncio.Task | None" = None


async def nudge_loop() -> None:
    while True:
        await nudge_once()
        await asyncio.sleep(NUDGE_EVERY)


def start_nudge_loop() -> None:
    """Once per process, and only where giveaways can run."""
    global nudge_task
    if GIVEAWAY_GUILD_IDS and (nudge_task is None or nudge_task.done()):
        nudge_task = asyncio.get_running_loop().create_task(nudge_loop())



# /drop-entries: who's in this server's current drop and with how many
# entries. READ ONLY. The site counts through the same function the draw
# picks from, so this list is the draw pool. Hidden from members by default
# (Manage Server), and the user id is checked here and again by the site.
DISCORD_MESSAGE_MAX = 2000


def entries_report(data: dict):
    """(text, csv_or_None). The CSV only when the list won't fit in one message."""
    round_ = data.get("round") or {}
    entrants = data.get("entrants") or []
    head = [
        f"{ENTER_EMOJI} **Moai Drop entries** ({round_.get('status', '?')})",
        f"Drop: {data.get('dates_label', '?')} · draw {data.get('draw_label', '?')}",
        f"{data.get('total_people', 0)} entrants · {data.get('total_entries', 0)} entries",
    ]
    extra = []
    if data.get("not_counted"):
        extra.append(f"{data['not_counted']} tapped Enter but aren't counted (not linked yet, or a staff account)")
    if data.get("left"):
        extra.append(f"{data['left']} left this week")
    if extra:
        head.append(" · ".join(extra))

    def name(e):
        return safe_name(e.get("discord_name") or "unknown")

    rows = [
        f"{i}. {name(e)} ({safe_name(e.get('username') or 'no username')}): "
        f"link {e.get('link_entry', 1)} + {e.get('days', 0)} days = **{e.get('total', 0)}**"
        for i, e in enumerate(entrants, 1)
    ]
    text = "\n".join(head + ([""] + rows if rows else ["", "Nobody counted yet."]))
    if len(text) <= DISCORD_MESSAGE_MAX:
        return text, None

    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["discord_name", "stonehead_username", "link_entry", "days_used", "total"])
    for e in entrants:
        writer.writerow([e.get("discord_name") or "", e.get("username") or "",
                         e.get("link_entry", 1), e.get("days", 0), e.get("total", 0)])
    summary = "\n".join(head + ["", "Full list attached as a .csv."])
    return summary, buf.getvalue()


@app_commands.command(name="drop-entries", description="See everyone in this week's Moai Drop (admins)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def drop_entries(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message(NOT_HOST, ephemeral=True)
        log.info("non-admin tried /drop-entries user=%s guild=%s", interaction.user.id, interaction.guild_id)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        status, data = await call_api("bot-round-entries", {
            "invoker_discord_id": str(interaction.user.id),
            "guild_id": str(interaction.guild_id),
        })
    except Unavailable:
        status, data = 0, {}
    if status != 200 or data.get("status") not in ("ok", "no_round"):
        await interaction.followup.send(data.get("message") or "Couldn't load entries. Try again in a minute.", ephemeral=True)
        return
    if data["status"] == "no_round":
        await interaction.followup.send(data.get("message") or "No Moai Drop in this server yet.", ephemeral=True)
        return
    text, csv_text = entries_report(data)
    if csv_text is None:
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    else:
        import io
        file = discord.File(io.BytesIO(csv_text.encode("utf-8")), filename="moai-drop-entries.csv")
        await interaction.followup.send(text, file=file, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

if GIVEAWAY_GUILD_IDS:
    tree.add_command(
        drop_entries,
        guilds=[discord.Object(id=g) for g in GIVEAWAY_GUILD_IDS],
    )
    tree.add_command(
        Giveaway(name="giveaway", description="Run the Weekly Moai Drop (host only)"),
        guilds=[discord.Object(id=g) for g in GIVEAWAY_GUILD_IDS],
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
        # /giveaway lives only in the allowed servers. A server the bot isn't
        # in (yet) refuses the sync; that's logged, not fatal.
        for guild_id in GIVEAWAY_GUILD_IDS:
            try:
                await tree.sync(guild=discord.Object(id=guild_id))
            except discord.HTTPException as err:
                log.info("could not sync /giveaway to guild %s: %s", guild_id, err)
        commands_synced = True
    start_nudge_loop()
    log.info(
        "connected as %s — in %d servers — v%s (build %s) — intents(reactions=%s, guilds=%s)",
        client.user, len(client.guilds), VERSION, BUILD,
        intents.reactions, intents.guilds,
    )


def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("set DISCORD_TOKEN")
    if not BOT_SECRET:
        raise SystemExit("set STONEHEAD_BOT_SECRET (must match BOT_SHARED_SECRET on the API)")
    client.run(token)


if __name__ == "__main__":
    main()
