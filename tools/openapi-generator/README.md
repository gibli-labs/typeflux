# OpenAPI generation

Run `npm ci --prefix tools/openapi-generator` from the repository root once after
checkout or a tool lockfile update. Existing package commands (`npm run generate`
in `clients/typescript`, and `pnpm --filter @typeflux/temporal-controlplane
generate:contract` from the root) use this installation.

Product builds use TypeScript 7. This private tool retains TypeScript 5 because
openapi-typescript 7.13 consumes its JavaScript compiler API. Its own npm lockfile
keeps both generators on exactly the same dependencies, independent of package
manager hoisting and the product compiler. Generation does not install packages
or require network access. CI regenerates both outputs and rejects drift.

Remove the legacy compiler isolation when a stable generator supports the native
compiler API and both generated contracts remain unchanged. Dependabot tracks
this directory; the compiler-major hold applies only here.
