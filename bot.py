"""
SWGOH Fleet Arena Attack Notifier Bot — v4
-------------------------------------------
Polls /playerArena every 30 seconds (fleet arena only, tab == 2).
When your rank drops, identifies the attacker as whoever is now
sitting at your old rank in the leaderboard entries.
Since fleet arena battles take at least 2 minutes, the attacker
is guaranteed to still be there on the next poll.
"""

import os
import json
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
    "my_rank": None,   # Last known fleet arena rank
    "ready":   False,
}


# ─────────────────────────────────────────────
#  COMLINK HELPER
# ─────────────────────────────────────────────
async def fetch_fleet_arena(session: aiohttp.ClientSession) -> dict | None:
    """
    Calls /playerArena and returns the fleet arena pvpProfile block (tab == 2).
    Returns None on any failure.

    The returned dict contains:
      "rank"             -- your current fleet arena rank (int)
      "leaderboardEntry" -- list of nearby players, each with:
                              "id" or "playerId" : unique player ID
                              "name"             : in-game name
                              "rank"             : their current rank
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
            data = await resp.json()

            # Always dump the top-level keys of the raw response so we can see
            # exactly what this Comlink instance returns.
            print(f"[DEBUG] Top-level response keys: {list(data.keys())}")

            pvp = data.get("pvpProfile", [])
            print(f"[DEBUG] pvpProfile has {len(pvp)} entries")
            for p in pvp:
                print(f"[DEBUG] pvpProfile entry keys: {list(p.keys())} | tab={p.get('tab')} rank={p.get('rank')}")

            # Fleet arena is tab == 2
            fleet_profile = None
            for profile in pvp:
                if profile.get("tab") == 2:
                    fleet_profile = profile
                    break

            if fleet_profile is None:
                print("[WARN] No fleet arena profile (tab==2) found in /playerArena response.")
                print(f"[DEBUG] Full pvpProfile dump: {json.dumps(pvp, indent=2)[:3000]}")
                return None

            # Dump the FULL fleet profile raw JSON once, so we can see every field
            # Comlink actually returns (leaderboardEntry, opponents, rivals, etc.)
            if state["my_rank"] is None:
                print(f"[DEBUG] FULL fleet pvpProfile JSON:\n{json.dumps(fleet_profile, indent=2)[:6000]}")

            return fleet_profile

    except Exception as e:
        print(f"[ERROR] fetch_fleet_arena: {e}")
        return None


def find_player_at_rank(leaderboard_entries: list, target_rank: int) -> str:
    """
    Scans leaderboardEntry list for whoever currently holds target_rank.
    Returns their in-game name, or None if not found.
    """
    for entry in leaderboard_entries:
        entry_rank = entry.get("rank")
        if entry_rank == target_rank:
            # Try all known name field variants
            name = (
                entry.get("name")
                or entry.get("playerName")
                or entry.get("player_name")
            )
            return name or "Unknown Player"
    return None


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
        fleet_profile = await fetch_fleet_arena(session)
        if fleet_profile is None:
            return

        current_rank = fleet_profile.get("rank")
        if current_rank is None:
            print("[WARN] Fleet arena profile has no 'rank' field.")
            return

        leaderboard_entries = fleet_profile.get("leaderboardEntry", [])
        last_rank = state["my_rank"]

        # Log all entries on first poll for debugging
        if last_rank is None:
            print(f"[INFO] First poll. Fleet rank: #{current_rank}. "
                  f"Leaderboard entries returned: {len(leaderboard_entries)}")
            for e in leaderboard_entries:
                print(f"  rank={e.get('rank')} name={e.get('name') or e.get('playerName')} "
                      f"id={e.get('id') or e.get('playerId')}")

        # ── First poll: establish baseline ───────────────────────────────────
        if last_rank is None:
            state["my_rank"] = current_rank
            await channel.send(
                f"🚀 **Fleet Arena Watcher is online!**\n"
                f"Currently monitoring rank **#{current_rank}** in Fleet Arena.\n"
                f"You'll be notified immediately when someone takes your rank."
            )
            return

        # ── Rank dropped — we were attacked! ─────────────────────────────────
        if current_rank > last_rank:
            print(f"[ALERT] Rank dropped #{last_rank} → #{current_rank}.")
            print(f"[DEBUG] Full fleet_profile JSON at time of attack:\n{json.dumps(fleet_profile, indent=2)[:6000]}")

            # The attacker is whoever now holds our old rank
            attacker_name = find_player_at_rank(leaderboard_entries, last_rank)

            if attacker_name:
                await channel.send(
                    f"⚔️ **Fleet Arena Attack!**\n"
                    f"**{attacker_name}** knocked you out!\n"
                    f"📉 Your rank: **#{last_rank}** → **#{current_rank}**"
                )
            else:
                # This means rank {last_rank} wasn't in the leaderboardEntry list at all.
                # Log what ranks we did get so we can diagnose.
                ranks_present = sorted([e.get("rank") for e in leaderboard_entries if e.get("rank")])
                print(f"[WARN] Rank #{last_rank} not found in leaderboard entries. "
                      f"Ranks present: {ranks_present}")
                await channel.send(
                    f"⚔️ **Fleet Arena Attack!**\n"
                    f"You were knocked from **#{last_rank}** to **#{current_rank}**.\n"
                    f"_(Could not find rank #{last_rank} in the {len(leaderboard_entries)} "
                    f"leaderboard entries returned — check logs for details.)_"
                )

            state["my_rank"] = current_rank

        # ── Rank improved ─────────────────────────────────────────────────────
        elif current_rank < last_rank:
            print(f"[INFO] Rank improved: #{last_rank} → #{current_rank}")
            state["my_rank"] = current_rank

        # ── No change ─────────────────────────────────────────────────────────
        else:
            print(f"[INFO] Rank unchanged: #{current_rank}")


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
