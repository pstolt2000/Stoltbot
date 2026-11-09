/**
 * Email -> Discord alerts (Google Apps Script)
 * Posts new emails from the people you choose into a Discord channel.
 *
 * Setup (one time):
 *  1. Paste this into a new project at script.google.com (signed in to your Gmail).
 *  2. Project Settings (gear) -> Script Properties -> Add:
 *       DISCORD_WEBHOOK = the channel's webhook URL (Discord: Edit Channel -> Integrations -> Webhooks)
 *  3. Edit WATCH below, then run setup() once and allow access.
 */

// ===== EDIT THIS =====
// name  = shown in Discord
// from  = email address(es) to watch (Gmail search style, use OR for more than one)
const WATCH = [
  { name: 'Jessica', from: 'jessica@example.com', color: 0x9b59b6 },
];
const CHECK_EVERY_MINUTES = 5;      // 1, 5, 10, 15 or 30
const LABEL = 'Bot/Sent to Discord'; // marks emails already posted
const PREVIEW_CHARS = 700;

// ===== THE BOT =====
function checkForEmails() {
  const hook = PropertiesService.getScriptProperties().getProperty('DISCORD_WEBHOOK');
  if (!hook) throw new Error('Add DISCORD_WEBHOOK in Project Settings -> Script Properties.');
  const label = GmailApp.getUserLabelByName(LABEL) || GmailApp.createLabel(LABEL);
  const labelSearch = LABEL.toLowerCase().replace(/[\/ ]/g, '-');

  WATCH.forEach(w => {
    const threads = GmailApp.search(`from:(${w.from}) newer_than:2d -label:${labelSearch}`, 0, 20);
    threads.reverse().forEach(t => {               // oldest first
      const msg = t.getMessages()[t.getMessageCount() - 1];
      const text = msg.getPlainBody().replace(/\r/g, '').replace(/\n{3,}/g, '\n\n').trim();
      const files = msg.getAttachments({ includeInlineImages: false });
      const payload = {
        username: 'Email Alerts',
        embeds: [{
          title: `📧 ${w.name}: ${msg.getSubject() || '(no subject)'}`.slice(0, 256),
          url: `https://mail.google.com/mail/u/0/#inbox/${t.getId()}`,
          description: (text.length > PREVIEW_CHARS ? text.slice(0, PREVIEW_CHARS) + '…' : text) || '(no text)',
          color: w.color,
          fields: files.length
            ? [{ name: `📎 ${files.length} attachment(s)`, value: files.map(f => f.getName()).join('\n').slice(0, 1000) }]
            : [],
          footer: { text: msg.getFrom().replace(/<.*>/, '').trim() },
          timestamp: msg.getDate().toISOString(),
        }],
      };
      const res = UrlFetchApp.fetch(hook, {
        method: 'post', contentType: 'application/json',
        payload: JSON.stringify(payload), muteHttpExceptions: true,
      });
      if (res.getResponseCode() >= 300) {
        console.log(`Discord said ${res.getResponseCode()}: ${res.getContentText()}`);
        return;                                    // try again next run
      }
      label.addToThread(t);
      Utilities.sleep(1000);                        // stay under Discord's rate limit
    });
  });
}

// Run this ONE time. It schedules the check and marks old emails so only NEW ones post.
function setup() {
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'checkForEmails')
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('checkForEmails').timeBased().everyMinutes(CHECK_EVERY_MINUTES).create();

  const label = GmailApp.getUserLabelByName(LABEL) || GmailApp.createLabel(LABEL);
  WATCH.forEach(w => label.addToThreads(GmailApp.search(`from:(${w.from})`, 0, 100)));
  console.log('Email -> Discord is set up. New emails will post within ' + CHECK_EVERY_MINUTES + ' minutes.');
}

// Optional: posts a test message so you can see it working.
function sendTest() {
  const hook = PropertiesService.getScriptProperties().getProperty('DISCORD_WEBHOOK');
  UrlFetchApp.fetch(hook, { method: 'post', contentType: 'application/json',
    payload: JSON.stringify({ username: 'Email Alerts', content: '✅ Email alerts are connected.' }) });
}
