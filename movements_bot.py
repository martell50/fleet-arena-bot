"""
SWGOH Fleet Arena Movement Tracker Bot
----------------------------------------
A standalone companion bot to the Attack Notifier. Polls the same shard
roster of ally codes every 30 seconds, and reports EVERY individual rank
change within the top 30 to a dedicated Discord channel.

Run this as a second, separate process alongside bot.py.
"""

import os
import asyncio
import aiohttp
import discord
from discord.ext import tasks

# ─────────────────────────────────────────────
#  CONFIGURATION  (set these as env variables)
# ─────────────────────────────────────────────
COMLINK_URL        = os.environ.get("COMLINK_URL",        "https://comlink.andeh.uk")
ALLY_CODE          = os.environ.get("ALLY_CODE",          "")   # Your own ally code (optional here, for context)
DISCORD_TOKEN      = os.environ.get("DISCORD_TOKEN",      "")   # Can be the SAME bot token as bot.py, or a different one
MOVEMENTS_CHANNEL_ID = int(os.environ.get("MOVEMENTS_CHANNEL_ID", "0"))  # Separate channel for movement alerts
POLL_SECONDS       = int(os.environ.get("POLL_SECONDS",   "30"))
TRACK_TOP_N        = int(os.environ.get("TRACK_TOP_N",    "30"))  # Only report movement within this rank range

# ─────────────────────────────────────────────
#  SHARD ROSTER — top 50 fleet arena ally codes
# ─────────────────────────────────────────────
SHARD_ALLY_CODES = [
    "318676317",  # Ghost
    "483636945",  # Baby Inyak
    "254768133",  # Welderdude23
    "182529616",  # ψβφ DDCat4
    "739593798",  # Goat
    "638551434",  # Shadowz
    "543791793",  # Strider59
    "698483912",  # Party of Fives
    "599241583",  # Cho Manno
    "295167858",  # Judo
    "671495778",  # DartGvin
    "123149486",  # Aesri
    "614528131",  # Kreb Hue
    "252177641",  # TheOddTimer
    "697647618",  # MaxRebo
    "419129292",  # Vlad Makanen
    "848315853",  # ZoroXion
    "479589263",  # Wanderer
    "346368277",  # Dewitt
    "178774198",  # RacistPumpkino
    "388516264",  # Darklord42069
    "346884961",  # Vaakuum
    "449138636",  # Мора мора
    "654196899",  # MaTheory
    "839626799",  # CornGut2
    "381298912",  # BarsD3
    "693653551",  # Iroh
    "897954862",  # Ravclaque
    "778481551",  # BigLongCransky
    "769259174",  # Miles
    "653422625",  # Lyfizin Shambles
    "217552661",  # Karp24
    "181623514",  # Loreck Avery
    "897942467",  # Falnewt
    "524729348",  # Messr Keller
    "398551717",  # Trevorious
    "574256119",  # Anakinn
    "627337336",  # deletraz
    "961555632",  # markbigs702
    "687923429",  # PunIntended
    "521888243",  # Mol Eliza
    "581817634",  # Erk
    "899269912",  # Caidos
    "848976633",  # Dаrth Vаder
    "379394556",  # Damir
    "939243836",  # HighOnQuack
    "731528132",  # cam playz2932
    "195795533",  # ISHIMURA
    "864586854",  # DarthAledom
    "592775641",  # LangDo44
]

if ALLY_CODE and ALLY_CODE not in SHARD_ALLY_CODES:
    SHARD_ALLY_CODES.append(ALLY_CODE)

