/**
 * The selected environment is URL state (`?env=`), validated at the root
 * route and inherited by every page; when absent it defaults to the first
 * declared environment — the same fallback `App.tsx` applied.
 */

import { useSearch } from "@tanstack/react-router";

import { useShellData } from "../queries";

export function useEnv(): string {
  const search = useSearch({ strict: false }) as { env?: string };
  const shell = useShellData();
  return search.env ?? shell.data?.environments[0]?.id ?? "";
}
