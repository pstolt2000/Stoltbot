/**
 * Paul's Gmail Bot (Google Apps Script)
 * - organizeInbox(): labels new mail by rules, archives junk categories
 * - dailySummary(): emails you a digest of the last 24 hours
 * - setupTriggers(): run ONCE to make both run automatically
 */

// ===== EDIT THESE RULES =====
// label   = the Gmail label to apply (a "/" makes a sub-label)
// query   = Gmail search words (same as the Gmail search bar)
// archive = true moves it out of the inbox after labeling
const RULES = [
  { label: 'Bot/Bills',  query: 'subject:(bill OR invoice OR statement OR "payment due" OR autopay)', archive: false },
  { label: 'Bot/Amazon', query: 'from:amazon.com', archive: false },
  { label: 'Bot/Orders', query: 'subject:(order OR shipped OR delivered OR tracking)', archive: false },
  { label: 'Bot/Promos', query: 'category:promotions', archive: true },
  { label: 'Bot/Social', query: 'category:social', archive: true },
];

const SUMMARY_HOUR = 16;   // hour (0-23) the daily summary is sent
const MAX_IN_SUMMARY = 20; // how many emails to list in the summary

// ===== THE BOT =====
function organizeInbox() {
  RULES.forEach(rule => {
    const label = GmailApp.getUserLabelByName(rule.label) || GmailApp.createLabel(rule.label);
    const labelSearch = rule.label.toLowerCase().replace(/[\/ ]/g, '-');
    const query = `in:inbox newer_than:7d -label:${labelSearch} ${rule.query}`;
    const threads = GmailApp.search(query, 0, 100);
    if (threads.length === 0) return;

    label.addToThreads(threads);
    if (rule.archive) GmailApp.moveThreadsToArchive(threads);
    console.log(`${rule.label}: ${threads.length} thread(s)`);
  });
}

function dailySummary() {
  const threads = GmailApp.search('in:inbox newer_than:1d', 0, MAX_IN_SUMMARY);
  const unread = GmailApp.search('in:inbox is:unread newer_than:1d').length;

  let body = `Inbox summary for the last 24 hours\n`;
  body += `Unread: ${unread}\n\n`;

  if (threads.length === 0) {
    body += 'Nothing new in your inbox. 🎉';
  } else {
    threads.forEach((t, i) => {
      const msg = t.getMessages()[t.getMessageCount() - 1];
      const from = msg.getFrom().replace(/<.*>/, '').trim();
      const star = t.isUnread() ? '• ' : '  ';
      body += `${star}${i + 1}. ${from} — ${t.getFirstMessageSubject()}\n`;
    });
  }

  const me = Session.getActiveUser().getEmail();
  GmailApp.sendEmail(me, '📬 Gmail Bot: daily summary', body);
}

// Run this ONE time. It schedules the bot.
function setupTriggers() {
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('organizeInbox').timeBased().everyHours(1).create();
  ScriptApp.newTrigger('dailySummary').timeBased().atHour(SUMMARY_HOUR).everyDays(1).create();
  organizeInbox(); // run once right now
  console.log('Gmail Bot is set up.');
}
