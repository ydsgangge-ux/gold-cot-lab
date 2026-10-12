/** Thin typed wrapper over the local FastAPI endpoints. */

export type PitMode = 'published_at' | 'conservative' | 'report_date'

export interface MarketOption {
  report_type: string
  market_code: string
  market_name: string | null
  exchange_code: string | null
  reports: number
  first_date: string
  last_date: string
  last_published_at: string
}

export interface ReportTypeOption {
  code: string
  available: boolean
}

export interface PitModeOption {
  code: PitMode
  label: string
}

export interface Filters {
  report_types: ReportTypeOption[]
  markets: MarketOption[]
  latest_published_at: string | null
  calculation_versions: string[]
  price_symbols: Array<{ symbol: string; rows: number; first_date: string; last_date: string }>
  effective_params: { params: string; params_hash: string; calculation_version: string }
  pit_modes: PitModeOption[]
}

/** Per-indicator validity envelope; an invalid indicator is never a guess. */
export interface IndicatorState {
  is_valid: number
  invalid_reason: string | null
  sample_size: number | null
}

export interface SeriesRow {
  report_date: string
  published_at: string
  published_at_source: string
  market_name: string | null
  exchange_code: string | null
  open_interest: number | null
  mm_long: number | null
  mm_short: number | null
  net: number | null
  mm_spread: number | null
  prod_merc_long: number | null
  prod_merc_short: number | null
  swap_long: number | null
  swap_short: number | null
  other_rept_long: number | null
  other_rept_short: number | null
  nonrept_long: number | null
  nonrept_short: number | null
  d_net: number | null
  d_long: number | null
  d_short: number | null
  net_oi_pct: number | null
  net_streak: number | null
  divergence: string | null
  alert: string | null
  [key: string]: string | number | null | IndicatorState | undefined
}

export interface SeriesResponse {
  report_type: string
  market_code: string
  as_of: string
  pit_mode: PitMode
  count: number
  series: SeriesRow[]
}

/** One report's long / short / net for every trader category. */
export interface StructureRow {
  report_date: string
  published_at: string
  open_interest: number | null
  managed_money_long: number | null
  managed_money_short: number | null
  managed_money_net: number | null
  producer_long: number | null
  producer_short: number | null
  producer_net: number | null
  swap_long: number | null
  swap_short: number | null
  swap_net: number | null
  other_reportables_long: number | null
  other_reportables_short: number | null
  other_reportables_net: number | null
  nonreportable_long: number | null
  nonreportable_short: number | null
  nonreportable_net: number | null
  /** Lets `${category}_${metric}` lookups stay type-checked. */
  [key: string]: string | number | null
}

export interface StructureResponse {
  report_type: string
  market_code: string
  as_of: string
  pit_mode: PitMode
  count: number
  structure: StructureRow[]
}

export interface PositionBlock {
  open_interest: number | null
  mm_long: number | null
  mm_short: number | null
  net: number | null
  net_change: number | null
  net_oi_pct: number | null
  net_streak: number | null
}

export interface OverviewIndicator {
  value: number | string | null
  is_valid: number
  invalid_reason: string | null
  sample_size: number | null
  version: string
}

export interface Overview {
  report_type: string
  market_code: string
  market_name: string | null
  /** The date the positions were held. */
  data_as_of: string
  /** The moment the report became public — a different date on purpose. */
  published_at: string
  published_at_source: string
  as_of: string
  pit_mode: PitMode
  latest_version: string
  position: PositionBlock
  indicators: Record<string, OverviewIndicator>
  notes: string[]
}

export interface AdminStatus {
  counts: Record<string, number>
  markets: Array<{
    report_type: string
    market_code: string
    reports: number
    first_date: string
    last_date: string
    last_published_at: string
  }>
  indicator_versions: Array<{ calculation_version: string; rows: number; computed_at: string }>
  price_symbols: Array<{ symbol: string; rows: number; first_date: string; last_date: string }>
  last_batch: Record<string, string | number | null> | null
  last_error: {
    batch_id: string
    started_at: string
    status: string
    error_json: {
      message?: string
      attempts?: Array<{ source: string; reason: string; detail?: string }>
      sources_tried?: string[]
    } | null
  } | null
}

export interface DbStats {
  report_type: string
  counts: Record<string, number>
  integrity: { duplicate_keys: number; weekly_rows_without_raw: number }
  markets: Array<{
    market_code: string
    market_name: string | null
    reports: number
    first_date: string | null
    last_date: string | null
    gaps: Array<{ after: string; before: string; gap_days: number }>
    gap_count: number
  }>
}

export interface BatchSummary {
  batch_id: string
  source_file: string
  status: string
  rows_read: number
  rows_inserted: number
  rows_updated: number
  rows_unchanged: number
  rows_revised: number
  rows_by_market: Record<string, number>
  date_range: [string, string]
}

