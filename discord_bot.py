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
  /exam             - practice exam: 10, 20 or 40 questions with a final score
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
    e.set_footer(text=f"Domain {dom} · tap a letter to answer (only you see if you're right)")
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
                             [app_commands.Choice(name="My weakest domain", value="weakest"),
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
@app_commands.describe(length="How many questions", domain="Optional: only one domain")
@app_commands.choices(length=[app_commands.Choice(name="10 questions (~10 min)", value=10),
                              app_commands.Choice(name="20 questions (~20 min)", value=20),
                              app_commands.Choice(name="40 questions (~40 min)", value=40)],
                      domain=[app_commands.Choice(name=d, value=d) for d in DOMAINS])
async def exam(interaction: discord.Interaction, length: Optional[app_commands.Choice[int]] = None,
               domain: Optional[app_commands.Choice[str]] = None):
    n = length.value if length else 10
    pool = [i for i, x in enumerate(QUIZ) if not domain or x[0] == domain.value]
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


bot.run(TOKEN)
