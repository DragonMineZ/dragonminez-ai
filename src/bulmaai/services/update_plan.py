"""Decides how a git update reaches the running bot: nothing, a hot reload of just what changed, or a restart.

Pure: it only reads the changed paths and the source/tests trees of the new revision. A module that does
`from x import name` keeps the old object after x reloads, so it has to reload too; `import x` and
`from pkg import module` read attributes at call time and pick the new code up on their own.
"""

import ast
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path


PACKAGE = "bulmaai"

# Module state a reload would lose or duplicate (DB pool, budget counters, the webhook route registry and
# server, the log forwarder) plus the process entry points. Anything that has to reload because of these
# means a real restart. A module can also opt in with `__hot_reload__ = False`.
PINNED_MODULES = frozenset(
    {
        "bulmaai",
        "bulmaai.__main__",
        "bulmaai.bot",
        "bulmaai.config",
        "bulmaai.logging_setup",
        "bulmaai.database",
        "bulmaai.database.db",
        "bulmaai.services.ai_budget",
        "bulmaai.services.discord_log_forwarding",
        "bulmaai.services.release_webhook",
        "bulmaai.services.scam_images",
        "bulmaai.utils.lifecycle",
    }
)

AGGREGATOR_MODULES = frozenset({"bulmaai.bot", "bulmaai.web.server"})
FULL_RESTART_FILES = frozenset({"requirements.txt"})
NOOP_PREFIXES = (".github/", ".idea/", "tests/", "scripts/", "data/knowledge/", "docs/", "AI/")
NOOP_FILES = frozenset({"README.md", "PRIVACY_POLICY.md", "LICENSE", ".gitignore"})
SCHEMA_FILE = "scripts/schema.sql"
SRC_PREFIX = f"src/{PACKAGE}/"
WEB_PACKAGE = f"{PACKAGE}.web"
WEB_STATIC_PREFIX = f"{SRC_PREFIX}web/static/"
SELF_UPDATE_EXTENSION = f"{PACKAGE}.cogs.self_update"


@dataclass(frozen=True)
class ImportGraph:
    # module -> modules it binds names from at import time (must reload with them)
    early: dict[str, set[str]]
    # module -> every intra-package module it imports, early or late (used for ordering and test picking)
    any: dict[str, set[str]]
    pinned: frozenset[str]


@dataclass
class UpdatePlan:
    mode: str  # "noop" | "hot" | "full"
    reasons: list[str] = field(default_factory=list)
    modules_to_reload: list[str] = field(default_factory=list)  # plain modules, dependencies first
    extensions_to_reload: list[str] = field(default_factory=list)  # dependencies first, self_update last
    rebind_panel: bool = False
    rerun_schema: bool = False
    clear_caches: bool = False
    tests: list[str] = field(default_factory=list)  # test modules covering what changed; a full plan runs them all
    check_imports: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.extensions_to_reload:
            parts.append("cogs: " + ", ".join(name.rsplit(".", 1)[-1] for name in self.extensions_to_reload))
        if self.modules_to_reload:
            parts.append("modules: " + ", ".join(name.removeprefix(PACKAGE + ".") for name in self.modules_to_reload))
        if self.rebind_panel:
            parts.append("panel rebind")
        if self.rerun_schema:
            parts.append("schema")
        if self.clear_caches:
            parts.append("caches")
        return "; ".join(parts) or "nothing to reload"


