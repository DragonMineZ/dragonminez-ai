import discord
from typing import TypedDict


PATREON_URL = "https://www.patreon.com/DragonMineZ"
GITHUB_URL = "https://github.com/DragonMineZ"


class SupportLanguageData(TypedDict):
    flag: str
    title: str
    description: str
    perks_title: str
    perks_value: str
    development_title: str
    development_value: str
    credits_title: str
    credits_value: str
    community_title: str
    community_value: str
    boosting_title: str
    boosting_description: str
    boost_tier1_title: str
    boost_tier1_value: str
    boost_tier2_title: str
    boost_tier2_value: str
    boost_tier3_title: str
    boost_tier3_value: str
    boosting_footer: str
    patreon_label: str
    github_label: str


SUPPORT_CONTENT: dict[str, SupportLanguageData] = {
    "en": {
        "flag": "🇺🇸",
        "title": "English",
        "description": (
            "**Welcome to DragonMineZ!** 🐉\n\n"
            "We're so excited about developing DragonMineZ, and we'd love your support to make it even better! "
            "The easiest way to help? Check out our CONTRIBUTING.md in our GitHub for more detailed information.\n\n"
            "You can also join us on [Patreon](https://www.patreon.com/DragonMineZ)! You will receive benefits like:"
        ),
        "perks_title": "🔥 Get Exclusive Perks",
        "perks_value": "Early access to new features, sneak peeks of development builds, and behind-the-scenes updates.",
        "development_title": "🎆 More Development",
        "development_value": "Your contributions will make Yuseix gain a life purpose at coding, design, and keep improving the mod.",
        "credits_title": "🎉 Join the Credits",
        "credits_value": "Gain a unique **Supporter Role** in our Discord server and appear in the credits!",
        "community_title": "We're always thrilled to collaborate with the community!",
        "community_value": (
            "You can get involved via introducing new features, reporting errors (bugs), or engaging with us. "
            "Every form of support, whether through Patreon or contributing to the project, helps keep our mod alive. "
            "Thank you for reading this, too! (It already means a lot.)"
        ),
        "boosting_title": "🚀 Server Boosting Rewards",
        "boosting_description": (
            "Love DragonMineZ? Show your support by boosting the server and unlock exclusive roles!\n"
        ),
        "boost_tier1_title": "<a:nitro_slide:1475248617416691874> 1× Boost — Supporter",
        "boost_tier1_value": "Unlock the **Supporter** role! Get recognized as a valued member of our community with exclusive perks.",
        "boost_tier2_title": "<a:boostgem3:1475248651658854654> 3× Boosts — Contributor",
        "boost_tier2_value": "Level up to the **Contributor** role! Stand out even more and gain access to additional benefits.",
        "boost_tier3_title": "<a:boostgem9:1475248556792221898> 4× Boosts — Benefactor",
        "boost_tier3_value": "Achieve the prestigious **Benefactor** role! The ultimate recognition for your incredible generosity.",
        "boosting_footer": "Every boost helps keep the server running strong. Thank you for your support! 💜",
        "patreon_label": "Become a Patron",
        "github_label": "GitHub Repository",
    },
    "es": {
        "flag": "🇪🇸",
        "title": "Español",
        "description": (
            "**¡Bienvenidx a DragonMineZ!** 🐉\n\n"
            "Estamos muy ilusionados con el desarrollo de DragonMineZ, ¡y nos encantaría contar con tu apoyo para hacerlo aún mejor! "
            "¿La forma más fácil de ayudar? Echa un vistazo a nuestro CONTRIBUTING.md en nuestro GitHub para obtener información más detallada.\n\n"
            "¡También puedes unirte a nosotros en [Patreon](https://www.patreon.com/DragonMineZ)! Recibirás beneficios como:"
        ),
        "perks_title": "🔥 Recibe Beneficios!",
        "perks_value": "Acceso anticipado a nuevas funciones, avances de las versiones de desarrollo y actualizaciones entre bastidores.",
        "development_title": "🎆 Más Desarrollo",
        "development_value": "Tus contribuciones harán que Yuseix adquiera un propósito de vida en la codificación, el diseño y mejore el mod.",
        "credits_title": "🎉 Únete a los Créds.",
        "credits_value": "¡Gana un **Rol de Beneficiador** único en nuestro servidor Discord y aparece en los créditos!",
        "community_title": "¡Estamos emocionados de colaborar con la comunidad!",
        "community_value": (
            "Puedes participar introduciendo nuevas funciones, informando de errores (bugs) o colaborando con nosotros. "
            "Toda forma de apoyo, ya sea a través de Patreon o contribuyendo al proyecto, ayuda a mantener vivo nuestro mod. "
            "¡Gracias también por leer esto! (Ya significa mucho)."
        ),
        "boosting_title": "🚀 Recompensas por Impulsar el Servidor",
        "boosting_description": (
            "¿Te encanta DragonMineZ? ¡Muestra tu apoyo impulsando el servidor y desbloquea roles exclusivos!\n"
        ),
        "boost_tier1_title": "<a:nitro_slide:1475248617416691874> 1× Boost — Supporter",
        "boost_tier1_value": "¡Desbloquea el rol de **Supporter**! Sé reconocido como un miembro valioso de nuestra comunidad con beneficios exclusivos.",
        "boost_tier2_title": "<a:boostgem3:1475248651658854654> 3× Boosts — Contributor",
        "boost_tier2_value": "¡Sube de nivel al rol de **Contributor**! Destaca aún más y obtén acceso a beneficios adicionales.",
        "boost_tier3_title": "<a:boostgem9:1475248556792221898> 4× Boosts — Benefactor",
        "boost_tier3_value": "¡Alcanza el prestigioso rol de **Benefactor**! El máximo reconocimiento por tu increíble generosidad.",
        "boosting_footer": "Cada boost ayuda a mantener el servidor fuerte. ¡Gracias por tu apoyo! 💜",
        "patreon_label": "Vuélvete un Patron",
        "github_label": "Repositorio de GitHub",
    },
    "pt": {
        "flag": "🇧🇷",
        "title": "Português",
        "description": (
            "**Bem-vindo(a) ao DragonMineZ!** 🐉\n\n"
            "Estamos muito empolgados com o desenvolvimento do DragonMineZ e adoraríamos contar com o seu apoio para torná-lo ainda melhor! "
            "A maneira mais fácil de ajudar? Confira nosso CONTRIBUTING.md no nosso GitHub para informações mais detalhadas.\n\n"
            "Você também pode se juntar a nós no [Patreon](https://www.patreon.com/DragonMineZ)! Você receberá benefícios como:"
        ),
        "perks_title": "🔥 Benefícios Exclusivos",
        "perks_value": "Acesso antecipado a novos recursos, prévias de versões de desenvolvimento e atualizações dos bastidores.",
        "development_title": "🎆 Mais Desenvolvimento",
        "development_value": "Suas contribuições farão Yuseix ganhar um propósito de vida na programação, design e continuar melhorando o mod.",
        "credits_title": "🎉 Entre nos Créditos",
        "credits_value": "Ganhe um **Cargo de Apoiador** único no nosso servidor Discord e apareça nos créditos!",
        "community_title": "Estamos sempre animados em colaborar com a comunidade!",
        "community_value": (
            "Você pode participar introduzindo novos recursos, reportando erros (bugs) ou interagindo conosco. "
            "Toda forma de apoio, seja pelo Patreon ou contribuindo com o projeto, ajuda a manter nosso mod vivo. "
            "Obrigado por ler isso também! (Já significa muito.)"
        ),
        "boosting_title": "🚀 Recompensas por Impulsionar o Servidor",
        "boosting_description": (
            "Ama o DragonMineZ? Mostre seu apoio impulsionando o servidor e desbloqueie roles exclusivos!\n"
        ),
        "boost_tier1_title": "<a:nitro_slide:1475248617416691874> 1× Boost — Supporter",
        "boost_tier1_value": "Desbloqueie o cargo de **Supporter**! Seja reconhecido como um membro valioso da nossa comunidade com benefícios exclusivos.",
        "boost_tier2_title": "<a:boostgem3:1475248651658854654> 3× Boosts — Contributor",
        "boost_tier2_value": "Suba de nível para o cargo de **Contributor**! Destaque-se ainda mais e ganhe acesso a benefícios adicionais.",
        "boost_tier3_title": "<a:boostgem9:1475248556792221898> 4× Boosts — Benefactor",
        "boost_tier3_value": "Alcance o prestigioso cargo de **Benefactor**! O reconhecimento máximo pela sua incrível generosidade.",
        "boosting_footer": "Cada boost ajuda a manter o servidor forte. Obrigado pelo seu apoio! 💜",
        "patreon_label": "Torne-se um Patron",
        "github_label": "Repositório do GitHub",
    },
}


