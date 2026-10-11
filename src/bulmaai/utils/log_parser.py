import re
from dataclasses import dataclass, field

# ── Java version ─────────────────────────────────────────────────────────────
# Matches: "ModLauncher 10.0.9 starting: java version 17.0.3 by Microsoft"
_RE_JAVA_VERSION = re.compile(
    r"java version\s+([\d._]+)",
    re.IGNORECASE,
)
# Fallback: crash report "Java Version: 17.0.3, Microsoft"
_RE_JAVA_VERSION_ALT = re.compile(
    r"Java Version:\s*([\d._]+)",
    re.IGNORECASE,
)
# Fallback: system property dump "java.version: 17.0.3"
_RE_JAVA_VERSION_PROP = re.compile(
    r"java\.version[:\s]+([\d._]+)",
    re.IGNORECASE,
)

# ── Forge version ─────────────────────────────────────────────────────────────
# Matches: "Forge mod loading, version 47.0.19, for MC 1.20.1 ..."
# Also:    "MinecraftForge v47.0.19 Initialized"
_RE_FORGE_VERSION = re.compile(
    r"(?:Forge mod loading,\s*version\s*|MinecraftForge\s+v)([\d.]+)",
    re.IGNORECASE,
)
# Fallback: "--fml.forgeVersion, 47.0.19" from the launch args line
_RE_FORGE_VERSION_ALT = re.compile(
    r"--fml\.forgeVersion,\s*([\d.]+)",
    re.IGNORECASE,
)

# ── MC version ────────────────────────────────────────────────────────────────
# Matches: "Forge mod loading, version 47.0.19, for MC 1.20.1 with MCP ..."
_RE_MC_VERSION = re.compile(
    r"for MC\s+([\d.]+)",
    re.IGNORECASE,
)
# Fallback: "--fml.mcVersion, 1.20.1" from the launch args line
_RE_MC_VERSION_ALT = re.compile(
    r"--fml\.mcVersion,\s*([\d.]+)",
    re.IGNORECASE,
)

# ── Mod discovery (primary for latest.log) ────────────────────────────────────
# Matches Forge LOADING debug lines:
#   "Found valid mod file journeymap-....jar with {journeymap} mods - versions {5.8.0beta5}"
_RE_MOD_DISCOVERY = re.compile(
    r"Found valid mod file \S+ with \{([a-z0-9_-]+)} mods - versions \{([^}]+)}",
    re.IGNORECASE,
)

# ── Mod list fallbacks ────────────────────────────────────────────────────────

# 1. Crash Report Table (found in crash-*.txt files)
# Format: Filename | Name | Mod ID | Version | Status | Manifest
# Example: xenon.jar | Xenon | xenon | 0.3.31 | DONE | ...
# Every class stays on one line ([ \t], [^|\n]): under MULTILINE, \s or [^|] scan to the end of the file
# from every line start, which is quadratic and froze the whole bot on multi-MB logs.
_RE_MOD_CRASH_REPORT_TABLE = re.compile(
    r"^[^|\n]+\|[^|\n]+\|[ \t]*([a-z0-9_\-]+)[ \t]*\|[ \t]*([0-9][\w.\-+]+)",
    re.MULTILINE | re.IGNORECASE,
)

# 2. Forge Log Table (found in latest.log usually with leading pipe)
# Format: | Mod ID | Version | ...
_RE_MOD_LOG_TABLE = re.compile(
    r"^\|?[ \t]*(\w[\w\-]*)[ \t]*\|[ \t]*([\d][\w.\-+]*)[ \t]*\|",
    re.MULTILINE,
)

# 3. "Mod ID: 'modid', ... Version: 'x.x.x'" style
_RE_MOD_ENTRY = re.compile(
    r"(?:Mod ID:[ \t]*'?|Loading[ \t]+)([a-z_][a-z0-9_]*)'?[^\n]{0,300}?Version:[ \t]*'?([^'\";\n]+)",
    re.IGNORECASE,
)

# 4. Simple 2-space-separated list
_RE_MOD_SIMPLE = re.compile(
    r"^[ \t]{2,}([a-z_][a-z0-9_]{1,63})[ \t]{2,}([\d][^\s]{0,40})",
    re.MULTILINE,
)

