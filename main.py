import os
import re
import math
import time
import asyncio
import requests
import discord
from discord import app_commands
from dotenv import load_dotenv
from datetime import datetime, timezone

import asyncpg

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ROVER_API_KEY = os.getenv("ROVER_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

ROVER_BASE = "https://registry.rover.link/api"

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

db_pool: asyncpg.Pool | None = None

# =====================================================
# DB init + helpers
# =====================================================

CREATE_TABLES_SQL = """
create table if not exists watched_groups (
  guild_id bigint not null,
  group_id bigint not null,
  label text,
  primary key (guild_id, group_id)
);

create table if not exists blacklisted_ranks (
  guild_id bigint not null,
  group_id bigint not null,
  rank_id int not null,
  reason text,
  added_by bigint,
  added_at timestamptz not null default now(),
  primary key (guild_id, group_id, rank_id)
);

create table if not exists blacklisted_users (
  guild_id bigint not null,
  roblox_user_id bigint not null,
  reason text not null,
  added_by bigint,
  added_at timestamptz not null default now(),
  primary key (guild_id, roblox_user_id)
);

create table if not exists ranklocks (
  guild_id bigint not null,
  roblox_user_id bigint not null,
  group_id bigint not null,
  max_rank_id int not null,
  reason text not null,
  set_by bigint,
  set_at timestamptz not null default now(),
  primary key (guild_id, roblox_user_id, group_id)
);
"""

async def ensure_db():
    global db_pool

    if db_pool is not None:
        return True

    if not DATABASE_URL:
        print("⚠️ DATABASE_URL missing — DB features disabled.")
        return False

    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        async with db_pool.acquire() as con:
            await con.execute(CREATE_TABLES_SQL)
        print("✅ DB connected and tables ensured.")
        return True
    except Exception as e:
        print(f"⚠️ DB connection failed — DB features disabled. Error: {e}")
        db_pool = None
        return False

async def db_exec(sql: str, *args):
    if not await ensure_db():
        return None
    assert db_pool is not None
    async with db_pool.acquire() as con:
        return await con.execute(sql, *args)

async def db_fetch(sql: str, *args):
    if not await ensure_db():
        return []
    assert db_pool is not None
    async with db_pool.acquire() as con:
        return await con.fetch(sql, *args)

async def db_fetchrow(sql: str, *args):
    if not await ensure_db():
        return None
    assert db_pool is not None
    async with db_pool.acquire() as con:
        return await con.fetchrow(sql, *args)

# =====================================================
# Helpers
# =====================================================

def safe_text(s: str, max_len: int) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s if len(s) <= max_len else (s[: max_len - 1] + "…")

def fmt_date(dt: datetime | None) -> str:
    if not dt:
        return "unknown"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")

def chunk_lines(lines: list[str], max_chars: int = 1024) -> list[str]:
    chunks = []
    cur = ""
    for line in lines:
        if len(cur) + len(line) + 1 > max_chars:
            if cur.strip():
                chunks.append(cur.rstrip())
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur.rstrip())
    return chunks

def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))

# =====================================================
# Roblox / RoVer helpers (sync, wrapped by asyncio.to_thread)
# =====================================================

def discord_to_roblox(guild_id: int, discord_id: int) -> int | None:
    headers = {"Authorization": f"Bearer {ROVER_API_KEY}"}
    url = f"{ROVER_BASE}/guilds/{guild_id}/discord-to-roblox/{discord_id}"
    r = requests.get(url, headers=headers, timeout=15)
    if r.status_code != 200:
        return None
    data = r.json()
    rid = data.get("robloxId") or data.get("roblox_id") or data.get("id")
    return int(rid) if rid else None

def username_to_roblox(username: str) -> int | None:
    r = requests.post(
        "https://users.roblox.com/v1/usernames/users",
        json={"usernames": [username], "excludeBannedUsers": False},
        timeout=15
    )
    if r.status_code != 200:
        return None
    data = r.json()
    if not data.get("data"):
        return None
    return int(data["data"][0]["id"])

