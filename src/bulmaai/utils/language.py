import re
import unicodedata
from typing import Literal

LangCode = Literal["en", "es", "pt"]

LANGUAGE_MARKERS: dict[LangCode, set[str]] = {
    "pt": {
        "nao", "voce", "esta", "sao", "tambem", "muito", "porque", "obrigado",
        "obrigada", "entao", "isso", "assim", "aqui", "ainda", "pode", "fazer",
        "tenho", "meu", "minha", "seu", "sua", "como", "quando", "onde", "qual",
        "oi", "ola", "tudo", "bom", "boa", "dia", "noite", "tarde", "por", "favor",
        "ajuda", "preciso", "quero", "problema", "funciona", "funcionando", "erro",
        "jogo", "servidor", "baixar", "instalar", "versao", "atualizacao",
    },
    "es": {
        "esta", "son", "tambien", "mucho", "porque", "gracias", "entonces",
        "esto", "asi", "aqui", "todavia", "puede", "hacer", "tengo", "mi", "tu",
        "su", "como", "cuando", "donde", "cual", "hola", "todo", "buen",
        "buena", "dia", "noche", "tarde", "por", "favor", "ayuda", "necesito",
        "quiero", "problema", "funciona", "funcionando", "error", "juego",
        "servidor", "descargar", "instalar", "version", "actualizacion", "que",
    },
    "en": {
        "the", "is", "are", "was", "were", "have", "has", "been", "being", "do",
        "does", "did", "will", "would", "could", "should", "can", "may", "might",
        "must", "shall", "this", "that", "these", "those", "what", "which", "who",
        "how", "why", "when", "where", "hello", "hi", "thanks", "thank", "please",
        "help", "need", "want", "problem", "issue", "work", "working", "error",
        "game", "server", "download", "install", "version", "update", "crash",
    },
}

LANGUAGE_HINTS = {
    "pt": "\u00e7\u00e3\u00f5\u00e1\u00e9\u00ed\u00f3\u00fa\u00e2\u00ea\u00f4\u00e0",
    "es": "\u00f1\u00bf\u00a1",
}


def _normalize_text(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def detect_language_from_text(text: str) -> LangCode:
    if not text or len(text.strip()) < 3:
        return "en"

    text_lower = text.lower()
    words = set(re.findall(r"[a-z0-9_]{2,}", _normalize_text(text)))

    pt_score = len(words & LANGUAGE_MARKERS["pt"])
    es_score = len(words & LANGUAGE_MARKERS["es"])
    en_score = len(words & LANGUAGE_MARKERS["en"])

    if any(char in text_lower for char in LANGUAGE_HINTS["pt"]):
        pt_score += 4
    if any(char in text_lower for char in LANGUAGE_HINTS["es"]):
        es_score += 4

    if pt_score > es_score and pt_score > en_score:
        return "pt"
    if es_score > en_score and es_score > pt_score:
        return "es"
    return "en"
