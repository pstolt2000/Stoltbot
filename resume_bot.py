"""
StoltBot resume builder
-----------------------
Anyone in the server can build a resume step by step in Discord and get a clean PDF back.
Everything is private ("only you can see this"), and each person's resume is kept separately.

  /resume basics      - name, job title, email, phone, location & links
  /resume summary     - a short "about me" paragraph
  /resume addjob      - add a job (title, company, dates, location, what you did)
  /resume editjob     - change a job you already added
  /resume addschool   - add school, a degree or a training program
  /resume editschool  - change a school entry
  /resume skills      - your skills (separate with commas)
  /resume addsection  - extra section: Certifications, Awards, Volunteer work, Languages...
  /resume move        - move a job or school up or down the list
  /resume remove      - delete one job, school or extra section
  /resume show        - preview what you have so far
  /resume check       - quick tips to make it stronger
  /resume pdf         - get your resume as a PDF (Classic or Modern style)
  /resume export      - download a backup of your resume data
  /resume import      - load a backup file
  /resume clear       - start over

Hooked up from discord_bot.py with:   import resume_bot; resume_bot.setup(bot)

Data is saved in resumes.json next to the bot (or in the folder named by the
DATA_DIR environment variable, e.g. a Railway Volume, so it survives redeploys).
"""

import io
import os
import re
import json
import pathlib
import datetime
from typing import Optional

import discord
from discord import app_commands
from fpdf import FPDF

# ===== SETTINGS =====
DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR") or os.environ.get("RESUME_DATA_DIR") or pathlib.Path(__file__).parent)
DATA_FILE = DATA_DIR / "resumes.json"
COLOR = discord.Color.from_rgb(52, 152, 219)   # embed color
MAX_JOBS = 15
MAX_SCHOOLS = 8
MAX_SECTIONS = 6


# ===== STORAGE =====
def _load_all():
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_all(data):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = DATA_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(DATA_FILE)
    except Exception as e:
        print(f"⚠️ Couldn't save resumes: {e}")


def blank_resume():
    return {"basics": {}, "summary": "", "jobs": [], "schools": [], "skills": [], "sections": []}


def get_resume(user_id):
    r = _load_all().get(str(user_id)) or blank_resume()
    base = blank_resume()
    base.update(r)
    return base


def save_resume(user_id, resume):
    data = _load_all()
    resume["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    data[str(user_id)] = resume
    _save_all(data)


def lines(text):
    """Split a box of text into clean lines (drops bullets/dashes people type at the start)."""
    out = []
    for ln in (text or "").splitlines():
        ln = re.sub(r"^\s*([-*•·▪●–—]|\d+[.)])\s*", "", ln).strip()
        if ln:
            out.append(ln)
    return out


# ===== PDF =====
_REPLACE = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
            "•": "-", "…": "...", " ": " ", "™": "(TM)", "→": "->"}


def pdf_text(s):
    """The built-in PDF fonts only know Western characters; swap or drop the rest."""
    s = "".join(_REPLACE.get(c, c) for c in str(s or ""))
    return s.encode("latin-1", "replace").decode("latin-1")


STYLES = {
    "classic": {"accent": (40, 40, 40), "font": "Times", "header_band": False},
    "modern": {"accent": (31, 97, 141), "font": "Helvetica", "header_band": True},
}


class ResumePDF(FPDF):
    def __init__(self, style):
        super().__init__(format="Letter")
        self.st = STYLES[style]
        self.set_margins(18, 16, 18)
        self.set_auto_page_break(True, margin=16)

    def font(self, size, bold=False, italic=False):
        self.set_font(self.st["font"], ("B" if bold else "") + ("I" if italic else ""), size)

    def heading(self, title):
        if self.get_y() > self.h - 40:
            self.add_page()
        self.ln(3)
        self.font(11.5, bold=True)
        self.set_text_color(*self.st["accent"])
        self.cell(0, 6, pdf_text(title.upper()), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*self.st["accent"])
        self.set_line_width(0.4)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)
        self.set_text_color(30, 30, 30)

    def two_sides(self, left, right, size=11, bold=True, italic=False):
        """Bold text on the left, dates or location lined up on the right."""
        self.font(size, bold=bold, italic=italic)
        right = pdf_text(right)
        rw = self.get_string_width(right) + 1 if right else 0
        y = self.get_y()
        self.multi_cell(self.epw - rw - 2, 5.2, pdf_text(left), new_x="LMARGIN", new_y="NEXT")
        end = self.get_y()
        if right:
            self.set_xy(self.w - self.r_margin - rw, y)
            self.font(10, italic=not italic)
            self.cell(rw, 5.2, right, align="R")
            self.set_xy(self.l_margin, end)

    def bullet(self, text):
        self.font(10.5)
        indent = 5
        y = self.get_y()
        self.set_fill_color(*self.st["accent"])
        self.ellipse(self.l_margin + 1.4, y + 1.9, 1.3, 1.3, style="F")
        self.set_x(self.l_margin + indent)
        self.multi_cell(self.epw - indent, 5, pdf_text(text), new_x="LMARGIN", new_y="NEXT")


