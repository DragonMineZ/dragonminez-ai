import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from bulmaai.config import load_settings

settings = load_settings()


DEFAULT_MESSAGE_PRESETS: dict[str, Any] = {
    "rules": {
        "en": {
            "title": "DragonMine Z - English 🇺🇸",
            "sections": [
                {
                    "title": None,
                    "content": (
                        "Welcome to the official DragonMineZ Discord server.\n"
                        "Below you will find our detailed server rules and moderation guidelines.\n"
                        "Some actions are not listed here because they fall under basic common sense and respectful behavior.\n\n"
                        "For most offenses, we follow these steps unless the situation requires a stricter or lighter action:\n"
                        "● 1st Offense > Warning\n"
                        "● 2nd Offense > Timeout/Kick\n"
                        "● 3rd Offense > Permanent Ban\n\n"
                        "We also follow Discord's [Terms of Service](https://discord.com/terms), including, but not limited to:\n"
                        "● No piracy\n\n"
                        "As of February 4, 2025, we also follow our "
                        "[Code of Conduct](https://github.com/DragonMineZ/dragonminez/blob/main/.github/CODE_OF_CONDUCT.md), "
                        "which is available in our GitHub repository.\n\n"
                        "Updated: 3/23/2026"
                    ),
                },
                {
                    "title": "1. Advertising is not allowed",
                    "content": (
                        "Please do not promote external products, services, servers, or communities in this server. "
                        "This rule does not apply if you have staff permission or if you are sharing a DragonMineZ community."
                    ),
                },
                {
                    "title": "2. Non-educational NSFW content is forbidden",
                    "content": (
                        "To keep this server safe and welcoming, we strictly prohibit content that is not safe for work (NSFW). "
                        "This includes, but is not limited to, sexual or graphic violent content, non-academic or excessive discussion "
                        "of sexual acts, and explicit verbal or visual depictions of sexual acts, sex organs, nudity, or similar material."
                    ),
                },
                {
                    "title": "3. Respect channel topics",
                    "content": (
                        "Each channel has a specific purpose, and we ask everyone to stay on topic. This rule is especially important in tickets, "
                        "bug reports, suggestions, and game-related channels. The general channel is exempt so members have more room for open conversation."
                    ),
                },
                {
                    "title": "4. Use common sense",
                    "content": (
                        "Be respectful, considerate, and mindful of how you interact with others. If something seems inappropriate, disruptive, or harmful, do not post it."
                    ),
                },
            ],
        },
        "es": {
            "title": "DragonMine Z - Español 🇪🇸",
            "sections": [
                {
                    "title": None,
                    "content": (
                        "Bienvenido al servidor oficial de DragonMineZ en Discord.\n"
                        "A continuación encontrarás nuestras reglas detalladas del servidor y nuestras directrices de moderación.\n"
                        "Algunas acciones no están incluidas aquí porque forman parte del sentido común y de una conducta respetuosa.\n\n"
                        "Para la mayoría de las infracciones, seguimos estos pasos, a menos que la situación requiera una acción más estricta o más leve:\n"
                        "● 1ra Infracción > Advertencia\n"
                        "● 2da Infracción > Tiempo de espera/Expulsión\n"
                        "● 3ra Infracción > Ban permanente\n\n"
                        "También cumplimos con los [Términos de Servicio](https://discord.com/terms) de Discord, incluidos, entre otros:\n"
                        "● No se permite la piratería\n\n"
                        "Desde el 4 de febrero de 2025, también cumplimos con nuestro "
                        "[Código de Conducta](https://github.com/DragonMineZ/dragonminez/blob/main/.github/CODE_OF_CONDUCT.md), "
                        "disponible en nuestro repositorio de GitHub.\n\n"
                        "Actualizado: 23/03/2026"
                    ),
                },
                {
                    "title": "1. No se permite la publicidad",
                    "content": (
                        "Por favor, no promociones productos, servicios, servidores o comunidades externas en este servidor. "
                        "Esta regla no se aplica si tienes permiso del equipo o si estás compartiendo una comunidad de DragonMineZ."
                    ),
                },
                {
                    "title": "2. El contenido NSFW no educativo está prohibido",
                    "content": (
                        "Para mantener este servidor seguro y acogedor, prohibimos estrictamente el contenido no apto para el trabajo (NSFW). "
                        "Esto incluye, entre otros, contenido sexual o violencia gráfica, discusiones no académicas o excesivas sobre actos sexuales, "
                        "y descripciones verbales o visuales explícitas de actos sexuales, órganos sexuales, desnudez o material similar."
                    ),
                },
                {
                    "title": "3. Respeta el tema de cada canal",
                    "content": (
                        "Cada canal tiene un propósito específico, y pedimos a todos que mantengan las conversaciones dentro del tema. "
                        "Esta regla es especialmente importante en tickets, reportes de errores, sugerencias y canales relacionados con el juego. "
                        "El canal general es una excepción para permitir conversaciones más abiertas."
                    ),
                },
                {
                    "title": "4. Usa el sentido común",
                    "content": (
                        "Sé respetuoso, considerado y consciente de la forma en que interactúas con los demás. Si algo parece inapropiado, perjudicial o disruptivo, no lo publiques."
                    ),
                },
            ],
        },
        "pt": {
            "title": "DragonMine Z - Português 🇧🇷",
            "sections": [
                {
                    "title": None,
                    "content": (
                        "Bem-vindo ao servidor oficial do DragonMineZ no Discord.\n"
                        "Abaixo você encontrará nossas regras detalhadas e diretrizes de moderação.\n"
                        "Algumas ações não estão listadas aqui porque fazem parte do bom senso e de um comportamento respeitoso.\n\n"
                        "Para a maioria das infrações, seguimos estas etapas, a menos que a situação exija uma ação mais rígida ou mais leve:\n"
                        "● 1ª Infração > Aviso\n"
                        "● 2ª Infração > Timeout/Expulsão\n"
                        "● 3ª Infração > Banimento Permanente\n\n"
                        "Também seguimos os [Termos de Serviço](https://discord.com/terms) do Discord, incluindo, entre outros:\n"
                        "● Proibição de pirataria\n\n"
                        "Desde 4 de fevereiro de 2025, também seguimos nosso "
                        "[Código de Conduta](https://github.com/DragonMineZ/dragonminez/blob/main/.github/CODE_OF_CONDUCT.md), "
                        "disponível em nosso repositório no GitHub.\n\n"
                        "Atualizado em: 23/03/2026"
                    ),
                },
                {
                    "title": "1. Propaganda não é permitida",
                    "content": (
                        "Por favor, não divulgue produtos, serviços, servidores ou comunidades externas neste servidor. "
                        "Esta regra não se aplica se você tiver permissão da equipe ou se estiver compartilhando uma comunidade do DragonMineZ."
                    ),
                },
                {
                    "title": "2. Conteúdo NSFW não educacional é proibido",
                    "content": (
                        "Para manter este servidor seguro e acolhedor, proibimos estritamente qualquer conteúdo impróprio para o trabalho (NSFW). "
                        "Isso inclui, entre outros, conteúdo sexual ou violência gráfica, discussões não acadêmicas ou excessivas sobre atos sexuais, "
                        "e descrições verbais ou visuais explícitas de atos sexuais, órgãos sexuais, nudez ou material semelhante."
                    ),
                },
                {
                    "title": "3. Respeite o tema de cada canal",
                    "content": (
                        "Cada canal tem um propósito específico, e pedimos que todos mantenham as conversas dentro do tema. "
                        "Esta regra é especialmente importante em tickets, relatos de bugs, sugestões e canais relacionados ao jogo. "
                        "O canal geral é uma exceção para permitir conversas mais abertas."
                    ),
                },
                {
                    "title": "4. Use o bom senso",
                    "content": (
                        "Seja respeitoso, atencioso e cuidadoso na forma como interage com os outros. Se algo parecer inadequado, prejudicial ou causar confusão, não publique."
                    ),
                },
            ],
        },
    },
    "support": {
        "en": {
            "flag": "🇺🇸",
            "title": "English",
            "description": (
                "**Welcome to DragonMineZ!** 🐉\n\n"
                "We're excited about DragonMineZ and we'd love your support to make it even better. "
                "The easiest way to help is to check our CONTRIBUTING guide on GitHub for more detailed information.\n\n"
                "You can also join us on [Patreon](https://www.patreon.com/DragonMineZ)! You will receive benefits like:"
            ),
            "perks_title": "🔥 Get Exclusive Perks",
            "perks_value": "Early access to new features, sneak peeks of development builds, and behind-the-scenes updates.",
            "development_title": "🎆 More Development",
            "development_value": "Your contributions help Yuseix keep building, designing, and improving the mod.",
            "credits_title": "🎉 Join the Credits",
            "credits_value": "Gain a unique **Supporter Role** in our Discord server and appear in the credits.",
            "community_title": "We're always thrilled to collaborate with the community!",
            "community_value": (
                "You can get involved via new feature ideas, reporting bugs, or collaborating with us. "
                "Every form of support, whether through Patreon or contributing to the project, helps keep the mod alive. "
                "Thank you for reading this too."
            ),
            "boosting_title": "🚀 Server Boosting Rewards",
            "boosting_description": (
                "Love DragonMineZ? Show your support by boosting the server and unlock exclusive roles.\n"
                "Choose your level and shine across the community."
            ),
            "boost_tier1_title": "<a:nitro_slide:1475248617416691874> 1× Boost — Supporter",
            "boost_tier1_value": "Claim the **Supporter** role and its perks — your badge of pride in the community.",
            "boost_tier2_title": "<a:boostgem3:1475248651658854654> 2× Boosts — Contributor",
            "boost_tier2_value": "Step up to **Contributor** for extra flair and a louder presence in the server.",
            "boost_tier3_title": "<a:boostgem9:1475248556792221898> 4× Boosts — Benefactor",
            "boost_tier3_value": "Ascend to **Benefactor** — the highest honor for legendary supporters.",
            "boosting_footer": "Every boost keeps the server thriving. Thank you for your support.",
            "patreon_label": "Become a Patron",
            "github_label": "GitHub Repository",
        },
        "es": {
            "flag": "🇪🇸",
            "title": "Español",
            "description": (
                "**¡Bienvenidx a DragonMineZ!** 🐉\n\n"
                "Estamos muy ilusionados con el desarrollo de DragonMineZ y nos encantaría contar con tu apoyo para hacerlo aún mejor. "
                "La forma más fácil de ayudar es revisar nuestra guía CONTRIBUTING en GitHub para ver más detalles.\n\n"
                "También puedes unirte a nosotros en [Patreon](https://www.patreon.com/DragonMineZ). Recibirás beneficios como:"
            ),
            "perks_title": "🔥 Beneficios Exclusivos",
            "perks_value": "Acceso anticipado a nuevas funciones, avances de las versiones de desarrollo y actualizaciones entre bastidores.",
            "development_title": "🎆 Más Desarrollo",
            "development_value": "Tus contribuciones ayudan a Yuseix a seguir programando, diseñando y mejorando el mod.",
            "credits_title": "🎉 Entra en los Créditos",
            "credits_value": "Consigue un **rol de Supporter** único en nuestro Discord y aparece en los créditos.",
            "community_title": "Siempre nos emociona colaborar con la comunidad.",
            "community_value": (
                "Puedes participar proponiendo nuevas funciones, reportando errores o colaborando con nosotros. "
                "Toda forma de apoyo, ya sea a través de Patreon o contribuyendo al proyecto, ayuda a mantener vivo el mod. "
                "Gracias también por leer esto."
            ),
            "boosting_title": "🚀 Recompensas por Impulsar el Servidor",
            "boosting_description": (
                "¿Te encanta DragonMineZ? Muestra tu apoyo impulsando el servidor y desbloquea roles exclusivos.\n"
                "Elige tu nivel y destaca en la comunidad."
            ),
            "boost_tier1_title": "<a:nitro_slide:1475248617416691874> 1× Boost — Supporter",
            "boost_tier1_value": "Obtén el rol **Supporter** con sus beneficios: tu sello de orgullo en la comunidad.",
            "boost_tier2_title": "<a:boostgem3:1475248651658854654> 2× Boosts — Contributor",
            "boost_tier2_value": "Sube a **Contributor** para más estilo y una presencia aún más fuerte en el servidor.",
            "boost_tier3_title": "<a:boostgem9:1475248556792221898> 4× Boosts — Benefactor",
            "boost_tier3_value": "Alcanza **Benefactor**, el máximo honor para quienes apoyan de verdad.",
            "boosting_footer": "Cada boost mantiene el servidor vivo. Gracias por tu apoyo.",
            "patreon_label": "Hazte Patron",
            "github_label": "Repositorio de GitHub",
        },
        "pt": {
            "flag": "🇧🇷",
            "title": "Português",
            "description": (
                "**Bem-vindo(a) ao DragonMineZ!** 🐉\n\n"
                "Estamos muito empolgados com o desenvolvimento do DragonMineZ e adoraríamos contar com o seu apoio para torná-lo ainda melhor. "
                "A maneira mais fácil de ajudar é conferir nossa guia CONTRIBUTING no GitHub para mais detalhes.\n\n"
                "Você também pode se juntar a nós no [Patreon](https://www.patreon.com/DragonMineZ). Você receberá benefícios como:"
            ),
            "perks_title": "🔥 Benefícios Exclusivos",
            "perks_value": "Acesso antecipado a novos recursos, prévias de versões de desenvolvimento e atualizações dos bastidores.",
            "development_title": "🎆 Mais Desenvolvimento",
            "development_value": "Suas contribuições ajudam Yuseix a continuar programando, criando e melhorando o mod.",
            "credits_title": "🎉 Entre nos Créditos",
            "credits_value": "Ganhe um **cargo de Supporter** único no nosso Discord e apareça nos créditos.",
            "community_title": "Estamos sempre animados em colaborar com a comunidade.",
            "community_value": (
                "Você pode participar sugerindo novos recursos, reportando bugs ou colaborando conosco. "
                "Toda forma de apoio, seja pelo Patreon ou contribuindo com o projeto, ajuda a manter o mod vivo. "
                "Obrigado por ler isso também."
            ),
            "boosting_title": "🚀 Recompensas por Impulsionar o Servidor",
            "boosting_description": (
                "Ama o DragonMineZ? Mostre seu apoio impulsionando o servidor e desbloqueie cargos exclusivos.\n"
                "Escolha seu nível e brilhe na comunidade."
            ),
            "boost_tier1_title": "<a:nitro_slide:1475248617416691874> 1× Boost — Supporter",
            "boost_tier1_value": "Garanta o cargo **Supporter** com benefícios exclusivos — seu selo de orgulho na comunidade.",
            "boost_tier2_title": "<a:boostgem3:1475248651658854654> 2× Boosts — Contributor",
            "boost_tier2_value": "Suba para **Contributor** e ganhe mais destaque e presença no servidor.",
            "boost_tier3_title": "<a:boostgem9:1475248556792221898> 4× Boosts — Benefactor",
            "boost_tier3_value": "Alcance **Benefactor** — o maior reconhecimento para apoiadores lendários.",
            "boosting_footer": "Cada boost fortalece o servidor. Obrigado pelo seu apoio.",
            "patreon_label": "Torne-se um Patron",
            "github_label": "Repositório do GitHub",
        },
    },
}


