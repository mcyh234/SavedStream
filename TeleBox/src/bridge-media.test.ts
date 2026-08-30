import assert from "node:assert/strict";
import test from "node:test";
import {
  decodeBase64UrlHeader,
  distributeUploadBytes,
  inferMediaKind,
  moderationMessage,
  normalizeMediaMimeType,
  parseFilenameTimestamp,
  preferredExtensionForMime,
  shouldForceDocument,
  supportsTelegramStreaming,
  syncPaginationCursor,
  timelineDate,
  uploadBodyMatchesLength,
} from "./bridge-media";

test("full sync advances with the oldest raw Telegram message ID", () => {
  assert.equal(syncPaginationCursor("full", [100, 98, 96], 3), 96);
  assert.equal(syncPaginationCursor("full", [100, 98], 3), null);
});

test("incremental sync advances with the newest raw Telegram message ID", () => {
  assert.equal(syncPaginationCursor("incremental", [100, 102, 101], 3), 102);
  assert.equal(syncPaginationCursor("incremental", [0, -1], 2), null);
});

test("upload protocol decodes metadata and rejects a truncated body", () => {
  const encoded = Buffer.from("movie name.mp4", "utf8").toString("base64url");
  assert.equal(decodeBase64UrlHeader(encoded), "movie name.mp4");
  assert.equal(uploadBodyMatchesLength(128, 128), true);
  assert.equal(uploadBodyMatchesLength(127, 128), false);
});

test("external quota byte distribution keeps the exact original total", () => {
  assert.deepEqual(distributeUploadBytes(10, 3), [4, 3, 3]);
  assert.equal(distributeUploadBytes(10, 3).reduce((sum, size) => sum + size, 0), 10);
  assert.throws(() => distributeUploadBytes(10, 0));
});

test("sanction messages include the reason and release time", () => {
  const temporary = moderationMessage({
    known: true,
    allowed_auth: true,
    allowed_upload: false,
    sanctions: [{ sanction_type: "upload_mute", reason: "spam", expires_at: "2026-08-26T00:00:00Z" }],
  }, "upload");
  assert.match(temporary, /spam/);
  assert.match(temporary, /解除时间/);
  const permanent = moderationMessage({
    known: true,
    allowed_auth: false,
    allowed_upload: false,
    sanctions: [{ sanction_type: "login_ban", reason: "abuse", expires_at: null }],
  }, "auth");
  assert.match(permanent, /永久/);
});

test("camera-style image names are recognized and preserve their timeline timestamp", () => {
  assert.equal(inferMediaKind("IMG_20250923_003303_054.jpg", ""), "image");
  assert.equal(inferMediaKind("IMG_20250923_003303_054.jpg", "application/octet-stream"), "image");
  assert.equal(inferMediaKind("IMG_20250923_003303_054.HEIC", "application/octet-stream"), "image");
  assert.equal(inferMediaKind("clip_20250923_003303.mp4", "application/octet-stream"), "video");
  assert.equal(inferMediaKind("clip_20250923_003303.MOV", "application/octet-stream"), "video");
  assert.equal(inferMediaKind("clip_20250923_003303.mkv", "application/octet-stream"), "video");
  assert.equal(inferMediaKind("unknown.bin", "video/mp4"), "video");
  assert.equal(normalizeMediaMimeType("IMG_20250923_003303_054.jpg", "application/octet-stream"), "image/jpeg");
  assert.equal(normalizeMediaMimeType("clip_20250923_003303.MOV", ""), "video/quicktime");
  assert.equal(normalizeMediaMimeType("misleading.jpg", "application/pdf"), "application/pdf");
  assert.equal(inferMediaKind("misleading.jpg", "application/pdf"), "file");
  assert.equal(preferredExtensionForMime("video/mp4"), ".mp4");
  assert.equal(preferredExtensionForMime("image/jpeg; charset=binary"), ".jpg");

  const parsed = parseFilenameTimestamp("/uploads/IMG_20250923_003303_054.jpg");
  assert.ok(parsed);
  assert.equal(parsed?.toISOString(), "2025-09-23T00:33:03.054Z");
  assert.equal(parseFilenameTimestamp("PXL_20250923_003303054.jpg")?.toISOString(), "2025-09-23T00:33:03.054Z");
  assert.equal(parseFilenameTimestamp("Screenshot_2025-09-23-00-33-03.png")?.toISOString(), "2025-09-23T00:33:03.000Z");
  assert.equal(parseFilenameTimestamp("IMG_20250230_003303_054.jpg"), null);

  const timeline = timelineDate("IMG_20250923_003303_054.jpg", "2030-01-01T00:00:00.000Z");
  assert.equal(timeline?.toISOString(), "2025-09-23T00:33:03.054Z");
  assert.equal(timelineDate("untimestamped.jpg", "2030-01-01T00:00:00.000Z")?.toISOString(), "2030-01-01T00:00:00.000Z");
});

test("image and video uploads use Telegram documents, including files over 10 MiB", () => {
  assert.equal(shouldForceDocument(1024, "photo.jpg", "image/jpeg"), true);
  assert.equal(shouldForceDocument(1024, "clip.mp4", "video/mp4"), true);
  assert.equal(shouldForceDocument(10 * 1024 * 1024 + 1, "unknown.bin", "application/octet-stream"), true);
  assert.equal(shouldForceDocument(1024, "archive.zip", "application/zip"), false);
  assert.equal(supportsTelegramStreaming("clip.mp4", "video/mp4"), true);
  assert.equal(supportsTelegramStreaming("clip.mkv", "video/x-matroska"), false);
});
