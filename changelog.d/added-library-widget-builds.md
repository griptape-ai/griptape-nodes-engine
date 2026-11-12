- Libraries can ship their widgets as TypeScript source and have them built on your machine, behind
  the "Library widget builds" beta. Point `widget_tooling.node_executable` and
  `widget_tooling.npm_executable` at your Node and npm, then allow the build when you download,
  update, or switch a library, or send `PrepareLibraryWidgetsRequest`. The build runs `npm ci` and
  `npm run build` with lifecycle scripts off and a filtered environment. Until a build succeeds for
  the current checkout, the editor shows the library's normal controls instead of its widgets.
  [#6275](https://github.com/griptape-ai/griptape-nodes-engine/issues/6275)
