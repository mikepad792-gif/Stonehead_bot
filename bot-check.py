"""
StoneHead bot harness — run: python3 bot-check.py

Drives the bot's own decisions against fakes. No Discord connection, no HTTP,
no token: every boundary the bot talks to is replaced, so each assertion below
is about the bot's logic and nothing else.

The load-bearing ones are the guards. A card that names the wrong strain, a
button anyone can press, and a reaction the bot answers itself are all silent
failures — they look like a working bot right up until somebody screenshots it.

Deliberately dependency-free (no pytest) so it runs anywhere the bot does.
"""

import asyncio
import json
import os
import sys

os.environ.setdefault("STONEHEAD_BOT_SECRET", "test-secret")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord  # noqa: E402
import stonehead_bot as B  # noqa: E402

FAILURES = []


def check(name, condition, extra=""):
    print(("  PASS  " if condition else "  FAIL  ") + name + ("" if condition else f"\n          {extra}"))
    if not condition:
        FAILURES.append(name)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── Fakes ───────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status, payload):
        self.status, self._payload = status, payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)


class FakeSession:
    """Records every request so the assertions can read what was asked."""

    def __init__(self):
        self.requests = []
        self.status = 200
        self.payload = {}

    def post(self, url, json=None, headers=None, timeout=None):
        self.requests.append(json)
        return FakeResponse(self.status, self.payload)


class FakeMessage:
    _next_id = 9000

    def __init__(self):
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id
        self.reactions = []
        self.edits = []
        self.replies = []
        self.embed = None
        self.content = None
        self.view = "unset"
        # Who Discord says posted it. The reaction handler fetches this to
        # tell one of our own cards from somebody else's message.
        self.author = type("A", (), {"id": 555})()

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)

    async def reply(self, content=None, embed=None, mention_author=None):
        child = FakeMessage()
        self.replies.append({"content": content, "embed": embed, "message": child})
        return child

    async def edit(self, content="unset", embed=None, view="unset"):
        self.edits.append({"content": content, "embed": embed, "view": view})
        if content != "unset":
            self.content = content
        if embed is not None:
            self.embed = embed
        if view != "unset":
            self.view = view


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, view=None, ephemeral=False, wait=False, **kw):
        message = FakeMessage()
        message.content = content
        message.embed = embed
        self.sent.append({"content": content, "embed": embed, "view": view, "ephemeral": ephemeral})
        return message if wait else None


class FakeInteractionResponse:
    def __init__(self, message=None):
        self.messages = []
        self.deferred = False
        self.edits = []
        self._message = message

    async def send_message(self, content=None, ephemeral=False, **kw):
        self.messages.append({"content": content, "ephemeral": ephemeral})

    async def defer(self):
        self.deferred = True

    async def edit_message(self, content="unset", embed="unset", view="unset", **kw):
        """The update-message response: acknowledges AND edits in one action."""
        self.edits.append({"content": content, "embed": embed, "view": view})
        if self._message is not None:
            if content != "unset":
                self._message.content = content
            if view != "unset":
                self._message.view = view


class FakeChannel:
    def __init__(self, nsfw=True):
        self._nsfw = nsfw
        self.messages = {}
        self.sent = []

    def is_nsfw(self):
        return self._nsfw

    async def fetch_message(self, message_id):
        if message_id not in self.messages:
            raise discord.HTTPException(type("R", (), {"status": 404})(), "not found")
        return self.messages[message_id]

    async def send(self, content=None, embed=None):
        message = FakeMessage()
        self.sent.append({"content": content, "embed": embed})
        return message


class FakeInteraction:
    def __init__(self, user_id=42, message=None):
        self.channel = FakeChannel()
        self.guild_id = 7
        self.user = type("U", (), {"id": user_id})()
        self.message = message
        self.response = FakeInteractionResponse(message)
        self.followup = FakeFollowup()


class FakeAvatar:
    url = "https://cdn.example/avatar.png"


class FakeClient:
    user = type("U", (), {"id": 555, "display_avatar": FakeAvatar()})()

    def __init__(self, channel=None):
        self._channel = channel

    def get_channel(self, _id):
        return self._channel


