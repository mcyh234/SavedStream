export type MediaKind = "all" | "video" | "image" | "audio" | "file";

export type MediaVisibility = "public" | "private" | "hidden";

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
  public_album_enabled: boolean;
  public_key_configured: boolean;
  public_authenticated: boolean;
  media_session_id: string | null;
  registration_enabled: boolean;
  registration_requires_approval: boolean;
  binding_sync_status: "pending" | "ready" | "error" | "not_required" | null;
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
  visibility: MediaVisibility;
  hidden?: boolean;
  requested_visibility?: "public" | "private";
  review_status?: "not_required" | "pending" | "approved" | "rejected" | "revoked";
  review_reason?: string | null;
  reviewed_at?: string | null;
  reviewed_by?: string | null;
  review_batch_id?: string | null;
  source_ingest_job_id?: number | null;
  submitter_telegram_user_id?: string | null;
  owner_user_id?: number | null;
  upload_source?: string;
  upload_batch_id?: string | null;
  like_count?: number;
  liked_by_me?: boolean;
  owned_by_me?: boolean;
  deleted?: boolean;
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
  banned?: number;
}

export interface IngestJob {
  id: number;
  account_id: string;
  status: string;
  error: string | null;
  created_at: number;
  requested_visibility?: "private" | "public";
  review_status?: "not_required" | "pending" | "approved" | "rejected" | "revoked";
  review_reason?: string | null;
  review_batch_id?: string | null;
  submitter_telegram_user_id?: string | null;
  source_file_size?: number;
  file_count?: number;
  saved_message_id?: number | null;
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
  next_cursor: string | number | null;
  has_more: boolean;
  scope?: "public" | "private" | "all";
  view?: "private" | "square" | "my_public" | "liked" | null;
  index?: MediaSyncState;
}

export interface TimelineDay {
  day: string;
  count: number;
  first_message_id: number;
  last_message_id: number;
}

export interface TimelineMonth {
  month: string;
  count: number;
  days: TimelineDay[];
}

export interface TimelineYear {
  year: number;
  count: number;
  months: TimelineMonth[];
}

export interface TimelineResponse {
  account_id: string | null;
  scope: "public" | "private" | "all";
  view?: "private" | "square" | "my_public" | "liked" | null;
  years: TimelineYear[];
  index: MediaSyncState | null;
}

export interface MediaSyncState {
  account_id: string;
  status: string;
  mode: string;
  cursor: number | null;
  high_watermark_id: number | null;
  indexed_count: number;
  last_sync_at: string | null;
  error: string | null;
  updated_at: string;
}

