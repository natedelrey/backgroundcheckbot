import os
import re
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

# =====================================================
# HTTP wrapper (async, non-blocking retries)
# =====================================================

ROBLOX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BGCheckBot/1.0; +https://discord.com)",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

async def http_get(url: str, *, params=None, timeout=20, retries=3):
    last_exc = None
    for attempt in range(retries):
        try:
            r = await asyncio.to_thread(
                requests.get,
                url,
                params,
                ROBLOX_HEADERS,
                timeout
            )
            # The call above doesn't map named args; do it properly:
        except TypeError:
            # fallback: use lambda to preserve named args
            try:
                r = await asyncio.to_thread(lambda: requests.get(url, params=params, headers=ROBLOX_HEADERS, timeout=timeout))
            except Exception as e:
                last_exc = e
                await asyncio.sleep(min(1.0 * (attempt + 1), 3.0))
                continue

        if hasattr(r, "status_code") and r.status_code == 429:
            await asyncio.sleep(min(2.0 * (attempt + 1), 6.0))
            continue

        return r

    return last_exc

async def http_post(url: str, *, json=None, timeout=20, retries=3):
    last_exc = None
    for attempt in range(retries):
        try:
            r = await asyncio.to_thread(lambda: requests.post(url, json=json, headers=ROBLOX_HEADERS, timeout=timeout))
        except Exception as e:
            last_exc = e
            await asyncio.sleep(min(1.0 * (attempt + 1), 3.0))
            continue

        if hasattr(r, "status_code") and r.status_code == 429:
            await asyncio.sleep(min(2.0 * (attempt + 1), 6.0))
            continue

        return r

    return last_exc

# =====================================================
# Roblox / RoVer helpers
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

async def username_to_roblox(username: str) -> int | None:
    r = await http_post(
        "https://users.roblox.com/v1/usernames/users",
        json={"usernames": [username], "excludeBannedUsers": False},
        timeout=15,
        retries=3
    )
    if isinstance(r, Exception) or getattr(r, "status_code", None) != 200:
        return None
    data = r.json()
    if not data.get("data"):
        return None
    return int(data["data"][0]["id"])

async def get_roblox_user(user_id: int) -> dict:
    r = await http_get(f"https://users.roblox.com/v1/users/{user_id}", timeout=15, retries=3)
    if isinstance(r, Exception) or getattr(r, "status_code", None) != 200:
        raise RuntimeError(f"Roblox users API failed: {getattr(r, 'status_code', r)}")
    return r.json()

async def get_user_groups(user_id: int) -> list:
    r = await http_get(f"https://groups.roblox.com/v2/users/{user_id}/groups/roles", timeout=20, retries=3)
    if isinstance(r, Exception) or getattr(r, "status_code", None) != 200:
        return []
    return r.json().get("data", [])

def account_age_days(created_iso: str) -> int:
    created_dt = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - created_dt).days

# =====================================================
# Inventory + Value estimation (best-effort)
# =====================================================

ASSET_TYPES = [
    (8,  "Hats"),
    (41, "Hair"),
    (42, "Face Acc"),
    (43, "Neck Acc"),
    (44, "Shoulder Acc"),
    (45, "Front Acc"),
    (46, "Back Acc"),
    (47, "Waist Acc"),
    (2,  "T-Shirts"),
    (11, "Shirts"),
    (12, "Pants"),
    (18, "Faces"),
]

async def inv_fetch_asset_type(user_id: int, asset_type_id: int, limit_pages: int = 2, page_size: int = 100):
    asset_ids: list[int] = []
    cursor = ""
    status_code = None
    seen = set()

    for _ in range(limit_pages):
        url = f"https://inventory.roblox.com/v2/users/{user_id}/inventory/{asset_type_id}"
        params = {"limit": page_size}
        if cursor:
            params["cursor"] = cursor

        r = await http_get(url, params=params, timeout=20, retries=3)
        if isinstance(r, Exception):
            return asset_ids, None

        status_code = r.status_code

        if r.status_code in (401, 403):
            return None, r.status_code
        if r.status_code != 200:
            return asset_ids, r.status_code

        data = r.json()
        items = data.get("data") or []
        for it in items:
            aid = it.get("assetId") or it.get("id")
            if isinstance(aid, int):
                asset_ids.append(aid)

        cursor = data.get("nextPageCursor")
        if not cursor:
            break
        if cursor in seen:
            break
        seen.add(cursor)

    return asset_ids, status_code

async def economy_asset_price(asset_id: int):
    url = f"https://economy.roblox.com/v2/assets/{asset_id}/details"
    r = await http_get(url, timeout=15, retries=2)
    if isinstance(r, Exception) or getattr(r, "status_code", None) != 200:
        return None
    data = r.json()
    price = data.get("price")
    if isinstance(price, (int, float)) and price > 0:
        return int(price)
    return None

