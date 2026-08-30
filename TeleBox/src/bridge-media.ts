export type SyncMode = "full" | "incremental";

/** Telegram rejects large photos and may otherwise reinterpret image/video
 * uploads as photo/media messages. Keep these uploads as documents so the
 * original filename and bytes are retained. */
export const TELEGRAM_PHOTO_LIMIT_BYTES = 10 * 1024 * 1024;

export type MediaKind = "image" | "video" | "audio" | "file";

const IMAGE_EXTENSIONS = new Set([
  ".jpg",
  ".jpeg",
  ".jpe",
  ".jfif",
  ".png",
  ".gif",
  ".webp",
  ".bmp",
  ".dng",
  ".ico",
  ".jxl",
  ".svg",
  ".tif",
  ".tiff",
  ".heic",
  ".heif",
  ".avif",
]);
const VIDEO_EXTENSIONS = new Set([
  ".mp4",
  ".m4v",
  ".mov",
  ".mkv",
  ".webm",
  ".avi",
  ".wmv",
  ".flv",
  ".3gp",
  ".3g2",
  ".mpeg",
  ".mpe",
  ".mpg",
  ".ts",
  ".mts",
  ".m2ts",
  ".ogv",
  ".vob",
]);
const AUDIO_EXTENSIONS = new Set([
  ".mp3",
  ".m4a",
  ".aac",
  ".flac",
  ".wav",
  ".ogg",
  ".oga",
  ".opus",
  ".wma",
]);

const EXTENSION_MIME_TYPES = new Map<string, string>([
  [".avif", "image/avif"],
  [".bmp", "image/bmp"],
  [".dng", "image/x-adobe-dng"],
  [".gif", "image/gif"],
  [".heic", "image/heic"],
  [".heif", "image/heif"],
  [".ico", "image/x-icon"],
  [".jfif", "image/jpeg"],
  [".jpe", "image/jpeg"],
  [".jpeg", "image/jpeg"],
  [".jpg", "image/jpeg"],
  [".jxl", "image/jxl"],
  [".png", "image/png"],
  [".svg", "image/svg+xml"],
  [".tif", "image/tiff"],
  [".tiff", "image/tiff"],
  [".webp", "image/webp"],
  [".3gp", "video/3gpp"],
  [".3g2", "video/3gpp2"],
  [".avi", "video/x-msvideo"],
  [".flv", "video/x-flv"],
  [".m2ts", "video/mp2t"],
  [".m4v", "video/x-m4v"],
  [".mkv", "video/x-matroska"],
  [".mov", "video/quicktime"],
  [".mp4", "video/mp4"],
  [".mpeg", "video/mpeg"],
  [".mpe", "video/mpeg"],
  [".mpg", "video/mpeg"],
  [".mts", "video/mp2t"],
  [".ogv", "video/ogg"],
  [".ts", "video/mp2t"],
  [".vob", "video/dvd"],
  [".webm", "video/webm"],
  [".wmv", "video/x-ms-wmv"],
  [".aac", "audio/aac"],
  [".flac", "audio/flac"],
  [".m4a", "audio/mp4"],
  [".mp3", "audio/mpeg"],
  [".oga", "audio/ogg"],
  [".ogg", "audio/ogg"],
  [".opus", "audio/opus"],
  [".wav", "audio/wav"],
  [".wma", "audio/x-ms-wma"],
]);

const GENERIC_BINARY_MIME_TYPES = new Set([
  "",
  "application/binary",
  "application/force-download",
  "application/octet-stream",
  "application/x-binary",
  "binary/octet-stream",
]);

const MIME_PREFERRED_EXTENSIONS = new Map<string, string>([
  ["image/avif", ".avif"],
  ["image/gif", ".gif"],
  ["image/heic", ".heic"],
  ["image/heif", ".heif"],
  ["image/jpeg", ".jpg"],
  ["image/png", ".png"],
  ["image/tiff", ".tiff"],
  ["image/webp", ".webp"],
  ["video/3gpp", ".3gp"],
  ["video/mp2t", ".mts"],
  ["video/mp4", ".mp4"],
  ["video/mpeg", ".mpeg"],
  ["video/quicktime", ".mov"],
  ["video/webm", ".webm"],
  ["video/x-matroska", ".mkv"],
  ["audio/aac", ".aac"],
  ["audio/flac", ".flac"],
  ["audio/mpeg", ".mp3"],
  ["audio/ogg", ".ogg"],
  ["audio/wav", ".wav"],
]);

function extensionOf(filename: string | null | undefined): string {
  const value = String(filename || "").trim().toLowerCase();
  const dot = value.lastIndexOf(".");
  return dot >= 0 ? value.slice(dot) : "";
}

export function preferredExtensionForMime(mimeType?: string | null): string {
  const bare = String(mimeType || "").split(";", 1)[0].trim().toLowerCase();
  return MIME_PREFERRED_EXTENSIONS.get(bare) || "";
}