def get_roblox_user(user_id: int) -> dict:
    r = requests.get(f"https://users.roblox.com/v1/users/{user_id}", timeout=15)
    r.raise_for_status()
    return r.json()

def get_user_groups(user_id: int) -> list:
    r = requests.get(f"https://groups.roblox.com/v2/users/{user_id}/groups/roles", timeout=20)
    r.raise_for_status()
    return r.json().get("data", [])

def account_age_days(created_iso: str) -> int:
    created_dt = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - created_dt).days

# =====================================================
# Inventory + Value estimation (best effort)
# =====================================================

# Common avatar-ish asset type IDs used with inventory.roblox.com/v2/users/{userId}/inventory/{assetTypeId}
# (This endpoint is officially documented, but not every type is guaranteed to work for every account.)
ASSET_TYPES = [
    (8, "Hats"),
    (41, "Hair"),
    (42, "Face Acc"),
    (43, "Neck Acc"),
    (44, "Shoulder Acc"),
    (45, "Front Acc"),
    (46, "Back Acc"),
    (47, "Waist Acc"),
    (2, "T-Shirts"),
    (11, "Shirts"),
    (12, "Pants"),
    (18, "Faces"),
]

def _inv_fetch_asset_type(user_id: int, asset_type_id: int, limit_pages: int = 3, page_size: int = 100):
    """
    Returns list of assetIds for this asset type.
    Stops after limit_pages to avoid hammering Roblox.
    If inventory is private or endpoint blocks, returns None.
    """
    asset_ids: list[int] = []
    cursor = ""
    seen_cursors = set()

    for _ in range(limit_pages):
        url = f"https://inventory.roblox.com/v2/users/{user_id}/inventory/{asset_type_id}"
        params = {"limit": page_size}
        if cursor:
            params["cursor"] = cursor

        r = requests.get(url, params=params, timeout=20)

        if r.status_code in (401, 403):
            return None  # private inventory / blocked
        if r.status_code != 200:
            return asset_ids  # partial

        data = r.json()
        items = data.get("data") or []
        for it in items:
            # usually: it["assetId"] exists
            aid = it.get("assetId") or it.get("id")
            if isinstance(aid, int):
                asset_ids.append(aid)

        cursor = data.get("nextPageCursor")
        if not cursor:
            break

        # guard against cursor loops
        if cursor in seen_cursors:
            break
        seen_cursors.add(cursor)

    return asset_ids


def _economy_asset_price(asset_id: int):
    """
    Best-effort price lookup.
    economy.roblox.com/v2/assets/{assetId}/details often returns "price" for catalog assets.
    If offsale/limited/etc, price might be None or 0.
    """
    url = f"https://economy.roblox.com/v2/assets/{asset_id}/details"
    r = requests.get(url, timeout=15)
    if r.status_code != 200:
        return None
    data = r.json()
    price = data.get("price")
    if isinstance(price, (int, float)) and price > 0:
        return int(price)
    return None


def _gamepasses_owned(user_id: int, max_pages: int = 3, count: int = 100):
    """
    Lists gamepasses a user owns using:
    https://apis.roblox.com/game-passes/v1/users/{USERID}/game-passes?count=100&exclusiveStartId=...
    (Endpoint pattern discussed on Roblox DevForum)  :contentReference[oaicite:3]{index=3}
    Returns list of gamePassIds (ints), or None if blocked.
    """
    gamepass_ids: list[int] = []
    exclusive_start_id = 0

    for _ in range(max_pages):
        url = f"https://apis.roblox.com/game-passes/v1/users/{user_id}/game-passes"
        params = {"count": count, "exclusiveStartId": exclusive_start_id}
        r = requests.get(url, params=params, timeout=20)

        if r.status_code in (401, 403):
            return None
        if r.status_code != 200:
            return gamepass_ids

        data = r.json()

        # response shape varies; try common keys
        items = data.get("gamePasses") or data.get("data") or data.get("gamepasses") or []
        if not items:
            break

        for gp in items:
            gpid = gp.get("gamePassId") or gp.get("id")
            if isinstance(gpid, int):
                gamepass_ids.append(gpid)

        # pagination: set exclusive_start_id to last ID
        exclusive_start_id = gamepass_ids[-1] if gamepass_ids else 0

        # if fewer than requested, we’re done
        if len(items) < count:
            break

    return gamepass_ids


