export type SyncMode = "full" | "incremental";

/** Return the raw Telegram ID used to continue a page walk. */
export function syncPaginationCursor(
  mode: SyncMode,
  messageIds: readonly number[],
  pageSize: number,
): number | null {
  if (messageIds.length < pageSize || messageIds.length === 0) return null;
  const ids = messageIds.filter((id) => Number.isSafeInteger(id) && id > 0);
  if (!ids.length) return null;
  return mode === "full" ? Math.min(...ids) : Math.max(...ids);
}

export function decodeBase64UrlHeader(value: string | undefined): string {
  if (!value) return "";
  try {
    const padded = value + "=".repeat((4 - (value.length % 4)) % 4);
    return Buffer.from(padded, "base64url").toString("utf8");
  } catch {
    return "";
  }
}

export function uploadBodyMatchesLength(actualSize: number, expectedSize: number): boolean {
  return Number.isSafeInteger(actualSize) && Number.isSafeInteger(expectedSize) && actualSize === expectedSize;
}
