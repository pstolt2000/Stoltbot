/**
 * Paul's Budget Bot (Google Sheets + Apps Script)
 * Create a Google Sheet, then Extensions → Apps Script, paste this, run setup() once.
 *
 * - Every hour: reads bank alert emails and logs purchases to the Transactions tab
 * - Every morning: bill reminders (email + Discord)
 * - 1st of each month: "where the money went" report for last month (email + Discord)
 * - Web link for StoltBot: /spent, /income, /balance, /budget, /bills, /grocery, /price from Discord
 * - Groceries: shopping list, staples, meal planner, price tracker
 * - Pantry: what's in stock, low-stock auto-restock, expiration reminders
 * - Vacation planner: /trip and /pack (savings goal, countdown, itinerary, packing, trip spending, quotes)
 *
 * Never put account numbers or passwords in this sheet.
 */

// ===== SETTINGS =====
// Your private values live in Project Settings (⚙️) → Script Properties, NOT in this code,
// so you can paste new versions of this file without losing them:
//   SECRET           the code from makeSecret() (also goes in StoltBot's budget.txt)
//   DISCORD_WEBHOOK  optional: webhook URL of your PRIVATE #budget channel
//   EMAIL_TO         optional: where reminders go (blank = your own Gmail)
const prop_ = key => PropertiesService.getScriptProperties().getProperty(key) || '';
const SETTINGS = {
  get DISCORD_WEBHOOK() { return prop_('DISCORD_WEBHOOK'); },
  get EMAIL_TO() { return prop_('EMAIL_TO'); },
  get SECRET() { return prop_('SECRET'); },
  REMIND_HOUR: 8,        // bill reminders around 8 AM
};

// Bank alert emails to read. Edit after you see what your bank's alerts look like.
const BANK_ALERTS = [
  {
    name: 'My bank',
    query: 'from:(alerts@yourbank.com) newer_than:30d',               // ← change to your bank's alert sender
    amount: /\$\s?([\d,]+\.\d{2})/,                                   // finds "$23.14"
    merchant: /(?:\bat\b|\bwith\b|\bto\b|merchant:?)\s+([^\n]+?)(?:\s+on\s|\s+was\s|\s+has\s|\.\s|\n|$)/i,  // finds "at KROGER #123"
  },
];

const DEFAULT_CATEGORIES = [
  ['Rent', 0], ['Groceries', 0], ['Eating Out', 0], ['Transportation', 0], ['Bills & Utilities', 0],
  ['Phone & Internet', 0], ['Shopping', 0], ['Entertainment', 0], ['Health', 0], ['Other', 0],
];
const DEFAULT_RULES = [
  ['KROGER', 'Groceries'], ['MEIJER', 'Groceries'], ['ALDI', 'Groceries'], ['WALMART', 'Groceries'],
  ['MCDONALD', 'Eating Out'], ['TACO BELL', 'Eating Out'], ['DOORDASH', 'Eating Out'], ['STARBUCKS', 'Eating Out'],
  ['SHELL', 'Transportation'], ['SPEEDWAY', 'Transportation'], ['SMART', 'Transportation'], ['UBER', 'Transportation'],
  ['DTE', 'Bills & Utilities'], ['CONSUMERS', 'Bills & Utilities'],
  ['T-MOBILE', 'Phone & Internet'], ['VERIZON', 'Phone & Internet'], ['XFINITY', 'Phone & Internet'],
  ['NETFLIX', 'Entertainment'], ['SPOTIFY', 'Entertainment'], ['STEAM', 'Entertainment'], ['BLIZZARD', 'Entertainment'],
  ['AMAZON', 'Shopping'], ['TARGET', 'Shopping'], ['CVS', 'Health'], ['WALGREENS', 'Health'],
];

// ===== SHEET HELPERS =====
function sheet_(name, headers) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    if (headers) {
      sh.appendRow(headers);
      sh.getRange(1, 1, 1, headers.length).setFontWeight('bold').setBackground('#e8eaf6');
      sh.setFrozenRows(1);
    }
  }
  return sh;
}
const tx_ = () => sheet_('Transactions', ['Date', 'Amount', 'Merchant', 'Category', 'Note', 'Source', 'ID']);
const budget_ = () => sheet_('Budget', ['Category', 'Monthly budget']);
const bills_ = () => sheet_('Bills', ['Bill', 'Amount', 'Due day (1-31)', 'Remind days before', 'Category', 'Last reminded']);
const rules_ = () => sheet_('Rules', ['If merchant contains', 'Category']);
const income_ = () => sheet_('Income', ['Date', 'Amount', 'Source', 'Note']);
const accounts_ = () => sheet_('Accounts', ['Account', 'Balance', 'Last updated']);

function setBalance_(account, amount) {
  const sh = accounts_();
  const name = String(account || 'Focus Card').trim();
  const rows = rows_(sh);
  const i = rows.findIndex(r => String(r[0]).toLowerCase() === name.toLowerCase());
  if (i >= 0) sh.getRange(i + 2, 2, 1, 2).setValues([[amount, new Date()]]);
  else sh.appendRow([name, amount, new Date()]);
  return name;
}

function balances_() {
  return rows_(accounts_()).filter(r => r[0]).map(r => ({ account: r[0], balance: Number(r[1]) || 0, updated: r[2] }));
}

// Bills still to pay later this month (due after today)
function billsLeftThisMonth_() {
  const today = new Date();
  const lastDay = new Date(today.getFullYear(), today.getMonth() + 1, 0).getDate();
  return rows_(bills_()).filter(r => r[0] && r[2] && Math.min(Number(r[2]), lastDay) > today.getDate())
    .map(r => ({ name: r[0], amount: Number(r[1]) || 0, day: Math.min(Number(r[2]), lastDay) }));
}

function rows_(sh) {
  const last = sh.getLastRow();
  return last < 2 ? [] : sh.getRange(2, 1, last - 1, sh.getLastColumn()).getValues();
}

function categories_() {
  return rows_(budget_()).map(r => String(r[0]).trim()).filter(Boolean);
}

function categorize_(merchant) {
  const m = String(merchant || '').toUpperCase();
  for (const [key, cat] of rows_(rules_())) {
    if (key && m.includes(String(key).toUpperCase())) return cat;
  }
  return 'Other';
}

function addTransaction_(date, amount, merchant, category, note, source, id) {
  const cats = categories_();
  const match = cats.find(c => c.toLowerCase() === String(category || '').toLowerCase());
  const cat = match || categorize_(merchant);
  tx_().appendRow([date, Number(amount), merchant || '', cat, note || '', source || '', id || '']);
  return cat;
}

// ===== 1. IMPORT BANK ALERT EMAILS =====
function importBankEmails() {
  const seen = new Set(rows_(tx_()).map(r => String(r[6])));
  let added = 0;
  BANK_ALERTS.forEach(bank => {
    GmailApp.search(bank.query, 0, 100).forEach(thread => {
      thread.getMessages().forEach(msg => {
        const id = msg.getId();
        if (seen.has(id)) return;
        const text = `${msg.getSubject()}\n${msg.getPlainBody()}`;
        const a = text.match(bank.amount);
        if (!a) return;
        const m = text.match(bank.merchant);
        const merchant = m ? m[1].trim().slice(0, 60) : '(unknown)';
        addTransaction_(msg.getDate(), Number(a[1].replace(/,/g, '')), merchant, '', '', bank.name, id);
        seen.add(id);
        added++;
      });
    });
  });
  console.log(`Imported ${added} new transaction(s)`);
}

// ===== 2. MONTH SUMMARY =====
function monthSummary_(year, month) {   // month = 0-11
  const spent = {}, merchants = {};
  let total = 0;
  rows_(tx_()).forEach(([date, amount, merchant, cat]) => {
    const d = new Date(date);
    if (d.getFullYear() !== year || d.getMonth() !== month) return;
    const amt = Number(amount) || 0;
    total += amt;
    spent[cat] = (spent[cat] || 0) + amt;
    const key = String(merchant || '(none)');
    merchants[key] = (merchants[key] || 0) + amt;
  });
  const budgets = {};
  rows_(budget_()).forEach(([cat, b]) => { if (cat) budgets[cat] = Number(b) || 0; });
  const cats = [...new Set([...Object.keys(budgets), ...Object.keys(spent)])]
    .map(c => ({ category: c, spent: spent[c] || 0, budget: budgets[c] || 0 }))
    .filter(c => c.spent || c.budget)
    .sort((a, b) => b.spent - a.spent);
  const top = Object.entries(merchants).sort((a, b) => b[1] - a[1]).slice(0, 5);
  const budgetTotal = Object.values(budgets).reduce((s, b) => s + b, 0);
  let income = 0;
  rows_(income_()).forEach(([date, amount]) => {
    const d = new Date(date);
    if (d.getFullYear() === year && d.getMonth() === month) income += Number(amount) || 0;
  });
  return { year, month, total, budgetTotal, cats, top, income };
}

const money_ = n => '$' + Number(n).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
const monthName_ = (y, m) => Utilities.formatDate(new Date(y, m, 1), Session.getScriptTimeZone(), 'MMMM yyyy');

