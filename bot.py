"""
SWGOH Fleet Arena Attack Notifier Bot
--------------------------------------
Every poll interval, fetches the top 50 of your fleet arena shard and
compares it to the previous snapshot. When your rank drops, the attacker
is identified by finding who newly appeared at your old rank.
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
ALLY_CODE     = os.environ.get("ALLY_CODE",     "")       # Your ally code (numbers only)
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")       # Your Discord bot token
CHANNEL_ID    = int(os.environ.get("CHANNEL_ID", "0"))    # Discord channel ID for alerts
POLL_SECONDS  = int(os.environ.get("POLL_SECONDS", "30")) # How often to check (seconds)
SNAPSHOT_SIZE = int(os.environ.get("SNAPSHOT_SIZE", "50"))# How many ranks to track

# ─────────────────────────────────────────────
#  DISCORD BOT SETUP
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# State shared across polls
state = {
    "my_rank":          None,   # Your current rank
    "my_player_id":     None,   # Your playerId from the leaderboard
    "last_snapshot":    {},     # Dict of {rank: {"name": ..., "playerId": ...}}
    "ready":            False,
}


# ─────────────────────────────────────────────
#  COMLINK HELPERS
# ─────────────────────────────────────────────
async def fetch_my_rank(session: aiohttp.ClientSession) -> tuple[int | None, str | None]:
    """
    Fetches your current fleet arena rank and playerId via /playerArena.
    Returns (rank, playerId) or (None, None) on failure.
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
                return None, None
            data = await resp.json()
            for profile in data.get("pvpProfile", []):
                if profile.get("tab") == 2:  # tab 2 = fleet arena
                    rank = profile.get("rank")
                    player_id = data.get("playerId") or data.get("id")
                    return rank, player_id
    except Exception as e:
        print(f"[ERROR] fetch_my_rank: {e}")
    return None, None


