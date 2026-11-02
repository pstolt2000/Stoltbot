/**
 * Dena's Diamond Nook - Store Email Sorter (Google Apps Script)
 * Run this in the STORE Gmail account (the one customers and WooCommerce email).
 *
 * Every 15 minutes it:
 *  1. Sorts new store email into labels: Orders, Google, Problems, Custom Requests, Questions
 *  2. Stars + labels anything a customer is waiting on as "Store/Needs reply"
 *  3. Writes a DRAFT reply for common questions (shipping time, size). Drafts are never sent.
 *  4. Optional: pings your Discord #store-alerts channel about problems and custom requests
 *
 * Setup: paste into a new Apps Script project, edit SETTINGS, run setupTriggers() once.
 */

// ===== SETTINGS (edit these) =====
const STORE_NAME = "Dena's Diamond Nook";
const STORE_URL = 'https://thediamondnook.shop';
const SHIPPING_COST = '$5.99 flat rate';
const SHIPPING_TIME = '3-7 business days';          // change to your real delivery time
const SIGN_OFF = "Thanks so much!\nDena's Diamond Nook";
const DISCORD_WEBHOOK = '';                          // optional: paste a Discord webhook URL here

// Gmail search rules. First match wins. needsReply = star it + "Needs reply" label.
const RULES = [
  { label: 'Store/Orders',          needsReply: false, query: 'subject:("new order" OR "order #" OR "order status")' },
  { label: 'Store/Google',          needsReply: false, query: 'from:google.com (merchant OR shopping OR "product disapproved" OR "account")' },
  { label: 'Store/Problems',        needsReply: true,  alert: true,
    query: '(refund OR damaged OR broken OR missing OR "wrong item" OR "never arrived" OR "not received" OR "hasn\'t arrived" OR cancel OR return)' },
  { label: 'Store/Custom Requests', needsReply: true,  alert: true,
    query: '(custom OR commission OR personalized OR personalised OR "my photo" OR "own photo" OR "my pet")' },
  { label: 'Store/Questions',       needsReply: true,
    query: '-from:(noreply OR no-reply OR donotreply OR do-not-reply OR mailer-daemon OR wordpress OR woocommerce OR google.com)' },
];

// Draft-reply templates for common questions (only used on "Store/Questions" emails)
const TEMPLATES = [
  {
    name: 'shipping',
    match: /(ship|deliver|arriv|how long|when will|tracking)/i,
    body: name => `Hi${name ? ' ' + name : ''},\n\nThanks for reaching out! Shipping is ${SHIPPING_COST}, and orders usually arrive within ${SHIPPING_TIME} after they ship. As soon as your order ships you'll get an email with tracking.\n\n${SIGN_OFF}`,
  },
  {
    name: 'size',
    match: /(size|how big|dimension|measure|inches|\bcm\b)/i,
    body: name => `Hi${name ? ' ' + name : ''},\n\nGreat question! Each piece is one of a kind, and its exact size is listed on its product page at ${STORE_URL}. If you tell me which piece you're looking at, I'm happy to double-check it for you.\n\n${SIGN_OFF}`,
  },
];

const DONE_LABEL = 'Store/Sorted';
const REPLY_LABEL = 'Store/Needs reply';
const DRAFT_LABEL = 'Store/Draft ready';

// ===== THE BOT =====
function sortStoreEmail() {
  const done = getLabel_(DONE_LABEL);
  const base = `in:inbox newer_than:7d -label:${slug_(DONE_LABEL)}`;
  const claimed = new Set();

  RULES.forEach(rule => {
    const threads = GmailApp.search(`${base} ${rule.query}`, 0, 50)
      .filter(t => !claimed.has(t.getId()));
    if (threads.length === 0) return;

    getLabel_(rule.label).addToThreads(threads);
    threads.forEach(t => claimed.add(t.getId()));

    if (rule.needsReply) {
      getLabel_(REPLY_LABEL).addToThreads(threads);
      threads.forEach(t => t.getMessages().slice(-1)[0].star());
    }
    if (rule.label === 'Store/Questions') threads.forEach(writeDraftIfCommon_);
    if (rule.alert) threads.forEach(t => discordAlert_(rule.label, t));

    console.log(`${rule.label}: ${threads.length}`);
  });

  if (claimed.size) {
    done.addToThreads(GmailApp.search(base).filter(t => claimed.has(t.getId())));
  }
}

function writeDraftIfCommon_(thread) {
  const msg = thread.getMessages().slice(-1)[0];
  const text = `${thread.getFirstMessageSubject()} ${msg.getPlainBody()}`.slice(0, 3000);
  const template = TEMPLATES.find(t => t.match.test(text));
  if (!template) return;
  const first = msg.getFrom().replace(/<.*>/, '').replace(/"/g, '').trim().split(' ')[0];
  const name = /@/.test(first) ? '' : first;
  msg.createDraftReply(template.body(name));
  getLabel_(DRAFT_LABEL).addToThread(thread);
  console.log(`Draft (${template.name}) for: ${thread.getFirstMessageSubject()}`);
}

function discordAlert_(label, thread) {
  if (!DISCORD_WEBHOOK) return;
  const msg = thread.getMessages().slice(-1)[0];
  const kind = label.replace('Store/', '');
  const payload = {
    username: 'StoltBot Mail',
    content: `📧 **${kind}** email needs a reply`,
    embeds: [{
      title: thread.getFirstMessageSubject().slice(0, 250) || '(no subject)',
      url: `https://mail.google.com/mail/u/0/#inbox/${thread.getId()}`,
      description: msg.getPlainBody().slice(0, 300),
      footer: { text: `From: ${msg.getFrom().replace(/<.*>/, '').trim()}` },
      color: kind === 'Problems' ? 15158332 : 10181046,
    }],
  };
  UrlFetchApp.fetch(DISCORD_WEBHOOK, {
    method: 'post', contentType: 'application/json',
    payload: JSON.stringify(payload), muteHttpExceptions: true,
  });
}

function getLabel_(name) {
  return GmailApp.getUserLabelByName(name) || GmailApp.createLabel(name);
}

function slug_(name) {
  return name.toLowerCase().replace(/[\/ ]/g, '-');
}

// Run this ONE time. It schedules the sorter every 15 minutes and runs it once now.
function setupTriggers() {
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'sortStoreEmail')
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('sortStoreEmail').timeBased().everyMinutes(15).create();
  sortStoreEmail();
  console.log('Store email sorter is set up.');
}