function summaryText_(s, prev, withBalances) {
  let t = '';
  if (s.income) t += `Income: ${money_(s.income)}\n`;
  t += `Total spent: ${money_(s.total)}` + (s.budgetTotal ? ` of ${money_(s.budgetTotal)} budget` : '') + '\n';
  if (s.income) {
    const net = s.income - s.total;
    t += net >= 0 ? `Saved: ${money_(net)} ✅\n` : `Spent more than earned by ${money_(-net)} ⚠️\n`;
  }
  if (prev) {
    const diff = s.total - prev.total;
    t += `vs. ${monthName_(prev.year, prev.month)}: ${diff >= 0 ? '▲' : '▼'} ${money_(Math.abs(diff))}\n`;
  }
  t += '\nBy category:\n';
  s.cats.forEach(c => {
    const flag = c.budget && c.spent > c.budget ? ' ⚠️ over' : '';
    t += `• ${c.category}: ${money_(c.spent)}` + (c.budget ? ` / ${money_(c.budget)}` : '') + flag + '\n';
  });
  if (s.top.length) {
    t += '\nTop places:\n' + s.top.map(([m, a]) => `• ${m}: ${money_(a)}`).join('\n') + '\n';
  }
  if (withBalances) {
    const bal = balances_();
    if (bal.length) {
      const tz = Session.getScriptTimeZone();
      t += '\nBalances:\n' + bal.map(b => `• ${b.account}: ${money_(b.balance)}` +
        (b.updated ? ` (as of ${Utilities.formatDate(new Date(b.updated), tz, 'MMM d')})` : '')).join('\n') + '\n';
      const left = billsLeftThisMonth_();
      const owed = left.reduce((x, b) => x + b.amount, 0);
      // Money in/out logged AFTER the latest balance update hasn't been counted in that balance yet
      const since = Math.max(...bal.map(b => b.updated ? new Date(b.updated).getTime() : 0));
      const spentSince = rows_(tx_()).filter(r => r[0] && new Date(r[0]).getTime() > since)
        .reduce((x, r) => x + (Number(r[1]) || 0), 0);
      const incomeSince = rows_(income_()).filter(r => r[0] && new Date(r[0]).getTime() > since)
        .reduce((x, r) => x + (Number(r[1]) || 0), 0);
      const total = bal.reduce((x, b) => x + b.balance, 0) - spentSince + incomeSince;
      if (spentSince) t += `Spent since last balance update: −${money_(spentSince)}\n`;
      if (incomeSince) t += `Income since last balance update: +${money_(incomeSince)}\n`;
      if (left.length) t += `Bills still due this month: −${money_(owed)} (${left.map(b => b.name).join(', ')})\n`;
      t += `Free to spend: ${money_(total - owed)}` + (total - owed < 0 ? ' ⚠️' : '') + '\n';
    }
  }
  return t;
}

function writeSummaryTab_(s) {
  const sh = sheet_('Summary');
  sh.clear();
  sh.appendRow([`Where the money went: ${monthName_(s.year, s.month)}`]);
  sh.getRange(1, 1).setFontWeight('bold').setFontSize(14);
  sh.appendRow(['Category', 'Spent', 'Budget', 'Left']);
  sh.getRange(2, 1, 1, 4).setFontWeight('bold').setBackground('#e8eaf6');
  s.cats.forEach(c => sh.appendRow([c.category, c.spent, c.budget, c.budget ? c.budget - c.spent : '']));
  sh.appendRow(['TOTAL', s.total, s.budgetTotal, s.budgetTotal ? s.budgetTotal - s.total : '']);
  sh.getRange(3, 2, s.cats.length + 1, 3).setNumberFormat('$#,##0.00');
  sh.autoResizeColumns(1, 4);
}

// Runs on the 1st of each month: report for LAST month
function monthlyReport() {
  const now = new Date();
  const last = new Date(now.getFullYear(), now.getMonth() - 1, 1);
  const before = new Date(now.getFullYear(), now.getMonth() - 2, 1);
  const s = monthSummary_(last.getFullYear(), last.getMonth());
  const p = monthSummary_(before.getFullYear(), before.getMonth());
  writeSummaryTab_(s);
  notify_(`💸 Where the money went: ${monthName_(s.year, s.month)}`, summaryText_(s, p));
}

// ===== 3. BILL REMINDERS =====
function upcomingBills_(daysAhead) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const out = [];
  rows_(bills_()).forEach((r, i) => {
    const [name, amount, dueDay, remindDays] = r;
    if (!name || !dueDay) return;
    for (const offset of [0, 1]) {   // this month and next month
      const y = today.getFullYear(), m = today.getMonth() + offset;
      const lastDay = new Date(y, m + 1, 0).getDate();
      const due = new Date(y, m, Math.min(Number(dueDay), lastDay));
      const days = Math.round((due - today) / 86400000);
      const win = daysAhead != null ? daysAhead : (Number(remindDays) || 3);
      if (days >= 0 && days <= win) {
        out.push({ row: i + 2, name, amount: Number(amount) || 0, due, days });
        break;
      }
    }
  });
  return out.sort((a, b) => a.days - b.days);
}

function dailyCheck() {
  try { tripReminders_(); } catch (e) { console.log('Trip reminders: ' + e.message); }
  try { pantryReminders_(); } catch (e) { console.log('Pantry reminders: ' + e.message); }
  try { careerReminder_(); } catch (e) { console.log('Career reminder: ' + e.message); }
  const sh = bills_();
  const due = upcomingBills_();
  const fresh = due.filter(b => {
    const tag = Utilities.formatDate(b.due, Session.getScriptTimeZone(), 'yyyy-MM-dd');
    if (sh.getRange(b.row, 6).getValue() === tag) return false;   // already reminded
    sh.getRange(b.row, 6).setValue(tag);
    return true;
  });
  if (!fresh.length) return;
  const lines = fresh.map(b => `• ${b.name}: ${money_(b.amount)}, due ${b.days === 0 ? 'TODAY' : b.days === 1 ? 'tomorrow' : 'in ' + b.days + ' days'} (${Utilities.formatDate(b.due, Session.getScriptTimeZone(), 'EEE MMM d')})`);
  notify_('🔔 Bill reminder', lines.join('\n'));
}

// ===== GROCERIES: list, staples, meal planner, price tracker =====
const groceries_ = () => sheet_('Groceries', ['Item', 'Section', 'Added', 'Done']);
const staples_ = () => sheet_('Staples', ['Item', 'Section']);
const meals_ = () => sheet_('Meals', ['Meal', 'Ingredients (comma separated)']);
const prices_ = () => sheet_('Prices', ['Date', 'Item', 'Store', 'Price', 'Size / note']);

const SECTIONS = [
  ['🥬 Produce', 'apple|banana|orange|grape|berr|strawberr|lettuce|spinach|salad|tomato|potato|onion|garlic|pepper|carrot|celery|cucumber|broccoli|avocado|lemon|lime|fruit|veg|mushroom|corn|eggplant|zucchini'],
  ['🥩 Meat & Seafood', 'chicken|beef|steak|pork|bacon|sausage|ham|turkey|ground beef|ground turkey|fish|salmon|tuna|shrimp|hot dog|hotdog'],
  ['🥛 Dairy & Eggs', 'milk|egg|cheese|butter|yogurt|cream|sour cream|creamer'],
  ['🍞 Bakery & Bread', 'bread|bun|bagel|tortilla|roll|muffin'],
  ['🧊 Frozen', 'frozen|ice cream|pizza|waffle|fries'],
  ['🥫 Pantry', 'rice|pasta|spaghetti|noodle|sauce|soup|can|bean|flour|sugar|oil|cereal|oat|peanut|jelly|salt|spice|coffee|tea|taco seasoning|seasoning|broth|ketchup|mustard|mayo'],
  ['🍿 Snacks & Drinks', 'chip|cracker|cookie|candy|soda|pop|juice|water|snack|popcorn'],
  ['🧻 Household & Pets', 'paper towel|toilet paper|tissue|soap|detergent|trash bag|dish|foil|wrap|cleaner|shampoo|toothpaste|dog food|cat food|litter|pet'],
];
function sectionOf_(item) {
  const name = String(item).toLowerCase();
  const words = name.split(/[^a-z]+/).filter(Boolean);
  let best = null, bestLen = 0;   // longest keyword wins ("dog food" beats "hot dog")
  for (const [section, list] of SECTIONS) {
    for (const k of list.split('|')) {
      const hit = k.includes(' ') ? name.includes(k) : words.some(w => w.startsWith(k));
      if (hit && k.length > bestLen) { best = section; bestLen = k.length; }
    }
  }
  return best || '🛒 Other';
}
const splitItems_ = text => String(text || '').split(/[,\n]+/).map(x => x.trim()).filter(Boolean);
const cap_ = x => x.charAt(0).toUpperCase() + x.slice(1);

