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
Below you will find the list of rules we have, being more expanded than in the Onboarding feature.
Some actions may not be listed, as they are taken to be part of a person's common sense, and there is no need to be mentioned.

Offenses guidelines:
● 1st Offense > Warning
● 2nd Offense > Mute/Kick/Soft-ban
● 3rd Offense > Ban

We also comply with Discord's [ToS](https://discord.com/terms)!

As of February 4, 2025, we fully comply with our [Code of Conduct](https://github.com/DragonMineZ/.github/blob/main/CODE_OF_CONDUCT.md) located in our GitHub Repository.

Updated: 2/4/2025"""
            },
            {
                "title": "1. Advertisement is not allowed",
                "content": "We kindly request that you refrain from promoting external products, services, or communities within this server. Let's keep the focus on our shared interests!"
            },
            {
                "title": "2. No NSFW content is permitted",
                "content": "To maintain a safe and welcoming environment, we strictly prohibit any content that is not safe for work (NSFW). Violating this rule will result in an immediate ban."
            },
            {
                "title": "3. Respect the Channel Topics!",
                "content": "Each channel has a specific purpose, and we appreciate it when members stay on topic. Let's make sure our discussions align with the designated channels."
            },
            {
                "title": "4. Don't be a d*ck, use common sense",
                "content": "Sometimes, all it takes is a little common sense. Be considerate, friendly, and mindful of your interactions."
            },
        ]
    },
    "es": {
        "title": "DragonMine Z - Spanish 🇪🇸",
        "sections": [
            {
                "title": None,
                "content": """Bienvenido/a al servidor oficial de Discord de DragonMine Z.
A continuación encontrarás una lista de nuestras reglas, más detallada que en la función de bienvenida.
Algunas acciones no están listadas, ya que se considera que son parte del sentido común y no necesitan ser mencionadas.

Guía de Sanciones:
● Primera infracción > Advertencia
● Segunda infracción > Silencio/Expulsión/Baneo leve
● Tercera infracción > Baneo

¡También cumplimos con los [Términos de Servicio](https://discord.com/terms) de Discord!

A partir del 4 de febrero de 2025, cumplimos plenamente con nuestro [Código de Conducta](https://github.com/DragonMineZ/.github/blob/main/CODE_OF_CONDUCT.md) ubicado en nuestro Repositorio de GitHub.

Actualizado: 4/2/2025"""
            },
            {
                "title": "1. La publicidad no está permitida",
                "content": "Te pedimos amablemente que te abstengas de promocionar productos externos, servicios o comunidades dentro de este servidor. ¡Mantengamos el enfoque en nuestros intereses compartidos!"
            },
            {
                "title": "2. No se permite contenido NSFW",
                "content": "Para mantener un ambiente seguro y acogedor, prohibimos estrictamente cualquier contenido que no sea apto para el trabajo (NSFW). Violar esta regla resultará en un baneo inmediato."
            },
            {
                "title": "3. ¡Respeta los temas de los canales!",
                "content": "Cada canal tiene un propósito específico, y apreciamos cuando los miembros se mantienen en el tema. Asegurémonos de que nuestras discusiones se alineen con los canales designados."
            },
            {
                "title": "4. No seas un idiota, usa el sentido común",
                "content": "A veces, todo lo que se necesita es un poco de sentido común. Sé considerado, amigable y consciente de tus interacciones."
            },
        ]
    },
    "pt": {
        "title": "DragonMine Z - Português 🇧🇷",
        "sections": [
            {
                "title": None,
                "content": """Bem-vindo(a) ao servidor oficial do Discord de DragonMine Z.
Abaixo você encontrará a lista de regras que temos, sendo mais expandida do que na funcionalidade de Boas-vindas.
Algumas ações podem não estar listadas, pois são consideradas parte do senso comum de uma pessoa, e não há necessidade de serem mencionadas.

Diretrizes de Penalidades:
● 1ª Infração > Aviso
● 2ª Infração > Mute/Kick/Soft-ban
● 3ª Infração > Ban

Também cumprimos os [Termos de Serviço](https://discord.com/terms) do Discord!

A partir de 4 de fevereiro de 2025, cumprimos totalmente nosso [Código de Conduta](https://github.com/DragonMineZ/.github/blob/main/CODE_OF_CONDUCT.md) localizado em nosso Repositório no GitHub.

Atualizado: 04/02/2025"""
            },
            {
                "title": "1. Publicidade não é permitida",
                "content": "Pedimos gentilmente que você se abstenha de promover produtos externos, serviços ou comunidades dentro deste servidor. Vamos manter o foco em nossos interesses compartilhados!"
            },
            {
                "title": "2. Conteúdo NSFW não é permitido",
                "content": "Para manter um ambiente seguro e acolhedor, proibimos estritamente qualquer conteúdo que não seja adequado para o trabalho (NSFW). Violar esta regra resultará em banimento imediato."
            },
            {
                "title": "3. Respeite os tópicos dos canais!",
                "content": "Cada canal tem um propósito específico, e apreciamos quando os membros permanecem no tópico. Vamos garantir que nossas discussões estejam alinhadas com os canais designados."
            },
            {
                "title": "4. Não seja um idiota, use o bom senso",
                "content": "Às vezes, tudo o que é preciso é um pouco de bom senso. Seja atencioso, amigável e consciente de suas interações."
            },
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

