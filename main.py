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
# HTTP wrapper (Roblox is picky on cloud hosts)
# =====================================================

ROBLOX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BGCheckBot/1.0; +https://discord.com)",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

def http_get(url: str, *, params=None, timeout=20, retries=3):
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=ROBLOX_HEADERS, timeout=timeout)
            # Roblox rate limiting
            if r.status_code == 429:
                wait = min(2.0 * (attempt + 1), 6.0)
                time.sleep(wait)
                last = r
                continue
            return r
        except Exception as e:
            last = e
            time.sleep(min(1.0 * (attempt + 1), 3.0))
    return last

def http_post(url: str, *, json=None, timeout=20, retries=3):
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(url, json=json, headers=ROBLOX_HEADERS, timeout=timeout)
            if r.status_code == 429:
                wait = min(2.0 * (attempt + 1), 6.0)
                time.sleep(wait)
                last = r
                continue
            return r
        except Exception as e:
            last = e
            time.sleep(min(1.0 * (attempt + 1), 3.0))
    return last

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

def username_to_roblox(username: str) -> int | None:
    r = http_post(
        "https://users.roblox.com/v1/usernames/users",
        json={"usernames": [username], "excludeBannedUsers": False},
        timeout=15,
        retries=3
    )
    if isinstance(r, Exception) or r.status_code != 200:
        return None
    data = r.json()
    if not data.get("data"):
        return None
    return int(data["data"][0]["id"])

def get_roblox_user(user_id: int) -> dict:
    r = http_get(f"https://users.roblox.com/v1/users/{user_id}", timeout=15, retries=3)
    if isinstance(r, Exception) or r.status_code != 200:
        raise RuntimeError(f"Roblox users API failed: {getattr(r, 'status_code', r)}")
    return r.json()

def get_user_groups(user_id: int) -> list:
    r = http_get(f"https://groups.roblox.com/v2/users/{user_id}/groups/roles", timeout=20, retries=3)
    if isinstance(r, Exception) or r.status_code != 200:
        return []
    return r.json().get("data", [])

def account_age_days(created_iso: str) -> int:
    created_dt = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - created_dt).days

# =====================================================
# Inventory + Value estimation (best-effort)
# =====================================================

# Keep this list conservative; Roblox inventory endpoints vary by account/privacy.
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

def _inv_fetch_asset_type(user_id: int, asset_type_id: int, limit_pages: int = 2, page_size: int = 100):
    """
    Returns (asset_ids, status_code)
    status_code is the HTTP code from the last request (or None if exception).
    """
    asset_ids: list[int] = []
    cursor = ""
    status_code = None
    seen = set()

    for _ in range(limit_pages):
        url = f"https://inventory.roblox.com/v2/users/{user_id}/inventory/{asset_type_id}"
        params = {"limit": page_size}
        if cursor:
            params["cursor"] = cursor

        r = http_get(url, params=params, timeout=20, retries=3)
        if isinstance(r, Exception):
            return asset_ids, None

        status_code = r.status_code

        if r.status_code in (401, 403):
            # inventory privacy or blocked
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

def _economy_asset_price(asset_id: int):
    url = f"https://economy.roblox.com/v2/assets/{asset_id}/details"
    r = http_get(url, timeout=15, retries=2)
    if isinstance(r, Exception) or r.status_code != 200:
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
        asset_ids, status = await asyncio.to_thread(_inv_fetch_asset_type, user_id, asset_type_id, 2, 100)
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
                price = await asyncio.to_thread(_economy_asset_price, aid)
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
# Commands (same core as before; only /bgcheck changed)
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


@tree.command(name="bgcheck", description="Background check a Roblox account (clean + flagged results)")
@app_commands.describe(
    discord_user="Discord user (RoVer)",
    roblox_id="Roblox userId",
    username="Roblox username",
    show_all="Show every group",
    include_value="Estimate inventory value (best-effort; may be slow)"
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

    # Minimal DB features (kept short; you already had more earlier — add back if you want)
    watched_map = {}
    if db_ok:
        watched = await db_fetch("select group_id, label from watched_groups where guild_id=$1", int(interaction.guild_id))
        watched_map = {int(r["group_id"]): (r["label"] or None) for r in watched}

    # Summary + groups view
    age_days = account_age_days(user["created"])
    created_date = user["created"][:10]

    lines = []
    watched_count = 0

    for g in groups_sorted:
        gid = int(g["group"]["id"])
        gname = g["group"]["name"]
        rank_id = int(g["role"]["rank"])
        role_name = g["role"]["name"]

        is_watched = gid in watched_map
        if is_watched:
            watched_count += 1

        should_show = show_all or is_watched
        if not should_show:
            continue

        label = watched_map.get(gid)
        display_name = label or gname
        lines.append(f"✅ **{safe_text(display_name, 42)}** (`{gid}`) — **{rank_id}** ({safe_text(role_name, 40)})")

    if not lines and not show_all:
        lines = ["No watched groups matched. (Use `show_all:true` to display every group.)"]

    chunks = chunk_lines(lines, 1024)

    title_name = f"{user['name']} ({user['displayName']})"
    embed = discord.Embed(
        title="Roblox Background Check",
        description=f"**{safe_text(title_name, 80)}**\nID: `{target_roblox_id}`",
        color=0x2f3136
    )
    embed.add_field(name="Account", value=f"Created: **{created_date}**\nAge: **{age_days} days**", inline=True)
    embed.add_field(name="Summary", value=f"Watched in: **{watched_count}**\nTotal groups: **{len(groups_sorted)}**", inline=True)

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

        # Debug status map so you can see what's failing on Railway
        status_bits = []
        for label, code in est["status_map"].items():
            if code is None:
                status_bits.append(f"{label}=ERR")
            else:
                status_bits.append(f"{label}={code}")
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

        embed.add_field(
            name="If this shows 403",
            value="On Roblox, set **Privacy → Inventory → Everyone** (not Friends). If it’s already Everyone, Roblox may be blocking cloud hosts; we can add a proxy/fallback endpoint next.",
            inline=False
        )

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