function openList_() {
  return rows_(groceries_()).map((r, i) => ({ row: i + 2, item: r[0], section: r[1], done: r[3] === true || r[3] === 'TRUE' }))
    .filter(x => x.item && !x.done);
}
function addGroceries_(items) {
  const have = new Set(openList_().map(x => String(x.item).toLowerCase()));
  const sh = groceries_(), added = [];
  items.forEach(raw => {
    const item = cap_(raw);
    if (have.has(item.toLowerCase())) return;
    sh.appendRow([item, sectionOf_(item), new Date(), false]);
    have.add(item.toLowerCase());
    added.push(item);
  });
  if (added.length) sh.getRange(2, 4, sh.getLastRow() - 1, 1).insertCheckboxes();
  return added;
}
function listText_() {
  const items = openList_();
  if (!items.length) return 'Your grocery list is empty. 🎉';
  const groups = {};
  items.forEach(x => (groups[x.section] = groups[x.section] || []).push(x.item));
  const order = SECTIONS.map(s => s[0]).concat('🛒 Other');
  let t = `🛒 Grocery list (${items.length} items)\n`;
  order.filter(sec => groups[sec]).forEach(sec => {
    t += `\n${sec}\n` + groups[sec].map(i => `☐ ${i}`).join('\n') + '\n';
  });
  const now = new Date();
  const g = monthSummary_(now.getFullYear(), now.getMonth()).cats.find(c => c.category === 'Groceries');
  if (g) t += `\n💸 Groceries this month: ${money_(g.spent)}` + (g.budget ? ` of ${money_(g.budget)} (${money_(g.budget - g.spent)} left)` : '') + '\n';
  return t;
}
function markDone_(items) {
  const open = openList_(), sh = groceries_(), done = [];
  const all = items.length === 1 && items[0].toLowerCase() === 'all';
  open.forEach(x => {
    if (all || items.some(i => String(x.item).toLowerCase().includes(i.toLowerCase()))) {
      sh.getRange(x.row, 4).setValue(true);
      done.push(x.item);
      if (PANTRY_AUTO_FROM_GROCERY) pantryAdd_(x.item);
    }
  });
  return done;
}
function clearDone_() {
  const sh = groceries_(), rows = rows_(sh);
  let removed = 0;
  for (let i = rows.length - 1; i >= 0; i--) {
    if (rows[i][3] === true || rows[i][3] === 'TRUE') { sh.deleteRow(i + 2); removed++; }
  }
  return removed;
}
function saveMeal_(name, ingredients) {
  const sh = meals_(), rows = rows_(sh);
  const i = rows.findIndex(r => String(r[0]).toLowerCase() === name.toLowerCase());
  const list = splitItems_(ingredients).map(cap_).join(', ');
  if (i >= 0) sh.getRange(i + 2, 2).setValue(list); else sh.appendRow([cap_(name), list]);
  return list;
}
function planMeals_(names) {
  const meals = rows_(meals_()), found = [], missing = [], items = [];
  names.forEach(n => {
    const m = meals.find(r => String(r[0]).toLowerCase() === n.toLowerCase());
    if (!m) { missing.push(n); return; }
    found.push(m[0]);
    items.push(...splitItems_(m[1]));
  });
  const have = items.filter(haveInPantry_), need = items.filter(i => !haveInPantry_(i));
  return { found, missing, have: [...new Set(have.map(cap_))], added: addGroceries_(need) };
}
function logPrice_(item, store, price, size) {
  prices_().appendRow([new Date(), cap_(item), cap_(store), Number(price), size || '']);
}
function comparePrices_(item) {
  const q = String(item).toLowerCase(), latest = {};
  rows_(prices_()).forEach(([date, it, store, price, size]) => {
    if (!String(it).toLowerCase().includes(q)) return;
    const prev = latest[store];
    if (!prev || new Date(date) > new Date(prev.date)) latest[store] = { store, price: Number(price), size, date, item: it };
  });
  return Object.values(latest).sort((a, b) => a.price - b.price);
}

function groceryAction_(body) {
  const a = body.action;
  if (a === 'g_add') return { added: addGroceries_(splitItems_(body.items)), text: listText_() };
  if (a === 'g_list') return { text: listText_() };
  if (a === 'g_done') return { done: markDone_(splitItems_(body.items)), text: listText_() };
  if (a === 'g_clear') return { removed: clearDone_() };
  if (a === 'g_staples') return { added: addGroceries_(rows_(staples_()).map(r => String(r[0])).filter(Boolean)), text: listText_() };
  if (a === 'g_savestaple') {
    const sh = staples_(), have = new Set(rows_(sh).map(r => String(r[0]).toLowerCase())), saved = [];
    splitItems_(body.items).map(cap_).forEach(i => { if (!have.has(i.toLowerCase())) { sh.appendRow([i, sectionOf_(i)]); saved.push(i); } });
    return { saved };
  }
  if (a === 'g_savemeal') return { meal: cap_(body.name), ingredients: saveMeal_(body.name, body.ingredients) };
  if (a === 'g_plan') return { ...planMeals_(splitItems_(body.meals)), text: listText_() };
  if (a === 'g_meals') return { meals: rows_(meals_()).filter(r => r[0]).map(r => ({ name: r[0], ingredients: r[1] })) };
  if (a === 'g_price') {
    const price = Number(String(body.price).replace(/[$,]/g, ''));
    if (!(price > 0)) throw new Error('price must be a number like 3.49');
    logPrice_(body.item, body.store, price, body.size);
    return { compare: comparePrices_(body.item) };
  }
  if (a === 'g_compare') return { compare: comparePrices_(body.item) };
  return null;
}

// ===== PANTRY INVENTORY =====
const pantry_ = () => sheet_('Pantry', ['Item', 'Section', 'Qty', 'Unit', 'Low at', 'Expires', 'Updated']);
const PANTRY_AUTO_FROM_GROCERY = true;   // checking an item off the grocery list adds it to the pantry
const UNITS = 'can cans lb lbs pound pounds oz box boxes bag bags bottle bottles jar jars pack packs gallon gallons dozen loaf loaves bunch bunches pkg carton cartons roll rolls'.split(' ');

// "2 cans black beans" -> {qty: 2, unit: 'cans', item: 'Black beans'}
function parseQtyItem_(text) {
  const words = String(text).trim().split(/\s+/);
  let qty = 1, unit = '';
  if (words.length > 1 && /^\d+(\.\d+)?$/.test(words[0])) qty = Number(words.shift());
  else if (words.length > 1 && /^(a|an|one)$/i.test(words[0])) words.shift();
  if (words.length > 1 && UNITS.includes(words[0].toLowerCase())) unit = words.shift().toLowerCase();
  return { qty, unit, item: cap_(words.join(' ')) };
}
function pantryRows_() {
  return rows_(pantry_()).map((r, i) => ({ row: i + 2, item: String(r[0]), section: r[1], qty: Number(r[2]) || 0,
    unit: r[3], low: r[4] === '' ? 0 : Number(r[4]) || 0, expires: r[5] ? new Date(r[5]) : null })).filter(x => x.item);
}
// Find a pantry item: exact name first, then a close match ("beans" finds "Black beans")
function findPantry_(name, fuzzy) {
  const list = pantryRows_(), norm = x => String(x).trim().toLowerCase().replace(/(es|s)$/, '');
  const q = norm(name);
  return list.find(x => norm(x.item) === q) ||                       // "eggs" = "egg"
         (fuzzy && list.find(x => q.length >= 4 && norm(x.item).endsWith(' ' + q))) ||   // "beans" finds "black beans"
         null;
}
function pantryAdd_(text, expires) {
  const { qty, unit, item } = parseQtyItem_(text);
  const sh = pantry_(), found = findPantry_(item);
  if (found) {
    sh.getRange(found.row, 3).setValue(found.qty + qty);
    if (unit && !found.unit) sh.getRange(found.row, 4).setValue(unit);
    if (expires) sh.getRange(found.row, 6).setValue(expires);
    sh.getRange(found.row, 7).setValue(new Date());
    return `${found.item}: ${found.qty + qty}${found.unit || unit ? ' ' + (found.unit || unit) : ''}`;
  }
  sh.appendRow([item, sectionOf_(item), qty, unit, '', expires || '', new Date()]);
  return `${item}: ${qty}${unit ? ' ' + unit : ''} (new)`;
}
// Use some up; if it drops to its "low at" level, it goes on the grocery list
function pantryUse_(text) {
  const { qty, item } = parseQtyItem_(text);
  const found = findPantry_(item, true);
  if (!found) return { line: `❓ ${item} isn't in the pantry` };
  const left = Math.max(0, found.qty - qty);
  pantry_().getRange(found.row, 3, 1, 1).setValue(left);
  pantry_().getRange(found.row, 7).setValue(new Date());
  let line = `${found.item}: ${left}${found.unit ? ' ' + found.unit : ''} left`;
  let listed = false;
  if (left <= found.low) {
    listed = addGroceries_([found.item]).length > 0;
    line += left === 0 ? ' · all gone' : ' · running low';
    if (listed) line += ' → added to grocery list 🛒';
  }
  return { line, listed };
}
function pantryText_(filter) {
  let items = pantryRows_();
  const soon = x => x.expires && daysUntil_(x.expires) <= 3;
  if (filter === 'low') items = items.filter(x => x.qty <= x.low);
  if (filter === 'expiring') items = items.filter(x => x.expires && daysUntil_(x.expires) <= 7);
  if (!items.length) return filter === 'low' ? 'Nothing is running low. 👍' : filter === 'expiring' ? 'Nothing expires this week. 👍' : 'The pantry is empty. Add things with /pantry add';
  const groups = {};
  items.forEach(x => (groups[x.section] = groups[x.section] || []).push(x));
  const order = SECTIONS.map(s => s[0]).concat('🛒 Other');
  const tz = Session.getScriptTimeZone();
  let t = filter === 'low' ? '⚠️ Running low\n' : filter === 'expiring' ? '⏰ Expiring this week\n' : `🥫 Pantry (${items.length} items)\n`;
  order.filter(sec => groups[sec]).forEach(sec => {
    t += `\n${sec}\n` + groups[sec].sort((a, b) => a.item.localeCompare(b.item)).map(x => {
      let line = `• ${x.item}: ${x.qty}${x.unit ? ' ' + x.unit : ''}`;
      if (x.qty <= x.low) line += x.qty === 0 ? ' ❌ out' : ' ⚠️ low';
      if (x.expires) {
        const d = daysUntil_(x.expires);
        line += d < 0 ? ' 🚫 expired' : soon(x) ? ` ⏰ expires ${d === 0 ? 'today' : d === 1 ? 'tomorrow' : 'in ' + d + ' days'}` : ` (exp ${Utilities.formatDate(x.expires, tz, 'MMM d')})`;
      }
      return line;
    }).join('\n') + '\n';
  });
  return t;
}
const haveInPantry_ = name => { const f = findPantry_(name); return f && f.qty > 0; };

