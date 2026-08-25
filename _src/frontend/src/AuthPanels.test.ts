import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { AccountAuthGate, accountStatusMessage } from "./AuthPanels";
import { I18nProvider } from "./I18n";

describe("account access states", () => {
  it("shows the new account approval and binding states", () => {
    expect(accountStatusMessage("pending")).toContain("等待管理员审核");
    expect(accountStatusMessage("disabled")).toContain("已被禁用");
    expect(accountStatusMessage("denied")).toContain("未批准");
    expect(accountStatusMessage("approved", "pending")).toContain("正在同步");
    expect(accountStatusMessage("approved", "ready", false)).toContain("尚未开放媒体库");
    expect(accountStatusMessage("pending", null, true, false, false)).toContain("/bind");
  });

  it("renders username login and registration instead of the retired Telegram code form", () => {
    const html = renderToStaticMarkup(
      createElement(I18nProvider, {
        children: createElement(AccountAuthGate, { registrationEnabled: true, approvalRequired: true, onAuthenticated: () => undefined }),
      }),
    );

    expect(html).toContain("登录 SavedStream");
    expect(html).toContain("注册");
    expect(html).toContain("用户名");
    expect(html).not.toContain("Telegram 身份验证");
    expect(html).not.toContain("一次性登录码");
  });
});
