"""
Paul's First Discord Bot
------------------------
Commands:
  /hello            - bot says hi
  /roll [sides]     - roll a die (default 6 sides)
  /poll [question]  - posts a yes/no poll with 👍 👎
  /serverinfo       - shows info about the server
  /newkit           - announce a new Diamond Nook kit (admins only)
  /addkit           - add a new kit to the Diamond Nook WEBSITE, then announce it (admins only)
  /orders           - show the latest paid store orders (admins only, only you see it)
Also: posts every new paid store order in the #orders channel (checks once a minute).
  /gshopping        - Google Shopping health check: approved / pending / disapproved + problems
Google Shopping watchdog: checks daily and posts problems in #store-alerts.
  /sharekit         - post a kit from the website to Facebook/Instagram now
  /salesreport      - sales summary for a week/month/year, with a CSV for your books
Weekly sales summary: every Monday morning in #orders.
Personal budget (talks to your Google Sheet; replies are private):
  /spent            - log a purchase   /budget - this month so far   /bills - bills due soon
  /income           - log a paycheck   /balance - update an account balance
  /grocery ...      - shopping list, staples, meal planner   /price ... - price tracker
  /career ...       - career roadmap: Security+ and freelancing
  /quiz             - a Security+ practice question (one is also posted daily in #study)
  /quizscore        - your practice score by domain, streak and weakest area
  /practicetest ... - log Udemy/full-length practice test scores and track progress
  /exam             - practice exam: 10, 20 or 40 questions with a final score (style: Scenario only)
  /pantry ...       - pantry inventory (low stock goes on the grocery list)
  /trip ...         - vacation planner (savings, countdown, flights, hotels & bookings, itinerary, quotes)   /pack ... - packing list
Sold-out cleanup: when a kit sells out, it's marked sold, pulled from Google Shopping, and a "Just sold!" post goes to #new-kits.
Social poster: new kits published on the website are posted to Facebook + Instagram automatically.
Listing helper: drop kit photos in #add-listing -> StoltBot cleans them up -> click a button -> draft product.
Also: welcomes new members in the #general channel.

Setup:
  1. Put your bot token in a file named token.txt in the SAME folder as this file.
  2. pip install discord.py
  3. python discord_bot.py
"""

import random
import pathlib
import re
from typing import Optional
import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
import json
import io
import csv
import asyncio
from zoneinfo import ZoneInfo
import datetime
from PIL import Image, ImageOps, ImageChops
import os

# On Railway, keys come from Variables instead of .txt files
HERE = pathlib.Path(__file__).parent
env = os.environ.get
if env("DISCORD_TOKEN"):
    (HERE / "token.txt").write_text(env("DISCORD_TOKEN"), encoding="utf-8")
if env("WOO_KEY") and env("WOO_SECRET"):
    (HERE / "woo.txt").write_text(f"{env('WOO_KEY')}\n{env('WOO_SECRET')}", encoding="utf-8")
if env("BUDGET_URL") and env("BUDGET_SECRET"):
    (HERE / "budget.txt").write_text(f"URL={env('BUDGET_URL')}\nSECRET={env('BUDGET_SECRET')}", encoding="utf-8")
if env("META_PAGE_ID") and env("META_PAGE_TOKEN"):
    (HERE / "meta.txt").write_text(
        f"PAGE_ID={env('META_PAGE_ID')}\nPAGE_TOKEN={env('META_PAGE_TOKEN')}\nIG_USER_ID={env('META_IG_USER_ID', '')}",
        encoding="utf-8")
        
# ===== SETTINGS =====
WELCOME_CHANNEL_NAME = "general"

# Diamond Nook announcer
ANNOUNCE_CHANNEL_NAME = "new-kits"           # posts here if it exists, otherwise in the channel you use the command in
STORE_URL = "https://thediamondnook.shop"    # used when you don't give a product link
STORE_NAME = "Dena's Diamond Nook"
EMBED_COLOR = discord.Color.from_rgb(155, 89, 182)  # sparkly purple

# WooCommerce website connection (for /addkit)
# Put your WooCommerce REST API keys in woo.txt (same folder):
#   line 1: consumer key    (starts with ck_)
#   line 2: consumer secret (starts with cs_)
WOO_FILE = "woo.txt"


def load_woo_keys():
    path = pathlib.Path(__file__).with_name(WOO_FILE)
    if not path.exists():
        return None
    lines = [re.sub(r"[^A-Za-z0-9_]", "", l) for l in path.read_text(encoding="utf-8-sig").splitlines() if l.strip()]
    if len(lines) < 2 or not lines[0].startswith("ck_") or not lines[1].startswith("cs_"):
        print("⚠️ woo.txt should have the ck_ key on line 1 and the cs_ secret on line 2. /addkit is disabled.")
        return None
    print("🛒 WooCommerce keys loaded")
    return lines[0], lines[1]


WOO_KEYS = load_woo_keys()


def woo_auth_header():
    """Login header for the WooCommerce API (made from the woo.txt keys)."""
    import base64
    key, secret = WOO_KEYS
    return {"Authorization": "Basic " + base64.b64encode(f"{key}:{secret}".encode()).decode()}


def clean_price(price: str) -> Optional[str]:
    """Turn '$24.99' or '24' into '24.99' / '24.00'. Returns None if it isn't a number."""
    try:
        return f"{float(price.replace('$', '').replace(',', '').strip()):.2f}"
    except ValueError:
        return None


async def build_announcement(name, price, url, details, image: Optional[discord.Attachment], category=None):
    embed = discord.Embed(
        title=f"✨ {name}",
        url=url,
        description=details or "A brand-new diamond art kit is in the shop!",
        color=EMBED_COLOR,
    )
    embed.add_field(name="Price", value=f"${price}")
    if category:
        embed.add_field(name="Category", value=category)
    embed.add_field(name="Shop", value=f"[Get it here]({url})")
    embed.set_footer(text=STORE_NAME)
    files = []
    if image:
        photo = await image.to_file()  # re-upload so the picture never expires
        embed.set_image(url=f"attachment://{photo.filename}")
        files.append(photo)
    return embed, files


async def post_announcement(interaction, embed, files):
    """Posts in #new-kits (or the current channel). Returns the channel, or None if not allowed."""
    channel = discord.utils.get(interaction.guild.text_channels, name=ANNOUNCE_CHANNEL_NAME) or interaction.channel
    try:
        await channel.send(content="💎 **New diamond art kit just dropped!**", embed=embed, files=files)
        return channel
    except discord.Forbidden:
        return None