# ─────────────────────────────────────────────
#  FLEET ARENA ATTACK RANGE TABLE
#  Maps an attacker's rank -> the best (lowest number) rank they can reach.
#  Used to validate winner/loser pairings when matching battles.
# ─────────────────────────────────────────────
def max_reachable_rank(attacker_rank: int) -> int:
    """Returns the best rank a player at attacker_rank could attack into."""
    if attacker_rank >= 28:
        return 21
    if attacker_rank == 21:
        return 15
    if attacker_rank == 20:
        return 14
    if attacker_rank == 19:
        return 13
    if attacker_rank == 18:
        return 13
    if attacker_rank == 17:
        return 12
    if attacker_rank == 16:
        return 11
    if attacker_rank == 15:
        return 10
    if attacker_rank == 14:
        return 9
    if attacker_rank == 13:
        return 8
    if attacker_rank == 12:
        return 8
    if attacker_rank == 11:
        return 7
    if attacker_rank == 10:
        return 6
    if attacker_rank == 9:
        return 5
    if attacker_rank == 8:
        return 4
    if attacker_rank == 7:
        return 3
    if attacker_rank == 6:
        return 2
    if attacker_rank <= 5:
        return 1
    # Ranks 22-27 weren't explicitly given; conservative linear estimate.
    return max(1, attacker_rank - 7)


def could_have_reached(attacker_old_rank: int, target_rank: int) -> bool:
    """True if a player starting at attacker_old_rank could attack target_rank."""
    return max_reachable_rank(attacker_old_rank) <= target_rank <= attacker_old_rank

# ─────────────────────────────────────────────
#  DISCORD BOT SETUP
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

state = {
    "last_table": {},    # {allyCode: {"name": str, "rank": int}} from previous poll
    "ready":      False,
}


# ─────────────────────────────────────────────
#  COMLINK HELPERS
# ─────────────────────────────────────────────
async def fetch_player_fleet_rank(session: aiohttp.ClientSession, ally_code: str) -> dict | None:
    """Calls /player and extracts name + fleet arena rank."""
    url = f"{COMLINK_URL}/player"
    payload = {
        "payload": {"allyCode": str(ally_code)},
        "enums": False,
    }
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                print(f"[WARN] /player ({ally_code}) returned HTTP {resp.status}")
                return None
            data = await resp.json()
            name = data.get("name", "Unknown Player")
            for profile in data.get("pvpProfile", []):
                if profile.get("tab") == 2:  # fleet arena
                    rank = profile.get("rank")
                    if rank is not None:
                        return {"name": name, "rank": rank}
            return None
    except Exception as e:
        print(f"[ERROR] fetch_player_fleet_rank({ally_code}): {e}")
        return None


async def fetch_shard_table(session: aiohttp.ClientSession) -> dict:
    """Fetches fleet rank for every ally code concurrently."""
    tasks_list = [fetch_player_fleet_rank(session, code) for code in SHARD_ALLY_CODES]
    results = await asyncio.gather(*tasks_list)
    table = {}
    for code, result in zip(SHARD_ALLY_CODES, results):
        if result is not None:
            table[code] = result
    return table


