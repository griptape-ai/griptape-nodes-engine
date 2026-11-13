- Node libraries can now ship their own docs in a `docs/` folder next to their manifest, and the
  engine can read them. `GetNodeDocumentationRequest` returns a node's page and parameters (name a
  node on the canvas with `node_name` to get that node's own parameters),
  `ListLibraryDocumentationRequest` lists a library's pages, nodes, templates, skills and the MCP
  tools it exposes to agents, and `GetLibraryDocumentationPageRequest` reads one page. Each node entry says whether it
  is a node or a workflow node. A page in
  `docs/mcp_tools/` named after a tool's request type documents that tool, and a template without a
  page gets its description and thumbnail from its own file. The manifest has a new optional
  `documentation_path` (library schema `0.15.0`) for docs kept somewhere other than `docs/`.
