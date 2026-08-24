// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import type { FolderItem, MediaItem, TimelineYear } from "./types";
import GalleryPage from "./GalleryPage";
import { I18nProvider } from "./I18n";

const mediaCryptoMock = vi.hoisted(() => {
  let unstable = false;
  const totalFetches: string[] = [];
  const record = (url: string) => {
    totalFetches.push(url);
    return { data: new ArrayBuffer(16), headers: new Headers({ "X-SavedStream-Mime": "image/jpeg" }) };
  };
  const stableFetch = vi.fn(async (url: string) => record(url));
  return {
    stableFetch,
    totalFetches,
    setUnstable(value: boolean) { unstable = value; },
    useMediaCrypto: () => ({
      status: "ready",
      mode: "session",
      sessionId: "test-session",
      hasStoredKey: false,
      fingerprint: "fp-repro",
      error: "",
      prepare: vi.fn(),
      unlock: vi.fn(),
      reset: vi.fn(),
      fetchAndDecrypt: unstable ? vi.fn(async (url: string) => record(url)) : stableFetch,
    }),
  };
});

vi.mock("./MediaCrypto", () => ({
  useMediaCrypto: mediaCryptoMock.useMediaCrypto,
}));

const apiMock = vi.hoisted(() => {
  let folders: FolderItem[] = [];
  let timelineYears: TimelineYear[] = [];
  const api = vi.fn(async (url: string) => {
    if (url.startsWith("/api/accounts")) return { items: [], default_account: "" };
    if (url.startsWith("/api/notifications/unread-count")) return { count: 0 };
    if (url.startsWith("/api/notifications")) return { items: [], next_cursor: null, has_more: false, unread: 0 };
    if (url.startsWith("/api/folders")) return { items: folders };
    if (url.startsWith("/api/media/timeline")) return { years: timelineYears, index: { status: "ready" } };
    if (url.startsWith("/api/media")) {
      const items: MediaItem[] = Array.from({ length: 36 }, (_, index) => ({
        account_id: "default",
        id: index + 1,
        kind: "image",
        mime_type: "image/jpeg",
        size: 10_000,
        filename: "file-" + (index + 1) + ".jpg",
        original_title: "Original " + (index + 1),
        local_title: null,
        title: "Title " + (index + 1),
        caption: "",
        date: "2026-08-01T10:00:00+00:00",
        duration: null,
        width: 100,
        height: 100,
        has_thumbnail: true,
        thumbnail_url: "/api/media/" + (index + 1) + "/thumbnail?account=default&v=1",
        stream_url: "/api/media/" + (index + 1) + "/stream?account=default",
        visibility: "public",
      }));
      return { items, next_cursor: null, has_more: false };
    }
    return {};
  });
  return {
    api,
    setFolders(value: FolderItem[]) { folders = value; },
    setTimelineYears(value: TimelineYear[]) { timelineYears = value; },
  };
});

vi.mock("./api", () => ({
  api: apiMock.api,
  errorMessage: (reason: unknown) => String(reason),
}));

