# Multimodal Claim Review

This example shows a workflow that declares evidence artifacts in YAML and routes them into a typed prompt.

- `runtime.artifacts` limits local artifact roots, source kinds, media types, and file size.
- `activities.definitions[].artifacts` extracts artifact groups from workflow input.
- Inline prompt messages can combine text parts with `artifact_group` parts.
- Manifests record safe artifact provenance: source kind, role, media type, hash, and size. They do not record raw paths, URLs, file IDs, or file contents.

Example input:

```json
{
  "claim_id": "CLM-1001",
  "claimant_summary": "Kitchen water damage after a supply line leak.",
  "documents": ["claim-note.txt", "repair-estimate.pdf"],
  "photos": ["kitchen-photo.svg"]
}
```

The OpenAI chat-completions adapter can attach images, text-like files, local PDFs, and provider-prepared file handles through content parts. Provider-prepared handles are represented with `provider_file` sources.