class FakePayload:
    """A raw reaction event."""

    def __init__(self, message_id, user_id=42, emoji=None, channel_id=5, guild_id=7):
        self.message_id = message_id
        self.user_id = user_id
        self.emoji = emoji if emoji is not None else B.MORE_EMOJI
        self.channel_id = channel_id
        self.guild_id = guild_id


CARD = {
    "reply": "Alaskan Thunder Fuck, yeah.",
    "matched": True,
    "strain": "Alaskan-Thunder-Fuck",
    "tier": "exact",
    "strain_data": {"type": "sativa", "rating": 4.2, "effects": ["Euphoric"], "flavor": ["Pine"]},
}

PICKER = {
    "reply": "thunder fuck og isn't one I know, but I've got a few in that family.",
    "matched": False,
    "strain": None,
    "tier": "family_picker",
    "strain_data": None,
    "candidates": [
        {"strain": "Alaskan-Thunder-Fuck", "label": "Alaskan"},
        {"strain": "Matanuska-Thunder-Fuck", "label": "Matanuska"},
        {"strain": "Cherry-Thunder-Fuck", "label": "Cherry"},
    ],
}

session = FakeSession()
B.session = session
B.client = FakeClient()
command = B.strain.callback


# ── P01: a family query becomes a row of buttons ────────────────────
print("\nP01  the picker")
session.status, session.payload = 200, PICKER
interaction = FakeInteraction()
run(command(interaction, "thunder fuck og"))

sent = interaction.followup.sent[0]
view = sent["view"]
check("P01a a family_picker posts a view, not an embed", sent["embed"] is None and view is not None)
check("P01b the line names what was typed", "thunder fuck og" in (sent["content"] or ""), sent["content"])
check("P01c one button per candidate", len(view.children) == 3, len(view.children))
check(
    "P01d labels are the distinguishing part only",
    [c.label for c in view.children] == ["Alaskan", "Matanuska", "Cherry"],
    [c.label for c in view.children],
)
check(
    "P01e custom_id carries the strain, so the handler needs no state",
    [c.custom_id for c in view.children]
    == ["pick:Alaskan-Thunder-Fuck", "pick:Matanuska-Thunder-Fuck", "pick:Cherry-Thunder-Fuck"],
    [c.custom_id for c in view.children],
)
check("P01f the timeout is set, so the row can expire", view.timeout == B.PICKER_TIMEOUT, view.timeout)

# ── P02: a stranger tapping changes nothing ─────────────────────────
print("\nP02  requester only")
picker_message = FakeMessage()
view.message = picker_message
button = view.children[0]

before = len(session.requests)
stranger = FakeInteraction(user_id=99, message=picker_message)
run(button.callback(stranger))

check("P02a a stranger gets an answer", len(stranger.response.messages) == 1)
check("P02b ...and it is ephemeral", stranger.response.messages[0]["ephemeral"] is True)
check("P02c the message is NOT edited", picker_message.edits == [], picker_message.edits)
check("P02d no lookup is spent on their behalf", len(session.requests) == before)
check("P02e the buttons stay live for the requester", not view.is_finished())

# ── P03: the requester tapping edits the list into the card ─────────
print("\nP03  the pick")
session.payload = CARD
requester = FakeInteraction(user_id=42, message=picker_message)
run(button.callback(requester))

ack = requester.response.edits[0] if requester.response.edits else {}
check("P03a the tap is acknowledged by UPDATING the message, not a silent defer",
      len(requester.response.edits) == 1 and requester.response.deferred is False)
check("P03a2 the ack names what is being pulled up",
      "Alaskan Thunder Fuck" in (ack.get("content") or ""), ack.get("content"))
check("P03a3 the whole row locks on the first tap",
      all(c.disabled for c in view.children))
check("P03a4 the tapped button is marked so the choice is visibly registered",
      button.style == discord.ButtonStyle.success, button.style)
check("P03a5 the other buttons are not marked",
      all(c.style != discord.ButtonStyle.success for c in view.children if c is not button))