def _gamepass_price(gamepass_id: int):
    """
    Tries to read price from game pass details endpoint:
    GET apis.roblox.com/game-passes/v1/game-passes/{gamePassId}/details
    Mentioned in Roblox announcements. :contentReference[oaicite:4]{index=4}
    This may be blocked/limited unauthenticated; if so returns None.
    """
    url = f"https://apis.roblox.com/game-passes/v1/game-passes/{gamepass_id}/details"
    r = requests.get(url, timeout=15)
    if r.status_code != 200:
        return None
    data = r.json()
    price = data.get("price")
    if isinstance(price, (int, float)) and price > 0:
        return int(price)
    return None


async def compute_value_estimate(user_id: int, max_assets_to_price: int = 120):
    """
    Returns dict:
      {
        "inventory_private": bool,
        "type_counts": {label: count},
        "priced_assets": n,
        "est_value_robux": int or None,
        "gamepasses_count": int or None,
        "gamepasses_priced": int,
        "gamepasses_value_robux": int or None,
        "notes": [str]
      }
    """
    # Settings: keep it light so Railway doesn’t get cooked
    max_assets_to_price = clamp(max_assets_to_price, 30, 300)

    notes: list[str] = []
    type_counts: dict[str, int] = {}
    all_assets: list[int] = []

    inventory_private = False

    # Fetch inventory ids per type (few pages each)
    for asset_type_id, label in ASSET_TYPES:
        asset_ids = await asyncio.to_thread(_inv_fetch_asset_type, user_id, asset_type_id, 2, 100)
        if asset_ids is None:
            inventory_private = True
            break
        type_counts[label] = len(asset_ids)
        all_assets.extend(asset_ids)

    est_value = None
    priced_assets = 0

    if inventory_private:
        notes.append("Inventory appears private or blocked (Roblox returned 403/401).")
    else:
        # Deduplicate and limit pricing calls
        uniq_assets = list(dict.fromkeys(all_assets))
        if not uniq_assets:
            notes.append("No inventory items found in checked categories.")
        else:
            # Sample first N (sorted for stable-ish output)
            uniq_assets = uniq_assets[:max_assets_to_price]

            total = 0
            for aid in uniq_assets:
                price = await asyncio.to_thread(_economy_asset_price, aid)
                if price is not None:
                    total += price
                    priced_assets += 1

            if priced_assets > 0:
                est_value = total
            else:
                notes.append("Could not read prices for sampled items (many items are offsale/limited/no price).")

    # Gamepasses owned
    gp_ids = await asyncio.to_thread(_gamepasses_owned, user_id, 3, 100)
    gamepasses_count = None if gp_ids is None else len(gp_ids)

    gp_value = None
    gp_priced = 0
    if gp_ids is None:
        notes.append("Gamepass ownership lookup blocked/private.")
    else:
        # Try pricing a small sample (avoid spamming)
        sample = gp_ids[:30]
        total_gp = 0
        for gpid in sample:
            p = await asyncio.to_thread(_gamepass_price, gpid)
            if p is not None:
                total_gp += p
                gp_priced += 1
        if gp_priced > 0:
            gp_value = total_gp
        else:
            # Not always possible without auth; call it out
            notes.append("Gamepass prices not available via public endpoint (count shown only).")

    return {
        "inventory_private": inventory_private,
        "type_counts": type_counts,
        "priced_assets": priced_assets,
        "est_value_robux": est_value,
        "gamepasses_count": gamepasses_count,
        "gamepasses_priced": gp_priced,
        "gamepasses_value_robux": gp_value,
        "notes": notes
    }

# =====================================================
# Commands: watched / blacklist / ranklock (same as before)
# =====================================================

