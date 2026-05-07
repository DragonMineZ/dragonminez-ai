import re

import discord

from bulmaai.services.faq_knowledge import FAQReviewCandidate, FAQReviewCandidateInput

FAQ_REVIEW_CUSTOM_ID_PREFIX = "faq_review"
FAQ_REVIEW_ACTIONS = {"approve", "reject", "modify"}


def _truncate(value: str, limit: int) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rsplit(" ", 1)[0] + "..."


def parse_faq_review_custom_id(custom_id: str) -> tuple[str, int]:
    parts = custom_id.split(":")
    if len(parts) != 3 or parts[0] != FAQ_REVIEW_CUSTOM_ID_PREFIX:
        raise ValueError("Unsupported FAQ review custom id")
    action = parts[1]
    if action not in FAQ_REVIEW_ACTIONS:
        raise ValueError(f"Unsupported FAQ review action: {action}")
    return action, int(parts[2])


def build_faq_review_embed(candidate: FAQReviewCandidate) -> discord.Embed:
    color = {
        "pending": discord.Color.gold(),
        "approved": discord.Color.green(),
        "rejected": discord.Color.red(),
    }.get(candidate.status, discord.Color.blurple())
    embed = discord.Embed(
        title=f"FAQ Review Candidate #{candidate.id}",
        description=(
            f"**Question:** {_truncate(candidate.canonical_question, 900)}\n"
            f"**Language:** `{candidate.lang}`\n"
            f"**Status:** `{candidate.status}`"
        ),
        color=color,
    )
    embed.add_field(
        name="Answer",
        value=_truncate(candidate.answer, 1000) or "No answer provided.",
        inline=False,
    )
    embed.add_field(
        name="Tags",
        value=", ".join(f"`{tag}`" for tag in candidate.tags) or "`none`",
        inline=False,
    )
    source_parts = []
    if candidate.source_ticket_channel_id is not None:
        source_parts.append(f"Channel `{candidate.source_ticket_channel_id}`")
    if candidate.source_question_message_ids:
        source_parts.append(
            "Question messages "
            + ", ".join(f"`{message_id}`" for message_id in candidate.source_question_message_ids[:5])
        )
    if candidate.source_answer_message_ids:
        source_parts.append(
            "Answer messages "
            + ", ".join(f"`{message_id}`" for message_id in candidate.source_answer_message_ids[:5])
        )
    embed.add_field(
        name="Source",
        value="\n".join(source_parts) if source_parts else "Manual staff proposal.",
        inline=False,
    )
    if candidate.reviewed_by is not None:
        embed.add_field(name="Reviewed By", value=f"`{candidate.reviewed_by}`", inline=True)
    if candidate.review_reason:
        embed.add_field(name="Review Reason", value=_truncate(candidate.review_reason, 500), inline=False)
    if candidate.approved_faq_id is not None:
        embed.add_field(name="Approved FAQ", value=f"`{candidate.approved_faq_id}`", inline=True)
    embed.set_footer(text="Approve to add this answer to the searchable FAQ knowledge base.")
    return embed


class FAQReviewView(discord.ui.View):
    def __init__(self, *, candidate_id: int, status: str = "pending"):
        super().__init__(timeout=None)
        disabled = status != "pending"
        self.add_item(
            discord.ui.Button(
                label="Approve",
                style=discord.ButtonStyle.success,
                custom_id=f"{FAQ_REVIEW_CUSTOM_ID_PREFIX}:approve:{candidate_id}",
                disabled=disabled,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Reject",
                style=discord.ButtonStyle.danger,
                custom_id=f"{FAQ_REVIEW_CUSTOM_ID_PREFIX}:reject:{candidate_id}",
                disabled=disabled,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Modify",
                style=discord.ButtonStyle.primary,
                custom_id=f"{FAQ_REVIEW_CUSTOM_ID_PREFIX}:modify:{candidate_id}",
                disabled=disabled,
            )
        )


class FAQRejectModal(discord.ui.Modal):
    def __init__(self, candidate_id: int):
        super().__init__(title=f"Reject FAQ Candidate #{candidate_id}")
        self.reason: str | None = None
        self.reason_input = discord.ui.InputText(
            label="Reason",
            placeholder="Why should this candidate not become an FAQ?",
            style=discord.InputTextStyle.long,
            min_length=3,
            max_length=1000,
            required=True,
        )
        self.add_item(self.reason_input)

    async def callback(self, interaction: discord.Interaction):
        self.reason = self.reason_input.value.strip()
        await interaction.response.defer()


class FAQModifyModal(discord.ui.Modal):
    def __init__(self, candidate: FAQReviewCandidate):
        super().__init__(title=f"Modify FAQ Candidate #{candidate.id}")
        self.result: FAQReviewCandidateInput | None = None
        self.lang_input = discord.ui.InputText(
            label="Language",
            value=candidate.lang,
            min_length=2,
            max_length=5,
            required=True,
        )
        self.question_input = discord.ui.InputText(
            label="Canonical Question",
            value=candidate.canonical_question[:400],
            min_length=3,
            max_length=400,
            required=True,
        )
        self.answer_input = discord.ui.InputText(
            label="Answer",
            value=candidate.answer[:3000],
            style=discord.InputTextStyle.long,
            min_length=3,
            max_length=3000,
            required=True,
        )
        self.tags_input = discord.ui.InputText(
            label="Tags",
            value=", ".join(candidate.tags),
            max_length=300,
            required=False,
        )
        self.add_item(self.lang_input)
        self.add_item(self.question_input)
        self.add_item(self.answer_input)
        self.add_item(self.tags_input)
        self._candidate = candidate

    async def callback(self, interaction: discord.Interaction):
        tags = [
            part.strip()
            for part in (self.tags_input.value or "").replace("\n", ",").split(",")
            if part.strip()
        ]
        self.result = FAQReviewCandidateInput(
            lang=self.lang_input.value.strip(),
            canonical_question=self.question_input.value.strip(),
            answer=self.answer_input.value.strip(),
            tags=tags,
            source_ticket_channel_id=self._candidate.source_ticket_channel_id,
            source_question_message_ids=self._candidate.source_question_message_ids,
            source_answer_message_ids=self._candidate.source_answer_message_ids,
            proposed_by=self._candidate.proposed_by,
        )
        await interaction.response.defer()