async def create_woo_product(name, price, details, image_url, publish, category_id=None, extra_images=()):
    """Creates a one-of-a-kind (stock 1) product on the website. Returns the product data."""
    key, secret = WOO_KEYS
    payload = {
        "name": name,
        "type": "simple",
        "status": "publish" if publish else "draft",
        "regular_price": price,
        "description": details or "",
        "manage_stock": True,
        "stock_quantity": 1,
        "images": [{"src": image_url}] + [{"src": u} for u in extra_images],
    }
    if category_id:
        payload["categories"] = [{"id": category_id}]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        async with session.post(
            f"{STORE_URL}/wp-json/wc/v3/products",
            json=payload,
            headers=woo_auth_header(),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 300:
                message = data.get("message") if isinstance(data, dict) else str(data)
                raise RuntimeError(f"{resp.status}: {message}")
            return data


_category_cache = {"time": 0, "items": []}


async def get_woo_categories():
    """Returns [(id, name), ...] from the website. Cached for 5 minutes."""
    import time
    if WOO_KEYS is None:
        return []
    if time.time() - _category_cache["time"] < 300 and _category_cache["items"]:
        return _category_cache["items"]
    key, secret = WOO_KEYS
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.get(
            f"{STORE_URL}/wp-json/wc/v3/products/categories",
            params={"per_page": 100, "hide_empty": "false"},
            headers=woo_auth_header(),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 300 or not isinstance(data, list):
                print(f"⚠️ Couldn't load categories: {resp.status}")
                return _category_cache["items"]
    items = [(c["id"], c["name"]) for c in data if c.get("slug") != "uncategorized"]
    _category_cache.update(time=time.time(), items=items)
    return items


async def find_category(text: str):
    """Match what was typed/picked to a category. Returns (id, name) or None."""
    cats = await get_woo_categories()
    text = text.strip()
    for cid, name in cats:
        if text == str(cid) or text.lower() == name.lower():
            return cid, name
    return None


# ===== ORDER ALERTS =====
ORDERS_CHANNEL_NAME = "orders"          # make a private #orders channel; StoltBot posts new orders there
PAID_STATUSES = "processing,on-hold,completed"   # unpaid/abandoned checkouts (pending, failed) are skipped
ORDER_STATE_FILE = pathlib.Path(__file__).with_name("orders_seen.json")


async def fetch_orders(count=20):
    key, secret = WOO_KEYS
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.get(
            f"{STORE_URL}/wp-json/wc/v3/orders",
            params={"per_page": count, "orderby": "id", "order": "desc", "status": PAID_STATUSES},
            headers=woo_auth_header(),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 300 or not isinstance(data, list):
                message = data.get("message") if isinstance(data, dict) else resp.status
                raise RuntimeError(f"{resp.status}: {message}")
            return data


def load_order_state():
    try:
        return json.loads(ORDER_STATE_FILE.read_text())
    except Exception:
        return None


def save_order_state(state):
    state["posted"] = state["posted"][-300:]
    ORDER_STATE_FILE.write_text(json.dumps(state))


def order_embed(order):
    items = "\n".join(
        f"• {i['quantity']} × {i['name']} — ${float(i['total']):.2f}" for i in order.get("line_items", [])
    ) or "—"
    billing = order.get("billing", {})
    ship = order.get("shipping", {})
    city = ship.get("city") or billing.get("city") or ""
    state = ship.get("state") or billing.get("state") or ""
    customer = f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip() or "Guest"
    embed = discord.Embed(
        title=f"🛒 New order #{order.get('number', order['id'])}",
        url=f"{STORE_URL}/wp-admin/admin.php?page=wc-orders&action=edit&id={order['id']}",
        description=items,
        color=discord.Color.green(),
    )
    embed.add_field(name="Total", value=f"${float(order.get('total', 0)):.2f}")
    embed.add_field(name="Shipping", value=f"${float(order.get('shipping_total', 0)):.2f}")
    embed.add_field(name="Sales tax", value=f"${float(order.get('total_tax', 0)):.2f}")
    embed.add_field(name="Customer", value=customer)
    embed.add_field(name="Ship to", value=", ".join(x for x in (city, state) if x) or "—")
    embed.add_field(name="Status", value=order.get("status", "").replace("-", " ").title())
    embed.set_footer(text=f"{STORE_NAME} · click the title to open the order")
    return embed


@tasks.loop(seconds=60)
async def check_new_orders():
    if WOO_KEYS is None:
        return
    try:
        orders = await fetch_orders()
    except Exception as e:
        print(f"⚠️ Order check failed: {e}")
        return

    state = load_order_state()
    if state is None:
        # First run: remember what already exists so old orders aren't posted
        newest = max((o["id"] for o in orders), default=0)
        save_order_state({"baseline": newest, "posted": []})
        print(f"🛒 Order alerts ready (ignoring orders up to #{newest})")
        return

    new = [o for o in orders if o["id"] > state["baseline"] and o["id"] not in state["posted"]]
    if not new:
        return

    channels = [c for g in bot.guilds for c in g.text_channels if c.name == ORDERS_CHANNEL_NAME]
    if not channels:
        print(f"⚠️ {len(new)} new order(s) but no #{ORDERS_CHANNEL_NAME} channel to post in")
        return

    for order in sorted(new, key=lambda o: o["id"]):
        for channel in channels:
            try:
                await channel.send(content="💰 **Cha-ching! New order!**", embed=order_embed(order))
            except discord.Forbidden:
                print(f"⚠️ No permission to post in #{channel.name}")
        state["posted"].append(order["id"])
        print(f"🛒 Posted order #{order['id']}")
        save_order_state(state)
        for item in order.get("line_items", []):
            try:
                await handle_sold_item(item.get("product_id"))
            except Exception as e:
                print(f"⚠️ Sold-out cleanup failed for {item.get('name')}: {e}")
    save_order_state(state)


@check_new_orders.before_loop
async def before_order_check():
    await bot.wait_until_ready()


# ===== GOOGLE SHOPPING WATCHDOG =====
ALERTS_CHANNEL_NAME = "store-alerts"
WATCHDOG_TIME = datetime.time(hour=14, minute=0, tzinfo=datetime.timezone.utc)  # 9 AM Eastern in winter, 10 AM in summer


async def gla_get(path, params=None):
    """Read from the Google for WooCommerce plugin on the website."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.get(
            f"{STORE_URL}/wp-json/wc/gla/{path}", params=params or {}, headers=woo_auth_header()
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 300:
                message = data.get("message") if isinstance(data, dict) else resp.status
                raise RuntimeError(f"{resp.status}: {message}")
            return data


async def google_shopping_report():
    """Returns (embed, has_problems)."""
    stats = await gla_get("mc/product-statistics")
    issues = await gla_get("mc/issues/product", {"per_page": 50})
    s = (stats or {}).get("statistics") or {}
    errors = [i for i in issues.get("issues", []) if i.get("severity") == "error"]
    warnings = [i for i in issues.get("issues", []) if i.get("severity") != "error"]
    bad = int(s.get("disapproved") or 0) > 0 or bool(errors)

    embed = discord.Embed(
        title="🛍️ Google Shopping check" + (" — needs attention" if bad else " — all good"),
        url=f"{STORE_URL}/wp-admin/admin.php?page=wc-admin&path=/google/product-feed",
        color=discord.Color.red() if bad else discord.Color.green(),
    )
    for label, key, icon in (("Approved", "active", "✅"), ("Pending", "pending", "⏳"),
                             ("Expiring", "expiring", "⌛"), ("Disapproved", "disapproved", "❌"),
                             ("Not synced", "not_synced", "🚫")):
        embed.add_field(name=f"{icon} {label}", value=str(s.get(key, 0)))

    def lines(items, limit):
        out = []
        for i in items[:limit]:
            fix = f" → {i['action']}" if i.get("action") else ""
            out.append(f"• **{i.get('product', 'Store')}**: {i.get('issue', '?')}{fix}")
        if len(items) > limit:
            out.append(f"…and {len(items) - limit} more")
        return "\n".join(out)[:1024]

    if errors:
        embed.add_field(name=f"❌ Problems to fix ({len(errors)})", value=lines(errors, 8), inline=False)
    if warnings:
        embed.add_field(name=f"⚠️ Warnings ({len(warnings)})", value=lines(warnings, 4), inline=False)
    if stats.get("loading") or issues.get("loading"):
        embed.set_footer(text="Google is still processing some products. Numbers may change.")
    else:
        embed.set_footer(text="Click the title to open the product feed in WordPress")
    return embed, bad


@tasks.loop(time=WATCHDOG_TIME)
async def google_shopping_watchdog():
    if WOO_KEYS is None:
        return
    try:
        embed, bad = await google_shopping_report()
    except Exception as e:
        print(f"⚠️ Google Shopping check failed: {e}")
        return
    print("🛍️ Google Shopping check: " + ("problems found" if bad else "all good"))
    if not bad:
        return  # stay quiet when everything is fine
    for channel in [c for g in bot.guilds for c in g.text_channels if c.name == ALERTS_CHANNEL_NAME]:
        try:
            await channel.send(content="🚨 **Google Shopping found problems**", embed=embed)
        except discord.Forbidden:
            print(f"⚠️ No permission to post in #{channel.name}")


@google_shopping_watchdog.before_loop
async def before_watchdog():
    await bot.wait_until_ready()


# ===== SOCIAL MEDIA POSTER =====
# Put your Meta keys in meta.txt (same folder), one per line:
#   PAGE_ID=1234567890
#   PAGE_TOKEN=EAAG...            (long-lived Page access token)
#   IG_USER_ID=1784...            (optional: Instagram Business account ID)
META_FILE = "meta.txt"
GRAPH = "https://graph.facebook.com/v25.0"
SOCIAL_STATE_FILE = pathlib.Path(__file__).with_name("social_seen.json")
HASHTAGS = "#diamondart #diamondpainting #handmade #oneofakind #giftideas #DenasDiamondNook"


def load_meta():
    path = pathlib.Path(__file__).with_name(META_FILE)
    if not path.exists():
        return None
    keys = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            keys[k.strip().upper()] = re.sub(r"\s", "", v)
    if not keys.get("PAGE_ID") or not keys.get("PAGE_TOKEN"):
        print("⚠️ meta.txt needs PAGE_ID= and PAGE_TOKEN= lines. Social posting is off.")
        return None
    print("📣 Meta keys loaded" + (" (Facebook + Instagram)" if keys.get("IG_USER_ID") else " (Facebook only)"))
    return keys


META = load_meta()


def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def social_caption(product, for_instagram=False):
    price = product.get("price") or product.get("regular_price") or ""
    blurb = strip_html(product.get("short_description") or product.get("description"))[:300]
    lines = [f"✨ New in the shop: {product['name']} ✨"]
    if blurb:
        lines.append(blurb)
    if price:
        lines.append(f"💎 ${float(price):.2f} · one of a kind, only 1 available!")
    lines.append("🛒 Shop now: link in bio" if for_instagram else f"🛒 Shop now: {product.get('permalink', STORE_URL)}")
    lines.append(HASHTAGS)
    return "\n\n".join(lines)


async def graph_post(session, path, data):
    async with session.post(f"{GRAPH}/{path}", data={**data, "access_token": META["PAGE_TOKEN"]}) as resp:
        out = await resp.json(content_type=None)
        if resp.status >= 300 or "error" in out:
            raise RuntimeError((out.get("error") or {}).get("message", f"HTTP {resp.status}"))
        return out


async def post_to_socials(product):
    """Posts one product. Returns a list of result lines."""
    images = product.get("images") or []
    if not images:
        return ["⚠️ Skipped: the product has no photo"]
    image_url = images[0]["src"]
    results = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        try:
            await graph_post(session, f"{META['PAGE_ID']}/photos",
                             {"url": image_url, "caption": social_caption(product)})
            results.append("✅ Facebook")
        except Exception as e:
            results.append(f"❌ Facebook: {e}")

        ig = META.get("IG_USER_ID")
        if ig:
            try:
                box = await graph_post(session, f"{ig}/media",
                                       {"image_url": image_url, "caption": social_caption(product, True)})
                for _ in range(10):  # wait for Instagram to finish processing the photo
                    async with session.get(f"{GRAPH}/{box['id']}",
                                           params={"fields": "status_code", "access_token": META["PAGE_TOKEN"]}) as r:
                        if (await r.json(content_type=None)).get("status_code") == "FINISHED":
                            break
                    await asyncio.sleep(3)
                await graph_post(session, f"{ig}/media_publish", {"creation_id": box["id"]})
                results.append("✅ Instagram")
            except Exception as e:
                results.append(f"❌ Instagram: {e}")
    return results


async def fetch_published_products(count=10, search=None):
    params = {"per_page": count, "orderby": "id", "order": "desc", "status": "publish"}
    if search:
        params["search"] = search
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.get(f"{STORE_URL}/wp-json/wc/v3/products", params=params, headers=woo_auth_header()) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 300 or not isinstance(data, list):
                raise RuntimeError(f"{resp.status}: {data.get('message') if isinstance(data, dict) else data}")
            return data


async def alert(text):
    for channel in [c for g in bot.guilds for c in g.text_channels if c.name == ALERTS_CHANNEL_NAME]:
        try:
            await channel.send(text)
        except discord.Forbidden:
            pass


@tasks.loop(minutes=5)
async def social_poster():
    if WOO_KEYS is None or META is None:
        return
    try:
        products = await fetch_published_products()
    except Exception as e:
        print(f"⚠️ Social poster couldn't read products: {e}")
        return
    try:
        state = json.loads(SOCIAL_STATE_FILE.read_text())
    except Exception:
        newest = max((p["id"] for p in products), default=0)
        SOCIAL_STATE_FILE.write_text(json.dumps({"baseline": newest, "posted": []}))
        print(f"📣 Social poster ready (skipping kits already in the shop, up to #{newest})")
        return
    new = [p for p in products if p["id"] > state["baseline"] and p["id"] not in state["posted"]]
    for product in sorted(new, key=lambda p: p["id"]):
        results = await post_to_socials(product)
        state["posted"].append(product["id"])
        state["posted"] = state["posted"][-300:]
        SOCIAL_STATE_FILE.write_text(json.dumps(state))
        print(f"📣 Posted {product['name']}: {', '.join(results)}")
        await alert(f"📣 Shared **{product['name']}** on social media: " + " · ".join(results))


@social_poster.before_loop
async def before_social():
    await bot.wait_until_ready()


# ===== SALES SUMMARY =====
EASTERN = ZoneInfo("America/New_York")
REPORT_CHANNEL_NAME = "orders"           # private channel, since it has customer names
REPORT_TIME = datetime.time(hour=9, minute=0, tzinfo=EASTERN)   # Mondays 9 AM


def period_range(period: str):
    """Returns (start, end, label) in Eastern time. end is exclusive."""
    now = datetime.datetime.now(EASTERN)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    monday = today - datetime.timedelta(days=today.weekday())
    first = today.replace(day=1)
    if period == "this_week":
        return monday, now, "This week so far"
    if period == "last_week":
        return monday - datetime.timedelta(days=7), monday, "Last week"
    if period == "this_month":
        return first, now, now.strftime("%B %Y") + " so far"
    if period == "last_month":
        prev = (first - datetime.timedelta(days=1)).replace(day=1)
        return prev, first, prev.strftime("%B %Y")
    if period == "this_year":
        return today.replace(month=1, day=1), now, f"{now.year} so far"
    raise ValueError(period)


async def fetch_orders_between(start, end):
    orders, page = [], 1
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        while True:
            params = {
                "per_page": 100, "page": page, "status": PAID_STATUSES,
                "after": start.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                "before": end.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                "dates_are_gmt": "true", "orderby": "date", "order": "asc",
            }
            async with session.get(f"{STORE_URL}/wp-json/wc/v3/orders", params=params, headers=woo_auth_header()) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 300 or not isinstance(data, list):
                    raise RuntimeError(f"{resp.status}: {data.get('message') if isinstance(data, dict) else data}")
            orders += data
            if len(data) < 100:
                return orders
            page += 1


async def count_in_stock():
    total, page = 0, 1
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        while True:
            params = {"per_page": 100, "page": page, "status": "publish", "stock_status": "instock"}
            async with session.get(f"{STORE_URL}/wp-json/wc/v3/products", params=params, headers=woo_auth_header()) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 300 or not isinstance(data, list):
                    return None
            total += len(data)
            if len(data) < 100:
                return total
            page += 1


def money(x):
    return f"${x:,.2f}"


async def build_sales_report(period: str):
    """Returns (embed, csv_file)."""
    start, end, label = period_range(period)
    orders = await fetch_orders_between(start, end)
    in_stock = await count_in_stock()

    total = sum(float(o.get("total", 0)) for o in orders)
    shipping = sum(float(o.get("shipping_total", 0)) for o in orders)
    tax = sum(float(o.get("total_tax", 0)) for o in orders)
    refunds = -sum(float(r.get("total", 0)) for o in orders for r in o.get("refunds", []))
    item_sales = total - shipping - tax
    sold = {}
    for o in orders:
        for i in o.get("line_items", []):
            name = i["name"]
            qty, amt = sold.get(name, (0, 0.0))
            sold[name] = (qty + i["quantity"], amt + float(i["total"]))

    embed = discord.Embed(
        title=f"📊 Sales summary · {label}",
        description=f"{start:%b %d} – {(end - datetime.timedelta(seconds=1)):%b %d, %Y}",
        color=discord.Color.gold(),
    )
    embed.add_field(name="🧾 Orders", value=str(len(orders)))
    embed.add_field(name="💰 Total collected", value=money(total))
    embed.add_field(name="💎 Kit sales", value=money(item_sales))
    embed.add_field(name="📦 Shipping", value=money(shipping))
    embed.add_field(name="🏛️ MI sales tax", value=money(tax))
    embed.add_field(name="↩️ Refunds", value=money(refunds))
    if sold:
        lines = [f"• {q} × {n} — {money(a)}" for n, (q, a) in sorted(sold.items(), key=lambda x: -x[1][1])]
        text = "\n".join(lines[:15]) + (f"\n…and {len(lines) - 15} more" if len(lines) > 15 else "")
        embed.add_field(name="What sold", value=text[:1024], inline=False)
    else:
        embed.add_field(name="What sold", value="No sales this period. 💎", inline=False)
    if in_stock is not None:
        embed.add_field(name="🛍️ Kits still for sale", value=str(in_stock), inline=False)
    embed.set_footer(text="Net to you = Total − Sales tax − Refunds · CSV attached for your books")

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Date", "Order #", "Status", "Customer", "Items", "Kit sales", "Shipping", "Sales tax", "Refunds", "Total"])
    for o in orders:
        created = datetime.datetime.fromisoformat(o["date_created_gmt"]).replace(tzinfo=datetime.timezone.utc).astimezone(EASTERN)
        b = o.get("billing", {})
        items = "; ".join(f"{i['quantity']} x {i['name']}" for i in o.get("line_items", []))
        o_ship, o_tax = float(o.get("shipping_total", 0)), float(o.get("total_tax", 0))
        o_total = float(o.get("total", 0))
        o_ref = -sum(float(r.get("total", 0)) for r in o.get("refunds", []))
        w.writerow([f"{created:%Y-%m-%d}", o.get("number", o["id"]), o.get("status"),
                    f"{b.get('first_name', '')} {b.get('last_name', '')}".strip(), items,
                    f"{o_total - o_ship - o_tax:.2f}", f"{o_ship:.2f}", f"{o_tax:.2f}", f"{o_ref:.2f}", f"{o_total:.2f}"])
    w.writerow([])
    w.writerow(["TOTALS", "", "", "", "", f"{item_sales:.2f}", f"{shipping:.2f}", f"{tax:.2f}", f"{refunds:.2f}", f"{total:.2f}"])
    filename = f"sales_{start:%Y-%m-%d}_to_{(end - datetime.timedelta(seconds=1)):%Y-%m-%d}.csv"
    return embed, discord.File(io.BytesIO(out.getvalue().encode("utf-8-sig")), filename=filename)


@tasks.loop(time=REPORT_TIME)
async def weekly_sales_report():
    if WOO_KEYS is None or datetime.datetime.now(EASTERN).weekday() != 0:  # Mondays only
        return
    try:
        embed, file = await build_sales_report("last_week")
    except Exception as e:
        print(f"⚠️ Weekly sales report failed: {e}")
        return
    for channel in [c for g in bot.guilds for c in g.text_channels if c.name == REPORT_CHANNEL_NAME]:
        try:
            await channel.send(content="📊 **Weekly sales summary**", embed=embed, file=file)
        except discord.Forbidden:
            print(f"⚠️ No permission to post in #{channel.name}")
    print("📊 Weekly sales report posted")


@weekly_sales_report.before_loop
async def before_report():
    await bot.wait_until_ready()


# ===== SOLD-OUT CLEANUP =====
HIDE_SOLD_FROM_SHOP = False   # False = keep it on the site marked "Sold out" (shows off past work)
                              # True  = hide sold kits from the shop and search
SOLD_CHANNEL_NAME = ANNOUNCE_CHANNEL_NAME   # "Just sold!" posts go to #new-kits


async def woo_request(method, path, payload=None):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.request(method, f"{STORE_URL}/wp-json/wc/v3/{path}",
                                   json=payload, headers=woo_auth_header()) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 300:
                raise RuntimeError(f"{resp.status}: {data.get('message') if isinstance(data, dict) else data}")
            return data


async def handle_sold_item(product_id):
    if not product_id:
        return
    product = await woo_request("GET", f"products/{product_id}")
    qty = product.get("stock_quantity")
    sold_out = product.get("stock_status") == "outofstock" or (qty is not None and qty <= 0)
    if not sold_out:
        return  # still has stock left (not one of a kind)

    done = []
    update = {"stock_status": "outofstock",
              "meta_data": [{"key": "_wc_gla_visibility", "value": "dont-sync-and-show"}]}
    if HIDE_SOLD_FROM_SHOP:
        update["catalog_visibility"] = "hidden"
    try:
        await woo_request("PUT", f"products/{product_id}", update)
        done.append("marked sold out")
        done.append("removed from Google Shopping")
        if HIDE_SOLD_FROM_SHOP:
            done.append("hidden from the shop")
    except Exception as e:
        done.append(f"⚠️ couldn't update the product ({e})")

    # Celebrate in Discord
    embed = discord.Embed(
        title=f"💎 Just sold: {product['name']}",
        url=product.get("permalink") or STORE_URL,
        description="This one-of-a-kind piece found a new home! 🎉\nNew kits are added all the time.",
        color=discord.Color.from_rgb(46, 204, 113),
    )
    images = product.get("images") or []
    if images:
        embed.set_image(url=images[0]["src"])
    embed.set_footer(text=STORE_NAME)
    for channel in [c for g in bot.guilds for c in g.text_channels if c.name == SOLD_CHANNEL_NAME]:
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            print(f"⚠️ No permission to post in #{channel.name}")

    # Also share on Facebook if social posting is set up
    if META:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                data = {"caption": f"💎 SOLD! {product['name']} found a new home. Thank you! 🎉\n\n"
                                   f"More one-of-a-kind pieces: {STORE_URL}\n\n{HASHTAGS}"}
                if images:
                    data["url"] = images[0]["src"]
                    await graph_post(session, f"{META['PAGE_ID']}/photos", data)
                else:
                    await graph_post(session, f"{META['PAGE_ID']}/feed", {"message": data["caption"]})
            done.append("posted 'Sold!' on Facebook")
        except Exception as e:
            done.append(f"⚠️ Facebook post failed ({e})")

    print(f"💎 Sold out: {product['name']} — {', '.join(done)}")
    await alert(f"💎 **{product['name']}** sold out: " + ", ".join(done))

# Read the token from token.txt (never paste it into this file or share it)
def load_token():
    raw = pathlib.Path(__file__).with_name("token.txt").read_bytes()
    # Handle files Notepad saved as "Unicode" (UTF-16) or UTF-8 with BOM
    text = raw.decode("utf-16") if b"\x00" in raw else raw.decode("utf-8-sig")
    lines = [line.strip().strip('"').strip("'") for line in text.splitlines() if line.strip()]
    if not lines:
        raise SystemExit("❌ token.txt is empty. Paste your bot token into it and save.")
    if len(lines) > 1:
        print("⚠️ token.txt has more than one line. Using only the first line.")
    # A real Discord token only uses letters, numbers, dots, dashes and underscores.
    # Strip out anything else (hidden characters from copy/paste).
    token = re.sub(r"[^A-Za-z0-9._-]", "", lines[0])
    removed = len(lines[0]) - len(token)
    print(f"🔑 Token loaded: {len(token)} characters, {token.count('.')} dots"
          + (f" (removed {removed} hidden character(s))" if removed else ""))
    return token

TOKEN = load_token()

intents = discord.Intents.default()
intents.members = True  # needed for welcome messages (turn on "Server Members Intent" in the portal)
intents.message_content = True  # needed to read photos + captions in #add-listing (turn on "Message Content Intent")


# ===== LISTING HELPER =====
LISTING_CHANNEL_NAME = "add-listing"   # drop kit photos here
MAX_PHOTOS = 6
MAX_SIZE = 1600                         # longest side in pixels (fast to load on the website)


def clean_photo(data: bytes) -> bytes:
    """Rotate the photo the right way up, trim plain borders, shrink it, save as JPEG."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)            # phone photos often come in sideways
    img = img.convert("RGB")
    # Trim plain borders (white/black edges from scans or screenshots)
    bg = Image.new("RGB", img.size, img.getpixel((0, 0)))
    diff = ImageChops.add(ImageChops.difference(img, bg), ImageChops.difference(img, bg), 2.0, -30)
    box = diff.getbbox()
    if box and (box[2] - box[0]) > img.width * 0.5 and (box[3] - box[1]) > img.height * 0.5:
        img = img.crop(box)
    img.thumbnail((MAX_SIZE, MAX_SIZE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


def guess_from_caption(text: str):
    """'Moonlit Forest 40x50 $24.99' -> name, price, details guesses."""
    text = (text or "").strip()
    price = ""
    m = re.search(r"\$?\s*(\d+(?:\.\d{1,2})?)\s*(?:\$|usd)?\s*$", text.splitlines()[0] if text else "", re.I) or \
        re.search(r"\$\s*(\d+(?:\.\d{1,2})?)", text)
    if m:
        price = m.group(1)
    lines = text.splitlines()
    first = lines[0] if lines else ""
    name = re.sub(r"\$?\s*\d+(?:\.\d{1,2})?\s*\$?\s*$", "", first).strip(" -|,") if price else first
    details = "\n".join(lines[1:]).strip()
    return name[:100], price, details[:1000]


class ListingModal(discord.ui.Modal, title="New listing (saved as a draft)"):
    def __init__(self, image_urls, name="", price="", details=""):
        super().__init__(timeout=None)
        self.image_urls = image_urls
        self.name = discord.ui.TextInput(label="Kit name", default=name or None, max_length=100)
        self.price = discord.ui.TextInput(label="Price", placeholder="24.99", default=price or None, max_length=10)
        self.details = discord.ui.TextInput(
            label="Description", style=discord.TextStyle.paragraph, required=False,
            default=details or None, max_length=1000,
            placeholder="Size, round/square drills, what's included",
        )
        self.category = discord.ui.TextInput(label="Category (optional)", required=False, max_length=100)
        for item in (self.name, self.price, self.details, self.category):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        clean = clean_price(self.price.value)
        if clean is None:
            await interaction.followup.send("❌ Price should be a number, like 24.99. Click the button again.", ephemeral=True)
            return
        cat, note = None, ""
        if self.category.value.strip():
            cat = await find_category(self.category.value)
            if cat is None:
                names = ", ".join(n for _, n in await get_woo_categories()) or "none found"
                note = f"\n⚠️ No category called **{self.category.value}**, so it was left out. Yours: {names}"
        try:
            product = await create_woo_product(
                self.name.value, clean, self.details.value, self.image_urls[0], publish=False,
                category_id=cat[0] if cat else None, extra_images=self.image_urls[1:],
            )
        except Exception as e:
            print(f"❌ Listing helper failed: {e}")
            await interaction.followup.send(f"❌ The website said no: `{e}`", ephemeral=True)
            return
        edit_url = f"{STORE_URL}/wp-admin/post.php?post={product.get('id')}&action=edit"
        await interaction.followup.send(
            f"📝 Draft created: **{self.name.value}** · ${clean}"
            + (f" · 🗂️ {cat[1]}" if cat else "")
            + f"\n✏️ Check it and click **Publish** when ready: {edit_url}{note}",
            ephemeral=True,
        )
        await interaction.message.edit(
            content=interaction.message.content + f"\n✅ Draft created by {interaction.user.mention}", view=None
        )


class ListingButton(discord.ui.View):
    def __init__(self, image_urls, name, price, details):
        super().__init__(timeout=None)
        self.args = (image_urls, name, price, details)

    @discord.ui.button(label="Create draft listing", emoji="📝", style=discord.ButtonStyle.primary)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Only admins can create listings.", ephemeral=True)
            return
        await interaction.response.send_modal(ListingModal(*self.args))


async def handle_listing_photos(message: discord.Message):
    photos = [a for a in message.attachments if (a.content_type or "").startswith("image/")][:MAX_PHOTOS]
    if not photos:
        return
    if not message.author.guild_permissions.manage_guild:
        return
    if WOO_KEYS is None:
        await message.reply("❌ The website isn't connected yet (woo.txt is missing).")
        return
    async with message.channel.typing():
        files = []
        for i, a in enumerate(photos, 1):
            try:
                cleaned = clean_photo(await a.read())
            except Exception as e:
                await message.reply(f"❌ Couldn't read **{a.filename}**: {e}")
                return
            files.append(discord.File(io.BytesIO(cleaned), filename=f"kit_{message.id}_{i}.jpg"))
        name, price, details = guess_from_caption(message.content)
        preview = await message.reply(
            f"✨ Cleaned up {len(files)} photo(s): turned upright, trimmed edges, resized.\n"
            "The **first** photo becomes the main product image. Click below to fill in the details.",
            files=files,
        )
    urls = [a.url for a in preview.attachments]
    await preview.edit(view=ListingButton(urls, name, price, details))


class MyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def on_ready(self):
        # Register slash commands in every server the bot is in (shows up instantly)
        for guild in self.guilds:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        print(f"✅ Logged in as {self.user} — in {len(self.guilds)} server(s)")
        if WOO_KEYS and not check_new_orders.is_running():
            check_new_orders.start()
        if WOO_KEYS and not google_shopping_watchdog.is_running():
            google_shopping_watchdog.start()
        if WOO_KEYS and META and not social_poster.is_running():
            social_poster.start()
        if WOO_KEYS and not weekly_sales_report.is_running():
            weekly_sales_report.start()
        if not daily_quiz.is_running():
            daily_quiz.start()

    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if message.channel.name == LISTING_CHANNEL_NAME and message.attachments:
            await handle_listing_photos(message)

    async def on_member_join(self, member: discord.Member):
        channel = discord.utils.get(member.guild.text_channels, name=WELCOME_CHANNEL_NAME)
        if channel:
            await channel.send(f"👋 Welcome to **{member.guild.name}**, {member.mention}!")


bot = MyBot()


@bot.tree.command(name="hello", description="Say hi to the bot")
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message(f"Hey {interaction.user.mention}! 👋")


@bot.tree.command(name="roll", description="Roll a die")
@app_commands.describe(sides="How many sides (default 6)")
async def roll(interaction: discord.Interaction, sides: int = 6):
    if sides < 2:
        await interaction.response.send_message("A die needs at least 2 sides 🙂", ephemeral=True)
        return
    result = random.randint(1, sides)
    await interaction.response.send_message(f"🎲 You rolled a **{result}** (d{sides})")


@bot.tree.command(name="poll", description="Start a yes/no poll")
@app_commands.describe(question="What do you want to ask?")
async def poll(interaction: discord.Interaction, question: str):
    await interaction.response.send_message(f"📊 **{question}**\nVote below!")
    message = await interaction.original_response()
    await message.add_reaction("👍")
    await message.add_reaction("👎")


@bot.tree.command(name="serverinfo", description="Show info about this server")
async def serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    embed = discord.Embed(title=g.name, color=discord.Color.blurple())
    embed.add_field(name="Members", value=g.member_count)
    embed.add_field(name="Channels", value=len(g.channels))
    embed.add_field(name="Created", value=g.created_at.strftime("%b %d, %Y"))
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="newkit", description="Announce a new Diamond Nook kit (Discord only)")
@app_commands.describe(
    name="Kit name, e.g. Moonlit Forest 40x50cm",
    price="Price, e.g. 24.99",
    image="Upload a photo of the kit",
    link="Product page link (leave blank to use the store homepage)",
    details="Short description: size, round/square drills, etc.",
)
@app_commands.default_permissions(manage_guild=True)  # only server admins see this command
@app_commands.guild_only()
async def newkit(
    interaction: discord.Interaction,
    name: str,
    price: str,
    image: Optional[discord.Attachment] = None,
    link: Optional[str] = None,
    details: Optional[str] = None,
):
    await interaction.response.defer(ephemeral=True)
    clean = clean_price(price)
    if clean is None:
        await interaction.followup.send("❌ Price should be a number, like 24.99", ephemeral=True)
        return
    if image and not (image.content_type or "").startswith("image/"):
        await interaction.followup.send("❌ That file isn't an image. Try a JPG or PNG.", ephemeral=True)
        return
    url = (link or STORE_URL).strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    embed, files = await build_announcement(name, clean, url, details, image)
    channel = await post_announcement(interaction, embed, files)
    if channel is None:
        await interaction.followup.send(
            "❌ I can't post there. Give StoltBot **Send Messages**, **Embed Links** and **Attach Files**.",
            ephemeral=True,
        )
        return
    await interaction.followup.send(f"✅ Announced **{name}** in {channel.mention}!", ephemeral=True)


@bot.tree.command(name="addkit", description="Add a new kit to the Diamond Nook website, then announce it")
@app_commands.describe(
    name="Kit name, e.g. Moonlit Forest 40x50cm",
    price="Price, e.g. 24.99",
    image="Photo of the kit (this becomes the product photo on the website)",
    details="Product description: size, round/square drills, what's included",
    category="Shop category (start typing to see your categories)",
    publish="Yes = live in the shop now. No = save as a draft to finish in WordPress",
    announce="Also post it in Discord? (only happens when published)",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def addkit(
    interaction: discord.Interaction,
    name: str,
    price: str,
    image: discord.Attachment,
    details: Optional[str] = None,
    category: Optional[str] = None,
    publish: bool = True,
    announce: bool = True,
):
    await interaction.response.defer(ephemeral=True)

    if WOO_KEYS is None:
        await interaction.followup.send(
            "❌ The website isn't connected yet. Upload **woo.txt** with your WooCommerce keys and restart the bot.",
            ephemeral=True,
        )
        return
    clean = clean_price(price)
    if clean is None:
        await interaction.followup.send("❌ Price should be a number, like 24.99", ephemeral=True)
        return
    if not (image.content_type or "").startswith("image/"):
        await interaction.followup.send("❌ That file isn't an image. Try a JPG or PNG.", ephemeral=True)
        return

    cat = None
    if category:
        cat = await find_category(category)
        if cat is None:
            names = ", ".join(n for _, n in await get_woo_categories()) or "none found"
            await interaction.followup.send(
                f"❌ No category called **{category}**. Your categories: {names}", ephemeral=True
            )
            return

    try:
        product = await create_woo_product(
            name, clean, details, image.url, publish, category_id=cat[0] if cat else None
        )
    except Exception as e:
        print(f"❌ /addkit failed: {e}")
        await interaction.followup.send(
            f"❌ The website said no: `{e}`\nCheck that the WooCommerce key has **Read/Write** access.",
            ephemeral=True,
        )
        return

    product_url = product.get("permalink") or STORE_URL
    edit_url = f"{STORE_URL}/wp-admin/post.php?post={product.get('id')}&action=edit"
    status = "🟢 Live in the shop" if publish else "📝 Saved as a draft"
    if cat:
        status += f" · 🗂️ {cat[1]}"
    reply = f"✅ **{name}** added to the website. {status}\n🔗 {product_url}\n✏️ Edit: {edit_url}"

    if publish and announce:
        embed, files = await build_announcement(
            name, clean, product_url, details, image, category=cat[1] if cat else None
        )
        channel = await post_announcement(interaction, embed, files)
        reply += f"\n📣 Announced in {channel.mention}" if channel else "\n⚠️ Couldn't post the announcement (check StoltBot's permissions)."

    await interaction.followup.send(reply, ephemeral=True)


@bot.tree.command(name="orders", description="Show the latest paid store orders")
@app_commands.describe(count="How many orders to show (1-10, default 5)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def orders(interaction: discord.Interaction, count: app_commands.Range[int, 1, 10] = 5):
    await interaction.response.defer(ephemeral=True)
    if WOO_KEYS is None:
        await interaction.followup.send("❌ The website isn't connected yet (woo.txt is missing).", ephemeral=True)
        return
    try:
        recent = await fetch_orders(count)
    except Exception as e:
        await interaction.followup.send(f"❌ Couldn't get orders: `{e}`", ephemeral=True)
        return
    if not recent:
        await interaction.followup.send("No paid orders yet. 💎", ephemeral=True)
        return
    await interaction.followup.send(embeds=[order_embed(o) for o in recent], ephemeral=True)


@bot.tree.command(name="gshopping", description="Check how your products are doing on Google Shopping")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def gshopping(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if WOO_KEYS is None:
        await interaction.followup.send("❌ The website isn't connected yet (woo.txt is missing).", ephemeral=True)
        return
    try:
        embed, _ = await google_shopping_report()
    except Exception as e:
        print(f"⚠️ /gshopping failed: {e}")
        hint = ""
        if "404" in str(e):
            hint = "\nThe Google for WooCommerce plugin didn't answer. Is it installed and connected?"
        elif "401" in str(e) or "403" in str(e):
            hint = "\nThe API key isn't allowed to read Google data. It must belong to an Administrator."
        await interaction.followup.send(f"❌ Couldn't check Google Shopping: `{e}`{hint}", ephemeral=True)
        return
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="sharekit", description="Post a kit from the website to Facebook/Instagram now")
@app_commands.describe(kit="Start typing the kit name")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def sharekit(interaction: discord.Interaction, kit: str):
    await interaction.response.defer(ephemeral=True)
    if WOO_KEYS is None or META is None:
        await interaction.followup.send("❌ Social posting isn't set up yet (meta.txt is missing).", ephemeral=True)
        return
    try:
        found = await fetch_published_products(5, search=kit)
    except Exception as e:
        await interaction.followup.send(f"❌ Couldn't read the shop: `{e}`", ephemeral=True)
        return
    if not found:
        await interaction.followup.send(f"❌ No published kit matches **{kit}**.", ephemeral=True)
        return
    results = await post_to_socials(found[0])
    await interaction.followup.send(f"📣 **{found[0]['name']}**: " + " · ".join(results), ephemeral=True)


@sharekit.autocomplete("kit")
async def sharekit_autocomplete(interaction: discord.Interaction, current: str):
    if WOO_KEYS is None:
        return []
    try:
        found = await fetch_published_products(25, search=current or None)
    except Exception:
        return []
    return [app_commands.Choice(name=p["name"][:100], value=p["name"][:100]) for p in found[:25]]


@bot.tree.command(name="salesreport", description="Sales summary with a CSV for your books")
@app_commands.describe(period="Which time period")
@app_commands.choices(period=[
    app_commands.Choice(name="This week", value="this_week"),
    app_commands.Choice(name="Last week", value="last_week"),
    app_commands.Choice(name="This month", value="this_month"),
    app_commands.Choice(name="Last month", value="last_month"),
    app_commands.Choice(name="This year", value="this_year"),
])
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def salesreport(interaction: discord.Interaction, period: str = "last_week"):
    await interaction.response.defer(ephemeral=True)
    if WOO_KEYS is None:
        await interaction.followup.send("❌ The website isn't connected yet (woo.txt is missing).", ephemeral=True)
        return
    try:
        embed, file = await build_sales_report(period)
    except Exception as e:
        print(f"⚠️ /salesreport failed: {e}")
        await interaction.followup.send(f"❌ Couldn't build the report: `{e}`", ephemeral=True)
        return
    await interaction.followup.send(embed=embed, file=file, ephemeral=True)


@addkit.autocomplete("category")
async def category_autocomplete(interaction: discord.Interaction, current: str):
    cats = await get_woo_categories()
    matches = [(cid, n) for cid, n in cats if current.lower() in n.lower()]
    return [app_commands.Choice(name=n[:100], value=n[:100]) for _, n in matches[:25]]


# ===== PERSONAL BUDGET (Google Sheet) =====
# budget.txt (same folder), two lines:
#   URL=https://script.google.com/macros/s/..../exec
#   SECRET=the secret from makeSecret()
def load_budget():
    path = pathlib.Path(__file__).with_name("budget.txt")
    if not path.exists():
        return None
    keys = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            keys[k.strip().upper()] = v.strip()
    if not keys.get("URL", "").startswith("https://script.google.com/") or not keys.get("SECRET"):
        print("⚠️ budget.txt needs URL= (your Apps Script web app link) and SECRET= lines. Budget commands are off.")
        return None
    print("💸 Budget sheet connected")
    return keys


BUDGET = load_budget()
_budget_cats = {"time": 0, "items": []}


async def budget_call(action, **data):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.post(BUDGET["URL"], data=json.dumps({"secret": BUDGET["SECRET"], "action": action, **data}),
                                headers={"Content-Type": "text/plain"}) as resp:
            out = await resp.json(content_type=None)
    if not out.get("ok"):
        raise RuntimeError(out.get("error", "unknown error"))
    return out


async def budget_ready(interaction):
    if BUDGET is None:
        await interaction.response.send_message("❌ The budget sheet isn't connected yet (budget.txt is missing).", ephemeral=True)
        return False
    return True


@bot.tree.command(name="spent", description="Log a purchase in your budget sheet")
@app_commands.describe(amount="How much, e.g. 12.50", category="Start typing a category", note="Where / what (optional)")
@app_commands.default_permissions(manage_guild=True)
async def spent(interaction: discord.Interaction, amount: str, category: str, note: Optional[str] = None):
    if not await budget_ready(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        out = await budget_call("add", amount=amount, category=category, note=note or "")
    except Exception as e:
        await interaction.followup.send(f"❌ Couldn't log it: `{e}`", ephemeral=True)
        return
    left = ""
    if out.get("budget"):
        remaining = out["budget"] - out["spent"]
        left = f" · ${remaining:,.2f} left of ${out['budget']:,.2f}" if remaining >= 0 else f" · ⚠️ ${-remaining:,.2f} over budget"
    await interaction.followup.send(
        f"✅ Logged ${float(str(amount).replace('$', '').replace(',', '')):,.2f} in **{out['category']}**. "
        f"This month: ${out['spent']:,.2f}{left}", ephemeral=True)


@spent.autocomplete("category")
async def spent_category_autocomplete(interaction: discord.Interaction, current: str):
    import time
    if BUDGET is None:
        return []
    if time.time() - _budget_cats["time"] > 600:
        try:
            _budget_cats.update(time=time.time(), items=(await budget_call("categories"))["categories"])
        except Exception:
            pass
    return [app_commands.Choice(name=c, value=c) for c in _budget_cats["items"] if current.lower() in c.lower()][:25]


@bot.tree.command(name="budget", description="Where your money went this month so far")
@app_commands.default_permissions(manage_guild=True)
async def budget(interaction: discord.Interaction):
    if not await budget_ready(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        out = await budget_call("summary")
    except Exception as e:
        await interaction.followup.send(f"❌ Couldn't read the sheet: `{e}`", ephemeral=True)
        return
    embed = discord.Embed(title=f"💸 {out['month']} so far", description=out["text"][:4000], color=discord.Color.teal())
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="bills", description="Bills due in the next 2 weeks")
@app_commands.default_permissions(manage_guild=True)
async def bills(interaction: discord.Interaction):
    if not await budget_ready(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        out = await budget_call("bills", days=14)
    except Exception as e:
        await interaction.followup.send(f"❌ Couldn't read the sheet: `{e}`", ephemeral=True)
        return
    if not out["bills"]:
        await interaction.followup.send("🎉 No bills due in the next 2 weeks.", ephemeral=True)
        return
    lines = [f"• **{b['name']}**: ${b['amount']:,.2f}, "
             f"{'TODAY' if b['days'] == 0 else 'tomorrow' if b['days'] == 1 else 'in ' + str(b['days']) + ' days'} ({b['due']})"
             for b in out["bills"]]
    total = sum(b["amount"] for b in out["bills"])
    await interaction.followup.send(f"🔔 **Bills due soon** (total ${total:,.2f})\n" + "\n".join(lines), ephemeral=True)


@bot.tree.command(name="income", description="Log a paycheck or other money coming in")
@app_commands.describe(amount="How much, e.g. 650.00", source="Where it came from", note="Optional note")
@app_commands.default_permissions(manage_guild=True)
async def income(interaction: discord.Interaction, amount: str, source: str = "Paycheck", note: Optional[str] = None):
    if not await budget_ready(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        out = await budget_call("income", amount=amount, source=source, note=note or "")
    except Exception as e:
        await interaction.followup.send(f"❌ Couldn't log it: `{e}`", ephemeral=True)
        return
    await interaction.followup.send(
        f"💵 Logged ${float(str(amount).replace('$', '').replace(',', '')):,.2f} from **{source}**. "
        f"Income this month: ${out['income']:,.2f}", ephemeral=True)


@bot.tree.command(name="balance", description="Update an account balance")
@app_commands.describe(amount="Current balance, e.g. 842.17", account="Which account (default: Focus Card)")
@app_commands.default_permissions(manage_guild=True)
async def balance(interaction: discord.Interaction, amount: str, account: str = "Focus Card"):
    if not await budget_ready(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        out = await budget_call("balance", amount=amount, account=account)
    except Exception as e:
        await interaction.followup.send(f"❌ Couldn't update it: `{e}`", ephemeral=True)
        return
    lines = [f"• {b['account']}: ${b['balance']:,.2f}" for b in out["balances"]]
    await interaction.followup.send(f"🏦 Updated **{out['account']}**.\n" + "\n".join(lines) +
                                    "\nUse /budget to see what's free to spend.", ephemeral=True)


@balance.autocomplete("account")
async def balance_account_autocomplete(interaction: discord.Interaction, current: str):
    if BUDGET is None:
        return []
    try:
        names = (await budget_call("accounts"))["accounts"]
    except Exception:
        names = []
    if "Focus Card" not in names:
        names.insert(0, "Focus Card")
    return [app_commands.Choice(name=n[:100], value=n[:100]) for n in names if current.lower() in n.lower()][:25]


# ===== GROCERIES (same Google Sheet) =====
grocery = app_commands.Group(name="grocery", description="Grocery list, staples and meal planner",
                             default_permissions=discord.Permissions(manage_guild=True))
price = app_commands.Group(name="price", description="Track grocery prices by store",
                           default_permissions=discord.Permissions(manage_guild=True))


async def grocery_run(interaction, action, **data):
    """Calls the sheet; returns the reply dict, or None after sending an error."""
    if not await budget_ready(interaction):
        return None
    await interaction.response.defer(ephemeral=True)
    try:
        return await budget_call(action, **data)
    except Exception as e:
        await interaction.followup.send(f"❌ {e}", ephemeral=True)
        return None


def list_embed(text, note=""):
    return discord.Embed(description=(note + "\n\n" if note else "") + text[:3900], color=discord.Color.green())


@grocery.command(name="add", description="Add items (separate with commas)")
@app_commands.describe(items="e.g. milk, eggs, bananas")
async def grocery_add(interaction: discord.Interaction, items: str):
    out = await grocery_run(interaction, "g_add", items=items)
    if out:
        note = f"✅ Added: {', '.join(out['added'])}" if out["added"] else "Those are already on the list."
        await interaction.followup.send(embed=list_embed(out["text"], note), ephemeral=True)


@grocery.command(name="list", description="Show the list, grouped by store section")
async def grocery_list(interaction: discord.Interaction):
    out = await grocery_run(interaction, "g_list")
    if out:
        await interaction.followup.send(embed=list_embed(out["text"]), ephemeral=True)


@grocery.command(name="done", description="Check items off (or type: all)")
@app_commands.describe(items="e.g. milk, eggs   or   all")
async def grocery_done(interaction: discord.Interaction, items: str):
    out = await grocery_run(interaction, "g_done", items=items)
    if out:
        note = f"☑️ Checked off: {', '.join(out['done'])}" if out["done"] else "Couldn't find those on the list."
        await interaction.followup.send(embed=list_embed(out["text"], note), ephemeral=True)


@grocery.command(name="clear", description="Remove checked-off items from the sheet")
async def grocery_clear(interaction: discord.Interaction):
    out = await grocery_run(interaction, "g_clear")
    if out:
        await interaction.followup.send(f"🧹 Removed {out['removed']} checked-off item(s).", ephemeral=True)


@grocery.command(name="staples", description="Add all your usual items to the list")
async def grocery_staples(interaction: discord.Interaction):
    out = await grocery_run(interaction, "g_staples")
    if out:
        note = f"✅ Added staples: {', '.join(out['added'])}" if out["added"] else "Your staples are already on the list."
        await interaction.followup.send(embed=list_embed(out["text"], note), ephemeral=True)


@grocery.command(name="savestaple", description="Save items you buy every week")
@app_commands.describe(items="e.g. coffee, milk, bread")
async def grocery_savestaple(interaction: discord.Interaction, items: str):
    out = await grocery_run(interaction, "g_savestaple", items=items)
    if out:
        msg = f"⭐ Saved as staples: {', '.join(out['saved'])}" if out["saved"] else "Those are already staples."
        await interaction.followup.send(msg + "\nUse /grocery staples to add them to the list.", ephemeral=True)


@grocery.command(name="savemeal", description="Save a meal and its ingredients")
@app_commands.describe(meal="e.g. Spaghetti", ingredients="e.g. spaghetti, pasta sauce, ground beef, parmesan")
async def grocery_savemeal(interaction: discord.Interaction, meal: str, ingredients: str):
    out = await grocery_run(interaction, "g_savemeal", name=meal, ingredients=ingredients)
    if out:
        await interaction.followup.send(f"🍽️ Saved **{out['meal']}**: {out['ingredients']}", ephemeral=True)


@grocery.command(name="plan", description="Plan meals: their ingredients go on the list")
@app_commands.describe(meals="Meals for the week, separated by commas (e.g. Tacos, Spaghetti)")
async def grocery_plan(interaction: discord.Interaction, meals: str):
    out = await grocery_run(interaction, "g_plan", meals=meals)
    if out:
        note = f"🍽️ Planned: {', '.join(out['found']) or 'nothing'}"
        if out["missing"]:
            note += f"\n⚠️ Not saved yet: {', '.join(out['missing'])} (use /grocery savemeal)"
        if out.get("have"):
            note += f"\n🥫 Already in the pantry: {', '.join(out['have'])}"
        if out["added"]:
            note += f"\n✅ Added: {', '.join(out['added'])}"
        await interaction.followup.send(embed=list_embed(out["text"], note), ephemeral=True)


@grocery.command(name="meals", description="Show your saved meals")
async def grocery_meals(interaction: discord.Interaction):
    out = await grocery_run(interaction, "g_meals")
    if out:
        if not out["meals"]:
            await interaction.followup.send("No saved meals yet. Use /grocery savemeal.", ephemeral=True)
            return
        lines = [f"🍽️ **{m['name']}**: {m['ingredients']}" for m in out["meals"]]
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)


def compare_text(rows):
    if not rows:
        return "No prices logged for that yet."
    best = rows[0]
    lines = [f"{'🏆' if i == 0 else '•'} **{r['store']}**: ${r['price']:,.2f}" + (f" ({r['size']})" if r.get("size") else "")
             for i, r in enumerate(rows)]
    if len(rows) > 1:
        lines.append(f"\nCheapest at **{best['store']}**, saves ${rows[-1]['price'] - best['price']:,.2f} vs {rows[-1]['store']}")
    return "\n".join(lines)


@price.command(name="log", description="Log what you paid for an item")
@app_commands.describe(item="e.g. Milk", store="e.g. Kroger", amount="e.g. 3.49", size="Optional, e.g. 1 gallon")
async def price_log(interaction: discord.Interaction, item: str, store: str, amount: str, size: Optional[str] = None):
    out = await grocery_run(interaction, "g_price", item=item, store=store, price=amount, size=size or "")
    if out:
        await interaction.followup.send(f"🏷️ Logged **{item}** at {store}.\n\n" + compare_text(out["compare"]), ephemeral=True)


@price.command(name="check", description="Which store is cheapest for an item")
@app_commands.describe(item="e.g. Milk")
async def price_check(interaction: discord.Interaction, item: str):
    out = await grocery_run(interaction, "g_compare", item=item)
    if out:
        await interaction.followup.send(f"🏷️ **{item}** prices:\n" + compare_text(out["compare"]), ephemeral=True)


bot.tree.add_command(grocery)
bot.tree.add_command(price)


# ===== VACATION PLANNER (same Google Sheet) =====
trip = app_commands.Group(name="trip", description="Vacation planner",
                          default_permissions=discord.Permissions(manage_guild=True))
pack = app_commands.Group(name="pack", description="Trip packing list",
                          default_permissions=discord.Permissions(manage_guild=True))
TRIP_COLOR = discord.Color.from_rgb(255, 170, 0)


async def trip_send(interaction, action, **data):
    out = await grocery_run(interaction, action, **data)
    if out and out.get("text"):
        note = data.pop("_note", "")
        await interaction.followup.send(embed=discord.Embed(description=out["text"][:3900], color=TRIP_COLOR), ephemeral=True)
    return out


async def trip_name_autocomplete(interaction: discord.Interaction, current: str):
    if BUDGET is None:
        return []
    try:
        names = (await budget_call("t_trips"))["trips"]
    except Exception:
        return []
    return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n.lower()][:25]


@trip.command(name="new", description="Start (or update) a trip")
@app_commands.describe(name="e.g. Vegas", goal="How much to save, e.g. 1500", date="Trip start date, YYYY-MM-DD")
async def trip_new(interaction: discord.Interaction, name: str, goal: Optional[str] = None, date: Optional[str] = None):
    await trip_send(interaction, "t_new", name=name, goal=goal or "0", date=date or "")


@trip.command(name="status", description="Savings, countdown and packing progress")
@app_commands.autocomplete(name=trip_name_autocomplete)
async def trip_status(interaction: discord.Interaction, name: Optional[str] = None):
    await trip_send(interaction, "t_status", trip=name or "")


@trip.command(name="save", description="Put money toward a trip (negative to take some out)")
@app_commands.describe(amount="e.g. 50", note="Optional")
@app_commands.autocomplete(name=trip_name_autocomplete)
async def trip_save(interaction: discord.Interaction, amount: str, note: Optional[str] = None, name: Optional[str] = None):
    await trip_send(interaction, "t_save", amount=amount, note=note or "", trip=name or "")


@trip.command(name="plan", description="Add to the itinerary")
@app_commands.describe(day="e.g. Day 1 or Friday", plans="Separate with commas: Fremont Street, Dinner at the Bellagio")
@app_commands.autocomplete(name=trip_name_autocomplete)
async def trip_plan(interaction: discord.Interaction, day: str, plans: str, name: Optional[str] = None):
    await trip_send(interaction, "t_plan", day=day, plan=plans, trip=name or "")


@trip.command(name="itinerary", description="Show the day-by-day plan")
@app_commands.autocomplete(name=trip_name_autocomplete)
async def trip_itinerary(interaction: discord.Interaction, name: Optional[str] = None):
    await trip_send(interaction, "t_itinerary", trip=name or "")


@trip.command(name="spent", description="Log money spent during the trip")
@app_commands.describe(amount="e.g. 45", what="e.g. Buffet")
@app_commands.autocomplete(name=trip_name_autocomplete)
async def trip_spent(interaction: discord.Interaction, amount: str, what: Optional[str] = None, name: Optional[str] = None):
    await trip_send(interaction, "t_spent", amount=amount, what=what or "", trip=name or "")


@trip.command(name="quote", description="Save a price you found (flight, hotel, show...)")
@app_commands.describe(type="e.g. Flight, Hotel, Show", where="e.g. Spirit, Circus Circus", price="e.g. 289", note="Optional: dates, room type...")
@app_commands.autocomplete(name=trip_name_autocomplete)
async def trip_quote(interaction: discord.Interaction, type: str, where: str, price: str,
                     note: Optional[str] = None, name: Optional[str] = None):
    await trip_send(interaction, "t_quote", type=type, where=where, price=price, note=note or "", trip=name or "")


@trip.command(name="quotes", description="Compare the prices you've saved")
@app_commands.autocomplete(name=trip_name_autocomplete)
async def trip_quotes(interaction: discord.Interaction, name: Optional[str] = None):
    await trip_send(interaction, "t_quotes", trip=name or "")


@pack.command(name="add", description="Add items to pack (separate with commas)")
@app_commands.autocomplete(trip=trip_name_autocomplete)
async def pack_add(interaction: discord.Interaction, items: str, trip: Optional[str] = None):
    await trip_send(interaction, "p_add", items=items, trip=trip or "")


@pack.command(name="always", description="Add your always-pack basics to this trip")
@app_commands.autocomplete(trip=trip_name_autocomplete)
async def pack_always(interaction: discord.Interaction, trip: Optional[str] = None):
    await trip_send(interaction, "p_always", trip=trip or "")


@pack.command(name="savealways", description="Save items you bring on every trip")
async def pack_savealways(interaction: discord.Interaction, items: str):
    out = await grocery_run(interaction, "p_savealways", items=items, trip="")
    if out:
        msg = f"⭐ Saved: {', '.join(out['saved'])}" if out["saved"] else "Those are already on your always-pack list."
        await interaction.followup.send(msg, ephemeral=True)


@pack.command(name="list", description="What's packed and what's left")
@app_commands.autocomplete(trip=trip_name_autocomplete)
async def pack_list(interaction: discord.Interaction, trip: Optional[str] = None):
    await trip_send(interaction, "p_list", trip=trip or "")


@pack.command(name="done", description="Check items off (or type: all)")
@app_commands.autocomplete(trip=trip_name_autocomplete)
async def pack_done(interaction: discord.Interaction, items: str, trip: Optional[str] = None):
    await trip_send(interaction, "p_done", items=items, trip=trip or "")


@trip.command(name="flight", description="Save a flight with departure and arrival times")
@app_commands.describe(
    leg="Going, Returning, or a connection",
    date="Departure date, YYYY-MM-DD",
    depart="Departure time, e.g. 6:05 AM",
    arrive="Arrival time, e.g. 8:15 AM (local time where you land)",
    airline="e.g. Spirit",
    number="Flight number, e.g. NK 1234",
    from_airport="e.g. DTW",
    to_airport="e.g. LAS",
    arrive_date="Only if you land on a different day, YYYY-MM-DD",
    duration="Flight length, e.g. 4h 10m (worked out automatically for most US airports)",
    note="Optional: seat, gate, terminal...")
@app_commands.choices(leg=[
    app_commands.Choice(name="Going", value="Going"),
    app_commands.Choice(name="Returning", value="Returning"),
    app_commands.Choice(name="Connection", value="Connection"),
])
@app_commands.autocomplete(name=trip_name_autocomplete)
async def trip_flight(interaction: discord.Interaction, leg: app_commands.Choice[str], date: str, depart: str,
                      arrive: Optional[str] = None, airline: Optional[str] = None, number: Optional[str] = None,
                      from_airport: Optional[str] = None, to_airport: Optional[str] = None,
                      arrive_date: Optional[str] = None, duration: Optional[str] = None,
                      note: Optional[str] = None, name: Optional[str] = None):
    await trip_send(interaction, "t_flight", leg=leg.value, date=date, depart=depart, arrive=arrive or "",
                    arrive_date=arrive_date or "", airline=airline or "", number=number or "",
                    **{"from": from_airport or "", "to": to_airport or ""}, duration=duration or "",
                    note=note or "", trip=name or "")


@trip.command(name="flights", description="Show your saved flights")
@app_commands.autocomplete(name=trip_name_autocomplete)
async def trip_flights(interaction: discord.Interaction, name: Optional[str] = None):
    await trip_send(interaction, "t_flights", trip=name or "")


@trip.command(name="hotel", description="Add a hotel stay")
@app_commands.describe(
    hotel="e.g. Circus Circus", check_in="YYYY-MM-DD", check_out="YYYY-MM-DD",
    cost="Total for the stay, e.g. 420", address="Optional", confirmation="Optional confirmation number",
    check_in_time="Default 3:00 PM", check_out_time="Default 11:00 AM", note="Optional: room type, resort fee...")
@app_commands.autocomplete(name=trip_name_autocomplete)
async def trip_hotel(interaction: discord.Interaction, hotel: str, check_in: str, check_out: str,
                     cost: Optional[str] = None, address: Optional[str] = None, confirmation: Optional[str] = None,
                     check_in_time: Optional[str] = None, check_out_time: Optional[str] = None,
                     note: Optional[str] = None, name: Optional[str] = None):
    await trip_send(interaction, "t_book", type="Hotel", name=hotel, date=check_in, time=check_in_time or "",
                    end_date=check_out, end_time=check_out_time or "", cost=cost or "0", address=address or "",
                    confirmation=confirmation or "", note=note or "", trip=name or "")


@trip.command(name="book", description="Add a booking: rental car, show, restaurant, tour...")
@app_commands.describe(
    type="What kind", what="e.g. Blue Man Group, Enterprise, Bacchanal Buffet",
    date="YYYY-MM-DD", time="e.g. 7:00 PM", end_date="Optional, e.g. car return day", end_time="Optional",
    cost="Optional", address="Optional", confirmation="Optional confirmation number", note="Optional")
@app_commands.choices(type=[
    app_commands.Choice(name="Rental car", value="Car"),
    app_commands.Choice(name="Show", value="Show"),
    app_commands.Choice(name="Restaurant", value="Restaurant"),
    app_commands.Choice(name="Tour", value="Tour"),
    app_commands.Choice(name="Activity", value="Activity"),
    app_commands.Choice(name="Hotel", value="Hotel"),
    app_commands.Choice(name="Other", value="Other"),
])
@app_commands.autocomplete(name=trip_name_autocomplete)
async def trip_book(interaction: discord.Interaction, type: app_commands.Choice[str], what: str,
                    date: Optional[str] = None, time: Optional[str] = None,
                    end_date: Optional[str] = None, end_time: Optional[str] = None,
                    cost: Optional[str] = None, address: Optional[str] = None, confirmation: Optional[str] = None,
                    note: Optional[str] = None, name: Optional[str] = None):
    await trip_send(interaction, "t_book", type=type.value, name=what, date=date or "", time=time or "",
                    end_date=end_date or "", end_time=end_time or "", cost=cost or "0", address=address or "",
                    confirmation=confirmation or "", note=note or "", trip=name or "")


@trip.command(name="bookings", description="Show hotels, rentals, shows and other bookings")
@app_commands.autocomplete(name=trip_name_autocomplete)
async def trip_bookings(interaction: discord.Interaction, name: Optional[str] = None):
    await trip_send(interaction, "t_bookings", trip=name or "")


class ConfirmReset(discord.ui.View):
    def __init__(self, user_id, part, trip_name):
        super().__init__(timeout=60)
        self.user_id, self.part, self.trip_name = user_id, part, trip_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Yes, clear it", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="⏳ Clearing…", view=None)
        try:
            out = await budget_call("t_reset", part=self.part, trip=self.trip_name)
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ {e}")
            return
        await interaction.edit_original_response(content=None,
            embed=discord.Embed(description=out["text"][:3900], color=TRIP_COLOR))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="👍 Nothing was changed.", view=None)


@trip.command(name="reset", description="Clear part of a trip (asks before deleting)")
@app_commands.describe(what="What to clear")
@app_commands.choices(what=[
    app_commands.Choice(name="Savings", value="savings"),
    app_commands.Choice(name="Itinerary", value="itinerary"),
    app_commands.Choice(name="Packing list", value="packing"),
    app_commands.Choice(name="Trip spending", value="spending"),
    app_commands.Choice(name="Price quotes", value="quotes"),
    app_commands.Choice(name="Flights", value="flights"),
    app_commands.Choice(name="Bookings (hotels, cars, shows...)", value="bookings"),
    app_commands.Choice(name="EVERYTHING (delete the whole trip)", value="everything"),
])
@app_commands.autocomplete(name=trip_name_autocomplete)
async def trip_reset(interaction: discord.Interaction, what: app_commands.Choice[str], name: Optional[str] = None):
    if not await budget_ready(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:   # find out which trip this applies to before asking
        status = await budget_call("t_status", trip=name or "")
    except Exception as e:
        await interaction.followup.send(f"❌ {e}", ephemeral=True)
        return
    trip_name = status["text"].splitlines()[0].replace("🌴", "").strip()
    warn = (f"⚠️ This deletes the **whole {trip_name} trip**: savings, itinerary, flights, bookings, packing, spending and quotes."
            if what.value == "everything" else f"⚠️ This clears **{what.name.lower()}** for **{trip_name}**.")
    await interaction.followup.send(warn + "\nIt can't be undone. Are you sure?",
                                    view=ConfirmReset(interaction.user.id, what.value, trip_name), ephemeral=True)


bot.tree.add_command(trip)
bot.tree.add_command(pack)


# ===== PANTRY (same Google Sheet) =====
pantry = app_commands.Group(name="pantry", description="What's in the pantry",
                            default_permissions=discord.Permissions(manage_guild=True))
PANTRY_COLOR = discord.Color.from_rgb(205, 133, 63)


async def pantry_send(interaction, action, **data):
    out = await grocery_run(interaction, action, **data)
    if out:
        await interaction.followup.send(embed=discord.Embed(description=out["text"][:3900], color=PANTRY_COLOR), ephemeral=True)


@pantry.command(name="add", description="Add food you have (e.g. 2 cans black beans, milk)")
@app_commands.describe(items="Separate with commas. Amounts optional: 2 cans beans, 3 lb chicken", expires="Optional, YYYY-MM-DD")
async def pantry_add(interaction: discord.Interaction, items: str, expires: Optional[str] = None):
    await pantry_send(interaction, "pn_add", items=items, expires=expires or "")


@pantry.command(name="use", description="Use something up (e.g. 1 can black beans, milk)")
@app_commands.describe(items="Separate with commas. Amounts optional")
async def pantry_use(interaction: discord.Interaction, items: str):
    await pantry_send(interaction, "pn_use", items=items)


@pantry.command(name="list", description="What's in the pantry")
@app_commands.choices(show=[
    app_commands.Choice(name="Everything", value=""),
    app_commands.Choice(name="Running low", value="low"),
    app_commands.Choice(name="Expiring this week", value="expiring"),
])
async def pantry_list(interaction: discord.Interaction, show: Optional[app_commands.Choice[str]] = None):
    await pantry_send(interaction, "pn_list", filter=show.value if show else "")


@pantry.command(name="set", description="Fix an amount, set the low-stock level, or an expiration date")
@app_commands.describe(item="e.g. Milk", qty="How many you have now", low_at="Put it on the grocery list when it gets this low",
                       expires="YYYY-MM-DD")
async def pantry_set(interaction: discord.Interaction, item: str, qty: Optional[float] = None,
                     low_at: Optional[float] = None, expires: Optional[str] = None):
    await pantry_send(interaction, "pn_set", item=item, qty="" if qty is None else qty,
                      low="" if low_at is None else low_at, expires=expires or "")


@pantry.command(name="remove", description="Take an item off the pantry list completely")
async def pantry_remove(interaction: discord.Interaction, item: str):
    await pantry_send(interaction, "pn_remove", item=item)


@pantry.command(name="restock", description="Put everything that's running low on the grocery list")
async def pantry_restock(interaction: discord.Interaction):
    await pantry_send(interaction, "pn_restock")


bot.tree.add_command(pantry)


# ===== CAREER ROADMAP (same Google Sheet) =====
career = app_commands.Group(name="career", description="Security+ and freelancing roadmap",
                            default_permissions=discord.Permissions(manage_guild=True))
CAREER_COLOR = discord.Color.from_rgb(31, 122, 104)


async def career_send(interaction, action, **data):
    out = await grocery_run(interaction, action, **data)
    if out:
        await interaction.followup.send(embed=discord.Embed(description=out["text"][:3900], color=CAREER_COLOR), ephemeral=True)


async def _career_steps(current, undo=False):
    if BUDGET is None:
        return []
    try:
        steps = (await budget_call("c_open", undo=undo))["steps"]
    except Exception:
        return []
    return [app_commands.Choice(name=(("🔐 " if x["track"] == "Security" else "💼 ") + x["step"])[:100], value=x["id"])
            for x in steps if current.lower() in x["step"].lower()][:25]


async def open_steps_ac(interaction: discord.Interaction, current: str):
    return await _career_steps(current)


async def done_steps_ac(interaction: discord.Interaction, current: str):
    return await _career_steps(current, undo=True)


@career.command(name="status", description="Progress, next steps, and the full roadmap")
async def career_status(interaction: discord.Interaction):
    await career_send(interaction, "c_status")


@career.command(name="done", description="Check off a step")
@app_commands.describe(step="Start typing; pick from the list")
@app_commands.autocomplete(step=open_steps_ac)
async def career_done(interaction: discord.Interaction, step: str):
    await career_send(interaction, "c_done", step=step)


@career.command(name="undo", description="Uncheck a step")
@app_commands.autocomplete(step=done_steps_ac)
async def career_undo(interaction: discord.Interaction, step: str):
    await career_send(interaction, "c_undo", step=step)


@career.command(name="gig", description="Log a finished freelance gig")
@app_commands.describe(gig="e.g. Discord bot", amount="What you earned, e.g. 75", client="Optional")
async def career_gig(interaction: discord.Interaction, gig: str, amount: Optional[str] = None, client: Optional[str] = None):
    await career_send(interaction, "c_gig", gig=gig, amount=amount or "0", client=client or "")


bot.tree.add_command(career)


# ===== SECURITY+ DAILY PRACTICE QUESTION =====
QUIZ_CHANNEL_NAMES = ["study", "budget"]          # posts in #study if it exists, otherwise #budget
QUIZ_TIME = datetime.time(hour=18, minute=0, tzinfo=EASTERN)   # 6 PM Eastern, before a night shift
# (domain, question, [A, B, C, D], correct index, explanation)
QUIZ = [
 ("1 General concepts", "Which part of the CIA triad does encryption mainly protect?", ["Confidentiality", "Integrity", "Availability", "Non-repudiation"], 0, "Encryption keeps data secret from people who shouldn't read it: confidentiality."),
 ("1 General concepts", "A hash of a file is checked before and after download. What is being verified?", ["Confidentiality", "Integrity", "Availability", "Authentication"], 1, "Matching hashes show the file wasn't changed: integrity."),
 ("1 General concepts", "A digital signature proves the sender can't deny sending a message. This is called:", ["Integrity", "Obfuscation", "Non-repudiation", "Authorization"], 2, "Non-repudiation means the sender can't credibly deny their action, because only their private key could make the signature."),
 ("1 General concepts", "Which control type is a security guard at a building entrance?", ["Technical", "Managerial", "Physical", "Operational only"], 2, "A guard physically controls access, so it's a physical control (also often called operational)."),
 ("1 General concepts", "A warning banner on a login screen is mainly what kind of control?", ["Deterrent", "Corrective", "Compensating", "Detective"], 0, "Banners discourage misuse before it happens: a deterrent."),
 ("1 General concepts", "Which key is used to ENCRYPT a message so only Bob can read it?", ["Alice's private key", "Bob's public key", "Bob's private key", "A shared hash"], 1, "Encrypt with the recipient's public key; only their private key can decrypt it."),
 ("1 General concepts", "Zero Trust is best summed up as:", ["Trust everything inside the firewall", "Never trust, always verify", "Trust only admins", "Block all traffic"], 1, "Zero Trust verifies every request, no matter where it comes from on the network."),
 ("1 General concepts", "Adding random data to a password before hashing it is called:", ["Salting", "Key stretching", "Tokenization", "Steganography"], 0, "A salt makes identical passwords hash differently and defeats precomputed rainbow tables."),
 ("2 Threats", "An email pretending to be from your bank asks you to 'verify' your login. This is:", ["Vishing", "Phishing", "Smishing", "Whaling"], 1, "Phishing is fraudulent email. Vishing is by voice, smishing by text, whaling targets executives."),
 ("2 Threats", "A phishing attack aimed specifically at a company's CEO is called:", ["Spear phishing", "Whaling", "Pharming", "Tailgating"], 1, "Whaling targets high-level executives (the 'big fish')."),
 ("2 Threats", "Malware that encrypts files and demands payment is:", ["Spyware", "A worm", "Ransomware", "A rootkit"], 2, "Ransomware holds data hostage until a ransom is paid."),
 ("2 Threats", "Malware that spreads across a network by itself, with no user action, is a:", ["Virus", "Worm", "Trojan", "Logic bomb"], 1, "Worms self-replicate across networks; viruses need a host file or user action."),
 ("2 Threats", "Someone follows an employee through a secure door without badging in. This is:", ["Shoulder surfing", "Tailgating", "Dumpster diving", "Pretexting"], 1, "Tailgating (piggybacking) slips through a physical access control behind someone else."),
 ("2 Threats", "A website form lets an attacker type ' OR 1=1 -- to log in. What attack is this?", ["XSS", "SQL injection", "CSRF", "Buffer overflow"], 1, "Injecting SQL into input fields to change the database query is SQL injection."),
 ("2 Threats", "Flooding a server from thousands of infected computers at once is a:", ["DoS", "DDoS", "MITM", "Replay attack"], 1, "Distributed denial of service uses many sources, often a botnet."),
 ("2 Threats", "A flaw that attackers exploit before the vendor knows about it or has a patch is:", ["A zero-day", "A misconfiguration", "Legacy software", "A false positive"], 0, "Zero-day means the defenders have had zero days to fix it."),
 ("2 Threats", "An attacker guesses common passwords like 'Password1' against many accounts. This is:", ["Brute force", "Password spraying", "Credential stuffing", "Rainbow table"], 1, "Spraying tries a few common passwords across many accounts to avoid lockouts."),
 ("2 Threats", "Reusing usernames and passwords leaked from another site to log in elsewhere is:", ["Credential stuffing", "Password spraying", "Pass the hash", "Birthday attack"], 0, "Credential stuffing works because people reuse passwords."),
 ("3 Architecture", "Which network zone holds public-facing servers, separated from the internal network?", ["Intranet", "Screened subnet (DMZ)", "VLAN 1", "Air gap"], 1, "A screened subnet (DMZ) exposes public services while protecting the internal LAN."),
 ("3 Architecture", "Splitting a network into separate segments to limit an attacker's movement is:", ["Segmentation", "Load balancing", "NAT", "Clustering"], 0, "Segmentation limits lateral movement and contains breaches."),
 ("3 Architecture", "Which protocol should replace Telnet for secure remote command-line access?", ["FTP", "SSH", "HTTP", "SNMPv1"], 1, "SSH encrypts the whole session; Telnet sends everything in plain text."),
 ("3 Architecture", "HTTPS uses which protocol to encrypt web traffic?", ["SSL 2.0", "TLS", "IPsec AH", "WEP"], 1, "Modern HTTPS uses TLS. Old SSL versions are insecure."),
 ("3 Architecture", "Which Wi-Fi security standard is currently the strongest choice?", ["WEP", "WPA", "WPA2-TKIP", "WPA3"], 3, "WPA3 is the current, strongest Wi-Fi security standard."),
 ("3 Architecture", "Keeping backups in a different building or cloud region protects mainly against:", ["Phishing", "Site-wide disasters", "SQL injection", "Weak passwords"], 1, "Offsite backups survive fire, flood or other events at the main site."),
 ("3 Architecture", "In the cloud 'shared responsibility model', who secures the physical data center?", ["The customer", "The cloud provider", "Both equally", "The ISP"], 1, "The provider secures the physical infrastructure; the customer secures their data, accounts and settings."),
 ("3 Architecture", "RAID 1 provides:", ["Striping with no redundancy", "Mirroring", "Encryption", "Off-site backup"], 1, "RAID 1 mirrors data on two drives, so one can fail without data loss."),
 ("4 Operations", "Which tool collects logs from many systems and correlates them to spot attacks?", ["SIEM", "DLP", "VPN", "WAF"], 0, "A SIEM gathers and correlates logs, then alerts on suspicious patterns."),
 ("4 Operations", "Requiring a password AND a code from a phone app is an example of:", ["Single sign-on", "Multifactor authentication", "Federation", "Least privilege"], 1, "Something you know plus something you have equals MFA."),
 ("4 Operations", "Giving users only the access they need to do their job is:", ["Least privilege", "Separation of duties", "Job rotation", "Implicit trust"], 0, "Least privilege limits the damage if an account is misused or compromised."),
 ("4 Operations", "What is the FIRST phase of the incident response process?", ["Containment", "Preparation", "Eradication", "Lessons learned"], 1, "Preparation comes first: plans, tools and training before an incident happens."),
 ("4 Operations", "An IDS differs from an IPS because an IDS:", ["Blocks traffic", "Only detects and alerts", "Encrypts traffic", "Replaces a firewall"], 1, "An IDS watches and alerts; an IPS sits inline and can block."),
 ("4 Operations", "Documenting who handled evidence and when is called:", ["Chain of custody", "Legal hold", "Order of volatility", "E-discovery"], 0, "Chain of custody proves evidence wasn't tampered with."),
 ("4 Operations", "A tool that stops employees from emailing out customer credit card numbers is:", ["DLP", "EDR", "NAC", "SOAR"], 0, "Data loss prevention detects and blocks sensitive data leaving the organization."),
 ("4 Operations", "Which is the best way to handle a known software vulnerability?", ["Ignore it", "Apply the vendor patch", "Turn off logging", "Rename the program"], 1, "Patching removes the vulnerability; it's core to vulnerability management."),
 ("5 Program management", "A document that sets rules for how employees may use company computers is a:", ["Acceptable use policy", "SLA", "NDA", "MOU"], 0, "An acceptable use policy (AUP) defines allowed and forbidden use."),
 ("5 Program management", "Buying cyber insurance is which risk response?", ["Avoid", "Transfer", "Accept", "Mitigate"], 1, "Insurance transfers the financial risk to another party."),
 ("5 Program management", "Annual loss expectancy (ALE) is calculated as:", ["SLE × ARO", "ARO ÷ SLE", "Asset value × 2", "SLE + ARO"], 0, "ALE = single loss expectancy × annualized rate of occurrence."),
 ("5 Program management", "A contract that defines guaranteed uptime from a vendor is a:", ["SLA", "NDA", "BPA", "AUP"], 0, "A service level agreement spells out performance promises like 99.9% uptime."),
 ("5 Program management", "Requiring two people to approve large payments is an example of:", ["Least privilege", "Separation of duties", "Mandatory vacation", "Need to know"], 1, "Separation of duties prevents one person from committing and hiding fraud."),
 ("5 Program management", "Training employees to spot phishing is mainly which kind of control?", ["Technical", "Physical", "Managerial/operational", "Compensating"], 2, "Security awareness training is a people-focused (managerial/operational) control."),
 ("1 General concepts", "Which is an example of 'something you are' in authentication?", ["A PIN", "A fingerprint", "A smart card", "A security question"], 1, "Biometrics like fingerprints are something you are."),
 ("1 General concepts", "Symmetric encryption uses:", ["One shared key", "A public and private key pair", "No key", "A hash only"], 0, "Symmetric encryption uses the same key to encrypt and decrypt, like AES."),
 ("1 General concepts", "Which is an asymmetric algorithm?", ["AES", "RSA", "3DES", "Blowfish"], 1, "RSA uses a public/private key pair. AES, 3DES and Blowfish are symmetric."),
 ("1 General concepts", "A fake system set up to attract and study attackers is a:", ["Honeypot", "Proxy", "Jump server", "Sandbox"], 0, "Honeypots lure attackers away from real systems and reveal their methods."),
 ("1 General concepts", "Which type of control is restoring from backup after an attack?", ["Preventive", "Detective", "Corrective", "Deterrent"], 2, "Corrective controls fix things after an incident, like restoring from backup."),
 ("1 General concepts", "A trusted third party that issues digital certificates is a:", ["CA", "CRL", "CSR", "HSM"], 0, "A certificate authority (CA) signs and issues certificates."),
 ("1 General concepts", "Formal approval steps before changing a production system are called:", ["Change management", "Patch Tuesday", "Hot fixing", "Shadow IT"], 0, "Change management reviews, approves and documents changes to reduce outages and risk."),
 ("1 General concepts", "A list of certificates revoked before they expired is a:", ["CSR", "CRL", "OID", "PEM"], 1, "The certificate revocation list (CRL) tells systems which certificates not to trust."),
 ("2 Threats", "A text message with a fake package-delivery link is:", ["Smishing", "Vishing", "Whaling", "Pharming"], 0, "Smishing is SMS phishing."),
 ("2 Threats", "Malicious code that pretends to be useful software is a:", ["Worm", "Trojan", "Rootkit", "Keylogger"], 1, "A Trojan hides malware inside something that looks legitimate."),
 ("2 Threats", "Malware that records every key you type is a:", ["Keylogger", "Adware", "Logic bomb", "Botnet"], 0, "Keyloggers capture keystrokes, including passwords."),
 ("2 Threats", "Code that waits and runs on a set date or event is a:", ["Logic bomb", "Worm", "Rootkit", "Backdoor"], 0, "A logic bomb triggers when a condition is met, like a certain date."),
 ("2 Threats", "An attacker secretly sits between you and a website, reading traffic. This is:", ["On-path (man-in-the-middle)", "DDoS", "Replay", "Privilege escalation"], 0, "An on-path attack intercepts communication between two parties."),
 ("2 Threats", "Injecting a script into a web page that runs in other users' browsers is:", ["SQL injection", "Cross-site scripting (XSS)", "Directory traversal", "Race condition"], 1, "XSS runs attacker JavaScript in victims' browsers."),
 ("2 Threats", "A government-backed hacker group with large resources is a:", ["Script kiddie", "Hacktivist", "Nation-state actor", "Insider threat"], 2, "Nation-state actors are highly skilled and well funded, often running advanced persistent threats."),
 ("2 Threats", "A disgruntled employee stealing data is which type of threat?", ["Insider threat", "Shadow IT", "Supply chain", "Zero-day"], 0, "Insiders already have legitimate access, which makes them dangerous."),
 ("2 Threats", "Leaving malware USB drives in a parking lot for someone to plug in is:", ["Baiting", "Tailgating", "Vishing", "Typosquatting"], 0, "Baiting uses curiosity to get victims to run malware."),
 ("2 Threats", "Registering 'amaz0n.com' to catch people who mistype the address is:", ["Typosquatting", "DNS poisoning", "Pharming", "Watering hole"], 0, "Typosquatting relies on common typing mistakes."),
 ("3 Architecture", "Which device filters traffic between networks based on rules?", ["Switch", "Firewall", "Hub", "Repeater"], 1, "Firewalls allow or block traffic based on rules like ports and addresses."),
 ("3 Architecture", "A VPN mainly provides:", ["Faster internet", "An encrypted tunnel over an untrusted network", "Virus scanning", "Load balancing"], 1, "VPNs encrypt traffic between you and a trusted network over the internet."),
 ("3 Architecture", "A web application firewall (WAF) mainly protects against:", ["Physical theft", "Web attacks like SQL injection and XSS", "Power outages", "Weak Wi-Fi"], 1, "A WAF inspects web traffic for application-layer attacks."),
 ("3 Architecture", "Physically isolating a system from all networks is called:", ["Air gapping", "Sandboxing", "Tunneling", "Hardening"], 0, "An air-gapped system has no network connection to attack."),
 ("3 Architecture", "Which backup copies only files changed since the LAST FULL backup?", ["Full", "Incremental", "Differential", "Snapshot"], 2, "Differential = changes since the last full. Incremental = changes since the last backup of any kind."),
 ("3 Architecture", "A recovery site with power and network but no equipment ready is a:", ["Hot site", "Warm site", "Cold site", "Mobile site"], 2, "Cold sites are cheapest and slowest to bring online; hot sites are ready to go."),
 ("3 Architecture", "Disabling unused services and changing default passwords is called:", ["Hardening", "Fuzzing", "Sandboxing", "Jailbreaking"], 0, "Hardening reduces the attack surface of a system."),
 ("3 Architecture", "Which port does HTTPS use by default?", ["21", "22", "80", "443"], 3, "HTTPS uses 443. HTTP is 80, SSH 22, FTP 21."),
 ("4 Operations", "Endpoint detection and response (EDR) mainly monitors:", ["Network cables", "Computers and devices for suspicious behavior", "Email spam", "Building doors"], 1, "EDR watches endpoints for threats and helps respond to them."),
 ("4 Operations", "Removing access promptly when an employee leaves is part of:", ["Onboarding", "Offboarding", "Job rotation", "Tokenization"], 1, "Offboarding disables accounts and collects equipment."),
 ("4 Operations", "Logging in once to access many apps is:", ["MFA", "SSO", "RBAC", "PAM"], 1, "Single sign-on uses one login for many services."),
 ("4 Operations", "Giving access based on job role, like 'Payroll Clerk', is:", ["DAC", "RBAC", "MAC", "ABAC"], 1, "Role-based access control assigns permissions to roles, then users to roles."),
 ("4 Operations", "Which incident response step isolates an infected computer from the network?", ["Preparation", "Containment", "Recovery", "Lessons learned"], 1, "Containment stops the incident from spreading."),
 ("4 Operations", "Attacking a system with permission to find weaknesses is a:", ["Vulnerability scan", "Penetration test", "Audit log", "Risk register"], 1, "Pen tests actively exploit weaknesses; vulnerability scans only identify them."),
 ("4 Operations", "A scanner reports a vulnerability that isn't actually there. This is a:", ["False positive", "False negative", "True positive", "Zero-day"], 0, "False positive = an alert for something that isn't real."),
 ("4 Operations", "Extra controls for admin and other high-power accounts are called:", ["PAM", "DLP", "NAC", "CASB"], 0, "Privileged access management (PAM) protects and monitors powerful accounts."),
 ("5 Program management", "The maximum time a system can be down before serious harm is the:", ["RPO", "RTO", "MTBF", "SLE"], 1, "Recovery time objective (RTO) is how fast you must recover."),
 ("5 Program management", "The most data loss you can accept, measured in time, is the:", ["RPO", "RTO", "MTTR", "ALE"], 0, "Recovery point objective (RPO), e.g. losing at most 4 hours of data."),
 ("5 Program management", "Deciding not to do a risky activity at all is which risk response?", ["Avoid", "Transfer", "Accept", "Mitigate"], 0, "Avoidance removes the risk by not doing the activity."),
 ("5 Program management", "A document listing risks, their owners and status is a:", ["Risk register", "SLA", "Playbook", "Runbook"], 0, "The risk register tracks risks and how they're handled."),
 ("5 Program management", "Checking a vendor's security practices before hiring them is:", ["Due diligence", "Due care", "Attestation", "Offboarding"], 0, "Due diligence is investigating before you commit."),
 ("5 Program management", "A US law protecting the privacy of health information is:", ["GDPR", "HIPAA", "SOX", "PCI DSS"], 1, "HIPAA covers health data. PCI DSS covers card data, GDPR is EU privacy, SOX covers financial reporting."),
]
LETTERS = "ABCD"

# ---- Scenario questions (longer, exam-style) ----
SCENARIO_START = len(QUIZ)
QUIZ += [
 ("1 General concepts", "A company wants proof that a contract emailed by its CFO really came from her and wasn't changed on the way. Which should the CFO use?", ["Encrypt the email with the recipient's public key", "Digitally sign the email with her private key", "Hash the email and send the hash separately", "Send it over a VPN"], 1, "A digital signature (made with the sender's private key) gives integrity, authentication and non-repudiation. Encryption alone only gives confidentiality."),
 ("1 General concepts", "Badge readers at a warehouse were down for a day, so guards checked IDs and logged visitors by hand instead. What kind of control were the guards?", ["Compensating", "Deterrent", "Corrective", "Directive"], 0, "A compensating control is an alternative used when the main control isn't available or practical."),
 ("1 General concepts", "A web server's TLS certificate was stolen. The admin issues a new one, but browsers might still trust the stolen one until it expires. What should the admin do next?", ["Ask the CA to revoke the old certificate", "Change the server's IP address", "Extend the new certificate's expiration", "Turn off HTTPS"], 0, "Revoking the stolen certificate puts it on the CRL/OCSP so clients stop trusting it."),
 ("1 General concepts", "An admin wants to stop attackers from using a precomputed rainbow table against a leaked password database. What should have been done when storing the passwords?", ["Encrypt them with AES", "Salt them before hashing", "Store them in a hidden folder", "Use longer usernames"], 1, "A unique salt per password makes precomputed rainbow tables useless."),
 ("1 General concepts", "A developer pushed an untested update to the live store website on a Friday night and it crashed checkout. Which process would most likely have prevented this?", ["Change management with testing and approval", "Data loss prevention", "Network segmentation", "Multifactor authentication"], 0, "Change management requires testing, approval, a schedule and a backout plan before changes go live."),
 ("2 Threats", "Several employees get an email that looks like it's from HR, with a link to 'update your direct deposit.' The link goes to hr-paylogin.net. What is the BEST first action for the security team?", ["Block the domain and warn employees", "Reinstall every computer", "Shut down the email server", "Ignore it since it's just spam"], 0, "Quickly block the malicious domain and alert staff to limit how many people enter credentials."),
 ("2 Threats", "A user's account is locked out every morning. Logs show hundreds of failed logins from many countries using the same few common passwords across all accounts. What attack is this?", ["Password spraying", "Pass the hash", "Credential stuffing", "Keylogging"], 0, "A few common passwords tried across many accounts is password spraying."),
 ("2 Threats", "After visiting a popular industry forum, several engineers' computers were infected. The forum had been compromised to target that industry. What type of attack is this?", ["Watering hole", "Typosquatting", "Whaling", "Tailgating"], 0, "A watering hole attack compromises a site the target group is known to visit."),
 ("2 Threats", "Files on a shared drive suddenly have new extensions and can't be opened, and a text file asks for cryptocurrency. What should be done FIRST?", ["Disconnect the affected systems from the network", "Pay the ransom", "Delete the ransom note", "Restart all servers"], 0, "Containment first: isolate infected systems so the ransomware can't spread further."),
 ("2 Threats", "A caller claiming to be from IT asks an employee to read back the code just texted to their phone 'to finish an upgrade.' What is the attacker trying to do?", ["Bypass MFA through social engineering", "Install a keylogger", "Start a DDoS attack", "Perform SQL injection"], 0, "Tricking someone into giving their one-time code lets the attacker get past MFA. That's vishing."),
 ("2 Threats", "A login form accepts the username  admin'--  and logs the attacker in without a password. What is the BEST fix?", ["Use parameterized queries (prepared statements)", "Make the password field longer", "Add a CAPTCHA", "Hide the login page"], 0, "Parameterized queries treat input as data, not SQL code, which stops SQL injection."),
 ("3 Architecture", "A small business runs its public website and its internal accounting server on the same network segment. What change BEST reduces the risk if the website is hacked?", ["Move the website into a screened subnet (DMZ)", "Add more RAM to the web server", "Change the website's color scheme", "Use a longer domain name"], 0, "Putting public servers in a DMZ separates them from internal systems like accounting."),
 ("3 Architecture", "A company needs its systems running again within 1 hour of a disaster and can't lose more than 15 minutes of data. Which statement is correct?", ["RTO is 1 hour and RPO is 15 minutes", "RPO is 1 hour and RTO is 15 minutes", "Both RTO and RPO are 1 hour", "MTBF is 15 minutes"], 0, "RTO = how fast you must recover (1 hour). RPO = how much data you can lose (15 minutes)."),
 ("3 Architecture", "Remote workers need secure access to internal company apps from home Wi-Fi. What is the BEST solution?", ["A VPN with MFA", "Open the apps directly to the internet", "Email them the files instead", "Use WEP on home routers"], 0, "A VPN encrypts the connection, and MFA protects the login."),
 ("3 Architecture", "A store keeps backups on a drive that stays plugged into the server. Ransomware encrypted the server AND the backups. What would have protected the backups?", ["Offline or immutable backups", "A bigger backup drive", "Backing up more often to the same drive", "Renaming the backup folder"], 0, "Backups that are offline, air-gapped or immutable can't be encrypted by ransomware on the server."),
 ("3 Architecture", "A company moves its email to a cloud provider. A user's mailbox is breached because their account had no MFA. Who was responsible for enabling MFA?", ["The customer (the company)", "The cloud provider", "The user's internet provider", "Nobody; it's optional"], 0, "In the shared responsibility model, the customer manages its own accounts, identities and settings like MFA."),
 ("4 Operations", "The SIEM alerts that a cashier's account logged in from Michigan and from overseas 10 minutes apart. What is this called, and what does it suggest?", ["Impossible travel; a likely compromised account", "Normal roaming", "A false negative", "A DDoS attack"], 0, "Logins too far apart to travel between in time point to stolen credentials."),
 ("4 Operations", "An employee is moving from Accounting to Shipping. A year later, they still have access to the payroll system. What problem does this show?", ["Permission creep from missing access reviews", "Least privilege working correctly", "Separation of duties", "Too much MFA"], 0, "Access piles up when it isn't removed on role changes. Regular access reviews prevent permission creep."),
 ("4 Operations", "During an incident, an analyst wants to collect evidence from a running computer. Which should be collected FIRST?", ["RAM (memory)", "The hard drive", "Printed reports", "Backup tapes"], 0, "Order of volatility: memory disappears when power is lost, so collect it first."),
 ("4 Operations", "A vulnerability scan finds a critical flaw on a server, but the vendor patch breaks a needed app. What is the BEST short-term action?", ["Apply compensating controls like isolating the server until it can be patched", "Ignore the finding", "Delete the scan report", "Disable all logging"], 0, "When you can't patch yet, reduce risk with compensating controls (segmentation, firewall rules, monitoring) and plan the fix."),
 ("4 Operations", "After a malware incident is cleaned up and systems are restored, the team meets to discuss what went well and what to improve. Which phase is this?", ["Lessons learned", "Preparation", "Detection", "Containment"], 0, "Lessons learned is the final phase; it improves the plan for next time."),
 ("4 Operations", "An online store wants to stop employees from accidentally emailing spreadsheets full of customer card numbers. Which tool fits BEST?", ["DLP (data loss prevention)", "An IDS", "A load balancer", "A honeypot"], 0, "DLP spots sensitive data like card numbers and blocks or flags it from leaving."),
 ("5 Program management", "A laptop worth $1,500 is stolen about twice a year. What is the annual loss expectancy (ALE)?", ["$3,000", "$1,500", "$750", "$15,000"], 0, "ALE = SLE × ARO = $1,500 × 2 = $3,000."),
 ("5 Program management", "A small online shop processes customer credit cards on its website. Which standard must it follow?", ["PCI DSS", "HIPAA", "FERPA", "GDPR only"], 0, "PCI DSS applies to any business that stores, processes or sends payment card data."),
 ("5 Program management", "A company decides a minor risk costs more to fix than the damage it could cause, so it documents the risk and does nothing else. What is this?", ["Risk acceptance", "Risk avoidance", "Risk transfer", "Risk mitigation"], 0, "Accepting a risk is a valid, documented choice when fixing it costs more than the risk itself."),
 ("5 Program management", "Before signing with a new payment processor, a store reviews the vendor's security audit reports and breach history. What is this?", ["Vendor due diligence (third-party risk assessment)", "Penetration testing", "Change management", "Offboarding"], 0, "Checking a vendor's security before signing is third-party risk management / due diligence."),
]


def _shuffle_answers(start):
    """Mix up answer positions (the same way every time) so the right answer isn't always A."""
    for i in range(start, len(QUIZ)):
        dom, q, opts, ans, why = QUIZ[i]
        order = list(range(4))
        random.Random(i * 7919).shuffle(order)
        QUIZ[i] = (dom, q, [opts[k] for k in order], order.index(ans), why)


_shuffle_answers(SCENARIO_START)

# ---- Score tracker (saved in quiz_scores.json next to the bot) ----
SCORE_FILE = pathlib.Path(__file__).with_name("quiz_scores.json")
DOMAINS = ["1 General concepts", "2 Threats", "3 Architecture", "4 Operations", "5 Program management"]


def load_scores():
    try:
        return json.loads(SCORE_FILE.read_text())
    except Exception:
        return {}


def save_scores(data):
    try:
        SCORE_FILE.write_text(json.dumps(data))
    except Exception as e:
        print(f"⚠️ Couldn't save quiz scores: {e}")


def record_answer(user_id, message_id, qi, correct):
    """Counts only the first answer to each posted question. Returns (counted, user_stats)."""
    data = load_scores()
    u = data.setdefault(str(user_id), {"answered": 0, "correct": 0, "domains": {}, "days": [], "seen": [], "missed": []})
    key = str(message_id)
    if key in u["seen"]:
        return False, u
    u["seen"] = (u["seen"] + [key])[-500:]
    d = u["domains"].setdefault(QUIZ[qi][0], [0, 0])
    u["answered"] += 1
    d[1] += 1
    if correct:
        u["correct"] += 1
        d[0] += 1
        u["missed"] = [m for m in u["missed"] if m != qi]
    elif qi not in u["missed"]:
        u["missed"] = (u["missed"] + [qi])[-60:]
    today = datetime.datetime.now(EASTERN).date().isoformat()
    if today not in u["days"]:
        u["days"] = (u["days"] + [today])[-400:]
    save_scores(data)
    return True, u


def streak(days):
    have, n = set(days), 0
    d = datetime.datetime.now(EASTERN).date()
    if d.isoformat() not in have:
        d -= datetime.timedelta(days=1)   # not answering yet today doesn't break the streak
    while d.isoformat() in have:
        n += 1
        d -= datetime.timedelta(days=1)
    return n


def pct(c, t):
    return round(c / t * 100) if t else 0


def quiz_embed(qi):
    dom, q, opts, _, _ = QUIZ[qi]
    e = discord.Embed(title="🔐 Security+ practice question", color=CAREER_COLOR,
                      description=f"**{q}**\n\n" + "\n".join(f"**{LETTERS[i]}.** {o}" for i, o in enumerate(opts)))
    e.set_footer(text=f"Domain {dom}{' · Scenario' if qi >= SCENARIO_START else ''} · tap a letter to answer (only you see if you're right)")
    return e


def quiz_view(qi):
    v = discord.ui.View(timeout=None)
    for i in range(4):
        v.add_item(discord.ui.Button(label=LETTERS[i], style=discord.ButtonStyle.secondary, custom_id=f"quiz:{qi}:{i}"))
    return v


def todays_question():
    day = datetime.datetime.now(EASTERN).date().toordinal()
    return day % len(QUIZ)


@bot.event
async def on_interaction(interaction: discord.Interaction):
    cid = (interaction.data or {}).get("custom_id", "") if interaction.type == discord.InteractionType.component else ""
    if cid.startswith("exam:"):
        await handle_exam(interaction, cid)
        return
    if cid.startswith("bj:"):
        await handle_blackjack(interaction, cid)
        return
    if cid.startswith("he:"):
        await handle_holdem(interaction, cid)
        return
    if cid.startswith("lir:"):
        await handle_lir(interaction, cid)
        return
    if cid.startswith("dv:"):
        await handle_dv(interaction, cid)
        return
    if cid.startswith("rl:"):
        await handle_roulette(interaction, cid)
        return
    if cid.startswith("sl:"):
        await handle_slots(interaction, cid)
        return
    if not cid.startswith("quiz:"):
        return
    _, qi, pick = cid.split(":")
    qi, pick = int(qi), int(pick)
    if qi >= len(QUIZ):
        await interaction.response.send_message("That question is no longer in the bank.", ephemeral=True)
        return
    _, _, opts, answer, why = QUIZ[qi]
    counted, u = record_answer(interaction.user.id, interaction.message.id if interaction.message else f"q{qi}", qi, pick == answer)
    if pick == answer:
        msg = f"✅ **Correct!** {LETTERS[answer]}. {opts[answer]}\n{why}"
    else:
        msg = f"❌ Not quite. You picked {LETTERS[pick]}. The answer is **{LETTERS[answer]}. {opts[answer]}**\n{why}"
    msg += (f"\n\n📊 {u['correct']}/{u['answered']} correct ({pct(u['correct'], u['answered'])}%) · 🔥 {streak(u['days'])}-day streak"
            if counted else "\n\n(You already answered this one, so it didn't count again.)")
    await interaction.response.send_message(msg, ephemeral=True)


@tasks.loop(time=QUIZ_TIME)
async def daily_quiz():
    qi = todays_question()
    for g in bot.guilds:
        channel = next((c for name in QUIZ_CHANNEL_NAMES for c in g.text_channels if c.name == name), None)
        if channel:
            try:
                await channel.send(content="📚 **Daily Security+ question**", embed=quiz_embed(qi), view=quiz_view(qi))
            except discord.Forbidden:
                print(f"⚠️ No permission to post the quiz in #{channel.name}")


@daily_quiz.before_loop
async def before_quiz():
    await bot.wait_until_ready()


@bot.tree.command(name="quiz", description="Get a Security+ practice question")
@app_commands.describe(domain="Optional: pick a domain")
@app_commands.choices(domain=[app_commands.Choice(name=d, value=d) for d in DOMAINS] +
                             [app_commands.Choice(name="Scenario questions (exam-style)", value="scenario"),
                              app_commands.Choice(name="My weakest domain", value="weakest"),
                              app_commands.Choice(name="Questions I missed", value="missed")])
async def quiz(interaction: discord.Interaction, domain: Optional[app_commands.Choice[str]] = None):
    u = load_scores().get(str(interaction.user.id), {})
    choice = domain.value if domain else None
    if choice == "weakest":
        st = u.get("domains", {})
        choice = min(DOMAINS, key=lambda d: (pct(*st[d]) if d in st else -1))
    if choice == "missed":
        pool = [i for i in u.get("missed", []) if i < len(QUIZ)]
        if not pool:
            await interaction.response.send_message("No missed questions to review. 🎉 Try /quiz for a new one.", ephemeral=True)
            return
    elif choice == "scenario":
        pool = list(range(SCENARIO_START, len(QUIZ)))
    else:
        pool = [i for i, x in enumerate(QUIZ) if not choice or x[0] == choice]
    qi = random.choice(pool)
    await interaction.response.send_message(embed=quiz_embed(qi), view=quiz_view(qi), ephemeral=True)



@bot.tree.command(name="quizscore", description="Your Security+ practice score, by domain")
async def quizscore(interaction: discord.Interaction):
    u = load_scores().get(str(interaction.user.id))
    if not u or not u.get("answered"):
        await interaction.response.send_message("No answers yet. Try /quiz to get started! 📚", ephemeral=True)
        return
    lines = []
    for d in DOMAINS:
        c, t = u["domains"].get(d, [0, 0])
        k = round(pct(c, t) / 10)
        lines.append(f"`{'█' * k}{'░' * (10 - k)}` **{d}**: {c}/{t}" + (f" ({pct(c, t)}%)" if t else " (not tried yet)"))
    tried = {d: u["domains"][d] for d in DOMAINS if u["domains"].get(d, [0, 0])[1]}
    weakest = min(tried, key=lambda d: pct(*tried[d])) if tried else None
    overall = pct(u["correct"], u["answered"])
    text = f"**{u['correct']}/{u['answered']} correct ({overall}%)** · 🔥 {streak(u['days'])}-day streak\n\n" + "\n".join(lines)
    if weakest:
        text += f"\n\n🎯 Focus next on **{weakest}**: `/quiz domain: My weakest domain`"
    if u.get("missed"):
        text += f"\n🔁 {len(u['missed'])} missed question(s) to review: `/quiz domain: Questions I missed`"
    if overall >= 80 and u["answered"] >= 30:
        text += "\n\n✅ 80%+ over 30+ questions: a good sign you're ready for full practice exams."
    await interaction.response.send_message(embed=discord.Embed(title="📊 Security+ practice score", description=text, color=CAREER_COLOR), ephemeral=True)



# ---- Practice exam: a run of questions with a final score ----
EXAMS = {}   # user id -> current exam (kept in memory; a restart ends unfinished exams)


def exam_question_embed(ex):
    qi = ex["qs"][ex["i"]]
    dom, q, opts, _, _ = QUIZ[qi]
    e = discord.Embed(title=f"📝 Practice exam · Question {ex['i'] + 1} of {len(ex['qs'])}", color=CAREER_COLOR,
                      description=f"**{q}**\n\n" + "\n".join(f"**{LETTERS[i]}.** {o}" for i, o in enumerate(opts)))
    e.set_footer(text=f"Domain {dom} · Score so far: {ex['correct']}/{ex['i']}")
    return e


def exam_buttons(kind):
    v = discord.ui.View(timeout=None)
    if kind == "answer":
        for i in range(4):
            v.add_item(discord.ui.Button(label=LETTERS[i], style=discord.ButtonStyle.secondary, custom_id=f"exam:{i}"))
    else:
        v.add_item(discord.ui.Button(label=kind, style=discord.ButtonStyle.primary, custom_id="exam:next"))
    v.add_item(discord.ui.Button(label="End exam", style=discord.ButtonStyle.danger, custom_id="exam:quit"))
    return v


def exam_results_embed(ex):
    n, c = ex["i"], ex["correct"]
    score = pct(c, n)
    by = {}
    for qi, ok in ex["log"]:
        d = by.setdefault(QUIZ[qi][0], [0, 0]); d[1] += 1; d[0] += ok
    lines = [f"• **{d}**: {v[0]}/{v[1]} ({pct(*v)}%)" for d, v in sorted(by.items())]
    weakest = min(by, key=lambda d: pct(*by[d])) if by else None
    verdict = ("🏆 Excellent! You're in good shape for this material." if score >= 90 else
               "✅ Passing range. Keep reviewing your weak spots." if score >= 80 else
               "📈 Getting there. Aim for 80%+ before the real exam." if score >= 60 else
               "📚 Keep studying. Review the domains below and try again.")
    text = f"**{c}/{n} correct ({score}%)**\n{verdict}\n\n" + "\n".join(lines)
    if weakest and pct(*by[weakest]) < 100:
        text += f"\n\n🎯 Weakest area: **{weakest}**. Practice it with `/quiz domain: My weakest domain`."
    missed = [qi for qi, ok in ex["log"] if not ok]
    if missed:
        text += f"\n🔁 Review your {len(missed)} miss(es) with `/quiz domain: Questions I missed`."
    text += "\n\n_Practice only: the real Security+ exam is scored differently and includes hands-on questions._"
    return discord.Embed(title="📝 Practice exam results", description=text, color=CAREER_COLOR)


async def handle_exam(interaction, cid):
    ex = EXAMS.get(interaction.user.id)
    if not ex:
        await interaction.response.edit_message(content="This exam has ended (StoltBot may have restarted). Start a new one with `/exam`.",
                                                embed=None, view=None)
        return
    action = cid.split(":")[1]
    if action == "quit":
        EXAMS.pop(interaction.user.id, None)
        if ex["i"] == 0:
            await interaction.response.edit_message(content="Exam ended.", embed=None, view=None)
        else:
            await interaction.response.edit_message(content=None, embed=exam_results_embed(ex), view=None)
        return
    if action == "next":
        if ex["i"] >= len(ex["qs"]):
            EXAMS.pop(interaction.user.id, None)
            await interaction.response.edit_message(content=None, embed=exam_results_embed(ex), view=None)
        else:
            await interaction.response.edit_message(content=None, embed=exam_question_embed(ex), view=exam_buttons("answer"))
        return
    # an answer A-D
    if ex.get("answered"):
        await interaction.response.defer()
        return
    pick = int(action)
    qi = ex["qs"][ex["i"]]
    _, _, opts, answer, why = QUIZ[qi]
    ok = pick == answer
    record_answer(interaction.user.id, f"exam:{ex['id']}:{ex['i']}", qi, ok)
    ex["log"].append((qi, ok))
    ex["correct"] += ok
    ex["i"] += 1
    e = exam_question_embed({**ex, "i": ex["i"] - 1})
    e.add_field(name="✅ Correct!" if ok else f"❌ You picked {LETTERS[pick]}", value=f"**{LETTERS[answer]}. {opts[answer]}**\n{why}", inline=False)
    e.set_footer(text=f"Score: {ex['correct']}/{ex['i']}")
    last = ex["i"] >= len(ex["qs"])
    await interaction.response.edit_message(embed=e, view=exam_buttons("See results" if last else "Next question ➡️"))


@bot.tree.command(name="exam", description="Take a practice exam: a run of Security+ questions with a final score")
@app_commands.describe(length="How many questions", domain="Optional: only one domain", style="Mix, or scenario questions only")
@app_commands.choices(length=[app_commands.Choice(name="10 questions (~10 min)", value=10),
                              app_commands.Choice(name="20 questions (~20 min)", value=20),
                              app_commands.Choice(name="40 questions (~40 min)", value=40)],
                      domain=[app_commands.Choice(name=d, value=d) for d in DOMAINS],
                      style=[app_commands.Choice(name="Mixed (all questions)", value="mixed"),
                             app_commands.Choice(name="Scenario only (exam-style)", value="scenario")])
async def exam(interaction: discord.Interaction, length: Optional[app_commands.Choice[int]] = None,
               domain: Optional[app_commands.Choice[str]] = None, style: Optional[app_commands.Choice[str]] = None):
    n = length.value if length else 10
    pool = [i for i, x in enumerate(QUIZ) if (not domain or x[0] == domain.value)
            and (not style or style.value != "scenario" or i >= SCENARIO_START)]
    if domain:
        qs = random.sample(pool, min(n, len(pool)))
    else:   # mix the domains evenly
        by = {d: random.sample([i for i in pool if QUIZ[i][0] == d], len([i for i in pool if QUIZ[i][0] == d])) for d in DOMAINS}
        qs = []
        while len(qs) < min(n, len(pool)):
            for d in DOMAINS:
                if by[d] and len(qs) < n:
                    qs.append(by[d].pop())
        random.shuffle(qs)
    ex = {"id": random.randint(100000, 999999), "qs": qs, "i": 0, "correct": 0, "log": []}
    EXAMS[interaction.user.id] = ex
    await interaction.response.send_message(embed=exam_question_embed(ex), view=exam_buttons("answer"), ephemeral=True)



# ===== PRACTICE TEST SCORE LOG (Udemy and other full-length tests) =====
PT_FILE = pathlib.Path(__file__).with_name("practice_tests.json")
practicetest = app_commands.Group(name="practicetest", description="Log and track full-length practice test scores")


def pt_load():
    try:
        return json.loads(PT_FILE.read_text())
    except Exception:
        return {}


def pt_save(data):
    try:
        PT_FILE.write_text(json.dumps(data))
    except Exception as e:
        print(f"⚠️ Couldn't save practice tests: {e}")


def pt_summary(tests):
    lines = []
    for t in tests[-12:]:
        p = t["pct"]
        k = round(p / 10)
        mark = "✅" if p >= 80 else "🟡" if p >= 70 else "🔴"
        lines.append(f"{mark} `{'█' * k}{'░' * (10 - k)}` **{p}%** · {t['name']} ({t['score']}/{t['total']}) · {t['date']}"
                     + (f"\n  ↳ weak spots: {t['weak']}" if t.get("weak") else ""))
    avg = round(sum(t["pct"] for t in tests) / len(tests))
    best = max(t["pct"] for t in tests)
    recent = tests[-3:]
    trend = ""
    if len(tests) >= 2:
        diff = tests[-1]["pct"] - tests[-2]["pct"]
        trend = f" · last change {'▲' if diff >= 0 else '▼'} {abs(diff)} pts"
    passing = len({t["name"].lower() for t in tests if t["pct"] >= 80})
    head = f"**{len(tests)} test(s)** · average **{avg}%** · best **{best}%**{trend}\n"
    head += (f"🎯 {passing} different test(s) at 80%+. " + ("Goal reached! ✅" if passing >= 2 else f"{2 - passing} more to go for the roadmap goal.")) + "\n\n"
    return head + "\n".join(lines)


@practicetest.command(name="log", description="Log a practice test score")
@app_commands.describe(name="Which test, e.g. Udemy Test 1", score="How many you got right, e.g. 47",
                       total="How many questions (default 60)", weak="Optional: topics or domains you missed most")
async def pt_log(interaction: discord.Interaction, name: str, score: app_commands.Range[int, 0, 500],
                 total: app_commands.Range[int, 1, 500] = 60, weak: Optional[str] = None):
    if score > total:
        await interaction.response.send_message("❌ The score can't be higher than the number of questions.", ephemeral=True)
        return
    data = pt_load()
    tests = data.setdefault(str(interaction.user.id), [])
    p = round(score / total * 100)
    tests.append({"name": name.strip(), "score": score, "total": total, "pct": p,
                  "date": datetime.datetime.now(EASTERN).strftime("%b %d"), "weak": (weak or "").strip()})
    pt_save(data)
    note = ("🏆 80%+! That's exam-ready territory for this test." if p >= 80 else
            "🟡 Close. Review the explanations for the ones you missed." if p >= 70 else
            "📚 Good baseline. Study your weak spots, then retake it.")
    msg = f"📝 Logged **{name}**: {score}/{total} (**{p}%**)\n{note}"
    # Two different tests at 80%+ → check off the roadmap step automatically
    passing = {t["name"].lower() for t in tests if t["pct"] >= 80}
    if len(passing) >= 2 and BUDGET is not None:
        try:
            await budget_call("c_done", step="s9")
            msg += "\n\n✅ Roadmap step checked off: **Score 80%+ on two full practice exams**. Next: schedule the exam! ★"
        except Exception:
            pass
    if weak:
        msg += "\n\n💡 Tip: tell Claude these weak topics to get extra StoltBot practice questions on them."
    await interaction.response.send_message(msg, ephemeral=True)


@practicetest.command(name="history", description="Your practice test scores and progress")
async def pt_history(interaction: discord.Interaction):
    tests = pt_load().get(str(interaction.user.id), [])
    if not tests:
        await interaction.response.send_message("No practice tests logged yet. Use `/practicetest log` after your next one.", ephemeral=True)
        return
    await interaction.response.send_message(embed=discord.Embed(title="📈 Practice test progress", description=pt_summary(tests)[:4000],
                                                                color=CAREER_COLOR), ephemeral=True)


@practicetest.command(name="undo", description="Remove the last score you logged")
async def pt_undo(interaction: discord.Interaction):
    data = pt_load()
    tests = data.get(str(interaction.user.id), [])
    if not tests:
        await interaction.response.send_message("Nothing to remove.", ephemeral=True)
        return
    t = tests.pop()
    pt_save(data)
    await interaction.response.send_message(f"↩️ Removed **{t['name']}** ({t['pct']}%).", ephemeral=True)


bot.tree.add_command(practicetest)

# ===== BLACKJACK TRAINER (practice chips only, with basic-strategy coaching) =====
# Rules: 6-deck shoe, dealer stands on soft 17, blackjack pays 3:2, double on any first two cards,
# double after split allowed, one split per hand, split aces get one card each. No real money.
BJ_FILE = pathlib.Path(__file__).with_name("blackjack_stats.json")
BJ_START_CHIPS = 1000
BJ_COLOR = 0x1F8B4C
BJ_GAMES = {}  # user id -> current game (in memory; a restart ends unfinished hands)
SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
ACTION_NAMES = {"H": "Hit", "S": "Stand", "D": "Double down", "P": "Split"}


def bj_load():
    try:
        return json.loads(BJ_FILE.read_text())
    except Exception:
        return {}


def bj_save(data):
    BJ_FILE.write_text(json.dumps(data, indent=1))


def bj_user(data, uid):
    return data.setdefault(str(uid), {"chips": BJ_START_CHIPS, "hands": 0, "wins": 0, "losses": 0, "pushes": 0,
                                      "blackjacks": 0, "decisions": 0, "correct": 0, "best": BJ_START_CHIPS})


def new_shoe():
    shoe = [(r, s) for _ in range(6) for s in SUITS for r in RANKS]
    random.shuffle(shoe)
    return shoe


def card_value(card):
    r = card[0]
    return 11 if r == "A" else 10 if r in ("10", "J", "Q", "K") else int(r)


def hand_total(cards):
    """Returns (total, is_soft)."""
    total = sum(card_value(c) for c in cards)
    aces = sum(1 for c in cards if c[0] == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total, aces > 0


def show_cards(cards):
    return "  ".join(f"`{r}{s}`" for r, s in cards)


def is_blackjack(cards):
    return len(cards) == 2 and hand_total(cards)[0] == 21


def basic_strategy(cards, up, can_double, can_split):
    """Basic strategy for 6 decks, dealer stands on soft 17, double after split. Returns (action, reason)."""
    d = 11 if up[0] == "A" else card_value(up)  # dealer upcard value, Ace = 11
    weak = 2 <= d <= 6
    total, soft = hand_total(cards)
    # Pairs
    if can_split and len(cards) == 2 and card_value(cards[0]) == card_value(cards[1]):
        v = card_value(cards[0])
        pair_rules = {
            11: (True, "Always split Aces: two chances at 21 beats one soft 12."),
            8: (True, "Always split 8s: 16 is the worst hand in blackjack, but 8 is a decent start."),
            10: (False, "Never split 10s: 20 is already a great hand."),
            9: (d not in (7, 10, 11), "Split 9s unless the dealer shows 7, 10 or Ace (then 18 is good enough to stand)."),
            7: (d <= 7, "Split 7s against a dealer 2–7."),
            6: (d <= 6, "Split 6s against a weak dealer card (2–6)."),
            4: (d in (5, 6), "Split 4s only against a dealer 5 or 6."),
            3: (d <= 7, "Split 3s against a dealer 2–7."),
            2: (d <= 7, "Split 2s against a dealer 2–7."),
        }
        if v in pair_rules and pair_rules[v][0]:
            return "P", pair_rules[v][1]
        if v == 10:
            return "S", pair_rules[10][1]
        if v == 9 and d in (7, 10, 11):
            return "S", pair_rules[9][1]
        # otherwise fall through and play it as a normal total (5,5 = hard 10, etc.)
    # Soft hands
    if soft and total <= 21:
        if total >= 19:
            return "S", f"Soft {total} is strong. Stand."
        if total == 18:
            if 3 <= d <= 6:
                return ("D", "Soft 18 vs a weak dealer card: double, you can't bust on one card.") if can_double \
                    else ("S", "Soft 18 vs a weak dealer card: stand (doubling isn't allowed now).")
            if d in (2, 7, 8):
                return "S", "Soft 18 vs a dealer 2, 7 or 8: stand."
            return "H", "Soft 18 vs a dealer 9, 10 or Ace: hit. 18 usually loses to those, and an Ace means you can't bust."
        lo = {17: 3, 16: 4, 15: 4, 14: 5, 13: 5}[total]
        if lo <= d <= 6 and can_double:
            return "D", f"Soft {total} vs a dealer {lo}–6: double down while the dealer is likely to bust."
        return "H", f"Soft {total}: hit. Your Ace counts as 1 if needed, so you can't bust on one card."
    # Hard hands
    if total >= 17:
        return "S", f"Hard {total}: always stand. Too much risk of busting."
    if 13 <= total <= 16:
        return ("S", f"Hard {total} vs a weak dealer card (2–6): stand and let the dealer bust.") if weak \
            else ("H", f"Hard {total} vs a strong dealer card (7–Ace): hit. The dealer will likely make 17+.")
    if total == 12:
        return ("S", "Hard 12 vs a dealer 4–6: stand. The dealer busts often.") if 4 <= d <= 6 \
            else ("H", "Hard 12 vs a dealer 2, 3 or 7–Ace: hit. Only a 10-card busts you.")
    if total == 11:
        if d != 11 and can_double:
            return "D", "11 is the best double: any 10-card gives you 21."
        return "H", "11 vs an Ace: just hit." if d == 11 else "11: hit (doubling isn't allowed now)."
    if total == 10:
        if d <= 9 and can_double:
            return "D", "10 vs a dealer 2–9: double down."
        return "H", "10: hit."
    if total == 9:
        if 3 <= d <= 6 and can_double:
            return "D", "9 vs a dealer 3–6: double down."
        return "H", "9: hit."
    return "H", f"Hard {total}: always hit. You can't bust."


def bj_draw(g):
    if len(g["shoe"]) < 52:
        g["shoe"] = new_shoe()
        g["note"] = "🔀 New shoe shuffled."
    return g["shoe"].pop()


def bj_current(g):
    return g["hands"][g["idx"]] if g["idx"] < len(g["hands"]) else None


def bj_options(g):
    h = bj_current(g)
    if not h:
        return False, False
    committed = sum(x["bet"] for x in g["hands"])
    room = g["chips"] - committed >= h["bet"]
    can_double = len(h["cards"]) == 2 and room and not h.get("split_aces")
    can_split = (len(h["cards"]) == 2 and len(g["hands"]) == 1 and room
                 and card_value(h["cards"][0]) == card_value(h["cards"][1]))
    return can_double, can_split


def bj_embed(g, reveal=False):
    e = discord.Embed(title="🃏 Blackjack trainer", color=BJ_COLOR)
    if reveal:
        dt, _ = hand_total(g["dealer"])
        e.add_field(name=f"Dealer · {dt}" + (" (bust)" if dt > 21 else ""), value=show_cards(g["dealer"]), inline=False)
    else:
        e.add_field(name="Dealer", value=f"{show_cards(g['dealer'][:1])}  `🂠`", inline=False)
    for i, h in enumerate(g["hands"]):
        t, soft = hand_total(h["cards"])
        label = f"{'👉 ' if (i == g['idx'] and not g['over']) else ''}Your hand{f' {i + 1}' if len(g['hands']) > 1 else ''}"
        label += f" · {'soft ' if soft and t < 21 else ''}{t}" + (" (bust)" if t > 21 else "")
        label += f" · bet {h['bet']}" + (" (doubled)" if h.get("doubled") else "")
        val = show_cards(h["cards"]) + (f"\n**{h['result']}**" if h.get("result") else "")
        e.add_field(name=label, value=val, inline=False)
    if g.get("coach"):
        e.add_field(name="🎓 Coach", value=g["coach"], inline=False)
    footer = f"Chips: {g['chips']}"
    if g.get("note"):
        footer += f" · {g['note']}"
    footer += " · Practice chips only"
    e.set_footer(text=footer)
    return e


def bj_view(g):
    v = discord.ui.View(timeout=None)
    if g["over"]:
        v.add_item(discord.ui.Button(label=f"Deal again ({g['bet']})", style=discord.ButtonStyle.success, custom_id="bj:again"))
        v.add_item(discord.ui.Button(label="Done", style=discord.ButtonStyle.secondary, custom_id="bj:quit"))
        return v
    can_double, can_split = bj_options(g)
    v.add_item(discord.ui.Button(label="Hit", style=discord.ButtonStyle.primary, custom_id="bj:H"))
    v.add_item(discord.ui.Button(label="Stand", style=discord.ButtonStyle.secondary, custom_id="bj:S"))
    v.add_item(discord.ui.Button(label="Double", style=discord.ButtonStyle.secondary, custom_id="bj:D", disabled=not can_double))
    v.add_item(discord.ui.Button(label="Split", style=discord.ButtonStyle.secondary, custom_id="bj:P", disabled=not can_split))
    v.add_item(discord.ui.Button(label="💡 Hint", style=discord.ButtonStyle.secondary, custom_id="bj:hint"))
    return v


def bj_deal(g):
    g.update(over=False, idx=0, coach="", note="")
    g["hands"] = [{"cards": [bj_draw(g), bj_draw(g)], "bet": g["bet"]}]
    g["dealer"] = [bj_draw(g), bj_draw(g)]
    p_bj, d_bj = is_blackjack(g["hands"][0]["cards"]), is_blackjack(g["dealer"])
    if p_bj or d_bj:
        h = g["hands"][0]
        if p_bj and d_bj:
            h["result"], h["net"] = "Push: you both have blackjack.", 0
        elif p_bj:
            h["result"], h["net"] = f"🎉 BLACKJACK! You win {g['bet'] * 3 // 2}.", g["bet"] * 3 // 2
            g["blackjack"] = True
        else:
            h["result"], h["net"] = "Dealer has blackjack. You lose.", -g["bet"]
        bj_finish(g)


def bj_advance(g):
    """Move to the next hand that still needs decisions, or finish the round."""
    while g["idx"] < len(g["hands"]):
        h = g["hands"][g["idx"]]
        t, _ = hand_total(h["cards"])
        if h.get("done") or t >= 21:
            h["done"] = True
            g["idx"] += 1
            continue
        return
    # Dealer plays only if a hand is still alive
    if any(hand_total(h["cards"])[0] <= 21 for h in g["hands"]):
        while hand_total(g["dealer"])[0] < 17:
            g["dealer"].append(bj_draw(g))
    dt = hand_total(g["dealer"])[0]
    for h in g["hands"]:
        t = hand_total(h["cards"])[0]
        if t > 21:
            h["result"], h["net"] = f"Bust. You lose {h['bet']}.", -h["bet"]
        elif dt > 21:
            h["result"], h["net"] = f"Dealer busts! You win {h['bet']}.", h["bet"]
        elif t > dt:
            h["result"], h["net"] = f"{t} beats {dt}. You win {h['bet']}.", h["bet"]
        elif t < dt:
            h["result"], h["net"] = f"{dt} beats {t}. You lose {h['bet']}.", -h["bet"]
        else:
            h["result"], h["net"] = f"Push at {t}. Bet returned.", 0
    bj_finish(g)


def bj_finish(g):
    g["over"] = True
    net = sum(h.get("net", 0) for h in g["hands"])
    g["chips"] += net
    data = bj_load()
    u = bj_user(data, g["uid"])
    u["chips"] = g["chips"]
    u["best"] = max(u.get("best", BJ_START_CHIPS), g["chips"])
    for h in g["hands"]:
        u["hands"] += 1
        n = h.get("net", 0)
        u["wins" if n > 0 else "losses" if n < 0 else "pushes"] += 1
    if g.pop("blackjack", False):
        u["blackjacks"] += 1
    u["decisions"] += g.pop("decisions", 0)
    u["correct"] += g.pop("correct", 0)
    bj_save(data)
    g["note"] = f"{'+' if net > 0 else ''}{net} this round" if net else "Even this round"


async def bj_start(interaction, bet, tips):
    data = bj_load()
    u = bj_user(data, interaction.user.id)
    note = ""
    if u["chips"] < bet:
        u["chips"] = BJ_START_CHIPS
        note = f"Out of chips, so you got a fresh {BJ_START_CHIPS}."
        bj_save(data)
    bet = min(bet, u["chips"])
    g = {"uid": interaction.user.id, "chips": u["chips"], "bet": bet, "tips": tips, "shoe": new_shoe()}
    BJ_GAMES[interaction.user.id] = g
    bj_deal(g)
    if note:
        g["note"] = note
    if tips and not g["over"]:
        g["coach"] = "I'll tell you after each move whether it matches basic strategy. Tap 💡 Hint if you're stuck."
    await interaction.response.send_message(embed=bj_embed(g, reveal=g["over"]), view=bj_view(g))
    g["msg_id"] = (await interaction.original_response()).id


async def handle_blackjack(interaction, cid):
    g = BJ_GAMES.get(interaction.user.id)
    if not g or (interaction.message and g.get("msg_id") != interaction.message.id):
        await interaction.response.send_message("That's not your table (or StoltBot restarted). Start your own with `/blackjack play`.",
                                                ephemeral=True)
        return
    action = cid.split(":")[1]
    if action == "quit":
        BJ_GAMES.pop(interaction.user.id, None)
        await interaction.response.edit_message(embed=bj_embed(g, reveal=True), view=None,
                                                content="Thanks for playing! Check your progress with `/blackjack stats`.")
        return
    if action == "again":
        if g["chips"] < g["bet"]:
            g["chips"] = BJ_START_CHIPS
            data = bj_load(); bj_user(data, g["uid"])["chips"] = BJ_START_CHIPS; bj_save(data)
        bj_deal(g)
        g["coach"] = "" if g["over"] else ("Your move. Tap 💡 Hint if you're stuck." if g["tips"] else "")
        await interaction.response.edit_message(embed=bj_embed(g, reveal=g["over"]), view=bj_view(g))
        return
    if g["over"]:
        await interaction.response.defer()
        return
    h = bj_current(g)
    can_double, can_split = bj_options(g)
    best, why = basic_strategy(h["cards"], g["dealer"][0], can_double, can_split)
    if action == "hint":
        g["coach"] = f"💡 Basic strategy says **{ACTION_NAMES[best]}**. {why}"
        await interaction.response.edit_message(embed=bj_embed(g), view=bj_view(g))
        return
    if (action == "D" and not can_double) or (action == "P" and not can_split):
        await interaction.response.defer()
        return
    # Grade the decision
    g["decisions"] = g.get("decisions", 0) + 1
    if action == best:
        g["correct"] = g.get("correct", 0) + 1
        g["coach"] = f"✅ **{ACTION_NAMES[action]}** is right. {why}" if g["tips"] else ""
    else:
        g["coach"] = f"⚠️ You chose **{ACTION_NAMES[action]}**, but basic strategy says **{ACTION_NAMES[best]}**. {why}" if g["tips"] else ""
    # Do it
    if action == "H":
        h["cards"].append(bj_draw(g))
    elif action == "S":
        h["done"] = True
    elif action == "D":
        h["bet"] *= 2
        h["doubled"] = True
        h["cards"].append(bj_draw(g))
        h["done"] = True
    elif action == "P":
        c1, c2 = h["cards"]
        aces = c1[0] == "A"
        g["hands"] = [{"cards": [c1, bj_draw(g)], "bet": h["bet"], "split_aces": aces, "done": aces},
                      {"cards": [c2, bj_draw(g)], "bet": h["bet"], "split_aces": aces, "done": aces}]
    bj_advance(g)
    if g["over"] and g["tips"] and g.get("coach"):
        g["coach"] += "\n\n_Remember: good decisions can still lose a hand. Strategy wins over many hands._"
    await interaction.response.edit_message(embed=bj_embed(g, reveal=g["over"]), view=bj_view(g))


blackjack = app_commands.Group(name="blackjack", description="Practice blackjack with a basic-strategy coach (fake chips)")


@blackjack.command(name="play", description="Deal a hand of practice blackjack")
@app_commands.describe(bet="Practice chips to bet per hand (default 10)", tips="Coach you after every move (default on)")
async def bj_play(interaction: discord.Interaction, bet: app_commands.Range[int, 1, 1000] = 10, tips: bool = True):
    await bj_start(interaction, bet, tips)


@blackjack.command(name="stats", description="Your practice chips, record and strategy accuracy")
async def bj_stats(interaction: discord.Interaction):
    u = bj_user(bj_load(), interaction.user.id)
    acc = pct(u["correct"], u["decisions"]) if u["decisions"] else 0
    grade = ("🏆 Casino-ready!" if acc >= 95 else "✅ Solid. Keep drilling the close calls." if acc >= 85
             else "📈 Getting there. Use 💡 Hint on hands you're unsure of." if acc >= 70 else "📚 Keep practicing with tips on.")
    text = (f"💰 **Chips:** {u['chips']} (best {u.get('best', u['chips'])}, started with {BJ_START_CHIPS})\n"
            f"🃏 **Hands:** {u['hands']} · Won {u['wins']} · Lost {u['losses']} · Pushed {u['pushes']} · Blackjacks {u['blackjacks']}\n"
            f"🎓 **Strategy accuracy:** {u['correct']}/{u['decisions']} decisions ({acc}%)\n{grade}" if u["decisions"] else
            f"💰 **Chips:** {u['chips']}\nNo hands played yet. Try `/blackjack play`!")
    await interaction.response.send_message(embed=discord.Embed(title="🃏 Blackjack stats", description=text, color=BJ_COLOR),
                                            ephemeral=True)


@blackjack.command(name="chart", description="Basic strategy cheat sheet")
async def bj_chart(interaction: discord.Interaction):
    text = (
        "**Dealer upcard:** 2–6 = weak (likely to bust) · 7–Ace = strong\n\n"
        "**Hard totals**\n"
        "• 8 or less → Hit\n• 9 → Double vs 3–6, else Hit\n• 10 → Double vs 2–9, else Hit\n"
        "• 11 → Double vs 2–10, Hit vs Ace\n• 12 → Stand vs 4–6, else Hit\n"
        "• 13–16 → Stand vs 2–6, else Hit\n• 17+ → Stand\n\n"
        "**Soft totals (with an Ace counted as 11)**\n"
        "• A2–A3 → Double vs 5–6, else Hit\n• A4–A5 → Double vs 4–6, else Hit\n"
        "• A6 → Double vs 3–6, else Hit\n• A7 → Double vs 3–6, Stand vs 2/7/8, Hit vs 9/10/A\n• A8–A9 → Stand\n\n"
        "**Pairs**\n"
        "• A-A, 8-8 → Always split\n• 10-10 → Never split\n• 9-9 → Split, except Stand vs 7/10/A\n"
        "• 7-7, 2-2, 3-3 → Split vs 2–7\n• 6-6 → Split vs 2–6\n• 4-4 → Split vs 5–6\n• 5-5 → Play as hard 10\n\n"
        "_For 6-deck games where the dealer stands on soft 17. If you can't double, hit (or stand on soft 18)._\n"
        "🎰 Vegas tip: pick tables that pay **3:2** for blackjack, not 6:5."
    )
    await interaction.response.send_message(embed=discord.Embed(title="📋 Basic strategy", description=text, color=BJ_COLOR),
                                            ephemeral=True)


@blackjack.command(name="reset", description="Reset your practice chips and stats")
async def bj_reset(interaction: discord.Interaction):
    data = bj_load()
    data.pop(str(interaction.user.id), None)
    bj_save(data)
    BJ_GAMES.pop(interaction.user.id, None)
    await interaction.response.send_message(f"🔄 Reset! You have {BJ_START_CHIPS} practice chips.", ephemeral=True)


bot.tree.add_command(blackjack)

# ===== TEXAS HOLD'EM TRAINER (drills with a coach, no real money) =====
from itertools import combinations

HE_FILE = pathlib.Path(__file__).with_name("holdem_stats.json")
HE_COLOR = 0x2E6B9E
HE_DRILLS = {}  # user id -> current drill (in memory)
HE_RANKCH = {14: "A", 13: "K", 12: "Q", 11: "J", 10: "10", 9: "9", 8: "8", 7: "7", 6: "6", 5: "5", 4: "4", 3: "3", 2: "2"}
HE_SHORT = {14: "A", 13: "K", 12: "Q", 11: "J", 10: "T"}
HAND_NAMES = ["High card", "One pair", "Two pair", "Three of a kind", "Straight", "Flush", "Full house",
              "Four of a kind", "Straight flush", "Royal flush"]
DRILL_LABELS = {"preflop": "Pre-flop decisions", "outs": "Counting outs", "odds": "Pot odds (call/fold)", "hands": "Reading your hand"}


def he_load():
    try:
        return json.loads(HE_FILE.read_text())
    except Exception:
        return {}


def he_record(uid, drill, ok):
    data = he_load()
    u = data.setdefault(str(uid), {})
    c = u.setdefault(drill, [0, 0])
    c[0] += int(ok)
    c[1] += 1
    HE_FILE.write_text(json.dumps(data, indent=1))
    return c


def he_deck():
    d = [(r, s) for r in range(2, 15) for s in SUITS]
    random.shuffle(d)
    return d


def he_show(cards):
    return "  ".join(f"`{HE_RANKCH[r]}{s}`" for r, s in cards)


def rank5(cards):
    ranks = sorted((r for r, _ in cards), reverse=True)
    flush = len({s for _, s in cards}) == 1
    uniq = sorted(set(ranks), reverse=True)
    straight_hi = 0
    if len(uniq) == 5 and uniq[0] - uniq[4] == 4:
        straight_hi = uniq[0]
    elif uniq == [14, 5, 4, 3, 2]:
        straight_hi = 5
    counts = sorted(((ranks.count(r), r) for r in uniq), reverse=True)
    shape = [c for c, _ in counts]
    order = [r for _, r in counts]
    if straight_hi and flush:
        return (8, [straight_hi])
    if shape[0] == 4:
        return (7, order)
    if shape == [3, 2]:
        return (6, order)
    if flush:
        return (5, ranks)
    if straight_hi:
        return (4, [straight_hi])
    if shape[0] == 3:
        return (3, order)
    if shape[:2] == [2, 2]:
        return (2, order)
    if shape[0] == 2:
        return (1, order)
    return (0, ranks)


def best_hand(cards):
    return max(rank5(list(c)) for c in combinations(cards, 5))


def hand_category(cards):
    cat, tb = best_hand(cards)
    return 9 if cat == 8 and tb[0] == 14 else cat


def hand_code(hole):
    (r1, s1), (r2, s2) = sorted(hole, reverse=True)
    a, b = HE_SHORT.get(r1, str(r1)), HE_SHORT.get(r2, str(r2))
    return a + b if r1 == r2 else a + b + ("s" if s1 == s2 else "o")


def _expand(spec):
    out = set()
    for item in spec.split():
        if item.endswith("+"):  # pairs like 77+
            lo = item[0]
            ranks = "23456789TJQKA"
            for r in ranks[ranks.index(lo):]:
                out.add(r + r)
        elif "-" in item:  # pairs like 66-22, or suited runs like A9s-A2s
            a, b = item.split("-")
            ranks = "23456789TJQKA"
            if len(a) == 2:
                for r in ranks[ranks.index(b[0]):ranks.index(a[0]) + 1]:
                    out.add(r + r)
            else:
                for r in ranks[ranks.index(b[1]):ranks.index(a[1]) + 1]:
                    out.add(a[0] + r + a[2])
        else:
            out.add(item)
    return out


PF_TIERS = [
    _expand("AA KK QQ AKs AKo"),
    _expand("JJ TT AQs AQo AJs KQs"),
    _expand("99 88 77 AJo ATs KQo KJs QJs JTs"),
    _expand("66-22 A9s-A2s KTs QTs T9s 98s 87s 76s ATo KJo QJo"),
]
PF_POSITIONS = {
    "early": ("Early position", "You act early, so lots of players still to act behind you. Play tight.", 2),
    "middle": ("Middle position", "About halfway around the table.", 3),
    "late": ("On the button", "Best seat: you act last after the flop.", 4),
    "bb": ("Big blind", "You already have $2 in the pot.", 3),
}


def pf_tier(code):
    for i, t in enumerate(PF_TIERS, 1):
        if code in t:
            return i
    return 5


def preflop_answer(code, pos, facing):
    tier = pf_tier(code)
    name, _, open_max = PF_POSITIONS[pos]
    tier_txt = {1: "a premium hand", 2: "a strong hand", 3: "a good hand", 4: "a speculative hand", 5: "a weak hand"}[tier]
    if not facing:
        if tier <= open_max:
            return "R", f"**{code}** is {tier_txt}, good enough to open from {name.lower()}. When it's folded to you, **raise or fold**, don't just call."
        return "F", f"**{code}** is {tier_txt}. From {name.lower()} it loses money over time. Fold and wait for a better spot."
    if tier == 1:
        return "R", f"**{code}** is {tier_txt}. Re-raise (3-bet) to build the pot while you're likely ahead."
    if tier == 2 or (tier == 3 and pos in ("late", "bb")):
        return "C", f"**{code}** is {tier_txt}. Against a raise it's strong enough to call, but not to re-raise."
    return "F", f"**{code}** is {tier_txt}. Against a raise from a tight player you're often dominated. Fold."


def he_outs_scenario():
    for _ in range(400):
        d = he_deck()
        hole, flop, rest = d[:2], d[2:5], d[5:]
        if hand_category(hole + flop) >= 4:
            continue
        groups = {}
        for c in rest:
            cat = hand_category(hole + flop + [c])
            if cat >= 4:
                groups.setdefault(cat, []).append(c)
        outs = sum(len(v) for v in groups.values())
        if outs in (4, 8, 9, 12, 15):
            return hole, flop, outs, groups
    return None


def he_hands_scenario():
    target = random.choices(range(8), weights=[1, 3, 3, 2, 2, 2, 2, 1])[0]
    for _ in range(4000):
        d = he_deck()
        cards = d[:7]
        cat = hand_category(cards)
        if cat == target or cat >= 8:
            return cards[:2], cards[2:], cat
    return d[:2], d[2:7], hand_category(d[:7])


def he_view(kind, buttons):
    v = discord.ui.View(timeout=None)
    for label, val in buttons:
        v.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, custom_id=f"he:{kind}:{val}"))
    return v


def he_next_view(kind):
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Next ➡️", style=discord.ButtonStyle.primary, custom_id=f"he:next:{kind}"))
    v.add_item(discord.ui.Button(label="Done", style=discord.ButtonStyle.secondary, custom_id="he:done:x"))
    return v


def he_new_drill(uid, kind):
    """Builds a drill, stores it, and returns (embed, view)."""
    g = {"kind": kind}
    e = discord.Embed(color=HE_COLOR)
    if kind == "preflop":
        d = he_deck()
        hole = d[:2]
        # Lean toward playable hands so the drill is interesting
        while pf_tier(hand_code(hole)) == 5 and random.random() < 0.6:
            random.shuffle(d)
            hole = d[:2]
        pos = random.choice(list(PF_POSITIONS))
        facing = True if pos == "bb" else random.random() < 0.45
        name, desc, _ = PF_POSITIONS[pos]
        action = ("A **tight player** who acts before you **raises to $8**. Everyone else folds to you."
                  if facing else "**Everyone folds to you.** No one has raised yet.")
        g.update(code=hand_code(hole), pos=pos, facing=facing)
        e.title = "♠️ Pre-flop drill"
        e.description = (f"**Table:** $1/$2 no-limit · **Seat:** {name}\n_{desc}_\n\n{action}\n\n"
                         f"**Your cards:** {he_show(hole)}  ({g['code']})\n\nWhat do you do?")
        view = he_view("pre", [("Fold", "F"), ("Call", "C"), ("Raise", "R")])
    elif kind == "outs":
        hole, flop, outs, groups = he_outs_scenario()
        g.update(hole=hole, flop=flop, outs=outs, groups=groups)
        e.title = "🎯 Outs drill"
        e.description = (f"**Your cards:** {he_show(hole)}\n**Flop:** {he_show(flop)}\n\n"
                         "How many **outs** do you have to make a **straight or better** on the next card?")
        view = he_view("outs", [(str(n), str(n)) for n in (4, 8, 9, 12, 15)])
    elif kind == "odds":
        hole, flop, outs, groups = he_outs_scenario()
        eq = outs / 47
        for _ in range(200):
            pot = random.choice(range(20, 160, 10))
            bet = random.choice([5, 10, 15, 20, 25, 30, 40, 50, 60, 75, 100])
            need = bet / (pot + 2 * bet)
            if bet <= pot and abs(eq - need) >= 0.04:
                break
        g.update(hole=hole, flop=flop, outs=outs, groups=groups, pot=pot, bet=bet, need=need, eq=eq)
        e.title = "🧮 Pot odds drill"
        e.description = (f"**Your cards:** {he_show(hole)}\n**Flop:** {he_show(flop)}\n\n"
                         f"The pot is **${pot}**. Your opponent bets **${bet}**. You're drawing to a straight or better.\n\n"
                         "Looking only at the **next card**, should you call or fold?")
        view = he_view("odds", [("Fold", "F"), ("Call", "C")])
    else:  # hands
        hole, board, cat = he_hands_scenario()
        g.update(hole=hole, board=board, cat=cat)
        e.title = "🃏 Read your hand"
        e.description = (f"**Your cards:** {he_show(hole)}\n**Board:** {he_show(board)}\n\n"
                         "What's your **best 5-card hand**? (You can use any mix of your 2 cards and the 5 on the board.)")
        view = he_view("hand", [(n, str(i)) for i, n in enumerate(HAND_NAMES)])
    HE_DRILLS[uid] = g
    return e, view


def outs_explain(groups, outs):
    parts = []
    for cat, cards in sorted(groups.items()):
        ranks = sorted({HE_RANKCH[r] for r, _ in cards}, key=lambda x: "2345678910JQKA".index(x) if x != "10" else 8)
        parts.append(f"{len(cards)} make a **{HAND_NAMES[cat].lower()}** ({', '.join(ranks)})")
    return (f"You have **{outs} outs**: " + "; ".join(parts) +
            f".\nRule of 2 and 4: about **{outs * 2}%** with one card to come, **{min(outs * 4, 100)}%** with two.")


async def handle_holdem(interaction, cid):
    _, kind, val = cid.split(":", 2)
    uid = interaction.user.id
    g = HE_DRILLS.get(uid)
    if kind in ("next", "done"):
        if not g or (interaction.message and g.get("msg_id") != interaction.message.id):
            await interaction.response.send_message("That's not your drill. Start your own with `/holdem`.", ephemeral=True)
            return
        if kind == "done":
            HE_DRILLS.pop(uid, None)
            await interaction.response.edit_message(view=None, content="Nice work! See your progress with `/holdem stats`.")
            return
        e, view = he_new_drill(uid, val)
        HE_DRILLS[uid]["msg_id"] = interaction.message.id
        await interaction.response.edit_message(embed=e, view=view, content=None)
        return
    if not g or (interaction.message and g.get("msg_id") != interaction.message.id):
        await interaction.response.send_message("That's not your drill (or StoltBot restarted). Start your own with `/holdem`.", ephemeral=True)
        return
    e = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else discord.Embed(color=HE_COLOR)
    names = {"F": "Fold", "C": "Call", "R": "Raise"}
    if kind == "pre":
        best, why = preflop_answer(g["code"], g["pos"], g["facing"])
        ok = val == best
        drill = "preflop"
        head = f"✅ **{names[val]}** is right." if ok else f"❌ You chose **{names[val]}**. Best play: **{names[best]}**."
        body = why
    elif kind == "outs":
        ok = int(val) == g["outs"]
        drill = "outs"
        head = "✅ Correct!" if ok else f"❌ You said {val}."
        body = outs_explain(g["groups"], g["outs"])
    elif kind == "odds":
        best = "C" if g["eq"] > g["need"] else "F"
        ok = val == best
        drill = "odds"
        head = f"✅ **{names[val]}** is right." if ok else f"❌ You chose **{names[val]}**. Best play: **{names[best]}**."
        body = (outs_explain(g["groups"], g["outs"]) +
                f"\n**Pot odds:** you pay ${g['bet']} to win ${g['pot'] + g['bet']}, so you need to win "
                f"${g['bet']} ÷ ${g['pot'] + 2 * g['bet']} = **{round(g['need'] * 100)}%** of the time.\n"
                f"Your chance on the next card is {g['outs']}/47 ≈ **{round(g['eq'] * 100)}%**, so "
                + ("calling makes money over time." if best == "C" else "calling loses money over time. Fold."))
    else:  # hand
        ok = int(val) == g["cat"]
        drill = "hands"
        best5 = best_hand(g["hole"] + g["board"])
        head = "✅ Correct!" if ok else f"❌ You picked {HAND_NAMES[int(val)]}."
        body = f"Your best hand is **{HAND_NAMES[g['cat']]}**."
        if g["cat"] in (4, 5, 8, 9):
            body += " Remember: a straight is 5 in a row, a flush is 5 of one suit, and the Ace can be high or low."
        if max(rank5(list(c)) for c in combinations(g["board"], 5)) == best5:
            body += "\n_Note: the board alone makes this hand, so everyone still in the pot has it too (\"playing the board\")._"
    c = he_record(uid, drill, ok)
    e.add_field(name="🎓 Coach", value=f"{head}\n{body}", inline=False)
    e.set_footer(text=f"{DRILL_LABELS[drill]}: {c[0]}/{c[1]} correct ({pct(c[0], c[1])}%)")
    await interaction.response.edit_message(embed=e, view=he_next_view(g["kind"]))


holdem = app_commands.Group(name="holdem", description="Texas Hold'em practice drills with a coach")


async def he_start(interaction, kind):
    e, view = he_new_drill(interaction.user.id, kind)
    await interaction.response.send_message(embed=e, view=view)
    HE_DRILLS[interaction.user.id]["msg_id"] = (await interaction.original_response()).id


@holdem.command(name="preflop", description="Drill: fold, call or raise before the flop")
async def he_preflop(interaction: discord.Interaction):
    await he_start(interaction, "preflop")


@holdem.command(name="outs", description="Drill: count how many cards improve your hand")
async def he_outs(interaction: discord.Interaction):
    await he_start(interaction, "outs")


@holdem.command(name="odds", description="Drill: use pot odds to decide whether to call a bet")
async def he_odds(interaction: discord.Interaction):
    await he_start(interaction, "odds")


@holdem.command(name="hands", description="Drill: name your best 5-card hand")
async def he_hands(interaction: discord.Interaction):
    await he_start(interaction, "hands")


@holdem.command(name="stats", description="Your Hold'em drill scores")
async def he_stats(interaction: discord.Interaction):
    u = he_load().get(str(interaction.user.id), {})
    if not u:
        text = "No drills yet. Try `/holdem preflop`, `/holdem outs`, `/holdem odds` or `/holdem hands`."
    else:
        lines = [f"• **{DRILL_LABELS[k]}:** {u[k][0]}/{u[k][1]} ({pct(*u[k])}%)" for k in DRILL_LABELS if k in u]
        weakest = min((k for k in DRILL_LABELS if k in u), key=lambda k: pct(*u[k]))
        text = "\n".join(lines) + f"\n\n🎯 Practice next: **{DRILL_LABELS[weakest]}**"
    await interaction.response.send_message(embed=discord.Embed(title="♠️ Hold'em stats", description=text, color=HE_COLOR),
                                            ephemeral=True)


@holdem.command(name="chart", description="Hand rankings, starting hands and pot odds cheat sheet")
async def he_chart(interaction: discord.Interaction):
    text = (
        "**Hand rankings (best → worst)**\nRoyal flush › Straight flush › Four of a kind › Full house › Flush › "
        "Straight › Three of a kind › Two pair › One pair › High card\n\n"
        "**Starting hands** (raise when it's folded to you)\n"
        "• Premium (any seat): AA KK QQ AK\n• Strong (any seat): JJ TT AQ AJs KQs\n"
        "• Good (middle/late): 99 88 77 AJo ATs KQo KJs QJs JTs\n"
        "• Speculative (button only): small pairs, suited Aces, suited connectors (T9s 98s 87s 76s)\n"
        "• Facing a raise: re-raise premiums, call strong hands, fold most others\n\n"
        "**Common outs**\n• Flush draw: 9 · Open-ended straight: 8 · Gutshot straight: 4\n"
        "• Flush + straight draw: 15 · Two pair → full house: 4\n\n"
        "**Rule of 2 and 4:** outs × 2 ≈ % on the next card, outs × 4 ≈ % by the river\n"
        "**Pot odds:** call ÷ (pot + bet + call) = % you need to win to call"
    )
    await interaction.response.send_message(embed=discord.Embed(title="📋 Hold'em cheat sheet", description=text, color=HE_COLOR),
                                            ephemeral=True)


bot.tree.add_command(holdem)

# ===== LET IT RIDE TRAINER (practice chips only, with strategy coaching) =====
LIR_FILE = pathlib.Path(__file__).with_name("letitride_stats.json")
LIR_START = 1000
LIR_COLOR = 0x8E44AD
LIR_GAMES = {}
LIR_PAYS = {1: 1, 2: 2, 3: 3, 4: 5, 5: 8, 6: 11, 7: 50, 8: 200, 9: 1000}


def lir_load():
    try:
        return json.loads(LIR_FILE.read_text())
    except Exception:
        return {}


def lir_user(data, uid):
    return data.setdefault(str(uid), {"chips": LIR_START, "hands": 0, "decisions": 0, "correct": 0,
                                      "best": LIR_START, "biggest": 0, "biggest_hand": ""})


def lir_paying(cards):
    """Returns (category, multiplier) for 3, 4 or 5 cards. Pairs only pay at 10s or better."""
    ranks = [r for r, _ in cards]
    if len(cards) == 5:
        cat = hand_category(cards)
        if cat == 1:
            pair = max(r for r in ranks if ranks.count(r) == 2)
            return (1, 1) if pair >= 10 else (0, 0)
        return cat, LIR_PAYS.get(cat, 0)
    counts = {r: ranks.count(r) for r in ranks}
    if 3 in counts.values():
        return 3, 3
    pairs = [r for r, c in counts.items() if c == 2]
    if len(pairs) == 2:
        return 2, 2
    if pairs and pairs[0] >= 10:
        return 1, 1
    return 0, 0


def lir_strategy(cards):
    """Basic strategy for the first two decisions. Returns (ride?, reason)."""
    cat, _ = lir_paying(cards)
    if cat:
        return True, f"You already have a paying hand ({HAND_NAMES[cat].lower()}{' of 10s or better' if cat == 1 else ''}). You can't lose, so let it ride."
    ranks = [r for r, _ in cards]
    suited = len({s for _, s in cards}) == 1
    high = sum(1 for r in ranks if r >= 10)
    if len(cards) == 3:
        if any(ranks.count(r) == 2 for r in ranks):
            return False, "A pair below 10s doesn't pay. Pull it back."
        if suited:
            options = [sorted(ranks)]
            if 14 in ranks:
                options.append(sorted(1 if r == 14 else r for r in ranks))
            for opt in options:
                gaps = opt[2] - opt[0] - 2
                if opt[2] - opt[0] > 4:
                    continue
                if all(r >= 10 for r in opt):
                    return True, "Three to a royal flush: lots of ways to win, including 1,000 to 1. Let it ride."
                if gaps == 0 and opt not in ([2, 3, 4], [1, 2, 3]):
                    return True, "Three suited cards in a row: a straight flush draw with many ways to win. Let it ride."
                if gaps == 1 and high >= 1:
                    return True, "Three to a straight flush with one gap and a high card. Let it ride."
                if gaps == 2 and high >= 2:
                    return True, "Three to a straight flush with two gaps and two high cards. Let it ride."
            return False, "Three suited cards, but not connected or high enough to ride. Pull it back."
        return False, "No paying hand and no strong straight-flush draw. Pull it back (this is most hands)."
    # four cards
    if any(ranks.count(r) == 2 for r in ranks):
        return False, "A pair below 10s doesn't pay, and four cards rarely improve enough. Pull it back."
    if suited:
        return True, "Four to a flush: 9 cards complete it for an 8-to-1 payout. Let it ride."
    uniq = sorted(set(ranks))
    if len(uniq) == 4 and uniq[3] - uniq[0] == 3 and uniq[0] >= 2 and uniq[3] <= 13:
        return True, "Four to an outside straight: 8 cards complete it for 5 to 1. Let it ride."
    if len(uniq) == 4 and all(r >= 10 for r in uniq):
        return True, "Four high cards to an inside straight: the straight card plus any pair of 10s+ pays. Let it ride."
    return False, "No paying hand and no strong draw. Pull it back."


def lir_embed(g):
    e = discord.Embed(title="🎰 Let It Ride trainer", color=LIR_COLOR)
    e.add_field(name="Your cards", value=he_show(g["cards"][:3]), inline=False)
    shown = g["stage"] - 1  # community cards face up
    parts = ([he_show(g["cards"][3:3 + shown])] if shown else []) + ["`🂠`"] * (2 - shown)
    e.add_field(name="Community cards", value="  ".join(parts), inline=False)
    bets = []
    for lab, on in zip(["1", "2", "$"], g["active"]):
        bets.append(f"**{lab}:** {g['bet'] if on else '~~' + str(g['bet']) + '~~ pulled back'}")
    e.add_field(name="Your bets", value=" · ".join(bets), inline=False)
    if g.get("result"):
        e.add_field(name="Result", value=g["result"], inline=False)
    if g.get("coach"):
        e.add_field(name="🎓 Coach", value=g["coach"], inline=False)
    e.set_footer(text=f"Chips: {g['chips']} · Practice chips only · Pays on pair of 10s or better")
    return e


def lir_view(g):
    v = discord.ui.View(timeout=None)
    if g["stage"] == 3:
        v.add_item(discord.ui.Button(label=f"Deal again ({g['bet']} × 3)", style=discord.ButtonStyle.success, custom_id="lir:again"))
        v.add_item(discord.ui.Button(label="Done", style=discord.ButtonStyle.secondary, custom_id="lir:quit"))
        return v
    v.add_item(discord.ui.Button(label=f"Pull back Bet {g['stage']}", style=discord.ButtonStyle.danger, custom_id="lir:pull"))
    v.add_item(discord.ui.Button(label="Let it ride", style=discord.ButtonStyle.success, custom_id="lir:ride"))
    v.add_item(discord.ui.Button(label="💡 Hint", style=discord.ButtonStyle.secondary, custom_id="lir:hint"))
    return v


def lir_deal(g):
    g.update(cards=he_deck()[:5], stage=1, active=[True, True, True], result="", coach="")


def lir_finish(g):
    cat, mult = lir_paying(g["cards"])
    staked = sum(g["active"]) * g["bet"]
    if mult:
        net = staked * mult
        g["result"] = f"🎉 **{HAND_NAMES[cat]}** pays {mult} to 1 on {sum(g['active'])} bet(s): **+{net}**"
    else:
        net = -staked
        g["result"] = f"No paying hand. You lose **{staked}**."
    g["chips"] += net
    data = lir_load()
    u = lir_user(data, g["uid"])
    u["chips"] = g["chips"]
    u["hands"] += 1
    u["best"] = max(u["best"], g["chips"])
    if net > u["biggest"]:
        u["biggest"], u["biggest_hand"] = net, HAND_NAMES[cat]
    u["decisions"] += g.pop("decisions", 0)
    u["correct"] += g.pop("correct", 0)
    LIR_FILE.write_text(json.dumps(data, indent=1))


async def handle_lir(interaction, cid):
    g = LIR_GAMES.get(interaction.user.id)
    if not g or (interaction.message and g.get("msg_id") != interaction.message.id):
        await interaction.response.send_message("That's not your table (or StoltBot restarted). Start your own with `/letitride play`.",
                                                ephemeral=True)
        return
    action = cid.split(":")[1]
    if action == "quit":
        LIR_GAMES.pop(interaction.user.id, None)
        await interaction.response.edit_message(view=None, content="Thanks for playing! See your progress with `/letitride stats`.")
        return
    if action == "again":
        if g["chips"] < g["bet"] * 3:
            g["chips"] = LIR_START
        lir_deal(g)
        await interaction.response.edit_message(embed=lir_embed(g), view=lir_view(g))
        return
    if g["stage"] == 3:
        await interaction.response.defer()
        return
    seen = g["cards"][:2 + g["stage"]]
    ride, why = lir_strategy(seen)
    if action == "hint":
        g["coach"] = f"💡 Strategy says **{'Let it ride' if ride else 'Pull it back'}**. {why}"
        await interaction.response.edit_message(embed=lir_embed(g), view=lir_view(g))
        return
    chose_ride = action == "ride"
    g["decisions"] = g.get("decisions", 0) + 1
    if chose_ride == ride:
        g["correct"] = g.get("correct", 0) + 1
        g["coach"] = f"✅ Right call. {why}" if g["tips"] else ""
    else:
        g["coach"] = (f"⚠️ Strategy says **{'Let it ride' if ride else 'Pull it back'}**. {why}" if g["tips"] else "")
    if not chose_ride:
        g["active"][g["stage"] - 1] = False
    g["stage"] += 1
    if g["stage"] == 3:
        lir_finish(g)
    await interaction.response.edit_message(embed=lir_embed(g), view=lir_view(g))


letitride = app_commands.Group(name="letitride", description="Practice Let It Ride with a strategy coach (fake chips)")


@letitride.command(name="play", description="Deal a hand of practice Let It Ride")
@app_commands.describe(bet="Practice chips per circle (you bet this 3 times, default 10)", tips="Coach after every decision (default on)")
async def lir_play(interaction: discord.Interaction, bet: app_commands.Range[int, 1, 300] = 10, tips: bool = True):
    data = lir_load()
    u = lir_user(data, interaction.user.id)
    if u["chips"] < bet * 3:
        u["chips"] = LIR_START
        LIR_FILE.write_text(json.dumps(data, indent=1))
    g = {"uid": interaction.user.id, "chips": u["chips"], "bet": bet, "tips": tips}
    lir_deal(g)
    LIR_GAMES[interaction.user.id] = g
    await interaction.response.send_message(embed=lir_embed(g), view=lir_view(g))
    g["msg_id"] = (await interaction.original_response()).id


@letitride.command(name="stats", description="Your Let It Ride chips and strategy accuracy")
async def lir_stats(interaction: discord.Interaction):
    u = lir_user(lir_load(), interaction.user.id)
    if not u["hands"]:
        text = f"💰 **Chips:** {u['chips']}\nNo hands yet. Try `/letitride play`!"
    else:
        acc = pct(u["correct"], u["decisions"])
        text = (f"💰 **Chips:** {u['chips']} (best {u['best']}, started with {LIR_START})\n"
                f"🃏 **Hands played:** {u['hands']}\n"
                f"🏆 **Biggest win:** +{u['biggest']}" + (f" ({u['biggest_hand']})" if u["biggest_hand"] else "") + "\n"
                f"🎓 **Strategy accuracy:** {u['correct']}/{u['decisions']} ({acc}%)")
    await interaction.response.send_message(embed=discord.Embed(title="🎰 Let It Ride stats", description=text, color=LIR_COLOR),
                                            ephemeral=True)


@letitride.command(name="chart", description="Let It Ride pay table and strategy cheat sheet")
async def lir_chart(interaction: discord.Interaction):
    text = (
        "**Pay table:** Pair of 10s+ 1:1 · Two pair 2:1 · Trips 3:1 · Straight 5:1 · Flush 8:1 · "
        "Full house 11:1 · Quads 50:1 · Straight flush 200:1 · Royal 1000:1\n\n"
        "**High cards = 10, J, Q, K, A**\n\n"
        "**After 3 cards, let it ride with:**\n• Any paying hand (pair of 10s+, trips)\n• Three to a royal flush\n"
        "• Three suited in a row (except 2-3-4 and A-2-3)\n• Three suited, one gap, 1+ high card\n"
        "• Three suited, two gaps, 2+ high cards\n\n"
        "**After 4 cards, let it ride with:**\n• Any paying hand\n• Four to a flush\n"
        "• Four to an outside straight (both ends open)\n• Four high cards to an inside straight\n\n"
        "Pull back everything else, including low pairs (2s–9s). The $ bet always stays."
    )
    await interaction.response.send_message(embed=discord.Embed(title="📋 Let It Ride cheat sheet", description=text, color=LIR_COLOR),
                                            ephemeral=True)


@letitride.command(name="reset", description="Reset your Let It Ride chips and stats")
async def lir_reset(interaction: discord.Interaction):
    data = lir_load()
    data.pop(str(interaction.user.id), None)
    LIR_FILE.write_text(json.dumps(data, indent=1))
    LIR_GAMES.pop(interaction.user.id, None)
    await interaction.response.send_message(f"🔄 Reset! You have {LIR_START} practice chips.", ephemeral=True)


bot.tree.add_command(letitride)

# ===== FIVE CARD DRAW + VIDEO POKER (JACKS OR BETTER) TRAINERS =====
# Shared engine: tap cards to HOLD, then Draw. The coach compares your hold with the recommended one.
DV_FILE = pathlib.Path(__file__).with_name("draw_vp_stats.json")
DV_GAMES = {}
VP_START = 1000
VP_PAYS = {"Royal flush": 800, "Straight flush": 50, "Four of a kind": 25, "Full house": 9, "Flush": 6,
           "Straight": 4, "Three of a kind": 3, "Two pair": 2, "Jacks or better": 1}


def dv_load():
    try:
        return json.loads(DV_FILE.read_text())
    except Exception:
        return {}


def dv_user(data, uid, mode):
    u = data.setdefault(str(uid), {})
    if mode == "vp":
        return u.setdefault("vp", {"credits": VP_START, "hands": 0, "decisions": 0, "correct": 0, "best_win": 0, "best_hand": ""})
    return u.setdefault("draw", {"hands": 0, "wins": 0, "decisions": 0, "correct": 0})


def _fits_straight(ranks):
    """True if these distinct ranks can all be part of one 5-card straight (Ace high or low)."""
    if len(set(ranks)) != len(ranks):
        return False
    for opt in (ranks, [1 if r == 14 else r for r in ranks]):
        if max(opt) - min(opt) <= 4:
            return True
    return False


def _subsets(cards, size, test):
    return [frozenset(ix) for ix in combinations(range(5), size) if test([cards[i] for i in ix])]


def _suited(cs):
    return len({s for _, s in cs}) == 1


def vp_hand_name(cards):
    cat, tb = rank5(cards)
    if cat == 8 and tb[0] == 14:
        return "Royal flush"
    if cat == 1:
        return "Jacks or better" if tb[0] >= 11 else None
    if cat == 0:
        return None
    return HAND_NAMES[cat]


def vp_strategy(cards):
    """Simple strategy for 9/6 Jacks or Better. Returns (list of acceptable holds, explanation)."""
    ranks = [r for r, _ in cards]
    cat, tb = rank5(cards)
    allc = frozenset(range(5))
    by_rank = {}
    for i, r in enumerate(ranks):
        by_rank.setdefault(r, set()).add(i)
    if cat in (7, 8):
        if cat == 7:
            four = frozenset(next(v for v in by_rank.values() if len(v) == 4))
            return [four, allc], "Four of a kind: keep it."
        return [allc], "Straight flush: keep all five!"
    s = _subsets(cards, 4, lambda cs: _suited(cs) and all(r >= 10 for r, _ in cs))
    if s:
        return s, "4 to a royal flush: break up anything (even a flush or straight) to chase the 800-to-1 royal."
    if cat == 3:
        return [frozenset(next(v for v in by_rank.values() if len(v) == 3))], "Three of a kind: keep the three, draw two."
    if cat in (4, 5, 6):
        return [allc], f"{HAND_NAMES[cat]}: a paying hand. Keep all five."
    s = _subsets(cards, 4, lambda cs: _suited(cs) and _fits_straight([r for r, _ in cs]))
    if s:
        return s, "4 to a straight flush: draw one."
    if cat == 2:
        return [frozenset(i for v in by_rank.values() if len(v) == 2 for i in v)], "Two pair: keep both pairs, draw one."
    if cat == 1:
        pair = frozenset(next(v for v in by_rank.values() if len(v) == 2))
        if tb[0] >= 11:
            return [pair], "High pair (Jacks or better) already pays. Keep the pair, draw three."
    s = _subsets(cards, 3, lambda cs: _suited(cs) and all(r >= 10 for r, _ in cs))
    if s:
        return s, "3 to a royal flush: keep them and draw two."
    s = _subsets(cards, 4, _suited)
    if s:
        return s, "4 to a flush: draw one (about 19% to hit, pays 6 to 1)."
    if cat == 1:
        return [pair], "Low pair: it doesn't pay yet, but it can become two pair, trips or better. Keep it."
    s = _subsets(cards, 4, lambda cs: (lambda u: len(u) == 4 and u[3] - u[0] == 3 and u[0] >= 2 and u[3] <= 13)(sorted({r for r, _ in cs})))
    if s:
        return s, "4 to an outside straight: draw one."
    s = _subsets(cards, 2, lambda cs: _suited(cs) and all(r >= 11 for r, _ in cs))
    if s:
        return s, "2 suited high cards: keep them (they can pair up or grow into a royal)."
    s = _subsets(cards, 3, lambda cs: _suited(cs) and _fits_straight([r for r, _ in cs]))
    if s:
        return s, "3 to a straight flush: keep them."
    highs = sorted((i for i in range(5) if ranks[i] >= 11), key=lambda i: ranks[i])
    if len(highs) >= 2:
        return [frozenset(highs[:2])], "Unsuited high cards: keep the two lowest ones (they're more likely to make a straight)."
    s = _subsets(cards, 2, lambda cs: _suited(cs) and sorted(r for r, _ in cs)[0] == 10 and sorted(r for r, _ in cs)[1] in (11, 12, 13))
    if s:
        return s, "Suited 10 with a J, Q or K: keep both."
    if highs:
        return [frozenset([i]) for i in highs], "One high card (J, Q, K or A): keep it and draw four."
    return [frozenset()], "Nothing worth keeping. Throw all five away and draw a fresh hand."


def draw_strategy(cards):
    """Five Card Draw holds. Returns (list of acceptable holds, explanation)."""
    ranks = [r for r, _ in cards]
    cat, tb = rank5(cards)
    allc = frozenset(range(5))
    by_rank = {}
    for i, r in enumerate(ranks):
        by_rank.setdefault(r, set()).add(i)
    if cat >= 4:
        return [allc], f"{HAND_NAMES[min(cat, 8)]}: stand pat and keep all five."
    if cat == 7:
        return [allc], "Four of a kind: stand pat."
    if cat == 3:
        return [frozenset(next(v for v in by_rank.values() if len(v) == 3))], "Three of a kind: keep the three, draw two."
    if cat == 2:
        return [frozenset(i for v in by_rank.values() if len(v) == 2 for i in v)], "Two pair: keep both pairs, draw one."
    if cat == 1 and tb[0] >= 11:
        return [frozenset(next(v for v in by_rank.values() if len(v) == 2))], "High pair: keep it and draw three. Don't keep a kicker."
    s = _subsets(cards, 4, _suited)
    if s:
        return s, "4 to a flush: draw one (about 19%)."
    if cat == 1:
        return [frozenset(next(v for v in by_rank.values() if len(v) == 2))], "Keep the pair and draw three. Pairs beat straight draws."
    s = _subsets(cards, 4, lambda cs: (lambda u: len(u) == 4 and u[3] - u[0] == 3 and u[0] >= 2 and u[3] <= 13)(sorted({r for r, _ in cs})))
    if s:
        return s, "4 to an outside straight: draw one (8 outs, about 17%)."
    order = sorted(range(5), key=lambda i: -ranks[i])
    if ranks[order[0]] == 14:
        holds = [frozenset([order[0]])]
        if ranks[order[1]] >= 11:
            holds.append(frozenset(order[:2]))
        return holds, "No pair or draw: keep the Ace (maybe with a King or Queen) and draw."
    return [frozenset([order[0]]), frozenset(order[:2])], "Nothing much here: keep your highest card or two. In a real game, this is usually a fold."


def dv_embed(g):
    vp = g["mode"] == "vp"
    e = discord.Embed(title="🎰 Video poker · Jacks or Better" if vp else "🃏 Five Card Draw trainer",
                      color=0xC0392B if vp else 0x16A085)
    held = g["held"]
    e.add_field(name="Your hand", value="  ".join(f"`{HE_RANKCH[r]}{s}`" + (" 🔒" if i in held and not g["done"] else "")
                                                   for i, (r, s) in enumerate(g["hand"])), inline=False)
    if not g["done"]:
        e.description = "Tap the cards you want to **HOLD**, then press **Draw**."
    if g.get("result"):
        e.add_field(name="Result", value=g["result"], inline=False)
    if g.get("coach"):
        e.add_field(name="🎓 Coach", value=g["coach"], inline=False)
    e.set_footer(text=(f"Credits: {g['credits']} · Bet: 5 · Practice credits only" if vp else
                       "Practice hands vs one opponent"))
    return e


def dv_view(g):
    v = discord.ui.View(timeout=None)
    p = g["mode"]
    if g["done"]:
        v.add_item(discord.ui.Button(label="Deal again", style=discord.ButtonStyle.success, custom_id=f"dv:{p}:again"))
        v.add_item(discord.ui.Button(label="Done", style=discord.ButtonStyle.secondary, custom_id=f"dv:{p}:quit"))
        return v
    for i, (r, s) in enumerate(g["hand"]):
        on = i in g["held"]
        v.add_item(discord.ui.Button(label=f"{'HOLD ' if on else ''}{HE_RANKCH[r]}{s}", row=0,
                                     style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary,
                                     custom_id=f"dv:{p}:t{i}"))
    v.add_item(discord.ui.Button(label="Draw", style=discord.ButtonStyle.primary, custom_id=f"dv:{p}:draw", row=1))
    v.add_item(discord.ui.Button(label="💡 Hint", style=discord.ButtonStyle.secondary, custom_id=f"dv:{p}:hint", row=1))
    return v


def dv_deal(g):
    deck = he_deck()
    g.update(deck=deck[10:], hand=deck[:5], opp=deck[5:10], held=set(), done=False, result="", coach="")
    if g["mode"] == "vp":
        if g["credits"] < 5:
            g["credits"] = VP_START
        g["credits"] -= 5


def dv_describe_hold(hand, hold):
    return " ".join(f"`{HE_RANKCH[hand[i][0]]}{hand[i][1]}`" for i in sorted(hold)) or "nothing (draw five)"


def dv_finish(g):
    strat = vp_strategy if g["mode"] == "vp" else draw_strategy
    good, why = strat(g["hand"])
    held = frozenset(g["held"])
    ok = held in good
    g["coach"] = (f"✅ Good hold. {why}" if ok else
                  f"⚠️ Better hold: {dv_describe_hold(g['hand'], good[0])}. {why}")
    new = [g["hand"][i] if i in held else g["deck"].pop() for i in range(5)]
    data = dv_load()
    u = dv_user(data, g["uid"], g["mode"])
    u["hands"] += 1
    u["decisions"] += 1
    u["correct"] += int(ok)
    if g["mode"] == "vp":
        name = vp_hand_name(new)
        win = VP_PAYS.get(name, 0) * 5 if name else 0
        g["credits"] += win
        u["credits"] = g["credits"]
        if win > u["best_win"]:
            u["best_win"], u["best_hand"] = win, name
        g["result"] = (f"{show_dv(new)}\n🎉 **{name}** pays **{win}** credits!" if win else f"{show_dv(new)}\nNo win this time.")
    else:
        og, _ = draw_strategy(g["opp"])
        ohold = og[0]
        drawn = 5 - len(ohold)
        opp_new = [g["opp"][i] if i in ohold else g["deck"].pop() for i in range(5)]
        mine, theirs = rank5(new), rank5(opp_new)
        verdict = "🏆 **You win!**" if mine > theirs else "❌ **Opponent wins.**" if mine < theirs else "🤝 **Split pot.**"
        u["wins"] += int(mine > theirs)
        g["result"] = (f"**You:** {show_dv(new)} = {HAND_NAMES[mine[0]]}\n"
                       f"**Opponent** (drew {drawn}): {show_dv(opp_new)} = {HAND_NAMES[theirs[0]]}\n{verdict}")
    g["hand"] = new
    g["done"] = True
    DV_FILE.write_text(json.dumps(data, indent=1))


def show_dv(cards):
    return "  ".join(f"`{HE_RANKCH[r]}{s}`" for r, s in cards)


async def handle_dv(interaction, cid):
    _, mode, action = cid.split(":")
    g = DV_GAMES.get((interaction.user.id, mode))
    if not g or (interaction.message and g.get("msg_id") != interaction.message.id):
        await interaction.response.send_message("That's not your game (or StoltBot restarted). Start your own with `/draw` or `/videopoker`.",
                                                ephemeral=True)
        return
    if action == "quit":
        DV_GAMES.pop((interaction.user.id, mode), None)
        await interaction.response.edit_message(view=None, content="Thanks for playing! Check `/draw stats` or `/videopoker stats`.")
        return
    if action == "again":
        dv_deal(g)
    elif g["done"]:
        await interaction.response.defer()
        return
    elif action.startswith("t"):
        g["held"] ^= {int(action[1])}
    elif action == "hint":
        good, why = (vp_strategy if mode == "vp" else draw_strategy)(g["hand"])
        g["coach"] = f"💡 Hold {dv_describe_hold(g['hand'], good[0])}. {why}"
    elif action == "draw":
        dv_finish(g)
    await interaction.response.edit_message(embed=dv_embed(g), view=dv_view(g))


async def dv_start(interaction, mode):
    g = {"uid": interaction.user.id, "mode": mode, "credits": 0}
    if mode == "vp":
        g["credits"] = dv_user(dv_load(), interaction.user.id, "vp")["credits"]
    dv_deal(g)
    DV_GAMES[(interaction.user.id, mode)] = g
    await interaction.response.send_message(embed=dv_embed(g), view=dv_view(g))
    g["msg_id"] = (await interaction.original_response()).id


drawgroup = app_commands.Group(name="draw", description="Five Card Draw trainer: pick your holds and beat an opponent")
vpgroup = app_commands.Group(name="videopoker", description="Video poker (Jacks or Better) trainer with practice credits")


@drawgroup.command(name="play", description="Deal a Five Card Draw hand against one opponent")
async def draw_play(interaction: discord.Interaction):
    await dv_start(interaction, "draw")


@vpgroup.command(name="play", description="Play a hand of Jacks or Better (5-credit max bet)")
async def vp_play(interaction: discord.Interaction):
    await dv_start(interaction, "vp")


@drawgroup.command(name="stats", description="Your Five Card Draw record and hold accuracy")
async def draw_stats(interaction: discord.Interaction):
    u = dv_user(dv_load(), interaction.user.id, "draw")
    text = (f"🃏 **Hands:** {u['hands']} · **Won:** {u['wins']} ({pct(u['wins'], u['hands'])}%)\n"
            f"🎓 **Hold accuracy:** {u['correct']}/{u['decisions']} ({pct(u['correct'], u['decisions'])}%)"
            if u["hands"] else "No hands yet. Try `/draw play`!")
    await interaction.response.send_message(embed=discord.Embed(title="🃏 Five Card Draw stats", description=text, color=0x16A085), ephemeral=True)


@vpgroup.command(name="stats", description="Your video poker credits and hold accuracy")
async def vp_stats(interaction: discord.Interaction):
    u = dv_user(dv_load(), interaction.user.id, "vp")
    text = (f"💰 **Credits:** {u['credits']} (started with {VP_START})\n🎰 **Hands:** {u['hands']}\n"
            f"🏆 **Best win:** {u['best_win']}" + (f" ({u['best_hand']})" if u["best_hand"] else "") +
            f"\n🎓 **Hold accuracy:** {u['correct']}/{u['decisions']} ({pct(u['correct'], u['decisions'])}%)"
            if u["hands"] else f"💰 **Credits:** {u['credits']}\nNo hands yet. Try `/videopoker play`!")
    await interaction.response.send_message(embed=discord.Embed(title="🎰 Video poker stats", description=text, color=0xC0392B), ephemeral=True)


@vpgroup.command(name="chart", description="Jacks or Better pay table and hold strategy")
async def vp_chart(interaction: discord.Interaction):
    text = ("**9/6 pay table (per credit, 5-credit bet):** Royal 800 · Straight flush 50 · Quads 25 · Full house 9 · "
            "Flush 6 · Straight 4 · Trips 3 · Two pair 2 · Jacks or better 1\n\n"
            "**Hold the first thing on this list that you have:**\n"
            "1. Quads, straight flush, royal\n2. 4 to a royal flush\n3. Trips, straight, flush, full house\n"
            "4. 4 to a straight flush\n5. Two pair\n6. High pair (J, Q, K, A)\n7. 3 to a royal flush\n"
            "8. 4 to a flush\n9. Low pair (2–10)\n10. 4 to an outside straight\n11. 2 suited high cards\n"
            "12. 3 to a straight flush\n13. 2 unsuited high cards (if more, the 2 lowest)\n"
            "14. Suited 10 with J, Q or K\n15. One high card\n16. Draw five new cards\n\n"
            "🎰 Vegas tip: look for **9/6** machines (Full house 9, Flush 6). **8/5** or **7/5** pay noticeably less. "
            "Always bet max (5 credits) so the royal pays 800.")
    await interaction.response.send_message(embed=discord.Embed(title="📋 Jacks or Better cheat sheet", description=text, color=0xC0392B), ephemeral=True)


bot.tree.add_command(drawgroup)
bot.tree.add_command(vpgroup)

# ===== ROULETTE TRAINER (practice chips only) =====
RL_FILE = pathlib.Path(__file__).with_name("roulette_stats.json")
RL_START = 1000
RL_COLOR = 0xB03A2E
RL_GAMES = {}
RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
DZ = 37  # stands for "00"
RL_BETS = {  # name: (label, payout)
    "straight": ("Straight up (1 number)", 35), "split": ("Split (2 numbers)", 17), "street": ("Street (row of 3)", 11),
    "corner": ("Corner (4 numbers)", 8), "line": ("Six line (2 rows)", 5), "five": ("Five number 0-00-1-2-3", 6),
    "red": ("Red", 1), "black": ("Black", 1), "odd": ("Odd", 1), "even": ("Even", 1), "low": ("Low 1–18", 1),
    "high": ("High 19–36", 1), "dozen1": ("1st dozen 1–12", 2), "dozen2": ("2nd dozen 13–24", 2),
    "dozen3": ("3rd dozen 25–36", 2), "col1": ("Column 1 (1, 4, 7…34)", 2), "col2": ("Column 2 (2, 5, 8…35)", 2),
    "col3": ("Column 3 (3, 6, 9…36)", 2),
}


def rl_load():
    try:
        return json.loads(RL_FILE.read_text())
    except Exception:
        return {}


def rl_user(data, uid):
    return data.setdefault(str(uid), {"chips": RL_START, "spins": 0, "wins": 0, "wagered": 0, "net": 0, "best": RL_START})


def rl_name(n):
    return "00" if n == DZ else str(n)


def rl_color(n):
    return "🟢" if n in (0, DZ) else "🔴" if n in RED else "⚫"


def rl_parse_nums(text):
    out = []
    for part in re.split(r"[\s,/-]+", (text or "").strip()):
        if part == "00":
            out.append(DZ)
        elif part.isdigit() and 0 <= int(part) <= 36:
            out.append(int(part))
        elif part:
            return None
    return out


def rl_cover(kind, pick, wheel):
    """Returns (set of winning numbers, error message)."""
    nums = rl_parse_nums(pick) if pick else []
    if nums is None:
        return None, "I couldn't read those numbers. Use something like `17`, `17-18` or `1-2-4-5`."
    real = [n for n in nums if 1 <= n <= 36]
    if kind == "straight":
        if len(nums) != 1 or (nums[0] == DZ and wheel == "european"):
            return None, "Pick one number, like `17` (or `0`, or `00` on an American wheel)."
        return {nums[0]}, None
    if kind == "split":
        if len(real) != 2:
            return None, "Pick 2 numbers next to each other, like `17-18` or `17-20`."
        a, b = sorted(real)
        if not ((b - a == 1 and (a - 1) // 3 == (b - 1) // 3) or b - a == 3):
            return None, f"{a} and {b} aren't next to each other on the layout."
        return {a, b}, None
    if kind == "street":
        if len(real) != 1:
            return None, "Pick any number in the row, like `13` for 13-14-15."
        s = (real[0] - 1) // 3 * 3 + 1
        return {s, s + 1, s + 2}, None
    if kind == "corner":
        if len(real) != 4:
            return None, "Pick 4 numbers in a square, like `1-2-4-5`."
        a = min(real)
        if sorted(real) != [a, a + 1, a + 3, a + 4] or a % 3 == 0:
            return None, "Those 4 numbers don't form a square. Example: `1-2-4-5` or `17-18-20-21`."
        return set(real), None
    if kind == "line":
        if len(real) != 1 or real[0] > 33:
            return None, "Pick a number in the top row of the pair, like `13` for 13–18."
        s = (real[0] - 1) // 3 * 3 + 1
        return set(range(s, s + 6)), None
    if kind == "five":
        if wheel == "european":
            return None, "The five-number bet only exists on American wheels (it needs 00)."
        return {0, DZ, 1, 2, 3}, None
    groups = {"red": RED, "black": set(range(1, 37)) - RED, "odd": {n for n in range(1, 37) if n % 2},
              "even": {n for n in range(1, 37) if n % 2 == 0}, "low": set(range(1, 19)), "high": set(range(19, 37)),
              "dozen1": set(range(1, 13)), "dozen2": set(range(13, 25)), "dozen3": set(range(25, 37)),
              "col1": set(range(1, 37, 3)), "col2": set(range(2, 37, 3)), "col3": set(range(3, 37, 3))}
    return groups[kind], None


def rl_spin(g):
    pockets = list(range(37)) + ([DZ] if g["wheel"] == "american" else [])
    n = random.choice(pockets)
    data = rl_load()
    u = rl_user(data, g["uid"])
    note = ""
    if u["chips"] < g["amount"]:
        u["chips"] = RL_START
        note = f"Out of chips, so you got a fresh {RL_START}.\n"
    win = n in g["cover"]
    payout = RL_BETS[g["kind"]][1]
    net = g["amount"] * payout if win else -g["amount"]
    u["chips"] += net
    u["spins"] += 1
    u["wins"] += int(win)
    u["wagered"] += g["amount"]
    u["net"] += net
    u["best"] = max(u["best"], u["chips"])
    RL_FILE.write_text(json.dumps(data, indent=1))
    g["history"] = ([n] + g.get("history", []))[:10]
    pockets_n = 38 if g["wheel"] == "american" else 37
    chance = len(g["cover"]) / pockets_n
    edge = (1 - chance * (payout + 1)) * 100
    label = RL_BETS[g["kind"]][0] + (f" on {g['pick']}" if g.get("pick") else "")
    e = discord.Embed(title=f"🎡 {rl_color(n)} **{rl_name(n)}**", color=RL_COLOR,
                      description=note + (f"🎉 **You win {net}!** ({payout} to 1)" if win else f"❌ You lose {g['amount']}."))
    e.add_field(name="Your bet", value=f"{label} · {g['amount']} chips", inline=False)
    e.add_field(name="🎓 Coach",
                value=(f"This bet wins **{len(g['cover'])} of {pockets_n}** pockets (**{chance * 100:.1f}%**) and pays {payout} to 1. "
                       f"House edge: **{edge:.2f}%**." +
                       (" ⚠️ This is the worst bet on the table." if g["kind"] == "five" else
                        " Every bet (except the five-number) has this same edge." if g["wheel"] == "american" else
                        " Single-zero wheels give you about half the house edge of American wheels.")), inline=False)
    e.add_field(name="Last spins", value=" ".join(f"{rl_color(x)}{rl_name(x)}" for x in g["history"]), inline=False)
    e.set_footer(text=f"Chips: {u['chips']} · {g['wheel'].title()} wheel · Practice chips only · Past spins don't predict the next one")
    return e


def rl_view():
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Spin again (same bet)", style=discord.ButtonStyle.success, custom_id="rl:again"))
    v.add_item(discord.ui.Button(label="Done", style=discord.ButtonStyle.secondary, custom_id="rl:quit"))
    return v


async def handle_roulette(interaction, cid):
    g = RL_GAMES.get(interaction.user.id)
    if not g or (interaction.message and g.get("msg_id") != interaction.message.id):
        await interaction.response.send_message("That's not your spin (or StoltBot restarted). Place your own bet with `/roulette spin`.",
                                                ephemeral=True)
        return
    if cid == "rl:quit":
        RL_GAMES.pop(interaction.user.id, None)
        await interaction.response.edit_message(view=None, content="Thanks for playing! See how you did with `/roulette stats`.")
        return
    await interaction.response.edit_message(embed=rl_spin(g), view=rl_view())


roulette = app_commands.Group(name="roulette", description="Practice roulette with fake chips and a coach")


@roulette.command(name="spin", description="Place a bet and spin the wheel")
@app_commands.describe(bet="What to bet on", amount="Practice chips (default 10)",
                       numbers="For inside bets: e.g. 17, 17-18, 13 (street), 1-2-4-5 (corner)",
                       wheel="American (0 and 00) or European (single 0)")
@app_commands.choices(bet=[app_commands.Choice(name=v[0], value=k) for k, v in RL_BETS.items()][:25],
                      wheel=[app_commands.Choice(name="American (0, 00)", value="american"),
                             app_commands.Choice(name="European (single 0)", value="european")])
async def rl_spin_cmd(interaction: discord.Interaction, bet: app_commands.Choice[str], amount: app_commands.Range[int, 1, 500] = 10,
                      numbers: Optional[str] = None, wheel: Optional[app_commands.Choice[str]] = None):
    w = wheel.value if wheel else "american"
    cover, err = rl_cover(bet.value, numbers, w)
    if err:
        await interaction.response.send_message(f"⚠️ {err}", ephemeral=True)
        return
    pick = ", ".join(rl_name(n) for n in sorted(cover)) if bet.value in ("straight", "split", "street", "corner", "line") else ""
    g = {"uid": interaction.user.id, "kind": bet.value, "cover": cover, "amount": amount, "wheel": w, "pick": pick,
         "history": RL_GAMES.get(interaction.user.id, {}).get("history", [])}
    RL_GAMES[interaction.user.id] = g
    await interaction.response.send_message(embed=rl_spin(g), view=rl_view())
    g["msg_id"] = (await interaction.original_response()).id


@roulette.command(name="stats", description="Your roulette results compared with the house edge")
async def rl_stats(interaction: discord.Interaction):
    u = rl_user(rl_load(), interaction.user.id)
    if not u["spins"]:
        text = f"💰 **Chips:** {u['chips']}\nNo spins yet. Try `/roulette spin`!"
    else:
        expected = -u["wagered"] * 0.0526
        text = (f"💰 **Chips:** {u['chips']} (best {u['best']}, started with {RL_START})\n"
                f"🎡 **Spins:** {u['spins']} · **Won:** {u['wins']}\n"
                f"💵 **Total bet:** {u['wagered']} · **Net:** {'+' if u['net'] >= 0 else ''}{u['net']}\n"
                f"📉 **Expected net** at a 5.26% house edge: about {round(expected)}\n\n"
                "_Over many spins, your net tends to drift toward the expected loss. Short-term luck can go either way._")
    await interaction.response.send_message(embed=discord.Embed(title="🎡 Roulette stats", description=text, color=RL_COLOR), ephemeral=True)


@roulette.command(name="chart", description="Roulette bets, payouts and odds")
async def rl_chart(interaction: discord.Interaction):
    text = ("**Inside bets:** Straight 35:1 · Split 17:1 · Street 11:1 · Corner 8:1 · Six line 5:1 · Five number 6:1 ⚠️\n"
            "**Outside bets:** Red/Black, Odd/Even, Low/High 1:1 · Dozens and Columns 2:1\n\n"
            "**House edge:** American (0, 00) **5.26%** · European (single 0) **2.70%** · Five-number bet **7.89%**\n\n"
            "**Remember:**\n• 0 and 00 lose all outside bets\n• Every bet has the same edge (except the five-number)\n"
            "• No system (like Martingale) beats the edge\n• Past spins don't affect the next spin\n\n"
            "🎰 Vegas tip: look for single-zero wheels, set a budget, and treat it as entertainment.")
    await interaction.response.send_message(embed=discord.Embed(title="📋 Roulette cheat sheet", description=text, color=RL_COLOR), ephemeral=True)


@roulette.command(name="reset", description="Reset your roulette chips and stats")
async def rl_reset(interaction: discord.Interaction):
    data = rl_load()
    data.pop(str(interaction.user.id), None)
    RL_FILE.write_text(json.dumps(data, indent=1))
    await interaction.response.send_message(f"🔄 Reset! You have {RL_START} practice chips.", ephemeral=True)


bot.tree.add_command(roulette)

# ===== SLOTS SIMULATOR (practice credits; shows RTP and volatility in action) =====
SL_FILE = pathlib.Path(__file__).with_name("slots_stats.json")
SL_START = 1000
SL_GAMES = {}
SL_MACHINES = {
    "low": {"name": "🍀 Lucky Fruits (low volatility)",
            "weights": {"➖": 8, "🍒": 4, "🍋": 6, "🔔": 5, "⭐": 3, "💎": 2, "7️⃣": 1},
            "pays": {"🍒": 15, "🍋": 15, "🔔": 25, "⭐": 60, "💎": 150, "7️⃣": 300}, "one_cherry": 1, "two_cherry": 4},
    "high": {"name": "💥 Mega Sevens (high volatility)",
             "weights": {"➖": 10, "🍒": 3, "🍋": 7, "🔔": 5, "⭐": 3, "💎": 2, "7️⃣": 1},
             "pays": {"🍒": 50, "🍋": 15, "🔔": 30, "⭐": 100, "💎": 500, "7️⃣": 5000}, "one_cherry": 0, "two_cherry": 6},
}


def sl_payout(m, reels):
    """Multiplier of the bet for one spin."""
    a, b, c = reels
    if a == b == c and a != "➖":
        return m["pays"][a]
    cherries = reels.count("🍒")
    return m["two_cherry"] if cherries == 2 else m["one_cherry"] if cherries == 1 else 0


def sl_math(m):
    """Exact RTP and hit rate for a machine (every reel combination)."""
    w = m["weights"]
    total = sum(w.values()) ** 3
    ret = hits = 0
    for a in w:
        for b in w:
            for c in w:
                p = w[a] * w[b] * w[c]
                x = sl_payout(m, [a, b, c])
                ret += p * x
                hits += p * (x > 0)
    return ret / total, hits / total


for _m in SL_MACHINES.values():
    _m["rtp"], _m["hit"] = sl_math(_m)


def sl_load():
    try:
        return json.loads(SL_FILE.read_text())
    except Exception:
        return {}


def sl_user(data, uid):
    return data.setdefault(str(uid), {"credits": SL_START, "spins": 0, "wagered": 0, "returned": 0, "biggest": 0, "longest_dry": 0})


def sl_spin_once(m):
    syms = list(m["weights"])
    wts = list(m["weights"].values())
    reels = random.choices(syms, weights=wts, k=3)
    return reels, sl_payout(m, reels)


def sl_play(g, count):
    m = SL_MACHINES[g["machine"]]
    data = sl_load()
    u = sl_user(data, g["uid"])
    note = ""
    if u["credits"] < g["bet"]:
        u["credits"] = SL_START
        note = f"Out of credits, so you got a fresh {SL_START}.\n"
    won_total = spun = 0
    last = None
    for _ in range(count):
        if u["credits"] < g["bet"]:
            break
        reels, mult = sl_spin_once(m)
        win = mult * g["bet"]
        u["credits"] += win - g["bet"]
        u["spins"] += 1
        u["wagered"] += g["bet"]
        u["returned"] += win
        u["biggest"] = max(u["biggest"], win)
        g["dry"] = 0 if win else g.get("dry", 0) + 1
        u["longest_dry"] = max(u["longest_dry"], g["dry"])
        g["session_bet"] = g.get("session_bet", 0) + g["bet"]
        g["session_won"] = g.get("session_won", 0) + win
        won_total += win
        spun += 1
        last = (reels, win)
    SL_FILE.write_text(json.dumps(data, indent=1))
    reels, win = last
    e = discord.Embed(title=m["name"], color=0xF1C40F)
    e.description = note + f"## {' │ '.join(reels)}\n" + (f"🎉 **Win: {win} credits!**" if win else "No win.")
    if spun > 1:
        e.description += f"\n\n**{spun} spins:** bet {spun * g['bet']}, won back {won_total} ({'+' if won_total - spun * g['bet'] >= 0 else ''}{won_total - spun * g['bet']})"
    sb, sw = g["session_bet"], g["session_won"]
    e.add_field(name="This session",
                value=f"Bet **{sb}** · Paid back **{sw}** · Actual return **{round(sw / sb * 100, 1)}%**", inline=False)
    e.add_field(name="🎓 How this machine works",
                value=(f"Programmed return (RTP): **{m['rtp'] * 100:.1f}%** · Hits on about **{m['hit'] * 100:.0f}%** of spins.\n"
                       f"That means over time it keeps about **{100 - m['rtp'] * 100:.1f}¢ of every $1** bet. "
                       "Short sessions swing wildly; thousands of spins get close to the RTP."), inline=False)
    if g.get("dry", 0) >= 15:
        e.add_field(name="🧊 Dry streak", value=f"{g['dry']} spins without a win. The machine isn't \"due\": every spin is random.", inline=False)
    e.set_footer(text=f"Credits: {u['credits']} · Bet {g['bet']} per spin · Practice credits only")
    return e


def sl_view():
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Spin", style=discord.ButtonStyle.success, custom_id="sl:1"))
    v.add_item(discord.ui.Button(label="Spin ×10", style=discord.ButtonStyle.primary, custom_id="sl:10"))
    v.add_item(discord.ui.Button(label="Spin ×100", style=discord.ButtonStyle.primary, custom_id="sl:100"))
    v.add_item(discord.ui.Button(label="Cash out", style=discord.ButtonStyle.secondary, custom_id="sl:quit"))
    return v


async def handle_slots(interaction, cid):
    g = SL_GAMES.get(interaction.user.id)
    if not g or (interaction.message and g.get("msg_id") != interaction.message.id):
        await interaction.response.send_message("That's not your machine (or StoltBot restarted). Start your own with `/slots play`.",
                                                ephemeral=True)
        return
    action = cid.split(":")[1]
    if action == "quit":
        SL_GAMES.pop(interaction.user.id, None)
        sb, sw = g.get("session_bet", 0), g.get("session_won", 0)
        msg = (f"💵 Cashed out. This session you bet **{sb}** and got back **{sw}** "
               f"({'+' if sw - sb >= 0 else ''}{sw - sb}). See all-time results with `/slots stats`." if sb else "Cashed out.")
        await interaction.response.edit_message(view=None, content=msg)
        return
    await interaction.response.edit_message(embed=sl_play(g, int(action)), view=sl_view())


slots = app_commands.Group(name="slots", description="Slot machine simulator that shows how RTP and volatility work (fake credits)")


@slots.command(name="play", description="Sit down at a practice slot machine")
@app_commands.describe(machine="Low volatility (small frequent wins) or high volatility (rare big wins)",
                       bet="Credits per spin (default 5)")
@app_commands.choices(machine=[app_commands.Choice(name="Lucky Fruits (low volatility)", value="low"),
                               app_commands.Choice(name="Mega Sevens (high volatility)", value="high")])
async def sl_play_cmd(interaction: discord.Interaction, machine: Optional[app_commands.Choice[str]] = None,
                      bet: app_commands.Range[int, 1, 100] = 5):
    g = {"uid": interaction.user.id, "machine": machine.value if machine else "low", "bet": bet}
    SL_GAMES[interaction.user.id] = g
    await interaction.response.send_message(embed=sl_play(g, 1), view=sl_view())
    g["msg_id"] = (await interaction.original_response()).id


@slots.command(name="stats", description="Your all-time slot results compared with the machine's RTP")
async def sl_stats(interaction: discord.Interaction):
    u = sl_user(sl_load(), interaction.user.id)
    if not u["spins"]:
        text = f"💰 **Credits:** {u['credits']}\nNo spins yet. Try `/slots play`!"
    else:
        actual = u["returned"] / u["wagered"] * 100
        text = (f"💰 **Credits:** {u['credits']} (started with {SL_START})\n🎰 **Spins:** {u['spins']}\n"
                f"💵 **Bet:** {u['wagered']} · **Paid back:** {u['returned']} · **Actual return:** {actual:.1f}%\n"
                f"🏆 **Biggest win:** {u['biggest']} · 🧊 **Longest dry streak:** {u['longest_dry']} spins\n\n"
                f"Machine RTPs: Lucky Fruits {SL_MACHINES['low']['rtp'] * 100:.1f}% · Mega Sevens {SL_MACHINES['high']['rtp'] * 100:.1f}%\n"
                "_The more you spin, the closer your actual return gets to the RTP._")
    await interaction.response.send_message(embed=discord.Embed(title="🎰 Slots stats", description=text, color=0xF1C40F), ephemeral=True)


@slots.command(name="paytable", description="See each machine's pay table, RTP and hit rate")
async def sl_paytable(interaction: discord.Interaction):
    e = discord.Embed(title="📋 Slot pay tables (pays × your bet)", color=0xF1C40F)
    for m in SL_MACHINES.values():
        pays = " · ".join(f"{s}{s}{s} {x}" for s, x in sorted(m["pays"].items(), key=lambda kv: kv[1]))
        cher = f"Two 🍒 {m['two_cherry']}" + (f" · One 🍒 {m['one_cherry']}" if m["one_cherry"] else "")
        e.add_field(name=m["name"], value=f"{pays}\n{cher}\n**RTP {m['rtp'] * 100:.1f}%** · hits {m['hit'] * 100:.0f}% of spins", inline=False)
    e.set_footer(text="Real Vegas slots usually return 85–95%. Video poker and blackjack have much better odds.")
    await interaction.response.send_message(embed=e, ephemeral=True)


@slots.command(name="reset", description="Reset your slot credits and stats")
async def sl_reset(interaction: discord.Interaction):
    data = sl_load()
    data.pop(str(interaction.user.id), None)
    SL_FILE.write_text(json.dumps(data, indent=1))
    await interaction.response.send_message(f"🔄 Reset! You have {SL_START} practice credits.", ephemeral=True)


bot.tree.add_command(slots)


try:
    bot.run(TOKEN)
except discord.HTTPException as err:
    if err.status == 429:
        # Discord (through Cloudflare) has temporarily blocked this server's IP for too many requests.
        # Wait quietly before exiting, so the host's auto-restart doesn't keep hitting Discord and extend the block.
        import time
        print("⏳ Discord is rate limiting this server's IP (error 429 / Cloudflare 1015). "
              "Waiting 15 minutes before exiting so the block can clear. Don't keep pressing Restart.")
        time.sleep(15 * 60)
    raise
