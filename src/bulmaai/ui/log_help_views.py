import discord
from typing import TypedDict


class LogHelpLanguageData(TypedDict):
    flag: str
    title: str
    description: str
    windows_title: str
    windows_value: str
    mac_title: str
    mac_value: str
    linux_title: str
    linux_value: str
    server_title: str
    server_value: str
    footer: str


LOG_HELP_CONTENT: dict[str, LogHelpLanguageData] = {
    "en": {
        "flag": "🇺🇸",
        "title": "Finding Your Logs",
        "description": (
            "Need help locating your logs? Grab `latest.log` or a `crash-report.txt` and share it with us.\n"
            "Here are the most common locations by platform:"
        ),
        "windows_title": "🪟 Windows (Client)",
        "windows_value": (
            "`%APPDATA%\\.minecraft\\logs\\latest.log`\n"
            "`%APPDATA%\\.minecraft\\crash-reports\\crash-YYYY-MM-DD_XX.XX.XX-client.txt`"
        ),
        "mac_title": "🍎 macOS (Client)",
        "mac_value": (
            "`~/Library/Application Support/minecraft/logs/latest.log`\n"
            "`~/Library/Application Support/minecraft/crash-reports/`"
        ),
        "linux_title": "🐧 Linux (Client)",
        "linux_value": (
            "`~/.minecraft/logs/latest.log`\n"
            "`~/.minecraft/crash-reports/`"
        ),
        "server_title": "🖥️ Dedicated Server",
        "server_value": (
            "Inside your server folder:\n"
            "`logs/latest.log`\n"
            "`crash-reports/`"
        ),
        "footer": "Tip: If the file is big, zip it before uploading."
    },
    "es": {
        "flag": "🇪🇸",
        "title": "Cómo encontrar tus logs",
        "description": (
            "¿Necesitas ayuda para encontrar tus logs? Busca `latest.log` o un `crash-report.txt` y compártelo.\n"
            "Estas son las ubicaciones más comunes por plataforma:"
        ),
        "windows_title": "🪟 Windows (Cliente)",
        "windows_value": (
            "`%APPDATA%\\.minecraft\\logs\\latest.log`\n"
            "`%APPDATA%\\.minecraft\\crash-reports\\crash-YYYY-MM-DD_XX.XX.XX-client.txt`"
        ),
        "mac_title": "🍎 macOS (Cliente)",
        "mac_value": (
            "`~/Library/Application Support/minecraft/logs/latest.log`\n"
            "`~/Library/Application Support/minecraft/crash-reports/`"
        ),
        "linux_title": "🐧 Linux (Cliente)",
        "linux_value": (
            "`~/.minecraft/logs/latest.log`\n"
            "`~/.minecraft/crash-reports/`"
        ),
        "server_title": "🖥️ Servidor dedicado",
        "server_value": (
            "Dentro de la carpeta del servidor:\n"
            "`logs/latest.log`\n"
            "`crash-reports/`"
        ),
        "footer": "Tip: Si el archivo es grande, comprímelo antes de subirlo."
    },
    "pt": {
        "flag": "🇧🇷",
        "title": "Como encontrar seus logs",
        "description": (
            "Precisa de ajuda para achar seus logs? Pegue o `latest.log` ou um `crash-report.txt` e envie pra gente.\n"
            "Aqui estao os caminhos mais comuns por plataforma:"
        ),
        "windows_title": "🪟 Windows (Cliente)",
        "windows_value": (
            "`%APPDATA%\\.minecraft\\logs\\latest.log`\n"
            "`%APPDATA%\\.minecraft\\crash-reports\\crash-YYYY-MM-DD_XX.XX.XX-client.txt`"
        ),
        "mac_title": "🍎 macOS (Cliente)",
        "mac_value": (
            "`~/Library/Application Support/minecraft/logs/latest.log`\n"
            "`~/Library/Application Support/minecraft/crash-reports/`"
        ),
        "linux_title": "🐧 Linux (Cliente)",
        "linux_value": (
            "`~/.minecraft/logs/latest.log`\n"
            "`~/.minecraft/crash-reports/`"
        ),
        "server_title": "🖥️ Servidor dedicado",
        "server_value": (
            "Dentro da pasta do servidor:\n"
            "`logs/latest.log`\n"
            "`crash-reports/`"
        ),
        "footer": "Dica: Se o arquivo for grande, compacte antes de enviar."
    },
}


def build_log_help_embeds(language: str = "en") -> list[discord.Embed]:
    data = LOG_HELP_CONTENT.get(language, LOG_HELP_CONTENT["en"])

    embed = discord.Embed(color=discord.Color.from_rgb(88, 101, 242))
    embed.title = f"{data['flag']} {data['title']}"
    embed.description = data["description"]

    embed.add_field(name=data["windows_title"], value=data["windows_value"], inline=False)
    embed.add_field(name=data["mac_title"], value=data["mac_value"], inline=False)
    embed.add_field(name=data["linux_title"], value=data["linux_value"], inline=False)
    embed.add_field(name=data["server_title"], value=data["server_value"], inline=False)

    embed.set_footer(text=data["footer"])

    return [embed]