/** Recover a useful MIME type when Telegram kept only a generic document MIME. */
export function normalizeMediaMimeType(
  filename?: string | null,
  mimeType?: string | null,
): string {
  const supplied = String(mimeType || "").trim();
  const bare = supplied.split(";", 1)[0].trim().toLowerCase();
  if (!GENERIC_BINARY_MIME_TYPES.has(bare)) return supplied;
  return EXTENSION_MIME_TYPES.get(extensionOf(filename)) || supplied || "application/octet-stream";
}

/** Infer a media kind from MIME first, then fall back to the filename. */
export function inferMediaKind(
  filename?: string | null,
  mimeType?: string | null,
  mediaClass?: string | null,
): MediaKind {
  const mime = String(mimeType || "").split(";", 1)[0].trim().toLowerCase();
  if (mime.startsWith("video/")) return "video";
  if (mime.startsWith("image/")) return "image";
  if (mime.startsWith("audio/")) return "audio";
  if (mediaClass === "MessageMediaPhoto") return "image";
  if (!GENERIC_BINARY_MIME_TYPES.has(mime)) return "file";
  const extension = extensionOf(filename);
  if (VIDEO_EXTENSIONS.has(extension)) return "video";
  if (IMAGE_EXTENSIONS.has(extension)) return "image";
  if (AUDIO_EXTENSIONS.has(extension)) return "audio";
  return "file";
}

/** Only mark containers Telegram reliably accepts as streamable.
 * Other video formats remain ordinary documents instead of failing with
 * VideoContentTypeError during upload. */
export function supportsTelegramStreaming(
  filename?: string | null,
  mimeType?: string | null,
): boolean {
  const extension = extensionOf(filename);
  const normalized = normalizeMediaMimeType(filename, mimeType).toLowerCase();
  return normalized === "video/mp4" || extension === ".mp4" || extension === ".m4v";
}

/** Force image/video uploads through Telegram's document path.
 *
 * We intentionally force all image/video documents (not only files over the
 * 10 MiB photo limit): this preserves the original filename and avoids the
 * client silently converting a file into a Telegram photo. The size check is
 * kept explicit for callers that only know the MIME/extension at runtime. */
export function shouldForceDocument(
  size: number,
  filename?: string | null,
  mimeType?: string | null,
): boolean {
  const kind = inferMediaKind(filename, mimeType);
  return kind === "image" || kind === "video" || Number(size) > TELEGRAM_PHOTO_LIMIT_BYTES;
}

/** Parse camera-style names such as IMG_20250923_003303_054.jpg. */
export function parseFilenameTimestamp(filename?: string | null): Date | null {
  const base = String(filename || "").split(/[\\/]/).pop() || "";
  const compact = base.match(
    /(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})(?:[_-]?(\d{1,6}))?(?!\d)/i,
  );
  const separated = compact
    ? null
    : base.match(
        /(?<!\d)((?:19|20)\d{2})[-_](\d{2})[-_](\d{2})[T _-](\d{2})[-_:](\d{2})[-_:](\d{2})(?:[._-](\d{1,6}))?(?!\d)/i,
      );
  const match = compact || separated;
  if (!match) return null;
  const [, yearText, monthText, dayText, hourText, minuteText, secondText, fractionText = ""] = match;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const hour = Number(hourText);
  const minute = Number(minuteText);
  const second = Number(secondText);
  const millisecond = fractionText ? Number(fractionText.slice(0, 3).padEnd(3, "0")) : 0;
  if (month < 1 || month > 12 || day < 1 || day > 31 || hour > 23 || minute > 59 || second > 59) {
    return null;
  }
  const value = new Date(Date.UTC(year, month - 1, day, hour, minute, second, millisecond));
  // Date.UTC normalizes invalid month lengths (e.g. 20250231), so verify the
  // components after construction before accepting the timestamp.
  if (
    value.getUTCFullYear() !== year ||
    value.getUTCMonth() !== month - 1 ||
    value.getUTCDate() !== day ||
    value.getUTCHours() !== hour ||
    value.getUTCMinutes() !== minute ||
    value.getUTCSeconds() !== second
  ) {
    return null;
  }
  return value;
}

/** Prefer a timestamp embedded in the original filename, else Telegram date. */
export function timelineDate(
  filename?: string | null,
  telegramDate?: string | number | Date | null,
): Date | null {
  const fromFilename = parseFilenameTimestamp(filename);
  if (fromFilename) return fromFilename;
  if (telegramDate == null || telegramDate === "") return null;
  const value = telegramDate instanceof Date
    ? new Date(telegramDate.getTime())
    : typeof telegramDate === "number"
      ? new Date(telegramDate < 2_000_000_000 ? telegramDate * 1000 : telegramDate)
      : new Date(telegramDate);
  return Number.isNaN(value.getTime()) ? null : value;
}

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