async def compute_value_estimate(user_id: int, max_assets_to_price: int = 120):
    max_assets_to_price = max(30, min(300, int(max_assets_to_price)))

    notes: list[str] = []
    type_counts: dict[str, int] = {}
    all_assets: list[int] = []
    status_map: dict[str, int | None] = {}

    inventory_private = False

    for asset_type_id, label in ASSET_TYPES:
        asset_ids, status = await inv_fetch_asset_type(user_id, asset_type_id, 2, 100)
        status_map[label] = status

        if asset_ids is None:
            inventory_private = True
            break

        type_counts[label] = len(asset_ids)
        all_assets.extend(asset_ids)

    est_value = None
    priced_assets = 0

    if inventory_private:
        notes.append("Inventory lookup returned 403/401 (privacy or Roblox blocking this host).")
    else:
        uniq_assets = list(dict.fromkeys(all_assets))
        if not uniq_assets:
            notes.append("No items found in checked categories (or Roblox returned empty lists).")
        else:
            uniq_assets = uniq_assets[:max_assets_to_price]
            total = 0
            for aid in uniq_assets:
                price = await economy_asset_price(aid)
                if price is not None:
                    total += price
                    priced_assets += 1
            if priced_assets > 0:
                est_value = total
            else:
                notes.append("Could not read prices for sampled items (offsale/limited/no public price).")

    return {
        "inventory_private": inventory_private,
        "type_counts": type_counts,
        "priced_assets": priced_assets,
        "est_value_robux": est_value,
        "notes": notes,
        "status_map": status_map
    }

# =====================================================
# Commands
# =====================================================

@tree.command(name="bgcheck", description="Background check a Roblox account (clean + value estimate)")
@app_commands.describe(
    discord_user="Discord user (RoVer)",
    roblox_id="Roblox userId",
    username="Roblox username",
    include_value="Estimate inventory value (best-effort; may be slow)"
)
async def bgcheck(
    interaction: discord.Interaction,
    discord_user: discord.Member | None = None,
    roblox_id: str | None = None,
    username: str | None = None,
    include_value: bool = False
):
    await interaction.response.defer(ephemeral=True)

    # Resolve Roblox ID
    target_roblox_id: int | None = None
    if discord_user:
        target_roblox_id = await asyncio.to_thread(discord_to_roblox, int(interaction.guild_id), int(discord_user.id))
        if not target_roblox_id:
            return await interaction.followup.send("❌ That Discord user is not verified with RoVer.")
    elif roblox_id:
        target_roblox_id = int(roblox_id)
    elif username:
        target_roblox_id = await username_to_roblox(username)
        if not target_roblox_id:
            return await interaction.followup.send("❌ Roblox username not found.")
    else:
        return await interaction.followup.send("❌ Provide discord_user OR roblox_id OR username.")

    user = await get_roblox_user(target_roblox_id)
    age_days = account_age_days(user["created"])
    created_date = user["created"][:10]

    title_name = f"{user['name']} ({user['displayName']})"
    embed = discord.Embed(
        title="Roblox Background Check",
        description=f"**{safe_text(title_name, 80)}**\nID: `{target_roblox_id}`",
        color=0x2f3136
    )
    embed.add_field(name="Account", value=f"Created: **{created_date}**\nAge: **{age_days} days**", inline=True)

    if include_value:
        est = await compute_value_estimate(target_roblox_id, 120)

        if est["inventory_private"]:
            inv_line = "Inventory: **Private/Blocked**"
        else:
            total_items = sum(est["type_counts"].values())
            top_types = sorted(est["type_counts"].items(), key=lambda kv: kv[1], reverse=True)[:5]
            top_txt = ", ".join([f"{k}: {v}" for k, v in top_types if v > 0]) or "no items found"
            inv_line = f"Items checked: **{total_items}** (top: {safe_text(top_txt, 140)})"

        val_line = "Est. catalog value: **N/A**"
        if est["est_value_robux"] is not None:
            val_line = f"Est. catalog value (sampled): **{est['est_value_robux']:,} R$** *(priced {est['priced_assets']} items)*"

        status_bits = []
        for label, code in est["status_map"].items():
            status_bits.append(f"{label}={code if code is not None else 'ERR'}")
        status_line = safe_text(" | ".join(status_bits), 900)

        embed.add_field(
            name="Value Estimate (Best-Effort)",
            value=f"{inv_line}\n{val_line}\n`{status_line}`",
            inline=False
        )

        if est["notes"]:
            embed.add_field(
                name="Value Notes",
                value="\n".join([f"• {safe_text(n, 200)}" for n in est["notes"]])[:1024],
                inline=False
            )

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