function pantryAction_(body) {
  const a = body.action;
  if (a === 'pn_add') {
    const exp = body.expires ? parseWhen_(body.expires, '12:00 PM') : '';
    const lines = splitItems_(body.items).map(i => pantryAdd_(i, exp));
    return { text: '✅ ' + lines.join('\n✅ ') + '\n\n' + pantryText_() };
  }
  if (a === 'pn_use') {
    const results = splitItems_(body.items).map(pantryUse_);
    return { text: results.map(r => r.line).join('\n') };
  }
  if (a === 'pn_list') return { text: pantryText_(body.filter || '') };
  if (a === 'pn_set') {
    const found = findPantry_(body.item, true);
    if (!found) throw new Error(`${body.item} isn't in the pantry yet. Add it with /pantry add`);
    const sh = pantry_(), changes = [];
    if (body.qty !== '' && body.qty != null) { sh.getRange(found.row, 3).setValue(Number(body.qty) || 0); changes.push(`qty ${Number(body.qty) || 0}`); }
    if (body.low !== '' && body.low != null) { sh.getRange(found.row, 5).setValue(Number(body.low) || 0); changes.push(`goes on the list at ${Number(body.low) || 0}`); }
    if (body.expires) { sh.getRange(found.row, 6).setValue(parseWhen_(body.expires, '12:00 PM')); changes.push(`expires ${body.expires}`); }
    sh.getRange(found.row, 7).setValue(new Date());
    return { text: `✏️ ${found.item}: ${changes.join(', ') || 'no changes'}` };
  }
  if (a === 'pn_remove') {
    const found = findPantry_(body.item, true);
    if (!found) throw new Error(`${body.item} isn't in the pantry`);
    pantry_().deleteRow(found.row);
    return { text: `🗑️ Removed ${found.item} from the pantry` };
  }
  if (a === 'pn_restock') {   // put everything that's low on the grocery list
    const low = pantryRows_().filter(x => x.qty <= x.low).map(x => x.item);
    const added = addGroceries_(low);
    return { text: added.length ? `🛒 Added to the grocery list: ${added.join(', ')}` : 'Nothing low that isn\'t already on the list. 👍' };
  }
  return null;
}

// Daily: warn about food expiring soon (called from dailyCheck)
function pantryReminders_() {
  const soon = pantryRows_().filter(x => x.qty > 0 && x.expires && daysUntil_(x.expires) >= 0 && daysUntil_(x.expires) <= 2);
  if (!soon.length) return;
  notify_('⏰ Use it before it goes bad', soon.map(x => {
    const d = daysUntil_(x.expires);
    return `• ${x.item} (${x.qty}${x.unit ? ' ' + x.unit : ''}) expires ${d === 0 ? 'TODAY' : d === 1 ? 'tomorrow' : 'in 2 days'}`;
  }).join('\n'));
}

// ===== CAREER ROADMAP: Security+ and freelancing, side by side =====
const career_ = () => sheet_('Career', ['ID', 'Phase', 'Track', 'Step', 'Milestone', 'Done']);
const gigs_ = () => sheet_('Freelance Gigs', ['Date', 'Gig', 'Client', 'Earned']);
const CAREER_PLAN = [
  ['s1','Foundation (Nov–Jan)','Security','Ask PXT at DET3: does Career Choice cover Security+?',''],
  ['s2','Foundation (Nov–Jan)','Security','Enroll in Career Choice, or pick a Security+ course',''],
  ['s3','Foundation (Nov–Jan)','Security','Lock in a weekly study rhythm (about 4 hrs)',''],
  ['s4','Foundation (Nov–Jan)','Security','Security+ domain 1: General security concepts',''],
  ['s5','Foundation (Nov–Jan)','Security','Security+ domain 2: Threats, vulnerabilities and mitigations',''],
  ['f1','Foundation (Nov–Jan)','Freelance','Create a GitHub account',''],
  ['f2','Foundation (Nov–Jan)','Freelance','Publish StoltBot on GitHub (no keys) with a README',''],
  ['f3','Foundation (Nov–Jan)','Freelance','Create a Fiverr profile',''],
  ['f4','Foundation (Nov–Jan)','Freelance','List 3 gigs: Discord bot, WooCommerce automation, Sheets budget',''],
  ['f5','Foundation (Nov–Jan)','Freelance','Land your first client','★'],
  ['s6','Momentum (Feb–Apr)','Security','Security+ domain 3: Security architecture',''],
  ['s7','Momentum (Feb–Apr)','Security','Security+ domain 4: Security operations',''],
  ['s8','Momentum (Feb–Apr)','Security','Security+ domain 5: Program management and oversight',''],
  ['s9','Momentum (Feb–Apr)','Security','Score 80%+ on two full practice exams',''],
  ['s10','Momentum (Feb–Apr)','Security','Schedule the Security+ exam','★'],
  ['f6','Momentum (Feb–Apr)','Freelance','Complete 3 gigs',''],
  ['f7','Momentum (Feb–Apr)','Freelance','Collect 3 five-star reviews',''],
  ['f8','Momentum (Feb–Apr)','Freelance','Raise gig prices after the reviews',''],
  ['f9','Momentum (Feb–Apr)','Freelance','Add "secure setup" to gig descriptions',''],
  ['s11','Credentials & income (May–Oct)','Security','Pass Security+','★'],
  ['s12','Credentials & income (May–Oct)','Security','Finish a TryHackMe beginner path',''],
  ['s13','Credentials & income (May–Oct)','Security','Update resume (Security+, freelance, logistics, accounting)',''],
  ['s14','Credentials & income (May–Oct)','Security','Apply to 5 IT roles (help desk, IT support, SOC)',''],
  ['f10','Credentials & income (May–Oct)','Freelance','Complete 10 gigs total',''],
  ['f11','Credentials & income (May–Oct)','Freelance','Reach $300 a month in side income','★'],
  ['f12','Credentials & income (May–Oct)','Freelance','Land a repeat client',''],
  ['f13','Credentials & income (May–Oct)','Freelance','Open an Upwork profile',''],
];
function careerRows_() {
  return rows_(career_()).filter(r => r[0]).map((r, i) => ({ row: i + 2, id: r[0], phase: r[1], track: r[2], step: r[3], star: r[4], done: r[5] }));
}
function findStep_(q) {
  const list = careerRows_(), t = String(q).trim().toLowerCase();
  return list.find(x => x.id.toLowerCase() === t) || list.find(x => x.step.toLowerCase() === t) || list.find(x => x.step.toLowerCase().includes(t));
}
function careerText_() {
  const list = careerRows_(), tz = Session.getScriptTimeZone();
  const bar = (n, t) => { const k = Math.round(n / t * 10); return '█'.repeat(k) + '░'.repeat(10 - k); };
  let out = '';
  ['Security', 'Freelance'].forEach(track => {
    const items = list.filter(x => x.track === track), n = items.filter(x => x.done).length, next = items.find(x => !x.done);
    out += `${track === 'Security' ? '🔐 IT Security' : '💼 Freelancing'}  ${bar(n, items.length)} ${n}/${items.length}\n` +
           (next ? `   ➡️ Next: ${next.step}${next.star ? ' ★' : ''}\n` : '   🎉 Track complete!\n');
  });
  const gigs = rows_(gigs_()).filter(r => r[0]);
  const earned = gigs.reduce((x, r) => x + (Number(r[3]) || 0), 0);
  const now = new Date();
  const month = gigs.filter(r => { const d = new Date(r[0]); return d.getMonth() === now.getMonth() && d.getFullYear() === now.getFullYear(); })
    .reduce((x, r) => x + (Number(r[3]) || 0), 0);
  out += `💵 Gigs: ${gigs.length} · Earned ${money_(earned)} total · ${money_(month)} this month\n`;
  const phases = [...new Set(list.map(x => x.phase))];
  out += '\n' + phases.map(ph => {
    const items = list.filter(x => x.phase === ph);
    return `**${ph}** (${items.filter(x => x.done).length}/${items.length})\n` + items.map(x =>
      `${x.done ? '✅' : '⬜'} ${x.track === 'Security' ? '🔐' : '💼'} ${x.step}${x.star ? ' ★' : ''}${x.done ? ' · ' + Utilities.formatDate(new Date(x.done), tz, 'MMM d') : ''}`).join('\n');
  }).join('\n\n');
  return out;
}
function careerAction_(body) {
  const a = body.action;
  if (a === 'c_status') return { text: careerText_() };
  if (a === 'c_open') return { steps: careerRows_().filter(x => body.undo ? x.done : !x.done).map(x => ({ id: x.id, step: x.step, track: x.track })) };
  if (a === 'c_done' || a === 'c_undo') {
    const x = findStep_(body.step);
    if (!x) throw new Error(`no step matches "${body.step}"`);
    career_().getRange(x.row, 6).setValue(a === 'c_done' ? new Date() : '');
    const head = a === 'c_done' ? (x.star ? `🏆 MILESTONE: ${x.step}! Huge.` : `✅ Done: ${x.step}`) : `↩️ Unchecked: ${x.step}`;
    return { text: head + '\n\n' + careerText_().split('\n\n')[0] };
  }
  if (a === 'c_gig') {
    const amt = Number(String(body.amount || 0).replace(/[$,]/g, '')) || 0;
    gigs_().appendRow([new Date(), cap_(body.gig || 'Gig'), body.client || '', amt]);
    return { text: `💼 Logged gig: ${cap_(body.gig || 'Gig')}${amt ? ' · ' + money_(amt) : ''}\n\n` + careerText_().split('\n\n')[0] };
  }
  return null;
}
// Mondays: career check-in (called from dailyCheck)
function careerReminder_() {
  if (new Date().getDay() !== 1 || !careerRows_().length) return;
  notify_('🧭 Weekly career check-in', careerText_().split('\n\n')[0] + '\nThis week: ~4 hrs Security+, ~3 hrs freelance. /career done when you finish a step.');
}