def _presets_path() -> Path:
    configured = Path(settings.message_presets_path)
    if configured.is_absolute():
        return configured
    return Path(__file__).resolve().parents[3] / configured


def _deep_merge(defaults: Any, current: Any) -> Any:
    if isinstance(defaults, dict) and isinstance(current, dict):
        merged: dict[str, Any] = {}
        for key, default_value in defaults.items():
            if key in current:
                merged[key] = _deep_merge(default_value, current[key])
            else:
                merged[key] = deepcopy(default_value)
        for key, value in current.items():
            if key not in merged:
                merged[key] = deepcopy(value)
        return merged
    return deepcopy(current if current is not None else defaults)


def load_message_presets() -> dict[str, Any]:
    path = _presets_path()
    if not path.exists():
        return deepcopy(DEFAULT_MESSAGE_PRESETS)

    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return deepcopy(DEFAULT_MESSAGE_PRESETS)

    return _deep_merge(DEFAULT_MESSAGE_PRESETS, current)


def save_message_presets(presets: dict[str, Any]) -> None:
    path = _presets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(presets, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_message_presets_file() -> dict[str, Any]:
    presets = load_message_presets()
    save_message_presets(presets)
    return presets


def get_rules_content() -> dict[str, Any]:
    return load_message_presets()["rules"]


def get_support_content() -> dict[str, Any]:
    return load_message_presets()["support"]


def update_rules_section(language: str, section_index: int, *, title: str | None, content: str) -> dict[str, Any]:
    presets = load_message_presets()
    rules = presets["rules"].setdefault(language, deepcopy(DEFAULT_MESSAGE_PRESETS["rules"]["en"]))
    if section_index < 0 or section_index >= len(rules["sections"]):
        raise IndexError("Invalid rules section index")
    rules["sections"][section_index] = {
        "title": title if title else None,
        "content": content,
    }
    save_message_presets(presets)
    return rules


def update_support_field(language: str, field: str, value: str) -> dict[str, Any]:
    presets = load_message_presets()
    support = presets["support"].setdefault(language, deepcopy(DEFAULT_MESSAGE_PRESETS["support"]["en"]))
    if field not in support:
        raise KeyError(field)
    support[field] = value
    save_message_presets(presets)
    return support