check("P03b the pick asks for the strain on the button", session.requests[-1]["query"] == "Alaskan-Thunder-Fuck", session.requests[-1])
check("P03c it is an ordinary lookup, not a special mode", "mode" not in session.requests[-1])
check("P03d the picker message is edited, not replaced", len(picker_message.edits) == 1 and requester.followup.sent == [])
edit = picker_message.edits[0] if picker_message.edits else {}
check("P03e the card replaces the line", edit.get("content") is None and edit.get("embed") is not None)
check("P03f the buttons are removed with it", edit.get("view") is None)
check("P03g the card is titled with the picked strain", getattr(edit.get("embed"), "title", None) == "Alaskan Thunder Fuck", getattr(edit.get("embed"), "title", None))
check("P03h the resulting card gets the more-like-this button", picker_message.reactions == [B.MORE_EMOJI], picker_message.reactions)
check("P03i ...and is remembered under the strain it shows", B.CARD_STRAINS.get(picker_message.id) == "Alaskan-Thunder-Fuck")
check("P03j the view is finished, so a second tap does nothing", view.is_finished())

# ── P3B: a repeat tap while loading spends nothing ──────────────────
#
# THE REPORT: nothing visible happened for 15-20 seconds and a tester tapped
# the same button four times. Disabling the row stops most of that, but the
# disable only lands once Discord renders the edit, and a fast double tap
# arrives inside that window.
print("\nP3B  repeat taps")

slow_picker = PICKER["candidates"]
view2 = B.StrainPicker(slow_picker, requester_id=42, query="thunder fuck og")
msg2 = FakeMessage()
view2.message = msg2
btn2 = view2.children[0]

gate = asyncio.Event()
real_ask = B.ask_stonehead

async def slow_ask(strain, user_id, guild_id):
    session.requests.append({"query": strain})
    await gate.wait()
    return dict(CARD)

B.ask_stonehead = slow_ask

async def double_tap():
    first = FakeInteraction(user_id=42, message=msg2)
    task = asyncio.ensure_future(btn2.callback(first))
    await asyncio.sleep(0)   # let the first tap reach the in-flight guard
    second = FakeInteraction(user_id=42, message=msg2)

    # Bounded. A second tap that reaches the endpoint blocks on the same gate
    # the first one is holding, which without the guard is a DEADLOCK rather
    # than a failure — and a test that hangs teaches nobody anything.
    blocked = False
    try:
        await asyncio.wait_for(btn2.callback(second), timeout=1.0)
    except asyncio.TimeoutError:
        blocked = True

    gate.set()
    await task
    return first, second, blocked

before = len(session.requests)
first, second, blocked = run(double_tap())
B.ask_stonehead = real_ask

check("P3B0 the second tap returns immediately instead of entering the lookup", not blocked)

check("P3Ba the first tap calls the endpoint once",
      len(session.requests) - before == 1, len(session.requests) - before)
check("P3Bb the second tap makes NO endpoint call",
      len(session.requests) - before == 1)
check("P3Bc the second tap is acknowledged so Discord shows no failure",
      second.response.deferred is True)
check("P3Bd ...and changes nothing on screen",
      second.response.edits == [] and second.response.messages == [])
check("P3Be the guard is released once the pick finishes",
      msg2.id not in B.PICKS_IN_FLIGHT, B.PICKS_IN_FLIGHT)

# ── P3C: a failure never leaves it stuck on the loading line ────────
print("\nP3C  failure recovery")

view3 = B.StrainPicker(PICKER["candidates"], requester_id=42, query="thunder fuck og")
msg3 = FakeMessage()
view3.message = msg3
btn3 = view3.children[0]

session.status, session.payload = 500, {"error": "boom"}
failed = FakeInteraction(user_id=42, message=msg3)
run(btn3.callback(failed))

last_edit = msg3.edits[-1] if msg3.edits else {}
check("P3Ca the message is edited off the loading line", len(msg3.edits) >= 1)
check("P3Cb it says it could not pull that one up",
      "Couldn" in (last_edit.get("content") or ""), last_edit.get("content"))
check("P3Cc every button is live again so they can retry",
      all(not c.disabled for c in view3.children))
check("P3Cd the green mark is cleared",
      all(c.style != discord.ButtonStyle.success for c in view3.children))
check("P3Ce the guard is released after a failure",
      msg3.id not in B.PICKS_IN_FLIGHT, B.PICKS_IN_FLIGHT)
check("P3Cf the view stays live so the timeout can still expire it",
      not view3.is_finished())
session.status = 200

