/**
 * Middle-truncate: versioned types (Name.<digest>) and digests differ at the
 * tail as often as the head, so keep both ends visible.
 */
export function middleTruncate(value: string): string {
  return value.length > 22 ? `${value.slice(0, 10)}…${value.slice(-8)}` : value;
}
