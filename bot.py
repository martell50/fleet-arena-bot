"""
SWGOH Fleet Arena Attack Notifier Bot — v3
-------------------------------------------
Uses /playerArena exclusively, which returns both your rank AND the
opponent list (the players visible in your arena battle screen).
When your rank drops, the attacker is identified from those opponents —
specifically whoever is now ranked above you that wasn't before.
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
ALLY_CODE     = os.environ.get("ALLY_CODE",     "")
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
CHANNEL_ID    = int(os.environ.get("CHANNEL_ID", "0"))
POLL_SECONDS  = int(os.environ.get("POLL_SECONDS", "30"))

# ─────────────────────────────────────────────
#  DISCORD BOT SETUP
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

state = {
    "my_rank":           None,   # Your last known rank
    "my_player_id":      None,   # Your playerId
    "last_opponents":    {},     # {playerId: {"name": str, "rank": int}} from last poll
    "ready":             False,
}


# ─────────────────────────────────────────────
#  COMLINK HELPERS
# ─────────────────────────────────────────────
async def fetch_arena_profile(session: aiohttp.ClientSession) -> dict | None:
    """
    Calls /playerArena with your ally code.
    Returns the full response dict, or None on failure.

    The response contains:
      - pvpProfile[].tab      (1 = squad arena, 2 = fleet arena)
      - pvpProfile[].rank     your current rank
      - pvpProfile[].leaderboardEntry[]  nearby players (your battle opponents)
      - playerId              your unique player ID
    """
    url = f"{COMLINK_URL}/playerArena"
    payload = {
        "payload": {
            "allyCode": str(ALLY_CODE),
            "playerDetailsOnly": False,
        },
        "enums": False,
    }
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                print(f"[WARN] /playerArena returned HTTP {resp.status}")
                return None
            return await resp.json()
    except Exception as e:
        print(f"[ERROR] fetch_arena_profile: {e}")
        return None


async def fetch_player_name(session: aiohttp.ClientSession, player_id: str) -> str:
    """
    Looks up a player's in-game name by their playerId via /player.
    Falls back to 'Unknown Player' on failure.
    """
    url = f"{COMLINK_URL}/player"
    payload = {
        "payload": {"playerId": player_id},
        "enums": False,
    }
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("name", "Unknown Player")
    except Exception as e:
        print(f"[ERROR] fetch_player_name({player_id}): {e}")
    return "Unknown Player"


def parse_fleet_profile(arena_data: dict) -> tuple[int | None, str | None, dict]:
    """
    Extracts fleet arena rank, your playerId, and opponent map from /playerArena response.

    Returns:
        rank         - your current fleet arena rank (int or None)
        player_id    - your playerId string (or None)
        opponents    - {playerId: {"name": str, "rank": int}} for all leaderboard entries
    """
    rank = None
    player_id = arena_data.get("playerId") or arena_data.get("id")
    opponents = {}

    for profile in arena_data.get("pvpProfile", []):
        if profile.get("tab") != 2:   # 2 = fleet arena
            continue

        rank = profile.get("rank")

        # leaderboardEntry contains the players shown as your battle opponents
        # Each entry has: playerId (or id), name, rank
        for entry in profile.get("leaderboardEntry", []):
            pid  = entry.get("playerId") or entry.get("id", "")
            name = entry.get("name", "Unknown Player")
            r    = entry.get("rank")
            if pid and r is not None:
                opponents[pid] = {"name": name, "rank": r}

        break  # found fleet profile, no need to continue

    return rank, player_id, opponents


def find_attacker(
    old_opponents: dict,
    new_opponents: dict,
    my_old_rank: int,
    my_player_id: str,
) -> dict | None:
    """
    Identifies the attacker by comparing old and new opponent lists.

    Strategy:
      1. Find any player who is now ranked ABOVE (lower number than) your old rank,
         but was ranked BELOW (higher number than) your old rank in the previous poll.
         That player beat you.
      2. If multiple candidates, prefer the one now sitting at exactly your old rank.
    """
    candidates = []

    for pid, new_entry in new_opponents.items():
        if pid == my_player_id:
            continue
        new_rank = new_entry.get("rank")
        if new_rank is None or new_rank >= my_old_rank:
            continue  # they're not above your old rank, not the attacker

        old_entry = old_opponents.get(pid)
        if old_entry is None:
            # Player wasn't in our opponent list before — appeared from outside, likely climbed up
            candidates.append(new_entry)
        elif old_entry.get("rank", 9999) > my_old_rank:
            # They were below you before and are now above — clear attacker
            candidates.append(new_entry)

    if not candidates:
        return None

    # Prefer whoever is now sitting at exactly my_old_rank
    for c in candidates:
        if c.get("rank") == my_old_rank:
            return c

    # Otherwise return the one closest to my_old_rank (highest rank number below it)
    return max(candidates, key=lambda c: c.get("rank", 0))


# ─────────────────────────────────────────────
#  POLLING LOOP
# ─────────────────────────────────────────────
@tasks.loop(seconds=POLL_SECONDS)
async def poll_fleet_rank():
    if not state["ready"]:
        return

    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        print(f"[ERROR] Cannot find Discord channel ID {CHANNEL_ID}. Check your CHANNEL_ID setting.")
        return

    async with aiohttp.ClientSession() as session:

        arena_data = await fetch_arena_profile(session)
        if arena_data is None:
            return

        current_rank, player_id, current_opponents = parse_fleet_profile(arena_data)

        if current_rank is None:
            print("[WARN] Could not parse fleet arena rank from API response.")
            return

        # Store our playerId on first successful poll
        if state["my_player_id"] is None and player_id:
            state["my_player_id"] = player_id
            print(f"[INFO] Your playerId: {player_id}")

        last_rank = state["my_rank"]

        # ── First poll: just record baseline ─────────────────────────────────
        if last_rank is None:
            state["my_rank"]        = current_rank
            state["last_opponents"] = current_opponents
            print(f"[INFO] Monitoring started. Fleet rank: #{current_rank}")
            await channel.send(
                f"🚀 **Fleet Arena Watcher is online!**\n"
                f"Currently monitoring rank **#{current_rank}** in Fleet Arena.\n"
                f"You'll be notified immediately if someone attacks and takes your rank."
            )
            return

        # ── Rank dropped — we were attacked! ─────────────────────────────────
        if current_rank > last_rank:
            print(f"[ALERT] Rank dropped #{last_rank} → #{current_rank}. Identifying attacker...")

            attacker = find_attacker(
                old_opponents=state["last_opponents"],
                new_opponents=current_opponents,
                my_old_rank=last_rank,
                my_player_id=state["my_player_id"] or "",
            )

            if attacker:
                name = attacker.get("name", "Unknown Player")
                await channel.send(
                    f"⚔️ **Fleet Arena Attack!**\n"
                    f"**{name}** knocked you out!\n"
                    f"📉 Your rank: **#{last_rank}** → **#{current_rank}**"
                )
            else:
                # Opponent list didn't show who did it — fall back to checking who
                # is now at our old rank by looking through current_opponents
                suspect = next(
                    (e for e in current_opponents.values() if e.get("rank") == last_rank),
                    None
                )
                if suspect:
                    await channel.send(
                        f"⚔️ **Fleet Arena Attack!**\n"
                        f"**{suspect['name']}** is now at your old rank!\n"
                        f"📉 Your rank: **#{last_rank}** → **#{current_rank}**"
                    )
                else:
                    await channel.send(
                        f"⚔️ **Fleet Arena Attack!**\n"
                        f"Someone knocked you from **#{last_rank}** to **#{current_rank}**.\n"
                        f"_(Attacker couldn't be identified — they may have been knocked back "
                        f"down within the {POLL_SECONDS}s poll window.)_"
                    )

            state["my_rank"]        = current_rank
            state["last_opponents"] = current_opponents

        # ── Rank improved ─────────────────────────────────────────────────────
        elif current_rank < last_rank:
            print(f"[INFO] Rank improved: #{last_rank} → #{current_rank}")
            state["my_rank"]        = current_rank
            state["last_opponents"] = current_opponents

        # ── No change ─────────────────────────────────────────────────────────
        else:
            print(f"[INFO] Rank unchanged: #{current_rank}")
            # Still update opponents so we track any positional shifts around us
            state["last_opponents"] = current_opponents


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


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN environment variable is not set.")
    client.run(DISCORD_TOKEN)
