# android-use

This repository owns the platform-neutral Android device CLI. Keep product-specific
flows, package names, deep links, and business assertions in consumer repositories.

- Prefer direct device E2E over broad test matrices; accept any online ADB transport.
- Keep the runtime dependency-free beyond Python, ADB, and optional documented host tools.
- Do not add arbitrary shell-string remote execution; integrations must pass argv.
- User documentation belongs in `README.md`; implementation and maintainer details belong
  in `docs/DEVELOPMENT.md`.