@tree.command(name="watchgroup", description="Manage watched Roblox groups (shown in /bgcheck)")
@app_commands.describe(action="add/remove/list", group_id="Roblox group id", label="Optional label shown in embeds")
async def watchgroup(interaction: discord.Interaction, action: str, group_id: str | None = None, label: str | None = None):
    if not interaction.user.guild_permissions.manage_guild and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You need **Manage Server** (or Admin).", ephemeral=True)

    action = action.lower().strip()
    gid = int(interaction.guild_id)

    if action == "add":
        if not group_id:
            return await interaction.response.send_message("Usage: `/watchgroup action:add group_id:<id> label:<optional>`", ephemeral=True)
        await db_exec(
            "insert into watched_groups (guild_id, group_id, label) values ($1,$2,$3) on conflict (guild_id, group_id) do update set label=excluded.label",
            gid, int(group_id), label
        )
        return await interaction.response.send_message("✅ Watched group added/updated.", ephemeral=True)

    if action == "remove":
        if not group_id:
            return await interaction.response.send_message("Usage: `/watchgroup action:remove group_id:<id>`", ephemeral=True)
        await db_exec("delete from watched_groups where guild_id=$1 and group_id=$2", gid, int(group_id))
        return await interaction.response.send_message("✅ Watched group removed.", ephemeral=True)

    if action == "list":
        rows = await db_fetch("select group_id, label from watched_groups where guild_id=$1 order by group_id asc", gid)
        if not rows:
            return await interaction.response.send_message("No watched groups set.", ephemeral=True)
        lines = [f"• `{r['group_id']}` — {r['label'] or 'no label'}" for r in rows]
        return await interaction.response.send_message("\n".join(lines), ephemeral=True)

    return await interaction.response.send_message("Actions: add / remove / list", ephemeral=True)


@tree.command(name="blacklistrank", description="Flag specific group rank IDs as blacklisted")
@app_commands.describe(action="add/remove/list", group_id="Roblox group id", rank_id="Roblox rank id (0-255)", reason="Optional reason")
async def blacklistrank(interaction: discord.Interaction, action: str, group_id: str | None = None, rank_id: int | None = None, reason: str | None = None):
    if not interaction.user.guild_permissions.manage_guild and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You need **Manage Server** (or Admin).", ephemeral=True)

    action = action.lower().strip()
    gid = int(interaction.guild_id)

    if action == "add":
        if not group_id or rank_id is None:
            return await interaction.response.send_message("Usage: `/blacklistrank action:add group_id:<id> rank_id:<num> reason:<optional>`", ephemeral=True)
        await db_exec(
            """insert into blacklisted_ranks (guild_id, group_id, rank_id, reason, added_by)
               values ($1,$2,$3,$4,$5)
               on conflict (guild_id, group_id, rank_id) do update set reason=excluded.reason, added_by=excluded.added_by, added_at=now()""",
            gid, int(group_id), int(rank_id), reason, int(interaction.user.id)
        )
        return await interaction.response.send_message("✅ Blacklisted rank rule added/updated.", ephemeral=True)

    if action == "remove":
        if not group_id or rank_id is None:
            return await interaction.response.send_message("Usage: `/blacklistrank action:remove group_id:<id> rank_id:<num>`", ephemeral=True)
        await db_exec("delete from blacklisted_ranks where guild_id=$1 and group_id=$2 and rank_id=$3", gid, int(group_id), int(rank_id))
        return await interaction.response.send_message("✅ Blacklisted rank removed.", ephemeral=True)

    if action == "list":
        rows = await db_fetch("select group_id, rank_id, reason from blacklisted_ranks where guild_id=$1 order by group_id asc, rank_id asc", gid)
        if not rows:
            return await interaction.response.send_message("No blacklisted ranks set.", ephemeral=True)
        lines = [f"• `{r['group_id']}` rank **{r['rank_id']}** — {r['reason'] or 'no reason'}" for r in rows]
        msg = "\n".join(lines[:80])
        if len(lines) > 80:
            msg += f"\n...and {len(lines)-80} more"
        return await interaction.response.send_message(msg, ephemeral=True)

    return await interaction.response.send_message("Actions: add / remove / list", ephemeral=True)


