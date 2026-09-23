// The live-proof project's activity IO schemas (#642): the injected-schemas
// module the subprocess resolver loads via `--schemas` — the same map the
// worker harness (live-binding-harness.mjs) builds in-process. The embedding
// host supplying schemas IS the deployment contract (#620); this module is
// what that contract looks like for a `--ts-resolver-cmd` operator.
import * as z from "zod";

export default {
  "schemas:Item": z.object({ value: z.string() }),
};