# ─────────────────────────────────────────────
#  POLLING LOOP
# ─────────────────────────────────────────────
@tasks.loop(seconds=POLL_SECONDS)
async def poll_movements():
    if not state["ready"]:
        return

    channel = client.get_channel(MOVEMENTS_CHANNEL_ID)
    if channel is None:
        print(f"[ERROR] Cannot find Discord channel ID {MOVEMENTS_CHANNEL_ID}.")
        return

    async with aiohttp.ClientSession() as session:

        new_table = await fetch_shard_table(session)
        if not new_table:
            print("[WARN] Shard table fetch returned nothing — skipping this poll.")
            return

        last_table = state["last_table"]

        # ── First poll: just establish baseline ──────────────────────────────
        if not last_table:
            state["last_table"] = new_table
            tracked_in_range = sum(1 for e in new_table.values() if e["rank"] <= TRACK_TOP_N)
            print(f"[INFO] Movement tracking started. {tracked_in_range} players currently in top {TRACK_TOP_N}.")
            await channel.send(
                f"📊 **Fleet Arena Movement Tracker is online!**\n"
                f"Watching the top **{TRACK_TOP_N}** ranks across {len(new_table)} tracked players.\n"
                f"You'll see a message for every individual rank change."
            )
            return

        # ── Compare old vs new table, pair winners with losers ───────────────
        # A "winner" is anyone who climbed to a better (lower-numbered) rank.
        # The "loser" of that exact battle is whoever now sits at the winner's
        # OLD rank (since fleet arena swaps are 1-to-1: winner takes loser's
        # spot, loser is bumped down to roughly the winner's old spot).
        winners = []  # (code, name, old_rank, new_rank)
        losers_by_old_rank = {}  # old_rank (winner's target) -> (code, name, old_rank, new_rank)

        for code, new_entry in new_table.items():
            old_entry = last_table.get(code)
            if old_entry is None:
                continue  # no previous data point, skip

            old_rank = old_entry["rank"]
            new_rank = new_entry["rank"]

            if old_rank == new_rank:
                continue  # no movement

            touches_range = old_rank <= TRACK_TOP_N or new_rank <= TRACK_TOP_N
            if not touches_range:
                continue

            if new_rank < old_rank:
                # Climbed up — this player WON a battle
                winners.append((code, new_entry["name"], old_rank, new_rank))
            else:
                # Dropped down — this player LOST a battle
                # Index losers by the rank they used to hold, since that's
                # the rank a winner would have targeted.
                losers_by_old_rank[old_rank] = (code, new_entry["name"], old_rank, new_rank)

        # Sort winners by their new (best) rank for readable ordering
        winners.sort(key=lambda w: w[3])

        reported_loser_codes = set()
        unpaired_winners = []

        for w_code, w_name, w_old_rank, w_new_rank in winners:
            # The loser of this battle should now be wherever the winner used
            # to be (winner's old rank == loser's new resting spot in most
            # fleet arena swap implementations) OR simply whoever previously
            # held the winner's new rank.
            loser = losers_by_old_rank.get(w_new_rank)

            if loser:
                l_code, l_name, l_old_rank, l_new_rank = loser
                # Validate against the attack range rules
                plausible = could_have_reached(w_old_rank, w_new_rank)
                range_note = "" if plausible else " _(unusual range — verify)_"

                await channel.send(
                    f"⚔️ **{w_name}** (#{w_old_rank} → **#{w_new_rank}**) defeated "
                    f"**{l_name}**, who dropped to **#{l_new_rank}**{range_note}"
                )
                reported_loser_codes.add(l_code)
            else:
                # No matching loser found in our tracked set — they may be
                # outside the top 50, or moved in a way we can't pair cleanly.
                unpaired_winners.append((w_name, w_old_rank, w_new_rank))

        # Report any winners we couldn't pair with a specific loser
        for name, old_rank, new_rank in unpaired_winners:
            await channel.send(
                f"📈 **{name}** moved up: **#{old_rank}** → **#{new_rank}** "
                f"_(opponent not in tracked top {len(SHARD_ALLY_CODES)})_"
            )

        # Report any losers who weren't matched to a winner we found
        # (e.g. their attacker is outside the tracked ally code list)
        for old_rank, (l_code, l_name, l_old_rank, l_new_rank) in losers_by_old_rank.items():
            if l_code in reported_loser_codes:
                continue
            await channel.send(
                f"📉 **{l_name}** moved down: **#{l_old_rank}** → **#{l_new_rank}** "
                f"_(attacker not in tracked top {len(SHARD_ALLY_CODES)})_"
            )

        total_events = len(winners) + len([1 for r in losers_by_old_rank if losers_by_old_rank[r][0] not in reported_loser_codes])
        if total_events:
            print(f"[INFO] Reported movement for this poll: {len(winners)} winner(s) processed.")
        else:
            print("[INFO] No rank changes in tracked range this poll.")

        state["last_table"] = new_table


@poll_movements.before_loop
async def before_poll():
    await client.wait_until_ready()


# ─────────────────────────────────────────────
#  BOT EVENTS
# ─────────────────────────────────────────────
@client.event
async def on_ready():
    print(f"[INFO] Logged in as {client.user}")

    if MOVEMENTS_CHANNEL_ID == 0:
        print("[ERROR] Missing MOVEMENTS_CHANNEL_ID environment variable.")
        await client.close()
        return

    state["ready"] = True
    poll_movements.start()
    print(f"[INFO] Polling shard every {POLL_SECONDS}s via {COMLINK_URL}")
    print(f"[INFO] Tracking {len(SHARD_ALLY_CODES)} ally codes, reporting top {TRACK_TOP_N} movement.")


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN environment variable is not set.")
    client.run(DISCORD_TOKEN)