@tree.command(name="blacklistuser", description="Blacklist a Roblox userId (hard flag in /bgcheck)")
@app_commands.describe(action="add/remove/check", roblox_id="Roblox user id", reason="Reason for blacklist")
async def blacklistuser(interaction: discord.Interaction, action: str, roblox_id: str, reason: str | None = None):
    if not interaction.user.guild_permissions.manage_guild and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You need **Manage Server** (or Admin).", ephemeral=True)

    action = action.lower().strip()
    gid = int(interaction.guild_id)
    rid = int(roblox_id)

    if action == "add":
        if not reason:
            return await interaction.response.send_message("Usage: `/blacklistuser action:add roblox_id:<id> reason:<text>`", ephemeral=True)
        await db_exec(
            """insert into blacklisted_users (guild_id, roblox_user_id, reason, added_by)
               values ($1,$2,$3,$4)
               on conflict (guild_id, roblox_user_id) do update set reason=excluded.reason, added_by=excluded.added_by, added_at=now()""",
            gid, rid, reason, int(interaction.user.id)
        )
        return await interaction.response.send_message("✅ User blacklisted.", ephemeral=True)

    if action == "remove":
        await db_exec("delete from blacklisted_users where guild_id=$1 and roblox_user_id=$2", gid, rid)
        return await interaction.response.send_message("✅ User removed from blacklist (if they were on it).", ephemeral=True)

    if action == "check":
        row = await db_fetchrow("select reason, added_at from blacklisted_users where guild_id=$1 and roblox_user_id=$2", gid, rid)
        if not row:
            return await interaction.response.send_message("Not blacklisted.", ephemeral=True)
        return await interaction.response.send_message(f"🚫 Blacklisted — {row['reason']} (since {fmt_date(row['added_at'])})", ephemeral=True)

    return await interaction.response.send_message("Actions: add / remove / check", ephemeral=True)


@tree.command(name="ranklock", description="Set/view/remove a max rank cap for a Roblox user in a group")
@app_commands.describe(action="set/remove/view", roblox_id="Roblox user ID", group_id="Roblox group ID", max_rank_id="Max allowed rank id", reason="Reason")
async def ranklock(interaction: discord.Interaction, action: str, roblox_id: str, group_id: str | None = None, max_rank_id: int | None = None, reason: str | None = None):
    if not interaction.user.guild_permissions.manage_roles and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You need **Manage Roles** (or Admin).", ephemeral=True)

    action = action.lower().strip()
    guild_id = int(interaction.guild_id)
    rid = int(roblox_id)

    if action == "set":
        if not group_id or max_rank_id is None or not reason:
            return await interaction.response.send_message("Usage: `/ranklock action:set roblox_id:<id> group_id:<id> max_rank_id:<num> reason:<text>`", ephemeral=True)

        await db_exec(
            """insert into ranklocks (guild_id, roblox_user_id, group_id, max_rank_id, reason, set_by)
               values ($1,$2,$3,$4,$5,$6)
               on conflict (guild_id, roblox_user_id, group_id)
               do update set max_rank_id=excluded.max_rank_id, reason=excluded.reason, set_by=excluded.set_by, set_at=now()""",
            guild_id, rid, int(group_id), int(max_rank_id), reason, int(interaction.user.id)
        )
        return await interaction.response.send_message("✅ Ranklock set/updated.", ephemeral=True)

    if action == "remove":
        if not group_id:
            return await interaction.response.send_message("Usage: `/ranklock action:remove roblox_id:<id> group_id:<id>`", ephemeral=True)
        await db_exec("delete from ranklocks where guild_id=$1 and roblox_user_id=$2 and group_id=$3", guild_id, rid, int(group_id))
        return await interaction.response.send_message("✅ Ranklock removed (if it existed).", ephemeral=True)

    if action == "view":
        rows = await db_fetch(
            "select group_id, max_rank_id, reason, set_at from ranklocks where guild_id=$1 and roblox_user_id=$2 order by group_id asc",
            guild_id, rid
        )
        if not rows:
            return await interaction.response.send_message("No ranklocks found.", ephemeral=True)
        lines = [f"• `{r['group_id']}` max **{r['max_rank_id']}** (set {fmt_date(r['set_at'])}) — {r['reason']}" for r in rows]
        return await interaction.response.send_message("\n".join(lines), ephemeral=True)

    return await interaction.response.send_message("Actions: set / remove / view", ephemeral=True)

