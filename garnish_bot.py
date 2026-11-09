"""
StoltBot garnishment tracker
----------------------------
Keeps track of wage / tax-refund garnishments: how much is owed, what's been taken, and about
how many paychecks are left. All replies are private, and only server managers can use it.

  /garnish add      - add a garnishment (creditor, type, balance owed, % or $ per check, case #)
  /garnish paid     - log money taken (e.g. from a paycheck stub)
  /garnish list     - every garnishment with balance left, total taken and paychecks to go
  /garnish history  - recent deductions for one garnishment
  /garnish edit     - fix the balance, amount per check or notes
  /garnish close    - mark one as paid off / released (keeps the history)

Hooked up from discord_bot.py with:   import garnish_bot; garnish_bot.setup(bot)

Data is saved in garnishments.json in DATA_DIR (the Railway Volume), never in the code,
so nothing personal ends up on GitHub.
"""

import os
import re
import json
import math
import pathlib
import datetime
from typing import Optional

import discord
from discord import app_commands

DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR") or pathlib.Path(__file__).parent)
DATA_FILE = DATA_DIR / "garnishments.json"
COLOR = discord.Color.from_rgb(231, 76, 60)

TYPES = {"wages": "💼 Paycheck", "tax": "🧾 Tax refund", "bank": "🏦 Bank account", "other": "📄 Other"}


# ===== STORAGE =====
def load():
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"next_id": 1, "items": []}


def save(data):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = DATA_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(DATA_FILE)
    except Exception as e:
        print(f"⚠️ Couldn't save garnishments: {e}")


def money(x):
    return f"${x:,.2f}"


def parse_money(text):
    """'$1,234.50' -> 1234.5 ; returns None if it isn't a number."""
    try:
        v = float(re.sub(r"[^\d.]", "", str(text)))
        return round(v, 2) if v >= 0 else None
    except ValueError:
        return None


def taken(item):
    return round(sum(p["amount"] for p in item["payments"]), 2)


def left(item):
    return max(0.0, round(item["balance"] - taken(item), 2))


def checks_left(item):
    """Rough number of paychecks to go, from the set amount or the average of recent deductions."""
    per = item.get("per_check") or 0
    if not per and item["payments"]:
        recent = item["payments"][-4:]
        per = sum(p["amount"] for p in recent) / len(recent)
    if per <= 0 or left(item) <= 0:
        return None
    return math.ceil(left(item) / per)


def find(data, gid):
    return next((i for i in data["items"] if i["id"] == gid), None)


# ===== COMMANDS =====
garnish = app_commands.Group(name="garnish", description="Track garnishments: balance, deductions, paychecks left",
                             default_permissions=discord.Permissions(manage_guild=True))


async def open_ac(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=f"#{i['id']} {i['creditor']} - {TYPES.get(i['type'], i['type'])}"[:100], value=i["id"])
            for i in load()["items"]
            if i["status"] == "open" and current.lower() in f"{i['creditor']} {i['type']}".lower()][:25]


async def any_ac(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=f"#{i['id']} {i['creditor']} - {TYPES.get(i['type'], i['type'])} ({i['status']})"[:100],
                                value=i["id"])
            for i in load()["items"] if current.lower() in f"{i['creditor']} {i['type']}".lower()][:25]


TYPE_CHOICES = [app_commands.Choice(name=v, value=k) for k, v in TYPES.items()]


@garnish.command(name="add", description="Add a garnishment")
@app_commands.describe(creditor="Who it's for, e.g. Ford Motor Credit", type="What's being garnished",
                       balance="Amount owed, e.g. 4601.24", per_check="Optional: $ taken each paycheck",
                       percent="Optional: % of take-home pay (e.g. 25)", case="Optional: court case number",
                       notes="Optional: lawyer, court, deadlines...")
@app_commands.choices(type=TYPE_CHOICES)
async def g_add(interaction: discord.Interaction, creditor: str, type: app_commands.Choice[str], balance: str,
                per_check: Optional[str] = None, percent: Optional[float] = None,
                case: Optional[str] = None, notes: Optional[str] = None):
    bal = parse_money(balance)
    if bal is None:
        return await interaction.response.send_message("❌ Balance should be a number, like 4601.24", ephemeral=True)
    data = load()
    item = {"id": data["next_id"], "creditor": creditor.strip()[:80], "type": type.value, "balance": bal,
            "per_check": parse_money(per_check) if per_check else None, "percent": percent,
            "case": (case or "").strip()[:60], "notes": (notes or "").strip()[:500], "status": "open",
            "added": datetime.date.today().isoformat(), "payments": []}
    data["next_id"] += 1
    data["items"].append(item)
    save(data)
    await interaction.response.send_message(f"✅ Added garnishment **#{item['id']}**: {creditor} ({TYPES[type.value]}), "
                                            f"{money(bal)} owed. Log deductions with `/garnish paid`.", ephemeral=True)


@garnish.command(name="paid", description="Log money that was taken (from a pay stub, bank, or tax refund)")
@app_commands.describe(garnishment="Which one", amount="Amount taken, e.g. 182.40",
                       date="Optional: date taken (YYYY-MM-DD), default today", note="Optional note")
