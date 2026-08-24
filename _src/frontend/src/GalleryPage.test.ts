import { describe, expect, it } from "vitest";
import { calculateTransferMetrics, encryptedThumbnailUrl, formatRemainingTime, mediaDay, normalizeMediaItem, readEncryptedChunks, visibilityLabel } from "./GalleryPage";
import type { MediaItem } from "./types";

describe("download progress", () => {
  it("calculates percentage, rolling speed, and remaining time", () => {
    const metrics = calculateTransferMetrics(10_000, 2_500, 2_000, 1_000);
    expect(metrics.percent).toBe(25);
    expect(metrics.speed).toBe(2_000);
    expect(metrics.etaSeconds).toBe(3.75);
  });

  it("handles an unknown transfer rate and formats long waits", () => {
    expect(calculateTransferMetrics(1_000, 0, 0, 0).etaSeconds).toBeNull();
    expect(formatRemainingTime(null)).toContain("计算中");
    expect(formatRemainingTime(90)).toBe("约剩余 2 分钟");
    expect(formatRemainingTime(3_900)).toBe("约剩余 1 小时 5 分钟");
  });

  it("stops before requesting another encrypted chunk when canceled", async () => {
    const controller = new AbortController();
    controller.abort();
    const fetchAndDecrypt = () => Promise.resolve({ data: new ArrayBuffer(1), headers: new Headers() });
    const item = { id: 1, account_id: "default", size: 1024, mime_type: "application/octet-stream" } as MediaItem;
    const mediaCrypto = { fetchAndDecrypt } as unknown as Parameters<typeof readEncryptedChunks>[1];
    await expect(readEncryptedChunks(item, mediaCrypto, () => undefined, controller.signal)).rejects.toMatchObject({ name: "AbortError" });
  });
});

describe("encrypted thumbnail url", () => {
  it("swaps to the encrypted route and appends the device cache key", () => {
    expect(encryptedThumbnailUrl("/api/media/1/thumbnail?account=alpha&size=4&v=2", "fp-123"))
      .toBe("/api/media/1/encrypted-thumbnail?account=alpha&size=4&v=2&device=fp-123");
    expect(encryptedThumbnailUrl("/api/media/1/thumbnail?account=alpha", "fp-123"))
      .toBe("/api/media/1/encrypted-thumbnail?account=alpha&device=fp-123");
  });

  it("leaves the url unchanged when the fingerprint is unavailable", () => {
    expect(encryptedThumbnailUrl("/api/media/1/thumbnail?account=alpha", ""))
      .toBe("/api/media/1/encrypted-thumbnail?account=alpha");
  });
});

describe("visibility labels", () => {
  const tr = (chinese: string, english: string) => chinese;
  it("maps the three visibility states", () => {
    expect(visibilityLabel("public", tr)).toBe("公开");
    expect(visibilityLabel("private", tr)).toBe("私有");
    expect(visibilityLabel("hidden", tr)).toBe("隐藏");
  });
});

describe("media date compatibility", () => {
  it("accepts the legacy message_date field without crashing date grouping", () => {
    const item = normalizeMediaItem({
      id: 1,
      account_id: "default",
      message_date: "2026-08-01T10:00:00+00:00",
    } as unknown as MediaItem);
    expect(item.date).toBe("2026-08-01T10:00:00+00:00");
    expect(mediaDay(item.date)).toBe("2026-08-01");
  });

  it("groups malformed dates into a safe unknown bucket", () => {
    expect(mediaDay(undefined)).toBe("unknown");
    expect(mediaDay("not-a-date")).toBe("unknown");
  });
});
