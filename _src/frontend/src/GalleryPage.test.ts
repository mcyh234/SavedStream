import { describe, expect, it } from "vitest";
import { calculateTransferMetrics, formatRemainingTime, readEncryptedChunks } from "./GalleryPage";
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
