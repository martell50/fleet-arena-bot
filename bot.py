"""
SWGOH Fleet Arena Attack Notifier Bot — v5
-------------------------------------------
Since /playerArena does not return opponent/leaderboard data on this
Comlink instance, this version maintains its own local rank table by
polling /player for a known list of ally codes (the top 50 of your
fleet shard) every 30 seconds, alongside your own /playerArena rank.

When your rank drops, the attacker is identified directly from this
local table — whoever is now sitting at your old rank.
"""

import os
import asyncio
import aiohttp
import discord
from discord.ext import tasks

# ─────────────────────────────────────────────
#  CONFIGURATION  (set these as env variables)
# ─────────────────────────────────────────────
COMLINK_URL   = os.environ.get("COMLINK_URL",   "https://comlink.andeh.uk")
ALLY_CODE     = os.environ.get("ALLY_CODE",     "")        # Your own ally code
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
CHANNEL_ID    = int(os.environ.get("CHANNEL_ID", "0"))
POLL_SECONDS  = int(os.environ.get("POLL_SECONDS", "30"))

# ─────────────────────────────────────────────
#  SHARD ROSTER — top 50 fleet arena ally codes
#  (rank values here are just the initial seed; the bot tracks
#   live rank changes itself after the first poll)
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

# Make sure your own ally code is always included in the tracked set
if ALLY_CODE and ALLY_CODE not in SHARD_ALLY_CODES:
    SHARD_ALLY_CODES.append(ALLY_CODE)

# ─────────────────────────────────────────────
#  FLEET ARENA ATTACK RANGE TABLE
#  Maps an attacker's rank -> the best (lowest number) rank they can reach.
#  Used to sanity-check that a detected "attacker" was actually capable
#  of reaching your old rank, filtering out coincidental rank changes.
# ─────────────────────────────────────────────
def max_reachable_rank(attacker_rank: int) -> int:
    """
    Returns the best (numerically lowest) rank a player at attacker_rank
    could realistically have attacked into, based on observed shard rules.
    """
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
    # Anything between 22-27 isn't explicitly defined; fall back to a
    # conservative linear estimate consistent with the surrounding values.
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
    "my_rank":        None,   # Your last known fleet rank
    "last_table":      {},    # {allyCode: {"name": str, "rank": int}} from previous poll
    "ready":           False,
}


# ─────────────────────────────────────────────
#  COMLINK HELPERS
# ─────────────────────────────────────────────
async def fetch_player_fleet_rank(session: aiohttp.ClientSession, ally_code: str) -> dict | None:
    """
    Calls /player for a given ally code and extracts name + fleet arena rank.
    Returns {"name": str, "rank": int} or None on failure.
    """
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
    """
    Fetches fleet rank for every ally code in SHARD_ALLY_CODES concurrently.
    Returns {allyCode: {"name": str, "rank": int}}.
    """
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
async def poll_fleet_rank():
    if not state["ready"]:
        return

    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        print(f"[ERROR] Cannot find Discord channel ID {CHANNEL_ID}.")
        return

    async with aiohttp.ClientSession() as session:

        new_table = await fetch_shard_table(session)
        if not new_table:
            print("[WARN] Shard table fetch returned nothing — skipping this poll.")
            return

        my_entry = new_table.get(ALLY_CODE)
        if my_entry is None:
            print(f"[WARN] Your ally code {ALLY_CODE} not found in shard table this poll.")
            return

        current_rank = my_entry["rank"]
        last_rank = state["my_rank"]

        # ── First poll: establish baseline ───────────────────────────────────
        if last_rank is None:
            state["my_rank"]    = current_rank
            state["last_table"] = new_table
            print(f"[INFO] Monitoring started. Fleet rank: #{current_rank}. "
                  f"Tracking {len(new_table)} players in shard.")
            await channel.send(
                f"🚀 **Fleet Arena Watcher is online!**\n"
                f"Currently monitoring rank **#{current_rank}** in Fleet Arena.\n"
                f"Tracking {len(new_table)} players in your shard.\n"
                f"You'll be notified immediately if someone attacks and takes your rank."
            )
            return

        # ── Rank dropped — we were attacked! ─────────────────────────────────
        if current_rank > last_rank:
            print(f"[ALERT] Rank dropped #{last_rank} → #{current_rank}. Identifying attacker...")

            # Find whoever is now sitting at your old rank
            attacker_name = None
            attacker_old_rank = None
            for code, entry in new_table.items():
                if code == ALLY_CODE:
                    continue
                if entry["rank"] == last_rank:
                    attacker_name = entry["name"]
                    # Look up where they were last poll, to sanity-check range
                    prev_entry = state["last_table"].get(code)
                    attacker_old_rank = prev_entry["rank"] if prev_entry else None
                    break

            # Sanity-check using the attack range table, if we know their old rank
            range_note = ""
            if attacker_name and attacker_old_rank is not None:
                if could_have_reached(attacker_old_rank, last_rank):
                    range_note = ""  # plausible, no need to flag
                else:
                    range_note = (
                        f"\n_(Note: based on rank #{attacker_old_rank}, this attack range is "
                        f"unusual — double check if this seems off.)_"
                    )

            if attacker_name:
                await channel.send(
                    f"⚔️ **Fleet Arena Attack!**\n"
                    f"**{attacker_name}** knocked you out!\n"
                    f"📉 Your rank: **#{last_rank}** → **#{current_rank}**"
                    f"{range_note}"
                )
            else:
                # Whoever took your spot might be outside the tracked top 50.
                await channel.send(
                    f"⚔️ **Fleet Arena Attack!**\n"
                    f"You were knocked from **#{last_rank}** to **#{current_rank}**.\n"
                    f"_(The attacker isn't in your tracked top {len(SHARD_ALLY_CODES)} list — "
                    f"they may have climbed from further down the shard.)_"
                )

            state["my_rank"]    = current_rank
            state["last_table"] = new_table

        # ── Rank improved ─────────────────────────────────────────────────────
        elif current_rank < last_rank:
            print(f"[INFO] Rank improved: #{last_rank} → #{current_rank}")
            state["my_rank"]    = current_rank
            state["last_table"] = new_table

        # ── No change ─────────────────────────────────────────────────────────
        else:
            print(f"[INFO] Rank unchanged: #{current_rank}")
            state["last_table"] = new_table


@poll_fleet_rank.before_loop
async def before_poll():
    await client.wait_until_ready()


# ─────────────────────────────────────────────
#  BOT EVENTS
# ─────────────────────────────────────────────
@client.event
async def on_ready():
    print(f"[INFO] Logged in as {client.user}")

    missing = []
    if not ALLY_CODE:
        missing.append("ALLY_CODE")
    if CHANNEL_ID == 0:
        missing.append("CHANNEL_ID")
    if missing:
        print(f"[ERROR] Missing environment variables: {', '.join(missing)}")
        await client.close()
        return

    state["ready"] = True
    poll_fleet_rank.start()
    print(f"[INFO] Polling fleet arena every {POLL_SECONDS}s via {COMLINK_URL}")
    print(f"[INFO] Tracking {len(SHARD_ALLY_CODES)} ally codes in shard roster.")


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN environment variable is not set.")
    client.run(DISCORD_TOKEN)