async def fetch_leaderboard_snapshot(session: aiohttp.ClientSession) -> dict:
    """
    Fetches the fleet arena leaderboard and returns a dict of:
        { rank (int): {"name": str, "playerId": str} }
    covering the top SNAPSHOT_SIZE ranks.
    Returns an empty dict on failure.
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
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                print(f"[WARN] /getLeaderboard returned HTTP {resp.status}")
                return {}
            data = await resp.json()
            entries = data.get("item", [])
            snapshot = {}
            for entry in entries:
                rank = entry.get("rank")
                if rank is None or rank > SNAPSHOT_SIZE:
                    continue
                snapshot[rank] = {
                    "name":     entry.get("name", "Unknown Player"),
                    "playerId": entry.get("playerId") or entry.get("id", ""),
                }
            return snapshot
    except Exception as e:
        print(f"[ERROR] fetch_leaderboard_snapshot: {e}")
        return {}


def find_my_rank_in_snapshot(snapshot: dict, my_player_id: str) -> int | None:
    """
    Scans the snapshot to find which rank your playerId currently occupies.
    More reliable than /playerArena alone because it uses the same data source.
    """
    for rank, entry in snapshot.items():
        if entry.get("playerId") == my_player_id:
            return rank
    return None


def find_attacker_in_snapshots(
    old_snapshot: dict,
    new_snapshot: dict,
    my_old_rank: int,
) -> dict | None:
    """
    Compares two snapshots to find who attacked you.

    When you get knocked from rank N to N+x, the attacker now sits somewhere
    at or above your old rank. We look for a player who:
      1. Appeared at a rank <= my_old_rank in the new snapshot
      2. Was NOT at that rank in the old snapshot (i.e. they moved up)
      3. Was at a rank WORSE than my_old_rank in the old snapshot (i.e. they climbed)

    We return the player now sitting at exactly my_old_rank as the primary suspect,
    falling back to any newcomer in ranks 1..my_old_rank if that slot is ambiguous.
    """
    # Primary check: who is now at my old rank?
    new_occupant = new_snapshot.get(my_old_rank)
    old_occupant = old_snapshot.get(my_old_rank)

    if new_occupant and (
        old_occupant is None
        or new_occupant["playerId"] != old_occupant["playerId"]
    ):
        # Confirm they weren't already at a rank better than mine
        old_rank_of_new_occupant = next(
            (r for r, e in old_snapshot.items() if e["playerId"] == new_occupant["playerId"]),
            None,
        )
        if old_rank_of_new_occupant is None or old_rank_of_new_occupant > my_old_rank:
            return new_occupant

    # Fallback: scan ranks 1..my_old_rank for anyone who climbed from below
    old_player_ids_above = {
        e["playerId"] for r, e in old_snapshot.items() if r <= my_old_rank
    }
    for rank in range(1, my_old_rank + 1):
        new_entry = new_snapshot.get(rank)
        if new_entry and new_entry["playerId"] not in old_player_ids_above:
            return new_entry

    return None


# ─────────────────────────────────────────────
#  POLLING LOOP
# ─────────────────────────────────────────────
@tasks.loop(seconds=POLL_SECONDS)
async def poll_fleet_rank():
    """Main polling loop — snapshots the leaderboard and fires alerts on rank drops."""
    if not state["ready"]:
        return

    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        print(f"[ERROR] Cannot find Discord channel ID {CHANNEL_ID}. Check your CHANNEL_ID setting.")
        return

    async with aiohttp.ClientSession() as session:

        # Always fetch a fresh leaderboard snapshot
        new_snapshot = await fetch_leaderboard_snapshot(session)
        if not new_snapshot:
            print("[WARN] Empty leaderboard snapshot — skipping this poll.")
            return

        # On the very first poll we need to establish our playerId
        if state["my_player_id"] is None:
            _, player_id = await fetch_my_rank(session)
            if player_id is None:
                print("[WARN] Could not determine your playerId yet — retrying next poll.")
                return
            state["my_player_id"] = player_id
            print(f"[INFO] Your playerId identified: {player_id}")

        # Find our rank within the fresh snapshot
        current_rank = find_my_rank_in_snapshot(new_snapshot, state["my_player_id"])

        if current_rank is None:
            # We're outside the top SNAPSHOT_SIZE — fall back to /playerArena
            current_rank, _ = await fetch_my_rank(session)
            if current_rank is None:
                print("[WARN] Could not determine your rank this poll.")
                return
            print(f"[INFO] You are outside top {SNAPSHOT_SIZE}. Rank from /playerArena: #{current_rank}")

        last_rank = state["my_rank"]

        # ── First poll ────────────────────────────────────────────────────────
        if last_rank is None:
            state["my_rank"]       = current_rank
            state["last_snapshot"] = new_snapshot
            print(f"[INFO] Monitoring started. Current fleet rank: #{current_rank}")
            await channel.send(
                f"🚀 **Fleet Arena Watcher is online!**\n"
                f"Currently monitoring rank **#{current_rank}** in Fleet Arena.\n"
                f"Tracking the top {SNAPSHOT_SIZE} players every {POLL_SECONDS}s.\n"
                f"You'll be notified immediately if someone attacks and takes your rank."
            )
            return

        # ── Rank dropped — we were attacked! ─────────────────────────────────
        if current_rank > last_rank:
            print(f"[ALERT] Rank dropped {last_rank} → {current_rank}. Identifying attacker...")

            attacker = find_attacker_in_snapshots(
                old_snapshot=state["last_snapshot"],
                new_snapshot=new_snapshot,
                my_old_rank=last_rank,
            )

            if attacker:
                await channel.send(
                    f"⚔️ **Fleet Arena Attack!**\n"
                    f"**{attacker['name']}** knocked you out!\n"
                    f"📉 Your rank: **#{last_rank}** → **#{current_rank}**"
                )
            else:
                # Attacker has already been knocked back down within this poll window
                await channel.send(
                    f"⚔️ **Fleet Arena Attack!**\n"
                    f"Someone knocked you from **#{last_rank}** to **#{current_rank}**.\n"
                    f"_(The attacker couldn't be identified — they were knocked back down "
                    f"within the {POLL_SECONDS}s poll window before we could catch them.)_"
                )

            state["my_rank"]       = current_rank
            state["last_snapshot"] = new_snapshot

        # ── Rank improved ─────────────────────────────────────────────────────
        elif current_rank < last_rank:
            print(f"[INFO] Rank improved: #{last_rank} → #{current_rank}")
            state["my_rank"]       = current_rank
            state["last_snapshot"] = new_snapshot

        # ── No change ─────────────────────────────────────────────────────────
        else:
            print(f"[INFO] Rank unchanged: #{current_rank}")
            state["last_snapshot"] = new_snapshot  # still update snapshot for attacker tracking


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
    print(f"[INFO] Tracking top {SNAPSHOT_SIZE} ranks per poll.")


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN environment variable is not set.")
    client.run(DISCORD_TOKEN)
