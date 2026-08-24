import assert from "node:assert/strict";
import test from "node:test";
import { decodeBase64UrlHeader, syncPaginationCursor, uploadBodyMatchesLength } from "./bridge-media";

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
