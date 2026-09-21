# Privacy Policy — BulmaAI (DragonMineZ Support Bot)

**Last updated:** September 21, 2026

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
  files, and provide translations. The Bot's moderation feature also inspects
  message content to detect phishing links and unsolicited server invites.
- **Support interaction logs.** When you use the AI support feature, we store the
  text of your question and the Bot's reply, together with technical metadata
  (timestamps, channel ID, user ID, model name, token counts, and latency) for
  debugging and quality evaluation.
- **Patreon supporter data.** If you link a Patreon account or receive supporter
  perks, we store your Discord user ID together with your Patreon link status,
  granted roles, and beta-access/whitelist records.
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
- Moderate the community by removing phishing links and invite spam.
- Translate staff announcements into additional languages.
- Manage Patreon supporter perks, welcome messages, and beta access.
- Track message counts to power server leveling, leaderboards, and
  level-based role rewards.
- Identify and repost popular messages in showcase channels based on
  reaction counts.
- Debug issues and evaluate the quality of the Bot's responses.

---

## 3. Third-Party Processing (OpenAI)

To generate AI support answers, translations, and log analysis, message content
is transmitted to the OpenAI API for processing at the time of your request.
OpenAI processes this data to return a response. This content is **not** used to
train or fine-tune any machine learning or AI model. OpenAI's handling of API
data is governed by its own policies: https://openai.com/policies/

We run internal quality evaluations on stored support questions and answers to
measure the accuracy of the Bot's responses. This tests our prompts and does not
train any model.

---

## 4. Data Retention

We retain support interaction logs and Patreon supporter records for as long as
needed to operate and improve the Bot. Moderation and translation content is
processed in memory and is not retained beyond what is required to act on it.
Leveling data (message counts, level, and timestamps) is retained for as long
as you remain part of the community, so your level and leaderboard standing are
preserved. Showcase highlight records store only message IDs and are kept for
as long as the highlights channel exists.

You may request deletion of your stored data at any time (see Section 7).

---

## 5. Data Storage and Security

Your data is stored in our own PostgreSQL database, separate from Discord. Data
is encrypted at rest, and access is restricted to the maintainers of the Bot.
We take reasonable measures to protect your information against unauthorized
access, loss, or misuse.

---

## 6. Data Sharing

We do not sell your data. We do not share your data with third parties except:

- **OpenAI**, solely to process message content and return AI responses, as
  described in Section 3.
- Where required by law or to protect the safety and integrity of our community.

---

## 7. Your Choices and Deletion Requests

You can request deletion of the data we store about you (support AI logs, session
data, Patreon link records, and leveling/message-count records). To do so,
contact us via:

- Our Discord community support channel: https://discord.dragonminez.com/
- Email: contact@dragonminez.com

We will action verified deletion requests by removing your stored records from
our database.

---

## 8. Children's Privacy

The Bot is intended for use in accordance with Discord's Terms of Service, which
require users to meet Discord's minimum age requirements. We do not knowingly
collect data from anyone who does not meet those requirements.

---

## 9. Changes to This Policy

We may update this Privacy Policy from time to time. Material changes will be
announced in the DragonMineZ Discord community. The "Last updated" date at the
top of this document reflects the most recent revision.

---

## 10. Contact

For questions about this Privacy Policy or your data, contact us at:

- Our Discord: https://discord.dragonminez.com/
- Email: contact@dragonminez.com
