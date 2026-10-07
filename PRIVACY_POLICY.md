# Privacy Policy — BulmaAI (DragonMineZ Support Bot)

**Last updated:** October 7, 2026

This Privacy Policy explains what information the BulmaAI Discord bot ("the Bot",
"we", "us") collects, how it is used, how long it is kept, and the choices you
have. The Bot operates within the official DragonMineZ Discord community and is
maintained by the DragonMineZ team.

By using the Bot or interacting with it in a server where it is present, you
agree to the practices described in this policy.

---

## 1. Information We Collect

We collect only the data needed to operate the Bot's features:

- **Discord identifiers.** Your Discord user ID, and the IDs of servers, channels,
  and roles relevant to the features you use.
- **Message content.** In designated support channels, the Bot reads the content
  of messages so it can answer support questions, detect and parse crash/log
  files, and provide translations. When you mention the Bot in a public channel,
  it also reads the recent messages around yours (up to 15 from the last 30
  minutes, from anyone) so it understands the conversation. The Bot's moderation feature also inspects
  message content to detect phishing links and unsolicited server invites.
- **Support interaction logs.** When you use the AI support feature, we store the
  text of your question and the Bot's reply, together with technical metadata
  (timestamps, channel ID, user ID, model name, token counts, and latency) for
  debugging and quality evaluation.
- **Patreon supporter data.** If you link a Patreon account, we store your
  Discord user ID and Discord username together with your Patreon user ID,
  Patreon member ID, Patreon full name, pledge status, tier IDs, last charge
  date, and whether your perks are active. We also store your beta-access
  records: the Minecraft usernames on your whitelist, whether each one is yours
  or a gift to another member, the Discord ID and username of the person who
  receives a gift, and the link to the GitHub pull request that added the name.
- **Minecraft usernames.** When you request beta access, we check the username
  against Mojang's public profile API (Section 7).
- **Beta access sign-in.** If you start beta access from inside Minecraft, you
  are sent to Discord to sign in with the `identify` scope, so we can learn
  which Discord account you are. A short-lived cookie
  (`dmz_beta_access_nonce`, about 10 minutes, HttpOnly) ties that sign-in to
  your browser. We do not use it for tracking.
- **Dev build downloads.** We record which Discord user downloaded which
  early-access file, and when.
- **Bug reports.** When you post in the bug-report forum, we store your Discord
  user ID, the thread, an AI-written title and summary, the status, and the
  GitHub issue it was filed under.
- **Tickets.** When you open a support ticket, we store the ticket number,
  category, language, who opened, claimed and closed it, the close reason, and
  timestamps. When it is closed we store the transcript text, an AI summary,
  and text descriptions of any screenshots.
- **Moderation records.** See Section 4 and the list below.
  - **Cases.** Warnings, timeouts, bans and notes, with the reason, the
    moderator (or "automod" source), the duration and timestamps.
  - **Automod hits.** When a message trips a filter, we store your user ID,
    the reason and action, details, any flagged domains, hashes of any images,
    and the staff review outcome. We do not store the images themselves.
  - **Joiner alerts.** When a new member looks risky, we store their user ID,
    the reason, the action taken and the staff outcome.
  - **Scam image hashes.** A list of perceptual hashes (fingerprints) of known
    scam images, with who added each one. These are not the images.
- **Message counts and timestamps (leveling).** To power the server's
  message-based leveling and leaderboard feature, the Bot counts the messages
  you send and records the timestamp of your most recent qualifying message,
  per server. It does **not** store the content of those messages for this
  purpose, and it does not track voice activity or presence.
- **Showcase highlights.** In designated showcase channels, the Bot counts
  reactions on messages to identify popular community content and repost it to
  a highlights channel. Reaction counts are read live from Discord and are not
  stored; the Bot records only the message IDs it has already highlighted, so
  the same post is not reposted twice. It does not store who reacted.
- **Staff panel data.** Staff use a web admin panel that signs in with Discord
  and uses a session cookie (HttpOnly). Panel actions are written to an audit
  log with the staff member's Discord ID, the action, its target and details.
- **Bot logs.** Operational logs can be forwarded to a Discord channel. They
  carry IDs (user, channel, guild, message) and error types. The
  forwarder removes fields that look like message text, tokens or secrets.

We do **not** collect message content outside of the specific support and
moderation contexts described above. Message content is never stored for
leveling or showcase-highlight purposes — only counts and timestamps, as
described above — and we do not track your presence, voice activity, or
online status.

---

## 2. How We Use Your Information

We use the information we collect to:

- Answer support questions about the DragonMineZ mod using an AI assistant.
- Detect, parse, and help resolve crash logs and errors you share.
- Moderate the community by removing phishing links and invite spam, and keep
  a history of warnings, timeouts, bans and automod incidents.
- Translate staff announcements into additional languages.
- Manage Patreon supporter perks, welcome messages, and beta access, including
  removing beta access when a pledge ends.
- Track message counts to power server leveling, leaderboards, and
  level-based role rewards.
- Identify and repost popular messages in showcase channels based on
  reaction counts.
- Run support tickets and bug reports, and track who downloaded early-access
  builds.
- Debug issues and evaluate the quality of the Bot's responses.

---

## 3. Third-Party Processing (OpenAI)