export interface ImportResponse {
  imported: number
  batches: BatchSummary[]
}

export interface UploadResponse extends ImportResponse {
  /** Where the uploaded file was archived before parsing. */
  saved_to: string
}

export interface PricePoint {
  trade_date: string
  open: number | null
  high: number | null
  low: number | null
  close: number | null
  volume: number | null
  source: string
  source_symbol: string | null
}

export interface PriceSeries {
  available: boolean
  symbol: string
  count: number
  date_range: [string, string] | null
  sources?: string[]
  notes?: string[]
  series: PricePoint[]
}

export interface PriceRefreshResponse {
  price: {
    status: string
    source?: string
    source_symbol?: string
    rows_read?: number
    rows_valid?: number
    counters?: Record<string, number>
    jump_candidates?: string[]
    issues?: string[]
    error?: {
      message: string
      attempts: Array<{ source: string; reason: string; detail?: string }>
    }
  }
  recompute: { rows_written: number } | null
}

export interface IngestionBatch {
  batch_id: string
  run_type: string
  status: string
  started_at: string
  finished_at: string | null
  duration_ms: number | null
  source_file: string | null
  rows_read: number
  rows_inserted: number
  rows_unchanged: number
  rows_revised: number
  validation_json: Array<{ level: string; rule: string; passed: boolean; checked: number; samples: string[]; note?: string | null }> | null
  issues_json: string[] | null
  error_json: { type: string; message: string } | null
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path)
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(`${path} → HTTP ${response.status} ${detail.slice(0, 300)}`)
  }
  return (await response.json()) as T
}

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(`${path} → HTTP ${response.status} ${detail.slice(0, 300)}`)
  }
  return (await response.json()) as T
}

export const api = {
  filters: () => getJson<Filters>('/api/meta/filters'),
  overview: (query: string) => getJson<Overview>(`/api/cot/overview?${query}`),
  series: (query: string) => getJson<SeriesResponse>(`/api/cot/series?${query}`),
  structure: (query: string) => getJson<StructureResponse>(`/api/cot/structure?${query}`),
  adminStatus: () => getJson<AdminStatus>('/api/admin/status'),
  dbStats: (reportType: string) =>
    getJson<DbStats>(`/api/admin/db/stats?report_type=${encodeURIComponent(reportType)}`),
  ingestions: (limit = 10) => getJson<{ count: number; batches: IngestionBatch[] }>(
    `/api/admin/ingestion?limit=${limit}`,
  ),
  ingestion: (batchId: string) => getJson<IngestionBatch>(`/api/admin/ingestion/${batchId}`),
  importCsv: (dryRun: boolean) =>
    postJson<ImportResponse>('/api/admin/import', { dry_run: dryRun }),
  uploadCsv: (filename: string, content: string, dryRun = false) =>
    postJson<UploadResponse>('/api/admin/upload', { filename, content, dry_run: dryRun }),
  refreshPrice: (recompute = true) =>
    postJson<PriceRefreshResponse>('/api/admin/refresh-price', { recompute }),
  priceSeries: (query = '') =>
    getJson<PriceSeries>(`/api/price/series${query ? `?${query}` : ''}`),
}

export const REPORT_TYPE_LABELS: Record<string, string> = {
  disaggregated_futures_only: '细分口径（本工具主用）',
  legacy_futures_only: 'Legacy 老口径（暂未导入）',
}

export const INDICATOR_LABELS: Record<string, string> = {
  net: '净持仓（看涨减看跌）',
  d_net: '净持仓周变化',
  d_long: '多头周变化',
  d_short: '空头周变化',
  net_oi_pct: '净持仓占市场比例',
  net_streak: '连续同向周数',
  pct_rank_3y: '3 年历史分位',
  pct_rank_5y: '5 年历史分位',
  cot_index_26w: 'COT Index（26 周）',
  cot_index_52w: 'COT Index（52 周）',
  cot_index_156w: 'COT Index（156 周）',
  ret_1w: '价格 1 周涨跌',
  ret_4w: '价格 4 周涨跌',
  ret_12w: '价格 12 周涨跌',
  trend_4w: '4 周价格趋势',
  trend_12w: '12 周价格趋势',
  divergence: '价格与仓位是否一致',
  alert: '极端仓位提示',
}

export const INVALID_REASON_LABELS: Record<string, string> = {
  insufficient_sample: '历史样本还不够（滚动窗口没填满）',
  flat_window: '窗口内没有任何波动，算不出比例',
  zero_oi: '未平仓量为 0 或缺失',
  no_price: '还没有黄金价格数据',
  no_prev: '缺少上一期的数据，算不出变化',
  flat_change: '本周变化正好是 0，算不出连续方向',
}