function folder(id: number, parentId: number, name: string, itemCount: number): FolderItem {
  return { id, parent_id: parentId, name, item_count: itemCount, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" };
}

describe("list view loop regression", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    apiMock.api.mockClear();
    mediaCryptoMock.stableFetch.mockClear();
    mediaCryptoMock.totalFetches.length = 0;
    mediaCryptoMock.setUnstable(false);
    apiMock.setFolders([]);
    apiMock.setTimelineYears([]);
    class FakeIntersectionObserver {
      callback: IntersectionObserverCallback;
      constructor(callback: IntersectionObserverCallback) { this.callback = callback; }
      observe(target: Element) {
        queueMicrotask(() => this.callback([{ isIntersecting: true, target, intersectionRatio: 1 } as IntersectionObserverEntry], this as unknown as IntersectionObserver));
      }
      disconnect() { /* no-op */ }
      unobserve() { /* no-op */ }
      takeRecords() { return []; }
    }
    vi.stubGlobal("IntersectionObserver", FakeIntersectionObserver);
    window.localStorage.clear();
  });

  afterEach(() => {
    act(() => { root.unmount(); });
    container.remove();
    vi.unstubAllGlobals();
  });

  async function mount() {
    await act(async () => {
      root.render(
        <I18nProvider>
          <GalleryPage isAdmin={false} />
        </I18nProvider>,
      );
    });
    for (let i = 0; i < 10; i++) {
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
    }
  }

  function clickButton(label: string) {
    const button = Array.from(container.querySelectorAll("button")).find((candidate) => {
      const text = (candidate.getAttribute("aria-label") || "") + " " + (candidate.textContent || "");
      return text.includes(label);
    });
    expect(button, "button " + label).toBeTruthy();
    act(() => { (button as HTMLButtonElement).click(); });
  }

  it("thumbnail fetches stay bounded after switching to list view", async () => {
    await mount();
    const listButton = Array.from(container.querySelectorAll("button"))
      .find((button) => (button.getAttribute("aria-label") || "").includes("List view"));
    expect(listButton).toBeTruthy();
    await act(async () => { (listButton as HTMLButtonElement).click(); });
    for (let i = 0; i < 10; i++) {
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
    }
    const afterSwitch = mediaCryptoMock.totalFetches.length;
    expect(afterSwitch).toBeLessThanOrEqual(72);
    for (let round = 0; round < 20; round++) {
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 10)); });
    }
    expect(mediaCryptoMock.totalFetches.length).toBe(afterSwitch);
  });

  it("does not loop even when the crypto context identity changes every render", async () => {
    mediaCryptoMock.setUnstable(true);
    await mount();
    const listButton = Array.from(container.querySelectorAll("button"))
      .find((button) => (button.getAttribute("aria-label") || "").includes("List view"));
    await act(async () => { (listButton as HTMLButtonElement).click(); });
    for (let i = 0; i < 10; i++) {
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
    }
    const afterSwitch = mediaCryptoMock.totalFetches.length;
    expect(afterSwitch).toBeLessThanOrEqual(72);
    for (let round = 0; round < 30; round++) {
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 10)); });
    }
    expect(mediaCryptoMock.totalFetches.length).toBe(afterSwitch);
  });

  it("renders folders inside the grid view with a breadcrumb instead of the sidebar", async () => {
    apiMock.setFolders([
      folder(1, 0, "相册A", 3),
      folder(2, 1, "子相册B", 0),
      folder(3, 0, "相册C", 5),
    ]);
    await mount();
    expect(container.querySelector(".folder-panel")).toBeNull();
    const cards = Array.from(container.querySelectorAll(".folder-card"));
    expect(cards.length).toBe(2);
    expect(container.textContent).toContain("相册A");
    expect(container.textContent).not.toContain("子相册B");
    const breadcrumb = container.querySelector(".folder-breadcrumb");
    expect(breadcrumb).toBeTruthy();
    const rootLabel = breadcrumb!.textContent || "";
    expect(rootLabel.includes("全部文件") || rootLabel.includes("All files")).toBe(true);
    clickButton("相册A");
    for (let i = 0; i < 6; i++) {
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
    }
    expect(container.querySelectorAll(".folder-card").length).toBe(1);
    expect(container.textContent).toContain("子相册B");
    const pathText = container.querySelector(".folder-breadcrumb")!.textContent || "";
    expect(pathText.includes("全部文件") || pathText.includes("All files")).toBe(true);
    expect(pathText).toContain("相册A");
  });

  it("renders folders as rows in list view", async () => {
    apiMock.setFolders([folder(1, 0, "相册A", 3)]);
    await mount();
    const listButton = Array.from(container.querySelectorAll("button"))
      .find((button) => (button.getAttribute("aria-label") || "").includes("List view"));
    await act(async () => { (listButton as HTMLButtonElement).click(); });
    for (let i = 0; i < 6; i++) {
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
    }
    const rows = Array.from(container.querySelectorAll(".folder-list-row"));
    expect(rows.length).toBe(1);
    expect(rows[0].textContent).toContain("相册A");
  });

  it("vertical timeline wheel sits between sidebar and main and reacts to wheel input", async () => {
    apiMock.setTimelineYears([{
      year: 2026,
      count: 2,
      months: [
        { month: "2026-08", count: 1, days: [] },
        { month: "2026-07", count: 1, days: [] },
      ],
    }]);
    await mount();
    const rail = container.querySelector(".timeline-rail");
    expect(rail).toBeTruthy();
    const nodes = Array.from(container.querySelectorAll(".timeline-wheel button"));
    expect(nodes.length).toBe(2);
    expect(nodes[0].getAttribute("aria-selected")).toBe("true");
    const tip = nodes[0].querySelector(".timeline-tip");
    expect(tip).toBeTruthy();
    expect(tip!.textContent).toContain("2026");
    await act(async () => {
      rail!.dispatchEvent(new WheelEvent("wheel", { deltaY: 120, bubbles: true, cancelable: true }));
    });
    for (let i = 0; i < 6; i++) {
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
    }
    const nodesAfter = Array.from(container.querySelectorAll(".timeline-wheel button"));
    expect(nodesAfter[1].getAttribute("aria-selected")).toBe("true");
    expect(nodesAfter[0].getAttribute("aria-selected")).toBe("false");
  });
});