# ── DragonMineZ version ───────────────────────────────────────────────────────
# Must look like a version (digits '.' digits ...): "dragonminez-2.1.3.jar", "DragonMineZ v2.2.0-beta",
# "DragonMineZ version: 2.1.3". A server host such as "dragonminez 22mkrei.enxada.gg" is not one.
_RE_DRAGONMINEZ_VERSION = re.compile(
    r"dragonminez[ \t\-_]*(?:v(?:ersion)?)?[ \t:]*v?(\d+(?:\.\d+)+(?:[\-+][0-9a-zA-Z.\-_+]*)?)",
    re.IGNORECASE,
)

# ── Log lines ─────────────────────────────────────────────────────────────────
# Matched one line at a time (never with MULTILINE over the whole file).
# Handles both timestamp formats:
#   [12:34:56] [Thread/ERROR]          <- standard
#   [24Jun2023 06:57:42.886] [Thread/FATAL]  <- extended (most launchers)
_RE_LOG_LINE = re.compile(r"\[[^]]+][ \t]*\[[^]]+/([A-Za-z]+)]")
# The "[timestamp] [thread/LEVEL] [logger/]:" prefix, stripped to dedupe errors by their message.
_RE_LOG_PREFIX = re.compile(r"\[[^]]+][ \t]*\[[^]]+](?:[ \t]*\[[^]]*])?:?[ \t]*")

# ── Stacktraces ───────────────────────────────────────────────────────────────
# An exception header line: "java.lang.NullPointerException: msg", "net.minecraft.ReportedException",
# 'Exception in thread "main" java.lang.Error: msg'. Fully-qualified names only, so bare "Error"
# words in normal lines don't start a block. Each group iteration ends at a '.', so no backtracking blowup.
_RE_EXCEPTION_HEADER = re.compile(
    r'(?:Exception in thread "[^"]*" )?(?:[A-Za-z_$][\w$]*\.)+[\w$]*(?:Exception|Error|Throwable)(?::|$)'
)
# Lines that continue an exception block (checked on the stripped line).
_TRACE_PREFIXES = ("at ", "Caused by:", "Suppressed:", "...")

# ── Noise ─────────────────────────────────────────────────────────────────────
# Well-known harmless ERROR lines. They're dropped from report.errors and never picked as the stacktrace.
# Keep this list short and only add messages that are harmless in every pack.
NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Embeddium disclaimer printed at ERROR whenever any mod mixes into it (Oculus and friends do).
    re.compile(r"Mod mixin into Embeddium internals detected"),
    # ModernFix diagnostic about another mod's registration timing, not a failure.
    re.compile(r"registerReloadListener at the wrong time"),
    # Update checkers failing to reach the internet (offline, firewall, expired cache).
    re.compile(r"io exception while checking versions|Online mod data expired|VersionChecker/", re.IGNORECASE),
    # Offline / non-premium launchers: Mojang auth, profile keys and the Yggdrasil key fetch fail harmlessly.
    re.compile(
        r"Failed to verify authentication|Failed to retrieve profile key pair|Failed to request yggdrasil public key",
        re.IGNORECASE,
    ),
    # Realms and telemetry can't reach Mojang's servers; the game doesn't need them.
    re.compile(r"RealmsClient|Realms Notification|[Tt]elemetry"),
    # A texture that failed to load falls back to the purple/black missing texture.
    re.compile(r"Using missing texture, unable to load"),
    # Optional-compat recipes for mods that aren't installed; the recipe is simply skipped.
    re.compile(r"Parsing error loading recipe"),
)


def is_noise(line: str) -> bool:
    """True if a log line is a well-known harmless error (see NOISE_PATTERNS). Our own mod's lines never are."""
    return "dragonminez" not in line.lower() and any(p.search(line) for p in NOISE_PATTERNS)

# ── OS / Memory ───────────────────────────────────────────────────────────────
# Crash report label (present when crash data is embedded in the log)
_RE_OS = re.compile(r"Operating System:\s*(.+)", re.IGNORECASE)
# ModLauncher startup line: "... java version 17.0.3 by Microsoft; OS Windows 10 arch amd64 ..."
_RE_OS_ALT = re.compile(
    r";\s*OS\s+(.+?)\s+arch",
    re.IGNORECASE,
)
_RE_MEMORY = re.compile(r"Memory:\s*(.+)", re.IGNORECASE)