# =====================================================
# /bgcheck (clean + watched + flags + value estimate)
# =====================================================

@tree.command(name="bgcheck", description="Background check a Roblox account (clean + flagged results)")
@app_commands.describe(
    discord_user="Discord user (RoVer)",
    roblox_id="Roblox userId",
    username="Roblox username",
    show_all="Show every group",
    include_value="Estimate inventory + gamepass value (best-effort; may be slow)"
)
async def bgcheck(
    interaction: discord.Interaction,
    discord_user: discord.Member | None = None,
    roblox_id: str | None = None,
    username: str | None = None,
    show_all: bool = False,
    include_value: bool = False
):
    await interaction.response.defer(ephemeral=True)

    db_ok = await ensure_db()

    # Resolve Roblox ID
    target_roblox_id: int | None = None
    if discord_user:
        target_roblox_id = await asyncio.to_thread(discord_to_roblox, int(interaction.guild_id), int(discord_user.id))
        if not target_roblox_id:
            return await interaction.followup.send("❌ That Discord user is not verified with RoVer.")
    elif roblox_id:
        target_roblox_id = int(roblox_id)
    elif username:
        target_roblox_id = await asyncio.to_thread(username_to_roblox, username)
        if not target_roblox_id:
            return await interaction.followup.send("❌ Roblox username not found.")
    else:
        return await interaction.followup.send("❌ Provide discord_user OR roblox_id OR username.")

    user = await asyncio.to_thread(get_roblox_user, target_roblox_id)
    groups = await asyncio.to_thread(get_user_groups, target_roblox_id)
    groups_sorted = sorted(groups, key=lambda x: (x["group"]["name"] or "").lower())

    watched_map = {}
    blrank_map = {}
    user_blacklist = None
    ranklock_map = {}

    if db_ok:
        watched = await db_fetch("select group_id, label from watched_groups where guild_id=$1", int(interaction.guild_id))
        watched_map = {int(r["group_id"]): (r["label"] or None) for r in watched}

        bl_rows = await db_fetch("select group_id, rank_id, reason from blacklisted_ranks where guild_id=$1", int(interaction.guild_id))
        blrank_map = {(int(r["group_id"]), int(r["rank_id"])): (r["reason"] or None) for r in bl_rows}

        user_blacklist = await db_fetchrow(
            "select reason, added_at from blacklisted_users where guild_id=$1 and roblox_user_id=$2",
            int(interaction.guild_id), target_roblox_id
        )

        rls = await db_fetch(
            "select group_id, max_rank_id, reason, set_at from ranklocks where guild_id=$1 and roblox_user_id=$2",
            int(interaction.guild_id), target_roblox_id
        )
        ranklock_map = {int(r["group_id"]): r for r in rls}

    age_days = account_age_days(user["created"])
    created_date = user["created"][:10]

    notes = []
    if age_days < 7:
        notes.append("Very new account (<7 days)")
    elif age_days < 30:
        notes.append("New-ish account (<30 days)")

    if user_blacklist:
        notes.append(f"🚫 Blacklisted user: {user_blacklist['reason']} (since {fmt_date(user_blacklist['added_at'])})")

    lines = []
    watched_count = 0
    flagged_count = 0

    for g in groups_sorted:
        gid = int(g["group"]["id"])
        gname = g["group"]["name"]
        rank_id = int(g["role"]["rank"])
        role_name = g["role"]["name"]

        is_watched = gid in watched_map
        if is_watched:
            watched_count += 1

        bl_reason = blrank_map.get((gid, rank_id))
        is_blacklisted_rank = bl_reason is not None

        rl = ranklock_map.get(gid)
        rl_txt = ""
        rl_flag = False
        if rl:
            max_rank = int(rl["max_rank_id"])
            rl_flag = rank_id > max_rank
            rl_txt = f" | RL max **{max_rank}** ({'⚠️ exceeds' if rl_flag else 'ok'}) set {fmt_date(rl['set_at'])}"

        should_show = show_all or is_watched or is_blacklisted_rank or rl_flag
        if not should_show:
            continue

        icon = "✅"
        extra = ""
        label = watched_map.get(gid)
        display_name = label or gname

        if is_blacklisted_rank:
            icon = "🚫"
            flagged_count += 1
            extra = f" — **BLACKLIST** ({safe_text(bl_reason or 'rule hit', 80)})"
        elif rl_flag:
            icon = "⚠️"
            flagged_count += 1
            extra = " — **RANKLOCK EXCEEDED**"

        line = f"{icon} **{safe_text(display_name, 42)}** (`{gid}`) — **{rank_id}** ({safe_text(role_name, 40)}){extra}{rl_txt}"
        lines.append(line)

    if not lines and not show_all:
        lines = ["No watched/flagged groups matched. (Use `show_all:true` to display every group.)"]

    chunks = chunk_lines(lines, 1024)

    # --------- Embed ----------
    title_name = f"{user['name']} ({user['displayName']})"
    embed = discord.Embed(
        title="Roblox Background Check",
        description=f"**{safe_text(title_name, 80)}**\nID: `{target_roblox_id}`",
        color=0x2f3136
    )

    embed.add_field(
        name="Account",
        value=f"Created: **{created_date}**\nAge: **{age_days} days**",
        inline=True
    )
    embed.add_field(
        name="Summary",
        value=f"Watched in: **{watched_count}**\nFlags: **{flagged_count}**\nTotal groups: **{len(groups_sorted)}**",
        inline=True
    )

    if not db_ok:
        embed.add_field(
            name="DB Status",
            value="⚠️ Database not connected — watched/blacklist/ranklock features disabled.",
            inline=False
        )

    if notes:
        embed.add_field(name="Notes", value="\n".join([f"• {safe_text(n, 200)}" for n in notes])[:1024], inline=False)

    # --------- Inventory / value estimate ----------
    if include_value:
        # Keep pricing calls reasonable for public endpoints
        est = await compute_value_estimate(target_roblox_id, max_assets_to_price=120)

        # counts summary
        if est["inventory_private"]:
            inv_line = "Inventory: **Private/Blocked**"
        else:
            total_items = sum(est["type_counts"].values())
            top_types = sorted(est["type_counts"].items(), key=lambda kv: kv[1], reverse=True)[:4]
            top_txt = ", ".join([f"{k}: {v}" for k, v in top_types if v > 0]) or "no items found"
            inv_line = f"Items checked: **{total_items}** (top: {safe_text(top_txt, 120)})"

        # value
        val_line = "Est. catalog value: **N/A**"
        if est["est_value_robux"] is not None:
            val_line = f"Est. catalog value (sampled): **{est['est_value_robux']:,} R$**  *(priced {est['priced_assets']} items)*"

        # gamepasses
        gp_line = "Owned gamepasses: **N/A**"
        if est["gamepasses_count"] is not None:
            gp_line = f"Owned gamepasses: **{est['gamepasses_count']}**"
            if est["gamepasses_value_robux"] is not None:
                gp_line += f" | Est. value (sampled): **{est['gamepasses_value_robux']:,} R$** *(priced {est['gamepasses_priced']} passes)*"

        embed.add_field(
            name="Value Estimate (Best-Effort)",
            value=f"{inv_line}\n{val_line}\n{gp_line}",
            inline=False
        )

        if est["notes"]:
            embed.add_field(
                name="Value Notes",
                value="\n".join([f"• {safe_text(n, 200)}" for n in est["notes"]])[:1024],
                inline=False
            )

    # Groups section
    for i, chunk in enumerate(chunks[:20]):
        embed.add_field(name="Groups" if i == 0 else f"Groups (cont. {i})", value=chunk, inline=False)

    embed.set_footer(text=f"Checked by {interaction.user}")
    await interaction.followup.send(embed=embed)

# =====================================================
# Startup
# =====================================================

@client.event
async def on_ready():
    await ensure_db()
    await tree.sync()
    print(f"✅ Logged in as {client.user}")

client.run(DISCORD_TOKEN)
