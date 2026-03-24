import discord
from typing import TypedDict


class RuleSection(TypedDict):
    title: str | None
    content: str


class RulesLanguageData(TypedDict):
    title: str
    sections: list[RuleSection]


RULES_CONTENT: dict[str, RulesLanguageData] = {
    "en": {
        "title": "DragonMine Z - English 🇺🇸",
        "sections": [
            {
                "title": None,
                "content": """Welcome to the official DragonMineZ Discord server.
        Below you will find our detailed server rules and moderation guidelines.
        Some actions are not listed here because they fall under basic common sense and respectful behavior.
        
        For most offenses, we follow these steps unless the situation requires a stricter or lighter action:
        ● 1st Offense > Warning
        ● 2nd Offense > Timeout/Kick
        ● 3rd Offense > Permanent Ban
        
        We also follow Discord's [Terms of Service](https://discord.com/terms), including, but not limited to:
        - No piracy
        
        As of February 4, 2025, we also follow our [Code of Conduct](https://github.com/DragonMineZ/dragonminez/blob/main/.github/CODE_OF_CONDUCT.md), which is available in our GitHub repository.
        
        Updated: 3/23/2026"""
                },
                {
                    "title": "1. Advertising is not allowed",
                    "content": "Please do not promote external products, services, servers, or communities in this server. "
                               "This rule does not apply if you have staff permission or if you are sharing a DragonMineZ community."
                },
                {
                    "title": "2. Non-educational NSFW content is forbidden",
                    "content": "To keep this server safe and welcoming, we strictly prohibit content that is not safe for work (NSFW). "
                               "This includes, but is not limited to, sexual or graphic violent content, non-academic or excessive discussion "
                               "of sexual acts, and explicit verbal or visual depictions of sexual acts, sex organs, nudity, or similar material."
                },
                {
                    "title": "3. Respect channel topics",
                    "content": "Each channel has a specific purpose, and we ask everyone to stay on topic. This rule is especially important in tickets, "
                               "bug reports, suggestions, and game-related channels. The general channel is exempt so members have more room for open conversation."
                },
                {
                    "title": "4. Use common sense",
                    "content": "Be respectful, considerate, and mindful of how you interact with others. If something seems inappropriate, disruptive, or harmful, do not post it."
                }
            ]
        },
    "es": {
        "title": "DragonMine Z - Español 🇪🇸",
        "sections": [
            {
                "title": None,
                "content": """Bienvenido al servidor oficial de DragonMineZ en Discord.
        A continuación encontrarás nuestras reglas detalladas del servidor y nuestras directrices de moderación.
        Algunas acciones no están incluidas aquí porque forman parte del sentido común y de una conducta respetuosa.
        
        Para la mayoría de las infracciones, seguimos estos pasos, a menos que la situación requiera una acción más estricta o más leve:
        ● 1ra Infracción > Advertencia
        ● 2da Infracción > Tiempo de espera/Expulsión
        ● 3ra Infracción > Ban permanente
        
        También cumplimos con los [Términos de Servicio](https://discord.com/terms) de Discord, incluidos, entre otros:
        - No se permite la piratería
        
        Desde el 4 de febrero de 2025, también cumplimos con nuestro [Código de Conducta](https://github.com/DragonMineZ/dragonminez/blob/main/.github/CODE_OF_CONDUCT.md), disponible en nuestro repositorio de GitHub.
        
        Actualizado: 23/03/2026"""
            },
            {
                "title": "1. No se permite la publicidad",
                "content": "Por favor, no promociones productos, servicios, servidores o comunidades externas en este servidor. "
                           "Esta regla no se aplica si tienes permiso del equipo o si estás compartiendo una comunidad de DragonMineZ."
            },
            {
                "title": "2. El contenido NSFW no educativo está prohibido",
                "content": "Para mantener este servidor seguro y acogedor, prohibimos estrictamente el contenido no apto para el trabajo (NSFW). "
                           "Esto incluye, entre otros, contenido sexual o violencia gráfica, discusiones no académicas o excesivas sobre actos sexuales, "
                           "y descripciones verbales o visuales explícitas de actos sexuales, órganos sexuales, desnudez o material similar."
            },
            {
                "title": "3. Respeta el tema de cada canal",
                "content": "Cada canal tiene un propósito específico, y pedimos a todos que mantengan las conversaciones dentro del tema. "
                           "Esta regla es especialmente importante en tickets, reportes de errores, sugerencias y canales relacionados con el juego. "
                           "El canal general es una excepción para permitir conversaciones más abiertas."
            },
            {
                "title": "4. Usa el sentido común",
                "content": "Sé respetuoso, considerado y consciente de la forma en que interactúas con los demás. Si algo parece inapropiado, perjudicial o disruptivo, no lo publiques."
            }
        ]
    },
    "pt": {
        "title": "DragonMine Z - Português 🇧🇷",
        "sections": [
            {
                "title": None,
                "content": """Bem-vindo ao servidor oficial do DragonMineZ no Discord.
        Abaixo você encontrará nossas regras detalhadas e diretrizes de moderação.
        Algumas ações não estão listadas aqui porque fazem parte do bom senso e de um comportamento respeitoso.
        
        Para a maioria das infrações, seguimos estas etapas, a menos que a situação exija uma ação mais rígida ou mais leve:
        ● 1ª Infração > Aviso
        ● 2ª Infração > Timeout/Expulsão
        ● 3ª Infração > Banimento Permanente
        
        Também seguimos os [Termos de Serviço](https://discord.com/terms) do Discord, incluindo, entre outros:
        - Proibição de pirataria
        
        Desde 4 de fevereiro de 2025, também seguimos nosso [Código de Conduta](https://github.com/DragonMineZ/dragonminez/blob/main/.github/CODE_OF_CONDUCT.md), disponível em nosso repositório no GitHub.
        
        Atualizado em: 23/03/2026"""
                },
            {
                "title": "1. Propaganda não é permitida",
                "content": "Por favor, não divulgue produtos, serviços, servidores ou comunidades externas neste servidor. "
                           "Esta regra não se aplica se você tiver permissão da equipe ou se estiver compartilhando uma comunidade do DragonMineZ."
            },
            {
                "title": "2. Conteúdo NSFW não educacional é proibido",
                "content": "Para manter este servidor seguro e acolhedor, proibimos строго qualquer conteúdo impróprio para o trabalho (NSFW). "
                           "Isso inclui, entre outros, conteúdo sexual ou violência gráfica, discussões não acadêmicas ou excessivas sobre atos sexuais, "
                           "e descrições verbais ou visuais explícitas de atos sexuais, órgãos sexuais, nudez ou material semelhante."
            },
            {
                "title": "3. Respeite o tema de cada canal",
                "content": "Cada canal tem um propósito específico, e pedimos que todos mantenham as conversas dentro do tema. "
                           "Esta regra é especialmente importante em tickets, relatos de bugs, sugestões e canais relacionados ao jogo. "
                           "O canal geral é uma exceção para permitir conversas mais abertas."
            },
            {
                "title": "4. Use o bom senso",
                "content": "Seja respeitoso, atencioso e cuidadoso na forma como interage com os outros. Se algo parecer inadequado, prejudicial ou causar confusão, não publique."
            }
        ]
    }
}