_RE_MODLOADER = re.compile(r"ModLauncher running: args", re.IGNORECASE)

MAX_STACKTRACE_LEN = 800
MAX_ERRORS = 15


@dataclass
class LogReport:
    java_version: str | None = None
    mc_version: str | None = None
    forge_version: str | None = None
    dragonminez_version: str | None = None
    operating_system: str | None = None
    memory: str | None = None
    mods: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    # Parallel to errors: 1-based line number of each error in the text (lines split on "\n").
    error_line_numbers: list[int] = field(default_factory=list)
    stacktrace: str | None = None
    is_forge: bool = False


@dataclass
class _Block:
    """One exception block: an ERROR line, crash Description or exception header plus its continuation lines."""

    lines: list[str]
    anchored: bool = False  # starts at an ERROR/FATAL log line
    crash: bool = False  # crash report "Description:" block
    noise: bool = False
    has_trace: bool = False  # holds an exception header or stack frame, not just a message
    uncaught: bool = False  # "Exception in thread ..."
    mentions_dmz: bool = False
    size: int = 0

    def add(self, stripped: str) -> None:
        if stripped.startswith(("at ", "Caused by:", "Suppressed:")) or _RE_EXCEPTION_HEADER.match(stripped):
            self.has_trace = True
        if "dragonminez" in stripped.lower():
            self.mentions_dmz = True
        # Stop storing once past the card budget; the block still consumes its remaining lines.
        if self.size <= MAX_STACKTRACE_LEN:
            self.lines.append(stripped)
            self.size += len(stripped) + 1


def _continues_trace(line: str, stripped: str) -> bool:
    return line[:1] in (" ", "\t") or stripped.startswith(_TRACE_PREFIXES) or bool(_RE_EXCEPTION_HEADER.match(stripped))


def _pick_block(blocks: list[_Block], saw_log_line: bool) -> _Block | None:
    """Crash report first, then a block mentioning DragonMineZ, then the first non-noise ERROR trace."""
    candidates = [b for b in blocks if not b.noise and (b.crash or b.has_trace)]
    for wanted in (
        lambda b: b.crash,
        lambda b: b.mentions_dmz,
        lambda b: b.anchored,
        # A bare trace with no log around it (pasted .txt), or an uncaught exception.
        lambda b: b.uncaught or not saw_log_line,
    ):
        for block in candidates:
            if wanted(block):
                return block
    return None


