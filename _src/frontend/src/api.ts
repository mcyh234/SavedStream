import { translateNow } from "./I18n";

export class ApiError extends Error {
  status: number;
  code: string | null;

  constructor(message: string, status: number, code: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : null;
  if (!response.ok) {
    const detail = payload?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : detail?.code || payload?.code || translateNow(`请求失败 (${response.status})`, `Request failed (${response.status})`);
    throw new ApiError(message, response.status, detail?.code || payload?.code || null);
  }
  return payload as T;
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "INVALID_ADMIN_KEY") return translateNow("管理员密钥不正确", "The administrator key is incorrect");
    if (error.code === "INVALID_TELEGRAM_LOGIN_CODE") return translateNow("登录码无效、已过期或已被使用", "The login code is invalid, expired, or already used");
    if (error.code === "ACCOUNT_ACCESS_DENIED") return translateNow("你没有访问该托管账号的权限", "You do not have access to this managed account");
    if (error.code === "ACCESS_DISABLED") return translateNow("该账号的媒体访问已被禁用", "Media access for this account has been disabled");
    if (error.code === "ACCESS_DENIED") return translateNow("管理员未批准该账号访问媒体库", "The administrator has not approved this account");
    if (error.code === "PUBLIC_ALBUM_DISABLED") return translateNow("公开相册尚未获得管理员许可", "The public album has not been enabled by the administrator");
    if (error.code === "PUBLIC_KEY_REQUIRED") return translateNow("请输入公开相册访问密钥", "Enter the public album access key");
    if (error.code === "INVALID_PUBLIC_KEY") return translateNow("公开相册访问密钥不正确", "The public album access key is incorrect");
    if (error.code === "PUBLIC_KEY_NOT_CONFIGURED") return translateNow("管理员尚未配置公开相册访问密钥", "The administrator has not configured a public album key");
    if (error.code === "MEDIA_INDEX_PENDING") return translateNow("媒体索引仍在建立，请稍后重试", "The media index is still being built. Try again later");
    if (error.code === "FOLDER_CONFLICT") return translateNow("同一位置已存在同名文件夹", "A folder with this name already exists in this location");
    if (error.code === "FOLDER_NOT_FOUND") return translateNow("文件夹不存在或已被删除", "The folder does not exist or was deleted");
    if (error.code === "AUTH_REQUIRED") return translateNow("请先登录账号", "Sign in to use this feature");
    if (error.code === "TRAFFIC_LIMIT_REACHED") return translateNow("本月媒体流量额度已用尽，请联系管理员或等待下月重置", "This month's media traffic allowance has been exhausted");
    if (error.code === "TRAFFIC_LIMIT_EXCEEDS_CAPACITY") return translateNow("允许流量不能超过服务器月容量", "The traffic limit cannot exceed the server's monthly capacity");
    if (error.code === "TELEGRAM_UNAVAILABLE") return error.message;
    return error.message;
  }
  return error instanceof Error ? error.message : translateNow("操作失败，请重试", "The operation failed. Please try again");
}
