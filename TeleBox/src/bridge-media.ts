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

export type SavedStreamModeration = {
  known: boolean;
  allowed_auth: boolean;
  allowed_upload: boolean;
  allowed_report?: boolean;
  role?: string;
  personal_quota_bypass?: boolean;
  sanctions: Array<{ sanction_type: string; reason?: string; expires_at?: string | null }>;
};

export function moderationMessage(payload: SavedStreamModeration, action: "auth" | "upload"): string {
  const relevant = payload.sanctions.find((item) =>
    action === "auth"
      ? item.sanction_type === "login_ban"
      : item.sanction_type === "login_ban" || item.sanction_type === "upload_mute",
  );
  if (!relevant) return action === "auth" ? "该账号当前禁止登录" : "该账号当前禁止上传文件";
  const expires = relevant.expires_at
    ? new Date(relevant.expires_at).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })
    : "永久";
  return `${action === "auth" ? "该账号当前禁止登录" : "该账号当前禁止上传文件"}。理由：${relevant.reason || "违反平台规则"}。解除时间：${expires}`;
}

/** Split an external multi-file reservation without inflating total bytes. */
export function distributeUploadBytes(totalBytes: number, fileCount: number): number[] {
  if (!Number.isSafeInteger(totalBytes) || totalBytes < 0) throw new Error("invalid total bytes");
  if (!Number.isSafeInteger(fileCount) || fileCount <= 0) throw new Error("invalid file count");
  const perFile = Math.floor(totalBytes / fileCount);
  const remainder = totalBytes % fileCount;
  return Array.from({ length: fileCount }, (_, index) => perFile + (index < remainder ? 1 : 0));
}
