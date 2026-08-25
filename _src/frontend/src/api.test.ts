// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, browserId } from "./api";

describe("api browser identity", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.restoreAllMocks();
  });

  it("persists one browser id and sends it with requests", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);

    const first = browserId();
    const second = browserId();
    await api("/api/status");

    expect(first).toBe(second);
    const request = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(request.headers).get("X-SavedStream-Browser-ID")).toBe(first);
  });
});