// ===== VACATION PLANNER: savings goal, countdown, itinerary, packing, trip spending, price quotes =====
const trips_ = () => sheet_('Trips', ['Trip', 'Goal', 'Trip date', 'Created']);
const tripSave_ = () => sheet_('Trip Savings', ['Date', 'Trip', 'Amount', 'Note']);
const tripPlan_ = () => sheet_('Itinerary', ['Trip', 'Day', 'Plan']);
const packing_ = () => sheet_('Packing', ['Trip', 'Item', 'Packed']);
const packAlways_ = () => sheet_('Always Pack', ['Item']);
const tripSpend_ = () => sheet_('Trip Spending', ['Date', 'Trip', 'Amount', 'What']);
const tripQuotes_ = () => sheet_('Trip Quotes', ['Date', 'Trip', 'Type', 'Where', 'Price', 'Note']);
const flights_ = () => sheet_('Flights', ['Trip', 'Leg', 'Airline', 'Flight #', 'From', 'To', 'Departs', 'Arrives', 'Note', 'Duration']);

const bookings_ = () => sheet_('Bookings', ['Trip', 'Type', 'Name', 'Starts', 'Ends', 'Cost', 'Address', 'Confirmation #', 'Note']);
const BOOK_ICON = { Hotel: '🏨', Car: '🚗', Show: '🎭', Restaurant: '🍽️', Tour: '🗺️', Activity: '🎢', Other: '📌' };

function tripBookings_(t) {
  return rows_(bookings_()).filter(r => sameName_(r[0], t.name) && r[2])
    .map(r => ({ type: r[1] || 'Other', name: r[2], start: r[3] ? new Date(r[3]) : null, end: r[4] ? new Date(r[4]) : null,
                 cost: Number(r[5]) || 0, address: r[6], conf: r[7], note: r[8] }))
    .sort((a, b) => (a.start ? a.start.getTime() : 9e15) - (b.start ? b.start.getTime() : 9e15));
}
function bookingLine_(b) {
  const icon = BOOK_ICON[b.type] || '📌';
  let line = `${icon} ${b.type}: ${b.name}`;
  if (b.type === 'Hotel' && b.start && b.end) {
    const nights = Math.round((dayStart_(b.end) - dayStart_(b.start)) / 86400000);
    line += `\n   🛎️ Check-in ${fmtWhen_(b.start)}\n   🧳 Check-out ${fmtWhen_(b.end)}\n   🌙 ${nights} night${nights === 1 ? '' : 's'}` +
            (b.cost && nights > 0 ? ` · ${money_(b.cost)} total (${money_(b.cost / nights)}/night)` : b.cost ? ` · ${money_(b.cost)}` : '');
  } else {
    if (b.start) line += `\n   🕐 ${fmtWhen_(b.start)}` + (b.end ? ` → ${dayStart_(b.end).getTime() === dayStart_(b.start).getTime() ? fmtTime_(b.end) : fmtWhen_(b.end)}` : '');
    if (b.cost) line += `\n   💵 ${money_(b.cost)}`;
  }
  if (b.address) line += `\n   📍 ${b.address}`;
  if (b.conf) line += `\n   🔖 Confirmation: ${b.conf}`;
  if (b.note) line += `\n   📝 ${b.note}`;
  return line;
}
function bookingsText_(t) {
  const list = tripBookings_(t);
  if (!list.length) return `No bookings for ${t.name} yet. Use /trip hotel or /trip book`;
  const total = list.reduce((x, b) => x + b.cost, 0);
  return `📌 ${t.name} bookings\n\n` + list.map(bookingLine_).join('\n\n') + (total ? `\n\n💳 Booked so far: ${money_(total)}` : '');
}

// Time zones for common US airports, used to work out flight length automatically
const AIRPORT_TZ = {
  'America/New_York': 'DTW FNT LAN GRR TVC MBS AZO ATL BOS JFK LGA EWR PHL DCA IAD BWI CLT MCO TPA MIA FLL RSW JAX PIT CLE CMH CVG IND BUF RDU BNA SDF RIC ORF SAV CHS MYR PBI SRQ PIE',
  'America/Chicago': 'ORD MDW MSP STL MCI DFW DAL IAH HOU AUS SAT MSY MEM MKE OMA DSM OKC TUL BHM LIT MSN',
  'America/Denver': 'DEN SLC ABQ ELP COS BOI BZN',
  'America/Phoenix': 'PHX TUS',
  'America/Los_Angeles': 'LAS LAX SFO SAN SJC OAK SMF SEA PDX BUR ONT SNA LGB PSP RNO GEG',
  'America/Anchorage': 'ANC FAI',
  'Pacific/Honolulu': 'HNL OGG KOA LIH',
};
function airportTz_(code) {
  const c = String(code || '').trim().toUpperCase();
  for (const [tz, list] of Object.entries(AIRPORT_TZ)) if (c && list.split(' ').includes(c)) return tz;
  return null;
}
function offsetMin_(date, tz) {            // e.g. -300 for Eastern Standard Time
  const z = Utilities.formatDate(date, tz, 'Z');
  return (z[0] === '-' ? -1 : 1) * (Number(z.slice(1, 3)) * 60 + Number(z.slice(3, 5)));
}
// A wall-clock time (as typed) at an airport -> real moment in time (ms)
function wallToUtc_(d, tz) {
  const p = Utilities.formatDate(d, Session.getScriptTimeZone(), 'yyyy-M-d-H-m').split('-').map(Number);
  const wall = Date.UTC(p[0], p[1] - 1, p[2], p[3], p[4]);
  let utc = wall - offsetMin_(new Date(wall), tz) * 60000;
  utc = wall - offsetMin_(new Date(utc), tz) * 60000;     // second pass handles daylight-saving edges
  return utc;
}
function parseDuration_(text) {             // "4h 10m", "4:10", "250m", "4.5h"
  const t = String(text || '').toLowerCase().replace(/\s+/g, '');
  if (!t) return null;
  let m = t.match(/^(\d+):(\d{2})$/); if (m) return Number(m[1]) * 60 + Number(m[2]);
  m = t.match(/^(?:(\d+(?:\.\d+)?)h(?:rs?|ours?)?)?(?:(\d+)m(?:in(?:s|utes?)?)?)?$/);
  if (m && (m[1] || m[2])) return Math.round(Number(m[1] || 0) * 60) + Number(m[2] || 0);
  throw new Error('flight length should look like 4h 10m or 4:10');
}
const fmtDur_ = mins => mins < 60 ? `${mins}m` : `${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, '0')}m`;

// "2027-06-12" + "6:05 AM" -> Date (times are local to each airport)
function parseWhen_(date, time) {
  const d = String(date || '').match(/(\d{4})-(\d{1,2})-(\d{1,2})/);
  if (!d) throw new Error('date should look like 2027-06-12');
  const t = String(time || '').trim().match(/^(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)?$/i);
  if (!t) throw new Error('time should look like 6:05 AM or 18:05');
  let h = Number(t[1]); const m = Number(t[2] || 0), ap = (t[3] || '').toLowerCase();
  if (ap.startsWith('p') && h < 12) h += 12;
  if (ap.startsWith('a') && h === 12) h = 0;
  if (h > 23 || m > 59) throw new Error('that time doesn\'t look right');
  return new Date(Number(d[1]), Number(d[2]) - 1, Number(d[3]), h, m);
}
const fmtWhen_ = d => Utilities.formatDate(new Date(d), Session.getScriptTimeZone(), 'EEE MMM d, h:mm a');
const fmtTime_ = d => Utilities.formatDate(new Date(d), Session.getScriptTimeZone(), 'h:mm a');