def build_pdf(resume, style="modern"):
    b = resume["basics"]
    pdf = ResumePDF(style)
    pdf.add_page()
    st = pdf.st

    # ---- Header ----
    name = b.get("name") or "Your Name"
    contact = [x for x in (b.get("email"), b.get("phone")) if x] + lines(b.get("location", ""))
    if st["header_band"]:
        pdf.set_fill_color(*st["accent"])
        pdf.rect(0, 0, pdf.w, 34 if b.get("headline") else 28, style="F")
        pdf.set_y(9)
        pdf.set_text_color(255, 255, 255)
        pdf.font(22, bold=True)
        pdf.cell(0, 9, pdf_text(name), new_x="LMARGIN", new_y="NEXT")
        if b.get("headline"):
            pdf.font(12)
            pdf.cell(0, 6, pdf_text(b["headline"]), new_x="LMARGIN", new_y="NEXT")
        pdf.set_y(36 if b.get("headline") else 30)
        pdf.set_text_color(70, 70, 70)
        pdf.font(9.5)
        pdf.multi_cell(0, 5, pdf_text("   |   ".join(contact)), new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_text_color(20, 20, 20)
        pdf.font(22, bold=True)
        pdf.cell(0, 10, pdf_text(name), align="C", new_x="LMARGIN", new_y="NEXT")
        if b.get("headline"):
            pdf.font(12, italic=True)
            pdf.cell(0, 6, pdf_text(b["headline"]), align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.font(10)
        pdf.multi_cell(0, 5, pdf_text("  |  ".join(contact)), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(30, 30, 30)

    # ---- Sections ----
    if resume["summary"]:
        pdf.heading("Summary")
        pdf.font(10.5)
        pdf.multi_cell(0, 5, pdf_text(resume["summary"]), align="L", new_x="LMARGIN", new_y="NEXT")

    if resume["jobs"]:
        pdf.heading("Experience")
        for j in resume["jobs"]:
            pdf.two_sides(j.get("title", ""), j.get("dates", ""))
            sub = " - ".join(x for x in (j.get("company"), j.get("location")) if x)
            if sub:
                pdf.font(10.5, italic=True)
                pdf.multi_cell(0, 5, pdf_text(sub), new_x="LMARGIN", new_y="NEXT")
            for ln in lines(j.get("bullets", "")):
                pdf.bullet(ln)
            pdf.ln(2)

    if resume["skills"]:
        pdf.heading("Skills")
        pdf.font(10.5)
        pdf.multi_cell(0, 5, pdf_text("  -  ".join(resume["skills"])) if style == "classic"
                       else pdf_text("  |  ".join(resume["skills"])), align="L", new_x="LMARGIN", new_y="NEXT")

    if resume["schools"]:
        pdf.heading("Education")
        for s in resume["schools"]:
            pdf.two_sides(s.get("degree", ""), s.get("dates", ""))
            sub = " - ".join(x for x in (s.get("school"), s.get("location")) if x)
            if sub:
                pdf.font(10.5, italic=True)
                pdf.multi_cell(0, 5, pdf_text(sub), new_x="LMARGIN", new_y="NEXT")
            for ln in lines(s.get("details", "")):
                pdf.bullet(ln)
            pdf.ln(1.5)

    for sec in resume["sections"]:
        pdf.heading(sec.get("title", "More"))
        for ln in lines(sec.get("items", "")):
            pdf.bullet(ln)

    return bytes(pdf.output()), pdf.page_no()


# ===== RESUME CHECK (simple rules, no AI needed) =====
WEAK_STARTS = ("responsible for", "duties included", "helped with", "worked on", "in charge of", "tasked with", "assisted with")


def review(resume):
    tips, b = [], resume["basics"]
    if not b.get("name"):
        tips.append("❗ Add your name with `/resume basics`.")
    if not b.get("email") and not b.get("phone"):
        tips.append("❗ Add an email or phone number so employers can reach you.")
    if b.get("email") and not re.match(r"[^@\s]+@[^@\s]+\.\w+$", b["email"]):
        tips.append("❗ Your email looks mistyped: double-check it.")
    if not resume["jobs"]:
        tips.append("❗ Add at least one job with `/resume addjob`.")
    if not resume["summary"]:
        tips.append("💡 A 2-3 sentence summary at the top helps readers see your fit fast (`/resume summary`).")
    elif len(resume["summary"].split()) > 80:
        tips.append("💡 Your summary is long. Aim for 2-3 sentences (under ~60 words).")
    if len(resume["skills"]) < 4:
        tips.append("💡 List 6-12 skills: tools, software and certifications employers search for.")

    has_number = False
    for j in resume["jobs"]:
        bl = lines(j.get("bullets", ""))
        label = f"**{j.get('title') or 'a job'}**"
        if not bl:
            tips.append(f"💡 {label} has no bullet points. Add 2-5 things you did or achieved.")
        if len(bl) > 7:
            tips.append(f"💡 {label} has {len(bl)} bullets. Keep the best 3-6.")
        for ln in bl:
            if any(ln.lower().startswith(w) for w in WEAK_STARTS):
                tips.append(f"✏️ {label}: start \"{ln[:45]}...\" with an action verb (Led, Built, Trained, Cut, Improved).")
            if len(ln.split()) > 35:
                tips.append(f"✏️ {label}: \"{ln[:45]}...\" is long. Try one line, two at most.")
            if re.search(r"\d", ln):
                has_number = True
        if not j.get("dates"):
            tips.append(f"💡 {label} is missing dates.")
    if resume["jobs"] and not has_number:
        tips.append("📈 None of your bullets have numbers. Add results like \"99.8% accuracy\", \"trained 12 new hires\" or \"1,200 packages a shift\".")
    return tips


# ===== DISCORD UI =====
def preview_embed(resume, user):
    b = resume["basics"]
    e = discord.Embed(title=b.get("name") or "Your resume", description=b.get("headline") or None, color=COLOR)
    contact = [x for x in (b.get("email"), b.get("phone")) if x] + lines(b.get("location", ""))
    if contact:
        e.add_field(name="Contact", value="\n".join(contact)[:1024], inline=False)
    if resume["summary"]:
        e.add_field(name="Summary", value=resume["summary"][:1024], inline=False)
    for i, j in enumerate(resume["jobs"], 1):
        bl = lines(j.get("bullets", ""))
        val = (f"*{j.get('company', '')}*" + (f" · {j['location']}" if j.get("location") else "")
               + (f"\n{j['dates']}" if j.get("dates") else "")
               + "".join(f"\n• {x}" for x in bl))
        e.add_field(name=f"💼 {i}. {j.get('title', '')}"[:256], value=val[:1024] or "-", inline=False)
    for i, s in enumerate(resume["schools"], 1):
        val = (f"*{s.get('school', '')}*" + (f"\n{s['dates']}" if s.get("dates") else "")
               + "".join(f"\n• {x}" for x in lines(s.get("details", ""))))
        e.add_field(name=f"🎓 {i}. {s.get('degree', '')}"[:256], value=val[:1024] or "-", inline=False)
    if resume["skills"]:
        e.add_field(name="🛠️ Skills", value=", ".join(resume["skills"])[:1024], inline=False)
    for sec in resume["sections"]:
        e.add_field(name=f"📌 {sec.get('title', '')}"[:256],
                    value="\n".join(f"• {x}" for x in lines(sec.get("items", "")))[:1024] or "-", inline=False)
    if len(e) > 5800 or len(e.fields) > 25:   # Discord's embed limits
        e.clear_fields()
        e.description = "Your resume is too big to preview here. Use `/resume pdf` to see it all."
    e.set_footer(text="Use /resume pdf to download it · /resume check for tips")
    return e


def field(label, default="", placeholder="", long=False, required=False, max_length=None):
    return discord.ui.TextInput(label=label, default=(default or "")[: (max_length or 4000)], placeholder=placeholder,
                                style=discord.TextStyle.paragraph if long else discord.TextStyle.short,
                                required=required, max_length=max_length or (4000 if long else 200))


class BasicsModal(discord.ui.Modal, title="Resume: the basics"):
    def __init__(self, resume):
        super().__init__()
        b = resume["basics"]
        self.name = field("Full name", b.get("name"), "Paul Stolt", required=True, max_length=80)
        self.headline = field("Job title / headline", b.get("headline"), "Warehouse Lead · Python Developer", max_length=100)
        self.email = field("Email", b.get("email"), "you@example.com", max_length=100)
        self.phone = field("Phone", b.get("phone"), "(248) 555-0123", max_length=40)
        self.location = field("City, state & links (one per line)", b.get("location"),
                              "Auburn Hills, MI\nlinkedin.com/in/you\ngithub.com/you", long=True, max_length=300)
        for f in (self.name, self.headline, self.email, self.phone, self.location):
            self.add_item(f)

    async def on_submit(self, interaction: discord.Interaction):
        r = get_resume(interaction.user.id)
        r["basics"] = {"name": self.name.value.strip(), "headline": self.headline.value.strip(),
                       "email": self.email.value.strip(), "phone": self.phone.value.strip(),
                       "location": self.location.value.strip()}
        save_resume(interaction.user.id, r)
        await interaction.response.send_message("✅ Basics saved. Next: `/resume summary` or `/resume addjob`.", ephemeral=True)


class SummaryModal(discord.ui.Modal, title="Resume: summary"):
    def __init__(self, resume):
        super().__init__()
        self.text = field("2-3 sentences about you", resume["summary"],
                          "Reliable team lead with 5 years in high-volume logistics...", long=True, max_length=800)
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        r = get_resume(interaction.user.id)
        r["summary"] = " ".join(self.text.value.split())
        save_resume(interaction.user.id, r)
        await interaction.response.send_message("✅ Summary saved.", ephemeral=True)


class JobModal(discord.ui.Modal):
    def __init__(self, job=None, index=None):
        super().__init__(title="Resume: edit job" if index is not None else "Resume: add a job")
        job, self.index = job or {}, index
        self.title_ = field("Job title", job.get("title"), "Shift Lead", required=True, max_length=100)
        self.company = field("Company", job.get("company"), "Amazon", required=True, max_length=100)
        self.dates = field("Dates", job.get("dates"), "Mar 2021 - Present", max_length=50)
        self.location = field("Location", job.get("location"), "Detroit, MI", max_length=80)
        self.bullets = field("What you did (one per line)", job.get("bullets"),
                             "Trained 12 new associates on safety and scanning\nKept 99.8% pick accuracy",
                             long=True, max_length=2000)
        for f in (self.title_, self.company, self.dates, self.location, self.bullets):
            self.add_item(f)

    async def on_submit(self, interaction: discord.Interaction):
        r = get_resume(interaction.user.id)
        job = {"title": self.title_.value.strip(), "company": self.company.value.strip(), "dates": self.dates.value.strip(),
               "location": self.location.value.strip(), "bullets": "\n".join(lines(self.bullets.value))}
        if self.index is not None and self.index < len(r["jobs"]):
            r["jobs"][self.index] = job
            msg = f"✅ Updated **{job['title']}**."
        else:
            r["jobs"].append(job)
            msg = (f"✅ Added **{job['title']}** at {job['company']} (job #{len(r['jobs'])}). "
                   "Add another with `/resume addjob`, or reorder with `/resume move`.")
        save_resume(interaction.user.id, r)
        await interaction.response.send_message(msg, ephemeral=True)


class SchoolModal(discord.ui.Modal):
    def __init__(self, school=None, index=None):
        super().__init__(title="Resume: edit school" if index is not None else "Resume: add school/training")
        school, self.index = school or {}, index
        self.degree = field("Degree, certificate or program", school.get("degree"), "BBA, Accounting", required=True, max_length=120)
        self.school = field("School", school.get("school"), "Walsh College", max_length=120)
        self.dates = field("Dates", school.get("dates"), "2018 - 2022", max_length=50)
        self.location = field("Location", school.get("location"), "Troy, MI", max_length=80)
        self.details = field("Details (optional, one per line)", school.get("details"),
                             "Dean's List\nRelevant courses: Tax, Auditing", long=True, max_length=800)
        for f in (self.degree, self.school, self.dates, self.location, self.details):
            self.add_item(f)

    async def on_submit(self, interaction: discord.Interaction):
        r = get_resume(interaction.user.id)
        s = {"degree": self.degree.value.strip(), "school": self.school.value.strip(), "dates": self.dates.value.strip(),
             "location": self.location.value.strip(), "details": "\n".join(lines(self.details.value))}
        if self.index is not None and self.index < len(r["schools"]):
            r["schools"][self.index] = s
            msg = f"✅ Updated **{s['degree']}**."
        else:
            r["schools"].append(s)
            msg = f"✅ Added **{s['degree']}**."
        save_resume(interaction.user.id, r)
        await interaction.response.send_message(msg, ephemeral=True)


class SkillsModal(discord.ui.Modal, title="Resume: skills"):
    def __init__(self, resume):
        super().__init__()
        self.text = field("Skills (separate with commas)", ", ".join(resume["skills"]),
                          "Python, Excel, WordPress, Forklift certified, Inventory control", long=True, max_length=1000)
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        r = get_resume(interaction.user.id)
        seen, skills = set(), []
        for s in re.split(r"[,\n;]", self.text.value):
            s = s.strip(" -•")
            if s and s.lower() not in seen:
                seen.add(s.lower())
                skills.append(s)
        r["skills"] = skills
        save_resume(interaction.user.id, r)
        await interaction.response.send_message(f"✅ Saved {len(skills)} skills.", ephemeral=True)


class SectionModal(discord.ui.Modal, title="Resume: extra section"):
    def __init__(self, title=""):
        super().__init__()
        self.sec_title = field("Section name", title, "Certifications", required=True, max_length=40)
        self.items = field("Items (one per line)", "", "CompTIA Security+ (in progress)\nOSHA 10", long=True,
                           required=True, max_length=1500)
        self.add_item(self.sec_title)
        self.add_item(self.items)

    async def on_submit(self, interaction: discord.Interaction):
        r = get_resume(interaction.user.id)
        title = self.sec_title.value.strip()
        for sec in r["sections"]:   # same name = replace that section
            if sec["title"].lower() == title.lower():
                sec["items"] = "\n".join(lines(self.items.value))
                break
        else:
            r["sections"].append({"title": title, "items": "\n".join(lines(self.items.value))})
        save_resume(interaction.user.id, r)
        await interaction.response.send_message(f"✅ Saved the **{title}** section.", ephemeral=True)


class ConfirmClear(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=60)
        self.user_id = user_id

    @discord.ui.button(label="Yes, delete my resume", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = _load_all()
        data.pop(str(self.user_id), None)
        _save_all(data)
        await interaction.response.edit_message(content="🗑️ Your resume was deleted. Start again with `/resume basics`.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="👍 Nothing was deleted.", view=None)


# ===== COMMANDS =====
resume = app_commands.Group(name="resume", description="Build a resume and get it as a PDF")


async def job_ac(interaction: discord.Interaction, current: str):
    r = get_resume(interaction.user.id)
    return [app_commands.Choice(name=f"{i+1}. {j.get('title')} - {j.get('company')}"[:100], value=i)
            for i, j in enumerate(r["jobs"]) if current.lower() in f"{j.get('title')} {j.get('company')}".lower()][:25]


async def school_ac(interaction: discord.Interaction, current: str):
    r = get_resume(interaction.user.id)
    return [app_commands.Choice(name=f"{i+1}. {s.get('degree')} - {s.get('school')}"[:100], value=i)
            for i, s in enumerate(r["schools"]) if current.lower() in f"{s.get('degree')} {s.get('school')}".lower()][:25]


async def item_ac(interaction: discord.Interaction, current: str):
    r = get_resume(interaction.user.id)
    opts = ([(f"Job: {j.get('title')} - {j.get('company')}", f"jobs:{i}") for i, j in enumerate(r["jobs"])]
            + [(f"School: {s.get('degree')}", f"schools:{i}") for i, s in enumerate(r["schools"])]
            + [(f"Section: {s.get('title')}", f"sections:{i}") for i, s in enumerate(r["sections"])]
            + ([("Summary", "summary")] if r["summary"] else []) + ([("Skills", "skills")] if r["skills"] else []))
    return [app_commands.Choice(name=n[:100], value=v) for n, v in opts if current.lower() in n.lower()][:25]


@resume.command(name="basics", description="Your name, job title and contact info")
async def r_basics(interaction: discord.Interaction):
    await interaction.response.send_modal(BasicsModal(get_resume(interaction.user.id)))


@resume.command(name="summary", description="A short paragraph about you at the top")
async def r_summary(interaction: discord.Interaction):
    await interaction.response.send_modal(SummaryModal(get_resume(interaction.user.id)))


@resume.command(name="addjob", description="Add a job (most recent first)")
async def r_addjob(interaction: discord.Interaction):
    if len(get_resume(interaction.user.id)["jobs"]) >= MAX_JOBS:
        return await interaction.response.send_message(f"You already have {MAX_JOBS} jobs. Remove one first.", ephemeral=True)
    await interaction.response.send_modal(JobModal())


@resume.command(name="editjob", description="Change a job you already added")
@app_commands.describe(job="Which job")
@app_commands.autocomplete(job=job_ac)
async def r_editjob(interaction: discord.Interaction, job: int):
    r = get_resume(interaction.user.id)
    if not 0 <= job < len(r["jobs"]):
        return await interaction.response.send_message("Pick a job from the list.", ephemeral=True)
    await interaction.response.send_modal(JobModal(r["jobs"][job], job))


@resume.command(name="addschool", description="Add a school, degree, certificate program or training")
async def r_addschool(interaction: discord.Interaction):
    if len(get_resume(interaction.user.id)["schools"]) >= MAX_SCHOOLS:
        return await interaction.response.send_message("That's a lot of schools! Remove one first.", ephemeral=True)
    await interaction.response.send_modal(SchoolModal())


@resume.command(name="editschool", description="Change a school entry")
@app_commands.describe(school="Which one")
@app_commands.autocomplete(school=school_ac)
async def r_editschool(interaction: discord.Interaction, school: int):
    r = get_resume(interaction.user.id)
    if not 0 <= school < len(r["schools"]):
        return await interaction.response.send_message("Pick one from the list.", ephemeral=True)
    await interaction.response.send_modal(SchoolModal(r["schools"][school], school))


@resume.command(name="skills", description="Your skills, separated by commas")
async def r_skills(interaction: discord.Interaction):
    await interaction.response.send_modal(SkillsModal(get_resume(interaction.user.id)))


@resume.command(name="addsection", description="Extra section: Certifications, Awards, Volunteer, Languages...")
@app_commands.describe(name="Section name (same name replaces that section)")
async def r_addsection(interaction: discord.Interaction, name: Optional[str] = None):
    r = get_resume(interaction.user.id)
    if len(r["sections"]) >= MAX_SECTIONS and not any(s["title"].lower() == (name or "").lower() for s in r["sections"]):
        return await interaction.response.send_message(f"You have {MAX_SECTIONS} extra sections. Remove one first.", ephemeral=True)
    modal = SectionModal(name or "")
    existing = next((s for s in r["sections"] if name and s["title"].lower() == name.lower()), None)
    if existing:
        modal.items.default = existing["items"]
    await interaction.response.send_modal(modal)


@resume.command(name="move", description="Move a job or school up or down the list")
@app_commands.describe(item="What to move", direction="Up = closer to the top")
@app_commands.autocomplete(item=item_ac)
@app_commands.choices(direction=[app_commands.Choice(name="Up", value=-1), app_commands.Choice(name="Down", value=1),
                                 app_commands.Choice(name="To the top", value=-99)])
async def r_move(interaction: discord.Interaction, item: str, direction: app_commands.Choice[int]):
    r = get_resume(interaction.user.id)
    try:
        key, i = item.split(":")
        i = int(i)
        lst = r[key]
        lst[i]
    except Exception:
        return await interaction.response.send_message("Pick a job, school or section from the list.", ephemeral=True)
    new = max(0, min(len(lst) - 1, i + direction.value))
    lst.insert(new, lst.pop(i))
    save_resume(interaction.user.id, r)
    await interaction.response.send_message(f"↕️ Moved it to spot #{new + 1}.", ephemeral=True)


@resume.command(name="remove", description="Delete a job, school or section")
@app_commands.describe(item="What to delete")
@app_commands.autocomplete(item=item_ac)
async def r_remove(interaction: discord.Interaction, item: str):
    r = get_resume(interaction.user.id)
    if item == "summary":
        r["summary"] = ""
        label = "your summary"
    elif item == "skills":
        r["skills"] = []
        label = "your skills"
    else:
        try:
            key, i = item.split(":")
            gone = r[key].pop(int(i))
            label = gone.get("title") or gone.get("degree") or "that item"
        except Exception:
            return await interaction.response.send_message("Pick something from the list.", ephemeral=True)
    save_resume(interaction.user.id, r)
    await interaction.response.send_message(f"🗑️ Removed **{label}**.", ephemeral=True)


@resume.command(name="show", description="Preview your resume")
async def r_show(interaction: discord.Interaction):
    await interaction.response.send_message(embed=preview_embed(get_resume(interaction.user.id), interaction.user), ephemeral=True)


@resume.command(name="check", description="Quick tips to make your resume stronger")
async def r_check(interaction: discord.Interaction):
    tips = review(get_resume(interaction.user.id))
    text = "\n".join(tips[:15]) if tips else "🎉 Looks solid! Grab it with `/resume pdf`."
    await interaction.response.send_message(embed=discord.Embed(title="Resume check", description=text[:4000], color=COLOR),
                                            ephemeral=True)


@resume.command(name="pdf", description="Get your resume as a PDF")
@app_commands.describe(style="Look of the PDF")
@app_commands.choices(style=[app_commands.Choice(name="Modern (blue header)", value="modern"),
                             app_commands.Choice(name="Classic (black & white, serif)", value="classic")])
async def r_pdf(interaction: discord.Interaction, style: Optional[app_commands.Choice[str]] = None):
    r = get_resume(interaction.user.id)
    if not r["basics"].get("name"):
        return await interaction.response.send_message("Start with `/resume basics` so the PDF has your name.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        data, pages = build_pdf(r, style.value if style else "modern")
    except Exception as e:
        return await interaction.followup.send(f"❌ Couldn't build the PDF: {e}", ephemeral=True)
    fname = re.sub(r"[^A-Za-z0-9]+", "_", r["basics"]["name"]).strip("_") + "_Resume.pdf"
    note = "📄 Here's your resume!" + ("" if pages <= 2 else f" It's {pages} pages; most employers like 1-2. Try trimming bullets.")
    await interaction.followup.send(note, file=discord.File(io.BytesIO(data), filename=fname), ephemeral=True)


@resume.command(name="export", description="Download a backup of your resume data")
async def r_export(interaction: discord.Interaction):
    r = get_resume(interaction.user.id)
    data = json.dumps(r, indent=2).encode("utf-8")
    await interaction.response.send_message("💾 Keep this file. Load it later with `/resume import`.",
                                            file=discord.File(io.BytesIO(data), filename="my_resume_backup.json"), ephemeral=True)


@resume.command(name="import", description="Load a backup made with /resume export")
@app_commands.describe(file="The my_resume_backup.json file")
async def r_import(interaction: discord.Interaction, file: discord.Attachment):
    if file.size > 200_000:
        return await interaction.response.send_message("That file is too big to be a resume backup.", ephemeral=True)
    try:
        loaded = json.loads((await file.read()).decode("utf-8"))
        r = blank_resume()
        for k in r:
            if k in loaded and isinstance(loaded[k], type(r[k])):
                r[k] = loaded[k]
    except Exception:
        return await interaction.response.send_message("❌ That doesn't look like a resume backup file.", ephemeral=True)
    save_resume(interaction.user.id, r)
    await interaction.response.send_message("✅ Resume loaded. Check it with `/resume show`.", ephemeral=True)


@resume.command(name="clear", description="Delete your resume and start over")
async def r_clear(interaction: discord.Interaction):
    await interaction.response.send_message("Delete your whole resume? This can't be undone (unless you have a backup).",
                                            view=ConfirmClear(interaction.user.id), ephemeral=True)


def setup(bot):
    bot.tree.add_command(resume)
