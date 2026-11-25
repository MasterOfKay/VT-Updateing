# Why `server/src` is not here

The provider ships two script backends and prefers Deno (preference 20) over
Node (10). The Deno one runs `server/src/generate_once.ts`; the Node one runs
the prebuilt `server/build/generate_once.js`.

Deno cannot load this tree: jsdom reaches for `cssstyle` through a CommonJS
path it does not support, and the script dies in `CSSStyleDeclaration-impl.js`.
Worse, when its `--version` probe times out instead, the error is not caught as
a provider rejection - it surfaces as a fatal yt-dlp ERROR and takes the whole
extraction down with it.

Removing `src/` makes the Deno backend find no script, reject cleanly, and hand
over to Node, which answers its version probe in about two seconds. Nothing at
runtime needs the TypeScript: `build/` is already compiled.

So Node 22 or newer has to be on PATH. Deno is still needed separately, for
yt-dlp's own `n` challenge - that one it solves fine.

To rebuild from source, fetch `server/` again from the upstream tag that
matches the `bgutil-ytdlp-pot-provider` version in `pyproject.toml`.