function tripFlights_(t) {
  return rows_(flights_()).filter(r => sameName_(r[0], t.name) && r[6])
    .map(r => ({ leg: r[1], airline: r[2], num: r[3], from: r[4], to: r[5], dep: new Date(r[6]), arr: r[7] ? new Date(r[7]) : null, note: r[8], dur: r[9] }))
    .sort((a, b) => a.dep - b.dep);
}
function flightLine_(f) {
  const sameDay = f.arr && dayStart_(f.arr).getTime() === dayStart_(f.dep).getTime();
  return `✈️ ${f.leg}: ${[f.airline, f.num].filter(String).join(' ')}` +
    `\n   🛫 ${f.from ? f.from + ' ' : ''}${fmtWhen_(f.dep)}` +
    (f.arr ? `\n   🛬 ${f.to ? f.to + ' ' : ''}${sameDay ? fmtTime_(f.arr) : fmtWhen_(f.arr)}` : '') +
    (f.dur ? `\n   ⏱️ ${f.dur} in the air` : '') +
    (f.note ? `\n   📝 ${f.note}` : '');
}
// Flights in order, with layovers worked out between connecting flights
function flightsBlock_(fl) {
  let out = '', journeyStart = 0;
  const totalLine = (a, b) => {
    const first = fl[a], last = fl[b];
    const fromTz = airportTz_(first.from), toTz = airportTz_(last.to);
    if (a === b || !last.arr || !fromTz || !toTz) return '';
    const mins = Math.round((wallToUtc_(last.arr, toTz) - wallToUtc_(first.dep, fromTz)) / 60000);
    return mins > 0 ? `\n   🧭 Total travel time ${first.from} → ${last.to}: ${fmtDur_(mins)}` : '';
  };
  fl.forEach((f, i) => {
    out += (i ? '\n' : '') + flightLine_(f);
    const next = fl[i + 1];
    // Same airport, and the gap is under 24 hours: that's a layover (both times are local to that airport)
    const gap = next && f.arr && f.to && next.from && String(f.to) === String(next.from)
      ? Math.round((next.dep - f.arr) / 60000) : null;
    if (gap != null && gap >= 0 && gap < 24 * 60) {
      const flag = gap < 45 ? ' ⚠️ tight connection, move fast!' : gap > 240 ? ' (long layover: grab a meal)' : '';
      out += `\n   🕐 Layover in ${f.to}: ${fmtDur_(gap)}${flag}`;
    } else {
      out += totalLine(journeyStart, i);
      journeyStart = i + 1;
    }
  });
  return out;
}
function flightsText_(t) {
  const fl = tripFlights_(t);
  if (!fl.length) return `No flights saved for ${t.name} yet. Use /trip flight`;
  return `✈️ ${t.name} flights\n\n` + flightsBlock_(fl) + '\n\nTimes are local to each airport.';
}

const sameName_ = (a, b) => String(a).trim().toLowerCase() === String(b).trim().toLowerCase();
const dayStart_ = d => { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; };
const daysUntil_ = date => Math.round((dayStart_(date) - dayStart_(new Date())) / 86400000);

function allTrips_() {
  return rows_(trips_()).filter(r => r[0]).map(r => ({ name: String(r[0]), goal: Number(r[1]) || 0, date: r[2] ? new Date(r[2]) : null }));
}
// Named trip, or the next upcoming one
function pickTrip_(name) {
  const trips = allTrips_();
  if (name) {
    const t = trips.find(x => sameName_(x.name, name));
    if (!t) throw new Error(`No trip called "${name}". Your trips: ${trips.map(x => x.name).join(', ') || 'none yet (use /trip new)'}`);
    return t;
  }
  const upcoming = trips.filter(t => !t.date || daysUntil_(t.date) >= -14)
    .sort((a, b) => (a.date ? a.date.getTime() : 9e15) - (b.date ? b.date.getTime() : 9e15));
  if (!upcoming.length) throw new Error('No trips yet. Start one with /trip new');
  return upcoming[0];
}
const sumFor_ = (sh, trip, col) => rows_(sh).filter(r => sameName_(r[1], trip)).reduce((x, r) => x + (Number(r[col]) || 0), 0);

function tripStatus_(t) {
  const saved = sumFor_(tripSave_(), t.name, 2);
  const spent = sumFor_(tripSpend_(), t.name, 2);
  const tz = Session.getScriptTimeZone();
  let out = `🌴 ${t.name}\n`;
  if (t.date) {
    const d = daysUntil_(t.date);
    out += d > 0 ? `📅 ${Utilities.formatDate(t.date, tz, 'EEE MMM d, yyyy')}: ${d} day${d === 1 ? '' : 's'} to go\n`
         : d === 0 ? `📅 It's TODAY! Have fun! 🎉\n` : `📅 Trip started ${-d} day(s) ago\n`;
  }
  if (t.goal) {
    const pct = Math.min(100, Math.round(saved / t.goal * 100));
    const bar = '█'.repeat(Math.round(pct / 10)) + '░'.repeat(10 - Math.round(pct / 10));
    out += `💰 Saved ${money_(saved)} of ${money_(t.goal)}  ${bar} ${pct}%\n`;
    const left = t.goal - saved;
    if (left > 0 && t.date && daysUntil_(t.date) > 0) {
      const weeks = Math.max(1, daysUntil_(t.date) / 7);
      out += `➡️ Save ${money_(left / weeks)}/week to reach the goal (${money_(left)} to go)\n`;
    } else if (left <= 0) out += `✅ Goal reached!\n`;
  } else if (saved) out += `💰 Saved ${money_(saved)}\n`;
  if (spent) out += `🧾 Spent on the trip: ${money_(spent)}` + (t.goal ? ` · ${money_(Math.max(0, saved) - spent)} left of what you saved` : '') + '\n';
  const bk = tripBookings_(t);
  bk.filter(b => b.type === 'Hotel').forEach(h => {
    const nights = h.start && h.end ? Math.round((dayStart_(h.end) - dayStart_(h.start)) / 86400000) : 0;
    out += `🏨 ${h.name}` + (nights ? ` · ${nights} night${nights === 1 ? '' : 's'}` : '') + '\n';
  });
  const bookedTotal = bk.reduce((x, b) => x + b.cost, 0);
  if (bookedTotal) out += `💳 Booked so far: ${money_(bookedTotal)}` + (t.goal ? ` (${Math.round(bookedTotal / t.goal * 100)}% of your goal)` : '') + '\n';
  const next = tripFlights_(t).find(f => f.dep > new Date());
  if (next) out += `✈️ Next flight: ${next.leg}, ${[next.airline, next.num].filter(String).join(' ')} departs ${fmtWhen_(next.dep)}\n`;
  const packing = rows_(packing_()).filter(r => sameName_(r[0], t.name));
  if (packing.length) out += `🧳 Packed ${packing.filter(r => r[2] === true).length} of ${packing.length} items\n`;
  return out;
}

function itineraryText_(t) {
  const plans = rows_(tripPlan_()).filter(r => sameName_(r[0], t.name))
    .sort((a, b) => (parseInt(String(a[1]).replace(/\D/g, '')) || 99) - (parseInt(String(b[1]).replace(/\D/g, '')) || 99));
  const fl = tripFlights_(t);
  const bkList = tripBookings_(t);
  const flightPart = (fl.length ? '\n\n' + flightsBlock_(fl) : '') + (bkList.length ? '\n\n' + bkList.map(bookingLine_).join('\n') : '');
  if (!plans.length) return flightPart ? `🗓️ ${t.name} itinerary${flightPart}\n\nAdd plans with /trip plan` : `No itinerary for ${t.name} yet. Add one with /trip plan`;
  const byDay = {};
  plans.forEach(([, day, plan]) => (byDay[day] = byDay[day] || []).push(plan));
  return `🗓️ ${t.name} itinerary${flightPart}\n` + Object.entries(byDay).map(([day, items]) => `\n${day}\n` + items.map(i => `• ${i}`).join('\n')).join('\n');
}

function packingText_(t) {
  const items = rows_(packing_()).map((r, i) => ({ row: i + 2, trip: r[0], item: r[1], packed: r[2] === true }))
    .filter(x => sameName_(x.trip, t.name) && x.item);
  if (!items.length) return `Packing list for ${t.name} is empty. Try /pack always or /pack add`;
  const left = items.filter(x => !x.packed), done = items.filter(x => x.packed);
  return `🧳 ${t.name} packing (${done.length}/${items.length} packed)\n` +
    (left.length ? '\nStill to pack:\n' + left.map(x => `☐ ${x.item}`).join('\n') : '\nEverything is packed! ✅') +
    (done.length ? '\n\nPacked:\n' + done.map(x => `☑ ${x.item}`).join('\n') : '');
}
function addPacking_(t, items) {
  const sh = packing_();
  const have = new Set(rows_(sh).filter(r => sameName_(r[0], t.name)).map(r => String(r[1]).toLowerCase()));
  const added = [];
  items.map(cap_).forEach(i => { if (!have.has(i.toLowerCase())) { sh.appendRow([t.name, i, false]); have.add(i.toLowerCase()); added.push(i); } });
  if (added.length) sh.getRange(2, 3, sh.getLastRow() - 1, 1).insertCheckboxes();
  return added;
}