def build_rules_embeds(language: str = "en") -> list[discord.Embed]:
    data = RULES_CONTENT.get(language, RULES_CONTENT["en"])
    embeds = []

    colors = [
        discord.Color.from_rgb(88, 101, 242),
        discord.Color.from_rgb(237, 66, 69),
        discord.Color.from_rgb(237, 66, 69),
        discord.Color.from_rgb(237, 66, 69),
        discord.Color.from_rgb(237, 66, 69),
    ]

    for idx, section in enumerate(data["sections"]):
        embed = discord.Embed(color=colors[idx] if idx < len(colors) else discord.Color.blurple())

        if idx == 0:
            embed.title = data["title"]

        if section["title"]:
            embed.add_field(name=section["title"], value=section["content"], inline=False)
        else:
            embed.description = section["content"]

        embeds.append(embed)

    return embeds


class RulesLanguageView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Español",
        style=discord.ButtonStyle.secondary,
        custom_id="rules_lang:es",
        emoji="🇪🇸",
    )
    async def spanish_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        embeds = build_rules_embeds("es")
        await interaction.response.send_message(embeds=embeds, ephemeral=True)

    @discord.ui.button(
        label="Português",
        style=discord.ButtonStyle.secondary,
        custom_id="rules_lang:pt",
        emoji="🇧🇷",
    )
    async def portuguese_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        embeds = build_rules_embeds("pt")
        await interaction.response.send_message(embeds=embeds, ephemeral=True)

