"""Turning a finished Patreon perk into a pull request on the mod repo, the way Yuseix adds them by hand.

Custom clothing (a standard no-helmet armor set, like `scarletblaze`): the two textures, one MainItems line, one
item-model line, three weaponleveling tag entries and three en_us lines. Outfits with their own model need new Java
and stay manual. Hairstyles: one `register(<next id>, "<code>")` line in HairPresets with a comment naming it.

Every edit is plain text inserted next to the newest entry of its kind, keeping the file's own line endings, so the
diff looks like a hand-made one. A person reviews and merges the PR; the bot never merges."""

import json
import re
from dataclasses import dataclass

MAIN_ITEMS = "src/main/java/com/dragonminez/common/init/MainItems.java"
ITEM_MODELS = "src/main/java/com/dragonminez/common/datagen/DMZItemModelProvider.java"
LANG = "src/main/resources/assets/dragonminez/lang/en_us.json"
TAGS = {
    "boots": "src/main/resources/data/weaponleveling/tags/items/boots.json",
    "chestplate": "src/main/resources/data/weaponleveling/tags/items/chestplates.json",
    "leggings": "src/main/resources/data/weaponleveling/tags/items/leggings.json",
}
HAIR_PRESETS = "src/main/java/com/dragonminez/common/hair/HairPresets.java"
ARMOR_TEXTURE = "src/main/resources/assets/dragonminez/textures/armor/{id}_layer{n}.png"

ARMOR_FILES = (MAIN_ITEMS, ITEM_MODELS, LANG, *TAGS.values())
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_TEXTURE_BYTES = 512 * 1024
HAIR_CODE = re.compile(r"^DMZF\d+:[A-Za-z0-9_\-]+$")
MAX_HAIR_CODE = 20_000
# What a name or Minecraft username may hold before it goes into Java comments, JSON and the PR: no backslash (Java
# reads \u000a as a line break even inside a // comment), quotes, backticks, @ or control characters.
SAFE_TEXT = re.compile(r"^[\w .,'&()\-]{1,40}$")
PIECES = ("chestplate", "leggings", "boots")


class ShipError(ValueError):
    """The input or the repo doesn't fit the pattern; the message is shown to staff as is."""


@dataclass(frozen=True, slots=True)
class Change:
    """What one PR changes: text files (new full text) and binary files, plus its title and branch."""

    texts: dict[str, str]
    binaries: dict[str, bytes]
    title: str
    branch: str


def _safe(value: str, what: str) -> str:
    value = value.strip()
    if not SAFE_TEXT.match(value):
        raise ShipError(f"The {what} can only use letters, digits, spaces and . , ' & ( ) - _ (40 at most).")
    return value


def _valid_json(text: str, path: str) -> str:
    try:
        json.loads(text)
    except ValueError:
        raise ShipError(f"The change would break {path.rsplit('/', 1)[-1]}. Add it by hand.") from None
    return text


def item_id(display_name: str) -> str:
    """'Scarlet Blaze' -> 'scarlet_blaze'; the registry and texture name."""
    slug = re.sub(r"[^a-z0-9]+", "_", display_name.casefold()).strip("_")
    if not slug or not slug[0].isalpha() or len(slug) > 40:
        raise ShipError("The name must start with a letter and give an id of at most 40 characters.")
    return slug


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _insert_after_last(text: str, pattern: str, new_lines: list[str], what: str) -> str:
    """new_lines (without indentation) after the last line matching pattern, indented like that line."""
    nl = _newline(text)
    lines = text.split(nl)
    matches = [i for i, line in enumerate(lines) if re.search(pattern, line)]
    if not matches:
        raise ShipError(f"Couldn't find where {what} goes; the file changed shape. Add it by hand.")
    anchor = matches[-1]
    indent = re.match(r"\s*", lines[anchor]).group(0)
    lines[anchor + 1:anchor + 1] = [indent + line for line in new_lines]
    return nl.join(lines)


def _json_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _add_tag_entry(text: str, entry_id: str) -> str:
    """One {"id": ..., "required": false} object at the end of the tag's values array."""
    nl = _newline(text)
    closing = re.search(r"\}(\s*)\]\s*\}\s*$", text)
    if closing is None:
        raise ShipError("A weaponleveling tag file changed shape. Add the armor to it by hand.")
    entry = f",{nl}    {{{nl}      \"id\": \"{entry_id}\",{nl}      \"required\": false{nl}    }}"
    return text[:closing.start() + 1] + entry + text[closing.start() + 1:]