# ── P04: the row greys out rather than dying live ───────────────────
print("\nP04  timeout")
timed = B.StrainPicker(PICKER["candidates"], requester_id=42, query="thunder fuck og")
timed.message = FakeMessage()
run(timed.on_timeout())
check("P04a every button is disabled", all(c.disabled for c in timed.children))
check("P04b the message is edited so it looks expired", len(timed.message.edits) == 1)

# ── P05: a card query still behaves exactly as before ───────────────
print("\nP05  cards are unchanged")
session.payload = CARD
interaction = FakeInteraction()
run(command(interaction, "alaskan thunder fuck"))
sent = interaction.followup.sent[0]
check("P05a a card tier posts an embed, no view", sent["embed"] is not None and sent["view"] is None)
check("P05b titled with the strain, not the query", sent["embed"].title == "Alaskan Thunder Fuck", sent["embed"].title)

# ── P06: the gate still holds ───────────────────────────────────────
print("\nP06  age gate")
before = len(session.requests)
blocked = FakeInteraction()
blocked.channel = FakeChannel(nsfw=False)
run(command(blocked, "thunder fuck og"))
check("P06a a non age-restricted channel is refused", len(blocked.response.messages) == 1)
check("P06b ...before any request is made", len(session.requests) == before)

# ── P07: the reaction resolves the strain ON THE CARD ───────────────
#
# THE REPORT: 🔁 did nothing on a card produced by the button picker. That
# card is the picker MESSAGE EDITED IN PLACE, so the id the reaction arrives
# on is the picker's — and if anything resolved the strain from the query that
# produced the picker ("thunder fuck og"), it would ask the endpoint for a
# family name that is not a key in the table and get nothing back.
#
# Every tier is checked, because the same question applies to each: what the
# reaction must resolve is the strain the person is LOOKING AT, never the one
# they typed.
print("\nP07  the reaction resolves what is on the card")

react_channel = FakeChannel()
B.client = FakeClient(react_channel)

SIMILAR = {
    "reply": "Try Romulan.",
    "matched": True,
    "strain": "Romulan",
    "source_strain": "x",
    "tier": "similar",
    "strain_data": {"type": "indica", "rating": 4, "effects": ["Relaxed"], "flavor": ["Earthy"]},
}


def card_from_lookup(payload, typed):
    """Run /strain and hand back the message the card landed on."""
    session.payload = payload
    interaction = FakeInteraction()
    run(command(interaction, typed))
    sent = interaction.followup.sent[-1]
    # The command's fake followup makes a message; mirror it into the channel
    # so the reaction handler can fetch it.
    message = FakeMessage()
    message.embed = sent["embed"]
    react_channel.messages[message.id] = message
    run(B.offer_more(message, payload["strain"]))
    return message


def tap_more(message):
    session.payload = SIMILAR
    before = len(session.requests)
    run(B.on_raw_reaction_add(FakePayload(message.id)))
    return session.requests[-1] if len(session.requests) > before else None


# --- picker-produced card: the picked strain, not the family query ---
session.payload = PICKER
picker_interaction = FakeInteraction()
run(command(picker_interaction, "thunder fuck og"))
pview = picker_interaction.followup.sent[0]["view"]
pmsg = FakeMessage()
react_channel.messages[pmsg.id] = pmsg
pview.message = pmsg

session.payload = {
    "reply": "Alaskan Thunder Fuck, yeah.", "matched": True,
    "strain": "Alaskan-Thunder-Fuck", "tier": "exact",
    "strain_data": {"type": "sativa", "rating": 4.2, "effects": ["Euphoric"], "flavor": ["Pine"]},
}
run(pview.children[0].callback(FakeInteraction(user_id=42, message=pmsg)))

sent = tap_more(pmsg)
check("P07a 🔁 on a picker card asks about the PICKED strain",
      sent and sent.get("source_strain") == "Alaskan-Thunder-Fuck", sent)
check("P07b ...not the family query that produced the picker",
      not sent or sent.get("source_strain") != "thunder fuck og")

# --- tier B corrected: the corrected strain, not the typed spelling ---
corrected = card_from_lookup({
    "reply": "Not under that spelling. Zkittlez though.", "matched": False,
    "strain": "Zkittlez", "tier": "candidate",
    "strain_data": {"type": "hybrid", "rating": 4.3, "effects": ["Happy"], "flavor": ["Sweet"]},
}, "skittlez")
sent = tap_more(corrected)
check("P07c 🔁 on a corrected card asks about the corrected strain",
      sent and sent.get("source_strain") == "Zkittlez", sent)