function quotesText_(t) {
  const q = rows_(tripQuotes_()).filter(r => sameName_(r[1], t.name));
  if (!q.length) return `No price quotes saved for ${t.name} yet. Use /trip quote`;
  const byType = {};
  q.forEach(([date, , type, where, price, note]) => (byType[type] = byType[type] || []).push({ where, price: Number(price), note }));
  return `🏷️ ${t.name} price quotes\n` + Object.entries(byType).map(([type, list]) => {
    list.sort((a, b) => a.price - b.price);
    return `\n${type}\n` + list.map((x, i) => `${i === 0 ? '🏆' : '•'} ${x.where}: ${money_(x.price)}${x.note ? ' (' + x.note + ')' : ''}`).join('\n');
  }).join('\n');
}

function tripAction_(body) {
  const a = body.action;
  if (a === 't_new') {
    const name = cap_(String(body.name || '').trim());
    if (!name) throw new Error('give the trip a name, like Vegas');
    const goal = Number(String(body.goal || 0).replace(/[$,]/g, '')) || 0;
    const date = body.date ? new Date(body.date + 'T12:00:00') : null;
    if (date && isNaN(date)) throw new Error('date should look like 2027-06-15');
    const sh = trips_(), rows = rows_(sh), i = rows.findIndex(r => sameName_(r[0], name));
    if (i >= 0) sh.getRange(i + 2, 2, 1, 2).setValues([[goal, date || '']]);
    else sh.appendRow([name, goal, date || '', new Date()]);
    return { text: (i >= 0 ? '✏️ Updated\n' : '✨ New trip!\n') + tripStatus_(pickTrip_(name)) };
  }
  if (a === 't_trips') return { trips: allTrips_().map(t => t.name) };
  const t = pickTrip_(body.trip);
  if (a === 't_status') return { text: tripStatus_(t) };
  if (a === 't_reset') {
    // part: savings | itinerary | packing | spending | quotes | everything
    const part = String(body.part || '');
    const targets = {
      savings: [[tripSave_(), 1]], itinerary: [[tripPlan_(), 0]], packing: [[packing_(), 0]],
      spending: [[tripSpend_(), 1]], quotes: [[tripQuotes_(), 1]], flights: [[flights_(), 0]], bookings: [[bookings_(), 0]],
    };
    const list = part === 'everything' ? Object.values(targets).flat() : targets[part];
    if (!list) throw new Error('unknown reset option');
    let removed = 0;
    list.forEach(([sh, col]) => {
      const rows = rows_(sh);
      for (let i = rows.length - 1; i >= 0; i--) {
        if (sameName_(rows[i][col], t.name)) { sh.deleteRow(i + 2); removed++; }
      }
    });
    if (part === 'everything') {
      const sh = trips_(), rows = rows_(sh);
      for (let i = rows.length - 1; i >= 0; i--) if (sameName_(rows[i][0], t.name)) sh.deleteRow(i + 2);
      return { text: `🗑️ Deleted the ${t.name} trip and ${removed} related row(s).` };
    }
    return { text: `🧹 Cleared ${part} for ${t.name} (${removed} row(s) removed).\n\n` + tripStatus_(t) };
  }
  if (a === 't_save') {
    const amt = Number(String(body.amount).replace(/[$,]/g, ''));
    if (!amt) throw new Error('amount must be a number like 50');
    tripSave_().appendRow([new Date(), t.name, amt, body.note || '']);
    return { text: (amt > 0 ? `💰 Put ${money_(amt)} toward ${t.name}!\n\n` : `↩️ Took ${money_(-amt)} out of ${t.name} savings\n\n`) + tripStatus_(t) };
  }
  if (a === 't_plan') {
    splitItems_(body.plan).forEach(p => tripPlan_().appendRow([t.name, cap_(String(body.day).trim()), cap_(p)]));
    return { text: itineraryText_(t) };
  }
  if (a === 't_itinerary') return { text: itineraryText_(t) };
  if (a === 't_spent') {
    const amt = Number(String(body.amount).replace(/[$,]/g, ''));
    if (!(amt > 0)) throw new Error('amount must be a number like 45');
    tripSpend_().appendRow([new Date(), t.name, amt, body.what || '']);
    return { text: `🧾 Logged ${money_(amt)}${body.what ? ' for ' + body.what : ''}\n\n` + tripStatus_(t) };
  }
  if (a === 't_quote') {
    const amt = Number(String(body.price).replace(/[$,]/g, ''));
    if (!(amt > 0)) throw new Error('price must be a number like 289');
    tripQuotes_().appendRow([new Date(), t.name, cap_(body.type), cap_(body.where), amt, body.note || '']);
    return { text: quotesText_(t) };
  }
  if (a === 't_quotes') return { text: quotesText_(t) };
  if (a === 't_flight') {
    const dep = parseWhen_(body.date, body.depart);
    let arr = body.arrive ? parseWhen_(body.arrive_date || body.date, body.arrive) : '';
    const fromTz = airportTz_(body.from), toTz = airportTz_(body.to);
    let mins = parseDuration_(body.duration), hint = '';
    if (arr) {
      const gap = () => (fromTz && toTz) ? (wallToUtc_(arr, toTz) - wallToUtc_(dep, fromTz)) / 60000 : (arr - dep) / 60000;
      if (!body.arrive_date && gap() <= 0) arr = new Date(arr.getTime() + 86400000);   // lands the next day
      if (mins == null && fromTz && toTz) mins = Math.round(gap());
    }
    if (mins == null && arr && !(fromTz && toTz)) hint = '\n\nℹ️ I don\'t know one of those airports\' time zones, so add the flight length yourself (duration: 4h 10m).';
    if (mins != null && (mins <= 0 || mins > 24 * 60)) throw new Error('flight length came out wrong. Check the times, or type the length yourself');
    flights_().appendRow([t.name, cap_(body.leg || 'Flight'), body.airline || '', body.number || '',
      String(body.from || '').toUpperCase(), String(body.to || '').toUpperCase(), dep, arr, body.note || '',
      mins != null ? fmtDur_(mins) : '']);
    return { text: flightsText_(t) + hint };
  }
  if (a === 't_flights') return { text: flightsText_(t) };
  if (a === 't_book') {
    const type = cap_(String(body.type || 'Other'));
    const isHotel = type === 'Hotel';
    const start = body.date ? parseWhen_(body.date, body.time || (isHotel ? '3:00 PM' : '12:00 PM')) : '';
    const end = body.end_date || body.end_time
      ? parseWhen_(body.end_date || body.date, body.end_time || (isHotel ? '11:00 AM' : '12:00 PM')) : '';
    if (isHotel && (!start || !end)) throw new Error('a hotel needs a check-in date and a check-out date');
    if (start && end && end < start) throw new Error('the end is before the start. Check the dates');
    const cost = Number(String(body.cost || 0).replace(/[$,]/g, '')) || 0;
    bookings_().appendRow([t.name, type, body.name || '', start, end, cost, body.address || '', body.confirmation || '', body.note || '']);
    return { text: bookingsText_(t) };
  }
  if (a === 't_bookings') return { text: bookingsText_(t) };
  if (a === 'p_add') return { added: addPacking_(t, splitItems_(body.items)), text: packingText_(t) };
  if (a === 'p_always') return { added: addPacking_(t, rows_(packAlways_()).map(r => String(r[0])).filter(Boolean)), text: packingText_(t) };
  if (a === 'p_savealways') {
    const sh = packAlways_(), have = new Set(rows_(sh).map(r => String(r[0]).toLowerCase())), saved = [];
    splitItems_(body.items).map(cap_).forEach(i => { if (!have.has(i.toLowerCase())) { sh.appendRow([i]); saved.push(i); } });
    return { saved };
  }
  if (a === 'p_list') return { text: packingText_(t) };
  if (a === 'p_done') {
    const want = splitItems_(body.items), all = want.length === 1 && want[0].toLowerCase() === 'all', sh = packing_(), done = [];
    rows_(sh).forEach((r, i) => {
      if (sameName_(r[0], t.name) && r[2] !== true && (all || want.some(w => String(r[1]).toLowerCase().includes(w.toLowerCase())))) {
        sh.getRange(i + 2, 3).setValue(true); done.push(r[1]);
      }
    });
    return { done, text: packingText_(t) };
  }
  return null;
}

// Daily: countdown milestones + Monday savings nudge (called from dailyCheck)
function tripReminders_() {
  const milestones = [180, 100, 60, 30, 14, 7, 3, 1, 0];
  const monday = new Date().getDay() === 1;
  allTrips_().forEach(t => {
    tripBookings_(t).forEach(b => {
      if (b.start && daysUntil_(b.start) === 1) {
        const what = b.type === 'Hotel' ? `Hotel check-in tomorrow: ${b.name}` : `Tomorrow: ${b.name}`;
        notify_(`${BOOK_ICON[b.type] || '📌'} ${what}`, bookingLine_(b));
      }
    });
    tripFlights_(t).forEach(f => {
      const d = daysUntil_(f.dep);
      if (d === 1) notify_(`✈️ Flight tomorrow: ${f.leg} for ${t.name}`, flightLine_(f) + '\n\nCheck-in usually opens 24 hours before departure. Times are local to each airport.');
      if (d === 0) notify_(`✈️ Flight TODAY: ${f.leg} for ${t.name}`, flightLine_(f) + '\n\nHave your ID ready. Safe travels! 🛫');
    });
    if (!t.date) return;
    const d = daysUntil_(t.date);
    if (d < 0) return;
    if (milestones.includes(d)) {
      let extra = '';
      if (d === 7) extra = '\nTime to start packing: /pack list';
      if (d === 1) extra = '\nLast check: /pack list and /trip itinerary';
      notify_(d === 0 ? `🎉 ${t.name} is TODAY!` : `⏳ ${d} days until ${t.name}!`, tripStatus_(t) + extra);
    } else if (monday && t.goal) {
      notify_(`💰 Weekly ${t.name} check-in`, tripStatus_(t));
    }
  });
}

