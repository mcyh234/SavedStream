import { describe, expect, it } from "vitest";
import { sanctionExpiry } from "./AdminPage";

describe("sanction expiry validation", () => {
  const now = Date.parse("2026-08-25T12:00:00Z");

  it("keeps permanent sanctions explicit and calculates presets from now", () => {
    expect(sanctionExpiry({ sanction_type: "upload_mute", enabled: true, reason: "x", duration: "permanent", customExpiry: "" }, now)).toBeNull();
    expect(sanctionExpiry({ sanction_type: "upload_mute", enabled: true, reason: "x", duration: "1h", customExpiry: "" }, now)).toBe("2026-08-25T13:00:00.000Z");
  });

  it("rejects a missing or expired custom time instead of silently making it permanent", () => {
    expect(() => sanctionExpiry({ sanction_type: "login_ban", enabled: true, reason: "x", duration: "custom", customExpiry: "" }, now)).toThrow();
    expect(() => sanctionExpiry({ sanction_type: "login_ban", enabled: true, reason: "x", duration: "custom", customExpiry: "2026-08-25T11:59:00Z" }, now)).toThrow();
    expect(sanctionExpiry({ sanction_type: "login_ban", enabled: true, reason: "x", duration: "custom", customExpiry: "2026-08-25T14:00:00Z" }, now)).toBe("2026-08-25T14:00:00.000Z");
  });
});
