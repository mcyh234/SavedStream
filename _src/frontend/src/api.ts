import { translateNow } from "./I18n";

const BROWSER_ID_KEY = "savedstream-browser-id-v1";
let fallbackBrowserId = "";

function newBrowserId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}

export function browserId(): string {
  try {
    const stored = window.localStorage.getItem(BROWSER_ID_KEY);
    if (stored) return stored;
    const created = newBrowserId();
    window.localStorage.setItem(BROWSER_ID_KEY, created);
    return created;
  } catch {
    if (!fallbackBrowserId) fallbackBrowserId = newBrowserId();
    return fallbackBrowserId;
  }
}

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
  if (!headers.has("X-SavedStream-Browser-ID")) {
    headers.set("X-SavedStream-Browser-ID", browserId());
  }
  if (options.body && !(typeof FormData !== "undefined" && options.body instanceof FormData) && !headers.has("Content-Type")) {
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
    if (error.code === "INVALID_CREDENTIALS") return translateNow("用户名或密码不正确", "The username or password is incorrect");
    if (error.code === "INVALID_USERNAME") return translateNow("用户名格式无效", "The username format is invalid");
    if (error.code === "INVALID_REGISTRATION_KEY") return translateNow("注册密钥不正确", "The registration key is incorrect");
    if (error.code === "REGISTRATION_KEY_REQUIRED") return translateNow("请输入有效的自定义注册密钥", "Enter a valid custom registration key");
    if (error.code === "REGISTRATION_DISABLED") return translateNow("管理员当前未开放新用户注册", "New user registration is currently disabled");
    if (error.code === "USERNAME_TAKEN") return translateNow("该用户名已被使用", "This username is already in use");
    if (error.code === "INVALID_REGISTRATION") return translateNow("注册资料不符合要求：用户名需为 3–32 位，密码需为 12–128 位", "The registration details are invalid: usernames must be 3–32 characters and passwords 12–128 characters");
    if (error.code === "AUTH_DISABLED") return translateNow("该账号已被禁用", "This account has been disabled");
    if (error.code === "AUTH_DENIED") return translateNow("该账号的访问申请未获批准", "This account's access request was denied");
    if (error.code === "AUTH_CHALLENGE_NOT_FOUND") return translateNow("Telegram 确认链接已失效，请返回后重试", "The Telegram confirmation link has expired. Go back and try again");
    if (error.code === "AUTH_FLOW_REPLACED") return translateNow("登录入口已升级，请刷新页面后使用账号登录", "The sign-in flow has changed. Refresh the page and sign in with your account");
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
    if (error.code === "UPLOAD_QUOTA_REACHED") return translateNow("你的 24 小时上传限额已达到上限，请稍后重试", "Your 24-hour upload quota has been reached. Try again later");
    if (error.code === "UPLOAD_MUTED") return translateNow("你的账号当前被禁止上传，请查看站内信了解原因和解除时间", "Your account is currently blocked from uploading. Check your mailbox for details");
    if (error.code === "LOGIN_BANNED") return translateNow("你的账号当前被禁止登录，请联系管理员或等待处罚解除", "Your account is currently banned from signing in");
    if (error.code === "REPORTING_DISABLED") return translateNow("你的举报功能已被管理员暂停，请查看站内信", "Your reporting access has been suspended. Check your mailbox");
    if (error.code === "SELF_LIKE_FORBIDDEN") return translateNow("不能给自己上传的资源点赞", "You cannot like your own upload");
    if (error.code === "SELF_REPORT_FORBIDDEN") return translateNow("不能举报自己上传的资源", "You cannot report your own upload");
    if (error.code === "REPORT_ALREADY_OPEN") return translateNow("你已经举报过该资源，请等待管理员处理", "You already reported this media. Wait for moderation");
    if (error.code === "UPLOAD_LENGTH_MISMATCH") return translateNow("上传内容长度与文件大小不一致，请重试", "The uploaded content length does not match the file size");
    if (error.code === "TRAFFIC_LIMIT_EXCEEDS_CAPACITY") return translateNow("允许流量不能超过服务器月容量", "The traffic limit cannot exceed the server's monthly capacity");
    if (error.code === "TELEGRAM_UNAVAILABLE") return error.message;
    return error.message;
  }
  return error instanceof Error ? error.message : translateNow("操作失败，请重试", "The operation failed. Please try again");
}
