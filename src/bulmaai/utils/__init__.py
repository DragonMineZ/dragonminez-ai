from src.bulmaai.utils import docs_search, patreon_whitelist

TOOLS = {
  "docs_search": {
      "schema": { ... },
      "func": docs_search.run,
  },
  "start_patreon_whitelist_flow": {
      "schema": { ... },
      "func": patreon_whitelist.start_flow,
  },
}