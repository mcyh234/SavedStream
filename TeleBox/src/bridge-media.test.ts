import assert from "node:assert/strict";
import test from "node:test";
import { decodeBase64UrlHeader, distributeUploadBytes, moderationMessage, syncPaginationCursor, uploadBodyMatchesLength } from "./bridge-media";

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
