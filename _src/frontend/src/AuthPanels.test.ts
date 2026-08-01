import { describe, expect, it } from "vitest";
import { accessStatusMessage } from "./AuthPanels";

describe("Telegram access states", () => {
  it("shows explicit pending, disabled, and denied states", () => {
    expect(accessStatusMessage("pending")).toContain("等待管理员批准");
    expect(accessStatusMessage("disabled")).toContain("已被禁用");
    expect(accessStatusMessage("denied")).toContain("未批准");
    expect(accessStatusMessage("approved")).toBe("");
  });
});
