export type MediaKind = "all" | "video" | "image" | "audio" | "file";

export interface PublicStatus {
  configuration_ok: boolean;
  telegram_authenticated: boolean;
  telegram_state: string;
  telegram_error: string | null;
  access_restricted: boolean;
  viewer_authenticated: boolean;
  admin_authenticated: boolean;
  media_authenticated: boolean;
  access_status: "unauthenticated" | "pending" | "approved" | "disabled" | "denied" | "admin";
  access_account_id: string | null;
  helper_bot_username: string | null;
}

export interface MediaItem {
  account_id: string;
  id: number;
  kind: Exclude<MediaKind, "all">;
  mime_type: string;
  size: number;
  filename: string;
  original_title: string;
  local_title: string | null;
  title: string;
  caption: string;
  date: string;
  duration: number | null;
  width: number | null;
  height: number | null;
  has_thumbnail: boolean;
  thumbnail_url: string | null;
  stream_url: string;
}

export interface AccountStatus {
  id: string;
  label: string;
  state: string;
  error?: string | null;
  user_id?: string | null;
  username?: string | null;
}

export interface HelperBotStatus {
  configured: boolean;
  username: string | null;
  token: string | null;
}

export interface BindingItem {
  telegram_user_id: string;
  account_id: string;
  created_at: number;
  enabled: number;
}

export interface IngestJob {
  id: number;
  account_id: string;
  status: string;
  error: string | null;
  created_at: number;
}

export interface AccessUser {
  telegram_user_id: string;
  account_id: string;
  username: string | null;
  display_name: string;
  status: "pending" | "approved" | "disabled" | "denied";
  requested_at: string;
  approved_at: string | null;
  last_login_at: string;
}

export interface MediaPage {
  items: MediaItem[];
  next_cursor: number | null;
  has_more: boolean;
}

export interface TelegramAuthStatus {
  state: string;
  authenticated: boolean;
  expires_at: string | null;
  error: string | null;
  url?: string;
}

export interface AdminSettings {
  cache_max_gb: number;
  cache_bytes: number;
  cache_files: number;
  access_restricted: boolean;
  viewer_key_configured: boolean;
  telegram: TelegramAuthStatus;
  accounts: AccountStatus[];
  helper_bot: HelperBotStatus;
  bindings: BindingItem[];
  ingest_jobs: IngestJob[];
  access_users: AccessUser[];
}
