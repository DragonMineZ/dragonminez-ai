import logging

import discord
from discord.ext import commands

from bulmaai.services.docs_ingestion import embed_texts
from bulmaai.services.faq_knowledge import (
    FAQReviewCandidateInput,
    approve_faq_candidate,
    create_faq_review_candidate,
    get_faq_review_candidate,
    list_pending_faq_review_candidates,
    reject_faq_candidate,
    update_faq_review_message,
)
from bulmaai.ui.faq_review_views import (
    FAQModifyModal,
    FAQRejectModal,
    FAQReviewView,
    build_faq_review_embed,
    parse_faq_review_custom_id,
)
from bulmaai.utils.permissions import is_staff

log = logging.getLogger(__name__)


def _parse_tags(raw_tags: str | None) -> list[str]:
    if not raw_tags:
        return []
    return [
        part.strip()
        for part in raw_tags.replace("\n", ",").split(",")
        if part.strip()
    ]


class FAQReviewCog(commands.Cog):
    """Staff review workflow for approving FAQ knowledge."""

    faq = discord.SlashCommandGroup("faq", "FAQ knowledge review tools")

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    def _is_staff_reviewer(self, user: discord.abc.User) -> bool:
        return isinstance(user, discord.Member) and is_staff(
            user,
            settings=self.bot.settings,
        )

    async def _reject_staff_only(self, responder) -> None:
        await responder.send_message(
            "Only staff can review FAQ candidates.",
            ephemeral=True,
        )

    async def _get_review_channel(self) -> discord.abc.Messageable | None:
        channel_id = (
            self.bot.settings.faq_review_channel_id
            or self.bot.settings.discord_log_channel_id
        )
        if channel_id is None:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                log.exception("Failed to fetch FAQ review channel %s", channel_id)
                return None
        return channel if hasattr(channel, "send") else None

    async def _send_review_message(self, candidate_id: int) -> discord.Message | None:
        candidate = await get_faq_review_candidate(candidate_id)
        if candidate is None:
            return None
        channel = await self._get_review_channel()
        if channel is None:
            return None
        message = await channel.send(
            embed=build_faq_review_embed(candidate),
            view=FAQReviewView(candidate_id=candidate.id, status=candidate.status),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await update_faq_review_message(
            candidate.id,
            channel_id=message.channel.id,
            message_id=message.id,
        )
        return message

    async def _edit_interaction_message(
        self,
        interaction: discord.Interaction,
        candidate_id: int,
    ) -> None:
        candidate = await get_faq_review_candidate(candidate_id)
        if candidate is None or interaction.message is None:
            return
        await interaction.message.edit(
            embed=build_faq_review_embed(candidate),
            view=FAQReviewView(candidate_id=candidate.id, status=candidate.status),
        )

    @faq.command(name="propose", description="Create a staff-reviewed FAQ candidate")
    @discord.option("question", description="Canonical support question", required=True)
    @discord.option("answer", description="Approved answer text", required=True)
    @discord.option("language", description="FAQ language", choices=["en", "es", "pt"], required=False)
    @discord.option("tags", description="Comma-separated tags", required=False)
    async def propose(
        self,
        ctx: discord.ApplicationContext,
        question: str,
        answer: str,
        language: str = "en",
        tags: str | None = None,
    ):
        if not self._is_staff_reviewer(ctx.author):
            return await ctx.respond("Only staff can propose FAQ candidates.", ephemeral=True)

        await ctx.defer(ephemeral=True)
        candidate_id = await create_faq_review_candidate(
            FAQReviewCandidateInput(
                lang=language,
                canonical_question=question,
                answer=answer,
                tags=_parse_tags(tags),
                proposed_by=ctx.author.id,
            )
        )
        message = await self._send_review_message(candidate_id)
        if message is None:
            return await ctx.followup.send(
                f"FAQ candidate #{candidate_id} was saved, but I could not post it to the review channel.",
                ephemeral=True,
            )
        await ctx.followup.send(
            f"FAQ candidate #{candidate_id} posted for staff review.",
            ephemeral=True,
        )

    @faq.command(name="review", description="Repost a FAQ candidate review card")
    @discord.option("candidate_id", description="FAQ candidate ID", required=True)
    async def review(self, ctx: discord.ApplicationContext, candidate_id: int):
        if not self._is_staff_reviewer(ctx.author):
            return await ctx.respond("Only staff can review FAQ candidates.", ephemeral=True)

        await ctx.defer(ephemeral=True)
        message = await self._send_review_message(candidate_id)
        if message is None:
            return await ctx.followup.send(
                "FAQ candidate not found or review channel unavailable.",
                ephemeral=True,
            )
        await ctx.followup.send(
            f"FAQ candidate #{candidate_id} reposted for review.",
            ephemeral=True,
        )

    @faq.command(name="pending", description="List pending FAQ review candidates")
    async def pending(self, ctx: discord.ApplicationContext):
        if not self._is_staff_reviewer(ctx.author):
            return await ctx.respond("Only staff can list FAQ candidates.", ephemeral=True)

        candidates = await list_pending_faq_review_candidates(limit=10)
        if not candidates:
            return await ctx.respond("No pending FAQ candidates.", ephemeral=True)
        lines = [
            f"`#{candidate.id}` [{candidate.lang}] {candidate.canonical_question[:120]}"
            for candidate in candidates
        ]
        await ctx.respond("\n".join(lines), ephemeral=True)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith("faq_review:"):
            return
        try:
            action, candidate_id = parse_faq_review_custom_id(custom_id)
        except ValueError:
            return

        if not self._is_staff_reviewer(interaction.user):
            return await self._reject_staff_only(interaction.response)

        if action == "approve":
            await self._handle_approve(interaction, candidate_id)
        elif action == "reject":
            await self._handle_reject(interaction, candidate_id)
        elif action == "modify":
            await self._handle_modify(interaction, candidate_id)

    async def _handle_approve(
        self,
        interaction: discord.Interaction,
        candidate_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            result = await approve_faq_candidate(
                candidate_id,
                actor_id=interaction.user.id,
                embedding_provider=embed_texts,
                embedding_model=self.bot.settings.openai_embedding_model,
            )
        except Exception as error:
            log.exception("Failed to approve FAQ candidate %s", candidate_id)
            return await interaction.followup.send(
                f"Failed to approve FAQ candidate: {error}",
                ephemeral=True,
            )

        await self._edit_interaction_message(interaction, candidate_id)
        await interaction.followup.send(
            f"FAQ candidate #{candidate_id} approved as FAQ #{result.faq_id}.",
            ephemeral=True,
        )

    async def _handle_reject(
        self,
        interaction: discord.Interaction,
        candidate_id: int,
    ) -> None:
        modal = FAQRejectModal(candidate_id)
        await interaction.response.send_modal(modal)
        await modal.wait()
        if not modal.reason:
            return
        try:
            await reject_faq_candidate(
                candidate_id,
                actor_id=interaction.user.id,
                reason=modal.reason,
            )
        except Exception as error:
            log.exception("Failed to reject FAQ candidate %s", candidate_id)
            return await interaction.followup.send(
                f"Failed to reject FAQ candidate: {error}",
                ephemeral=True,
            )

        await self._edit_interaction_message(interaction, candidate_id)
        await interaction.followup.send(
            f"FAQ candidate #{candidate_id} rejected.",
            ephemeral=True,
        )

    async def _handle_modify(
        self,
        interaction: discord.Interaction,
        candidate_id: int,
    ) -> None:
        candidate = await get_faq_review_candidate(candidate_id)
        if candidate is None:
            return await interaction.response.send_message(
                "FAQ candidate not found.",
                ephemeral=True,
            )
        if candidate.status != "pending":
            return await interaction.response.send_message(
                f"FAQ candidate is already {candidate.status}.",
                ephemeral=True,
            )

        modal = FAQModifyModal(candidate)
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal.result is None:
            return
        try:
            result = await approve_faq_candidate(
                candidate_id,
                actor_id=interaction.user.id,
                embedding_provider=embed_texts,
                embedding_model=self.bot.settings.openai_embedding_model,
                overrides=modal.result,
            )
        except Exception as error:
            log.exception("Failed to modify and approve FAQ candidate %s", candidate_id)
            return await interaction.followup.send(
                f"Failed to approve modified FAQ candidate: {error}",
                ephemeral=True,
            )

        await self._edit_interaction_message(interaction, candidate_id)
        await interaction.followup.send(
            f"Modified FAQ candidate #{candidate_id} approved as FAQ #{result.faq_id}.",
            ephemeral=True,
        )


def setup(bot: discord.Bot):
    bot.add_cog(FAQReviewCog(bot))
