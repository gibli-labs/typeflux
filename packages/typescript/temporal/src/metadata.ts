/** Typed, runtime-neutral metadata contributions. Contributions receive no redaction exemption. */
export interface MetadataContributor<Context> {
  contribute(context: Context): Readonly<Record<string, unknown>>;
}

/** Reject namespace collisions instead of silently overwriting another contributor's evidence. */
export function collectMetadata<Context>(context: Context, contributors: readonly MetadataContributor<Context>[]): Record<string, unknown> {
  const metadata: Record<string, unknown> = Object.create(null);
  for (const contributor of contributors) {
    for (const [key, value] of Object.entries(contributor.contribute(context))) {
      if (Object.hasOwn(metadata, key)) throw new Error(`duplicate metadata contribution: ${key}`);
      metadata[key] = value;
    }
  }
  return metadata;
}