def parse_log(text: str) -> LogReport:
    """Parse a Minecraft Forge latest.log OR crash report."""
    report = LogReport()

    # ── Java version ──────────────────────────────────────────────────────────
    for pat in (_RE_JAVA_VERSION, _RE_JAVA_VERSION_ALT, _RE_JAVA_VERSION_PROP):
        m = pat.search(text)
        if m:
            report.java_version = m.group(1).strip()
            break

    # ── Forge version ─────────────────────────────────────────────────────────
    m = _RE_FORGE_VERSION.search(text)
    if m:
        report.forge_version = m.group(1).strip()
    else:
        m = _RE_FORGE_VERSION_ALT.search(text)
        if m:
            report.forge_version = m.group(1).strip()

    # ── MC version ────────────────────────────────────────────────────────────
    m = _RE_MC_VERSION.search(text)
    if m:
        report.mc_version = m.group(1).strip()
    else:
        m = _RE_MC_VERSION_ALT.search(text)
        if m:
            report.mc_version = m.group(1).strip()

    # ── Forge detection ───────────────────────────────────────────────────────
    if _RE_MODLOADER.search(text):
        report.is_forge = True
    # If it's a crash report with "Forge" mentioned in the stacktrace or headers
    if "forge" in text.lower() and "minecraft crash report" in text.lower():
        report.is_forge = True

    # ── OS ────────────────────────────────────────────────────────────────────
    m = _RE_OS.search(text)
    if m:
        report.operating_system = m.group(1).strip()
    else:
        m = _RE_OS_ALT.search(text)
        if m:
            report.operating_system = m.group(1).strip()

    # ── Memory ────────────────────────────────────────────────────────────────
    m = _RE_MEMORY.search(text)
    if m:
        report.memory = m.group(1).strip()

    # ── Mods ──────────────────────────────────────────────────────────────────
    # 1. Primary: Forge LOADING debug discovery lines (most reliable in latest.log)
    for m in _RE_MOD_DISCOVERY.finditer(text):
        mod_id = m.group(1).strip().lower()
        version = m.group(2).strip()
        report.mods[mod_id] = version

    # 2. Crash Report Table (found in crash-*.txt files)
    # Captures 3rd column (Mod ID) and 4th column (Version)
    for m in _RE_MOD_CRASH_REPORT_TABLE.finditer(text):
        mod_id = m.group(1).strip().lower()
        version = m.group(2).strip()
        if mod_id not in report.mods and mod_id not in ("mod id", "modid"):
            report.mods[mod_id] = version

    # 3. Log Table (found in latest.log, usually starts with |)
    for m in _RE_MOD_LOG_TABLE.finditer(text):
        mod_id = m.group(1).strip().lower()
        version = m.group(2).strip()
        if mod_id not in report.mods:
            report.mods[mod_id] = version

    # 4. "Mod ID: / Loading " style
    for m in _RE_MOD_ENTRY.finditer(text):
        mod_id = m.group(1).strip().lower()
        version = m.group(2).strip()
        if mod_id not in report.mods:
            report.mods[mod_id] = version

    # 5. Simple indented list
    for m in _RE_MOD_SIMPLE.finditer(text):
        mod_id = m.group(1).strip().lower()
        version = m.group(2).strip()
        if mod_id not in report.mods:
            report.mods[mod_id] = version

    # ── DragonMineZ version ───────────────────────────────────────────────────
    dmz = report.mods.get("dragonminez")
    if dmz:
        report.dragonminez_version = dmz
    else:
        m = _RE_DRAGONMINEZ_VERSION.search(text)
        if m:
            version = m.group(1).strip()
            report.dragonminez_version = version[:-4] if version.lower().endswith(".jar") else version

    # ── Errors & stacktrace ───────────────────────────────────────────────────
    # Split on "\n" only so line numbers match a plain viewer of the same file.
    is_crash_report = "---- Minecraft Crash Report ----" in text
    seen_errors: set[str] = set()
    blocks: list[_Block] = []
    current: _Block | None = None
    saw_log_line = False

    def add_error(stripped: str, line_no: int) -> None:
        prefix = _RE_LOG_PREFIX.match(stripped)
        key = stripped[prefix.end():] if prefix else stripped
        if key in seen_errors or len(report.errors) >= MAX_ERRORS:
            return
        seen_errors.add(key)
        report.errors.append(stripped)
        report.error_line_numbers.append(line_no)

    for line_no, raw in enumerate(text.split("\n"), start=1):
        line = raw.rstrip("\r")
        stripped = line.strip()

        # Any timestamped log line ends the current block; ERROR/FATAL lines start a new one.
        m = _RE_LOG_LINE.match(line)
        if m:
            saw_log_line = True
            current = None
            if m.group(1).upper() in ("ERROR", "FATAL"):
                noisy = is_noise(stripped)
                if not noisy:
                    add_error(stripped, line_no)
                current = _Block([], anchored=True, noise=noisy)
                current.add(stripped)
                blocks.append(current)
            continue

        # Crash report "Description:" opens the crash block; its exception follows after a blank line.
        if is_crash_report and stripped.startswith("Description:"):
            add_error(stripped, line_no)
            current = _Block([], crash=True)
            current.add(stripped)
            blocks.append(current)
            continue

        if not stripped:
            if not (current and current.crash and len(current.lines) == 1):
                current = None
            continue

        if current is not None and _continues_trace(line, stripped):
            current.add(stripped)
            continue

        if _RE_EXCEPTION_HEADER.match(stripped):
            current = _Block([], uncaught=stripped.startswith("Exception in thread"))
            current.add(stripped)
            blocks.append(current)
            continue

        current = None

    block = _pick_block(blocks, saw_log_line)
    if block:
        full_trace = "\n".join(block.lines)
        if len(full_trace) > MAX_STACKTRACE_LEN:
            full_trace = full_trace[:MAX_STACKTRACE_LEN] + "\n... (truncated)"
        report.stacktrace = full_trace

    return report