export interface UploadJob {
  id: string;
  account_id: string;
  filename: string;
  mime_type: string;
  size: number;
  status: string;
  phase: string;
  progress: number;
  bytes_sent: number;
  message_id: number | null;
  error: string | null;
  owner_user_id?: number | null;
  requested_visibility?: "public" | "private";
  review_status?: "not_required" | "pending" | "approved" | "rejected" | "revoked";
  batch_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface TrafficSettings {
  enabled: boolean;
  monthly_capacity_bytes: number;
  monthly_limit_bytes: number;
  monthly_capacity_gb: number;
  monthly_limit_gb: number;
  warning_percent: number;
  admin_bypass: boolean;
  timezone: string;
  updated_at: string | null;
}

export interface TrafficUsage {
  bucket_type: string;
  bucket_start: string;
  bytes_in: number;
  bytes_out: number;
  bytes_total: number;
  request_count: number;
  remaining_bytes: number | null;
  usage_percent: number;
  updated_at: string | null;
}

export interface TrafficSummary {
  settings: TrafficSettings;
  usage: TrafficUsage;
  active_requests: number;
  active_streams: number;
  active_uploads: number;
  inbound_bps: number;
  outbound_bps: number;
}

export interface TrafficSeriesPoint {
  bucket_start: string;
  bytes_in: number;
  bytes_out: number;
  bytes_total: number;
  request_count: number;
}

export interface FolderItem {
  id: number;
  parent_id: number;
  name: string;
  created_by?: string | null;
  created_at: string;
  updated_at: string;
  item_count: number;
}

export interface NotificationItem {
  id: number;
  user_id: number;
  kind: string;
  title: string;
  body: string;
  link: string | null;
  is_read: boolean;
  created_at: string;
  read_at: string | null;
  recipient?: string;
}

export interface NotificationPage {
  items: NotificationItem[];
  next_cursor: number | null;
  has_more: boolean;
  unread: number;
}

export interface StorageAlert {
  level: "critical" | "warning" | "info";
  code: string;
  title: string;
  message: string;
}

export interface StorageRecommendation {
  code: string;
  action: "cleanup_backups" | "clear_cache" | string;
  title: string;
  message: string;
}

export interface StorageSnapshot {
  host: { total_bytes: number; used_bytes: number; free_bytes: number; percent_used: number };
  data_volume: { total_bytes: number; used_bytes: number; free_bytes: number; percent_used: number };
  data_volume_path: string;
  backups: { bytes: number; count: number; writable: boolean; configured: boolean };
  cache: { bytes: number; files: number; limit_bytes: number; percent_used: number };
  database_bytes: number;
  probe_path: string;
  alerts: StorageAlert[];
  recommendations: StorageRecommendation[];
}

export interface BackupEntry {
  stamp: string;
  size_bytes: number;
  file_count: number;
  modified_at: string;
  code_size_bytes: number;
  volume_size_bytes: number;
  code_files: string[];
  volume_files: string[];
  has_code: boolean;
  has_volumes: boolean;
  deletable: boolean;
}

export interface BackupListResponse {
  configured: boolean;
  writable: boolean;
  dir?: string;
  items: BackupEntry[];
}

export interface SystemBackupSettings {
  enabled: boolean;
  cron_expr: string;
  timezone: string;
  account_id: string | null;
  passphrase_configured: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  last_status: string;
  last_error: string | null;
  updated_at?: string | null;
}

export interface SystemBackupRecord {
  id: string;
  filename: string;
  source: "scheduled" | "manual" | "upload" | "telegram" | string;
  status: string;
  created_at: string;
  size_bytes: number;
  sha256: string;
  account_id?: string | null;
  message_id?: number | null;
  manifest_json?: string;
  error?: string | null;
  imported_at?: string | null;
}

export interface SystemBackupJob {
  id: string;
  backup_id?: string | null;
  trigger: string;
  status: string;
  phase: string;
  progress: number;
  attempts: number;
  error?: string | null;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
}

export interface AuthUser {
  id: number;
  username: string | null;
  role: string;
  status: string;
  telegram_user_id: string | null;
  telegram_username?: string | null;
  display_name?: string | null;
  account_id?: string | null;
  binding_sync_status?: "pending" | "ready" | "error" | "not_required";
  ban_reason?: string | null;
  created_at?: string;
  sanctions?: UserSanction[];
}

export interface UserSanction {
  id: number;
  user_id: number;
  sanction_type: "upload_mute" | "login_ban" | "report_mute";
  reason: string;
  starts_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

export interface ContentDeletionJob {
  id: string;
  target_user_id: number;
  reason: string;
  status: "queued" | "running" | "completed" | "partial" | "failed" | "cancelled";
  total_items: number;
  processed_items: number;
  failed_items: number;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface MediaReportItem {
  id: number;
  reporter_user_id: number;
  reporter_name: string;
  reason_code: string;
  details: string | null;
  status: string;
  resolution_reason?: string | null;
  created_at: string;
}

export interface MediaReportGroup {
  account_id: string;
  message_id: number;
  media_title: string;
  owner_user_id: number | null;
  owner_name: string;
  visibility: MediaVisibility;
  deleted: boolean;
  review_status: string;
  report_count: number;
  reports: MediaReportItem[];
}

export interface HelperRateLimit {
  per_user_files_24h: number;
  per_user_bytes_24h: number;
  per_user_concurrent: number;
  max_file_bytes: number;
  global_files_per_minute: number;
  max_album_items: number;
  max_album_bytes: number;
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
  auth_users: AuthUser[];
  public_album_enabled: boolean;
  public_key_configured: boolean;
  public_key_version: number;
  registration_enabled: boolean;
  registration_key_configured: boolean;
  registration_key_version: number;
  registration_key_fingerprint: string;
  registration_requires_approval: boolean;
  media_sync: MediaSyncState[];
  upload_jobs: UploadJob[];
  traffic: TrafficSummary;
  helper_rate_limit: HelperRateLimit;
}