check("P07d ...not the typed spelling", not sent or sent.get("source_strain") != "skittlez")

# --- tier C random: the strain shown, not the unknown query ---
unrelated = card_from_lookup({
    "reply": "Never heard of fhqwhgads. Here's Gg 5 though, different strain.",
    "matched": False, "strain": "Gg-5", "tier": "unrelated",
    "strain_data": {"type": "hybrid", "rating": 4, "effects": ["Relaxed"], "flavor": ["Pine"]},
}, "fhqwhgads")
sent = tap_more(unrelated)
check("P07e 🔁 on a no-match card asks about the strain SHOWN",
      sent and sent.get("source_strain") == "Gg-5", sent)
check("P07f ...not the query nobody could resolve",
      not sent or sent.get("source_strain") != "fhqwhgads")

# ── P08: a reaction never dies quietly ──────────────────────────────
#
# A tap that does nothing is indistinguishable from a bot that has fallen
# over. 190 of the 2,351 records have no profile to score against, so "no
# candidates" is a reachable state and not a fault — Hawaiian-Thunder-Fuck is
# one of them and it sits in a picker row.
print("\nP08  no silent dead ends")

# --- nothing to recommend (the endpoint 404s) ---
target = card_from_lookup({
    "reply": "Hawaiian Thunder Fuck.", "matched": True,
    "strain": "Hawaiian-Thunder-Fuck", "tier": "exact",
    "strain_data": {"type": "sativa", "rating": 4, "effects": ["Happy"], "flavor": ["Pine"]},
}, "hawaiian thunder fuck")
session.status, session.payload = 404, {"error": "No similar strains"}
run(B.on_raw_reaction_add(FakePayload(target.id)))
check("P08a a 404 posts a visible reply instead of nothing",
      len(target.replies) == 1, target.replies)
check("P08b ...naming the strain it could not match",
      "Hawaiian Thunder Fuck" in (target.replies[0]["content"] or "") if target.replies else False,
      target.replies[0]["content"] if target.replies else None)
session.status = 200

# --- rate limited ---
target2 = card_from_lookup({
    "reply": "Blue Dream.", "matched": True, "strain": "Blue-Dream", "tier": "exact",
    "strain_data": {"type": "hybrid", "rating": 4.3, "effects": ["Happy"], "flavor": ["Berry"]},
}, "blue dream")
session.status = 429
run(B.on_raw_reaction_add(FakePayload(target2.id)))
check("P08c a rate limit says so instead of going quiet", len(target2.replies) == 1, target2.replies)
session.status = 200

# --- endpoint down ---
target3 = card_from_lookup({
    "reply": "Blue Dream.", "matched": True, "strain": "Blue-Dream", "tier": "exact",
    "strain_data": {"type": "hybrid", "rating": 4.3, "effects": ["Happy"], "flavor": ["Berry"]},
}, "blue dream")
session.status = 500
run(B.on_raw_reaction_add(FakePayload(target3.id)))
check("P08d an outage says so instead of going quiet", len(target3.replies) == 1, target3.replies)
session.status = 200

# --- our card, strain forgotten (a restart empties CARD_STRAINS) ---
orphan = FakeMessage()
react_channel.messages[orphan.id] = orphan
B.CARD_STRAINS.pop(orphan.id, None)
before = len(session.requests)
run(B.on_raw_reaction_add(FakePayload(orphan.id)))
check("P08e an unresolvable card of OURS gets a visible reply",
      len(orphan.replies) == 1, orphan.replies)
check("P08f ...and spends no lookup doing it", len(session.requests) == before)

# --- somebody else's message wearing the same emoji: stay out of it ---
theirs = FakeMessage()
theirs.author = type("A", (), {"id": 999})()
react_channel.messages[theirs.id] = theirs
before_sent = len(react_channel.sent)
run(B.on_raw_reaction_add(FakePayload(theirs.id)))
check("P08g a 🔁 on someone else's message is left alone",
      theirs.replies == [] and len(react_channel.sent) == before_sent,
      theirs.replies)

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)}")
    for name in FAILURES:
        print("  - " + name)
    sys.exit(1)
print("All bot checks passed.")
