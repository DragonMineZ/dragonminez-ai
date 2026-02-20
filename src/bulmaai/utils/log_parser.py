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

# ── Mod discovery (primary) ───────────────────────────────────────────────────
# Matches Forge LOADING debug lines:
#   "Found valid mod file journeymap-....jar with {journeymap} mods - versions {5.8.0beta5}"
_RE_MOD_DISCOVERY = re.compile(
    r"Found valid mod file \S+ with \{([a-z0-9_\-]+)} mods - versions \{([^}]+)}",
    re.IGNORECASE,
)

# ── Mod list fallbacks ────────────────────────────────────────────────────────
# Crash-report pipe table (lines may start with | in some launcher outputs):
#   "|forge-1.20.1-47.0.19-universal.jar |Forge |forge |47.0.19 |COMMON_SET|...|"
_RE_MOD_TABLE = re.compile(
    r"^\|?\s*(\w[\w\-]*)\s*\|\s*([\d][\w.\-+]*)\s*\|",
    re.MULTILINE,
)
# "Mod ID: 'modid', ... Version: 'x.x.x'" style
_RE_MOD_ENTRY = re.compile(
    r"(?:Mod ID:\s*'?|Loading\s+)([a-z_][a-z0-9_]*)'?.*?Version:\s*'?([^'\";\n]+)",
    re.IGNORECASE,
)
# Simple 2-space-separated list
_RE_MOD_SIMPLE = re.compile(
    r"^\s{2,}([a-z_][a-z0-9_]{1,63})\s{2,}(\d\S{0,40})",
    re.MULTILINE,
)

# ── DragonMineZ version ───────────────────────────────────────────────────────
_RE_DRAGONMINEZ_VERSION = re.compile(
    r"dragonminez[\s\-_]*(?:v(?:ersion)?)?[\s:]*([0-9][0-9a-zA-Z.\-_]+)",
    re.IGNORECASE,
)

# ── Error lines ───────────────────────────────────────────────────────────────
# Handles both timestamp formats:
#   [12:34:56] [Thread/ERROR]          <- standard
#   [24Jun2023 06:57:42.886] [Thread/FATAL]  <- extended (most launchers)
_RE_ERROR_LINE = re.compile(
    r"^\[[^]]+]\s*\[[^]]+/(ERROR|FATAL)]",
    re.MULTILINE,
)

# ── Stacktrace start ──────────────────────────────────────────────────────────
# Tightened: only triggers on fully-qualified Java exception names or block headers,
# not on bare "Error" words that appear in normal INFO lines.
_RE_STACKTRACE_START = re.compile(
    r"(?:java|net|com|org|io)\.\S+(?:Exception|Error)[^\n]*"
    r"|Caused by:\s*\S+"
    r"|--- [^\-]+ ---",
)

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
    stacktrace: str | None = None
    is_forge: bool = False


def parse_log(text: str) -> LogReport:
    """Parse a Minecraft Forge 1.20.1 latest.log and return a structured report."""
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

    # 2. Crash-report pipe table (present if crash data is embedded)
    for m in _RE_MOD_TABLE.finditer(text):
        mod_id = m.group(1).strip().lower()
        version = m.group(2).strip()
        if mod_id not in report.mods:
            report.mods[mod_id] = version

    # 3. "Mod ID: / Loading " style
    for m in _RE_MOD_ENTRY.finditer(text):
        mod_id = m.group(1).strip().lower()
        version = m.group(2).strip()
        if mod_id not in report.mods:
            report.mods[mod_id] = version

    # 4. Simple indented list
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
            report.dragonminez_version = m.group(1).strip()

    # ── Errors & stacktrace ───────────────────────────────────────────────────
    lines = text.splitlines()
    error_lines: list[str] = []
    trace_lines: list[str] = []
    in_trace = False
    consecutive_blanks = 0

    for line in lines:
        if _RE_ERROR_LINE.match(line):
            error_lines.append(line.strip())
            in_trace = True
            consecutive_blanks = 0
            trace_lines.append(line.strip())
            continue

        if _RE_STACKTRACE_START.search(line):
            in_trace = True
            consecutive_blanks = 0

        if in_trace:
            stripped = line.strip()
            if stripped:
                consecutive_blanks = 0
                trace_lines.append(stripped)
            else:
                consecutive_blanks += 1
                # Two consecutive blank lines reliably signal the end of a block.
                # One blank may just be a separator between "Caused by:" chains.
                if consecutive_blanks >= 2:
                    in_trace = False
                    consecutive_blanks = 0

    report.errors = error_lines[:15]

    if trace_lines:
        full_trace = "\n".join(trace_lines)
        if len(full_trace) > MAX_STACKTRACE_LEN:
            full_trace = full_trace[:MAX_STACKTRACE_LEN] + "\n... (truncated)"
        report.stacktrace = full_trace

    return report
