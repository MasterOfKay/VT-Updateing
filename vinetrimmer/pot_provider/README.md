# PO token provider

YouTube will only serve the full DASH format set - AV1 and every dubbed audio
language - to a request that carries a PO token. Without one it answers from a
fallback client, which offers H.264 and the default audio language only.

`server/` is the JS side of [bgutil-ytdlp-pot-provider][1], vendored here so a
checkout carries it instead of relying on a clone in someone's home directory.
The Python side is the `bgutil-ytdlp-pot-provider` package, which `poetry
install` pulls in, and `vinetrimmer/services/youtube.py` points the two at each
other through `POT_SERVER_HOME`.

## Setting it up on a new machine

The dependencies are platform specific (`canvas` builds a native addon), so
they are not vendored. With Node 22 or newer on PATH:

    cd vinetrimmer/pot_provider/server
    npm ci

That is all. The service picks the provider up on its own and logs a warning if
`node_modules` is missing.

A JS runtime also has to be reachable for yt-dlp itself, to solve YouTube's `n`
challenge. `binaries/deno.exe` covers Windows; on Linux and macOS install deno,
node or bun and make sure it is on PATH.

## Keeping it in step

The Python package and this directory must stay on the same major version, or
the provider warns about a mismatch. To update, bump the dependency in
`pyproject.toml` and replace `server/` from the matching release tag of the
upstream repository, then run `npm ci` again.

[1]: https://github.com/Brainicism/bgutil-ytdlp-pot-provider