To generate AI support answers, translations, and log analysis, message content
is transmitted to the OpenAI API for processing at the time of your request.
This includes the recent conversation around your request (see Section 1).
When your question is about your own account, the Bot also sends the relevant
account data to OpenAI: your Patreon link and role status, your beta whitelist
entries (Minecraft usernames), your dev jar access, and your bug report status.
It never sends your Patreon name, Patreon IDs, or billing details, and it only
looks up the account of the person asking.

We participate in OpenAI's data sharing program, so inputs and outputs sent to
the OpenAI API **may be used by OpenAI to train and improve its models**. Do not
share passwords, personal contact details, or other sensitive information with
the Bot. OpenAI's handling of API data is governed by its own policies:
https://openai.com/policies/

We run internal quality evaluations on stored support questions and answers to
measure the accuracy of the Bot's responses. This tests our prompts and does not
train any model.

When a support ticket is closed, the text messages from the ticket (no images;
only previously generated text descriptions of screenshots) are summarized by
OpenAI and the transcript is stored in our database. Tickets with a reusable
problem/solution are uploaded to an OpenAI vector store (file storage used for
search, not model training) with the requester's name replaced by "Requester",
so the Bot can answer similar questions later.

---

## 4. Data Retention

We retain support interaction logs and Patreon supporter records for as long as
needed to operate and improve the Bot. When a pledge ends, your beta whitelist
grants are marked inactive and the names are removed from the current
whitelist file. The database rows are kept. Leveling data (message counts,
level, and timestamps) is retained for as long
as you remain part of the community, so your level and leaderboard standing are
preserved. Showcase highlight records store only message IDs and are kept for
as long as the highlights channel exists. Closed ticket transcripts and
screenshot descriptions are retained to improve support answers and can be
deleted on request (Section 8).

Translation content is processed in memory and is not retained beyond what is
required to act on it. Moderation is different. Cases, automod hits, joiner
alerts and scam image hashes are stored in our database and kept as moderation
history. Some records are marked inactive when a warning is deleted or a
temporary ban ends, but the rows are not erased automatically. Dev build
download records and the staff audit log are also kept without an automatic
expiry.

Hosted ticket transcript pages (Section 5) are removed after a set number of
days (30 by default) unless staff keep them permanently.

You may request deletion of your stored data at any time (see Section 8).

---

## 5. Public Information

Some information is public by design. Please read this before you request
beta access.

- **Beta whitelist on GitHub.** Beta access is managed through pull requests in
  the public GitHub repository DragonMineZ/.github. The whitelist file lists
  Minecraft usernames in plain text, visible to anyone.
- **Pull requests and comments.** Each pull request and its comments name the
  Discord username and Discord user ID of the member who requested the change,
  and for gifts also the recipient's. They also name the Minecraft username.
- **Git history is permanent.** Removing a name from the current file does not
  remove it from past commits or closed pull requests. We cannot erase those
  on request in the same way we can erase database rows.
- **Bug reports.** When staff create a GitHub issue from a bug report, the
  issue includes the AI-written summary, severity, affected area and steps to
  reproduce, your Discord mention and user ID, and a link to the Discord
  thread. Issues live in the mod's GitHub repository.
- **Hosted ticket transcripts.** Closed tickets can be published as an HTML
  page at a link of the form `/t/<token>`. No login is needed. Anyone who has
  the link can read the whole transcript. The pages are marked noindex, but
  do not post your link publicly. Pages expire as described in Section 4.

---

## 6. Data Storage and Security

Your data is stored in our own PostgreSQL database, separate from Discord. Data
is encrypted at rest, and access is restricted to the maintainers of the Bot.
We take reasonable measures to protect your information against unauthorized
access, loss, or misuse.

---

## 7. Data Sharing

We do not sell your data. We do not share your data with third parties except:

- **OpenAI**, solely to process message content and return AI responses, as
  described in Section 3.
- **Patreon.** You authorize Patreon through OAuth, and we also receive
  updates from Patreon (webhooks and the creator API) about your pledge. This
  data comes from Patreon to us. We do not send your data to Patreon.
- **Mojang**, which receives the Minecraft username you submit so we can check
  that it exists.
- **GitHub**, which receives and publishes the information described in
  Section 5.
- **Discord**, which hosts the community and signs you in for beta access and
  the staff panel.
- Where required by law or to protect the safety and integrity of our community.

---

## 8. Your Choices and Deletion Requests

You can request deletion of the data we store about you (support AI logs, session
data, Patreon link records, beta grants, tickets, and leveling/message-count
records). To do so, contact us via:

- Our Discord community support channel: https://discord.dragonminez.com/
- Email: contact@dragonminez.com

We will action verified deletion requests by removing your stored records from
our database. We may keep moderation records that are needed to keep the
community safe. Public GitHub history (Section 5) is outside our database and
may not be removable.

---

## 9. Children's Privacy

The Bot is intended for use in accordance with Discord's Terms of Service, which
require users to meet Discord's minimum age requirements. We do not knowingly
collect data from anyone who does not meet those requirements.

---

## 10. Changes to This Policy

We may update this Privacy Policy from time to time. Material changes will be
announced in the DragonMineZ Discord community. The "Last updated" date at the
top of this document reflects the most recent revision.

---

## 11. Contact

For questions about this Privacy Policy or your data, contact us at:

- Our Discord: https://discord.dragonminez.com/
- Email: contact@dragonminez.com