def build_support_embeds(language: str = "en") -> list[discord.Embed]:
    data = SUPPORT_CONTENT.get(language, SUPPORT_CONTENT["en"])

    embed = discord.Embed(
        color=discord.Color.from_rgb(88, 101, 242),
    )

    embed.title = f"{data['flag']} {data['title']}"
    embed.description = data["description"]

    embed.add_field(name=data["perks_title"], value=data["perks_value"], inline=True)
    embed.add_field(name=data["development_title"], value=data["development_value"], inline=True)
    embed.add_field(name=data["credits_title"], value=data["credits_value"], inline=True)

    embed.add_field(
        name=data["community_title"],
        value=data["community_value"],
        inline=False,
    )

    # Boosting rewards embed
    boost_embed = discord.Embed(
        color=discord.Color.from_rgb(244, 127, 255),
    )
    boost_embed.title = data["boosting_title"]
    boost_embed.description = data["boosting_description"]

    boost_embed.add_field(name=data["boost_tier1_title"], value=data["boost_tier1_value"], inline=False)
    boost_embed.add_field(name=data["boost_tier2_title"], value=data["boost_tier2_value"], inline=False)
    boost_embed.add_field(name=data["boost_tier3_title"], value=data["boost_tier3_value"], inline=False)

    boost_embed.set_footer(text=data["boosting_footer"])

    return [embed, boost_embed]


