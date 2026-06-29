"""
SWGOH Fleet Arena Attack Notifier Bot
--------------------------------------
Polls the SWGOH Comlink API to detect when your fleet arena rank drops,
then sends a Discord notification with the attacker's name and rank change.
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
ALLY_CODE     = os.environ.get("ALLY_CODE",     "")          # Your in-game ally code (numbers only)
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")          # Your bot token
CHANNEL_ID    = int(os.environ.get("CHANNEL_ID", "0"))       # Discord channel ID to post alerts
POLL_SECONDS  = int(os.environ.get("POLL_SECONDS", "30"))    # How often to check (seconds)

# ─────────────────────────────────────────────
#  DISCORD BOT SETUP
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# Track state between polls
state = {
    "last_rank": None,
    "my_player_id": None,
    "ready": False,
}


# ─────────────────────────────────────────────
#  COMLINK HELPERS
# ─────────────────────────────────────────────
async def fetch_my_arena_data(session: aiohttp.ClientSession) -> dict | None:
    """
    Fetches your playerArena profile from Comlink.
    Returns the full pvpProfile list or None on error.
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
                print(f"[WARN] playerArena returned HTTP {resp.status}")
                return None
            data = await resp.json()
            return data.get("pvpProfile", [])
    except Exception as e:
        print(f"[ERROR] fetch_my_arena_data: {e}")
        return None


def extract_fleet_rank(pvp_profile: list) -> int | None:
    """
    Extracts fleet arena rank from pvpProfile.
    tab == 2 is fleet arena; tab == 1 is squad arena.
    """
    for profile in pvp_profile:
        if profile.get("tab") == 2:
            return profile.get("rank")
    return None


async def fetch_leaderboard_slice(session: aiohttp.ClientSession, rank: int) -> list:
    """
    Fetches a slice of the fleet arena leaderboard around a given rank.
    Returns a list of leaderboard entries.
    """
    url = f"{COMLINK_URL}/getLeaderboard"
    payload = {
        "payload": {
            "leaderboardType": 4,   # 4 = fleet arena
            "monthOffset": 0,
        },
        "enums": False,
    }
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                print(f"[WARN] getLeaderboard returned HTTP {resp.status}")
                return []
            data = await resp.json()
            return data.get("item", [])
    except Exception as e:
        print(f"[ERROR] fetch_leaderboard_slice: {e}")
        return []


async def find_player_at_rank(session: aiohttp.ClientSession, target_rank: int) -> dict | None:
    """
    Finds the player currently sitting at target_rank in fleet arena.
    Returns a dict with 'name' and 'allyCode', or None.
    """
    entries = await fetch_leaderboard_slice(session, target_rank)
    for entry in entries:
        if entry.get("rank") == target_rank:
            return {
                "name": entry.get("name", "Unknown Player"),
                "allyCode": entry.get("allyCode", ""),
            }
    return None


async def get_my_player_name(session: aiohttp.ClientSession) -> str:
    """Fetches your own in-game name for the startup message."""
    url = f"{COMLINK_URL}/player"
    payload = {
        "payload": {"allyCode": str(ALLY_CODE)},
        "enums": False,
    }
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("name", "Unknown")
    except Exception as e:
        print(f"[ERROR] get_my_player_name: {e}")
    return "Unknown"


# ─────────────────────────────────────────────
#  POLLING LOOP
# ─────────────────────────────────────────────
@tasks.loop(seconds=POLL_SECONDS)
async def poll_fleet_rank():
    """Main polling loop — checks your fleet rank and fires alerts on drops."""
    if not state["ready"]:
        return

    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        print(f"[ERROR] Cannot find Discord channel ID {CHANNEL_ID}. Check your CHANNEL_ID setting.")
        return

    async with aiohttp.ClientSession() as session:
        pvp_profile = await fetch_my_arena_data(session)
        if pvp_profile is None:
            return

        current_rank = extract_fleet_rank(pvp_profile)
        if current_rank is None:
            print("[WARN] Could not find fleet arena rank in API response.")
            return

        last_rank = state["last_rank"]

        if last_rank is None:
            # First successful poll — just record the rank silently
            state["last_rank"] = current_rank
            print(f"[INFO] Monitoring started. Current fleet rank: {current_rank}")
            await channel.send(
                f"🚀 **Fleet Arena Watcher is online!**\n"
                f"Currently monitoring rank **#{current_rank}** in Fleet Arena.\n"
                f"You'll be notified immediately if someone attacks and takes your rank."
            )
            return

        if current_rank > last_rank:
            # Rank dropped — we were attacked!
            print(f"[ALERT] Rank dropped from {last_rank} → {current_rank}. Finding attacker...")

            # The attacker now sits at your old rank
            attacker = await find_player_at_rank(session, last_rank)

            if attacker:
                attacker_name = attacker["name"]
                attacker_code = attacker["allyCode"]
                ally_code_fmt = f"`{attacker_code}`" if attacker_code else ""
                await channel.send(
                    f"⚔️ **Fleet Arena Attack!**\n"
                    f"**{attacker_name}** {ally_code_fmt} knocked you out!\n"
                    f"📉 Your rank: **#{last_rank}** → **#{current_rank}**"
                )
            else:
                # Fallback if leaderboard lookup fails
                await channel.send(
                    f"⚔️ **Fleet Arena Attack!**\n"
                    f"Someone knocked you from rank **#{last_rank}** to **#{current_rank}**!\n"
                    f"_(Could not identify the attacker — they may have already been knocked down too)_"
                )

            state["last_rank"] = current_rank

        elif current_rank < last_rank:
            # Rank improved (you attacked someone, or someone above you dropped out)
            print(f"[INFO] Rank improved: {last_rank} → {current_rank}")
            state["last_rank"] = current_rank

        else:
            # No change
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

    # Validate config
    missing = []
    if not ALLY_CODE:
        missing.append("ALLY_CODE")
    if CHANNEL_ID == 0:
        missing.append("CHANNEL_ID")
    if missing:
        print(f"[ERROR] Missing environment variables: {', '.join(missing)}")
        print("        Set them and restart the bot.")
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
