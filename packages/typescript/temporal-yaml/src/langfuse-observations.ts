/** SDK-independent Observations v2 pagination shared by erasure and control-plane reads. */
export interface LangfuseObservationRow {
  id?: string;
  traceId?: string | null;
  name?: string | null;
  traceName?: string | null;
  tags?: string[] | null;
  userId?: string | null;
  startTime?: string;
  endTime?: string | null;
  parentObservationId?: string | null;
  isRootObservation?: boolean;
  metadata?: unknown;
  level?: string;
}
export interface LangfuseObservationQuery {
  limit?: number;
  cursor?: string;
  fields?: string;
  expandMetadata?: string;
  traceId?: string;
  fromStartTime?: string;
  toStartTime?: string;
  filter?: string;
}
export interface LangfuseObservationPage {
  data: LangfuseObservationRow[];
  meta: { cursor?: string | null };
}
export const LANGFUSE_METADATA_KEYS = "typeflux,typeflux_moderation,typeflux.execution_manifest,typeflux.activity_execution_manifest";
export async function readLangfuseObservations(
  getPage: (query: LangfuseObservationQuery) => Promise<unknown>,
  query: LangfuseObservationQuery,
  maxPages: number,
): Promise<{ rows: LangfuseObservationRow[]; complete: boolean; pagesScanned: number }> {
  if (!Number.isInteger(maxPages) || maxPages < 1) throw new Error("Langfuse maxPages must be a positive integer");
  const rows: LangfuseObservationRow[] = [];
  const cursors = new Set<string>();
  let cursor = query.cursor;
  if (cursor !== undefined) cursors.add(cursor);
  for (let page = 1; page <= maxPages; page++) {
    const response = await getPage({ ...query, ...(cursor === undefined ? {} : { cursor }) });
    if (typeof response !== "object" || response === null || !("data" in response) || !Array.isArray(response.data) ||
        !("meta" in response) || typeof response.meta !== "object" || response.meta === null ||
        response.data.some((row: unknown) => typeof row !== "object" || row === null)) {
      throw new Error("Invalid Langfuse Observations v2 page; completeness is unknown");
    }
    rows.push(...response.data);
    const next = (response.meta as { cursor?: unknown }).cursor;
    if (next == null || next === "") return { rows, complete: true, pagesScanned: page };
    if (typeof next !== "string" || cursors.has(next)) throw new Error("Invalid or repeated Langfuse observation cursor");
    cursors.add(next);
    cursor = next;
  }
  return { rows, complete: false, pagesScanned: maxPages };
}
