import { describe, expect, it } from "vitest";
import { localizedText, resolveLanguage } from "./I18n";

describe("language selection", () => {
  it("prefers the saved language and otherwise follows the browser locale", () => {
    expect(resolveLanguage("en", "zh-CN")).toBe("en");
    expect(resolveLanguage("zh-CN", "en-US")).toBe("zh-CN");
    expect(resolveLanguage(null, "zh-Hans-CN")).toBe("zh-CN");
    expect(resolveLanguage(null, "en-US")).toBe("en");
  });

  it("returns the matching Chinese or English copy", () => {
    expect(localizedText("zh-CN", "媒体库", "Media library")).toBe("媒体库");
    expect(localizedText("en", "媒体库", "Media library")).toBe("Media library");
  });
});