// ===== NOTIFY (email + Discord) =====
function notify_(title, text) {
  const to = SETTINGS.EMAIL_TO || Session.getActiveUser().getEmail();
  GmailApp.sendEmail(to, title, text);
  if (SETTINGS.DISCORD_WEBHOOK) {
    UrlFetchApp.fetch(SETTINGS.DISCORD_WEBHOOK, {
      method: 'post', contentType: 'application/json', muteHttpExceptions: true,
      payload: JSON.stringify({ username: 'StoltBot Budget', content: `**${title}**\n${text}`.slice(0, 1990) }),
    });
  }
}

// ===== WEB LINK FOR STOLTBOT (/spent, /budget, /bills) =====
function doPost(e) {
  let body = {};
  try { body = JSON.parse(e.postData.contents); } catch (err) {}
  const reply = obj => ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
  if (!SETTINGS.SECRET || body.secret !== SETTINGS.SECRET) return reply({ ok: false, error: 'wrong secret' });

  if (body.action === 'add') {
    const amount = Number(String(body.amount).replace(/[$,]/g, ''));
    if (!(amount > 0)) return reply({ ok: false, error: 'amount must be a number like 12.50' });
    const cat = addTransaction_(new Date(), amount, body.note || '(no note)', body.category, '', 'Discord', '');
    const now = new Date();
    const s = monthSummary_(now.getFullYear(), now.getMonth());
    const c = s.cats.find(x => x.category === cat) || { spent: amount, budget: 0 };
    return reply({ ok: true, category: cat, spent: c.spent, budget: c.budget });
  }
  if (body.action === 'summary') {
    const now = new Date();
    const s = monthSummary_(now.getFullYear(), now.getMonth());
    return reply({ ok: true, month: monthName_(s.year, s.month), text: summaryText_(s, null, true) });
  }
  if (body.action === 'income') {
    const amount = Number(String(body.amount).replace(/[$,]/g, ''));
    if (!(amount > 0)) return reply({ ok: false, error: 'amount must be a number like 650.00' });
    income_().appendRow([new Date(), amount, body.source || 'Paycheck', body.note || '']);
    const now = new Date();
    return reply({ ok: true, income: monthSummary_(now.getFullYear(), now.getMonth()).income });
  }
  if (body.action === 'balance') {
    const amount = Number(String(body.amount).replace(/[$,]/g, ''));
    if (isNaN(amount)) return reply({ ok: false, error: 'balance must be a number like 842.17' });
    const name = setBalance_(body.account, amount);
    return reply({ ok: true, account: name, balances: balances_().map(b => ({ account: b.account, balance: b.balance })) });
  }
  if (body.action === 'accounts') return reply({ ok: true, accounts: balances_().map(b => String(b.account)) });
  if (body.action === 'bills') {
    const list = upcomingBills_(Number(body.days) || 14).map(b => ({
      name: b.name, amount: b.amount, days: b.days,
      due: Utilities.formatDate(b.due, Session.getScriptTimeZone(), 'EEE MMM d'),
    }));
    return reply({ ok: true, bills: list });
  }
  if (body.action === 'categories') return reply({ ok: true, categories: categories_() });
  if (String(body.action).startsWith('c_')) {
    try {
      const out = careerAction_(body);
      if (out) return reply({ ok: true, ...out });
    } catch (err) {
      return reply({ ok: false, error: err.message });
    }
  }
  if (String(body.action).startsWith('pn_')) {
    try {
      const out = pantryAction_(body);
      if (out) return reply({ ok: true, ...out });
    } catch (err) {
      return reply({ ok: false, error: err.message });
    }
  }
  if (/^[tp]_/.test(String(body.action))) {
    try {
      const out = tripAction_(body);
      if (out) return reply({ ok: true, ...out });
    } catch (err) {
      return reply({ ok: false, error: err.message });
    }
  }
  if (String(body.action).startsWith('g_')) {
    try {
      const out = groceryAction_(body);
      if (out) return reply({ ok: true, ...out });
    } catch (err) {
      return reply({ ok: false, error: err.message });
    }
  }
  return reply({ ok: false, error: 'unknown action' });
}

// Makes a NEW random secret, saves it in Script Properties, and prints it.
// Afterwards: put the printed code in budget.txt, restart StoltBot, and deploy a New version.
function makeSecret() {
  const s = Utilities.getUuid().replace(/-/g, '');
  PropertiesService.getScriptProperties().setProperty('SECRET', s);
  console.log('New secret saved. Put this in budget.txt as SECRET=' + s);
}

// Shows which settings are filled in (without printing the secret itself)
function checkSettings() {
  console.log('SECRET: ' + (SETTINGS.SECRET ? 'set ✅' : 'MISSING ❌'));
  console.log('DISCORD_WEBHOOK: ' + (SETTINGS.DISCORD_WEBHOOK ? 'set ✅' : 'not set (optional)'));
  console.log('EMAIL_TO: ' + (SETTINGS.EMAIL_TO || 'not set, using your own Gmail'));
}

// ===== RUN ONCE =====
function setup() {
  const b = budget_(); if (b.getLastRow() < 2) DEFAULT_CATEGORIES.forEach(r => b.appendRow(r));
  const r = rules_(); if (r.getLastRow() < 2) DEFAULT_RULES.forEach(x => r.appendRow(x));
  const bl = bills_(); if (bl.getLastRow() < 2) bl.appendRow(['Rent (example, edit me)', 0, 1, 3, 'Rent', '']);
  tx_(); income_(); groceries_(); prices_(); pantry_();
  const cr = career_(); if (cr.getLastRow() < 2) CAREER_PLAN.forEach(r => cr.appendRow([...r, '']));
  cr.getRange('F:F').setNumberFormat('mmm d, yyyy'); gigs_().getRange('D:D').setNumberFormat('$#,##0.00');
  pantry_().getRange('F:F').setNumberFormat('mmm d, yyyy');
  pantry_().getRange('G:G').setNumberFormat('mmm d, h:mm am/pm');
  trips_(); tripSave_(); tripPlan_(); packing_(); tripSpend_(); tripQuotes_(); flights_(); bookings_();
  bookings_().getRange('D:E').setNumberFormat('ddd mmm d, yyyy h:mm am/pm');
  bookings_().getRange('F:F').setNumberFormat('$#,##0.00');
  flights_().getRange('G:H').setNumberFormat('ddd mmm d, yyyy h:mm am/pm');
  if (!flights_().getRange(1, 10).getValue()) flights_().getRange(1, 10).setValue('Duration').setFontWeight('bold').setBackground('#e8eaf6');
  const pa = packAlways_(); if (pa.getLastRow() < 2) ['Phone charger', 'ID / license', 'Toothbrush', 'Medications', 'Clothes', 'Headphones'].forEach(i => pa.appendRow([i]));
  ['Trips', 'Trip Savings', 'Trip Spending'].forEach(n => SpreadsheetApp.getActiveSpreadsheet().getSheetByName(n).getRange(n === 'Trips' ? 'B:B' : 'C:C').setNumberFormat('$#,##0.00'));
  tripQuotes_().getRange('E:E').setNumberFormat('$#,##0.00');
  const st = staples_(); if (st.getLastRow() < 2) ['Milk', 'Eggs', 'Bread', 'Coffee'].forEach(i => st.appendRow([i, sectionOf_(i)]));
  const ml = meals_(); if (ml.getLastRow() < 2) ml.appendRow(['Tacos', 'Ground beef, Taco seasoning, Tortillas, Cheese, Lettuce, Tomato, Sour cream']);
  prices_().getRange('D:D').setNumberFormat('$#,##0.00');
  const ac = accounts_(); if (ac.getLastRow() < 2) ac.appendRow(['Focus Card', 0, new Date()]);
  income_().getRange('B:B').setNumberFormat('$#,##0.00');
  accounts_().getRange('B:B').setNumberFormat('$#,##0.00');
  budget_().getRange('B:B').setNumberFormat('$#,##0.00');
  bills_().getRange('B:B').setNumberFormat('$#,##0.00');
  tx_().getRange('B:B').setNumberFormat('$#,##0.00');
  const def = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('Sheet1');
  if (def && def.getLastRow() === 0) SpreadsheetApp.getActiveSpreadsheet().deleteSheet(def);

  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('importBankEmails').timeBased().everyHours(1).create();
  ScriptApp.newTrigger('dailyCheck').timeBased().atHour(SETTINGS.REMIND_HOUR).everyDays(1).create();
  ScriptApp.newTrigger('monthlyReport').timeBased().onMonthDay(1).atHour(9).create();
  console.log('Budget bot is set up. Fill in the Budget and Bills tabs next.');
}