@app_commands.autocomplete(garnishment=open_ac)
async def g_paid(interaction: discord.Interaction, garnishment: int, amount: str,
                 date: Optional[str] = None, note: Optional[str] = None):
    amt = parse_money(amount)
    if not amt:
        return await interaction.response.send_message("❌ Amount should be a number, like 182.40", ephemeral=True)
    try:
        when = datetime.date.fromisoformat(date).isoformat() if date else datetime.date.today().isoformat()
    except ValueError:
        return await interaction.response.send_message("❌ Date should look like 2026-11-14", ephemeral=True)
    data = load()
    item = find(data, garnishment)
    if not item:
        return await interaction.response.send_message("Pick a garnishment from the list.", ephemeral=True)
    item["payments"].append({"date": when, "amount": amt, "note": (note or "")[:200]})
    msg = f"💸 Logged {money(amt)} to **{item['creditor']}**. Left: **{money(left(item))}**"
    n = checks_left(item)
    if n:
        msg += f" (~{n} more paycheck{'s' if n != 1 else ''})"
    if left(item) <= 0:
        item["status"] = "paid"
        msg += "\n🎉 That should pay it off! Ask the court/creditor for a **release of garnishment** and send it to payroll."
    save(data)
    await interaction.response.send_message(msg, ephemeral=True)


def item_field(i):
    lines = [f"Owed: {money(i['balance'])} · Taken: **{money(taken(i))}** · Left: **{money(left(i))}**"]
    per = []
    if i.get("percent"):
        per.append(f"{i['percent']:g}% of take-home")
    if i.get("per_check"):
        per.append(f"{money(i['per_check'])}/check")
    if per:
        lines.append("Rate: " + " · ".join(per))
    n = checks_left(i)
    if n and i["status"] == "open":
        lines.append(f"About **{n}** paycheck{'s' if n != 1 else ''} to go")
    if i["payments"]:
        lines.append(f"Last taken: {money(i['payments'][-1]['amount'])} on {i['payments'][-1]['date']}")
    if i.get("case"):
        lines.append(f"Case: {i['case']}")
    if i.get("notes"):
        lines.append(i["notes"][:300])
    return "\n".join(lines)[:1024]


@garnish.command(name="list", description="All garnishments with what's left")
@app_commands.describe(show_closed="Also show paid-off / released ones")
async def g_list(interaction: discord.Interaction, show_closed: bool = False):
    items = [i for i in load()["items"] if show_closed or i["status"] == "open"]
    e = discord.Embed(title="Garnishments", color=COLOR)
    if not items:
        e.description = "Nothing to show. Add one with `/garnish add`."
    total_left = 0
    for i in items[:20]:
        total_left += left(i) if i["status"] == "open" else 0
        status = "" if i["status"] == "open" else f" ({i['status']})"
        e.add_field(name=f"#{i['id']} {i['creditor']} · {TYPES.get(i['type'], i['type'])}{status}"[:256],
                    value=item_field(i), inline=False)
    if items:
        e.description = f"Total still owed on open garnishments: **{money(total_left)}**"
    e.set_footer(text="Log deductions with /garnish paid · Only you can see this")
    await interaction.response.send_message(embed=e, ephemeral=True)


@garnish.command(name="history", description="Every deduction logged for one garnishment")
@app_commands.autocomplete(garnishment=any_ac)
async def g_history(interaction: discord.Interaction, garnishment: int):
    item = find(load(), garnishment)
    if not item:
        return await interaction.response.send_message("Pick a garnishment from the list.", ephemeral=True)
    rows = [f"`{p['date']}`  {money(p['amount'])}" + (f" · {p['note']}" if p.get("note") else "") for p in item["payments"]]
    text = "\n".join(rows[-40:]) or "No deductions logged yet."
    e = discord.Embed(title=f"#{item['id']} {item['creditor']} - history", description=text[:4000], color=COLOR)
    e.set_footer(text=f"Total taken {money(taken(item))} · Left {money(left(item))}")
    await interaction.response.send_message(embed=e, ephemeral=True)


@garnish.command(name="edit", description="Change the balance, rate or notes")
@app_commands.autocomplete(garnishment=any_ac)
@app_commands.describe(balance="New total owed", per_check="$ per paycheck", percent="% of take-home",
                       notes="Replace the notes", case="Case number")
async def g_edit(interaction: discord.Interaction, garnishment: int, balance: Optional[str] = None,
                 per_check: Optional[str] = None, percent: Optional[float] = None,
                 notes: Optional[str] = None, case: Optional[str] = None):
    data = load()
    item = find(data, garnishment)
    if not item:
        return await interaction.response.send_message("Pick a garnishment from the list.", ephemeral=True)
    if balance is not None:
        b = parse_money(balance)
        if b is None:
            return await interaction.response.send_message("❌ Balance should be a number.", ephemeral=True)
        item["balance"] = b
    if per_check is not None:
        item["per_check"] = parse_money(per_check)
    if percent is not None:
        item["percent"] = percent
    if notes is not None:
        item["notes"] = notes[:500]
    if case is not None:
        item["case"] = case[:60]
    if item["status"] == "paid" and left(item) > 0:
        item["status"] = "open"
    save(data)
    await interaction.response.send_message(f"✏️ Updated **#{item['id']} {item['creditor']}**.", ephemeral=True)


@garnish.command(name="close", description="Mark a garnishment as paid off or released")
@app_commands.autocomplete(garnishment=open_ac)
@app_commands.choices(reason=[app_commands.Choice(name="Paid off", value="paid"),
                              app_commands.Choice(name="Released / stopped by court", value="released"),
                              app_commands.Choice(name="Expired", value="expired")])
async def g_close(interaction: discord.Interaction, garnishment: int, reason: app_commands.Choice[str]):
    data = load()
    item = find(data, garnishment)
    if not item:
        return await interaction.response.send_message("Pick a garnishment from the list.", ephemeral=True)
    item["status"] = reason.value
    item["closed"] = datetime.date.today().isoformat()
    save(data)
    await interaction.response.send_message(f"✅ Marked **{item['creditor']}** as {reason.name.lower()}.", ephemeral=True)


def setup(bot):
    bot.tree.add_command(garnish)
