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
        : detail?.code || payload?.code || `请求失败 (${response.status})`;
    throw new ApiError(message, response.status, detail?.code || payload?.code || null);
  }
  return payload as T;
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "INVALID_ADMIN_KEY") return "管理员密钥不正确";
    if (error.code === "INVALID_VIEWER_KEY") return "访问口令不正确";
    if (error.code === "TELEGRAM_UNAVAILABLE") return error.message;
    return error.message;
  }
  return error instanceof Error ? error.message : "操作失败，请重试";
}

