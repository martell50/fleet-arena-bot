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
MOVEMENTS_CHANNEL_ID  = int(os.environ.get("MOVEMENTS_CHANNEL_ID", "0"))   # Primary channel (server 1)
MOVEMENTS_CHANNEL_ID_2 = int(os.environ.get("MOVEMENTS_CHANNEL_ID_2", "0")) # Optional second channel (server 2)
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


async def broadcast_message(text: str):
    """
    Sends a message to both configured channels, across both servers.
    Silently skips any channel ID that's not set (0) or not found.
    """
    for channel_id in (MOVEMENTS_CHANNEL_ID, MOVEMENTS_CHANNEL_ID_2):
        if channel_id == 0:
            continue
        channel = client.get_channel(channel_id)
        if channel is None:
            print(f"[WARN] Cannot find channel ID {channel_id} — bot may not be invited to that server.")
            continue
        try:
            await channel.send(text)
        except Exception as e:
            print(f"[ERROR] Failed to send to channel {channel_id}: {e}")


# ─────────────────────────────────────────────
#  POLLING LOOP
# ─────────────────────────────────────────────
@tasks.loop(seconds=POLL_SECONDS)
async def poll_movements():
    if not state["ready"]:
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
            await broadcast_message(
                f"📊 **Fleet Arena Movement Tracker is online!**\n"
                f"Watching the top **{TRACK_TOP_N}** ranks across {len(new_table)} tracked players.\n"
                f"You'll see a message for every individual rank change."
            )
            return

        # ── Compare old vs new table, pair winners with losers ───────────────
        # CONFIRMED RULE: fleet arena swaps are an isolated 1-to-1 exchange.
        # The winner takes the loser's exact old rank, and the loser takes
        # the winner's exact old rank. Nothing else in the leaderboard shifts.
        # So a valid pair must satisfy BOTH:
        #     winner.new_rank == loser.old_rank
        #     loser.new_rank  == winner.old_rank
        movers = []  # (code, name, old_rank, new_rank, is_winner)

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

            is_winner = new_rank < old_rank
            movers.append((code, new_entry["name"], old_rank, new_rank, is_winner))

        winners = [m for m in movers if m[4]]
        losers  = [m for m in movers if not m[4]]
        winners.sort(key=lambda w: w[3])  # best new rank first, for readable order

        matched_codes = set()
        unpaired_winners = []
        unpaired_losers = []

        for w_code, w_name, w_old_rank, w_new_rank, _ in winners:
            # Find the exact swap partner: someone who went FROM the winner's
            # new rank TO the winner's old rank. Strict match, no fallback.
            partner = next(
                (l for l in losers
                 if l[0] not in matched_codes
                 and l[2] == w_new_rank      # loser's old_rank == winner's new_rank
                 and l[3] == w_old_rank),    # loser's new_rank == winner's old_rank
                None
            )

            if partner:
                l_code, l_name, l_old_rank, l_new_rank, _ = partner
                matched_codes.add(w_code)
                matched_codes.add(l_code)

                await broadcast_message(
                    f"⚔️ **{w_name}** (#{w_old_rank} → **#{w_new_rank}**) defeated "
                    f"**{l_name}**, who dropped to **#{l_new_rank}**"
                )
            else:
                unpaired_winners.append((w_name, w_old_rank, w_new_rank))

        # Anyone who dropped but wasn't claimed as a swap partner
        for l_code, l_name, l_old_rank, l_new_rank, _ in losers:
            if l_code in matched_codes:
                continue
            unpaired_losers.append((l_name, l_old_rank, l_new_rank))

        # Report unpaired winners (their opponent isn't in our tracked set,
        # or the swap partner's data didn't line up exactly)
        for name, old_rank, new_rank in unpaired_winners:
            await broadcast_message(
                f"📈 **{name}** moved up: **#{old_rank}** → **#{new_rank}** "
                f"_(opponent not in tracked top {len(SHARD_ALLY_CODES)})_"
            )

        # Report unpaired losers (their attacker isn't in our tracked set,
        # or the swap partner's data didn't line up exactly)
        for name, old_rank, new_rank in unpaired_losers:
            await broadcast_message(
                f"📉 **{name}** moved down: **#{old_rank}** → **#{new_rank}** "
                f"_(attacker not in tracked top {len(SHARD_ALLY_CODES)})_"
            )

        total_events = len(winners) + len(losers)
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

    if MOVEMENTS_CHANNEL_ID == 0 and MOVEMENTS_CHANNEL_ID_2 == 0:
        print("[ERROR] No channel configured — set MOVEMENTS_CHANNEL_ID and/or MOVEMENTS_CHANNEL_ID_2.")
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