class SupportLinkButtons(discord.ui.View):
    """Link buttons for Patreon and GitHub – shown under each language embed."""

    def __init__(self, language: str = "en"):
        super().__init__(timeout=None)
        data = SUPPORT_CONTENT.get(language, SUPPORT_CONTENT["en"])
        self.add_item(
            discord.ui.Button(
                label=data["patreon_label"],
                style=discord.ButtonStyle.link,
                url=PATREON_URL,
                emoji="🧡",
            )
        )
        self.add_item(
            discord.ui.Button(
                label=data["github_label"],
                style=discord.ButtonStyle.link,
                url=GITHUB_URL,
                emoji="🔗",
            )
        )


class SupportLanguageView(discord.ui.View):
    """Persistent view with language selection buttons."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Español",
        style=discord.ButtonStyle.secondary,
        custom_id="support_lang:es",
        emoji="🇪🇸",
    )
    async def spanish_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        embeds = build_support_embeds("es")
        link_view = SupportLinkButtons("es")
        await interaction.response.send_message(embeds=embeds, view=link_view, ephemeral=True)

    @discord.ui.button(
        label="Português",
        style=discord.ButtonStyle.secondary,
        custom_id="support_lang:pt",
        emoji="🇧🇷",
    )
    async def portuguese_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        embeds = build_support_embeds("pt")
        link_view = SupportLinkButtons("pt")
        await interaction.response.send_message(embeds=embeds, view=link_view, ephemeral=True)