def armor_change(
    files: dict[str, str],
    *,
    display_name: str,
    layer1: bytes,
    layer2: bytes,
    patron: str,
    claim_id: int,
    existing_paths: frozenset[str] = frozenset(),
) -> Change:
    """The PR for a custom clothing perk. files: the current text of ARMOR_FILES on the base branch;
    existing_paths: every file on it, so a name whose textures exist already is refused instead of overwriting them."""
    name, patron = _safe(display_name, "armor name"), _safe(patron, "Minecraft username")
    armor = item_id(name)
    constant = armor.upper() + "_ARMOR"
    for layer in (layer1, layer2):
        if not layer.startswith(PNG_MAGIC):
            raise ShipError("Both textures must be PNG files.")
        if len(layer) > MAX_TEXTURE_BYTES:
            raise ShipError("A texture is over 512 KB; armor textures are a few KB.")
    textures = [ARMOR_TEXTURE.format(id=armor, n=n) for n in (1, 2)]
    taken = any(path in existing_paths for path in textures)
    if taken or f'"{armor}_armor"' in files[MAIN_ITEMS] or f" {constant} " in files[MAIN_ITEMS]:
        raise ShipError(f"`{armor}_armor` already exists in the mod. Pick another name.")
    texts = {
        MAIN_ITEMS: _insert_after_last(
            files[MAIN_ITEMS],
            r"^\s*public static final Map<ArmorItem\.Type, RegistryObject<Item>> \w+ = fullArmorNoHelmetSet\(.*\);\s*$",
            [f"// {name.upper()} (Patreon: {patron})",
             f'public static final Map<ArmorItem.Type, RegistryObject<Item>> {constant} = '
             f'fullArmorNoHelmetSet("{armor}_armor", "{armor}");'],
            "the armor registration",
        ),
        ITEM_MODELS: _insert_after_last(
            files[ITEM_MODELS], r"^\s*generateArmorSetModels\(MainItems\.\w+\);\s*$", [f"generateArmorSetModels(MainItems.{constant});"],
            "the item model",
        ),
        LANG: _insert_after_last(
            files[LANG],
            r'^\s*"item\.dragonminez\.\w+_armor_boots":',
            [f'"item.dragonminez.{armor}_armor_{piece}": "{_json_string(name)} {piece.capitalize()}",' for piece in PIECES],
            "the item names",
        ),
    }
    for piece, path in TAGS.items():
        texts[path] = _add_tag_entry(files[path], f"dragonminez:{armor}_armor_{piece}")
    for path in (LANG, *TAGS.values()):
        _valid_json(texts[path], path)
    binaries = dict(zip(textures, (layer1, layer2)))
    return Change(texts, binaries, f"feat: patreon armor {name}", f"perk/{claim_id}-{armor}")


def hair_change(text: str, *, preset_name: str, code: str, patron: str, claim_id: int) -> Change:
    """The PR for a hairstyle perk. text: HairPresets.java on the base branch. The new preset gets the next id."""
    code = code.strip()
    if len(code) > MAX_HAIR_CODE or not HAIR_CODE.match(code):
        raise ShipError("That isn't a hair code from the editor (it starts with DMZF and has no spaces).")
    ids = [int(found) for found in re.findall(r"^\s*register\((\d+),\s*\"", text, flags=re.M)]
    if not ids:
        raise ShipError("Couldn't find the hair presets in HairPresets.java. Add it by hand.")
    name, patron = _safe(preset_name, "preset name"), _safe(patron, "Minecraft username")
    new_text = _insert_after_last(
        text, r'^\s*register\(\d+,\s*"', [f"// {name} Patreon ({patron})", f'register({max(ids) + 1}, "{code}");'],
        "the hair preset",
    )
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:30] or "hair"
    return Change({HAIR_PRESETS: new_text}, {}, f"feat: patreon hair preset {name}", f"perk/{claim_id}-{slug}")


def _body(*, perk_label: str, patron: str, ticket_url: str, made_by: str) -> str:
    made_by = re.sub(r"[^\w .'\-]", "", made_by) or "the perk team"  # nothing GitHub would turn into a mention
    return (
        f"Patreon perk: **{perk_label}** for `{patron}`, made by {made_by}.\n\n"
        f"Claim ticket: {ticket_url}\n\n"
        "Opened by BulmaAI from the claim card. Review it, build it and merge it by hand; the bot never merges."
    )


async def open_pr(
    github,
    *,
    base: str,
    claim_id: int,
    perk: str,
    perk_label: str,
    patron: str,
    ticket_url: str,
    made_by: str,
    display_name: str = "",
    layer1: bytes = b"",
    layer2: bytes = b"",
    code: str = "",
) -> str:
    """Reads the files on base, builds the change and opens the PR; its URL. ShipError for anything to fix first.
    The branch is fixed per claim and name, so a retry finds the PR it already opened instead of a second one."""
    base_sha = await github.head_sha(base)
    try:
        return await _open_pr(
            github, base, base_sha, claim_id=claim_id, perk=perk, perk_label=perk_label, patron=patron,
            ticket_url=ticket_url, made_by=made_by, display_name=display_name, layer1=layer1, layer2=layer2, code=code,
        )
    except UnicodeDecodeError:
        raise ShipError("A mod file isn't valid UTF-8, so the bot won't rewrite it. Add it by hand.") from None


async def _open_pr(github, base, base_sha, *, claim_id, perk, perk_label, patron, ticket_url, made_by, display_name,
                   layer1, layer2, code) -> str:
    if perk == "clothing":
        files = {path: (await github.get_file(path, base_sha, strict=True))[0] for path in ARMOR_FILES}
        existing = frozenset(await github.list_tree(base_sha))
        change = armor_change(
            files, display_name=display_name, layer1=layer1, layer2=layer2, patron=patron, claim_id=claim_id,
            existing_paths=existing,
        )
    elif perk == "hairstyle":
        text, _sha = await github.get_file(HAIR_PRESETS, base_sha, strict=True)
        change = hair_change(text, preset_name=display_name, code=code, patron=patron, claim_id=claim_id)
    else:
        raise ShipError("Only custom clothing and hairstyles can be shipped from Discord.")
    files = {path: text.encode("utf-8") for path, text in change.texts.items()} | change.binaries
    return await github.open_pr_with_files(
        base_sha=base_sha, base=base, branch=change.branch, files=files, message=change.title,
        title=change.title, body=_body(perk_label=perk_label, patron=patron, ticket_url=ticket_url, made_by=made_by),
    )