def module_name(path: Path, src_root: Path) -> str:
    parts = list(path.relative_to(src_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_from(node: ast.ImportFrom, current: str, is_package: bool) -> str | None:
    if node.level == 0:
        return node.module
    base = current.split(".") if is_package else current.split(".")[:-1]
    if node.level > 1:
        base = base[: len(base) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def _top_level_statements(body: list[ast.stmt]) -> Iterable[ast.stmt]:
    for node in body:
        if isinstance(node, ast.If):
            test = node.test
            if (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
                isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
            ):
                continue
            yield from _top_level_statements(node.body)
            yield from _top_level_statements(node.orelse)
        elif isinstance(node, ast.Try):
            yield from _top_level_statements(node.body)
            for handler in node.handlers:
                yield from _top_level_statements(handler.body)
            yield from _top_level_statements(node.orelse)
            yield from _top_level_statements(node.finalbody)
        else:
            yield node


def _imports(tree: ast.Module, current: str, is_package: bool, modules: set[str]) -> tuple[set[str], set[str]]:
    """(early, any) intra-package dependencies of one module."""

    def owning_module(name: str) -> str | None:
        while name:
            if name in modules:
                return name
            name = name.rpartition(".")[0]
        return None

    def targets(node: ast.stmt) -> list[tuple[str, bool]]:
        found: list[tuple[str, bool]] = []
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (target := owning_module(alias.name)) is not None:
                    found.append((target, False))
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from(node, current, is_package)
            if not base or not (base == PACKAGE or base.startswith(PACKAGE + ".")):
                return found
            for alias in node.names:
                submodule = f"{base}.{alias.name}"
                if submodule in modules:
                    found.append((submodule, False))
                elif (target := owning_module(base)) is not None:
                    found.append((target, True))
        return found

    early: set[str] = set()
    every: set[str] = set()
    top = set(map(id, _top_level_statements(tree.body)))
    for node in ast.walk(tree):
        for target, binds_names in targets(node) if isinstance(node, (ast.Import, ast.ImportFrom)) else ():
            if target == current:
                continue
            every.add(target)
            # Imports inside functions run on every call, so they always see the current module.
            if binds_names and id(node) in top:
                early.add(target)
    return early, every


def _opted_out(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__hot_reload__" for target in node.targets
        ):
            return isinstance(node.value, ast.Constant) and node.value.value is False
    return False


def build_import_graph(src_root: Path) -> ImportGraph:
    files = {module_name(path, src_root): path for path in (src_root / PACKAGE).rglob("*.py")}
    modules = set(files)
    early: dict[str, set[str]] = {}
    every: dict[str, set[str]] = {}
    pinned = set(PINNED_MODULES)
    for name, path in files.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            early[name], every[name] = set(), set()
            continue
        early[name], every[name] = _imports(tree, name, path.name == "__init__.py", modules)
        if _opted_out(tree):
            pinned.add(name)
    return ImportGraph(early=early, any=every, pinned=frozenset(pinned))


def _dependents(graph: dict[str, set[str]], roots: set[str]) -> set[str]:
    """roots plus every module that (transitively) imports one of them through graph's edges."""
    reverse: dict[str, set[str]] = {}
    for module, deps in graph.items():
        for dep in deps:
            reverse.setdefault(dep, set()).add(module)
    seen = set(roots)
    stack = list(roots)
    while stack:
        for importer in reverse.get(stack.pop(), ()):
            if importer not in seen:
                seen.add(importer)
                stack.append(importer)
    return seen


def _dependencies_first(modules: set[str], graph: ImportGraph) -> list[str]:
    ordered: list[str] = []
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(module: str) -> None:
        if module in done or module in visiting:
            return  # a cycle just keeps discovery order
        visiting.add(module)
        for dep in sorted(graph.any.get(module, ())):
            if dep in modules:
                visit(dep)
        visiting.discard(module)
        done.add(module)
        ordered.append(module)

    for module in sorted(modules):
        visit(module)
    return ordered


def select_tests(tests_root: Path, affected: set[str], changed_tests: set[str]) -> list[str]:
    """Test modules that import or are named after something affected, plus changed tests and their users."""
    if not tests_root.is_dir():
        return []
    imports: dict[str, set[str]] = {}
    for path in tests_root.glob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            imports[path.stem] = set()
            continue
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
        imports[path.stem] = names

    # Panel routes are only reachable through create_app, so their tests are found by name
    # (routes_status -> test_admin_panel_status, cogs/tickets -> test_tickets).
    stems = {module.rsplit(".", 1)[-1].removeprefix("routes_") for module in affected}

    def named_after(test: str) -> bool:
        return any(test == f"test_{stem}" or test.endswith(f"_{stem}") for stem in stems)

    selected = {
        name for name in imports if name in changed_tests or imports[name] & affected or named_after(name)
    }
    # A changed shared fixture (test_admin_panel's make_bot...) pulls in every test built on it.
    selected |= {name for name in imports if imports[name] & changed_tests}
    return sorted(name for name in selected if name.startswith("test"))


def plan_update(
    changed_paths: Iterable[str],
    *,
    src_root: Path,
    tests_root: Path,
    loaded_extensions: Iterable[str],
) -> UpdatePlan:
    changed = sorted(set(changed_paths))
    plan = UpdatePlan(mode="noop")
    if not changed:
        return plan

    graph = build_import_graph(src_root)
    extensions = set(loaded_extensions)
    known_modules = set(graph.any)
    changed_modules: set[str] = set()
    changed_tests: set[str] = set()
    data_hits: set[str] = set()

    def needs_full(reason: str) -> None:
        plan.mode = "full"
        plan.reasons.append(reason)

    for path in changed:
        if path in FULL_RESTART_FILES:
            needs_full(f"`{path}` changed (packages are installed once per process)")
        elif path == SCHEMA_FILE:
            plan.rerun_schema = True
        elif path.startswith("tests/") and path.endswith(".py"):
            changed_tests.add(Path(path).stem)
        elif path in NOOP_FILES or path.startswith(NOOP_PREFIXES) or not path.startswith(SRC_PREFIX):
            continue
        elif path.startswith(WEB_STATIC_PREFIX):
            # index.html carries a content hash of every static file, computed when web.server imports.
            changed_modules.add(f"{WEB_PACKAGE}.server")
        elif path.endswith(".py"):
            name = module_name(Path(path), Path("src"))
            if name in known_modules:
                changed_modules.add(name)
            # Deleted modules: whoever imported them changed too and shows up on its own.
        else:
            # Prompt text and other package data: cached readers recompute after a cache clear.
            plan.clear_caches = True
            data_hits.add(Path(path).name)

    to_reload = _dependents(graph.early, changed_modules)
    pinned_hit = sorted(to_reload & graph.pinned)
    for module in pinned_hit:
        why = "changed" if module in changed_modules else "imports something that changed"
        needs_full(f"`{module}` {why} and can't be hot-reloaded")

    # What the tests have to cover: everything reloading plus direct importers of the changed code, but not
    # through the modules that import nearly everything (their own tests would select the whole suite).
    direct_importers = {
        module
        for module, deps in graph.any.items()
        if deps & changed_modules and (module not in AGGREGATOR_MODULES or module in changed_modules)
    }
    affected = (to_reload | direct_importers | changed_modules) - (AGGREGATOR_MODULES - changed_modules)
    if data_hits:
        affected |= {module for module in known_modules if _mentions(src_root, module, data_hits)}
    plan.tests = select_tests(tests_root, affected, changed_tests)

    if plan.mode == "full":
        plan.check_imports = sorted(extensions | {f"{PACKAGE}.bot"})
        return plan

    ordered = _dependencies_first(to_reload, graph)
    plan.extensions_to_reload = [module for module in ordered if module in extensions]
    plan.modules_to_reload = [module for module in ordered if module not in extensions]
    # Its own reload cancels the poll loop that is running the update, so it goes last.
    if SELF_UPDATE_EXTENSION in plan.extensions_to_reload:
        plan.extensions_to_reload.remove(SELF_UPDATE_EXTENSION)
        plan.extensions_to_reload.append(SELF_UPDATE_EXTENSION)
    plan.rebind_panel = any(module == WEB_PACKAGE or module.startswith(WEB_PACKAGE + ".") for module in to_reload)
    plan.check_imports = ordered
    if to_reload or plan.rerun_schema or plan.clear_caches:
        plan.mode = "hot"
    return plan


def _mentions(src_root: Path, module: str, filenames: set[str]) -> bool:
    relative = Path(*module.split("."))
    for path in (src_root / relative.with_suffix(".py"), src_root / relative / "__init__.py"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            return any(name in text for name in filenames)
    return False
